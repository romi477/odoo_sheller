"""One live session: the state machine over a bootstrap process."""

import asyncio
import contextlib
import secrets
from collections import deque
from enum import Enum

from odoo_sheller.journal import Journal
from odoo_sheller.protocol import (
    ProtocolError,
    close_frame,
    commit_frame,
    decode_frame,
    encode_frame,
    exec_frame,
    rollback_frame,
    run_test_frame,
)
from odoo_sheller.transport import PRODUCTION, Target, send_signal


class SessionState(str, Enum):
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    CLOSED = "closed"
    DEAD = "dead"


def _journal_fields(frame: dict) -> dict:
    """Drop the wire type — the journal record already carries its own `kind`."""

    return {key: value for key, value in frame.items() if key != "t"}


class _StderrWindow:
    """Lines Odoo logged while one command ran.

    The session's own `_stderr` is a short rolling tail, so an index into it
    is meaningless once a noisy run has wrapped it. This collects the run's
    own lines as they arrive, keeping the tail if there are too many — and
    knows the difference between "exactly at the ceiling" and "over it".
    """

    def __init__(self, limit: int):
        self.lines: deque[str] = deque(maxlen=limit)
        self.total = 0

    def append(self, line: str) -> None:
        self.lines.append(line)
        self.total += 1

    @property
    def truncated(self) -> bool:

        return self.total > len(self.lines)


class SessionBusy(Exception):
    """A command is already running in this session."""


class SessionNotReady(Exception):
    """The session has not reported hello yet."""


class SessionDead(Exception):
    """The container-side process is gone."""


class CommitNotAllowed(Exception):
    """This session was opened without the right to write to the database."""


class CommitForbidden(CommitNotAllowed):
    """A write that will never be allowed here, not one awaiting a grant.

    Subclasses CommitNotAllowed so every existing caller keeps refusing; the
    difference is that there is nothing to ask for. Raised on a production
    instance, by both `commit` and the attempt to grant the right — a guard
    that can be granted around is not a guard.
    """


HUMAN_OWNER = {"kind": "human", "label": "browser"}

# Per-run stderr ceiling. The session-wide `_stderr` tail is far too short for
# a whole test class, but an uncapped collector would grow without limit on a
# long run. The journal keeps every line either way.
RUN_STDERR_LIMIT = 20000
# A command's own log, bounded so one noisy exec cannot carry a session's
# worth of lines into every response. The journal keeps all of it.
EXEC_STDERR_LIMIT = 2000
# The stderr reader is its own task: lines already in the pipe when the result
# frame lands have not necessarily been appended yet. Yielding briefly keeps
# the tail of a run's output from being cut at an arbitrary point — the same
# reason `_read_frames` drains stderr before declaring the process dead.
STDERR_DRAIN = 0.05


class Session:
    def __init__(
        self,
        session_id,
        target: Target,
        process,
        journal: Journal,
        on_event=None,
        owner: dict | None = None,
        allow_commit: bool | None = None,
        client_token: str | None = None,
        autoclose: bool = False,
    ):
        self.id = session_id
        self.target = target
        self.process = process
        self.journal = journal
        # Opaque, chosen by whoever asked for the session. Container and
        # database do not identify one: an agent may open the same target while
        # a browser is opening its own, and both need to know which is theirs.
        self.client_token = client_token
        self.owner = dict(owner or HUMAN_OWNER)
        # A human drives the UI and confirms every commit there; an agent has to
        # be granted the right explicitly. On a remote instance neither applies:
        # being the owner is enough locally, and is not enough on someone's
        # own Odoo, so a human starts without the right there too.
        if allow_commit is None:
            local_human = self.owner.get("kind") == "human" and not target.is_remote
            self.allow_commit = local_human
        else:
            self.allow_commit = allow_commit
        if target.stage == PRODUCTION:
            self.allow_commit = False
        # Held by whoever may execute code here. Rotated on every transfer, so a
        # previous owner's key stops working the moment ownership moves.
        self.write_key = secrets.token_urlsafe(24)
        # Keys of previous owners. They cannot type any more, but whoever handed
        # a session over keeps the right to end it or to take it back: giving
        # away the right to type is not giving away the session.
        self.former_keys: set[str] = set()
        self.hello: dict | None = None
        self.pending_commands = 0
        self._state = SessionState.STARTING
        self._closing = False
        self._on_event = on_event
        self._next_id = 0
        self._waiter: asyncio.Future | None = None
        self._waiter_id: int | None = None
        # Set when a command outlives its ceiling. The command keeps running in
        # the container (SIGINT is a request, not a guarantee), so the session
        # stays BUSY until its result frame finally arrives. Going READY here
        # would let a second command queue up behind the first one in the pipe.
        self._abandoned_id: int | None = None
        # What is holding BUSY (`exec`, `run_test`, …). None when not busy.
        # A timeout leaves the session BUSY, so this stays set until the result.
        self._activity: str | None = None
        self._hello_waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._stderr: deque[str] = deque(maxlen=2000)
        # A session opened to run one test and then get out of the way. It
        # announces itself finished once the run has really settled, and the
        # registry closes it — see `_maybe_autoclose`.
        self.autoclose = autoclose
        self._ran_test = False
        self._autoclose_announced = False
        # Live windows, one per in-flight run_test — see `_StderrWindow`.
        self._stderr_collectors: list[_StderrWindow] = []
        self._reader = asyncio.create_task(self._read_frames())
        self._stderr_reader = asyncio.create_task(self._read_stderr())

    @property
    def state(self) -> SessionState:

        return self._state

    def stderr_tail(self, limit: int = 200) -> list[str]:

        return list(self._stderr)[-limit:]

    def describe(self) -> dict:

        return {
            "id": self.id,
            "state": self._state.value,
            # The identity slot: a container name locally, a build id on
            # odoo.sh. Kept under this key so every existing reader — the UI,
            # the journal, the MCP tools — keeps working.
            "container": self.target.name,
            "database": self.target.database,
            # A local container has neither, and a reader that only knows
            # about containers keeps working because the slot above is shared.
            "kind": self.target.kind,
            "host": self.target.host,
            "stage": self.target.stage,
            "odoo": (self.hello or {}).get("odoo"),
            "python": (self.hello or {}).get("python"),
            "pending_commands": self.pending_commands,
            "owner": dict(self.owner),
            "allow_commit": self.allow_commit,
            "client_token": self.client_token,
            "activity": self._activity,
        }

    # -- lifecycle -------------------------------------------------------

    async def start(self, timeout: float = 90.0) -> dict:
        try:
            self.hello = await asyncio.wait_for(self._hello_waiter, timeout)
        except TimeoutError:
            self._die(f"no hello frame within {timeout:.0f}s")
            await self._stop_process()
            raise SessionDead("session did not start: " + self._stderr_text()) from None
        if self._closing or self._state in (SessionState.CLOSED, SessionState.DEAD):
            raise SessionDead("session is closed")
        self._set_state(SessionState.READY)
        self.journal.write(
            "session_open",
            owner=dict(self.owner),
            allow_commit=self.allow_commit,
            container=self.target.name,
            database=self.target.database,
            odoo_bin=self.target.odoo_bin,
            odoo=self.hello.get("odoo"),
            python=self.hello.get("python"),
            pid=self.hello.get("pid"),
        )

        return self.hello

    async def close(self, timeout: float = 10.0) -> None:
        if self._state in (SessionState.CLOSED, SessionState.DEAD):

            return
        self._closing = True
        self._fail_pending_waiters("session is closed")
        await asyncio.sleep(0)
        with contextlib.suppress(Exception):
            await self._request(close_frame(self._take_id()), timeout)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.process.wait(), timeout)
        if self.process.returncode is None:
            await self.kill()

            return
        self._set_state(SessionState.CLOSED)
        self.journal.write("session_close")
        self._cancel_readers()

    async def kill(self) -> None:
        self._closing = True
        was_dead = self._state is SessionState.DEAD
        self._fail_pending_waiters("session is closed")
        pid = (self.hello or {}).get("pid")
        if pid:
            with contextlib.suppress(Exception):
                await send_signal(self.target, pid, "KILL")
        with contextlib.suppress(ProcessLookupError):
            self.process.kill()
        with contextlib.suppress(Exception):
            await self.process.wait()
        if not was_dead:  # a dead session keeps its cause of death
            self._set_state(SessionState.CLOSED)
            self.journal.write("session_close", killed=True)
        self._cancel_readers()

    def transfer_owner(self, owner: dict) -> str:
        """Hand the session to someone else and return the new write key.

        The process, its namespace and the open transaction all survive: that is
        the point of a handover. Only the right to type into it moves.
        """
        previous = dict(self.owner)
        self.owner = dict(owner)
        self.former_keys.add(self.write_key)
        self.write_key = secrets.token_urlsafe(24)
        # Humans confirm each commit in the UI. An agent starts without the
        # right; a grant does not travel either way.
        self.allow_commit = self.owner.get("kind") == "human"
        self.journal.write(
            "owner_changed",
            **{"from": previous, "to": dict(self.owner),
               "pending_commands": self.pending_commands},
        )
        self._emit({"kind": "owner", "owner": dict(self.owner), "session": self.id})

        return self.write_key

    def set_allow_commit(self, allowed: bool) -> None:
        if allowed and self.target.stage == PRODUCTION:
            raise CommitForbidden(self._production_refusal())
        self.allow_commit = allowed
        self.journal.write("policy_changed", allow_commit=allowed)
        self._emit({"kind": "policy", "allow_commit": allowed, "session": self.id})

    def _production_refusal(self) -> str:

        return (
            f"this session runs on production ({self.target.name} at "
            f"{self.target.host}); commit is refused there, rollback is not"
        )

    def _may_commit(self) -> bool:
        """Whether a commit may even be attempted.

        Locally a human owner confirms in the UI, so the flag is an agent
        gate. On a remote instance that reasoning does not carry: the flag
        gates everyone, and on production nothing lifts it.
        """
        if self.target.stage == PRODUCTION:
            raise CommitForbidden(self._production_refusal())
        if self.owner.get("kind") == "human" and not self.target.is_remote:

            return True

        return bool(self.allow_commit)

    async def interrupt(self) -> None:
        pid = (self.hello or {}).get("pid")
        if not pid:
            raise SessionNotReady("no pid yet")
        await send_signal(self.target, pid, "INT")
        self.journal.write("interrupt", actor=dict(self.owner))

    # -- commands --------------------------------------------------------

    async def execute(self, code: str, timeout: float = 300.0) -> dict:
        self._ensure_acceptable("exec")
        request_id = self._take_id()
        self.journal.write("exec", id=request_id, code=code, actor=dict(self.owner))
        # Collected the same way as for a test run: the lines are on the
        # journal either way, but a caller that is not handed them concludes
        # there was no log — and writes a logging handler of its own.
        window = _StderrWindow(EXEC_STDERR_LIMIT)
        self._stderr_collectors.append(window)
        try:
            result = await self._request(exec_frame(request_id, code), timeout)
            if window.total:
                # Only when Odoo actually said something. exec is the hot
                # path — a trivial command runs in under a millisecond, and
                # waiting out the drain on every one of them would cost more
                # than the whole command.
                await asyncio.sleep(STDERR_DRAIN)
        finally:
            self._stderr_collectors.remove(window)
        result = {
            **result,
            "stderr": list(window.lines),
            "stderr_truncated": window.truncated,
        }
        self.journal.write("result", **_journal_fields(result))
        self.pending_commands += 1

        return result

    async def run_test(
        self,
        module: str,
        test_class: str,
        test_method: str | None = None,
        timeout: float = 300.0,
    ) -> dict:
        self._ensure_acceptable("run_test")
        # Odoo's own run_tests() rolls back env.cr if it holds an open
        # transaction before testing (odoo/tests/shell.py) — silently, unless
        # we say so here. Whatever was pending is gone either way.
        discarded_pending = self.pending_commands > 0
        self.pending_commands = 0
        request_id = self._take_id()
        self.journal.write(
            "run_test", id=request_id, module=module, test_class=test_class,
            test_method=test_method, actor=dict(self.owner),
        )
        self._ran_test = True
        window = _StderrWindow(RUN_STDERR_LIMIT)
        self._stderr_collectors.append(window)
        try:
            result = await self._request(
                run_test_frame(request_id, module, test_class, test_method), timeout
            )
            await asyncio.sleep(STDERR_DRAIN)  # let the reader catch up on the tail
        finally:
            # A run that raised (timeout, death) must not leave a collector
            # behind: it would keep filling for the rest of the session.
            self._stderr_collectors.remove(window)
        result = {
            **result,
            "stderr": list(window.lines),
            "stderr_truncated": window.truncated,
            "discarded_pending": discarded_pending,
        }
        self.journal.write("result", **_journal_fields(result))
        self._maybe_autoclose()

        return result

    async def commit(self, timeout: float = 300.0) -> dict:

        return await self._boundary("commit", commit_frame, timeout)

    async def rollback(self, timeout: float = 300.0) -> dict:

        return await self._boundary("rollback", rollback_frame, timeout)

    async def _boundary(self, kind, builder, timeout) -> dict:
        if kind == "commit" and not self._may_commit():
            raise CommitNotAllowed(
                "this session may not commit; a human has to grant the right first"
            )
        request_id = self._take_id()
        result = await self._request(builder(request_id), timeout)
        self.journal.write(
            kind, id=request_id, error=result.get("error"), actor=dict(self.owner)
        )
        if not result.get("error"):
            self.pending_commands = 0

        return result

    def _ensure_acceptable(self, frame_type: str) -> None:
        """Raise unless the session can take this frame right now.

        Called before anything is journaled or an id is spent, so a rejected
        command leaves no trace of having been attempted.
        """
        if self._state is SessionState.DEAD:
            raise SessionDead("session is dead: " + self._stderr_text())
        if self._state is SessionState.CLOSED:
            raise SessionDead("session is closed")
        if self._closing and frame_type != "close":
            raise SessionDead("session is closed")
        if self._state is SessionState.STARTING:
            raise SessionNotReady("session is still starting")
        # `close` is the one frame allowed through a busy session: it is how a
        # command abandoned at timeout gets cleaned up.
        if self._state is SessionState.BUSY and frame_type != "close":
            raise SessionBusy(self._busy_reason())

    async def _request(self, frame: dict, timeout: float) -> dict:
        self._ensure_acceptable(frame.get("t", ""))

        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._waiter = waiter
        self._waiter_id = frame.get("id")
        # Set before BUSY so the state event already names the work. A close
        # accepted while busy must not overwrite it: the original command is
        # still what the container is doing.
        if self._state is not SessionState.BUSY:
            self._activity = frame.get("t")
        self._set_state(SessionState.BUSY)
        self.process.stdin.write(encode_frame(frame).encode("utf-8"))
        await self.process.stdin.drain()
        abandoned = False
        try:

            return await asyncio.wait_for(waiter, timeout)
        except TimeoutError:
            abandoned = True
            self._abandoned_id = frame.get("id")
            pid = (self.hello or {}).get("pid")
            if pid:
                with contextlib.suppress(Exception):
                    await send_signal(self.target, pid, "INT")
            self.journal.write("timeout", id=frame.get("id"), seconds=timeout)
            raise TimeoutError(f"command exceeded {timeout}s, interrupt sent") from None
        finally:
            if self._waiter is waiter:  # a concurrent close may own it by now
                self._waiter = None
                self._waiter_id = None
            if self._state is SessionState.BUSY and not abandoned:
                self._set_state(SessionState.READY)

    def held_by(self, key: str | None) -> bool:
        """True for the current owner and for anyone who owned it before.

        Enough to close the session or take it back; never enough to type,
        which always checks `write_key` itself.
        """

        return bool(key) and (key == self.write_key or key in self.former_keys)

    def _busy_reason(self) -> str:
        if self._abandoned_id is not None:

            return (
                f"command {self._abandoned_id} exceeded its ceiling and is still "
                "running in the container; interrupt or kill the session"
            )

        return "a command is already running"

    def _take_id(self) -> int:
        self._next_id += 1

        return self._next_id

    # -- pipes -----------------------------------------------------------

    async def _read_frames(self) -> None:
        while True:
            try:
                line = await self.process.stdout.readline()
            except ValueError:
                # asyncio wraps LimitOverrunError: a clipped bootstrap frame
                # still exceeds the default 64 KiB limit. Swallowing it here
                # keeps the session usable; raising the spawn limit is what
                # makes the frame readable in the first place.
                self._overrun_frame()
                continue
            if not line:
                break
            try:
                frame = decode_frame(line.decode("utf-8", "replace"))
            except ProtocolError:
                continue
            kind = frame.get("t")
            if kind == "hello":
                if not self._hello_waiter.done():
                    self._hello_waiter.set_result(frame)
            elif kind == "result" and frame.get("id") == self._abandoned_id:
                self._settle_abandoned(frame)
            elif (
                kind in ("result", "bye")
                and self._waiter is not None
                and not self._waiter.done()
                and frame.get("id") == self._waiter_id
            ):
                self._waiter.set_result(frame)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(self._stderr_reader), timeout=0.2)
        self._die("process ended")

    async def _read_stderr(self) -> None:
        while True:
            line = await self.process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\n")
            self._stderr.append(text)
            for collector in self._stderr_collectors:
                collector.append(text)
            self.journal.write("stderr", line=text)
            self._emit({"kind": "stderr", "line": text})

    def _stderr_text(self) -> str:

        return "\n".join(self.stderr_tail(20))

    def _overrun_frame(self) -> None:
        """The current line was dropped; unblock whoever is waiting for it."""
        waiting = (
            self._waiter is not None
            and not self._waiter.done()
            and self._waiter_id is not None
        )
        # The id has to be the command this line belonged to. On the abandoned
        # path `_request` has already cleared `_waiter_id`, and an
        # `abandoned_result` journalled with `id: null` never rejoins its exec
        # entry: `feed_from_records` drops it and the cell reads `running` for
        # the rest of the journal's life.
        error = {
            "t": "result",
            "id": self._waiter_id if waiting else self._abandoned_id,
            "stdout": "",
            "stdout_truncated": False,
            "result": None,
            "result_truncated": True,
            "error": {
                "type": "FrameTooLarge",
                "message": "result frame exceeded the stdout line limit",
                "traceback": "",
            },
            "duration": 0.0,
        }
        if waiting:
            self._waiter.set_result(error)
        elif self._abandoned_id is not None:
            self._settle_abandoned(error)

    def _settle_abandoned(self, frame: dict) -> None:
        """The command we stopped waiting for has finally finished.

        Nobody is listening for its result any more, so it goes to the journal
        and the session becomes usable again.
        """
        self._abandoned_id = None
        self.journal.write("abandoned_result", **_journal_fields(frame))
        if self._state is SessionState.BUSY:
            self._set_state(SessionState.READY)
        self._maybe_autoclose()

    def _maybe_autoclose(self) -> None:
        """Say the session has done what it was opened for.

        Announced only after the run has really settled *and* been journalled:
        the registry closes on this, and closing any earlier would put
        `session_close` ahead of the result in the transcript. A run that blew
        its ceiling is deliberately not settled — it still owns the container,
        so the announcement waits for `_settle_abandoned`.
        """
        if not (self.autoclose and self._ran_test) or self._autoclose_announced:

            return
        self._autoclose_announced = True
        self._emit({"kind": "autoclose", "session": self.id})

    def _fail_pending_waiters(self, reason: str) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_exception(SessionDead(reason))
        if not self._hello_waiter.done():
            self._hello_waiter.set_exception(SessionDead(reason))

    def _die(self, reason: str) -> None:
        if self._closing or self._state in (SessionState.CLOSED, SessionState.DEAD):

            return
        self._set_state(SessionState.DEAD)
        self.journal.write("session_died", reason=reason, stderr=self.stderr_tail(50))
        self._maybe_autoclose()
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_exception(SessionDead(f"{reason}: {self._stderr_text()}"))
        if not self._hello_waiter.done():
            self._hello_waiter.set_exception(SessionDead(reason))

    def _cancel_readers(self) -> None:
        for task in (self._reader, self._stderr_reader):
            task.cancel()

    async def _stop_process(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            self.process.kill()
        with contextlib.suppress(Exception):
            await self.process.wait()
        self._cancel_readers()
        await asyncio.gather(
            self._reader,
            self._stderr_reader,
            return_exceptions=True,
        )

    def _set_state(self, state: SessionState) -> None:
        if state is self._state:

            return
        if state in (SessionState.CLOSED, SessionState.DEAD):
            self._abandoned_id = None  # nothing is coming back now
        if state is not SessionState.BUSY:
            self._activity = None
        self._state = state
        self._emit({
            "kind": "state",
            "state": state.value,
            "session": self.id,
            "activity": self._activity,
        })

    def _emit(self, event: dict) -> None:
        if self._on_event is not None:
            self._on_event(event)
