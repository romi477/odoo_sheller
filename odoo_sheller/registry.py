"""Sessions by id. Plural from day one so stage 2 needs no rewrite."""

import asyncio
import contextlib
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

from odoo_sheller.journal import (
    JOURNAL_ROOT,
    Journal,
    journal_path,
    list_journals,
    target_from_records,
)
from odoo_sheller.session import Session
from odoo_sheller.transport import Target, bootstrap_source, build_command, spawn

ADMIN_KEY_PATH = JOURNAL_ROOT.parent / "admin.key"


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
    path.write_text(key + "\n", encoding="utf-8")
    path.chmod(0o600)

    return key


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

    def journal_file_for(self, session_id: str) -> Journal | None:
        """The on-disk journal for an id, whether or not a live session holds it."""
        for entry in list_journals(self.journal_root):
            if entry["session_id"] == session_id:

                return Journal(Path(entry["path"]))

        return None

    def target_of_past_session(self, session_id: str) -> dict | None:
        """Where a session that is no longer registered used to run."""
        past = self.journal_file_for(session_id)
        if past is None:

            return None

        return target_from_records(past.records())

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
    ) -> Session:
        if replace:
            previous = self.target_of_past_session(replace)
            if previous is None:
                raise KeyError(f"no journal for session {replace}")
            container = container or previous["container"]
            database = database or previous["database"]
            odoo_bin = odoo_bin or previous["odoo_bin"]
        if not (container and database and odoo_bin):
            raise ValueError("container, database and odoo_bin are required")
        session_id = uuid.uuid4().hex[:12]
        target = Target(container=container, database=database, odoo_bin=odoo_bin)
        process = await spawn(build_command(target, bootstrap_source()))
        session = None
        announced = False
        try:
            journal = Journal(
                journal_path(
                    self.journal_root,
                    session_id,
                    container,
                    database,
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

    async def close(self, session_id: str, force: bool = False) -> None:
        session = self.sessions[session_id]
        if force:
            await session.kill()
        else:
            await session.close()
        self.sessions.pop(session_id, None)
        self._broadcast({"kind": "session_closed", "session": session_id})

    def watch(self, queue: asyncio.Queue) -> None:
        self.watchers.append(queue)

    def unwatch(self, queue: asyncio.Queue) -> None:
        if queue in self.watchers:
            self.watchers.remove(queue)

    def _broadcast(self, event: dict) -> None:
        for queue in self.watchers:
            queue.put_nowait(event)

    def subscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        if session_id not in self.sessions:
            raise KeyError(session_id)
        self.subscribers.setdefault(session_id, []).append(queue)

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        queues = self.subscribers.get(session_id, [])
        if queue in queues:
            queues.remove(queue)

    def _publish(self, session_id: str, event: dict) -> None:
        for queue in self.subscribers.get(session_id, []):
            queue.put_nowait(event)
        if event.get("kind") == "autoclose":
            # A session opened to run one test says here that the run has
            # settled and been journalled. Closing is scheduled rather than
            # awaited: this runs inside the session's own event callback.
            asyncio.create_task(self._autoclose(session_id))

            return
        if event.get("kind") in ("state", "owner", "policy"):
            self._broadcast({**event, "session": session_id})

    async def _autoclose(self, session_id: str) -> None:
        if session_id not in self.sessions:

            return  # already closed by hand, or gone with the daemon
        with contextlib.suppress(Exception):
            await self.close(session_id)
