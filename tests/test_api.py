import json
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from odoo_sheller import journal
from odoo_sheller.api import create_app
from odoo_sheller.registry import Registry
from odoo_sheller.session import (
    CommitNotAllowed,
    SessionBusy,
    SessionDead,
    SessionNotReady,
    SessionState,
)

OWNER_KEY = "write-key-s1"
ADMIN_KEY = "admin-key"


class FakeSession:
    def __init__(self):
        self.id = "s1"
        self.state = SessionState.READY
        self.pending_commands = 0
        self.hello = {"odoo": "19.0", "python": "3.12.3", "pid": 4242}
        self.calls = []
        self.raises = None
        self.write_key = OWNER_KEY
        self.former_keys = set()
        self.owner = {"kind": "human", "label": "browser"}
        self.allow_commit = True

    def describe(self):

        return {
            "id": self.id,
            "state": self.state.value,
            "container": "c",
            "database": "db",
            "odoo": "19.0",
            "python": "3.12.3",
            "pending_commands": self.pending_commands,
            "owner": dict(self.owner),
            "allow_commit": self.allow_commit,
            "activity": None,
        }

    def held_by(self, key):

        return bool(key) and (key == self.write_key or key in self.former_keys)

    def transfer_owner(self, owner):
        self.calls.append(("transfer_owner", owner))
        self.former_keys.add(self.write_key)
        self.owner = dict(owner)
        self.write_key = "rotated-key"
        self.allow_commit = self.owner.get("kind") == "human"

        return self.write_key

    def set_allow_commit(self, allowed):
        self.calls.append(("set_allow_commit", allowed))
        self.allow_commit = allowed

    def stderr_tail(self, limit=200):

        return ["WARNING something"]

    async def execute(self, code, timeout=300.0):
        self.calls.append(("execute", code))
        if self.raises:
            raise self.raises

        return {
            "id": 1,
            "stdout": "out\n",
            "stdout_truncated": False,
            "result": "42",
            "result_truncated": False,
            "error": None,
            "duration": 0.01,
        }

    async def commit(self):
        self.calls.append(("commit",))
        if self.raises:
            raise self.raises

        return {"error": None}

    async def rollback(self):
        self.calls.append(("rollback",))

        return {"error": None}

    async def interrupt(self):
        self.calls.append(("interrupt",))

    async def run_test(self, module, test_class, test_method=None, timeout=300.0):
        self.calls.append(("run_test", module, test_class, test_method))
        if self.raises:
            raise self.raises

        return {
            "stdout": "", "error": None, "duration": 0.01,
            "test": {
                "module": module, "test_class": test_class, "test_method": test_method,
                "tests_run": 1, "failures": 0, "errors": 0, "skipped": 0, "success": True,
            },
            "stderr": [], "discarded_pending": False,
        }

    async def close(self, timeout=10.0):
        self.calls.append(("close",))
        self.state = SessionState.CLOSED

    async def kill(self):
        self.calls.append(("kill",))
        self.state = SessionState.CLOSED


class FakeRegistry:
    def __init__(self):
        self.session = FakeSession()
        self.sessions = {"s1": self.session}
        self.subscribers = {}
        self.admin_key = ADMIN_KEY
        self.past_target = None
        self.watchers = []

    def watch(self, queue):
        self.watchers.append(queue)

    def unwatch(self, queue):
        if queue in self.watchers:
            self.watchers.remove(queue)

    def target_of_past_session(self, session_id):

        return self.past_target

    def journal_file_for(self, session_id):

        return None

    async def open(self, **kwargs):
        # Mirrors the real signature loosely on purpose: the point is to record
        # what the endpoint forwarded, not to re-type it here.
        self.open_kwargs = kwargs

        return self.session

    def get(self, session_id):
        if session_id not in self.sessions:
            raise KeyError(session_id)

        return self.sessions[session_id]

    async def close(self, session_id, force=False):
        session = self.get(session_id)
        await (session.kill() if force else session.close())
        del self.sessions[session_id]

    def subscribe(self, session_id, queue):
        if session_id not in self.sessions:
            raise KeyError(session_id)
        self.subscribers.setdefault(session_id, []).append(queue)

    def unsubscribe(self, session_id, queue):
        self.subscribers.get(session_id, []).remove(queue)


@pytest.fixture
def client():
    registry = FakeRegistry()
    app = create_app(registry=registry)
    with TestClient(app, headers={"X-OS-Session-Key": OWNER_KEY}) as test_client:
        test_client.registry = registry
        yield test_client


def test_exec_returns_the_result(client):
    response = client.post("/api/sessions/s1/exec", json={"code": "1 + 1"})
    assert response.status_code == 200
    assert response.json()["result"] == "42"
    assert client.registry.session.calls == [("execute", "1 + 1")]


def test_exec_on_busy_session_is_409(client):
    client.registry.session.raises = SessionBusy("busy")
    response = client.post("/api/sessions/s1/exec", json={"code": "1 + 1"})
    assert response.status_code == 409
    assert "busy" in response.json()["detail"]


def test_exec_on_starting_session_is_409(client):
    client.registry.session.raises = SessionNotReady("still starting")
    response = client.post("/api/sessions/s1/exec", json={"code": "1 + 1"})
    assert response.status_code == 409


def test_exec_on_dead_session_is_410(client):
    client.registry.session.raises = SessionDead("process ended")
    response = client.post("/api/sessions/s1/exec", json={"code": "1 + 1"})
    assert response.status_code == 410
    detail = response.json()["detail"]
    assert detail["error"] == "session_gone"
    assert "process ended" in detail["reason"]
    assert detail["recovery"]


def test_exec_timeout_is_504(client):
    client.registry.session.raises = TimeoutError("command exceeded 300s, interrupt sent")
    response = client.post("/api/sessions/s1/exec", json={"code": "SLEEP"})
    assert response.status_code == 504


def test_unknown_session_is_404(client):
    assert client.post("/api/sessions/nope/exec", json={"code": "1"}).status_code == 404


def test_run_test_parses_class_only_spec(client):
    response = client.post("/api/sessions/s1/run_test", json={"test": "sale.TestSaleOrder"})
    assert response.status_code == 200
    assert response.json()["test"]["success"] is True
    assert client.registry.session.calls[-1] == ("run_test", "sale", "TestSaleOrder", None)


def test_run_test_parses_class_and_method_spec(client):
    response = client.post(
        "/api/sessions/s1/run_test", json={"test": "sale.TestSaleOrder.test_x"}
    )
    assert response.status_code == 200
    assert client.registry.session.calls[-1] == ("run_test", "sale", "TestSaleOrder", "test_x")


def test_run_test_rejects_a_malformed_spec(client):
    response = client.post("/api/sessions/s1/run_test", json={"test": "not a valid spec!!"})
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_test_spec"
    assert client.registry.session.calls == []


@pytest.mark.parametrize("bad", [0, -1, 100000])
def test_run_test_rejects_a_timeout_that_cannot_work(client, bad):
    """timeout=0 sends the frame and gives up on it at once: the run really
    starts, and the session stays busy for its whole length."""
    response = client.post(
        "/api/sessions/s1/run_test", json={"test": "sale.TestSaleOrder", "timeout": bad}
    )
    assert response.status_code == 422
    assert client.registry.session.calls == [], "nothing may reach the container"


def test_run_test_accepts_a_long_but_sane_timeout(client):
    response = client.post(
        "/api/sessions/s1/run_test", json={"test": "sale.TestSaleOrder", "timeout": 600}
    )
    assert response.status_code == 200
    assert client.registry.session.calls[-1] == ("run_test", "sale", "TestSaleOrder", None)


def test_run_test_on_busy_session_is_409(client):
    client.registry.session.raises = SessionBusy("busy")
    response = client.post("/api/sessions/s1/run_test", json={"test": "sale.TestSaleOrder"})
    assert response.status_code == 409


def test_run_test_without_the_write_key_is_403(client):
    response = client.post(
        "/api/sessions/s1/run_test",
        json={"test": "sale.TestSaleOrder"},
        headers={"X-OS-Session-Key": ""},
    )
    assert response.status_code == 403
    assert client.registry.session.calls == []


class FakeJournal:
    def __init__(self, records=None):
        self._records = list(records or [])

    def records(self):

        return list(self._records)


def test_session_history_rebuilds_feed_from_the_journal(client):
    records = [
        {"kind": "exec", "id": 1, "code": "1 + 1"},
        {"kind": "result", "id": 1, "stdout": "", "error": None, "duration": 0.01},
        {"kind": "commit", "id": 2, "error": None},
    ]
    client.registry.session.journal = FakeJournal(records)
    response = client.get("/api/sessions/s1/history")
    assert response.status_code == 200
    body = response.json()
    expected = journal.feed_from_records(records)
    assert body["history"] == expected["history"]
    assert body["entries"] == expected["entries"]


def test_session_history_carries_session_meta(client):
    records = [
        {"kind": "session_open", "ts": "t0", "container": "c", "database": "db",
         "odoo": "19.0", "python": "3.12.13", "pid": 42},
        {"kind": "exec", "id": 1, "code": "1 + 1"},
        {"kind": "result", "id": 1, "stdout": "", "error": None, "duration": 0.01},
    ]
    client.registry.session.journal = FakeJournal(records)
    meta = client.get("/api/sessions/s1/history").json()["session"]
    assert meta["session_id"] == "s1"
    assert meta["container"] == "c"
    assert meta["database"] == "db"
    assert meta["odoo"] == "19.0"
    assert meta["pid"] == 42
    assert meta["commands"] == 1
    assert meta["committed"] is False
    assert meta["unmasked"] is True
    # The live session overrides what the journal remembers.
    assert meta["state"] == "ready"


def test_open_passes_the_client_token_through(client):
    """The browser has to recognise its own session_starting among several."""
    response = client.post(
        "/api/sessions",
        json={
            "container": "c", "database": "db",
            "odoo_bin": "/odoo-bin", "client_token": "tab-7",
        },
    )
    assert response.status_code == 200
    assert client.registry.open_kwargs["client_token"] == "tab-7"


def test_session_history_unknown_session_is_404(client):
    assert client.get("/api/sessions/nope/history").status_code == 404


def test_session_history_reads_a_closed_journal(tmp_path):
    """The Journals tab and /history share an id; the session need not still be live."""
    _journal_with_records(tmp_path, session_id="d6227894a75c")
    with TestClient(create_app(registry=Registry(journal_root=tmp_path))) as client:
        response = client.get("/api/sessions/d6227894a75c/history")
    assert response.status_code == 200
    body = response.json()
    assert body["history"] == ["env['res.partner'].search_count([])"]
    assert body["session"]["session_id"] == "d6227894a75c"
    assert body["session"]["ended_as"] == "session_close"
    # The transcript comes back, but never without saying the session is over:
    # an agent that reads only `entries` would carry on as if it could type.
    assert body["session"]["state"] == "gone"
    assert body["session"]["gone"] == {
        "error": "session_gone",
        "session_id": "d6227894a75c",
        "reason": "not registered",
        "target": {
            "container": "integra19",
            "database": "integra_db_19",
            "odoo_bin": None,
        },
        "journal": "/api/journals/d6227894a75c",
        "recovery": "open a new session on the same target; variables are lost",
    }


def test_session_history_of_a_live_session_carries_no_gone_marker(client):
    client.registry.session.journal = FakeJournal([{"kind": "exec", "id": 1, "code": "1"}])
    body = client.get("/api/sessions/s1/history").json()
    assert "gone" not in body, "everything about the session lives under `session`"
    assert "gone" not in body["session"]
    assert body["session"]["state"] != "gone"


def test_session_history_includes_journalled_stderr_only_when_asked(client):
    records = [
        {"kind": "exec", "id": 1, "code": "1"},
        {"kind": "stderr", "ts": "t1", "line": "INFO x"},
        {"kind": "result", "id": 1, "error": None},
    ]
    client.registry.session.journal = FakeJournal(records)
    body = client.get("/api/sessions/s1/history").json()
    assert "logs" not in body
    with_logs = client.get("/api/sessions/s1/history", params={"logs": True}).json()
    assert with_logs["logs"] == [{"ts": "t1", "line": "INFO x"}]
    assert with_logs["entries"] == body["entries"]
    as_browser = client.get("/api/sessions/s1/history?logs=true").json()
    assert as_browser["logs"] == with_logs["logs"]


def test_transaction_and_control_routes(client):
    assert client.post("/api/sessions/s1/commit").status_code == 200
    assert client.post("/api/sessions/s1/rollback").status_code == 200
    assert client.post("/api/sessions/s1/interrupt").status_code == 200
    assert [call[0] for call in client.registry.session.calls] == [
        "commit",
        "rollback",
        "interrupt",
    ]


def test_delete_closes_and_force_kills(client):
    assert client.delete("/api/sessions/s1").status_code == 200
    assert ("close",) in client.registry.session.calls


def test_sessions_and_logs(client):
    listing = client.get("/api/sessions").json()
    assert listing[0]["id"] == "s1"
    assert client.get("/api/sessions/s1/logs").json()["lines"] == ["WARNING something"]


def test_websocket_delivers_published_events(client):
    with client.websocket_connect("/ws/sessions/s1") as socket:
        queue = client.registry.subscribers["s1"][0]
        queue.put_nowait({"kind": "state", "state": "busy"})
        assert socket.receive_json() == {"kind": "state", "state": "busy"}


def test_websocket_unknown_session_fails_promptly(client):
    with client.websocket_connect("/ws/sessions/nope") as socket:
        assert socket.receive_json() == {"error": "no session nope"}


def test_journals_use_registry_journal_root(tmp_path):
    session_id = "abc123"
    path = journal.journal_path(
        tmp_path,
        session_id,
        "c",
        "db",
        datetime(2026, 8, 15, 14, 30, 5, tzinfo=UTC),
    )
    journal.Journal(path).write("session_open", container="c", database="db", odoo="19.0")

    app = create_app(registry=Registry(journal_root=tmp_path))
    with TestClient(app) as test_client:
        entries = test_client.get("/api/journals").json()
        assert len(entries) == 1
        assert entries[0]["session_id"] == session_id


def test_index_is_served(client):
    response = client.get("/web")
    assert response.status_code == 200
    assert "odoo-sheller" in response.text


def test_root_redirects_to_web(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].endswith("/web")


def test_swagger_docs_are_served(client):
    response = client.get("/docs")
    assert response.status_code == 200
    assert "swagger" in response.text.lower()
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "odoo-sheller"


def test_swagger_pulls_nothing_from_a_third_party_but_the_bundle(client):
    """This page is the one that needs network. Name exactly what it reaches for."""
    hosts = set(re.findall(r"https?://([^/\"')\s]+)", client.get("/docs").text))
    assert hosts == {"cdn.jsdelivr.net"}, (
        "only the swagger-ui bundle may be remote; the favicon is served locally"
    )
    assert "/static/logo.svg" in client.get("/docs").text


def test_session_history_caps_the_log_tail(client):
    records = [{"kind": "stderr", "ts": f"t{i}", "line": f"line {i}"} for i in range(30)]
    client.registry.session.journal = FakeJournal(records)
    body = client.get("/api/sessions/s1/history", params={"logs": True, "log_tail": 5}).json()
    assert len(body["logs"]) == 5
    assert body["logs"][-1]["line"] == "line 29"
    assert body["logs_truncated"] is True


def test_session_history_rejects_an_absurd_log_tail(client):
    response = client.get("/api/sessions/s1/history", params={"logs": True, "log_tail": 999999})
    assert response.status_code == 422


def _journal_with_records(root, session_id="abc123"):
    path = journal.journal_path(
        root, session_id, "integra19", "integra_db_19", datetime(2026, 8, 15, 14, 30, 5, tzinfo=UTC)
    )
    log = journal.Journal(path)
    log.write("session_open", container="integra19", database="integra_db_19",
              odoo="19.0", python="3.12.13", pid=42)
    log.write("exec", id=1, code="env['res.partner'].search_count([])")
    log.write("result", id=1, stdout="", result="7", error=None, duration=0.05)
    log.write("commit", id=2, error=None)
    log.write("session_close")

    return path


def test_jsonl_export_leads_with_export_meta(tmp_path):
    _journal_with_records(tmp_path)
    with TestClient(create_app(registry=Registry(journal_root=tmp_path))) as client:
        response = client.get("/api/journals/abc123", params={"fmt": "jsonl"})
        assert response.status_code == 200
        lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]

    meta = lines[0]
    assert meta["kind"] == "export_meta"
    assert meta["session_id"] == "abc123"
    assert meta["container"] == "integra19"
    assert meta["database"] == "integra_db_19"
    assert meta["odoo"] == "19.0"
    assert meta["commands"] == 1
    assert meta["committed"] is True
    assert meta["unmasked"] is True
    # The journal's own records follow untouched.
    assert [record["kind"] for record in lines[1:]] == [
        "session_open", "exec", "result", "commit", "session_close",
    ]


def test_markdown_export_states_the_session_up_front(tmp_path):
    _journal_with_records(tmp_path)
    with TestClient(create_app(registry=Registry(journal_root=tmp_path))) as client:
        text = client.get("/api/journals/abc123", params={"fmt": "markdown"}).text

    head = text.split("## ")[0]
    assert "abc123" in head
    assert "integra19" in head
    assert "integra_db_19" in head
    assert "19.0" in head
    assert "unmasked" in head.lower()


def test_exports_are_named_after_the_journal(tmp_path):
    path = _journal_with_records(tmp_path)
    with TestClient(create_app(registry=Registry(journal_root=tmp_path))) as client:
        for fmt, suffix in (("jsonl", "jsonl"), ("markdown", "md")):
            response = client.get("/api/journals/abc123", params={"fmt": fmt})
            assert f'filename="{path.stem}.{suffix}"' in response.headers["content-disposition"]


def test_delete_journal_removes_the_file(tmp_path):
    path = _journal_with_records(tmp_path)
    registry = Registry(journal_root=tmp_path)
    with TestClient(create_app(registry=registry)) as client:
        response = client.delete(
            "/api/journals/abc123", headers={"X-OS-Admin-Key": registry.admin_key}
        )
        assert response.status_code == 200
        assert response.json() == {"deleted": "abc123"}
        assert client.get("/api/journals").json() == []
    assert not path.exists()


def test_delete_journal_needs_the_admin_key(tmp_path):
    """The only irreversible file operation in the API keeps the same guard."""
    path = _journal_with_records(tmp_path)
    registry = Registry(journal_root=tmp_path)
    with TestClient(create_app(registry=registry)) as client:
        assert client.delete("/api/journals/abc123").status_code == 403
        wrong = client.delete(
            "/api/journals/abc123", headers={"X-OS-Admin-Key": "not-the-key"}
        )
        assert wrong.status_code == 403
        assert wrong.json()["detail"]["error"] == "admin_only"
    assert path.exists()


def test_delete_journal_missing_is_404(tmp_path):
    registry = Registry(journal_root=tmp_path)
    with TestClient(create_app(registry=registry)) as client:
        response = client.delete(
            "/api/journals/nope", headers={"X-OS-Admin-Key": registry.admin_key}
        )
    assert response.status_code == 404
    assert "nope" in str(response.json()["detail"])


def test_delete_journal_of_a_live_session_is_409(tmp_path):
    path = _journal_with_records(tmp_path, session_id="s1")
    registry = Registry(journal_root=tmp_path)
    registry.sessions["s1"] = object()
    with TestClient(create_app(registry=registry)) as client:
        response = client.delete(
            "/api/journals/s1", headers={"X-OS-Admin-Key": registry.admin_key}
        )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "session_live"
    assert path.exists()


def test_delete_journal_refuses_a_glob_instead_of_an_id(tmp_path):
    """`*` once matched every journal and unlinked whichever came first."""
    kept = _journal_with_records(tmp_path)
    registry = Registry(journal_root=tmp_path)
    with TestClient(create_app(registry=registry)) as client:
        response = client.delete(
            "/api/journals/*", headers={"X-OS-Admin-Key": registry.admin_key}
        )
    assert response.status_code == 404
    assert kept.exists()


# --- ownership and authorization ---------------------------------------


def test_exec_without_the_write_key_is_403(client):
    response = client.post(
        "/api/sessions/s1/exec", json={"code": "1"}, headers={"X-OS-Session-Key": ""}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "not_owner"
    assert client.registry.session.calls == [], "a refused call must not reach the session"


def test_exec_with_the_wrong_key_is_403(client):
    response = client.post(
        "/api/sessions/s1/exec", json={"code": "1"}, headers={"X-OS-Session-Key": "nope"}
    )
    assert response.status_code == 403


def test_admin_key_does_not_grant_the_right_to_type(client):
    response = client.post(
        "/api/sessions/s1/exec",
        json={"code": "1"},
        headers={"X-OS-Session-Key": "", "X-OS-Admin-Key": ADMIN_KEY},
    )
    assert response.status_code == 403, "watching is not editing"


def test_commit_without_the_right_is_423(client):
    client.registry.session.raises = CommitNotAllowed("not granted")
    response = client.post("/api/sessions/s1/commit")
    assert response.status_code == 423
    assert response.json()["detail"]["error"] == "commit_not_allowed"


def test_admin_may_interrupt_and_close_a_session_it_does_not_own(client):
    headers = {"X-OS-Session-Key": "", "X-OS-Admin-Key": ADMIN_KEY}
    assert client.post("/api/sessions/s1/interrupt", headers=headers).status_code == 200
    assert client.delete("/api/sessions/s1", headers=headers).status_code == 200


def test_a_stranger_cannot_close_a_session(client):
    response = client.delete("/api/sessions/s1", headers={"X-OS-Session-Key": ""})
    assert response.status_code == 403


def test_transfer_rotates_the_key_and_reports_pending_work(client):
    client.registry.session.pending_commands = 3
    response = client.post(
        "/api/sessions/s1/owner",
        json={"owner": {"kind": "agent", "label": "claude"}},
        headers={"X-OS-Admin-Key": ADMIN_KEY},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["owner"] == {"kind": "agent", "label": "claude"}
    assert body["write_key"] == "rotated-key"
    assert body["pending_commands"] == 3
    assert body["allow_commit"] is False, "a granted right must not travel with the session"


def test_taking_a_session_back_restores_the_human_commit_right(client):
    session = client.registry.session
    session.owner = {"kind": "agent", "label": "claude"}
    session.allow_commit = False
    session.write_key = "agents-key"
    session.former_keys = {OWNER_KEY}

    response = client.post(
        "/api/sessions/s1/owner",
        json={"owner": {"kind": "human", "label": "browser"}},
        headers={"X-OS-Session-Key": OWNER_KEY},
    )
    assert response.status_code == 200
    assert response.json()["owner"]["kind"] == "human"
    assert response.json()["allow_commit"] is True


def test_handing_over_your_own_session_needs_only_its_key(client):
    """You hold the key; all you are doing is giving it up."""
    response = client.post(
        "/api/sessions/s1/owner",
        json={"owner": {"kind": "agent", "label": "claude"}},
        headers={"X-OS-Session-Key": OWNER_KEY},
    )
    assert response.status_code == 200
    assert response.json()["owner"]["label"] == "claude"


def test_a_former_owner_can_take_a_session_back(client):
    """Handing work over is not giving up the session — no admin key needed."""
    session = client.registry.session
    session.write_key = "agents-key"
    session.former_keys = {OWNER_KEY}

    response = client.post(
        "/api/sessions/s1/owner",
        json={"owner": {"kind": "human", "label": "browser"}},
        headers={"X-OS-Session-Key": OWNER_KEY},
    )
    assert response.status_code == 200
    assert response.json()["owner"]["kind"] == "human"


def test_taking_a_session_from_someone_else_needs_the_admin_key(client):
    response = client.post(
        "/api/sessions/s1/owner",
        json={"owner": {"kind": "human", "label": "browser"}},
        headers={"X-OS-Session-Key": "not-the-key"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "admin_only"

    granted = client.post(
        "/api/sessions/s1/owner",
        json={"owner": {"kind": "human", "label": "browser"}},
        headers={"X-OS-Session-Key": "", "X-OS-Admin-Key": ADMIN_KEY},
    )
    assert granted.status_code == 200


def test_the_owner_grants_commit_on_their_own_session(client):
    """Whether your session may write is your call, not an admin ceremony."""
    response = client.post(
        "/api/sessions/s1/policy",
        json={"allow_commit": True},
        headers={"X-OS-Session-Key": OWNER_KEY},
    )
    assert response.status_code == 200
    assert response.json()["allow_commit"] is True


def test_a_former_owner_grants_commit_after_handing_over(client):
    """The commonest case: you gave the session to an agent, then allow it to write."""
    session = client.registry.session
    session.write_key = "agents-key"
    session.former_keys = {OWNER_KEY}

    response = client.post(
        "/api/sessions/s1/policy",
        json={"allow_commit": True},
        headers={"X-OS-Session-Key": OWNER_KEY},
    )
    assert response.status_code == 200


def test_revoking_commit_from_a_human_owner_is_refused(client):
    """The gate only applies to agents; recording a revocation would be a lie."""
    response = client.post(
        "/api/sessions/s1/policy",
        json={"allow_commit": False},
        headers={"X-OS-Admin-Key": ADMIN_KEY},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "policy_not_applicable"
    assert not client.registry.session.calls


def test_revoking_commit_from_an_agent_owner_still_works(client):
    client.registry.session.owner = {"kind": "agent", "label": "mcp-agent"}
    response = client.post(
        "/api/sessions/s1/policy",
        json={"allow_commit": False},
        headers={"X-OS-Admin-Key": ADMIN_KEY},
    )
    assert response.status_code == 200
    assert ("set_allow_commit", False) in client.registry.session.calls


def test_policy_grant_needs_the_admin_key(client):
    response = client.post(
        "/api/sessions/s1/policy",
        json={"allow_commit": True},
        headers={"X-OS-Session-Key": "a-key-from-nowhere"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "admin_only"

    response = client.post(
        "/api/sessions/s1/policy",
        json={"allow_commit": True},
        headers={"X-OS-Admin-Key": ADMIN_KEY},
    )
    assert response.status_code == 200
    assert response.json()["allow_commit"] is True
    assert ("set_allow_commit", True) in client.registry.session.calls


def test_unknown_session_answers_with_recovery_instructions(client):
    client.registry.past_target = {
        "container": "integra19", "database": "integra_db_19", "odoo_bin": "/opt/odoo/odoo-bin",
    }
    detail = client.post("/api/sessions/gone/exec", json={"code": "1"}).json()["detail"]
    assert detail["error"] == "session_gone"
    assert detail["target"]["container"] == "integra19"
    assert detail["journal"] == "/api/journals/gone"
    assert "variables are lost" in detail["recovery"]


def test_sessions_never_leak_write_keys(client):
    listing = client.get("/api/sessions").json()
    assert "write_key" not in listing[0]
    assert listing[0]["owner"]["kind"] == "human"
    assert client.get("/api/sessions/s1").json().get("write_key") is None


def test_registry_socket_announces_sessions_coming_and_going(client):
    """A session opened by an agent must reach a watching browser without a reload."""
    with client.websocket_connect("/ws/sessions") as socket:
        queue = client.registry.watchers[0]
        queue.put_nowait({"kind": "session_opened", "session": {"id": "s2"}})
        assert socket.receive_json()["kind"] == "session_opened"


def test_registry_watchers_are_dropped_when_the_socket_closes(client):
    with client.websocket_connect("/ws/sessions"):
        assert len(client.registry.watchers) == 1
    assert client.registry.watchers == []


def test_a_dead_session_can_be_closed_by_anyone(client):
    """A stranded tab is worse than a strict rule: the process is already gone."""
    client.registry.session.state = SessionState.DEAD
    response = client.delete("/api/sessions/s1", headers={"X-OS-Session-Key": ""})
    assert response.status_code == 200
    assert "s1" not in client.registry.sessions


def test_a_live_session_still_needs_rights_to_close(client):
    client.registry.session.state = SessionState.READY
    response = client.delete("/api/sessions/s1", headers={"X-OS-Session-Key": ""})
    assert response.status_code == 403
    assert "s1" in client.registry.sessions


def test_the_previous_owner_can_close_a_session_it_handed_over(client):
    """After a handover the browser has no write key — it must still be able to stop."""
    session = client.registry.session
    session.write_key = "rotated-key"
    session.former_keys = {OWNER_KEY}
    session.held_by = lambda key: bool(key) and (
        key == session.write_key or key in session.former_keys
    )

    response = client.delete("/api/sessions/s1", headers={"X-OS-Session-Key": OWNER_KEY})
    assert response.status_code == 200


def test_a_stranger_still_cannot_close_a_live_session(client):
    session = client.registry.session
    session.former_keys = set()
    session.held_by = lambda key: key == session.write_key
    response = client.delete("/api/sessions/s1", headers={"X-OS-Session-Key": "nope"})
    assert response.status_code == 403


def test_list_tests_returns_the_catalogue(client, monkeypatch):
    async def fake_list(container, module, runner=None):
        assert container == "qbo19"
        assert module == "widget"

        return {
            "ok": True,
            "module": "widget",
            "path": "/addons/widget",
            "classes": [{"name": "TestAlpha", "spec": "widget.TestAlpha", "methods": []}],
            "error": None,
            "error_code": None,
        }

    monkeypatch.setattr("odoo_sheller.discovery.list_tests", fake_list)
    response = client.get("/api/containers/qbo19/tests", params={"module": "widget"})
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "module": "widget",
        "path": "/addons/widget",
        "classes": [{"name": "TestAlpha", "spec": "widget.TestAlpha", "methods": []}],
    }
    assert "ok" not in body


def test_list_tests_rejects_a_dotted_spec(client):
    response = client.get("/api/containers/qbo19/tests", params={"module": "sale.TestSale"})
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_module_name"


def test_list_tests_missing_module_query_is_invalid(client):
    response = client.get("/api/containers/qbo19/tests")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_module_name"


def test_list_tests_unknown_module_is_404(client, monkeypatch):
    async def fake_list(container, module, runner=None):

        return {
            "ok": False,
            "module": module,
            "path": None,
            "classes": [],
            "error": "module widget not on addons path",
            "error_code": "module_not_found",
        }

    monkeypatch.setattr("odoo_sheller.discovery.list_tests", fake_list)
    response = client.get("/api/containers/qbo19/tests", params={"module": "widget"})
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error"] == "module_not_found"
    assert "disk" in detail["recovery"]


def test_list_tests_docker_failure_is_502(client, monkeypatch):
    async def fake_list(container, module, runner=None):

        return {
            "ok": False,
            "module": module,
            "path": None,
            "classes": [],
            "error": "OCI runtime exec failed",
            "error_code": None,
        }

    monkeypatch.setattr("odoo_sheller.discovery.list_tests", fake_list)
    response = client.get("/api/containers/qbo19/tests", params={"module": "widget"})
    assert response.status_code == 502


def test_open_forwards_autoclose(client):
    """Only a caller that says so gets a self-closing session."""
    response = client.post(
        "/api/sessions",
        json={"container": "c", "database": "db", "odoo_bin": "/odoo-bin", "autoclose": True},
    )
    assert response.status_code == 200
    assert client.registry.open_kwargs["autoclose"] is True


def test_open_does_not_autoclose_by_default(client):
    client.post(
        "/api/sessions", json={"container": "c", "database": "db", "odoo_bin": "/odoo-bin"}
    )
    assert client.registry.open_kwargs["autoclose"] is False


# --- odoo.sh targets ----------------------------------------------------


def test_open_forwards_an_odoosh_target(client):
    response = client.post(
        "/api/sessions",
        json={"kind": "odoosh", "build": "36887345", "host": "build.dev.odoo.com"},
    )
    assert response.status_code == 200
    assert client.registry.open_kwargs["kind"] == "odoosh"
    assert client.registry.open_kwargs["build"] == "36887345"
    assert client.registry.open_kwargs["host"] == "build.dev.odoo.com"


def test_open_is_a_local_docker_target_unless_told_otherwise(client):
    client.post("/api/sessions", json={"container": "c", "database": "db", "odoo_bin": "/b"})
    assert client.registry.open_kwargs["kind"] == "docker"


def test_probing_a_build_answers_what_the_instance_said(client, monkeypatch):
    async def fake_probe(build, host, runner=None):

        return {"ok": True, "supported": True, "stage": "staging",
                "db_name": "ventor-dev-36887345", "odoo_version": "19.0", "error": None}

    monkeypatch.setattr("odoo_sheller.api.discovery.probe_odoosh", fake_probe)
    body = client.post(
        "/api/probe/odoosh", json={"build": "36887345", "host": "build.dev.odoo.com"}
    ).json()
    assert body["stage"] == "staging"
    assert body["db_name"] == "ventor-dev-36887345"


def test_opening_an_unusable_build_is_422_not_500(client):
    async def refuse(**kwargs):
        raise ValueError("Odoo 17.0 found; only 19 is supported")

    client.registry.open = refuse
    response = client.post("/api/sessions", json={"kind": "odoosh", "build": "1", "host": "h"})
    assert response.status_code == 422
    assert "17.0" in str(response.json()["detail"])


def test_a_production_commit_is_refused_as_terminal_not_as_pending(client):
    """`commit_not_allowed` means "ask the human". This one means "never", and
    an agent told to poll for a grant that will never come would poll forever."""
    from odoo_sheller.session import CommitForbidden

    client.registry.session.raises = CommitForbidden(
        "this session runs on production (99 at build-99.dev.odoo.com); "
        "commit is refused there, rollback is not"
    )
    response = client.post("/api/sessions/s1/commit")
    assert response.status_code == 423
    detail = response.json()["detail"]
    assert detail["error"] == "commit_forbidden"
    assert "production" in detail["message"]
    assert "build-99.dev.odoo.com" in detail["message"], "name the instance"
    assert "rollback" in detail["recovery"]


def test_granting_commit_on_production_is_refused_too(client):
    """A guard that can be granted around is not a guard."""
    from odoo_sheller.session import CommitForbidden

    def refuse(allowed):
        raise CommitForbidden("this session runs on production (99 at h)")

    client.registry.session.set_allow_commit = refuse
    response = client.post(
        "/api/sessions/s1/policy",
        json={"allow_commit": True},
        headers={"X-OS-Admin-Key": ADMIN_KEY},
    )
    assert response.status_code == 423
    assert response.json()["detail"]["error"] == "commit_forbidden"


def test_revoking_commit_from_a_human_on_a_remote_target_is_allowed(client):
    """The refusal exists because locally the flag does not gate a human, so
    storing a revocation would lie. On someone else's instance it does gate
    them, so revoking is a real act."""
    session = client.registry.session
    session.owner = {"kind": "human", "label": "browser"}
    session.describe = lambda: {"id": "s1", "kind": "odoosh", "stage": "staging",
                                "owner": dict(session.owner)}

    response = client.post(
        "/api/sessions/s1/policy",
        json={"allow_commit": False},
        headers={"X-OS-Admin-Key": ADMIN_KEY},
    )
    assert response.status_code == 200
    assert ("set_allow_commit", False) in session.calls


def test_revoking_commit_from_a_local_human_is_still_refused(client):
    response = client.post(
        "/api/sessions/s1/policy",
        json={"allow_commit": False},
        headers={"X-OS-Admin-Key": ADMIN_KEY},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "policy_not_applicable"
