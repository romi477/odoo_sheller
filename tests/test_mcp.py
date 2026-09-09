"""The MCP server: argument mapping, refusals, and the stdio rules.

Tools are exercised against the real FastAPI app through an ASGI transport, so
these are integration tests of the pair — no network, no daemon, but the actual
routes, authorization and status codes.
"""

import ast
import json
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
        self.inherited_pending = 0
        self.calls = []
        # What a test wants to override in the description without reaching
        # into the session's own bookkeeping.
        self.described: dict = {}

    def describe(self):

        return {
            "id": self.id,
            "state": "ready",
            "container": "integra19",
            "database": "integra_db_19",
            "odoo": "19.0",
            "python": "3.12.13",
            "pending_commands": self.pending_commands,
            "inherited_pending": self.inherited_pending,
            "owner": dict(self.owner),
            "allow_commit": self.allow_commit,
            "activity": None,
            **self.described,
        }

    def stderr_tail(self, limit=200):

        return []

    async def execute(self, code, timeout=300.0):
        self.calls.append(("execute", code))
        if getattr(self, "source_payload", None) is not None and "_os_read" in code:
            import json

            return {
                "id": 1,
                "stdout": "",
                "stdout_truncated": False,
                "result": repr(json.dumps(self.source_payload)),
                "result_truncated": False,
                "error": None,
                "duration": 0.01,
            }

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

        if not self.allow_commit:
            raise CommitNotAllowed("not granted")
        self.calls.append(("commit",))

        return {"error": None}

    async def run_test(self, module, test_class, test_method=None, timeout=300.0):
        self.calls.append(("run_test", module, test_class, test_method, timeout))

        return {
            "stdout": "printed\n",
            "error": None,
            "duration": 0.05,
            "test": {
                "module": module, "test_class": test_class, "test_method": test_method,
                "tests_run": 1, "failures": 0, "errors": 0, "skipped": 0, "success": True,
            },
            "stderr": ["INFO something"],
            "discarded_pending": False,
        }


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

    async def open(self, **kwargs):
        self.open_kwargs = kwargs

        return self.session


def guidance() -> str:
    """Everything this server tells the model, wherever it now lives.

    A host delivers only the first `INSTRUCTION_CAP` characters of
    INSTRUCTIONS, so anything longer than a rule moved into HELP behind
    os_help. Which of the two holds a given sentence is a budget decision and
    may change; that it is *somewhere* is the contract these tests pin.
    """

    return server.INSTRUCTIONS + "\n" + "\n".join(server.HELP.values())


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


async def test_run_test_opens_a_fresh_session_and_returns_the_outcome(wired):
    result = await server.os_run_test("sale.TestSaleOrder.test_x", container="c", database="db")
    assert wired.open_kwargs["owner"] == {"kind": "agent", "label": server.AGENT_LABEL}
    assert wired.open_kwargs["allow_commit"] is False
    assert result["session_id"] == "s1"
    assert result["tests_run"] == 1
    assert result["success"] is True
    assert result["stdout"] == "printed\n"
    assert result["stderr"] == "INFO something"
    assert result["discarded_pending"] is False
    assert "s1" in server._keys


async def test_run_test_never_returns_the_write_key(wired):
    result = await server.os_run_test("sale.TestSaleOrder")
    assert "write_key" not in result


async def test_run_test_defaults_to_a_short_timeout(wired):
    await server.os_run_test("sale.TestSaleOrder")
    assert wired.session.calls[-1] == ("run_test", "sale", "TestSaleOrder", None, 30.0)


async def test_run_test_keeps_the_session_open_inside_the_budget(monkeypatch):
    """Waiting out the daemon's full 90s ceiling only means the host kills the
    call first — and then the client_token that finds a stranded session never
    reaches the caller either. Staying inside the budget is what delivers it."""
    registry = FakeRegistry()
    app = create_app(registry=registry)
    transport = httpx.ASGITransport(app=app)
    timeouts = []

    class Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            kwargs["transport"] = transport
            kwargs.setdefault("base_url", "http://testserver")
            super().__init__(*args, **{k: v for k, v in kwargs.items() if k != "timeout"})

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    monkeypatch.setattr(server, "_keys", {})

    await server.os_run_test("sale.TestSaleOrder")

    assert timeouts[0] <= server.MCP_CALL_BUDGET, (
        "an open the host will not wait for is an open that answers nothing"
    )


async def test_run_test_reports_the_session_it_may_have_stranded(monkeypatch):
    """On an open timeout the daemon may still register the session. Saying so
    is the difference between a recoverable session and an unkillable one."""
    class Slow:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, *args, **kwargs):
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(server.httpx, "AsyncClient", Slow)
    monkeypatch.setattr(server, "_keys", {})

    refusal = await server.os_run_test("sale.TestSaleOrder")

    assert refusal["error"] == "request_timed_out"
    assert refusal["client_token"], "the token is how the stranded session is found"
    assert "os_list_sessions" in refusal["recovery"]


@pytest.mark.parametrize("bad", [0, -5, 99999])
async def test_run_test_refuses_an_unusable_timeout_before_opening_anything(wired, bad):
    """Catch it here rather than opening a session and handing back a raw 422."""
    refusal = await server.os_run_test("sale.TestSaleOrder", timeout=bad)
    assert refusal["error"] == "invalid_timeout"
    assert refusal["recovery"]
    assert wired.session.calls == [], "no session may be opened for a doomed call"


async def test_run_test_with_a_malformed_spec_is_a_clean_refusal(wired):
    """A typo'd test name must not crash the tool with an AttributeError."""
    refusal = await server.os_run_test("not a valid spec!!")
    assert refusal["error"] == "invalid_test_spec"
    assert refusal["session_id"] == "s1"


async def test_run_test_truncates_stdout_and_stderr_for_the_agent(wired, monkeypatch):
    async def big_run_test(self, module, test_class, test_method=None, timeout=300.0):

        return {
            "stdout": "x" * 10_000,
            "error": None,
            "duration": 0.05,
            "test": {"tests_run": 1, "failures": 0, "errors": 0, "skipped": 0, "success": True},
            "stderr": ["y" * 10_000],
            "discarded_pending": False,
        }

    monkeypatch.setattr(FakeSession, "run_test", big_run_test)
    result = await server.os_run_test("sale.TestSaleOrder")
    assert len(result["stdout"]) == server.MAX_STDOUT
    assert len(result["stderr"]) == server.MAX_STDOUT
    assert result["truncated"] is True
    assert result["journal"] == "/api/journals/s1"


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


async def test_a_client_side_read_timeout_is_not_reported_as_daemon_down(monkeypatch):
    """The daemon is still working on a slow run_test/exec — telling the agent
    to go start it would be actively wrong, and inviting a retry would kick
    off a brand-new, duplicate slow run instead of checking on the old one."""
    class Slow:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, *args, **kwargs):
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(server.httpx, "AsyncClient", Slow)
    monkeypatch.setattr(server, "_keys", {"s1": "key"})
    refusal = await server.os_exec("1 + 1")
    assert refusal["error"] == "request_timed_out"
    assert "os_history" in refusal["recovery"]
    assert "start it" not in refusal["recovery"]


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
    assert {"os_exec", "os_commit", "os_open_session", "os_journal", "os_session"} <= names


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


async def test_history_shapes_a_run_test_entry_with_its_outcome(wired, monkeypatch):
    """A run_test entry must not collapse to a bare {kind, actor} like an
    unrecognized command would — the whole point of journaling it is that the
    outcome survives a transport timeout and is recoverable from here."""
    feed = {
        "session": {"session_id": "s1"},
        "entries": [{
            "kind": "run_test", "id": 1, "ordinal": 1,
            "module": "sale", "test_class": "TestSaleOrder", "test_method": "test_x",
            "status": "done",
            "actor": {"kind": "agent", "label": "mcp-agent"},
            "result": {
                "stdout": "printed\n", "stderr": ["INFO x"], "error": None,
                "duration": 88.4,
                "test": {"tests_run": 68, "failures": 0, "errors": 0,
                          "skipped": 0, "success": True},
            },
        }],
    }

    async def fake_call(method, path, session_id=None, **kwargs):

        return feed

    monkeypatch.setattr(server, "_call", fake_call)
    entry = (await server.os_history("s1"))["entries"][0]

    assert entry["test"] == "sale.TestSaleOrder.test_x"
    assert entry["tests_run"] == 68
    assert entry["success"] is True
    assert entry["duration"] == 88.4
    assert entry["stdout"] == "printed\n"
    assert entry["stderr"] == "INFO x"


def test_the_instructions_explain_every_refusal_the_agent_can_meet():
    """A refusal code with no explanation leaves the agent guessing or retrying."""
    text = guidance()
    for code in ("session_busy", "session_gone", "not_owner", "commit_not_allowed"):
        assert code in text, code


def test_the_instructions_say_how_to_obtain_commit_rights():
    text = guidance().lower()
    assert "grant commit" in text, "the agent must know where the human grants it"
    assert "do not retry" in text
    assert "rollback is the default" in text


def test_the_instructions_say_a_granted_commit_needs_no_further_check_in():
    """Otherwise a cautious model repeats the ask-first ritual on every commit."""
    text = guidance()
    assert "the right stays granted" in text
    assert "call os_commit directly" in text
    assert "no need to repeat this ritual" in text


def test_the_instructions_say_exec_is_never_gated_by_commit_rights():
    """Grant commit answers one question only: can this session persist."""
    text = guidance()
    assert "running code is never gated by it" in text
    assert "os_exec always works" in text


def test_the_instructions_state_what_a_handover_does_and_does_not_move():
    text = guidance()
    assert "write key" in text
    assert "namespace" in text
    assert "does not survive a handover" in text, "a granted right must not look permanent"


def test_the_instructions_point_the_agent_at_mapped_instead_of_a_loop():
    """Without this, code that could be one mapped() call arrives as a for-loop."""
    text = guidance()
    assert "records.mapped('name')" in text
    assert "partner_id.bank_ids" in text, "the dotted-path union behavior is the non-obvious part"
    assert "lambda" in text
    assert "filtered()" in text and "sorted()" in text


def test_the_instructions_push_filtering_into_search_not_python():
    """Fetching broadly then filtering in Python defeats the point of a domain."""
    text = guidance()
    assert "search_count(domain)" in text
    assert "search([('is_company', '=', True)])" in text


def test_the_instructions_mention_set_operators_and_ensure_one():
    text = guidance()
    assert "intersection" in text and "difference" in text
    assert "self.ensure_one()" in text
    assert "one-record recordsets" in text


def test_the_instructions_explain_os_run_test():
    text = guidance()
    assert "os_run_test" in text
    assert "module.TestClass" in text
    assert "discards" in text.lower() or "discarded" in text.lower()


def test_the_instructions_say_not_to_retry_a_run_that_is_still_going():
    """Retrying would start a brand-new, duplicate run instead of waiting on
    the slow one already in flight."""
    text = guidance().lower()
    assert "never answer a `status: \"running\"` by calling os_run_test again" in text
    assert "duplicate run" in text
    assert "os_test_result" in text


async def test_os_list_tests_uses_the_session_container_when_omitted(wired, monkeypatch):
    async def fake_list(container, module, runner=None):
        assert container == "integra19"
        assert module == "sale"

        return {
            "ok": True,
            "module": "sale",
            "path": "/opt/odoo/odoo/addons/sale",
            "classes": [],
            "error": None,
            "error_code": None,
        }

    monkeypatch.setattr("odoo_sheller.discovery.list_tests", fake_list)
    result = await server.os_list_tests("sale")
    assert result["module"] == "sale"
    assert result["classes"] == []
    assert "ok" not in result


async def test_os_list_tests_no_session_requires_container(wired, monkeypatch):
    monkeypatch.setattr(server, "_keys", {})
    refusal = await server.os_list_tests("sale")
    assert refusal["error"] == "no_session"
    assert "container" in refusal["recovery"]


async def test_os_list_tests_ambiguous_session_requires_container(wired, monkeypatch):
    monkeypatch.setattr(server, "_keys", {"s1": "k1", "s2": "k2"})
    refusal = await server.os_list_tests("sale")
    assert refusal["error"] == "ambiguous_session"
    assert "container" in refusal["recovery"]


async def test_os_list_tests_passes_an_explicit_container(wired, monkeypatch):
    async def fake_list(container, module, runner=None):
        assert container == "qbo19"

        return {
            "ok": True,
            "module": module,
            "path": "/x",
            "classes": [],
            "error": None,
            "error_code": None,
        }

    monkeypatch.setattr("odoo_sheller.discovery.list_tests", fake_list)
    monkeypatch.setattr(server, "_keys", {})
    result = await server.os_list_tests("widget", container="qbo19")
    assert result["module"] == "widget"


def test_the_instructions_point_at_os_list_tests():
    text = guidance()
    assert "os_list_tests" in text
    assert "inventing" in text.lower()
    assert "parallel" in text.lower()


async def test_os_session_reports_whether_commit_is_granted(wired):
    """A grant happens in the UI; the agent has to read it, not wait to be told."""
    described = await server.os_session()
    assert described["id"] == "s1"
    assert described["allow_commit"] is False
    assert described["state"] == "ready"
    wired.session.allow_commit = True
    assert (await server.os_session())["allow_commit"] is True


def test_the_instructions_say_to_poll_os_session_for_a_grant():
    """'Wait' without a tool leaves the agent asking the human if they granted it."""
    text = guidance()
    assert "os_session" in text
    assert "allow_commit" in text
    assert "will not be told in chat" in text.lower()


def test_the_instructions_say_to_close_a_finished_session():
    text = guidance()
    assert "os_close_session" in text
    assert "more steps" in text


def test_the_instructions_say_how_to_run_with_delay_inline():
    text = guidance()
    assert "queue_job__no_delay" in text
    assert "with_delay()" in text
    assert "env = env(context=dict(env.context, queue_job__no_delay=True))" in text


# --- long runs: budget, polling, auto-closing sessions -------------------


def _cut_at_budget(monkeypatch, *, after=1):
    """A client that answers `after` calls, then times out like the host does."""
    calls = {"n": 0}

    class Client:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, path, **kwargs):
            calls["n"] += 1
            if calls["n"] <= after:

                return httpx.Response(
                    200, json={"id": "ab12", "write_key": "k", "container": "c"}
                )
            raise httpx.ReadTimeout("budget")

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)

    return calls


async def test_run_test_opens_a_self_closing_session(wired):
    await server.os_run_test("sale.TestSaleOrder", container="c", database="db")
    assert wired.open_kwargs["autoclose"] is True, (
        "a test session must not outlive its run"
    )


async def test_a_long_run_is_not_reported_as_a_failure(monkeypatch):
    """The host cuts the call long before the run ends; that is not an error."""
    _cut_at_budget(monkeypatch)
    monkeypatch.setattr(server, "_keys", {})

    answer = await server.os_run_test("qbo.TestBig", timeout=300)

    assert answer.get("error") is None, "still running is not a failure"
    assert answer["status"] == "running"
    assert answer["session_id"] == "ab12"
    assert answer["test"] == "qbo.TestBig"
    assert "os_test_result" in answer["recovery"]


async def test_the_daemon_keeps_the_full_timeout_the_caller_asked_for(monkeypatch):
    """Only our own waiting is cut short — the run itself still gets 300s."""
    sent = []

    class Client:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")
            sent.append(self.timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, path, **kwargs):
            if path.endswith("/run_test"):
                sent.append(kwargs["json"]["timeout"])
                raise httpx.ReadTimeout("budget")

            return httpx.Response(200, json={"id": "ab12", "write_key": "k"})

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    monkeypatch.setattr(server, "_keys", {})

    await server.os_run_test("qbo.TestBig", timeout=300)

    assert 300 in sent, "the daemon's own ceiling stays what the caller asked"
    client_legs = [t for t in sent if t != 300]
    assert client_legs and max(client_legs) <= server.MCP_CALL_BUDGET, (
        "no leg of the call may outlast the budget the host allows"
    )


async def test_os_test_result_returns_the_outcome_once_it_lands(monkeypatch):
    feed = {
        "session": {"session_id": "ab12", "state": "gone"},
        "entries": [{
            "kind": "run_test", "id": 1, "ordinal": 1,
            "module": "qbo", "test_class": "TestBig", "status": "done",
            "actor": {"kind": "agent", "label": "mcp-agent"},
            "result": {
                "stdout": "", "stderr": [], "error": None, "duration": 88.4,
                "test": {"tests_run": 62, "failures": 0, "errors": 0,
                          "skipped": 0, "success": True},
            },
        }],
    }

    async def fake_call(method, path, session_id=None, **kwargs):

        return feed

    monkeypatch.setattr(server, "_call", fake_call)
    answer = await server.os_test_result("ab12")

    assert answer["status"] == "done"
    assert answer["tests_run"] == 62
    assert answer["success"] is True
    assert answer["test"] == "qbo.TestBig"


async def test_os_test_result_waits_rather_than_saying_running_at_once(monkeypatch):
    """A bare peek would leave the agent spinning with no way to pause."""
    seen = {"n": 0}
    landed = {
        "session": {"session_id": "ab12", "state": "ready"},
        "entries": [{
            "kind": "run_test", "id": 1, "module": "qbo", "test_class": "TestBig",
            "status": "done",
            "result": {"duration": 1.0, "test": {"tests_run": 4, "success": True}},
        }],
    }
    running = {
        "session": {"session_id": "ab12", "state": "busy"},
        "entries": [{
            "kind": "run_test", "id": 1, "module": "qbo", "test_class": "TestBig",
            "status": "running", "result": None,
        }],
    }

    async def fake_call(method, path, session_id=None, **kwargs):
        seen["n"] += 1

        return landed if seen["n"] >= 3 else running

    monkeypatch.setattr(server, "_call", fake_call)
    monkeypatch.setattr(server, "TEST_RESULT_POLL", 0.01)
    answer = await server.os_test_result("ab12")

    assert seen["n"] >= 3, "it must keep looking, not answer on the first peek"
    assert answer["status"] == "done"
    assert answer["tests_run"] == 4


async def test_os_test_result_says_running_when_the_budget_runs_out(monkeypatch):
    running = {
        "session": {"session_id": "ab12", "state": "busy"},
        "entries": [{
            "kind": "run_test", "id": 1, "module": "qbo", "test_class": "TestBig",
            "status": "running", "result": None,
        }],
    }

    async def fake_call(method, path, session_id=None, **kwargs):

        return running

    monkeypatch.setattr(server, "_call", fake_call)
    monkeypatch.setattr(server, "TEST_RESULT_POLL", 0.01)
    monkeypatch.setattr(server, "MCP_CALL_BUDGET", 0.05)
    answer = await server.os_test_result("ab12")

    assert answer["status"] == "running"
    assert "again" in answer["recovery"]


async def test_os_test_result_reports_a_run_that_died_with_its_process(monkeypatch):
    """Without this the agent would poll a vanished run forever."""
    dead = {
        "session": {"session_id": "ab12", "state": "gone"},
        "entries": [{
            "kind": "run_test", "id": 1, "module": "qbo", "test_class": "TestBig",
            "status": "running", "result": None,
        }],
    }

    async def fake_call(method, path, session_id=None, **kwargs):

        return dead

    monkeypatch.setattr(server, "_call", fake_call)
    monkeypatch.setattr(server, "TEST_RESULT_POLL", 0.01)
    answer = await server.os_test_result("ab12")

    assert answer["status"] == "lost"
    assert answer["journal"] == "/api/journals/ab12"


def test_the_instructions_no_longer_ask_for_a_manual_close_after_a_test():
    """The session closes itself now; telling the agent otherwise wastes a call."""
    text = guidance()
    assert "one os_run_test call, one session, one close" not in text.lower()
    assert "closes itself" in text


def test_the_instructions_explain_waiting_out_a_long_run():
    text = guidance()
    assert "os_test_result" in text
    assert "status" in text and "running" in text


# --- the budget covers the whole call, not just the run leg -------------


class _Clock:
    """A loop clock we can advance, so budget arithmetic is testable."""

    def __init__(self, monkeypatch, open_cost=0.0):
        self.now = 1000.0
        self.open_cost = open_cost
        self.run_leg_timeout = None
        outer = self

        class Client:
            def __init__(self, *args, **kwargs):
                self.timeout = kwargs.get("timeout")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def request(self, method, path, **kwargs):
                if path.endswith("/run_test"):
                    outer.run_leg_timeout = self.timeout
                    raise httpx.ReadTimeout("budget")
                outer.now += outer.open_cost  # the registry load
                return httpx.Response(200, json={"id": "ab12", "write_key": "k"})

        monkeypatch.setattr(server.httpx, "AsyncClient", Client)
        monkeypatch.setattr(server, "_keys", {})

        import asyncio as _asyncio

        loop = _asyncio.get_event_loop()
        monkeypatch.setattr(loop, "time", lambda: outer.now, raising=False)


async def test_a_slow_session_open_eats_into_the_budget(monkeypatch):
    """The host times the whole tool call. Spending the budget on the run leg
    alone, after an open that already took seconds, blows straight past it."""
    clock = _Clock(monkeypatch, open_cost=12.0)

    await server.os_run_test("qbo.TestBig", timeout=600)

    assert clock.run_leg_timeout is not None
    total = 12.0 + clock.run_leg_timeout
    assert total <= server.MCP_CALL_BUDGET, (
        f"whole call would take {total}s, over the {server.MCP_CALL_BUDGET}s budget"
    )


async def test_the_budget_is_never_overshot_by_a_safety_margin(monkeypatch):
    """A margin added on top of the cap defeats the cap."""
    clock = _Clock(monkeypatch, open_cost=0.0)

    await server.os_run_test("qbo.TestBig", timeout=600)

    assert clock.run_leg_timeout <= server.MCP_CALL_BUDGET


async def test_an_open_that_ate_everything_still_starts_the_run(monkeypatch):
    """With no time left we must still send the request — otherwise nothing
    runs at all and the session_id we hand back is useless."""
    clock = _Clock(monkeypatch, open_cost=999.0)

    answer = await server.os_run_test("qbo.TestBig", timeout=600)

    assert clock.run_leg_timeout >= server.RUN_START_GRACE
    assert answer["status"] == "running"
    assert answer["session_id"] == "ab12"


async def test_the_default_budget_leaves_room_under_a_one_minute_host_cut():
    assert server.MCP_CALL_BUDGET <= 45.0


async def test_stderr_comes_back_as_the_tail_not_the_head(wired, monkeypatch):
    """The line worth reading — `Tests passed: …` — is the last one. Clipping
    from the front hands back the framework's boot chatter instead."""
    async def noisy(self, module, test_class, test_method=None, timeout=300.0):
        lines = ["Importing test framework"]
        lines += [f"filler {n}" for n in range(4000)]
        lines.append("Tests passed: 0 failed, 0 error(s) of 62 tests")

        return {
            "stdout": "", "error": None, "duration": 91.7,
            "test": {"tests_run": 62, "failures": 0, "errors": 0,
                     "skipped": 0, "success": True},
            "stderr": lines, "stderr_truncated": False, "discarded_pending": False,
        }

    monkeypatch.setattr(FakeSession, "run_test", noisy)
    result = await server.os_run_test("qbo.TestBig")

    assert len(result["stderr"]) == server.MAX_STDOUT
    assert result["stderr"].endswith("Tests passed: 0 failed, 0 error(s) of 62 tests")
    assert "Importing test framework" not in result["stderr"]


async def test_the_daemon_side_line_cap_is_reported_separately(wired, monkeypatch):
    """Dropping whole lines at the daemon and clipping characters here are two
    different losses; one flag cannot answer for both."""
    async def capped(self, module, test_class, test_method=None, timeout=300.0):

        return {
            "stdout": "", "error": None, "duration": 1.0,
            "test": {"tests_run": 1, "failures": 0, "errors": 0,
                     "skipped": 0, "success": True},
            "stderr": ["short"], "stderr_truncated": True, "discarded_pending": False,
        }

    monkeypatch.setattr(FakeSession, "run_test", capped)
    result = await server.os_run_test("qbo.TestBig")

    assert result["stderr_truncated"] is True
    assert result["truncated"] is False, "nothing was clipped for context here"


async def test_listing_sessions_forgets_keys_of_sessions_that_are_gone(wired):
    """Test sessions close themselves, so `yours` filled up with dead ids."""
    server._keys["closed-one"] = "stale"
    server._keys["closed-two"] = "stale"

    listing = await server.os_list_sessions()

    assert listing["yours"] == ["s1"], "only sessions the daemon still has"
    assert "closed-one" not in server._keys, "the stale key is dropped, not just hidden"


async def test_os_test_result_keeps_the_stderr_tail_like_os_run_test(monkeypatch):
    """The tail fix landed in os_run_test only; recovering the same run through
    os_test_result handed back the head again."""
    lines = ["Importing test framework"]
    lines += [f"filler {n}" for n in range(4000)]
    lines.append("Tests passed: 0 failed, 0 error(s) of 62 tests")
    feed = {
        "session": {"session_id": "ab12", "state": "gone"},
        "entries": [{
            "kind": "run_test", "id": 1, "ordinal": 1,
            "module": "qbo", "test_class": "TestBig", "status": "done",
            "result": {
                "stdout": "", "stderr": lines, "stderr_truncated": False,
                "error": None, "duration": 84.9,
                "test": {"tests_run": 62, "failures": 0, "errors": 0,
                          "skipped": 0, "success": True},
            },
        }],
    }

    async def fake_call(method, path, session_id=None, **kwargs):

        return feed

    monkeypatch.setattr(server, "_call", fake_call)
    answer = await server.os_test_result("ab12")

    assert len(answer["stderr"]) == server.MAX_STDOUT
    assert answer["stderr"].endswith("Tests passed: 0 failed, 0 error(s) of 62 tests")
    assert "Importing test framework" not in answer["stderr"]
    assert answer["stderr_truncated"] is False, "the daemon's own flag must survive"


async def test_os_test_result_forwards_a_daemon_side_line_cap(monkeypatch):
    feed = {
        "session": {"session_id": "ab12", "state": "gone"},
        "entries": [{
            "kind": "run_test", "id": 1, "module": "qbo", "test_class": "TestBig",
            "status": "done",
            "result": {
                "stderr": ["short"], "stderr_truncated": True, "duration": 1.0,
                "test": {"tests_run": 1, "success": True},
            },
        }],
    }

    async def fake_call(method, path, session_id=None, **kwargs):

        return feed

    monkeypatch.setattr(server, "_call", fake_call)
    answer = await server.os_test_result("ab12")
    assert answer["stderr_truncated"] is True


async def test_the_run_test_tool_description_matches_what_it_now_does():
    """The description is what the model reads before calling. It still
    promised a session to close by hand and said nothing about `running`."""
    tools = await server.mcp.list_tools()
    description = next(
        tool.description for tool in tools if tool.name == "os_run_test"
    )
    assert "os_close_session" not in description
    assert "closes itself" in description
    assert "running" in description
    assert "os_test_result" in description


def test_the_instructions_say_how_to_find_the_code_being_debugged():
    """An agent that cannot see which override wins is guessing."""
    text = guidance()
    assert "inspect.getsource" in text
    assert "__mro__" in text


def test_the_instructions_say_how_to_read_a_whole_record():
    """Naming fields one at a time is how an agent misses the field it needed."""
    text = guidance()
    assert ".read()[0]" in text, "read() always returns a list"
    assert "read(load=None)" in text
    # The two things read() does not do, both of which surprise a caller:
    # relations come back as ids, and a wide model is expensive to read whole.
    assert "display_name" in text
    assert "ids" in text


def test_the_instructions_say_how_to_update_a_module():
    """Only what works: the ORM call, what it costs, and what it cannot do."""
    text = guidance()
    assert "button_immediate_upgrade" in text
    # It commits on its own, so the human's consent comes first, and it does
    # not re-import Python.
    assert "commits" in text
    assert "new session" in text
    # What an upgrade is actually for, and the window that decides whether a
    # script runs at all.
    assert "migration" in text
    assert "manifest" in text
    # Installing is the same act with the same cost, and a module the loader
    # has never seen has no record to install.
    assert "button_immediate_install" in text
    assert "update_list" in text


def test_the_instructions_say_how_to_read_non_python_files():
    """Views and data files are XML; inspect cannot reach them."""
    text = guidance()
    assert "file_open" in text
    assert "filter_ext" in text


def test_an_agent_cannot_name_a_remote_target():
    """Decision by omission, not by check: os_open_session has no host, build
    or kind parameter, so an agent has no way to reach an odoo.sh instance —
    staging or production — except through a handover a human performed. A
    guard that cannot be forgotten because there is nothing to forget."""
    import inspect

    params = set(inspect.signature(server.os_open_session).parameters)
    assert params == {"container", "database", "odoo_bin", "replace"}
    assert not params & {"host", "build", "kind", "stage"}


def test_no_mcp_tool_offers_a_remote_target():
    """The same has to hold for every tool that can open a session."""
    import inspect

    for name in ("os_open_session", "os_run_test"):
        params = set(inspect.signature(getattr(server, name)).parameters)
        assert not params & {"host", "build", "kind", "stage"}, name


def test_the_instructions_distinguish_the_two_commit_refusals():
    """One says ask; the other says never. Polling the second one is a loop."""
    text = guidance()
    assert "commit_forbidden" in text
    assert "production" in text


# --- running a test in a session someone handed over --------------------


async def test_run_test_in_a_handed_over_session_opens_nothing(wired):
    """On odoo.sh this is the only way: an agent cannot open the target, so
    without it the tests feature is unreachable exactly where staging lives."""
    result = await server.os_run_test("qbo.TestBig", session_id="s1")

    assert not hasattr(wired, "open_kwargs"), "no session may be opened"
    assert result["session_id"] == "s1"
    assert result["tests_run"] == 1
    assert wired.session.calls[-1][:4] == ("run_test", "qbo", "TestBig", None)


async def test_run_test_does_not_close_a_session_it_was_lent(wired):
    """Closing what you were handed is not yours to do."""
    result = await server.os_run_test("qbo.TestBig", session_id="s1")

    assert "closes itself" not in str(result), "it does not, and must not claim to"
    assert "s1" in wired.sessions, "still there for the human who lent it"


async def test_run_test_refuses_a_lent_session_and_a_target_at_once(wired):
    """One says where to run, the other says where to open. Both is a muddle."""
    refusal = await server.os_run_test("qbo.TestBig", session_id="s1", container="c")

    assert refusal["error"] == "ambiguous_target"
    assert refusal["recovery"]
    assert wired.session.calls == [], "nothing may run until the caller decides"


async def test_run_test_in_a_lent_session_reports_discarded_work(wired, monkeypatch):
    """Here it stops being theoretical: the human may have left work in it, and
    Odoo's own runner rolls back a mid-transaction cursor before testing."""
    async def with_pending(self, module, test_class, test_method=None, timeout=300.0):

        return {
            "stdout": "", "error": None, "duration": 1.0,
            "test": {"tests_run": 4, "failures": 0, "errors": 0,
                     "skipped": 0, "success": True},
            "stderr": [], "discarded_pending": True,
        }

    monkeypatch.setattr(FakeSession, "run_test", with_pending)
    result = await server.os_run_test("qbo.TestBig", session_id="s1")
    assert result["discarded_pending"] is True


def test_the_instructions_say_how_to_test_on_a_lent_session():
    text = guidance()
    assert "os_run_test(session_id" in text or "session_id=" in text


async def test_os_exec_hands_back_the_log_when_it_is_asked_for(monkeypatch):
    """Without this an agent builds its own logging handler to see the log."""
    async def fake_call(method, path, session_id=None, **kwargs):

        return {
            "stdout": "", "result": "None", "error": None, "duration": 0.2,
            "stderr": ["INFO odoo: one", "ERROR odoo: two"],
            "stderr_truncated": False,
        }

    monkeypatch.setattr(server, "_call", fake_call)
    monkeypatch.setattr(server, "_keys", {"s1": "k"})
    out = await server.os_exec("x", stderr=True)
    assert out["stderr"] == "INFO odoo: one\nERROR odoo: two"
    assert out["stderr_truncated"] is False


async def test_os_exec_counts_the_log_without_spending_the_context(monkeypatch):
    """Every line in every response is a token bill nobody asked for. The
    count is one number, and it is what tells an agent a log exists at all."""
    async def fake_call(method, path, session_id=None, **kwargs):

        return {
            "stdout": "", "result": "None", "error": None, "duration": 0.2,
            "stderr": ["INFO odoo: one", "ERROR odoo: two"],
            "stderr_truncated": False,
        }

    monkeypatch.setattr(server, "_call", fake_call)
    monkeypatch.setattr(server, "_keys", {"s1": "k"})
    out = await server.os_exec("x")
    assert out["stderr_lines"] == 2
    assert "stderr" not in out, "the lines themselves are opt-in"
    assert out["journal"], "and the way to read them without re-running"


async def test_os_exec_says_nothing_about_a_log_that_is_not_there(monkeypatch):
    async def fake_call(method, path, session_id=None, **kwargs):

        return {"stdout": "ok", "result": None, "error": None, "duration": 0.1,
                "stderr": [], "stderr_truncated": False}

    monkeypatch.setattr(server, "_call", fake_call)
    monkeypatch.setattr(server, "_keys", {"s1": "k"})
    out = await server.os_exec("x")
    assert out["stderr_lines"] == 0
    assert "stderr" not in out
    assert out["journal"] is None


async def test_os_exec_clips_a_long_log_from_the_end(monkeypatch):
    """The last line is the one worth having, the same as in os_run_test."""
    async def fake_call(method, path, session_id=None, **kwargs):

        return {
            "stdout": "", "result": None, "error": None, "duration": 0.2,
            "stderr": ["x" * 500 for _ in range(40)] + ["THE LAST LINE"],
            "stderr_truncated": True,
        }

    monkeypatch.setattr(server, "_call", fake_call)
    monkeypatch.setattr(server, "_keys", {"s1": "k"})
    out = await server.os_exec("x", stderr=True)
    assert out["stderr"].endswith("THE LAST LINE")
    assert out["truncated"] is True
    assert out["stderr_truncated"] is True
    assert out["journal"]


async def test_the_exec_tool_says_the_log_comes_back_with_it():
    """An agent that is not told will build a logging handler instead."""
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    description = tools["os_exec"].description
    assert "stderr" in description
    assert "stderr_lines" in description, "the count is what says a log exists"
    text = guidance()
    assert "os_exec" in text
    # Three halves, really: the count is free, the lines are on request, and
    # the whole log is on the journal without re-running anything.
    assert "stderr_lines" in text
    assert "stderr=True" in text
    assert "os_journal" in text


# --- the host truncates server instructions -------------------------------
#
# Measured, not guessed: two different hosts delivered this server's
# instructions cut at exactly 2048 characters — mid-sentence, at the same
# offset, with a truncation marker. Everything past that simply does not
# reach the model, which is why one agent wrote its own logging handler and
# another reached for `Job.load` instead of `queue_job__no_delay`.
INSTRUCTION_CAP = 2048


def test_the_instructions_fit_in_what_the_host_delivers():
    assert len(server.INSTRUCTIONS) <= INSTRUCTION_CAP, (
        f"{len(server.INSTRUCTIONS)} chars: everything past {INSTRUCTION_CAP} "
        "is dropped before the model sees it"
    )


def test_the_instructions_spend_the_budget_on_what_cannot_be_asked_for():
    """What must arrive unprompted: the rules an agent would break unknowingly."""
    text = server.INSTRUCTIONS
    for must in (
        "session_busy",        # one command at a time
        "os_rollback",         # rollback is the default
        "commit_not_allowed",  # the grant ritual
        "commit_forbidden",    # and where nothing will ever grant it
        "os_session",          # how a grant is observed
        "not_owner",           # whose session it is
        "~/.odoo-sheller",     # what never to touch
        "os_help",             # and where the rest lives
    ):
        assert must in text, must


def test_every_help_topic_is_named_in_the_instructions_and_answers():
    text = server.INSTRUCTIONS
    assert server.HELP, "the long-form guidance has to live somewhere"
    for topic in server.HELP:
        assert topic in text, f"{topic} is unreachable: nothing names it"
        assert len(server.HELP[topic]) > 200, topic


async def test_os_help_without_a_topic_lists_them():
    out = await server.os_help()
    assert set(out["topics"]) == set(server.HELP)
    assert out["instructions_truncated_at"] == INSTRUCTION_CAP


async def test_os_help_returns_one_topic_in_full():
    out = await server.os_help("jobs")
    assert out["topic"] == "jobs"
    assert "queue_job__no_delay" in out["text"]


async def test_os_help_refuses_an_unknown_topic_with_the_list():
    out = await server.os_help("nonsense")
    assert out["error"] == "no_such_topic"
    assert set(out["topics"]) == set(server.HELP)


def test_nothing_that_used_to_be_instructions_was_simply_deleted():
    """The cap forces a move, not a loss: every rule still has a home."""
    everywhere = server.INSTRUCTIONS + "\n".join(server.HELP.values())
    for must in (
        "queue_job__no_delay",       # delayed jobs
        "__mro__",                   # which override runs
        "inspect.getsource",
        "file_open",                 # views and data
        "stderr_lines",              # the log, and that it is opt-in
        "os_journal",
        ".read()[0]",                # reading a record
        "button_immediate_install",  # installing a module
        "button_immediate_upgrade",
        "discarded_pending",         # a lent session and a test run
        "os_list_tests",
        "os_test_result",
        "mapped(",                   # ORM idioms
        "ensure_one",
        "session_gone",
    ):
        assert must in everywhere, must


async def test_os_commit_refuses_to_write_work_it_inherited(wired):
    """The agent promised to check `pending_commands` by hand before its first
    commit on a handed-over session. A promise is not a mechanism."""
    session = wired.session
    session.allow_commit = True
    session.pending_commands = 3
    session.described["inherited_pending"] = 2
    out = await server.os_commit("s1")
    assert out["error"] == "inherited_pending"
    assert out["inherited_pending"] == 2
    assert "os_commit" in out["recovery"], "and how to go ahead once said out loud"
    assert ("commit",) not in session.calls, "nothing was committed"


async def test_os_commit_writes_inherited_work_once_it_is_acknowledged(wired):
    session = wired.session
    session.allow_commit = True
    session.pending_commands = 3
    session.described["inherited_pending"] = 2
    out = await server.os_commit("s1", include_inherited=True)
    assert out.get("error") is None, out
    assert ("commit",) in session.calls


async def test_os_commit_on_your_own_work_asks_nothing(wired):
    session = wired.session
    session.allow_commit = True
    session.pending_commands = 2
    session.described["inherited_pending"] = 0
    out = await server.os_commit("s1")
    assert out.get("error") is None, out
    assert ("commit",) in session.calls


def test_the_code_topic_does_not_send_python_to_the_shell():
    """Our own wording sent an agent to `docker exec sed`: it framed file_open
    as the reader for files that are *not* Python. It reads any file inside
    the addons tree, .py included — verified on a live container."""
    code = server.HELP["code"]
    assert "not Python" not in code, "the sentence that caused it"
    assert ".py" in code, "say plainly that Python files are readable too"
    # And the thing sed was reached for: a line range.
    assert "splitlines()" in code
    assert "[362:470]" in code or "first" in code


def test_the_delivered_rules_forbid_shelling_into_the_container():
    """The one line an agent needs without having to ask for a topic first."""
    text = server.INSTRUCTIONS
    assert "docker" in text
    assert len(text) <= server.INSTRUCTION_CAP


async def test_os_source_reads_a_line_range_the_way_sed_would(wired):
    """The shortcut it has to beat is `docker exec ... sed -n '363,470p'`."""
    session = wired.session
    session.source_payload = {
        "path": "account_accountant/models/account_bank_statement.py",
        "total_lines": 2026, "first": 363, "last": 470,
        "text": "    def _try_auto_reconcile_statement_lines(self):\n        pass\n",
    }
    out = await server.os_source(
        path="account_accountant/models/account_bank_statement.py", first=363, last=470
    )
    assert out["path"].endswith("account_bank_statement.py")
    assert out["first"] == 363
    assert out["total_lines"] == 2026
    assert "_try_auto_reconcile_statement_lines" in out["text"]
    sent = [call for call in session.calls if call[0] == "execute"][-1][1]
    assert "file_open" in sent, "Odoo's own reader, confined to the addons paths"
    assert "docker" not in sent


async def test_os_source_of_a_method_says_whose_override_won(wired):
    """The question the filesystem cannot answer, and the reason to prefer this."""
    session = wired.session
    session.source_payload = {
        "model": "account.move", "method": "_post",
        "file": "/opt/enterprise/account_accountant/models/account_move.py",
        "line": 1204,
        "defined_in": "odoo.addons.account_accountant.models.account_move",
        "overrides": [
            {"module": "account_accountant", "line": 1204},
            {"module": "account", "line": 6179},
        ],
        "text": "def _post(self, soft=True):\n    return super()._post(soft)\n",
    }
    out = await server.os_source(model="account.move", method="_post")
    assert out["defined_in"].endswith("account_accountant.models.account_move")
    assert out["overrides"][0]["module"] == "account_accountant", "winner first"
    assert out["line"] == 1204
    sent = [call for call in session.calls if call[0] == "execute"][-1][1]
    assert "__mro__" in sent
    assert "getsource" in sent


async def test_os_source_leaves_no_names_behind_in_the_workspace(wired):
    """A session is the human's workspace; a read must not litter it."""
    wired.session.source_payload = {"path": "sale/__manifest__.py", "total_lines": 3,
                                    "first": 1, "last": 3, "text": "{}"}
    await server.os_source(path="sale/__manifest__.py")
    sent = [call for call in wired.session.calls if call[0] == "execute"][-1][1]
    # One function, whose imports and locals stay inside it, and which pops
    # its own name on the way out.
    assert sent.count("\ndef ") == 0 and sent.startswith("def _os_read(")
    # Popped first, so a read that raises leaves nothing behind either.
    body = sent.splitlines()
    assert body[1].strip() == '_os_ns.pop("_os_read", None)'
    assert sent.strip().endswith("_os_read()")
    # And it returns rather than prints: this server's stdout is JSON-RPC.
    assert "print(" not in sent


async def test_os_source_refuses_a_call_that_names_nothing(wired):
    out = await server.os_source()
    assert out["error"] == "nothing_to_read"
    assert "path" in out["recovery"] and "model" in out["recovery"]


async def test_os_source_refuses_a_path_and_a_model_at_once(wired):
    out = await server.os_source(path="sale/__manifest__.py", model="sale.order")
    assert out["error"] == "ambiguous_request"


async def test_os_source_clips_a_long_read_and_says_how_to_narrow_it(wired):
    wired.session.source_payload = {
        "path": "big/file.py", "total_lines": 9000, "first": 1, "last": 9000,
        "text": "x" * 40_000,
    }
    out = await server.os_source(path="big/file.py")
    assert len(out["text"]) <= server.MAX_SOURCE
    assert out["truncated"] is True
    assert "first" in out["recovery"] and "last" in out["recovery"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"path": "sale/models/sale_order.py"},
        {"path": "sale/models/sale_order.py", "first": 10},
        {"path": "sale/models/sale_order.py", "last": 40},
        {"path": "sale/models/sale_order.py", "first": 10, "last": 40},
        {"model": "sale.order"},
        {"model": "sale.order", "method": "action_confirm"},
    ],
)
def test_the_source_snippet_is_python_in_every_form(kwargs):
    """A mocked session never runs the snippet, so a snippet that is not even
    Python passes every other test here. It did: `json.dumps(None)` put a
    JavaScript `null` in the source, and only a live container noticed."""
    code = server._source_snippet(
        kwargs.get("path"), kwargs.get("model"), kwargs.get("method"),
        kwargs.get("first"), kwargs.get("last"),
    )
    compile(code, "<snippet>", "exec")
    assert "null" not in code


def test_the_source_snippet_actually_reads_with_stubs(monkeypatch, tmp_path):
    """Run it for real against a stub `odoo.tools` — the cheapest thing that
    exercises the code path a mocked session leaves untouched."""
    import sys
    import types

    sample = tmp_path / "thing.py"
    sample.write_text("\n".join(f"line {n}" for n in range(1, 51)), encoding="utf-8")

    tools = types.ModuleType("odoo.tools")
    tools.file_open = lambda path, *a, **kw: open(sample, encoding="utf-8")  # noqa: SIM115
    # file_path resolves a module-relative path; here everything resolves to
    # the one sample file, so the read branch is what runs.
    tools.file_path = lambda path, *a, **kw: str(sample)
    odoo = types.ModuleType("odoo")
    odoo.tools = tools
    monkeypatch.setitem(sys.modules, "odoo", odoo)
    monkeypatch.setitem(sys.modules, "odoo.tools", tools)

    def run(**kwargs):
        code = server._source_snippet(
            kwargs.get("path"), kwargs.get("model"), kwargs.get("method"),
            kwargs.get("first"), kwargs.get("last"),
        )
        namespace: dict = {}
        tree = ast.parse(code)
        tail = ast.Expression(tree.body.pop().value)
        ast.fix_missing_locations(tail)
        exec(compile(tree, "<snippet>", "exec"), namespace)  # noqa: S102
        out = json.loads(eval(compile(tail, "<snippet>", "eval"), namespace))
        assert "_os_read" not in namespace, "the read must leave nothing behind"

        return out

    whole = run(path="module/thing.py")
    assert whole["total_lines"] == 50
    assert whole["first"] == 1 and whole["last"] == 50
    assert whole["text"].startswith("line 1\n")
    assert whole["text"].rstrip().endswith("line 50")

    ranged = run(path="module/thing.py", first=10, last=12)
    assert ranged["text"] == "line 10\nline 11\nline 12"

    open_ended = run(path="module/thing.py", first=48)
    assert open_ended["last"] == 50

    past_the_end = run(path="module/thing.py", first=45, last=9999)
    assert past_the_end["last"] == 50, "clamped to what the file has"


def test_a_method_read_lists_only_the_modules_that_define_it():
    """On account.move.action_post the MRO is 52 classes and exactly three of
    them define the method. Listing all 52 answers a question nobody asked."""
    code = server._source_snippet(None, "account.move", "action_post", None, None)
    assert "__dict__" in code, "definers, not everything in the MRO"
    assert "for klass in cls.__mro__" in code


async def test_a_method_read_can_be_pinned_to_one_module_in_the_chain(wired):
    session = wired.session
    session.source_payload = {
        "model": "account.move", "method": "action_post", "module": "sale",
        "file": "/opt/odoo/addons/sale/models/account_move.py", "line": 83,
        "defined_in": "odoo.addons.sale.models.account_move",
        "overrides": [{"module": "integration", "line": 43},
                      {"module": "sale", "line": 83},
                      {"module": "account", "line": 6179}],
        "text": "    def action_post(self):\n        return super().action_post()\n",
    }
    out = await server.os_source(
        model="account.move", method="action_post", module="sale"
    )
    assert out["line"] == 83
    assert out["defined_in"].endswith("sale.models.account_move")
    sent = [call for call in session.calls if call[0] == "execute"][-1][1]
    assert "'sale'" in sent or '"sale"' in sent


async def test_a_module_without_the_method_refuses_with_the_chain(wired):
    wired.session.source_payload = {
        "error": "not_in_that_module",
        "model": "account.move", "method": "action_post", "module": "stock",
        "overrides": [{"module": "integration", "line": 43},
                      {"module": "account", "line": 6179}],
    }
    out = await server.os_source(model="account.move", method="action_post", module="stock")
    assert out["error"] == "not_in_that_module"
    assert [link["module"] for link in out["overrides"]] == ["integration", "account"]


async def test_pinning_a_module_needs_a_method(wired):
    out = await server.os_source(model="account.move", module="sale")
    assert out["error"] == "module_without_method"


async def test_os_source_of_a_directory_lists_what_is_in_it(wired):
    """The question before "read me line 363" is "what files are there".
    Answering it in the same tool keeps that from being a shell command too."""
    wired.session.source_payload = {
        "path": "integration_shopify",
        "directory": True,
        "entries": [
            {"path": "integration_shopify/__manifest__.py", "lines": 42},
            {"path": "integration_shopify/models/external/external_payout.py", "lines": 1080},
            {"path": "integration_shopify/data/ir_config_parameter_data.xml", "lines": 17},
        ],
        "files": 3,
    }
    out = await server.os_source(path="integration_shopify")
    assert out["directory"] is True
    assert out["files"] == 3
    assert any(e["path"].endswith("external_payout.py") for e in out["entries"])
    sent = [call for call in wired.session.calls if call[0] == "execute"][-1][1]
    assert "isdir" in sent, "one call answers for a file or a directory"
    assert "walk" in sent


def test_a_module_tree_is_confined_the_way_a_file_read_is():
    """A listing must not become the way out of the addons paths."""
    code = server._source_snippet("integration_shopify", None, None, None, None)
    assert "file_path" in code, "Odoo resolves the module directory, not us"
    assert "os.walk" in code


def test_a_listing_returns_paths_you_can_ask_for():
    """A subdirectory listed paths relative to its own parent — `external/x.py`
    — which is not something os_source can be called with. Every entry has to
    be module-relative, the way the caller writes them."""
    code = server._source_snippet("integration_shopify/models/external", None, None,
                                  None, None)
    assert "__manifest__.py" in code, "the module root is where a path is rooted"
