from unittest.mock import AsyncMock, MagicMock

import pytest

from odoo_sheller.registry import Registry
from odoo_sheller.session import SessionDead, SessionState
from odoo_sheller.transport import Target


def test_subscribe_raises_for_unknown_session():
    registry = Registry()

    with pytest.raises(KeyError):
        registry.subscribe("nope", __import__("asyncio").Queue())


@pytest.mark.asyncio
async def test_open_kills_process_when_session_construction_fails(tmp_path, monkeypatch):
    process_kill_called = []
    process_wait_called = []

    class FakeProcess:
        def kill(self):
            process_kill_called.append(True)

        async def wait(self):
            process_wait_called.append(True)

    def failing_session(*args, **kwargs):
        raise RuntimeError("session init failed")

    monkeypatch.setattr("odoo_sheller.registry.spawn", AsyncMock(return_value=FakeProcess()))
    monkeypatch.setattr("odoo_sheller.registry.Session", failing_session)
    monkeypatch.setattr("odoo_sheller.registry.bootstrap_source", lambda: "bootstrap")

    registry = Registry(journal_root=tmp_path)
    with pytest.raises(RuntimeError, match="session init failed"):
        await registry.open("c", "db", "/odoo-bin")

    assert process_kill_called == [True]
    assert process_wait_called == [True]
    assert registry.sessions == {}


@pytest.mark.asyncio
async def test_open_kills_session_when_start_fails(tmp_path, monkeypatch):
    kill_called = []

    class TrackingSession:
        def __init__(self, session_id, target, process, journal, on_event=None, **kwargs):
            self.id = session_id
            self.target = target
            self.process = process

        def describe(self):
            return {
                "id": self.id,
                "state": "starting",
                "container": self.target.container,
                "database": self.target.database,
            }

        async def start(self):
            raise SessionDead("no hello")

        async def kill(self):
            kill_called.append(True)

    monkeypatch.setattr("odoo_sheller.registry.Session", TrackingSession)
    monkeypatch.setattr("odoo_sheller.registry.spawn", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr("odoo_sheller.registry.bootstrap_source", lambda: "bootstrap")

    registry = Registry(journal_root=tmp_path)
    with pytest.raises(SessionDead, match="no hello"):
        await registry.open("c", "db", "/odoo-bin")

    assert kill_called == [True]
    assert registry.sessions == {}


@pytest.mark.asyncio
async def test_open_broadcasts_session_starting_before_hello(tmp_path, monkeypatch):
    """Watchers need the id while POST still waits for hello, or startup stderr is invisible."""
    import asyncio

    proceed = asyncio.Event()

    class TrackingSession:
        def __init__(self, session_id, target, process, journal, on_event=None, **kwargs):
            self.id = session_id
            self.target = target
            self.ready = False

        def describe(self):
            return {
                "id": self.id,
                "state": "ready" if self.ready else "starting",
                "container": self.target.container,
                "database": self.target.database,
            }

        async def start(self):
            await proceed.wait()
            self.ready = True

    monkeypatch.setattr("odoo_sheller.registry.Session", TrackingSession)
    monkeypatch.setattr("odoo_sheller.registry.spawn", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr("odoo_sheller.registry.bootstrap_source", lambda: "bootstrap")

    registry = Registry(journal_root=tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    registry.watch(queue)

    task = asyncio.create_task(registry.open("integra19", "acme", "/odoo-bin"))
    starting = await asyncio.wait_for(queue.get(), timeout=1)
    assert starting["kind"] == "session_starting"
    assert starting["session"]["state"] == "starting"
    assert starting["session"]["container"] == "integra19"
    assert starting["session"]["database"] == "acme"
    assert starting["session"]["id"] in registry.sessions
    assert not task.done()

    proceed.set()
    session = await task
    opened = await asyncio.wait_for(queue.get(), timeout=1)
    assert opened["kind"] == "session_opened"
    assert opened["session"]["id"] == session.id
    assert opened["session"]["state"] == "ready"


@pytest.mark.asyncio
async def test_open_broadcasts_session_failed_when_start_fails(tmp_path, monkeypatch):
    """session_starting without a counterpart leaves every watcher holding a phantom."""
    import asyncio

    class TrackingSession:
        def __init__(self, session_id, target, process, journal, on_event=None, **kwargs):
            self.id = session_id
            self.target = target

        def describe(self):
            return {
                "id": self.id,
                "state": "starting",
                "container": self.target.container,
                "database": self.target.database,
            }

        async def start(self):
            raise SessionDead("no hello frame within 90s")

        async def kill(self):
            pass

    monkeypatch.setattr("odoo_sheller.registry.Session", TrackingSession)
    monkeypatch.setattr("odoo_sheller.registry.spawn", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr("odoo_sheller.registry.bootstrap_source", lambda: "bootstrap")

    registry = Registry(journal_root=tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    registry.watch(queue)

    with pytest.raises(SessionDead):
        await registry.open("integra19", "acme", "/odoo-bin")

    starting = queue.get_nowait()
    assert starting["kind"] == "session_starting"
    failed = queue.get_nowait()
    assert failed["kind"] == "session_failed"
    assert failed["session"] == starting["session"]["id"]
    assert "no hello" in failed["reason"]
    assert queue.empty()
    assert registry.sessions == {}


@pytest.mark.asyncio
async def test_a_spawn_failure_announces_nothing(tmp_path, monkeypatch):
    """Nothing was announced, so there is no phantom to retract."""
    import asyncio

    async def boom(argv):
        raise RuntimeError("docker exec failed")

    monkeypatch.setattr("odoo_sheller.registry.spawn", boom)
    monkeypatch.setattr("odoo_sheller.registry.bootstrap_source", lambda: "bootstrap")

    registry = Registry(journal_root=tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    registry.watch(queue)

    with pytest.raises(RuntimeError):
        await registry.open("integra19", "acme", "/odoo-bin")

    assert queue.empty()


@pytest.mark.asyncio
async def test_open_echoes_the_client_token_back(tmp_path, monkeypatch):
    """(container, database) is not an identity: an agent may open the same target."""
    import asyncio

    class TrackingSession:
        def __init__(self, session_id, target, process, journal, on_event=None, **kwargs):
            self.id = session_id
            self.target = target
            self.client_token = kwargs.get("client_token")

        def describe(self):
            return {"id": self.id, "client_token": self.client_token}

        async def start(self):
            pass

    monkeypatch.setattr("odoo_sheller.registry.Session", TrackingSession)
    monkeypatch.setattr("odoo_sheller.registry.spawn", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr("odoo_sheller.registry.bootstrap_source", lambda: "bootstrap")

    registry = Registry(journal_root=tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    registry.watch(queue)

    await registry.open("integra19", "acme", "/odoo-bin", client_token="tab-7")

    starting = queue.get_nowait()
    assert starting["kind"] == "session_starting"
    assert starting["session"]["client_token"] == "tab-7"


def test_registry_does_not_broadcast_stderr_to_watchers(tmp_path):
    """Odoo startup is hundreds of lines; the registry socket is for sessions coming and going."""
    import asyncio

    registry = Registry(journal_root=tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    registry.watch(queue)

    class Starting:
        state = SessionState.STARTING
        target = Target(container="integra19", database="acme", odoo_bin="/odoo-bin")

    registry.sessions["abc"] = Starting()
    registry._publish(
        "abc",
        {"kind": "stderr", "line": "loading base", "container": "integra19", "database": "acme"},
    )
    assert queue.empty()


@pytest.mark.asyncio
async def test_registry_broadcasts_session_lifecycle(tmp_path, monkeypatch):
    """Watchers hear about sessions they did not open themselves."""
    import asyncio

    registry = Registry(journal_root=tmp_path)
    queue: asyncio.Queue = asyncio.Queue()
    registry.watch(queue)

    registry._broadcast({"kind": "session_opened", "session": {"id": "abc"}})
    assert (await queue.get())["kind"] == "session_opened"

    registry.unwatch(queue)
    registry._broadcast({"kind": "session_closed", "session": "abc"})
    assert queue.empty(), "a closed watcher must stop receiving"


# --- autoclose ----------------------------------------------------------


class _FinishingSession:
    """A session that announces itself finished, the way a test session does."""

    def __init__(self, session_id="s1"):
        self.id = session_id
        self.state = SessionState.READY
        self.closed = []

    async def close(self, timeout=10.0):
        self.closed.append("close")
        self.state = SessionState.CLOSED

    async def kill(self):
        self.closed.append("kill")
        self.state = SessionState.CLOSED

    def describe(self):

        return {"id": self.id}


async def _settle(registry, session_id="s1"):
    """Let the task the registry scheduled for the announcement run."""
    registry._publish(session_id, {"kind": "autoclose", "session": session_id})
    for _ in range(50):
        await __import__("asyncio").sleep(0)
        if session_id not in registry.sessions:
            break


@pytest.mark.asyncio
async def test_an_announced_session_is_closed_and_dropped(tmp_path):
    registry = Registry(journal_root=tmp_path)
    session = _FinishingSession()
    registry.sessions["s1"] = session

    await _settle(registry)

    assert session.closed == ["close"], "a graceful close, not a kill"
    assert "s1" not in registry.sessions


@pytest.mark.asyncio
async def test_announcing_a_session_that_is_already_gone_is_harmless(tmp_path):
    """The human may have closed it first; the announcement still arrives."""
    registry = Registry(journal_root=tmp_path)

    await _settle(registry)

    assert registry.sessions == {}


@pytest.mark.asyncio
async def test_watchers_hear_the_session_close(tmp_path):
    """The browser drops the tab on session_closed; it must still be sent."""
    import asyncio

    registry = Registry(journal_root=tmp_path)
    registry.sessions["s1"] = _FinishingSession()
    queue: asyncio.Queue = asyncio.Queue()
    registry.watch(queue)

    await _settle(registry)

    kinds = []
    while not queue.empty():
        kinds.append(queue.get_nowait()["kind"])
    assert "session_closed" in kinds


# --- odoo.sh targets ----------------------------------------------------


def _stub_spawn_and_session(monkeypatch, captured):
    """Capture the Target a spawn was built from, without spawning anything."""
    from odoo_sheller import transport

    real_build = transport.build_command

    async def fake_spawn(argv):
        captured["argv"] = argv

        return MagicMock()

    def spy_build(target, source):
        captured["target"] = target

        return real_build(target, source)

    class FakeSession:
        def __init__(self, session_id, target, process, journal, **kwargs):
            self.id = session_id
            self.target = target
            self.kwargs = kwargs
            self.state = SessionState.READY

        async def start(self, timeout=90.0):

            return {"odoo": "19.0", "db": target_db(self.target), "pid": 1}

        def describe(self):

            return {"id": self.id}

    monkeypatch.setattr("odoo_sheller.registry.spawn", fake_spawn)
    monkeypatch.setattr("odoo_sheller.registry.build_command", spy_build)
    monkeypatch.setattr("odoo_sheller.registry.Session", FakeSession)


def target_db(target):

    return target.database


@pytest.mark.asyncio
async def test_opening_an_odoosh_build_takes_the_stage_from_the_instance(
    tmp_path, monkeypatch
):
    """Not from the request. A client that could name its own stage would make
    the production guard decorative — production differs from staging by the
    digits in a build id."""
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)

    async def fake_probe(build, host, runner=None):
        captured["probed"] = (build, host)

        return {
            "ok": True, "supported": True, "stage": "production",
            "db_name": "prod-db-99", "odoo_version": "19.0", "error": None,
        }

    monkeypatch.setattr("odoo_sheller.registry.probe_odoosh", fake_probe)

    registry = Registry(journal_root=tmp_path)
    await registry.open(kind="odoosh", build="99", host="build-99.dev.odoo.com")

    assert captured["probed"] == ("99", "build-99.dev.odoo.com")
    target = captured["target"]
    assert target.kind == "odoosh"
    assert target.name == "99"
    assert target.stage == "production", "the instance said so, nobody else"
    assert target.database == "prod-db-99"
    assert captured["argv"][0] == "ssh"


@pytest.mark.asyncio
async def test_opening_an_odoosh_build_refuses_an_unsupported_instance(
    tmp_path, monkeypatch
):
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)

    async def fake_probe(build, host, runner=None):

        return {
            "ok": True, "supported": False, "stage": "staging", "db_name": "db",
            "odoo_version": "17.0", "error": "Odoo 17.0 found; only 19 is supported",
        }

    monkeypatch.setattr("odoo_sheller.registry.probe_odoosh", fake_probe)

    registry = Registry(journal_root=tmp_path)
    with pytest.raises(ValueError, match="17.0"):
        await registry.open(kind="odoosh", build="1", host="h")
    assert "argv" not in captured, "nothing may be spawned for a refused target"


@pytest.mark.asyncio
async def test_opening_an_odoosh_build_needs_a_build_and_a_host(tmp_path):
    registry = Registry(journal_root=tmp_path)
    with pytest.raises(ValueError, match="build"):
        await registry.open(kind="odoosh", host="h")


@pytest.mark.asyncio
async def test_replacing_a_lost_session_stays_local_only(tmp_path, monkeypatch):
    """A journal records the identity slot but not how to reach it again over
    SSH. Rather than rebuild a docker target from an odoo.sh one, say so — an
    agent cannot open these anyway, and a human retypes the build."""
    registry = Registry(journal_root=tmp_path)
    monkeypatch.setattr(
        registry, "target_of_past_session",
        lambda session_id: {"container": "36887345", "database": "db", "odoo_bin": None},
    )
    with pytest.raises(ValueError):
        await registry.open(replace="gone", kind="odoosh")
