"""Verifies against a real `sshd`, a real `sudo` and a real Odoo what a shim
cannot: that the command pipe survives the user switch, that a session opens
through a card, that an interrupt reaches a process owned by another user, and
that a plain server is `production` until a human says otherwise.

Runs against the double in `tests/ssh_double/`: a container that looks like the
server this target kind is for — Odoo installed in the system, a login user who
may become the `odoo` user, a config only `odoo` can read. Start it with

    tests/ssh_double/up.sh

and run with

    .venv/bin/pytest tests/test_e2e_ssh.py -v -m e2e

It never writes to the database: every session ends in rollback, and nothing
here asks for a commit — which on `production` would be refused anyway.

The double's host key is trusted through a throwaway known_hosts file added to
ssh's own options. That is the daemon's side, not the card's: the grammar
refuses a card that tries to weaken host key checking, and this does not touch
that, or the user's own ~/.ssh.
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from odoo_sheller import discovery, transport
from odoo_sheller.registry import Registry
from odoo_sheller.session import CommitForbidden, CommitNotAllowed, SessionDead
from odoo_sheller.targets import TargetStore

STATE = Path(
    os.environ.get("PT_E2E_SSH_STATE") or Path.home() / ".cache" / "odoo-sheller-ssh-double"
)
PORT = os.environ.get("PT_E2E_SSH_PORT", "2222")
DATABASE = os.environ.get("PT_E2E_SSH_DB", "odoo")
PYTHON = os.environ.get("PT_E2E_SSH_PYTHON", "/usr/local/bin/python3")
ODOO_BIN = os.environ.get("PT_E2E_SSH_ODOO_BIN", "/opt/odoo/odoo-bin")
CONFIG = os.environ.get("PT_E2E_SSH_CONFIG", "/etc/odoo/odoo.conf")

pytestmark = pytest.mark.e2e

BECOME = "sudo -n -u odoo -H"
# The double's config says `workers = 2`, as a server that is in use does, and
# something is listening on its HTTP port. A launch that does not turn HTTP off
# dies binding that port — which is what the real server did, and what this was
# written to reproduce.
BARE = f"{PYTHON} {ODOO_BIN} shell -c {CONFIG}"
LAUNCH = f"{BARE} --no-http"


def access(become=BECOME):
    return f"ssh -i {STATE / 'key'} -p {PORT} ubuntu@127.0.0.1 {become}".strip()


@pytest.fixture(autouse=True)
def the_double(tmp_path, monkeypatch):
    """Skip unless the double is up, and point ssh at its throwaway host key."""
    if not (STATE / "key").is_file():
        pytest.skip(f"no ssh double: run tests/ssh_double/up.sh (state in {STATE})")
    options = list(transport.SSH_OPTS)
    # The control sockets belong in a directory of the test's own, not in
    # ~/.odoo-sheller — and a short one: a unix socket's path is limited to 104
    # bytes on macOS, which a pytest tmp_path alone can exceed.
    sockets = tempfile.mkdtemp(prefix="os-", dir="/tmp")
    control = options.index("-o", options.index("ControlMaster=auto") + 1)
    options[control + 1] = f"ControlPath={sockets}/ssh-%C"
    options += [
        "-o", f"UserKnownHostsFile={STATE / 'known_hosts'}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
    ]
    monkeypatch.setattr(transport, "SSH_OPTS", tuple(options))
    monkeypatch.setattr(discovery, "SSH_OPTS", tuple(options))
    yield
    shutil.rmtree(sockets, ignore_errors=True)


@pytest.fixture
def registry(tmp_path):
    return Registry(journal_root=tmp_path, targets=TargetStore(tmp_path / "targets.json"))


async def open_card(registry, stage="production", name=None, **overrides):
    fields = {"access": access(), "launch": LAUNCH, "database": DATABASE, "stage": stage}
    fields.update(overrides)
    card = registry.targets.add_ssh(name or f"double-{stage}", **fields)

    return await registry.open(target_id=card["id"])


@pytest.fixture
async def session(registry):
    live = await open_card(registry)
    yield live
    if live.id in registry.sessions:
        await registry.close(live.id, force=True)


async def test_probe_says_who_the_recipe_lands_as_and_which_odoo():
    recipe = _parsed(access(), LAUNCH, DATABASE)
    result = await discovery.probe_ssh(*recipe)
    assert result["ok"] is True, result
    assert result["login_user"] == "ubuntu"
    assert result["effective_user"] == "odoo"
    assert result["odoo_version"].startswith("19")
    assert result["config_readable"] is True
    assert result["executable_ok"] is True
    assert result["supported"] is True
    assert result["workers"] == 2
    assert result["launch_warning"] is None, "this launch turns HTTP off"


async def test_forgetting_the_become_is_named_the_way_the_real_server_would_show_it():
    """The config is readable only by `odoo`, so the login user cannot read it:
    the likely mistake, with its own words."""
    result = await discovery.probe_ssh(*_parsed(access(become=""), LAUNCH, DATABASE))
    assert result["ok"] is False
    assert result["error_code"] == "config_unreadable"
    assert "ubuntu" in result["error"]
    assert CONFIG in result["error"]


async def test_a_become_the_login_user_may_not_make_says_what_to_check():
    recipe = _parsed(access(become="sudo -n -u root -H"), LAUNCH, DATABASE)
    result = await discovery.probe_ssh(*recipe)
    assert result["ok"] is False
    assert result["error_code"] == "sudo"
    assert "sudo -l" in result["error"] or "NOPASSWD" in result["error"]


async def test_a_host_key_nobody_trusted_is_a_failure_with_instructions(monkeypatch, tmp_path):
    options, rest = [], iter(transport.SSH_OPTS)
    for option in rest:
        if option == "-o":
            value = next(rest)
            if "KnownHostsFile" in value or "StrictHostKeyChecking" in value:
                continue
            options += [option, value]
        else:
            options.append(option)
    options += ["-o", f"UserKnownHostsFile={tmp_path / 'empty'}"]
    monkeypatch.setattr(discovery, "SSH_OPTS", tuple(options))
    result = await discovery.probe_ssh(*_parsed(access(), LAUNCH, DATABASE))
    assert result["ok"] is False
    assert result["error_code"] == "host_key"
    assert "terminal" in result["error"]


async def test_a_session_opens_through_the_card_and_odoo_is_odoo_s_own(session):
    assert session.hello["odoo"].startswith("19")
    assert session.hello["db"] == DATABASE
    assert session.target.kind == "ssh"
    result = await session.execute("env.cr.dbname")
    assert result["error"] is None
    assert DATABASE in result["result"]


async def test_the_session_runs_as_the_user_the_card_became(session):
    """The pid in hello is on the server, and belongs to `odoo`."""
    result = await session.execute("import getpass, os\nprint(getpass.getuser(), os.getuid() == 1000)")
    assert result["error"] is None
    assert "odoo True" in result["stdout"]


async def test_the_namespace_persists_through_ssh_and_sudo(session):
    first = await session.execute("n = env['res.users'].search_count([])\nn")
    assert first["error"] is None
    second = await session.execute("n + 0")
    assert second["result"] == first["result"]


async def test_an_interrupt_reaches_a_process_owned_by_another_user(session):
    """`kill` has to go as `odoo` too: the login user may not signal its process."""
    running = asyncio.create_task(session.execute("import time\ntime.sleep(60)"))
    await asyncio.sleep(2.0)
    await session.interrupt()
    result = await asyncio.wait_for(running, 20)
    assert result["error"] is not None
    assert "KeyboardInterrupt" in result["error"]["type"] or "Interrupt" in str(result["error"])
    again = await session.execute("1 + 1")
    assert again["result"] == "2", "the session survives its interrupted command"


async def test_a_plain_server_is_production_until_a_human_says_otherwise(session):
    assert session.target.stage == "production"
    assert session.allow_commit is False
    with pytest.raises(CommitNotAllowed) as refused:
        await session.commit()
    assert not isinstance(refused.value, CommitForbidden), "a human may grant this one"


@pytest.mark.parametrize("stage", ["staging"])
async def test_a_declared_non_production_server_is_closed_to_commit_until_granted(registry, stage):
    live = await open_card(registry, stage=stage)
    try:
        assert live.allow_commit is False, "even for a human owner: it is someone's own Odoo"
        with pytest.raises(CommitNotAllowed):
            await live.commit()
    finally:
        await registry.close(live.id, force=True)


async def test_what_a_session_created_is_gone_when_it_closes(registry):
    """Closing discards the open transaction; a second session never sees it."""
    live = await open_card(registry, stage="staging")
    try:
        created = await live.execute("p = env['res.partner'].create({'name': 'pt-e2e-ssh-never'})\np.id")
        assert created["error"] is None
    finally:
        await registry.close(live.id)
    again = await open_card(registry, stage="staging", name="second")
    try:
        seen = await again.execute(
            "env['res.partner'].search_count([('name', '=', 'pt-e2e-ssh-never')])"
        )
        assert seen["result"] == "0"
    finally:
        await registry.close(again.id, force=True)


async def test_a_launch_that_is_not_there_never_starts_a_session(registry):
    with pytest.raises(ValueError, match="/nonexistent/python"):
        await open_card(registry, launch=f"/nonexistent/python {ODOO_BIN} shell -c {CONFIG}")


def _parsed(access_text, launch_text, database):
    from odoo_sheller.recipe import parse_access, parse_launch

    return parse_access(access_text), parse_launch(launch_text, database)


# --- a config with workers above 0 ------------------------------------------------


async def test_the_probe_says_what_the_config_will_do_to_a_launch_that_leaves_http_alone():
    result = await discovery.probe_ssh(*_parsed(access(), BARE, DATABASE))
    assert result["ok"] is True
    assert result["workers"] == 2
    assert "workers = 2" in result["launch_warning"]
    assert "8069" in result["launch_warning"]
    assert "--no-http" in result["launch_warning"]


async def test_a_launch_that_leaves_http_alone_dies_on_the_port_and_says_what_to_add(registry):
    """The failure from the real server, reproduced: the shell binds the HTTP port
    before it starts, something is already there, and Odoo says so in its own words.
    The words are kept; the fix is added."""
    with pytest.raises(SessionDead) as died:
        await open_card(registry, launch=BARE, name="bare")
    message = str(died.value)
    assert "Address already in use" in message
    assert "--no-http" in message
    assert registry.sessions == {}


@pytest.mark.parametrize("flag", ["--no-http", "--workers=0"])
async def test_either_way_of_turning_http_off_opens(registry, flag):
    live = await open_card(registry, launch=f"{BARE} {flag}", name="off")
    try:
        result = await live.execute("1 + 1")
        assert result["result"] == "2"
    finally:
        await registry.close(live.id, force=True)


async def test_moving_the_port_to_a_free_one_works_every_time(registry):
    """Odoo's prefork server binds the port for a moment and closes it again
    before the shell starts, so a moved port is only ever in the way of something
    that is listening on it. `--xmlrpc-port 8068` was this, and it worked."""
    moved = f"{BARE} --http-port 8068"
    for name in ("first", "second"):
        live = await open_card(registry, launch=moved, name=name)
        try:
            assert (await live.execute("1 + 1"))["result"] == "2"
        finally:
            await registry.close(live.id, force=True)


async def test_moving_the_port_onto_the_services_own_dies_like_leaving_it_alone(registry):
    onto_the_service = f"{BARE} --http-port 8069"
    with pytest.raises(SessionDead, match="Address already in use"):
        await open_card(registry, launch=onto_the_service, name="onto")
    result = await discovery.probe_ssh(*_parsed(access(), onto_the_service, DATABASE))
    assert "8069" in result["launch_warning"], "and the probe says so beforehand"
