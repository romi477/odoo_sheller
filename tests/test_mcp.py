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
            "activity": None,
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


def test_the_instructions_explain_os_run_test():
    text = server.INSTRUCTIONS
    assert "os_run_test" in text
    assert "module.TestClass" in text
    assert "discards" in text.lower() or "discarded" in text.lower()


def test_the_instructions_say_not_to_retry_a_run_that_is_still_going():
    """Retrying would start a brand-new, duplicate run instead of waiting on
    the slow one already in flight."""
    text = server.INSTRUCTIONS.lower()
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
    text = server.INSTRUCTIONS
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
    text = server.INSTRUCTIONS
    assert "os_session" in text
    assert "allow_commit" in text
    assert "will not be told in chat" in text.lower()


def test_the_instructions_say_to_close_a_finished_session():
    text = server.INSTRUCTIONS
    assert "os_close_session" in text
    assert "more steps" in text


def test_the_instructions_say_how_to_run_with_delay_inline():
    text = server.INSTRUCTIONS
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
    text = server.INSTRUCTIONS
    assert "one os_run_test call, one session, one close" not in text.lower()
    assert "closes itself" in text


def test_the_instructions_explain_waiting_out_a_long_run():
    text = server.INSTRUCTIONS
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
    text = server.INSTRUCTIONS
    assert "inspect.getsource" in text
    assert "__mro__" in text


def test_the_instructions_say_how_to_read_a_whole_record():
    """Naming fields one at a time is how an agent misses the field it needed."""
    text = server.INSTRUCTIONS
    assert ".read()[0]" in text, "read() always returns a list"
    assert "read(load=None)" in text
    # The two things read() does not do, both of which surprise a caller:
    # relations come back as ids, and a wide model is expensive to read whole.
    assert "display_name" in text
    assert "ids" in text


def test_the_instructions_say_how_to_update_a_module():
    """Only what works: the ORM call, what it costs, and what it cannot do."""
    text = server.INSTRUCTIONS
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
    text = server.INSTRUCTIONS
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
    text = server.INSTRUCTIONS
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
    text = server.INSTRUCTIONS
    assert "os_run_test(session_id" in text or "session_id=" in text
