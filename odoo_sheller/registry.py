"""Sessions by id. Plural from day one so stage 2 needs no rewrite."""

import asyncio
import contextlib
import os
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

from odoo_sheller.discovery import probe, probe_odoosh
from odoo_sheller.journal import (
    JOURNAL_ROOT,
    Journal,
    find_journal,
    journal_path,
    target_from_records,
)
from odoo_sheller.session import Session
from odoo_sheller.transport import (
    DOCKER,
    ODOOSH,
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


def load_admin_key(path: Path = ADMIN_KEY_PATH) -> str:
    """Read the admin key, creating it on first run.

    Never served by any endpoint: the UI is behind the same unauthenticated API,
    so anything able to fetch the page would get the key with it. The human
    copies it from the daemon's own output instead.
    """
    if path.exists():

        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(24)
    # Created with its final mode. Writing first and narrowing it after left
    # the key in a file anyone on the machine could read until the chmod.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another daemon started in the same instant; its key is the one.

        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")

    return key


def _hang_up(queue: asyncio.Queue) -> None:
    """Tell a socket's reader it has heard the last event.

    `None` is the signal. Whatever it had not read yet is dropped first: the
    queue may be full, which is one of the two reasons to hang up.
    """
    while not queue.empty():
        queue.get_nowait()
    queue.put_nowait(None)


class Registry:
    def __init__(self, journal_root=JOURNAL_ROOT, admin_key: str | None = None):
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

    async def _odoosh_target(self, build: str | None, host: str | None) -> Target:
        """Ask the build what it is before opening anything in it.

        The stage comes from here rather than from the request: a caller that
        could name its own stage would make the production guard decorative,
        and production differs from staging by the digits in a build id. The
        probe doubles as validation — an unsupported version is refused now
        instead of on the first command.
        """
        if not (build and host):
            raise ValueError("build and host are required for an odoo.sh target")
        probe = await probe_odoosh(build, host)
        if not probe.get("supported"):
            raise ValueError(probe.get("error") or f"build {build} is not usable")

        return Target(
            kind=ODOOSH,
            build=build,
            host=host,
            stage=probe.get("stage"),
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
        kind: str = DOCKER,
        build: str | None = None,
        host: str | None = None,
    ) -> Session:
        if replace:
            if kind != DOCKER:
                # A journal records the identity slot but not how to reach it
                # again over SSH. An agent cannot open these at all, and a
                # human retypes the build, so say so rather than rebuild the
                # wrong kind of target from the right-looking fields.
                raise ValueError("replace is only for local targets")
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
        if kind == ODOOSH:
            target = await self._odoosh_target(build, host)
        else:
            target = await self._docker_target(container, database, odoo_bin)
        session_id = uuid.uuid4().hex[:12]
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
            )
            self.sessions[session_id] = session
            # Watchers need the id before hello: startup stderr is already
            # flowing, and POST /api/sessions still blocks on the registry.
            self._broadcast({"kind": "session_starting", "session": session.describe()})
            announced = True
            await session.start()
        except BaseException as exc:
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
            raise

        self._broadcast({"kind": "session_opened", "session": session.describe()})

        return session

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
