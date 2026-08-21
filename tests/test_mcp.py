"""The MCP server: argument mapping, refusals, and the stdio rules.

Tools are exercised against the real FastAPI app through an ASGI transport, so
these are integration tests of the pair — no network, no daemon, but the actual
routes, authorization and status codes.
"""

import re
from pathlib import Path

import httpx2 as httpx
import pytest

from odoo_sheller import mcp as server
from odoo_sheller.api import create_app

MODULE = Path(__file__).resolve().parent.parent / "odoo_sheller" / "mcp.py"


class FakeSession:
    def __init__(self, session_id="s1"):
        self.id = session_id
        self.write_key = "key-s1"
        self.owner = {"kind": "agent", "label": "mcp-agent"}
        self.allow_commit = False
        self.pending_commands = 0
        self.calls = []

    def describe(self):

        return {
            "id": self.id,
            "state": "ready",
            "container": "integra19",
            "database": "integra_db_19",
            "odoo": "19.0",
            "python": "3.12.13",
            "pending_commands": self.pending_commands,
            "owner": dict(self.owner),
            "allow_commit": self.allow_commit,
        }

    def stderr_tail(self, limit=200):

        return []

    async def execute(self, code, timeout=300.0):
        self.calls.append(("execute", code))

        return {
            "id": 1,
            "stdout": "x" * 10_000,
            "stdout_truncated": False,
            "result": "y" * 5_000,
            "result_truncated": False,
            "error": None,
            "duration": 0.02,
        }

    async def rollback(self):
        self.calls.append(("rollback",))

        return {"error": None}

    async def commit(self):
        from odoo_sheller.session import CommitNotAllowed

        raise CommitNotAllowed("not granted")


class FakeRegistry:
    admin_key = "admin-key"

    def __init__(self):
        self.session = FakeSession()
        self.sessions = {"s1": self.session}
        self.subscribers = {}
        self.journal_root = Path("/nonexistent")
        self.past_target = {
            "container": "integra19",
            "database": "integra_db_19",
            "odoo_bin": "/opt/odoo/odoo-bin",
        }

    def target_of_past_session(self, session_id):

        return self.past_target

    def journal_file_for(self, session_id):

        return None

    def get(self, session_id):

        return self.sessions[session_id]


@pytest.fixture
def wired(monkeypatch):
    """Point the MCP module's HTTP calls at the app itself."""
    registry = FakeRegistry()
    app = create_app(registry=registry)
    transport = httpx.ASGITransport(app=app)

    class Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            kwargs.setdefault("base_url", "http://testserver")
            super().__init__(*args, **{k: v for k, v in kwargs.items() if k != "timeout"})

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    monkeypatch.setattr(server, "_keys", {"s1": "key-s1"})

    return registry


async def test_exec_runs_and_truncates_for_the_agent(wired):
    result = await server.os_exec("1 + 1")
    assert wired.session.calls == [("execute", "1 + 1")]
    assert len(result["stdout"]) == server.MAX_STDOUT
    assert len(result["result"]) == server.MAX_RESULT
    assert result["truncated"] is True
    assert result["journal"] == "/api/journals/s1"


async def test_commit_without_the_right_comes_back_as_guidance(wired):
    refusal = await server.os_commit()
    assert refusal["error"] == "commit_not_allowed"
    assert "grant" in refusal["recovery"]


async def test_exec_without_a_key_is_refused_as_not_owner(wired, monkeypatch):
    monkeypatch.setattr(server, "_keys", {"s1": "wrong-key"})
    refusal = await server.os_exec("1 + 1")
    assert refusal["error"] == "not_owner"


async def test_a_gone_session_explains_how_to_carry_on(wired):
    server._keys["missing"] = "key"
    refusal = await server.os_exec("1 + 1", session_id="missing")
    assert refusal["error"] == "session_gone"
    assert refusal["target"]["container"] == "integra19"
    assert "variables are lost" in refusal["recovery"]


async def test_rollback_reaches_the_session(wired):
    await server.os_rollback()
    assert ("rollback",) in wired.session.calls


async def test_sessions_are_marked_as_ours(wired):
    listing = await server.os_list_sessions()
    assert listing["count"] == 1
    assert listing["sessions"][0]["yours"] is True
    assert "write_key" not in listing["sessions"][0]
    assert listing["yours"] == ["s1"]


async def test_an_empty_session_list_is_still_an_answer(wired, monkeypatch):
    """A bare [] renders as no output at all, which reads like a broken tool."""
    monkeypatch.setattr(server, "_keys", {})
    wired.sessions.clear()
    listing = await server.os_list_sessions()
    assert listing == {"sessions": [], "count": 0, "yours": []}


async def test_rollback_answers_with_an_outcome_not_a_wire_frame(wired):
    result = await server.os_rollback()
    assert result == {"ok": True, "error": None}
    assert "stdout_truncated" not in result


async def test_a_markdown_journal_comes_back_as_text(tmp_path, monkeypatch):
    """Journal exports are markdown and NDJSON — calling .json() on them throws."""
    from datetime import UTC, datetime

    from odoo_sheller import journal
    from odoo_sheller.registry import Registry

    path = journal.journal_path(
        tmp_path, "abc123", "integra19", "db", datetime(2026, 8, 18, 9, 0, 0, tzinfo=UTC)
    )
    log = journal.Journal(path)
    log.write("session_open", container="integra19", database="db", odoo="19.0")
    log.write("exec", id=1, code="1 + 1")

    app = create_app(registry=Registry(journal_root=tmp_path, admin_key="admin-key"))
    transport = httpx.ASGITransport(app=app)

    class Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            kwargs.setdefault("base_url", "http://testserver")
            super().__init__(*args, **{k: v for k, v in kwargs.items() if k != "timeout"})

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)

    exported = await server.os_journal("abc123", fmt="markdown")
    assert "# Session abc123" in exported["text"]
    assert "```python" in exported["text"]


async def test_no_session_yet_tells_the_agent_what_to_do(wired, monkeypatch):
    monkeypatch.setattr(server, "_keys", {})
    refusal = await server.os_exec("1 + 1")
    assert refusal["error"] == "no_session"
    assert "os_open_session" in refusal["recovery"]


async def test_several_sessions_require_an_explicit_id(wired, monkeypatch):
    monkeypatch.setattr(server, "_keys", {"s1": "key-s1", "s2": "key-s2"})
    refusal = await server.os_exec("1 + 1")
    assert refusal["error"] == "ambiguous_session"
    assert refusal["sessions"] == ["s1", "s2"]


async def test_an_unreachable_daemon_is_reported_as_such(monkeypatch):
    class Broken:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            raise OSError("connection refused")

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(server.httpx, "AsyncClient", Broken)
    monkeypatch.setattr(server, "_keys", {"s1": "key"})
    refusal = await server.os_exec("1 + 1")
    assert refusal["error"] == "daemon_unreachable"
    assert "odoo_sheller" in refusal["recovery"]


def test_the_module_never_writes_to_stdout():
    """stdio carries JSON-RPC: one stray print breaks the server for good."""
    source = MODULE.read_text(encoding="utf-8")
    assert not re.search(r"(?<![\w.])print\s*\(", source)
    assert "logging.getLogger" in source


def test_tools_declare_their_nature():
    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert {"os_exec", "os_commit", "os_open_session", "os_journal"} <= names


async def test_history_answers_in_the_same_shape_as_exec(wired, monkeypatch):
    """History is read to reason about past work, not to inspect wire frames."""
    feed = {
        "session": {
            "session_id": "s1", "container": "integra19", "database": "db",
            "owner": {"kind": "agent", "label": "mcp-agent"}, "state": "ready",
            "allow_commit": False, "commands": 1, "committed": False,
            "unmasked": True, "pid": 800, "odoo_bin": "/opt/odoo/odoo-bin",
        },
        "entries": [
            {
                "kind": "exec", "id": 1, "ordinal": 1, "code": "1 + 1",
                "status": "done",
                "actor": {"kind": "agent", "label": "mcp-agent"},
                "result": {
                    "id": 1, "stdout": "", "stdout_truncated": False,
                    "result": "2", "result_truncated": False, "error": None,
                    "duration": 0.0474085807800293,
                },
            },
            {"kind": "rollback", "actor": {"kind": "human", "label": "browser"}},
        ],
    }

    async def fake_call(method, path, session_id=None, **kwargs):

        return feed

    monkeypatch.setattr(server, "_call", fake_call)
    history = await server.os_history("s1")

    assert history["entries"] == [
        {"n": 1, "code": "1 + 1", "status": "done", "result": "2",
         "duration": 0.047, "actor": "agent:mcp-agent"},
        {"kind": "rollback", "actor": "human:browser"},
    ]
    assert "pid" not in history["session"], "trim what the agent cannot act on"
    assert history["session"]["owner"]["kind"] == "agent"
    assert history["journal"] == "/api/journals/s1"
    assert "gone" not in history["session"], "a live session must not look dead"


async def test_history_of_a_dead_session_still_says_it_is_gone(wired, monkeypatch):
    """The daemon answers 200 from the journal; the death signal rides in the body."""
    feed = {
        "session": {
            "session_id": "s1",
            "state": "gone",
            "gone": {
                "error": "session_gone",
                "session_id": "s1",
                "reason": "not registered",
                "target": {"container": "integra19", "database": "db", "odoo_bin": None},
                "journal": "/api/journals/s1",
                "recovery": "open a new session on the same target; variables are lost",
            },
        },
        "entries": [{
            "kind": "exec", "id": 1, "ordinal": 1, "code": "1 + 1", "status": "done",
            "result": {"stdout": "", "result": "2", "error": None, "duration": 0.05},
        }],
    }

    async def fake_call(method, path, session_id=None, **kwargs):

        return feed

    monkeypatch.setattr(server, "_call", fake_call)
    history = await server.os_history("s1")

    assert history["session"]["state"] == "gone"
    assert history["session"]["gone"]["error"] == "session_gone"
    assert history["session"]["gone"]["target"]["container"] == "integra19"
    assert history["session"]["gone"]["recovery"]
    assert history["entries"], "the transcript is still worth reading"


async def test_history_marks_truncated_and_abandoned_commands(wired, monkeypatch):
    feed = {
        "session": {},
        "entries": [{
            "kind": "exec", "ordinal": 4, "code": "big()", "status": "done",
            "abandoned": True,
            "result": {"stdout": "x" * 9000, "result": None, "error": None, "duration": 301.0},
        }],
    }

    async def fake_call(method, path, session_id=None, **kwargs):

        return feed

    monkeypatch.setattr(server, "_call", fake_call)
    entry = (await server.os_history("s1"))["entries"][0]
    assert len(entry["stdout"]) == server.MAX_STDOUT
    assert entry["truncated"] is True
    assert entry["abandoned"] is True
    assert entry["duration"] == 301.0


def test_the_instructions_explain_every_refusal_the_agent_can_meet():
    """A refusal code with no explanation leaves the agent guessing or retrying."""
    text = server.INSTRUCTIONS
    for code in ("session_busy", "session_gone", "not_owner", "commit_not_allowed"):
        assert code in text, code


def test_the_instructions_say_how_to_obtain_commit_rights():
    text = server.INSTRUCTIONS.lower()
    assert "grant commit" in text, "the agent must know where the human grants it"
    assert "do not retry" in text
    assert "rollback is the default" in text


def test_the_instructions_say_a_granted_commit_needs_no_further_check_in():
    """Otherwise a cautious model repeats the ask-first ritual on every commit."""
    text = server.INSTRUCTIONS
    assert "the right stays granted" in text
    assert "call os_commit directly" in text
    assert "no need to repeat this ritual" in text


def test_the_instructions_say_exec_is_never_gated_by_commit_rights():
    """Grant commit answers one question only: can this session persist."""
    text = server.INSTRUCTIONS
    assert "running code is never gated by it" in text
    assert "os_exec always works" in text


def test_the_instructions_state_what_a_handover_does_and_does_not_move():
    text = server.INSTRUCTIONS
    assert "write key" in text
    assert "namespace" in text
    assert "does not survive a handover" in text, "a granted right must not look permanent"


def test_the_instructions_point_the_agent_at_mapped_instead_of_a_loop():
    """Without this, code that could be one mapped() call arrives as a for-loop."""
    text = server.INSTRUCTIONS
    assert "records.mapped('name')" in text
    assert "partner_id.bank_ids" in text, "the dotted-path union behavior is the non-obvious part"
    assert "lambda" in text
    assert "filtered()" in text and "sorted()" in text


def test_the_instructions_push_filtering_into_search_not_python():
    """Fetching broadly then filtering in Python defeats the point of a domain."""
    text = server.INSTRUCTIONS
    assert "search_count(domain)" in text
    assert "search([('is_company', '=', True)])" in text


def test_the_instructions_mention_set_operators_and_ensure_one():
    text = server.INSTRUCTIONS
    assert "intersection" in text and "difference" in text
    assert "self.ensure_one()" in text
    assert "one-record recordsets" in text
