"""Verifies what fakes cannot: fd 3 survives, the heredoc is intact, rollback
on exit works, and SIGINT interrupts a command without killing the session.

Runs against the development container integra19 / integra_db_19_presta, which
is a sandbox: creating and deleting records there is fine. Every test cleans up
after itself and touches only records it created (prefix "pt-e2e-").

Run with:
    .venv/bin/pytest tests/test_e2e.py -v -m e2e

Override the target with PT_E2E_CONTAINER, PT_E2E_DB, PT_E2E_ODOO_BIN.
"""

import asyncio
import os
import re
import secrets
import subprocess

import pytest

from odoo_sheller import discovery, psql
from odoo_sheller.agent_commit import AgentCommitStore
from odoo_sheller.discovery import SUPPORTED_MAJORS
from odoo_sheller.psql import PsqlCommitNotAllowed, PsqlRefused, PsqlTimedOut
from odoo_sheller.registry import Registry
from odoo_sheller.session import CommitNotAllowed
from odoo_sheller.transport import DOCKER, Target, spawn

CONTAINER = os.environ.get("PT_E2E_CONTAINER", "integra19")
DATABASE = os.environ.get("PT_E2E_DB", "integra_db_19_presta")
ODOO_BIN = os.environ.get("PT_E2E_ODOO_BIN", "/opt/odoo/odoo-bin")
NON_ODOO = os.environ.get("PT_E2E_NON_ODOO", "odoo-postgres")
CONFIG_PATH = os.environ.get("PT_E2E_CONFIG", "/opt/odoo.conf")
# A core test that exists, under the same names, in every supported major.
# `base.TestFloatPrecision` used to be that and is not any more: 20 moved the
# float tests out of `addons/base/tests`, which turns a run here into
# `tests_run: 0` — the right answer to a name that matches nothing, and a
# useless fixture.
TEST_CLASS = os.environ.get("PT_E2E_TEST_CLASS", "TestIrDefault")
TEST_METHOD = os.environ.get("PT_E2E_TEST_METHOD", "test_conditions")

pytestmark = pytest.mark.e2e


@pytest.fixture
async def session(tmp_path):
    registry = Registry(journal_root=tmp_path)
    live = await registry.open(CONTAINER, DATABASE, ODOO_BIN)
    yield live
    if live.id in registry.sessions:
        await registry.close(live.id, force=True)


async def test_probe_reads_the_real_container():
    result = await discovery.probe(CONTAINER)
    assert result["ok"] is True
    assert result["supported"] is True
    assert result["odoo_bin"] == ODOO_BIN
    # The override env vars can point this at a 17 or 18 container just as
    # legitimately; what matters is that the probe read a supported one.
    assert result["odoo_major"] in SUPPORTED_MAJORS
    assert result["db_name"] == DATABASE
    assert DATABASE in result["databases"]


async def test_probe_rejects_a_container_that_is_not_odoo():
    """odoo-postgres has neither odoo-bin nor python3; the failure must be legible."""
    result = await discovery.probe(NON_ODOO)
    assert result["ok"] is False
    assert result["supported"] is False
    assert "python3" in result["error"] or "odoo-bin" in result["error"]


async def test_session_starts_and_reports_odoo_19(session):
    assert session.hello["odoo"].startswith("19")
    assert session.hello["db"] == DATABASE
    assert session.hello["pid"] > 0


async def test_orm_works_and_namespace_persists(session):
    first = await session.execute("partners = env['res.partner'].search([], limit=3)\nlen(partners)")
    assert first["error"] is None
    assert int(first["result"]) >= 0
    second = await session.execute("partners.mapped('name')")
    assert second["error"] is None


async def test_odoo_logs_go_to_stderr_not_into_frames(session):
    result = await session.execute("import logging; logging.getLogger('pt').error('marker')")
    assert result["error"] is None
    assert "marker" not in result["stdout"]
    await asyncio.sleep(0.5)
    assert any("marker" in line for line in session.stderr_tail())


async def test_rollback_discards_a_created_record(session):
    created = await session.execute(
        "p = env['res.partner'].create({'name': 'pt-e2e-temp'})\np.id"
    )
    assert created["error"] is None
    await session.rollback()
    check = await session.execute(
        "env['res.partner'].search_count([('name', '=', 'pt-e2e-temp')])"
    )
    assert check["result"] == "0"


async def test_commit_persists_and_is_visible_to_another_session(session):
    """The one test that writes for real, then removes what it wrote.

    It also proves the cache rule: the second session only sees the record
    because commit invalidates, and the first session only sees the deletion
    afterwards for the same reason.
    """
    created = await session.execute(
        "p = env['res.partner'].create({'name': 'pt-e2e-commit'})\np.id"
    )
    assert created["error"] is None
    partner_id = int(created["result"])
    await session.commit()

    try:
        registry = Registry(journal_root=session.journal.path.parent)
        other = await registry.open(CONTAINER, DATABASE, ODOO_BIN)
        try:
            seen = await other.execute(
                "env['res.partner'].search_count([('name', '=', 'pt-e2e-commit')])"
            )
            assert seen["result"] == "1"
        finally:
            await registry.close(other.id, force=True)
    finally:
        await session.execute(f"env['res.partner'].browse({partner_id}).unlink()")
        await session.commit()

    gone = await session.execute(
        "env['res.partner'].search_count([('name', '=', 'pt-e2e-commit')])"
    )
    assert gone["result"] == "0"


async def test_interrupt_stops_a_command_and_keeps_the_session(session):
    running = asyncio.create_task(session.execute("import time\ntime.sleep(30)"))
    await asyncio.sleep(1.0)
    await session.interrupt()
    result = await running
    assert result["error"]["type"] == "KeyboardInterrupt"
    alive = await session.execute("'still here'")
    assert alive["result"] == "'still here'"


async def test_close_leaves_no_committed_data(session):
    created = await session.execute(
        "env['res.partner'].create({'name': 'pt-e2e-close'})"
    )
    assert created["error"] is None
    await session.close()

    registry = Registry(journal_root=session.journal.path.parent)
    fresh = await registry.open(CONTAINER, DATABASE, ODOO_BIN)
    try:
        check = await fresh.execute(
            "env['res.partner'].search_count([('name', '=', 'pt-e2e-close')])"
        )
        assert check["result"] == "0"
    finally:
        await registry.close(fresh.id, force=True)


async def test_run_test_runs_a_single_known_passing_method(session):
    """base is always installed; this class is stable core Odoo code."""
    result = await session.run_test("base", TEST_CLASS, TEST_METHOD)
    assert result["error"] is None
    assert result["test"]["tests_run"] == 1
    assert result["test"]["failures"] == 0
    assert result["test"]["errors"] == 0
    assert result["test"]["success"] is True
    assert result["discarded_pending"] is False


async def test_run_test_runs_a_whole_class(session):
    result = await session.run_test("base", TEST_CLASS)
    assert result["error"] is None
    assert result["test"]["tests_run"] > 1
    assert result["test"]["success"] is True


async def test_run_test_with_no_matching_name_reports_zero_not_success(session):
    """OdooTestResult.wasSuccessful() is True for zero tests too — tests_run must
    be checked, or a typo'd name silently reads as a pass."""
    result = await session.run_test("base", "ThereIsNoSuchTestClass")
    assert result["error"] is None
    assert result["test"]["tests_run"] == 0
    assert result["test"]["success"] is True  # the misleading part, made visible



# A module with a few menus under an app that is not its own.
CLICK_MODULE = os.environ.get("PT_E2E_CLICK_MODULE", "hr_skills")


async def test_click_module_clicks_a_module_and_restores_load_menus(session):
    """Verified on 19 (integra19 / integra_db_19_presta, 2026-10-27): the
    browser saw only the module's app, each failing menu was blamed under its
    foreign root with the server's error as the reason, the run went on past
    each one, and an interrupt mid-run left the class restored. That database
    fails every menu (its `sale_integration` table lacks a column the code has),
    so a clean pass is not shown there; 15 to 18 and 20 were not run."""
    result = await session.click_module(CLICK_MODULE, timeout=600)
    clickbot = result["clickbot"]
    assert clickbot["restored"] is True
    assert clickbot["refused"] is None
    assert result["test"]["tests_run"] == 1
    accounted = clickbot["clicked"] + [f["xmlid"] for f in clickbot["failed"]]
    assert accounted, "the clickbot clicked nothing"
    assert all(xmlid.startswith(CLICK_MODULE + ".") for xmlid in accounted)
    assert result["test"]["success"] is (not clickbot["failed"] and all(
        root["success"] for root in clickbot["roots"]))
    after = await session.execute(
        "cls = type(env['ir.ui.menu'])\n('load_menus' in cls.__dict__, "
        "len(env['ir.ui.menu'].load_menus('1')['root']['children']))"
    )
    assert after["error"] is None
    # Our own repr of a tuple.
    own, roots = eval(after["result"])
    assert own is False
    assert roots > len(clickbot["roots"]), "load_menus still answers trimmed"

async def test_run_test_discards_pending_exec_work_in_the_same_session(session):
    created = await session.execute(
        "p = env['res.partner'].create({'name': 'pt-e2e-run-test'})\np.id"
    )
    assert created["error"] is None
    assert session.pending_commands == 1

    result = await session.run_test("base", TEST_CLASS, TEST_METHOD)
    assert result["discarded_pending"] is True
    assert session.pending_commands == 0

    check = await session.execute(
        "env['res.partner'].search_count([('name', '=', 'pt-e2e-run-test')])"
    )
    assert check["result"] == "0"


async def test_a_test_session_closes_itself_when_the_run_ends(tmp_path):
    """The whole point of autoclose: nobody has to remember."""
    registry = Registry(journal_root=tmp_path)
    live = await registry.open(CONTAINER, DATABASE, ODOO_BIN, autoclose=True)
    session_id = live.id
    try:
        result = await live.run_test("base", TEST_CLASS, TEST_METHOD)
        assert result["test"]["success"] is True
        # Not only "success": that is True for zero tests too, so this would
        # have passed while running nothing at all.
        assert result["test"]["tests_run"] == 1

        for _ in range(100):
            if session_id not in registry.sessions:
                break
            await asyncio.sleep(0.05)
        assert session_id not in registry.sessions, "the session should be gone"

        # And the result is still readable from the journal it left behind.
        records = live.journal.records()
        assert any(record["kind"] == "run_test" for record in records)
        assert any(record["kind"] == "session_close" for record in records)
    finally:
        if session_id in registry.sessions:
            await registry.close(session_id, force=True)


async def test_handover_keeps_the_namespace_and_moves_the_right_to_type(session):
    """The human prepares, the agent continues, and the journal knows who did what."""
    prepared = await session.execute("handover_probe = 21 * 2\nhandover_probe")
    assert prepared["error"] is None
    assert prepared["result"] == "42"

    old_key = session.write_key
    new_key = session.transfer_owner({"kind": "agent", "label": "e2e-agent"})
    assert new_key != old_key
    assert session.owner["kind"] == "agent"
    assert session.allow_commit is False

    # Same process, same namespace: the variable the human left is still there.
    continued = await session.execute("handover_probe + 1")
    assert continued["error"] is None
    assert continued["result"] == "43"

    with pytest.raises(CommitNotAllowed):
        await session.commit()

    session.set_allow_commit(True)
    granted = await session.commit()
    assert granted["error"] is None

    records = session.journal.records()
    handover = next(r for r in records if r["kind"] == "owner_changed")
    assert handover["from"]["kind"] == "human"
    assert handover["to"]["label"] == "e2e-agent"
    actors = [r["actor"]["kind"] for r in records if r["kind"] == "exec"]
    assert actors == ["human", "agent"]


# --- os_psql ----------------------------------------------------------------
#
# Writes one scratch table, under a name nothing else can have, and drops it.


def _docker(*argv):

    return subprocess.run(
        ["docker", *argv], capture_output=True, text=True, timeout=30, check=False
    )


def _psql_processes() -> set[str]:
    """Pids of `psql` in the container, from /proc: the image need not have `ps`."""
    code = (
        "import os\n"
        "for p in filter(str.isdigit, os.listdir('/proc')):\n"
        "    try:\n"
        "        c = open('/proc/%s/comm' % p).read().strip()\n"
        "    except OSError:\n"
        "        continue\n"
        "    if c == 'psql':\n"
        "        print(p)\n"
    )

    return set(_docker("exec", CONTAINER, "python3", "-c", code).stdout.split())


async def _until_no_new_psql(before: set[str], seconds: float) -> None:
    for _ in range(int(seconds * 4)):
        if _psql_processes() <= before:

            return
        await asyncio.sleep(0.25)
    raise AssertionError(f"psql still running: {_psql_processes() - before}")


@pytest.fixture
async def psql_target(tmp_path):
    """A registry whose switch is ours: the human's file is never read."""
    store = AgentCommitStore(tmp_path / "agent-commit.json")
    registry = Registry(journal_root=tmp_path / "journals", agent_commit=store)
    table = f"pt_e2e_psql_{secrets.token_hex(6)}"
    target = Target(kind=DOCKER, container=CONTAINER, database=DATABASE)
    store.turn_on(CONTAINER)
    await registry.run_psql(target, f"create table {table} (a int);", True, 10.0)
    store.turn_off(CONTAINER)
    yield registry, store, target, table
    store.turn_on(CONTAINER)
    await registry.run_psql(target, f"drop table if exists {table};", True, 10.0)


async def _rows(registry, target, table) -> str:
    answer = await registry.run_psql(target, f"select count(*) from {table};", False, 10.0)
    assert answer["error"] is None

    return answer["output"].split()[2]


@pytest.mark.parametrize(
    "sql",
    [
        "select 1 -- x\r; select 2;",
        "select 1 as €$a$; select 2; -- $a$",
        "select $€$ ' $€$; select 2; -- '",
        "select 1 as a$b$c; select 2; -- $b$",
        "select 1 /* /* */ */; select 2;",
        "select E'\\''; select 2;",
        "select '\\'; select 2;",
        "select 'a'\n'b'; select 2;",
        'select "a""b" from (select 1 as "a""b") s; select 2;',
        "select $$;$$; select 2;",
        "select U&'\\0061'; select 2;",
    ],
)
async def test_the_guard_counts_statements_as_real_psql_does(sql):
    """The guard's whole claim, tried against what it is a claim about."""
    target = Target(kind=DOCKER, container=CONTAINER, database=DATABASE)
    psql.check_sql(sql)
    answer = await psql.run_transaction(target, sql, read_only=True, commit=False, timeout=10.0)
    assert answer["error"] is None
    results = len(re.findall(r"^\(\d+ rows?\)$", answer["output"], re.MULTILINE))
    assert results == len(psql.statements(sql))


async def test_psql_read_only_cannot_write(psql_target):
    registry, _store, target, table = psql_target
    answer = await registry.run_psql(target, f"insert into {table} values (1);", False, 10.0)
    assert "read-only transaction" in answer["error"]
    assert answer["mode"] == "read_only"
    assert answer["committed"] is False
    assert await _rows(registry, target, table) == "0"


@pytest.mark.parametrize(
    "sql",
    [
        "set transaction read write; insert into t values (1);",
        "select set_config('transaction_read_only', 'off', true);",
        "\\set AUTOCOMMIT on",
        "commit;",
    ],
)
async def test_psql_escapes_from_read_only_are_refused(psql_target, sql):
    registry, _store, target, _table = psql_target
    with pytest.raises(PsqlRefused):
        await registry.run_psql(target, sql, False, 10.0)


async def test_psql_dry_run_writes_and_leaves_nothing(psql_target):
    registry, store, target, table = psql_target
    store.turn_on(CONTAINER)
    sql = f"insert into {table} values (1); select count(*) from {table};"
    answer = await registry.run_psql(target, sql, False, 10.0)
    assert answer["error"] is None
    assert "INSERT 0 1" in answer["output"]
    assert answer["committed"] is False
    assert answer["rolled_back"] is True
    assert await _rows(registry, target, table) == "0"


async def test_psql_commit_persists_with_the_switch_on(psql_target):
    registry, store, target, table = psql_target
    store.turn_on(CONTAINER)
    answer = await registry.run_psql(target, f"insert into {table} values (1);", True, 10.0)
    assert answer["committed"] is True
    assert answer["error"] is None
    assert await _rows(registry, target, table) == "1"


async def test_psql_commit_with_the_switch_off_runs_nothing(psql_target):
    registry, _store, target, table = psql_target
    with pytest.raises(PsqlCommitNotAllowed):
        await registry.run_psql(target, f"insert into {table} values (1);", True, 10.0)
    assert await _rows(registry, target, table) == "0"


async def test_psql_error_in_the_second_statement_leaves_no_first_write(psql_target):
    registry, store, target, table = psql_target
    store.turn_on(CONTAINER)
    sql = f"insert into {table} values (1);\nselect nonsense from nowhere;"
    answer = await registry.run_psql(target, sql, True, 10.0)
    assert "nowhere" in answer["error"]
    assert answer["committed"] is False
    assert await _rows(registry, target, table) == "0"


async def test_psql_commit_failing_on_a_deferred_constraint_is_known(psql_target):
    registry, store, target, table = psql_target
    store.turn_on(CONTAINER)
    sql = (
        f"alter table {table} add unique (a) deferrable initially deferred;"
        f"insert into {table} values (1), (1);"
    )
    answer = await registry.run_psql(target, sql, True, 10.0)
    assert answer["committed"] is False
    assert answer["rolled_back"] is True
    assert "duplicate key" in answer["error"]
    assert await _rows(registry, target, table) == "0"


async def test_psql_sql_without_a_semicolon_commits_cleanly(psql_target):
    registry, store, target, table = psql_target
    store.turn_on(CONTAINER)
    answer = await registry.run_psql(target, f"insert into {table} values (1) -- done", True, 10.0)
    assert answer["committed"] is True
    assert await _rows(registry, target, table) == "1"


async def test_psql_deadline_ends_psql_and_the_backend(psql_target):
    registry, _store, target, _table = psql_target
    before = _psql_processes()
    tag = f"pt_e2e_{secrets.token_hex(6)}"
    sql = f"/* {tag} */ select pg_sleep(30); select pg_sleep(30);"
    with pytest.raises(PsqlTimedOut) as timed_out:
        await registry.run_psql(target, sql, False, 2.0)
    assert timed_out.value.committed is False
    await _until_no_new_psql(before, 5)
    await asyncio.sleep(3)  # the server's own timeout, two seconds past the daemon's
    answer = await registry.run_psql(
        target,
        f"select count(*) from pg_stat_activity "
        f"where query like '%{tag}%' and pid <> pg_backend_pid();",
        False,
        10.0,
    )
    assert answer["output"].split()[2] == "0"


async def test_psql_killed_client_leaves_nothing_committed(psql_target):
    """The daemon dying: its `docker exec` client goes, and with it `psql`'s stdin."""
    registry, store, target, table = psql_target
    store.turn_on(CONTAINER)
    before = _psql_processes()
    proc = await spawn(psql.build_command(target, read_only=False, timeout=6.0))
    proc.stdin.write(
        f"\\set AUTOCOMMIT off\nBEGIN;\ninsert into {table} values (1);\n"
        "select pg_sleep(2);\n;\n\\echo m\n".encode()
    )
    await proc.stdin.drain()
    await asyncio.sleep(1)
    proc.kill()
    await proc.wait()
    await _until_no_new_psql(before, 20)
    assert await _rows(registry, target, table) == "0"


async def test_psql_password_is_nowhere_it_should_not_be(psql_target, tmp_path):
    registry, store, target, table = psql_target
    store.turn_on(CONTAINER)
    config = _docker("exec", CONTAINER, "sh", "-c", f"cat {CONFIG_PATH}").stdout
    found = re.search(r"^db_password\s*=\s*(.+)$", config, re.MULTILINE)
    assert found, "this test needs a password in the config"
    password = found.group(1).strip()
    sql = f"select pg_sleep(1); insert into {table} values (1);"
    running = asyncio.create_task(registry.run_psql(target, sql, True, 10.0))
    await asyncio.sleep(0.5)
    host_ps = (
        await asyncio.to_thread(
            subprocess.run, ["ps", "axwww"], capture_output=True, text=True, timeout=10, check=False
        )
    ).stdout
    answer = await running
    assert answer["committed"] is True
    inspected = _docker("inspect", CONTAINER).stdout
    journals = "".join(p.read_text() for p in (tmp_path / "journals").glob("*.jsonl"))
    # The script itself is in argv and names the variable; the value is the point.
    # `inspect` holds the container's own environment, which may well have the
    # same value under a name of its own, so it is the pair that is looked for.
    needle = f"PGPASSWORD={password}"
    assert needle not in host_ps, "the password reached a host process list"
    assert needle not in inspected, "the password reached docker inspect"
    assert needle not in journals, "the password reached a journal"
    assert "db_password" not in journals, "the config reached a journal"
