from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from odoo_sheller.agent_commit import AgentCommitError, AgentCommitStore
from odoo_sheller.journal import Journal, journal_path
from odoo_sheller.registry import AdminKeyUnusable, Registry, load_admin_key, new_session_id
from odoo_sheller.session import SessionDead, SessionState
from odoo_sheller.targets import TargetStore
from odoo_sheller.transport import Target


@pytest.fixture(autouse=True)
def _no_docker_inspect(monkeypatch):
    """Every local open asks docker for the container's name. A test that does
    not say otherwise gets back what it gave, without running docker."""

    async def same_name(container, runner=None):

        return container

    monkeypatch.setattr("odoo_sheller.registry.container_name", same_name)


def test_load_admin_key_creates_once(tmp_path):
    path = tmp_path / "admin.key"
    first = load_admin_key(path)
    assert path.is_file()
    assert path.read_text(encoding="utf-8").strip() == first
    assert load_admin_key(path) == first


@pytest.mark.parametrize("content", ["", "\n", "  \n"])
def test_load_admin_key_refuses_an_empty_file(tmp_path, content):
    """An empty file is not a missing one, and not a key either. Read as one,
    the daemon swapped in a random key it never printed or kept, and every
    admin act was refused on every start until the file was found. Writing a
    fresh key over it would disagree with a daemon that already read it, so
    the file is named and left for a human to remove."""
    path = tmp_path / "admin.key"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(AdminKeyUnusable, match=str(path)):
        load_admin_key(path)
    assert path.read_text(encoding="utf-8") == content


def test_new_session_id_is_never_all_digits():
    """An agent's host can send an all-digit id as a JSON number, and the MCP
    tools take it as a string, so the call fails validation before it runs —
    and a leading zero would be lost besides. About one hex draw in 280 is all
    digits; such a draw is thrown away."""
    draws = iter(["274327107366", "012345678901", "27432710736a"])
    assert new_session_id(lambda: next(draws)) == "27432710736a"


def test_new_session_id_is_never_a_number_in_exponent_form():
    """`27432710e366` is a JSON number as well — 2.74e373, which overflows —
    and such a draw is about as common as an all-digit one."""
    draws = iter(["27432710e366", "0e1234567890", "2743271e036a", "27432710e36a"])
    assert new_session_id(lambda: next(draws)) == "2743271e036a"


def test_new_session_id_default_shape():
    session_id = new_session_id()
    assert len(session_id) == 12
    assert int(session_id, 16) >= 0
    assert not session_id.isdigit()


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

            return {"odoo": captured.get("hello_odoo", "19.0"), "db": target_db(self.target), "pid": 1}

        async def kill(self):
            captured["killed"] = True

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

    registry = Registry(journal_root=tmp_path, targets=TargetStore(tmp_path / "t.json"))
    card = registry.targets.add_odoosh("99", "build-99.dev.odoo.com")
    await registry.open(target_id=card["id"])

    assert captured["probed"] == ("99", "build-99.dev.odoo.com")
    target = captured["target"]
    assert target.kind == "odoosh"
    assert target.name == "99"
    assert target.stage == "production", "the instance said so, nobody else"
    assert target.database == "prod-db-99"
    assert captured["argv"][0] == "ssh"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared", "reported", "stage"),
    [("production", "staging", "production"), ("staging", "production", "production"),
     ("staging", None, "staging")],
)
async def test_opening_a_build_goes_by_the_stricter_of_its_card_and_itself(
    tmp_path, monkeypatch, declared, reported, stage
):
    """The card may close a build further than the build says it is, never
    open it: a production build stays production whatever its card says."""
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)

    async def fake_probe(build, host, runner=None):

        return {
            "ok": True, "supported": True, "stage": reported,
            "db_name": "db", "odoo_version": "19.0", "error": None,
        }

    monkeypatch.setattr("odoo_sheller.registry.probe_odoosh", fake_probe)

    registry = Registry(journal_root=tmp_path, targets=TargetStore(tmp_path / "t.json"))
    card = registry.targets.add_odoosh("99", "build-99.dev.odoo.com", stage=declared)
    await registry.open(target_id=card["id"])

    assert captured["target"].stage == stage


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

    registry = Registry(journal_root=tmp_path, targets=TargetStore(tmp_path / "t.json"))
    card = registry.targets.add_odoosh("1", "h")
    with pytest.raises(ValueError, match="17.0"):
        await registry.open(target_id=card["id"])
    assert "argv" not in captured, "nothing may be spawned for a refused target"


@pytest.mark.asyncio
async def test_opening_a_card_nobody_wrote_is_a_key_error(tmp_path):
    """The API turns this into a 404; it must not turn into a probe of
    whatever the id happens to spell."""
    registry = Registry(journal_root=tmp_path, targets=TargetStore(tmp_path / "t.json"))
    with pytest.raises(KeyError):
        await registry.open(target_id="odoosh-99")


@pytest.mark.asyncio
async def test_a_card_names_the_whole_target(tmp_path):
    registry = Registry(journal_root=tmp_path, targets=TargetStore(tmp_path / "t.json"))
    card = registry.targets.add_odoosh("1", "h")
    for extra in ({"container": "c"}, {"database": "d"}, {"odoo_bin": "/b"}):
        with pytest.raises(ValueError, match="target_id"):
            await registry.open(target_id=card["id"], **extra)


# --- odoo_bin is found, not asked for ------------------------------------


def _local_probe(monkeypatch, captured, **payload):
    async def fake_probe(container, runner=None):
        captured.setdefault("probed", []).append(container)

        return {
            "ok": True, "supported": True, "odoo_bin": "/opt/odoo/odoo-bin",
            "odoo_version": "19.0", "odoo_major": 19, "error": None, **payload,
        }

    monkeypatch.setattr("odoo_sheller.registry.probe", fake_probe)


@pytest.mark.asyncio
async def test_a_local_open_without_odoo_bin_takes_it_from_the_probe(tmp_path, monkeypatch):
    """odoo_bin is a fact about the container, not a choice. An agent that left
    it out got `http_422` and had to probe every container to learn one path."""
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _local_probe(monkeypatch, captured)

    registry = Registry(journal_root=tmp_path)
    await registry.open(container="odoo19", database="db")

    assert captured["probed"] == ["odoo19"]
    assert captured["target"].odoo_bin == "/opt/odoo/odoo-bin"
    assert captured["target"].database == "db"


@pytest.mark.asyncio
async def test_a_given_odoo_bin_is_not_probed_for(tmp_path, monkeypatch):
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _local_probe(monkeypatch, captured)

    registry = Registry(journal_root=tmp_path)
    await registry.open(container="odoo19", database="db", odoo_bin="/srv/odoo-bin")

    assert "probed" not in captured
    assert captured["target"].odoo_bin == "/srv/odoo-bin"


@pytest.mark.asyncio
async def test_a_probe_that_refuses_the_container_opens_nothing(tmp_path, monkeypatch):
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _local_probe(
        monkeypatch, captured, supported=False, odoo_version="14.0", odoo_major=14,
        error="Odoo 14.0 found; supported: 15, 16, 17, 18, 19, 20",
    )

    registry = Registry(journal_root=tmp_path)
    with pytest.raises(ValueError, match="14.0"):
        await registry.open(container="odoo14", database="db")
    assert "argv" not in captured, "nothing may be spawned for a refused target"


@pytest.mark.asyncio
async def test_a_container_with_no_odoo_bin_says_so(tmp_path, monkeypatch):
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _local_probe(
        monkeypatch, captured, ok=False, supported=False, odoo_bin=None,
        error="no odoo-bin in this container",
    )

    registry = Registry(journal_root=tmp_path)
    with pytest.raises(ValueError, match="no odoo-bin in this container"):
        await registry.open(container="postgres", database="db")
    assert "argv" not in captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("given", "missing"),
    [({"database": "db"}, "container"), ({"container": "odoo19"}, "database")],
)
async def test_a_missing_choice_is_named(tmp_path, monkeypatch, given, missing):
    """container and database are the caller's to choose; say which one is
    missing rather than list every field as if all were."""
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _local_probe(monkeypatch, captured)

    registry = Registry(journal_root=tmp_path)
    with pytest.raises(ValueError) as raised:
        await registry.open(**given)
    assert str(raised.value).startswith(f"{missing} is required")
    assert "odoo_bin" not in str(raised.value)
    assert "probed" not in captured, "no probe before the request is known to be whole"


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
        await registry.open(replace="gone", target_id="odoosh-1")


async def test_replacing_a_remote_session_says_it_is_not_yours_to_open(tmp_path, monkeypatch):
    """An agent read the generic validation error as a client bug and went
    hunting for a way around it. The refusal has to name the actual rule."""
    registry = Registry(journal_root=tmp_path)
    journal = Journal(
        journal_path(tmp_path, "old1", "36887345", "build_db", datetime.now(UTC))
    )
    journal.write(
        "session_open", container="36887345", database="build_db",
        odoo_bin=None, target_kind="odoosh", host="build.dev.odoo.com", odoo="19.0",
    )
    with pytest.raises(ValueError) as excinfo:
        await registry.open(replace="old1", owner={"kind": "agent", "label": "claude"})
    message = str(excinfo.value)
    assert "remote" in message
    assert "build.dev.odoo.com" in message, "and which instance it was"
    assert "handover" in message, "and it has to say what to do instead"


def test_a_new_admin_key_is_never_readable_by_anyone_else(tmp_path):
    """Written first and narrowed after, the key sat in a world-readable file
    for as long as the gap between the two calls."""
    path = tmp_path / "admin.key"
    load_admin_key(path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_new_admin_key_never_appears_without_its_key(tmp_path, monkeypatch):
    """Created and then written, a crash between the two left an empty file
    that read as no key at all. It is linked into place written, and the file
    it was written to does not outlive the call."""
    import os

    path = tmp_path / "admin.key"
    seen = []
    real_link = os.link

    def link(source, target):
        seen.append(Path(source).read_text(encoding="utf-8").strip())
        real_link(source, target)

    monkeypatch.setattr("odoo_sheller.registry.os.link", link)
    key = load_admin_key(path)
    assert seen == [key]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["admin.key"]


def test_a_key_another_daemon_linked_first_is_the_one(tmp_path, monkeypatch):
    import os

    path = tmp_path / "admin.key"

    def lose_the_race(source, target):
        Path(target).write_text("theirs\n", encoding="utf-8")
        raise FileExistsError(target)

    monkeypatch.setattr("odoo_sheller.registry.os.link", lose_the_race)
    assert load_admin_key(path) == "theirs"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["admin.key"]


async def test_closing_a_session_hangs_up_its_sockets(tmp_path):
    """They used to wait on a session that would never speak again, and the
    entry stayed in `subscribers` for the life of the daemon."""
    import asyncio

    registry = Registry(journal_root=tmp_path)
    session = MagicMock()
    session.close = AsyncMock()
    registry.sessions["s1"] = session
    queue = asyncio.Queue(maxsize=10)
    registry.subscribe("s1", queue)
    queue.put_nowait({"kind": "stderr", "line": "unread"})

    await registry.close("s1")

    assert "s1" not in registry.subscribers
    assert queue.get_nowait() is None, "the hang-up, with nothing stale before it"


async def test_a_socket_that_falls_behind_is_cut_off_not_buffered(tmp_path):
    """A tab that stopped reading would have held every log line of every run
    in memory; raising instead would have stopped the session's log reader,
    because publishing runs inside it."""
    import asyncio

    registry = Registry(journal_root=tmp_path)
    registry.sessions["s1"] = MagicMock()
    slow = asyncio.Queue(maxsize=2)
    fast = asyncio.Queue(maxsize=100)
    registry.subscribe("s1", slow)
    registry.subscribe("s1", fast)
    for n in range(5):
        registry._publish("s1", {"kind": "stderr", "line": str(n)})

    assert registry.subscribers["s1"] == [fast]
    assert slow.get_nowait() is None
    assert fast.qsize() == 5


async def test_a_watcher_that_falls_behind_is_cut_off_too(tmp_path):
    import asyncio

    registry = Registry(journal_root=tmp_path)
    slow = asyncio.Queue(maxsize=1)
    registry.watch(slow)
    registry._broadcast({"kind": "session_closed", "session": "a"})
    registry._broadcast({"kind": "session_closed", "session": "b"})
    assert slow not in registry.watchers
    assert slow.get_nowait() is None


async def test_a_launcher_that_is_not_installed_is_a_refusal(tmp_path, monkeypatch):
    async def missing(argv):
        raise FileNotFoundError(2, "No such file or directory", "docker")

    monkeypatch.setattr("odoo_sheller.registry.spawn", missing)
    monkeypatch.setattr("odoo_sheller.registry.bootstrap_source", lambda: "bootstrap")
    registry = Registry(journal_root=tmp_path)
    with pytest.raises(ValueError, match="docker is not installed"):
        await registry.open("integra19", "acme", "/odoo-bin")


# --- a server reached by ssh --------------------------------------------------

SSH_ACCESS = "ssh -i /k/key.pem ubuntu@srv.example.com sudo -n -u odoo -H"
SSH_LAUNCH = "/opt/odoo/env/bin/python /opt/odoo/odoo-bin shell -c /opt/odoo/odoo.conf"


def _ssh_registry(tmp_path, **card):
    store = TargetStore(tmp_path / "t.json", is_file=lambda path: True)
    registry = Registry(journal_root=tmp_path, targets=store)
    fields = {"access": SSH_ACCESS, "launch": SSH_LAUNCH, "stage": "production"}
    fields.update(card)
    saved = store.add_ssh("acme", **fields)

    return registry, saved


def _ssh_probe(monkeypatch, captured, **payload):
    async def fake_probe(access, launch, runner=None):
        captured["probed"] = (access, launch)

        return {
            "ok": True, "supported": True, "version_known": True, "odoo_version": "19.0",
            "db_name": "from-the-conf", "error": None, **payload,
        }

    monkeypatch.setattr("odoo_sheller.registry.probe_ssh", fake_probe)


@pytest.mark.asyncio
async def test_opening_an_ssh_card_opens_what_it_says(tmp_path, monkeypatch):
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _ssh_probe(monkeypatch, captured)
    registry, card = _ssh_registry(tmp_path, stage="staging", launch=SSH_LAUNCH + " -d acme")
    await registry.open(target_id=card["id"])

    target = captured["target"]
    assert target.kind == "ssh"
    assert target.name == "acme"
    assert target.host == "srv.example.com"
    assert target.is_remote
    assert target.access.destination == "ubuntu@srv.example.com"
    assert target.launch.argv[-2:] == ("-d", "acme")
    assert captured["argv"][0] == "ssh"
    assert captured["probed"][0] is target.access


@pytest.mark.asyncio
async def test_the_stage_of_a_plain_server_is_the_one_a_human_declared(tmp_path, monkeypatch):
    """Not from a probe, and not from whoever asked to open it. This is what the
    commit guard turns on, and nothing on a plain server says what it is."""
    for declared in ("production", "staging"):
        captured = {}
        _stub_spawn_and_session(monkeypatch, captured)
        _ssh_probe(monkeypatch, captured, stage="staging")
        registry, card = _ssh_registry(tmp_path, stage=declared)
        await registry.open(target_id=card["id"])
        assert captured["target"].stage == declared
        registry.targets.delete(card["id"])


@pytest.mark.asyncio
async def test_a_database_the_server_names_is_shown_until_hello_says_otherwise(tmp_path, monkeypatch):
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _ssh_probe(monkeypatch, captured, db_name="from-the-conf")
    registry, card = _ssh_registry(tmp_path)
    await registry.open(target_id=card["id"])
    assert captured["target"].database == "from-the-conf"


@pytest.mark.asyncio
async def test_a_refused_probe_spawns_nothing(tmp_path, monkeypatch):
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _ssh_probe(
        monkeypatch, captured, ok=False, error="the config /x is not readable by ubuntu"
    )
    registry, card = _ssh_registry(tmp_path)
    with pytest.raises(ValueError, match="not readable by ubuntu"):
        await registry.open(target_id=card["id"])
    assert "argv" not in captured


@pytest.mark.asyncio
async def test_an_unsupported_version_is_refused_before_anything_starts(tmp_path, monkeypatch):
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _ssh_probe(monkeypatch, captured, supported=False, error="Odoo 12.0 found; supported: 15")
    registry, card = _ssh_registry(tmp_path)
    with pytest.raises(ValueError, match="12.0"):
        await registry.open(target_id=card["id"])
    assert "argv" not in captured


@pytest.mark.asyncio
async def test_a_version_the_probe_could_not_read_is_gated_when_the_session_says_it(
    tmp_path, monkeypatch
):
    """Not every install has `odoo/release.py` beside `odoo-bin`. The probe lets
    it through; the hello frame is where the version is finally a fact."""
    captured = {"hello_odoo": "12.0"}
    _stub_spawn_and_session(monkeypatch, captured)
    _ssh_probe(monkeypatch, captured, version_known=False, odoo_version=None)
    registry, card = _ssh_registry(tmp_path)
    with pytest.raises(ValueError, match="12.0"):
        await registry.open(target_id=card["id"])
    assert captured["killed"] is True
    assert registry.sessions == {}


@pytest.mark.asyncio
async def test_a_card_that_no_longer_parses_says_so_and_spawns_nothing(tmp_path, monkeypatch):
    """A key file that has gone since the card was saved."""
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _ssh_probe(monkeypatch, captured)
    registry, card = _ssh_registry(tmp_path)
    registry.targets._is_file = lambda path: False
    with pytest.raises(ValueError, match="key.pem"):
        await registry.open(target_id=card["id"])
    assert "argv" not in captured and "probed" not in captured


@pytest.mark.asyncio
async def test_a_server_whose_shell_could_not_bind_its_port_is_told_what_to_add(tmp_path, monkeypatch):
    """The process died with Odoo's own words in its stderr. The words are right and
    not enough: this is a config with workers above 0, and the fix is a flag."""
    from odoo_sheller.session import SessionDead

    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _ssh_probe(monkeypatch, captured)

    class DiesBinding:
        def __init__(self, session_id, target, process, journal, **kwargs):
            self.id = session_id
            self.target = target

        async def start(self, timeout=90.0):
            raise SessionDead(
                "session did not start: OSError: [Errno 98] Address already in use"
            )

        async def kill(self):
            pass

        def describe(self):

            return {"id": self.id}

    monkeypatch.setattr("odoo_sheller.registry.Session", DiesBinding)
    registry, card = _ssh_registry(tmp_path)
    with pytest.raises(SessionDead) as raised:
        await registry.open(target_id=card["id"])
    message = str(raised.value)
    assert "Address already in use" in message, "Odoo's own words are kept"
    assert "--no-http" in message
    assert "workers" in message


@pytest.mark.asyncio
async def test_other_deaths_are_not_given_that_advice(tmp_path, monkeypatch):
    from odoo_sheller.session import SessionDead

    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _ssh_probe(monkeypatch, captured)

    class DiesOtherwise:
        def __init__(self, session_id, target, process, journal, **kwargs):
            self.id = session_id
            self.target = target

        async def start(self, timeout=90.0):
            raise SessionDead("session did not start: database does not exist")

        async def kill(self):
            pass

        def describe(self):

            return {"id": self.id}

    monkeypatch.setattr("odoo_sheller.registry.Session", DiesOtherwise)
    registry, card = _ssh_registry(tmp_path)
    with pytest.raises(SessionDead) as raised:
        await registry.open(target_id=card["id"])
    assert "--no-http" not in str(raised.value)


@pytest.mark.asyncio
async def test_an_ssh_card_with_no_database_anywhere_still_opens(tmp_path, monkeypatch):
    """The card leaves Database empty, Launch has no `-d`, and the server's own
    config names no `db_name` either — several databases behind a dbfilter. The
    spec allows exactly this, and the session is then opened on what the shell
    picks; the daemon must not need a name just to make a journal file."""
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _ssh_probe(monkeypatch, captured, db_name=None)
    registry, card = _ssh_registry(tmp_path)
    session = await registry.open(target_id=card["id"])

    assert captured["target"].database is None
    assert session.id in registry.sessions


@pytest.mark.asyncio
async def test_a_remote_session_remembers_the_card_it_was_opened_from(tmp_path, monkeypatch):
    """By id, not by name: a card can be renamed under a live session, and its
    name can equal a container's. Both kinds of card."""
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _ssh_probe(monkeypatch, captured)
    registry, card = _ssh_registry(tmp_path)
    await registry.open(target_id=card["id"])
    assert captured["target"].card_id == card["id"]

    async def fake_probe(build, host, runner=None):

        return {
            "ok": True, "supported": True, "stage": "staging",
            "db_name": "db-7", "odoo_version": "19.0", "error": None,
        }

    monkeypatch.setattr("odoo_sheller.registry.probe_odoosh", fake_probe)
    build = registry.targets.add_odoosh("7", "build-7.dev.odoo.com")
    await registry.open(target_id=build["id"])
    assert captured["target"].card_id == build["id"] == "odoosh-7"


# --- the container switch -----------------------------------------------------


def _switch_registry(tmp_path, *on):
    store = AgentCommitStore(tmp_path / "agent-commit.json")
    for name in on:
        store.turn_on(name)

    return Registry(journal_root=tmp_path, agent_commit=store), store


def _resolve(monkeypatch, names):
    async def fake_name(container, runner=None):

        return names.get(container, container)

    monkeypatch.setattr("odoo_sheller.registry.container_name", fake_name)


@pytest.mark.asyncio
async def test_a_docker_open_is_given_the_switch_for_its_resolved_name(tmp_path, monkeypatch):
    """An agent may name a container by id; the switch is keyed by name."""
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _resolve(monkeypatch, {"3f2a": "mig18"})
    registry, _store = _switch_registry(tmp_path, "mig18")

    session = await registry.open(container="3f2a", database="db", odoo_bin="/odoo-bin")

    assert session.kwargs["container_switch"]() is True
    assert session.container_name == "mig18"


@pytest.mark.asyncio
async def test_a_session_opened_by_id_is_known_by_name_before_any_switch(tmp_path, monkeypatch):
    """A switch turned on later is matched against this name; an id kept here
    would leave the session out of the sweep for good."""
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _resolve(monkeypatch, {"3f2a": "mig18"})
    registry, store = _switch_registry(tmp_path)

    session = await registry.open(container="3f2a", database="db", odoo_bin="/odoo-bin")

    assert session.container_name == "mig18"
    store.turn_on("mig18")
    assert session.kwargs["container_switch"]() is True


@pytest.mark.asyncio
async def test_a_remote_open_is_given_no_switch(tmp_path, monkeypatch):
    """A server card's name is free text and can equal a container's."""
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _ssh_probe(monkeypatch, captured)
    store = TargetStore(tmp_path / "t.json", is_file=lambda path: True)
    switches = AgentCommitStore(tmp_path / "agent-commit.json")
    switches.turn_on("mig18")
    registry = Registry(journal_root=tmp_path, targets=store, agent_commit=switches)
    card = store.add_ssh("mig18", access=SSH_ACCESS, launch=SSH_LAUNCH, stage="staging")

    session = await registry.open(target_id=card["id"])

    assert "container_switch" not in session.kwargs
    assert getattr(session, "container_name", None) is None


class _Swept:
    def __init__(self, sid, target, name, state=SessionState.READY):
        self.id = sid
        self.target = target
        self.container_name = name
        self.state = state
        self.applied = []

    def apply_container_switch(self, on):
        self.applied.append(on)

        return True


def test_set_agent_commit_sweeps_only_docker_sessions_on_that_container(tmp_path):
    registry, store = _switch_registry(tmp_path)
    docker = Target(container="mig18", database="db", odoo_bin="/odoo-bin")
    sessions = {
        "a": _Swept("a", docker, "mig18"),
        "b": _Swept("b", docker, "mig18", SessionState.STARTING),
        "c": _Swept("c", Target(container="other", database="db"), "other"),
        "d": _Swept("d", Target(kind="ssh", label="mig18", database="db"), None),
        "e": _Swept("e", docker, "mig18", SessionState.CLOSED),
    }
    registry.sessions.update(sessions)

    assert registry.set_agent_commit("mig18", True) == ["a", "b"]
    assert store.is_on("mig18") is True
    assert sessions["c"].applied == [] and sessions["d"].applied == []
    assert sessions["e"].applied == []

    assert registry.set_agent_commit("mig18", False) == ["a", "b"]
    assert store.is_on("mig18") is False
    assert sessions["a"].applied == [True, False]


@pytest.mark.asyncio
async def test_agent_commit_allows_resolves_and_is_false_on_an_empty_store(tmp_path, monkeypatch):
    _resolve(monkeypatch, {"3f2a": "mig18"})
    registry, store = _switch_registry(tmp_path)
    assert await registry.agent_commit_allows("3f2a") is False
    store.turn_on("mig18")
    assert await registry.agent_commit_allows("3f2a") is True
    assert await registry.agent_commit_allows(None) is False


@pytest.mark.asyncio
async def test_an_unreadable_switch_file_never_stops_a_session_opening(tmp_path, monkeypatch):
    """It reads as nothing switched on: the safe way to be wrong."""
    captured = {}
    _stub_spawn_and_session(monkeypatch, captured)
    _resolve(monkeypatch, {})
    (tmp_path / "agent-commit.json").write_text("{nope")
    registry = Registry(
        journal_root=tmp_path, agent_commit=AgentCommitStore(tmp_path / "agent-commit.json")
    )

    session = await registry.open(container="mig18", database="db", odoo_bin="/odoo-bin")

    assert session.kwargs["container_switch"]() is False
    assert await registry.agent_commit_allows("mig18") is False


def test_off_reaches_the_sessions_even_when_the_file_cannot_be_written(tmp_path):
    """Off is how a human takes the right back. A broken file must not leave
    agents holding it while the error is reported."""
    path = tmp_path / "agent-commit.json"
    path.write_text("{nope")
    registry = Registry(journal_root=tmp_path, agent_commit=AgentCommitStore(path))
    swept = _Swept("a", Target(container="mig18", database="db", odoo_bin="/odoo-bin"), "mig18")
    registry.sessions["a"] = swept

    with pytest.raises(AgentCommitError):
        registry.set_agent_commit("mig18", False)

    assert swept.applied == [False]
    assert path.read_text() == "{nope"


@pytest.mark.parametrize("failure", [PermissionError, OSError])
def test_load_admin_key_is_created_where_the_filesystem_has_no_hard_links(
    tmp_path, monkeypatch, failure
):
    """FAT, SMB and some FUSE mounts refuse link(). The daemon started there
    before the key was linked into place, and must still."""
    def refuse(source, target, *args, **kwargs):
        raise failure(1, "Operation not permitted")

    monkeypatch.setattr("odoo_sheller.registry.os.link", refuse)
    path = tmp_path / "admin.key"
    key = load_admin_key(path)

    assert path.read_text(encoding="utf-8").strip() == key
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_admin_key(path) == key
    assert [p.name for p in tmp_path.iterdir()] == ["admin.key"], "no temporary left"


def test_load_admin_key_clears_the_temporaries_of_a_start_that_was_killed(tmp_path):
    """A kill between creating the temporary and removing it leaves a second
    copy of the key under another name."""
    import os
    import time

    old = tmp_path / ".admin-key-dead"
    old.write_text("secret\n", encoding="utf-8")
    long_ago = time.time() - 3600
    os.utime(old, (long_ago, long_ago))
    live = tmp_path / ".admin-key-starting"
    live.write_text("", encoding="utf-8")

    load_admin_key(tmp_path / "admin.key")

    assert not old.exists()
    assert live.exists(), "another daemon may be mid-start; its temporary is not stale"


async def test_a_target_read_already_is_not_read_again_by_a_replacing_open(tmp_path, monkeypatch):
    registry = Registry(journal_root=tmp_path)
    reads = []
    monkeypatch.setattr(
        registry, "target_of_past_session", lambda session_id: reads.append(session_id)
    )
    past = {"target_kind": "ssh", "container": "box", "odoo_bin": None}
    with pytest.raises(ValueError, match="did not run on a local container"):
        await registry.open(replace="gone", past_target=past)

    assert reads == []
