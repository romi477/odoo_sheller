"""Sessions by id. Plural from day one so stage 2 needs no rewrite."""

import asyncio
import contextlib
import os
import re
import secrets
import tempfile
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from odoo_sheller.agent_commit import AgentCommitError, AgentCommitStore
from odoo_sheller.discovery import (
    container_name,
    probe,
    probe_odoosh,
    probe_ssh,
    version_refusal,
)
from odoo_sheller.journal import (
    JOURNAL_ROOT,
    Journal,
    find_journal,
    journal_path,
    target_from_records,
)
from odoo_sheller.session import Session, SessionDead, SessionState
from odoo_sheller.targets import TargetStore, odoosh_stage
from odoo_sheller.transport import (
    DOCKER,
    ODOOSH,
    SSH,
    Target,
    bootstrap_source,
    build_command,
    spawn,
)

ADMIN_KEY_PATH = JOURNAL_ROOT.parent / "admin.key"

# How far a socket may fall behind before it is cut off. A tab that stopped
# reading would otherwise keep every stderr line of every test run in memory
# for as long as it stayed connected. A closed socket reconnects and resyncs.
EVENT_BACKLOG = 10_000


_TEMPORARY_PREFIX = ".admin-key-"
_TEMPORARY_MAX_AGE = 60.0


class AdminKeyUnusable(Exception):
    """The admin key file exists and holds no key. It has not been touched."""


def _read_admin_key(path: Path) -> str:
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        # Read as a key, the empty string had the registry swap in a random
        # one nobody was shown, and every admin act failed on every start.
        raise AdminKeyUnusable(
            f"{path} is empty; remove it and start again, and a new key is written"
        )

    return key


def load_admin_key(path: Path = ADMIN_KEY_PATH) -> str:
    """Read the admin key, creating it on first run.

    Never served by any endpoint: the UI is behind the same unauthenticated API,
    so anything able to fetch the page would get the key with it. The human
    copies it from the daemon's own output instead.
    """
    if path.exists():

        return _read_admin_key(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _clear_stale_temporaries(path.parent)
    key = secrets.token_urlsafe(24)
    # Written whole beside it, then linked into place: the name never points at
    # a file without the key in it, which a crash between creating and writing
    # used to leave behind. mkstemp creates it 0600 — writing first and
    # narrowing it after left the key readable by anyone until the chmod.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=_TEMPORARY_PREFIX)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            # Another daemon started in the same instant; its key is the one.

            return _read_admin_key(path)
        except OSError:
            # No hard links here (FAT, SMB, some FUSE mounts). Created with its
            # final mode and exclusively, as it was before it was linked: the
            # crash window comes back with it, and a start that finds the file
            # empty stops and says so rather than run with a key nobody saw.

            return _create_exclusively(path, key)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)

    return key


def _create_exclusively(path: Path, key: str) -> str:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:

        return _read_admin_key(path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")

    return key


def _clear_stale_temporaries(directory: Path) -> None:
    """Remove what a start killed mid-way left: a copy of the key, under a name.

    Only the old ones: a younger temporary may belong to a daemon starting at
    this moment, whose link would fail if it vanished.
    """
    cutoff = time.time() - _TEMPORARY_MAX_AGE
    for leftover in directory.glob(f"{_TEMPORARY_PREFIX}*"):
        with contextlib.suppress(OSError):
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()


# A JSON number spelled in hex digits: decimal ones, or two runs of them
# around an `e` — `27432710e366` is 2.74e373.
_NUMBER_SHAPED = re.compile(r"\d+(?:e\d+)?")


def new_session_id(draw=lambda: uuid.uuid4().hex[:12]) -> str:
    """Twelve hex digits that never read as a JSON number.

    An agent's host can send `274327107366` as a JSON number; every MCP tool
    takes `session_id` as a string, so the call is refused before it runs, and
    a leading zero would be gone besides. About one draw in 280 is all digits
    and about as many are digits around one `e`; those are drawn again.
    """
    while True:
        session_id = draw()
        if not _NUMBER_SHAPED.fullmatch(session_id):

            return session_id


def _hang_up(queue: asyncio.Queue) -> None:
    """Tell a socket's reader it has heard the last event.

    `None` is the signal. Whatever it had not read yet is dropped first: the
    queue may be full, which is one of the two reasons to hang up.
    """
    while not queue.empty():
        queue.get_nowait()
    queue.put_nowait(None)


# Odoo's own words, for a shell that could not bind the port beside a running
# service. They are kept; the fix is not in them.
BIND_HINT = (
    "\n— the server's config probably has workers above 0, which makes Odoo's shell "
    "bind its HTTP port before it starts. Add --no-http to Launch (or --workers=0)"
)


def _with_advice(exc: BaseException, target: Target) -> BaseException:
    """The same death, plus what to do about it where that is known."""
    if (
        target.kind == SSH
        and isinstance(exc, SessionDead)
        and "Address already in use" in str(exc)
        and "--no-http" not in str(exc)
    ):

        return SessionDead(f"{exc}{BIND_HINT}")

    return exc


class Registry:
    def __init__(
        self,
        journal_root=JOURNAL_ROOT,
        admin_key: str | None = None,
        targets: TargetStore | None = None,
        agent_commit: AgentCommitStore | None = None,
    ):
        # The cards a human wrote down. Only ever read from here; writing them
        # is the API's, behind the admin key.
        self.targets = targets if targets is not None else TargetStore()
        # The local containers a human opened to every agent's commit. Beside
        # the journals rather than at a fixed path, so a registry pointed at a
        # scratch directory never reads the real one.
        self.agent_commit = (
            agent_commit
            if agent_commit is not None
            else AgentCommitStore(Path(journal_root).parent / "agent-commit.json")
        )
        self.sessions: dict[str, Session] = {}
        self.subscribers: dict[str, list[asyncio.Queue]] = {}
        # Watchers of the registry itself. Per-session sockets cannot announce a
        # session that did not exist when the page loaded — an agent opening one
        # has to be visible without a reload.
        self.watchers: list[asyncio.Queue] = []
        self.journal_root = journal_root
        self.admin_key = admin_key or secrets.token_urlsafe(24)
        # Tasks started from a callback. The loop holds them only weakly, and
        # a task nobody references can be collected before it has finished.
        self._tasks: set[asyncio.Task] = set()

    def journal_file_for(self, session_id: str) -> Journal | None:
        """The on-disk journal for an id, whether or not a live session holds it.

        Found by filename: summarising every journal to find one cost seconds
        per call on a machine that has kept a few thousand of them.
        """
        path = find_journal(self.journal_root, session_id)

        return Journal(path) if path is not None else None

    def target_of_past_session(self, session_id: str) -> dict | None:
        """Where a session that is no longer registered used to run.

        `session_open` is in the head of the file, so the read stops there.
        """
        past = self.journal_file_for(session_id)
        if past is None:

            return None

        return target_from_records(past.iter_records())

    async def _docker_target(
        self, container: str | None, database: str | None, odoo_bin: str | None
    ) -> Target:
        """A local container, with odoo-bin found rather than asked for.

        Container and database are the caller's choice; odoo-bin is a fact
        about the container. An agent that left it out used to get a bare
        `http_422` and probe every running container to learn one path. The
        probe here is the same one Connect runs, so it also refuses an
        unsupported version before anything is spawned. A caller that already
        knows the path skips it.
        """
        for name, value in (("container", container), ("database", database)):
            if not value:
                raise ValueError(f"{name} is required for a local container")
        if not odoo_bin:
            found = await probe(container)
            if not (found.get("ok") and found.get("supported") and found.get("odoo_bin")):
                raise ValueError(
                    found.get("error") or f"no usable Odoo in container {container}"
                )
            odoo_bin = found["odoo_bin"]

        return Target(container=container, database=database, odoo_bin=odoo_bin)

    async def _remote_target(self, target_id: str) -> Target:
        """A target a human wrote down, opened by the id of its card."""
        try:
            card = self.targets.get(target_id)
        except KeyError:
            raise KeyError(f"no target {target_id!r}") from None
        if card["kind"] == "ssh":
            target = await self._ssh_target(card)
        else:
            target = await self._odoosh_target(card["build"], card["host"], card["stage"])

        return replace(target, card_id=card["id"])

    async def _ssh_target(self, card: dict) -> Target:
        """A server someone wrote a card for: parse it, probe it, then open it.

        The stage is the one the card declares — nothing on a plain server says
        what it is, so a human did, and the probe has no say. The probe is the
        card's own recipe run once to see who and where it lands; a refusal is
        raised here, before anything is spawned.
        """
        access, launch = self.targets.recipe(card["id"])
        probe = await probe_ssh(access, launch)
        if not (probe.get("ok") and probe.get("supported")):
            raise ValueError(probe.get("error") or f"{card['name']} is not usable")

        return Target(
            kind=SSH,
            label=card["name"],
            access=access,
            launch=launch,
            host=access.host,
            stage=card["stage"],
            # Informational, and the hello frame has the last word: it lets the
            # session read right from the moment it is announced.
            database=launch.database or probe.get("db_name"),
        )

    async def _odoosh_target(self, build: str, host: str, declared: str | None) -> Target:
        """Ask the build what it is before opening anything in it.

        The stage comes from here rather than from the request: a caller that
        could name its own stage would make the production guard decorative,
        and production differs from staging by the digits in a build id. The
        card's own stage only adds to it — the stricter of the two — so a
        human can close a build further, never open one. The probe doubles as
        validation — an unsupported version is refused now instead of on the
        first command.
        """
        probe = await probe_odoosh(build, host)
        if not probe.get("supported"):
            raise ValueError(probe.get("error") or f"build {build} is not usable")

        return Target(
            kind=ODOOSH,
            build=build,
            host=host,
            stage=odoosh_stage(declared, probe.get("stage")),
            # Informational: the wrapper picks the database, and the hello
            # frame confirms it. Set here so the session reads right from the
            # moment it is announced, before hello has arrived.
            database=probe.get("db_name"),
        )

    async def open(
        self,
        container: str | None = None,
        database: str | None = None,
        odoo_bin: str | None = None,
        owner: dict | None = None,
        allow_commit: bool | None = None,
        replace: str | None = None,
        client_token: str | None = None,
        autoclose: bool = False,
        target_id: str | None = None,
    ) -> Session:
        if target_id and replace:
            # A journal records the identity slot but not how to reach it
            # again over SSH. An agent cannot open these at all, and a human
            # picks the card again, so say so rather than rebuild the wrong
            # kind of target from the right-looking fields.
            raise ValueError("replace is only for local targets")
        if target_id and (container or database or odoo_bin):
            raise ValueError(
                "a target_id names the whole target; container, database and "
                "odoo_bin belong to a local one"
            )
        if replace:
            previous = self.target_of_past_session(replace)
            if previous is None:
                raise KeyError(f"no journal for session {replace}")
            if previous.get("target_kind") != DOCKER or not previous.get("odoo_bin"):
                # Saying "container, database and odoo_bin are required" here
                # reads as a serialisation complaint, and an agent that met it
                # went looking for a way around instead of asking. Name the
                # rule: a remote target is a human's to open, and the journal
                # has no way back to it anyway.
                raise ValueError(
                    f"session {replace} did not run on a local container "
                    f"({previous['container']} on "
                    f"{previous.get('host') or previous.get('target_kind')}); a "
                    "remote target is a human's to open from the UI — ask for a "
                    "handover instead of reopening it"
                )
            container = container or previous["container"]
            database = database or previous["database"]
            odoo_bin = odoo_bin or previous["odoo_bin"]
        if target_id:
            target = await self._remote_target(target_id)
        else:
            target = await self._docker_target(container, database, odoo_bin)
        switch: dict = {}
        docker_name = None
        if target.kind == DOCKER:
            docker_name = await self._docker_name(target.container)
            switch["container_switch"] = lambda: self._switched_on(docker_name)
        session_id = new_session_id()
        try:
            process = await spawn(build_command(target, bootstrap_source()))
        except FileNotFoundError as exc:
            # `docker` off a GUI's PATH, or `ssh` in an image that has none:
            # a fact about this host to say plainly, not a crash to report.
            raise ValueError(
                f"{exc.filename or 'the launcher'} is not installed where this "
                "daemon runs"
            ) from None
        session = None
        announced = False
        try:
            journal = Journal(
                journal_path(
                    self.journal_root,
                    session_id,
                    # From the target, not the request: an odoo.sh session was
                    # never asked for by container name, and its database came
                    # back from the probe.
                    target.name,
                    target.database,
                    datetime.now(UTC),
                )
            )
            session = Session(
                session_id,
                target,
                process,
                journal,
                on_event=lambda event: self._publish(session_id, event),
                owner=owner,
                allow_commit=allow_commit,
                client_token=client_token,
                autoclose=autoclose,
                **switch,
            )
            session.container_name = docker_name
            self.sessions[session_id] = session
            # Watchers need the id before hello: startup stderr is already
            # flowing, and POST /api/sessions still blocks on the registry.
            self._broadcast({"kind": "session_starting", "session": session.describe()})
            announced = True
            hello = await session.start()
            if target.kind == SSH and (refusal := version_refusal(hello.get("odoo"))):
                # The probe could not read the version; now it is a fact.
                raise ValueError(refusal)
        except BaseException as caught:
            exc = _with_advice(caught, target)
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.kill()
            else:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
            self.sessions.pop(session_id, None)
            # Only the caller of `open` sees the exception. Without this, a
            # watcher that acted on `session_starting` keeps a session that
            # never becomes ready and never goes away.
            if announced:
                self._broadcast({
                    "kind": "session_failed",
                    "session": session_id,
                    "reason": str(exc),
                })
            if exc is not caught:
                raise exc from caught
            raise

        self._broadcast({"kind": "session_opened", "session": session.describe()})

        return session

    def _any_switched_on(self) -> bool:
        """Whether any container is switched on. A file that cannot be read
        counts as none: it must not stop a session opening, and reading it as
        nothing switched on is the safe way to be wrong. The API reports it."""
        try:

            return bool(self.agent_commit.entries())
        except AgentCommitError:

            return False

    def _switched_on(self, name: str | None) -> bool:
        try:

            return self.agent_commit.is_on(name)
        except AgentCommitError:

            return False

    async def _docker_name(self, container: str | None) -> str | None:
        """The name a switch is keyed by, resolved at open whether or not any
        switch is on yet: one turned on later is matched against it, and an
        id kept instead would leave this session out of that sweep for good."""
        if not container:

            return container

        return await container_name(container)

    async def agent_commit_allows(self, container: str | None) -> bool:
        """Whether a human switched this local container on for agents."""
        if not container or not self._any_switched_on():

            return False

        return self._switched_on(await container_name(container))

    def set_agent_commit(self, container: str, on: bool) -> list[str]:
        """Switch a container on or off, and every open session on it with it.

        Matched by target kind first: only a local Docker session is ever
        swept, whatever a remote card happens to be called. A session still
        starting is swept too — it is already the container's. Returns the
        ids whose right changed.
        """
        if on:
            self.agent_commit.turn_on(container)
        changed = []
        for session in list(self.sessions.values()):
            if (
                session.target.kind == DOCKER
                and session.container_name == container
                and session.state not in (SessionState.CLOSED, SessionState.DEAD)
                and session.apply_container_switch(on)
            ):
                changed.append(session.id)
        if not on:
            # After the sweep: off is how a human takes the right back, and a
            # file that cannot be written must not leave agents holding it
            # while the error goes back to them.
            self.agent_commit.turn_off(container)

        return changed

    def get(self, session_id: str) -> Session:

        return self.sessions[session_id]

    async def close(
        self, session_id: str, force: bool = False, timeout: float = 10.0
    ) -> None:
        session = self.sessions[session_id]
        if force:
            await session.kill()
        else:
            await session.close(timeout)
        self.sessions.pop(session_id, None)
        # Its sockets would otherwise wait on a session that will never say
        # anything again, and the entry here would outlive it for good.
        for queue in self.subscribers.pop(session_id, []):
            _hang_up(queue)
        self._broadcast({"kind": "session_closed", "session": session_id})

    def watch(self, queue: asyncio.Queue) -> None:
        self.watchers.append(queue)

    def unwatch(self, queue: asyncio.Queue) -> None:
        if queue in self.watchers:
            self.watchers.remove(queue)

    def _broadcast(self, event: dict) -> None:
        for queue in list(self.watchers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self.unwatch(queue)
                _hang_up(queue)

    def subscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        if session_id not in self.sessions:
            raise KeyError(session_id)
        self.subscribers.setdefault(session_id, []).append(queue)

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        queues = self.subscribers.get(session_id, [])
        if queue in queues:
            queues.remove(queue)

    def _publish(self, session_id: str, event: dict) -> None:
        for queue in list(self.subscribers.get(session_id, [])):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Raising here would land in the session's stderr reader —
                # this runs inside it — and stop the log for everyone.
                self.unsubscribe(session_id, queue)
                _hang_up(queue)
        if event.get("kind") == "autoclose":
            # A session opened to run one test says here that the run has
            # settled and been journalled. Closing is scheduled rather than
            # awaited: this runs inside the session's own event callback.
            task = asyncio.create_task(self._autoclose(session_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

            return
        if event.get("kind") in ("state", "owner", "policy"):
            self._broadcast({**event, "session": session_id})

    async def _autoclose(self, session_id: str) -> None:
        if session_id not in self.sessions:

            return  # already closed by hand, or gone with the daemon
        with contextlib.suppress(Exception):
            await self.close(session_id)
