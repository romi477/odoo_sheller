import asyncio

import pytest

from odoo_sheller.journal import Journal, feed_from_records
from odoo_sheller.protocol import FRAME_LINE_LIMIT
from odoo_sheller.session import (
    CommitNotAllowed,
    Session,
    SessionBusy,
    SessionDead,
    SessionNotReady,
    SessionState,
)
from odoo_sheller.transport import Target

TARGET = Target(container="c", database="db", odoo_bin="/opt/odoo/odoo-bin")

FAKE = r"""
import json, sys, time
sys.stdout.write(json.dumps({"t":"hello","protocol":1,"odoo":"19.0","python":"3.12.0",
                             "db":"db","uid":1,"pid":4242}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    frame = json.loads(line)
    if frame["t"] == "close":
        sys.stdout.write(json.dumps({"t":"bye","id":frame["id"]}) + "\n")
        sys.stdout.flush()
        break
    if frame.get("code") == "SLEEP":
        time.sleep(3)
    if frame.get("code") == "DIE":
        sys.stderr.write("odoo exploded\n")
        sys.stderr.flush()
        break
    sys.stdout.write(json.dumps({"t":"result","id":frame["id"],"stdout":"out\n",
                                 "stdout_truncated":False,"result":"42",
                                 "result_truncated":False,"error":None,
                                 "duration":0.01}) + "\n")
    sys.stdout.flush()
"""

LATE_RESULT_FAKE = r"""
import json, sys, time
sys.stdout.write(json.dumps({"t":"hello","protocol":1,"odoo":"19.0","python":"3.12.0",
                             "db":"db","uid":1,"pid":4242}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    frame = json.loads(line)
    if frame["t"] == "close":
        sys.stdout.write(json.dumps({"t":"bye","id":frame["id"]}) + "\n")
        sys.stdout.flush()
        break
    if frame.get("code") == "FIRST":
        time.sleep(0.2)
    sys.stdout.write(json.dumps({"t":"result","id":frame["id"],"stdout":"",
                                 "stdout_truncated":False,"result":frame.get("code"),
                                 "result_truncated":False,"error":None,
                                 "duration":0.01}) + "\n")
    sys.stdout.flush()
"""

HUGE_RESULT_FAKE = r"""
import json, sys
sys.stdout.write(json.dumps({"t":"hello","protocol":1,"odoo":"19.0","python":"3.12.0",
                             "db":"db","uid":1,"pid":4242}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    frame = json.loads(line)
    if frame["t"] == "close":
        sys.stdout.write(json.dumps({"t":"bye","id":frame["id"]}) + "\n")
        sys.stdout.flush()
        break
    payload = "x" * 80000
    sys.stdout.write(json.dumps({"t":"result","id":frame["id"],"stdout":"",
                                 "stdout_truncated":False,"result":payload,
                                 "result_truncated":False,"error":None,
                                 "duration":0.01}) + "\n")
    sys.stdout.flush()
"""

OVERSIZED_LATE_FAKE = r"""
import json, sys, time
sys.stdout.write(json.dumps({"t":"hello","protocol":1,"odoo":"19.0","python":"3.12.0",
                             "db":"db","uid":1,"pid":4242}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    frame = json.loads(line)
    if frame["t"] == "close":
        sys.stdout.write(json.dumps({"t":"bye","id":frame["id"]}) + "\n")
        sys.stdout.flush()
        break
    time.sleep(0.2)
    sys.stdout.write(json.dumps({"t":"result","id":frame["id"],"stdout":"y" * 5000,
                                 "stdout_truncated":False,"result":None,
                                 "result_truncated":False,"error":None,
                                 "duration":0.01}) + "\n")
    sys.stdout.flush()
"""


async def make_session(tmp_path, script=FAKE, **kwargs):
    proc = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=FRAME_LINE_LIMIT,
    )

    return Session("s1", TARGET, proc, Journal(tmp_path / "s1.jsonl"), **kwargs)


async def test_ready_only_after_hello(tmp_path):
    session = await make_session(tmp_path)
    assert session.state is SessionState.STARTING
    hello = await session.start()
    assert session.state is SessionState.READY
    assert hello["odoo"] == "19.0"
    assert session.hello["pid"] == 4242
    await session.close()


async def test_start_timeout_stops_process_and_readers(tmp_path):
    session = await make_session(tmp_path, script="import time; time.sleep(10)")
    try:
        with pytest.raises(SessionDead):
            await session.start(timeout=0.05)
        assert session.process.returncode is not None
        assert session._reader.done()
        assert session._stderr_reader.done()
    finally:
        if session.process.returncode is None:
            session.process.kill()
            await session.process.wait()
        session._cancel_readers()


async def test_execute_returns_the_result_frame(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    result = await session.execute("1 + 1")
    assert result["stdout"] == "out\n"
    assert result["result"] == "42"
    assert session.state is SessionState.READY
    await session.close()


async def test_a_result_over_64kib_does_not_leave_the_session_busy(tmp_path):
    """asyncio's default StreamReader dies at 64 KiB; res.users.read() is larger."""
    session = await make_session(tmp_path, script=HUGE_RESULT_FAKE)
    await session.start()
    result = await session.execute("self.read()[0]", timeout=2)
    assert len(result["result"]) == 80000
    assert session.state is SessionState.READY
    await session.close()


async def test_second_command_while_busy_is_rejected(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    running = asyncio.create_task(session.execute("SLEEP"))
    await asyncio.sleep(0.2)
    assert session.state is SessionState.BUSY
    with pytest.raises(SessionBusy):
        await session.execute("1 + 1")
    await running
    await session.close()


async def test_execute_before_hello_is_rejected(tmp_path):
    session = await make_session(tmp_path)
    with pytest.raises(SessionNotReady):
        await session.execute("1 + 1")
    await session.start()
    await session.close()


async def test_process_death_marks_dead_and_fails_the_pending_command(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    with pytest.raises(SessionDead) as excinfo:
        await session.execute("DIE")
    assert "odoo exploded" in str(excinfo.value)
    assert session.state is SessionState.DEAD
    with pytest.raises(SessionDead):
        await session.execute("1 + 1")


async def test_process_death_includes_stderr_written_after_stdout_eof(tmp_path):
    command_sent = asyncio.Event()

    class FakeStdin:
        def write(self, data):
            command_sent.set()

        async def drain(self):
            pass

    class FakeStdout:
        def __init__(self):
            self.hello_pending = True

        async def readline(self):
            if self.hello_pending:
                self.hello_pending = False

                return (
                    b'{"t":"hello","protocol":1,"odoo":"19.0","python":"3.12.0",'
                    b'"db":"db","uid":1,"pid":4242}\n'
                )
            await command_sent.wait()

            return b""

    class FakeStderr:
        def __init__(self):
            self.line_pending = True

        async def readline(self):
            await command_sent.wait()
            if self.line_pending:
                self.line_pending = False
                await asyncio.sleep(0.05)

                return b"last fatal detail\n"

            return b""

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.returncode = 1

        async def wait(self):

            return self.returncode

    process = FakeProcess()
    session = Session("s1", TARGET, process, Journal(tmp_path / "s1.jsonl"))
    await session.start()
    with pytest.raises(SessionDead) as excinfo:
        await session.execute("DIE")
    assert "last fatal detail" in str(excinfo.value)


async def test_pending_counter_tracks_transaction_boundaries(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    await session.execute("a")
    await session.execute("b")
    assert session.pending_commands == 2
    await session.commit()
    assert session.pending_commands == 0
    await session.execute("c")
    await session.rollback()
    assert session.pending_commands == 0
    await session.close()


async def test_close_moves_to_closed(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    await session.close()
    assert session.state is SessionState.CLOSED
    kinds = [record["kind"] for record in session.journal.records()]
    assert "session_died" not in kinds


async def test_execute_rejected_while_closing(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    close_task = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    with pytest.raises(SessionDead, match="session is closed"):
        await session.execute("1 + 1")
    await close_task
    assert session.state is SessionState.CLOSED


async def test_start_rejected_if_closed_before_ready(tmp_path):
    slow_hello = r"""
import json, sys, time
time.sleep(1)
sys.stdout.write(json.dumps({"t":"hello","protocol":1,"odoo":"19.0","python":"3.12.0",
                             "db":"db","uid":1,"pid":4242}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    frame = json.loads(line)
    if frame["t"] == "close":
        sys.stdout.write(json.dumps({"t":"bye","id":frame["id"]}) + "\n")
        sys.stdout.flush()
        break
"""
    session = await make_session(tmp_path, script=slow_hello)
    start_task = asyncio.create_task(session.start(timeout=5))
    await asyncio.sleep(0.1)
    await session.close()
    with pytest.raises(SessionDead):
        await start_task
    assert session.state is SessionState.CLOSED
    kinds = [record["kind"] for record in session.journal.records()]
    assert "session_open" not in kinds


async def test_close_fails_in_flight_execute_promptly(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    running = asyncio.create_task(session.execute("SLEEP"))
    await asyncio.sleep(0.2)
    assert session.state is SessionState.BUSY

    close_task = asyncio.create_task(session.close())
    with pytest.raises(SessionDead, match="session is closed"):
        await asyncio.wait_for(running, timeout=1.0)

    await close_task
    assert session.state is SessionState.CLOSED
    kinds = [record["kind"] for record in session.journal.records()]
    assert "session_died" not in kinds
    assert "session_close" in kinds


async def test_kill_fails_in_flight_execute_promptly(tmp_path, monkeypatch):
    async def fake_signal(container, pid, name):
        pass

    monkeypatch.setattr("odoo_sheller.session.send_signal", fake_signal)
    session = await make_session(tmp_path)
    await session.start()
    running = asyncio.create_task(session.execute("SLEEP"))
    await asyncio.sleep(0.2)
    assert session.state is SessionState.BUSY

    kill_task = asyncio.create_task(session.kill())
    with pytest.raises(SessionDead, match="session is closed"):
        await asyncio.wait_for(running, timeout=1.0)

    await kill_task
    assert session.state is SessionState.CLOSED
    kinds = [record["kind"] for record in session.journal.records()]
    assert "session_died" not in kinds
    assert "session_close" in kinds


async def test_stderr_is_collected_and_journalled(tmp_path):
    noisy = 'import sys, json, time\nsys.stderr.write("WARNING boom\\n")\nsys.stderr.flush()\n' + FAKE
    session = await make_session(tmp_path, script=noisy)
    await session.start()
    await asyncio.sleep(0.3)
    assert any("boom" in line for line in session.stderr_tail())
    await session.close()
    kinds = [record["kind"] for record in session.journal.records()]
    assert "stderr" in kinds


async def test_journal_records_the_whole_session(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    await session.execute("print('x')")
    await session.commit()
    await session.close()
    kinds = [record["kind"] for record in session.journal.records()]
    assert kinds[0] == "session_open"
    assert "exec" in kinds and "result" in kinds and "commit" in kinds
    assert kinds[-1] == "session_close"


async def test_events_are_published_on_state_changes(tmp_path):
    seen = []
    session = await make_session(tmp_path, on_event=lambda event: seen.append(event))
    await session.start()
    await session.close()
    kinds = [event["kind"] for event in seen]
    assert "state" in kinds
    assert {"ready", "closed"} <= {
        event.get("state") for event in seen if event["kind"] == "state"
    }


async def test_timeout_interrupts_and_reports(tmp_path, monkeypatch):
    signals = []

    async def fake_signal(container, pid, name):
        signals.append((container, pid, name))

    monkeypatch.setattr("odoo_sheller.session.send_signal", fake_signal)
    session = await make_session(tmp_path)
    await session.start()
    with pytest.raises(TimeoutError):
        await session.execute("SLEEP", timeout=0.3)
    assert signals == [("c", 4242, "INT")]
    await session.kill()


async def wait_for_state(session, state, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while session.state is not state:
        if loop.time() > deadline:
            raise AssertionError(f"session stayed {session.state.value}, wanted {state.value}")
        await asyncio.sleep(0.02)


async def test_timeout_keeps_the_session_busy_until_the_late_result_lands(
    tmp_path, monkeypatch
):
    """A timed-out command still owns the container, so nothing may queue behind it."""

    async def fake_signal(container, pid, name):
        pass

    monkeypatch.setattr("odoo_sheller.session.send_signal", fake_signal)
    session = await make_session(tmp_path, script=LATE_RESULT_FAKE)
    await session.start()
    try:
        with pytest.raises(TimeoutError):
            await session.execute("FIRST", timeout=0.05)
        assert session.state is SessionState.BUSY

        with pytest.raises(SessionBusy) as excinfo:
            await session.execute("SECOND", timeout=1)
        assert "still running" in str(excinfo.value)

        await wait_for_state(session, SessionState.READY)
        result = await session.execute("SECOND", timeout=1)
        assert result["result"] == "SECOND"
        # The rejected attempt spent no request id and left no journal entry.
        assert result["id"] == 2
    finally:
        await session.kill()

    kinds = [record["kind"] for record in session.journal.records()]
    assert kinds.count("exec") == 2
    assert "timeout" in kinds
    assert "abandoned_result" in kinds


async def test_describe_carries_the_opener_token(tmp_path):
    """The one field that says which client asked for this session."""
    session = await make_session(tmp_path, client_token="tab-7")
    try:
        assert session.describe()["client_token"] == "tab-7"
    finally:
        await session.kill()

    plain = await make_session(tmp_path)
    try:
        assert plain.describe()["client_token"] is None
    finally:
        await plain.kill()


async def test_an_oversized_late_result_keeps_its_command_id(tmp_path, monkeypatch):
    """`_waiter_id` is already None by then; an id-less record leaves the feed."""

    async def fake_signal(container, pid, name):
        pass

    monkeypatch.setattr("odoo_sheller.session.send_signal", fake_signal)
    process = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        OVERSIZED_LATE_FAKE,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=1024,  # far below the real ceiling: the 5000-byte line overruns
    )
    session = Session("s1", TARGET, process, Journal(tmp_path / "s1.jsonl"))
    await session.start()
    try:
        with pytest.raises(TimeoutError):
            await session.execute("FIRST", timeout=0.05)
        await wait_for_state(session, SessionState.READY)
    finally:
        await session.kill()

    records = session.journal.records()
    late = [record for record in records if record["kind"] == "abandoned_result"]
    assert late, "the dropped line must still settle the abandoned command"
    assert late[0]["id"] == 1, "an id of None never rejoins its exec entry"
    entry = next(one for one in feed_from_records(records)["entries"] if one["kind"] == "exec")
    assert entry["status"] == "error"
    assert entry["abandoned"] is True
    assert entry["result"]["error"]["type"] == "FrameTooLarge"


async def test_close_is_accepted_while_a_command_is_abandoned(tmp_path, monkeypatch):
    """Close must stay reachable when the session is busy with a lost command."""

    async def fake_signal(container, pid, name):
        pass

    monkeypatch.setattr("odoo_sheller.session.send_signal", fake_signal)
    session = await make_session(tmp_path, script=LATE_RESULT_FAKE)
    await session.start()
    with pytest.raises(TimeoutError):
        await session.execute("FIRST", timeout=0.05)
    assert session.state is SessionState.BUSY

    await session.close(timeout=3)
    assert session.state is SessionState.CLOSED
    assert session.process.returncode == 0
    assert not any(
        record.get("killed") for record in session.journal.records()
    ), "graceful close must not degrade into a kill"


async def test_interrupt_signals_the_container_pid(tmp_path, monkeypatch):
    signals = []

    async def fake_signal(container, pid, name):
        signals.append((container, pid, name))

    monkeypatch.setattr("odoo_sheller.session.send_signal", fake_signal)
    session = await make_session(tmp_path)
    await session.start()
    await session.interrupt()
    assert signals == [("c", 4242, "INT")]
    await session.close()


# --- ownership ----------------------------------------------------------


async def test_a_session_opens_owned_by_the_human_who_may_commit(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    try:
        assert session.owner["kind"] == "human"
        assert session.allow_commit is True
        assert session.write_key
        assert "write_key" not in session.describe()
    finally:
        await session.kill()


async def test_an_agent_session_may_not_commit_until_granted(tmp_path):
    session = await make_session(
        tmp_path, owner={"kind": "agent", "label": "claude"}
    )
    await session.start()
    try:
        with pytest.raises(CommitNotAllowed):
            await session.commit()
        assert session.journal.records()[-1]["kind"] != "commit", "nothing may reach the pipe"

        session.set_allow_commit(True)
        result = await session.commit()
        assert result["error"] is None
    finally:
        await session.kill()

    kinds = [record["kind"] for record in session.journal.records()]
    assert "policy_changed" in kinds


async def test_a_human_may_commit_even_when_the_flag_is_off(tmp_path):
    """Grant commit is an agent gate. The human confirms in the UI instead."""
    session = await make_session(tmp_path, allow_commit=False)
    await session.start()
    try:
        assert session.owner["kind"] == "human"
        result = await session.commit()
        assert result["error"] is None
    finally:
        await session.kill()


async def test_taking_a_session_back_lets_the_human_commit(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    try:
        session.transfer_owner({"kind": "agent", "label": "claude"})
        assert session.allow_commit is False
        session.transfer_owner({"kind": "human", "label": "browser"})
        assert session.allow_commit is True
        result = await session.commit()
        assert result["error"] is None
    finally:
        await session.kill()


async def test_transfer_rotates_the_key_and_keeps_the_namespace(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    try:
        old_key = session.write_key
        await session.execute("counter = 1")

        new_key = session.transfer_owner({"kind": "agent", "label": "claude"})
        assert new_key != old_key
        assert session.owner == {"kind": "agent", "label": "claude"}
        assert session.allow_commit is False, "a granted right must not travel"

        # The process never restarted, so the namespace is still there.
        result = await session.execute("counter")
        assert result["result"] == "42"  # the fake answers 42 to everything
    finally:
        await session.kill()

    records = session.journal.records()
    handover = next(record for record in records if record["kind"] == "owner_changed")
    assert handover["from"]["kind"] == "human"
    assert handover["to"]["label"] == "claude"
    assert handover["pending_commands"] == 1


async def test_every_command_records_its_actor(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    try:
        await session.execute("first")
        session.transfer_owner({"kind": "agent", "label": "claude"})
        await session.execute("second")
    finally:
        await session.kill()

    actors = [
        record["actor"]["kind"]
        for record in session.journal.records()
        if record["kind"] == "exec"
    ]
    assert actors == ["human", "agent"]


async def test_a_former_owner_may_still_close_what_it_handed_over(tmp_path):
    """Giving work away is not giving up the ability to stop it."""
    session = await make_session(tmp_path)
    await session.start()
    try:
        mine = session.write_key
        theirs = session.transfer_owner({"kind": "agent", "label": "claude"})

        assert session.held_by(mine), "the previous owner can still end it"
        assert session.held_by(theirs), "so can the new one"
        assert not session.held_by("some-other-key")
        assert not session.held_by(None)

        # But the old key is no longer the write key: typing is the new owner's.
        assert mine != session.write_key
        assert mine in session.former_keys
    finally:
        await session.kill()
