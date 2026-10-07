"""One live session: the state machine over a bootstrap process."""

import asyncio
import contextlib
import re
import secrets
import time
from collections import deque
from dataclasses import replace
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

# What Odoo's test result logs around each test, identical in 15 through 20
# (`odoo/tests/result.py`, `runner.py` in 15): `Starting X ...` from
# startTest, `FAIL: X` / `ERROR: X` from logError, `skipped X : why` from
# addSkip. The logger is the test's own module, which always sits under an
# addon's `tests` package — anchoring on it keeps an unrelated `ERROR:` out
# of the counts. What follows the message is perf_info, empty or `- - -`.
_TEST_LOGGER = r" odoo\.addons\.\S+\.tests(?:\.\S+)?: "
# A fixture that fails outside any test — setUpClass, setUpModule and their
# teardowns — is reported through an `_ErrorHolder`, and the result logs
# under the holder's module, not the test's: `odoo.tests.suite` from 16,
# `unittest.suite` in 15. Those two loggers say nothing else, but the
# description is pinned to the fixture shape anyway.
_SUITE_LOGGER = r" (?:odoo\.tests|unittest)\.suite: "
_FIXTURE = r"((?:setUp|tearDown)(?:Class|Module) \([\w.]+\))"
# perf_info on a one-line message: `- - -`, `- - - -`, or a query count and
# two timings, sometimes followed by a cursor mode.
_PERF = r"(?: - - -(?: -)?| \d+ \d+\.\d+ \d+\.\d+(?: \S+)?)?\s*$"
_TEST_LINES = (
    ("start", re.compile(_TEST_LOGGER + r"Starting (.+?) \.\.\.(?:\s|$)")),
    ("failure", re.compile(_TEST_LOGGER + r"FAIL: (.+?)" + _PERF)),
    ("error", re.compile(_TEST_LOGGER + r"ERROR: (.+?)" + _PERF)),
    ("skip", re.compile(_TEST_LOGGER + r"skipped (.+?) : ")),
    ("failure", re.compile(_SUITE_LOGGER + r"FAIL: " + _FIXTURE + _PERF)),
    ("error", re.compile(_SUITE_LOGGER + r"ERROR: " + _FIXTURE + _PERF)),
    ("skip", re.compile(_SUITE_LOGGER + r"skipped " + _FIXTURE + r" : ")),
)
_PROGRESS_COUNTER = {
    "start": "started", "failure": "failures", "error": "errors", "skip": "skipped",
}


# How unittest names a fixture that failed outside any test. A class:
# `setUpClass (odoo.addons.sale.tests.test_sale_order.TestBroken)`. A test
# file: `setUpModule (odoo.addons.sale.tests.test_sale_order)` — whose last
# part is a file, not a class, and no test tag can name a file.
_CLASS_FIXTURE = re.compile(r"^(?:setUp|tearDown)Class \((?:[\w.]+\.)?(\w+)\)$")
_MODULE_FIXTURE = re.compile(r"^(?:setUp|tearDown)Module \([\w.]+\)$")
# `Subtest TestX.test_y (n=2)`: the method is what can be run again.
_SUBTEST = re.compile(r"^Subtest (\w+\.\w+) ")


def rerun_spec(module: str, description: str) -> str:
    """What os_run_test takes to run this failure again.

    Odoo names a test `Class.method`; the module is the run's own, since a
    run only ever loads one module's tests. A failed subtest is rerun as its
    method, a class that failed to set up as the class, and a test file that
    failed to set up as the whole module — the narrowest thing a tag can
    name that still holds it. Anything else is returned as Odoo said it
    rather than guessed at.
    """
    subtest = _SUBTEST.match(description)
    if subtest:

        return f"{module}.{subtest.group(1)}"
    fixture = _CLASS_FIXTURE.match(description)
    if fixture:

        return f"{module}.{fixture.group(1)}"
    if _MODULE_FIXTURE.match(description):

        return module
    if re.fullmatch(r"\w+\.\w+", description):

        return f"{module}.{description}"

    return description


def parse_test_line(line: str) -> tuple[str, str] | None:
    """`(kind, test description)` if Odoo logged this about a test, else None."""
    for kind, pattern in _TEST_LINES:
        match = pattern.search(line)
        if match:

            return kind, match.group(1)

    return None


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
        if allow_commit is None:
            self.allow_commit = self._starts_with_commit()
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
        # Who each key was issued to, current and former. Granting commit, or
        # handing a session to a human, is a human's act: an agent holding its
        # own key must not be able to do either and so lift its own gate.
        self._key_kinds: dict[str, str | None] = {self.write_key: self.owner.get("kind")}
        self.hello: dict | None = None
        self.pending_commands = 0
        self.inherited_pending = 0
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
        # The frame type of that command, so its late result is accounted for
        # the way an answer in time would have been: an exec is pending work,
        # a commit or rollback that finally finished ends the transaction.
        self._abandoned_kind: str | None = None
        # What is holding BUSY (`exec`, `run_test`, …). None when not busy.
        # A timeout leaves the session BUSY, so this stays set until the result.
        self._activity: str | None = None
        # How far the running test has got, read off Odoo's own log lines.
        # None unless a run_test holds BUSY — see `parse_test_line`.
        self._test_progress: dict | None = None
        # What failed in the running test, as specs to run again. A whole
        # module's log is thousands of lines; this is the part an agent acts on.
        self._test_failed: list[dict] = []
        self._test_module: str | None = None
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
            # How many of those were run by a previous owner.
            "inherited_pending": self.inherited_pending,
            "owner": dict(self.owner),
            "allow_commit": self.allow_commit,
            "client_token": self.client_token,
            "activity": self._activity,
            "test_progress": dict(self._test_progress) if self._test_progress else None,
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
        if not self.target.database and self.hello.get("db"):
            # A server card may leave the database to the server's config. The
            # shell knows which one it opened; until hello nothing did, and a
            # session that cannot say what it is writing to is not one to
            # commit in blind.
            self.target = replace(self.target, database=self.hello["db"])
        self._set_state(SessionState.READY)
        self.journal.write(
            "session_open",
            owner=dict(self.owner),
            allow_commit=self.allow_commit,
            container=self.target.name,
            database=self.target.database,
            odoo_bin=self.target.odoo_bin,
            # `kind` is the record's own type, so the target's goes under its
            # own name. Without it a journal cannot say whether reopening the
            # same target is even a thing a caller may do.
            target_kind=self.target.kind,
            host=self.target.host,
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
        # Whatever is pending now belongs to whoever is leaving. A commit by
        # the new owner would write it too, so the count is kept rather than
        # left to be noticed: os_commit refuses on it once, and the UI can say
        # whose work is in the transaction.
        self.inherited_pending = self.pending_commands
        self.former_keys.add(self.write_key)
        self.write_key = secrets.token_urlsafe(24)
        self._key_kinds[self.write_key] = self.owner.get("kind")
        # A grant does not travel either way: the new owner starts where
        # anyone of their kind starts on this target. That used to be "a human
        # may", full stop — and on a remote instance a session handed to an
        # agent and taken back came home able to commit, with no grant ever
        # made, while on production it claimed a right nothing would honour.
        self.allow_commit = self._starts_with_commit()
        self.journal.write(
            "owner_changed",
            **{"from": previous, "to": dict(self.owner),
               "pending_commands": self.pending_commands},
        )
        self._emit({"kind": "owner", "owner": dict(self.owner), "session": self.id})

        return self.write_key

    def _starts_with_commit(self) -> bool:
        """Whether the current owner holds the right without being granted it.

        A human drives the UI and confirms every commit there; an agent has
        to be granted the right explicitly. On a remote instance neither
        applies: being the owner is enough locally, and is not enough on
        someone's own Odoo, so a human starts without the right there too —
        and on production nothing ever holds it.
        """

        return (
            self.owner.get("kind") == "human"
            and not self.target.is_remote
            and self.target.stage != PRODUCTION
        )

    def held_by_human(self, key: str | None) -> bool:
        """`held_by`, for a key that was issued to a human.

        What separates "the agent this session was handed to" from "the human
        who handed it over": both hold a key that `held_by` accepts, and only
        one of them may grant commit or give the session to a human.
        """

        return self.held_by(key) and self._key_kinds.get(key) == "human"

    def key_status(self, key: str | None) -> str:
        """What a presented key is worth here, without saying whose it is."""
        if key and key == self.write_key:

            return "owner"
        if key and key in self.former_keys:

            return "former_owner"

        return "invalid"

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

    async def execute(
        self, code: str, timeout: float = 300.0, read_only: bool = False
    ) -> dict:
        """Run code; `read_only` is the caller's word that it writes nothing.

        Such a command is journalled like any other, flagged, and not counted
        as pending work: a source read from os_source is not something a
        handover should warn about, nor a test run report as discarded.
        """
        self._ensure_acceptable("exec")
        request_id = self._take_id()
        flags = {"read_only": True} if read_only else {}
        self.journal.write(
            "exec", id=request_id, code=code, actor=dict(self.owner), **flags
        )
        # Collected the same way as for a test run: the lines are on the
        # journal either way, but a caller that is not handed them concludes
        # there was no log — and writes a logging handler of its own.
        window = _StderrWindow(EXEC_STDERR_LIMIT)
        self._stderr_collectors.append(window)
        try:
            result = await self._request(
                exec_frame(request_id, code), timeout,
                account="read" if read_only else "exec",
            )
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
        if not read_only:
            self.pending_commands += 1

        return result

    async def run_test(
        self,
        module: str,
        test_class: str | None = None,
        test_method: str | None = None,
        timeout: float = 300.0,
    ) -> dict:
        self._ensure_acceptable("run_test")
        # Odoo's own run_tests() rolls back env.cr if it holds an open
        # transaction before testing (odoo/tests/shell.py) — silently, unless
        # we say so here. Whatever was pending is gone either way.
        discarded_pending = self.pending_commands > 0
        self.pending_commands = 0
        self.inherited_pending = 0
        request_id = self._take_id()
        self.journal.write(
            "run_test", id=request_id, module=module, test_class=test_class,
            test_method=test_method, actor=dict(self.owner),
        )
        self._ran_test = True
        window = _StderrWindow(RUN_STDERR_LIMIT)
        self._stderr_collectors.append(window)
        # Cleared with `_activity`, when the session leaves BUSY: a timed-out
        # run is still running and still worth watching.
        self._test_progress = {
            "spec": ".".join(part for part in (module, test_class, test_method) if part),
            "current": None,
            "started": 0,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "started_at": time.time(),
        }
        self._emit_test_progress()
        self._test_failed = []
        self._test_module = module
        try:
            result = await self._request(
                run_test_frame(request_id, module, test_class, test_method), timeout
            )
            await asyncio.sleep(STDERR_DRAIN)  # let the reader catch up on the tail
        finally:
            # A run that raised (timeout, death) must not leave a collector
            # behind: it would keep filling for the rest of the session.
            self._stderr_collectors.remove(window)
            self._test_module = None
        result = {
            **result,
            "failed": self._test_failed,
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
            # The transaction is over either way, so nobody's work is pending
            # any more — including whatever a handover carried in.
            self.pending_commands = 0
            self.inherited_pending = 0

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

    async def _request(
        self, frame: dict, timeout: float, account: str | None = None
    ) -> dict:
        """Send one frame and wait for its answer.

        `account` is how a late answer is booked if this one is abandoned —
        the frame type unless the caller says otherwise; see
        `_settle_abandoned`.
        """
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
            self._abandoned_kind = account or frame.get("t")
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
            # Not while closing: the command a close cut short is still what
            # the container is doing, and going READY in between told every
            # watcher the session was free — then busy with `close`, then
            # free again, then closed — and cleared a live test run's card.
            if self._state is SessionState.BUSY and not abandoned and not self._closing:
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
            self._note_test_line(text)

    def _note_test_line(self, line: str) -> None:
        if self._test_progress is None:

            return
        parsed = parse_test_line(line)
        if parsed is None:

            return
        kind, description = parsed
        if kind in ("failure", "error") and self._test_module:
            failed = {"test": rerun_spec(self._test_module, description), "kind": kind}
            if failed not in self._test_failed:
                self._test_failed.append(failed)
        self._test_progress[_PROGRESS_COUNTER[kind]] += 1
        if kind == "start":
            self._test_progress["current"] = description
        self._emit_test_progress()

    def _emit_test_progress(self) -> None:
        self._emit({
            "kind": "test_progress",
            "progress": self.describe()["test_progress"],
            "session": self.id,
        })

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
        and the session becomes usable again. What it did is booked the way an
        answer in time would have been: an exec that ran on past its ceiling
        may still have written, and a commit that finally got its lock did
        commit — the journal has to say so, or a transcript reads "discarded"
        over a write that happened.
        """
        kind = self._abandoned_kind
        self._abandoned_id = None
        self._abandoned_kind = None
        self.journal.write("abandoned_result", **_journal_fields(frame))
        if kind == "exec":
            self.pending_commands += 1
        elif kind in ("commit", "rollback"):
            self.journal.write(
                kind, id=frame.get("id"), error=frame.get("error"),
                actor=dict(self.owner), late=True,
            )
            if not frame.get("error"):
                self.pending_commands = 0
                self.inherited_pending = 0
        if self._state is SessionState.BUSY and not self._closing:
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
            # The cause is in what Odoo logged on its way out — a database
            # that does not exist, a broken config, an import error. Without
            # it an agent was told only "process ended" and had nothing to
            # act on; the UI was not, because it streams the startup log.
            tail = self._stderr_text()
            self._hello_waiter.set_exception(SessionDead(
                f"{reason} before the session started"
                + (f": {tail}" if tail else "")
            ))

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
            self._abandoned_kind = None
        if state is not SessionState.BUSY:
            self._activity = None
            if self._test_progress is not None:
                self._test_progress = None
                self._emit_test_progress()
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
