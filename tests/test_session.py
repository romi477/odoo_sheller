import asyncio

import pytest

from odoo_sheller.journal import Journal, feed_from_records
from odoo_sheller.protocol import FRAME_LINE_LIMIT
from odoo_sheller.session import (
    CommitForbidden,
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
    if frame["t"] == "run_test" and frame.get("test_class") == "SLEEP":
        time.sleep(3)
    if frame.get("code") == "DIE" or frame.get("test_class") == "DIE":
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
    if frame.get("code") == "FIRST" or frame.get("test_class") == "Slow":
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

RUN_TEST_STDERR_FAKE = r"""
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
    sys.stderr.write("ERROR: FAIL TestSaleOrder.test_x\n")
    sys.stderr.flush()
    time.sleep(0.1)
    sys.stdout.write(json.dumps({"t":"result","id":frame["id"],"stdout":"",
                                 "stdout_truncated":False,"result":None,
                                 "result_truncated":False,"error":None,"duration":0.01,
                                 "test":{"tests_run":1,"failures":1,"errors":0,
                                 "skipped":0,"success":False}}) + "\n")
    sys.stdout.flush()
"""

RUN_TEST_NOISY_FAKE = r"""
import json, sys, time
sys.stdout.write(json.dumps({"t":"hello","protocol":1,"odoo":"19.0","python":"3.12.0",
                             "db":"db","uid":1,"pid":4242}) + "\n")
sys.stdout.flush()
for i in range(2100):
    sys.stderr.write("BEFORE %d\n" % i)
sys.stderr.flush()
for line in sys.stdin:
    frame = json.loads(line)
    if frame["t"] == "close":
        sys.stdout.write(json.dumps({"t":"bye","id":frame["id"]}) + "\n")
        sys.stdout.flush()
        break
    for i in range(3000):
        sys.stderr.write("NOISE %d\n" % i)
    sys.stderr.flush()
    time.sleep(0.5)  # let the daemon drain stderr before the result lands
    sys.stdout.write(json.dumps({"t":"result","id":frame["id"],"stdout":"",
                                 "stdout_truncated":False,"result":None,
                                 "result_truncated":False,"error":None,"duration":0.01,
                                 "test":{"tests_run":62,"failures":0,"errors":0,
                                 "skipped":0,"success":True}}) + "\n")
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


def oosh_target(stage="staging"):

    return Target(kind="odoosh", build="36887345", host="build.dev.odoo.com",
                  database="ventor-dev-36887345", stage=stage)


async def make_session(tmp_path, script=FAKE, target=None, **kwargs):
    proc = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=FRAME_LINE_LIMIT,
    )

    return Session(
        "s1", target or TARGET, proc, Journal(tmp_path / "s1.jsonl"), **kwargs
    )


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


async def test_run_test_sends_the_frame_and_reports_no_discarded_work(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    result = await session.run_test("sale", "TestSaleOrder", "test_x")
    assert result["result"] == "42"  # the generic fake reply, proves it dispatched
    assert result["discarded_pending"] is False
    assert result["stderr"] == []
    await session.close()


async def test_run_test_flags_discarded_pending_and_clears_the_counter(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    await session.execute("a")
    assert session.pending_commands == 1
    result = await session.run_test("sale", "TestSaleOrder")
    assert result["discarded_pending"] is True
    assert session.pending_commands == 0
    await session.close()


async def test_run_test_captures_stderr_produced_during_the_run(tmp_path):
    session = await make_session(tmp_path, script=RUN_TEST_STDERR_FAKE)
    await session.start()
    result = await session.run_test("sale", "TestSaleOrder", "test_x")
    assert any("FAIL TestSaleOrder.test_x" in line for line in result["stderr"])
    assert result["test"]["success"] is False
    await session.close()


async def test_run_test_stderr_excludes_lines_from_before_the_call(tmp_path):
    noisy = 'import sys\nsys.stderr.write("OLD noise\\n")\nsys.stderr.flush()\n' + RUN_TEST_STDERR_FAKE
    session = await make_session(tmp_path, script=noisy)
    await session.start()
    await asyncio.sleep(0.05)  # let the old stderr line land before the snapshot
    result = await session.run_test("sale", "TestSaleOrder")
    assert not any("OLD noise" in line for line in result["stderr"])
    assert any("FAIL" in line for line in result["stderr"])
    await session.close()


async def test_run_test_stderr_survives_the_tail_deque_overflowing(tmp_path):
    """`_stderr` is capped at 2000 lines. A real test class logs far more than
    that, so an index into it is meaningless by the time the result lands."""
    session = await make_session(tmp_path, script=RUN_TEST_NOISY_FAKE)
    await session.start()
    # Wait for the pre-run noise to fill the tail deque to its ceiling.
    for _ in range(200):
        if len(session._stderr) >= 2000:
            break
        await asyncio.sleep(0.02)
    assert len(session._stderr) == 2000, "the deque must be full before the run"

    result = await session.run_test("qbo", "TestBig", timeout=10)

    lines = result["stderr"]
    assert len(lines) == 3000, "every line the run produced, not a slice of the tail"
    assert lines[0] == "NOISE 0", "the window starts where the run started"
    assert lines[-1] == "NOISE 2999"
    assert not any(line.startswith("BEFORE") for line in lines)
    assert result["stderr_truncated"] is False
    await session.close()


async def test_run_test_stderr_is_capped_and_says_so(tmp_path):
    """An unbounded per-run collector would be a memory hole on a long run."""
    session = await make_session(tmp_path, script=RUN_TEST_NOISY_FAKE)
    await session.start()
    for _ in range(200):
        if len(session._stderr) >= 2000:
            break
        await asyncio.sleep(0.02)

    from odoo_sheller import session as session_module

    original = session_module.RUN_STDERR_LIMIT
    session_module.RUN_STDERR_LIMIT = 500
    try:
        result = await session.run_test("qbo", "TestBig", timeout=10)
    finally:
        session_module.RUN_STDERR_LIMIT = original

    assert len(result["stderr"]) == 500
    assert result["stderr"][-1] == "NOISE 2999", "keep the tail, like every other cap here"
    assert result["stderr_truncated"] is True
    await session.close()


async def test_run_test_stops_collecting_stderr_once_it_answers(tmp_path):
    """A collector left registered would keep growing for the session's life."""
    session = await make_session(tmp_path, script=RUN_TEST_STDERR_FAKE)
    await session.start()
    await session.run_test("sale", "TestSaleOrder")
    assert session._stderr_collectors == []
    await session.close()


async def test_run_test_while_busy_is_rejected(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    running = asyncio.create_task(session.execute("SLEEP"))
    await asyncio.sleep(0.2)
    assert session.state is SessionState.BUSY
    with pytest.raises(SessionBusy):
        await session.run_test("sale", "TestSaleOrder")
    await running
    await session.close()


async def test_describe_names_a_running_test(tmp_path):
    """The UI cannot tell a test from exec unless describe() says so."""
    session = await make_session(tmp_path)
    await session.start()
    assert session.describe().get("activity") is None
    running = asyncio.create_task(session.run_test("sale", "SLEEP"))
    await asyncio.sleep(0.2)
    assert session.describe()["activity"] == "run_test"
    assert session.describe()["state"] == "busy"
    await running
    assert session.describe().get("activity") is None
    await session.close()


async def test_describe_names_a_running_exec(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    running = asyncio.create_task(session.execute("SLEEP"))
    await asyncio.sleep(0.2)
    assert session.describe()["activity"] == "exec"
    await running
    assert session.describe().get("activity") is None
    await session.close()


async def test_busy_state_events_carry_the_running_activity(tmp_path):
    seen = []
    session = await make_session(tmp_path, on_event=lambda event: seen.append(event))
    await session.start()
    running = asyncio.create_task(session.run_test("sale", "SLEEP"))
    await asyncio.sleep(0.2)
    busy = [event for event in seen if event.get("state") == "busy"]
    assert busy[-1]["activity"] == "run_test"
    await running
    ready = [event for event in seen if event.get("state") == "ready"]
    assert ready[-1].get("activity") is None
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

    async def fake_signal(target, pid, name):
        signals.append((target.name, pid, name))

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

    async def fake_signal(target, pid, name):
        signals.append((target.name, pid, name))

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


# --- autoclose (test sessions clean up after themselves) -----------------


def autoclose_events(seen):

    return [event for event in seen if event["kind"] == "autoclose"]


async def test_a_test_session_announces_it_is_finished(tmp_path):
    seen = []
    session = await make_session(
        tmp_path, autoclose=True, on_event=lambda event: seen.append(event)
    )
    await session.start()
    assert autoclose_events(seen) == [], "hello alone must not end the session"

    await session.run_test("sale", "TestSaleOrder")

    assert len(autoclose_events(seen)) == 1
    assert autoclose_events(seen)[0]["session"] == session.id
    await session.close()


async def test_autoclose_fires_after_the_result_reaches_the_journal(tmp_path):
    """Closing before the result is written would put session_close ahead of
    it in the transcript."""
    seen = []
    kinds_at_signal = []

    def watch(event):
        seen.append(event)
        if event["kind"] == "autoclose":
            kinds_at_signal.extend(r["kind"] for r in session.journal.records())

    session = await make_session(tmp_path, autoclose=True, on_event=watch)
    await session.start()
    await session.run_test("sale", "TestSaleOrder")

    assert "result" in kinds_at_signal, "the result must already be journalled"
    await session.close()


async def test_a_plain_session_never_announces_autoclose(tmp_path):
    seen = []
    session = await make_session(tmp_path, on_event=lambda event: seen.append(event))
    await session.start()
    await session.run_test("sale", "TestSaleOrder")
    await session.execute("1 + 1")
    assert autoclose_events(seen) == []
    await session.close()


async def test_a_test_session_that_never_ran_a_test_stays_open(tmp_path):
    """The flag says how it ends, not that it ends before doing its job."""
    seen = []
    session = await make_session(
        tmp_path, autoclose=True, on_event=lambda event: seen.append(event)
    )
    await session.start()
    await session.execute("1 + 1")
    assert autoclose_events(seen) == []
    await session.close()


async def test_a_timed_out_run_announces_only_when_the_result_lands(
    tmp_path, monkeypatch
):
    """A run that blew its ceiling is still going: closing then would kill it."""

    async def fake_signal(container, pid, name):
        pass

    monkeypatch.setattr("odoo_sheller.session.send_signal", fake_signal)
    seen = []
    session = await make_session(
        tmp_path,
        script=LATE_RESULT_FAKE,
        autoclose=True,
        on_event=lambda event: seen.append(event),
    )
    await session.start()
    with pytest.raises(TimeoutError):
        await session.run_test("sale", "Slow", timeout=0.05)
    assert autoclose_events(seen) == [], "the run still owns the container"

    await wait_for_state(session, SessionState.READY)
    assert len(autoclose_events(seen)) == 1
    kinds = [record["kind"] for record in session.journal.records()]
    assert "abandoned_result" in kinds
    await session.kill()


async def test_a_test_session_whose_process_died_is_announced_too(tmp_path):
    """Nothing is coming back; the registry entry should not linger either."""
    seen = []
    session = await make_session(
        tmp_path, autoclose=True, on_event=lambda event: seen.append(event)
    )
    await session.start()
    with pytest.raises(SessionDead):
        await session.run_test("sale", "DIE")
    assert session.state is SessionState.DEAD
    assert len(autoclose_events(seen)) == 1


async def test_autoclose_is_announced_once(tmp_path):
    seen = []
    session = await make_session(
        tmp_path, autoclose=True, on_event=lambda event: seen.append(event)
    )
    await session.start()
    await session.run_test("sale", "TestSaleOrder")
    await session.close()
    assert len(autoclose_events(seen)) == 1


# --- committing to a remote instance ------------------------------------


async def test_a_local_human_session_may_still_commit_at_once(tmp_path):
    """Unchanged: locally the human confirms in the UI instead."""
    session = await make_session(tmp_path)
    await session.start()
    try:
        assert session.allow_commit is True
    finally:
        await session.kill()


async def test_a_remote_session_starts_with_commit_off_even_for_a_human(tmp_path):
    """Being the owner is enough locally. On someone's instance it is not."""
    session = await make_session(tmp_path, target=oosh_target())
    await session.start()
    try:
        assert session.owner["kind"] == "human"
        assert session.allow_commit is False
        with pytest.raises(CommitNotAllowed):
            await session.commit()

        session.set_allow_commit(True)
        assert (await session.commit())["error"] is None
    finally:
        await session.kill()


async def test_production_refuses_a_commit_outright(tmp_path):
    """Not "awaiting a grant" — there is no grant to wait for."""
    session = await make_session(tmp_path, target=oosh_target("production"))
    await session.start()
    try:
        with pytest.raises(CommitForbidden):
            await session.commit()
    finally:
        await session.kill()


async def test_production_refuses_the_grant_itself(tmp_path):
    """A guard that can be granted around is not a guard."""
    session = await make_session(tmp_path, target=oosh_target("production"))
    await session.start()
    try:
        with pytest.raises(CommitForbidden):
            session.set_allow_commit(True)
        assert session.allow_commit is False
        with pytest.raises(CommitForbidden):
            await session.commit()
        assert not any(
            record["kind"] == "commit" for record in session.journal.records()
        ), "nothing may reach the pipe"
    finally:
        await session.kill()


async def test_rollback_on_production_is_untouched(tmp_path):
    """Reading a production instance is the legitimate case; only writing is not."""
    session = await make_session(tmp_path, target=oosh_target("production"))
    await session.start()
    try:
        assert (await session.execute("1 + 1"))["error"] is None
        assert (await session.rollback())["error"] is None
    finally:
        await session.kill()


async def test_a_remote_session_describes_where_it_is(tmp_path):
    session = await make_session(tmp_path, target=oosh_target("production"))
    await session.start()
    try:
        described = session.describe()
        assert described["kind"] == "odoosh"
        assert described["stage"] == "production"
        assert described["container"] == "36887345"
        assert described["host"] == "build.dev.odoo.com"
    finally:
        await session.kill()
