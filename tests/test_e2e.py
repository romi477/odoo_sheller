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

import pytest

from odoo_sheller import discovery
from odoo_sheller.registry import Registry
from odoo_sheller.session import CommitNotAllowed

CONTAINER = os.environ.get("PT_E2E_CONTAINER", "integra19")
DATABASE = os.environ.get("PT_E2E_DB", "integra_db_19_presta")
ODOO_BIN = os.environ.get("PT_E2E_ODOO_BIN", "/opt/odoo/odoo-bin")
NON_ODOO = os.environ.get("PT_E2E_NON_ODOO", "odoo-postgres")

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
    assert result["odoo_major"] == 19
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
