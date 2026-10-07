import asyncio
import time

import pytest

from odoo_sheller.journal import Journal, feed_from_records
from odoo_sheller.protocol import FRAME_LINE_LIMIT
from odoo_sheller.session import (
    STDERR_DRAIN,
    CommitForbidden,
    CommitNotAllowed,
    Session,
    SessionBusy,
    SessionDead,
    SessionNotReady,
    SessionState,
    parse_test_line,
    rerun_spec,
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


async def test_exec_returns_the_stderr_written_while_it_ran(tmp_path):
    """The agent should not have to build a log handler to see Odoo's log.

    The lines are already collected for run_test; exec must hand them back
    the same way, or a caller reasonably concludes there was no log at all.
    """
    session = await make_session(tmp_path, script=RUN_TEST_STDERR_FAKE)
    await session.start()
    result = await session.execute("anything")
    assert any("FAIL TestSaleOrder.test_x" in line for line in result["stderr"])
    assert result["stderr_truncated"] is False
    await session.close()


async def test_exec_stderr_is_empty_and_costs_nothing_when_odoo_said_nothing(tmp_path):
    """A quiet command must not pay the drain wait: exec is the hot path."""
    session = await make_session(tmp_path)
    await session.start()
    started = time.monotonic()
    result = await session.execute("1")
    elapsed = time.monotonic() - started
    assert result["stderr"] == []
    assert result["stderr_truncated"] is False
    assert elapsed < STDERR_DRAIN, "no lines arrived, so there is nothing to wait for"
    await session.close()


async def test_exec_stderr_excludes_lines_from_before_the_call(tmp_path):
    noisy = 'import sys\nsys.stderr.write("OLD noise\\n")\nsys.stderr.flush()\n' + RUN_TEST_STDERR_FAKE
    session = await make_session(tmp_path, script=noisy)
    await session.start()
    await asyncio.sleep(0.05)
    result = await session.execute("anything")
    assert not any("OLD noise" in line for line in result["stderr"])
    await session.close()


async def test_a_handover_remembers_whose_pending_work_it_carries(tmp_path):
    """The rule was documented and the agent promised to check it by hand.
    Making it a field is what stops the next one committing someone's work."""
    session = await make_session(tmp_path)
    await session.start()
    await session.execute("a")
    await session.execute("b")
    assert session.describe()["inherited_pending"] == 0, "nothing inherited yet"
    session.transfer_owner({"kind": "agent", "label": "claude"})
    described = session.describe()
    assert described["pending_commands"] == 2
    assert described["inherited_pending"] == 2, "both were the human's"
    # The new owner's own work is theirs, and the inherited count stays put.
    await session.execute("c")
    assert session.describe()["inherited_pending"] == 2
    await session.close()


async def test_a_transaction_boundary_clears_what_was_inherited(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    await session.execute("a")
    session.transfer_owner({"kind": "agent", "label": "claude"})
    assert session.describe()["inherited_pending"] == 1
    await session.rollback()
    assert session.describe()["inherited_pending"] == 0
    await session.execute("b")
    session.transfer_owner({"kind": "human", "label": "browser"})
    assert session.describe()["inherited_pending"] == 1
    await session.commit()
    assert session.describe()["inherited_pending"] == 0
    await session.close()


# -- live test progress --------------------------------------------------

LOGGER = "odoo.addons.sale.tests.test_sale_order"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # Odoo 19: the perf_info suffix trails the message.
        (f"2026-10-01 10:00:00,123 42 INFO db {LOGGER}: Starting TestSaleOrder.test_x ... - - -",
         ("start", "TestSaleOrder.test_x")),
        # Odoo 15: perf_info empty, so only a trailing space.
        (f"2026-10-01 10:00:00,123 42 INFO db {LOGGER}: Starting TestSaleOrder.test_x ... ",
         ("start", "TestSaleOrder.test_x")),
        (f"2026-10-01 10:00:00,123 42 ERROR db {LOGGER}: FAIL: TestSaleOrder.test_x",
         ("failure", "TestSaleOrder.test_x")),
        (f"2026-10-01 10:00:00,123 42 ERROR db {LOGGER}: ERROR: TestSaleOrder.test_y",
         ("error", "TestSaleOrder.test_y")),
        (f"2026-10-01 10:00:00,123 42 ERROR db {LOGGER}: FAIL: Subtest TestSaleOrder.test_x (n=2)",
         ("failure", "Subtest TestSaleOrder.test_x (n=2)")),
        (f"2026-10-01 10:00:00,123 42 INFO db {LOGGER}: skipped TestSaleOrder.test_z : no stock",
         ("skip", "TestSaleOrder.test_z")),
        # perf_info trails a one-line message in every shape Odoo writes it.
        (f"2026-10-01 10:00:00,123 42 ERROR db {LOGGER}: ERROR: TestSaleOrder.test_y - - -",
         ("error", "TestSaleOrder.test_y")),
        (f"2026-10-01 10:00:00,123 42 ERROR db {LOGGER}: FAIL: TestSaleOrder.test_x - - - -",
         ("failure", "TestSaleOrder.test_x")),
        (f"2026-10-01 10:00:00,123 42 ERROR db {LOGGER}: FAIL: TestSaleOrder.test_x 12 0.034 1.207",
         ("failure", "TestSaleOrder.test_x")),
        (f"2026-10-01 10:00:00,123 42 ERROR db {LOGGER}: FAIL: TestSaleOrder.test_x 12 0.034 1.207 ro->rw",
         ("failure", "TestSaleOrder.test_x")),
        (f"2026-10-01 10:00:00,123 42 INFO db {LOGGER}: Starting TestSaleOrder.test_x ... 3 0.001 0.020",
         ("start", "TestSaleOrder.test_x")),
        # A fixture that failed outside any test is logged by the suite's
        # error holder: odoo.tests.suite from 16, unittest.suite in 15.
        (("2026-10-01 10:00:00,123 42 ERROR db odoo.tests.suite: ERROR: setUpClass "
          "(odoo.addons.sale.tests.test_sale_order.TestBroken)"),
         ("error", "setUpClass (odoo.addons.sale.tests.test_sale_order.TestBroken)")),
        (("2026-10-01 10:00:00,123 42 ERROR db unittest.suite: ERROR: setUpModule "
          "(odoo.addons.sale.tests.test_sale_flow)"),
         ("error", "setUpModule (odoo.addons.sale.tests.test_sale_flow)")),
        (("2026-10-01 10:00:00,123 42 ERROR db odoo.tests.suite: FAIL: tearDownClass "
          "(odoo.addons.sale.tests.test_sale_order.TestBroken) - - -"),
         ("failure", "tearDownClass (odoo.addons.sale.tests.test_sale_order.TestBroken)")),
        (("2026-10-01 10:00:00,123 42 INFO db odoo.tests.suite: skipped setUpClass "
          "(odoo.addons.sale.tests.test_sale_order.TestOff) : no printer"),
         ("skip", "setUpClass (odoo.addons.sale.tests.test_sale_order.TestOff)")),
        # The suite logger names nothing else; anything off the fixture shape
        # stays out of the counts.
        ("2026-10-01 10:00:00,123 42 ERROR db odoo.tests.suite: ERROR: something else", None),
        # A traceback line under a FAIL, an unrelated logger, plain noise.
        ('  File "/opt/odoo/addons/sale/tests/test_sale_order.py", line 12, in test_x', None),
        ("2026-10-01 10:00:00,123 42 ERROR db odoo.sql_db: ERROR: relation does not exist", None),
        ("2026-10-01 10:00:00,123 42 INFO db odoo.modules.loading: Starting registry ... ", None),
        ("plain noise", None),
    ],
)
def test_parse_test_line(line, expected):
    assert parse_test_line(line) == expected


PROGRESS_FAKE = r"""
import json, sys, time
L = "2026-10-01 10:00:00,123 42 %s db odoo.addons.sale.tests.test_sale_order: %s - - -\n"
sys.stdout.write(json.dumps({"t":"hello","protocol":1,"odoo":"19.0","python":"3.12.0",
                             "db":"db","uid":1,"pid":4242}) + "\n")
sys.stdout.flush()
# Before any run: must not count.
sys.stderr.write(L % ("INFO", "Starting TestOld.test_old ..."))
sys.stderr.flush()
for line in sys.stdin:
    frame = json.loads(line)
    if frame["t"] == "close":
        sys.stdout.write(json.dumps({"t":"bye","id":frame["id"]}) + "\n")
        sys.stdout.flush()
        break
    if frame["t"] == "run_test":
        for message in (("INFO", "Starting TestSaleOrder.test_a ..."),
                        ("INFO", "Starting TestSaleOrder.test_b ..."),
                        ("ERROR", "FAIL: TestSaleOrder.test_b"),
                        ("INFO", "noise that is not a test"),
                        ("INFO", "skipped TestSaleOrder.test_c : why")):
            sys.stderr.write(L % message)
            sys.stderr.flush()
        time.sleep(0.4)
    sys.stdout.write(json.dumps({"t":"result","id":frame["id"],"stdout":"",
                                 "stdout_truncated":False,"result":None,
                                 "result_truncated":False,"error":None,"duration":0.01}) + "\n")
    sys.stdout.flush()
"""


async def test_describe_reports_live_test_progress(tmp_path):
    session = await make_session(tmp_path, script=PROGRESS_FAKE)
    await session.start()
    await asyncio.sleep(0.1)  # the pre-run line lands outside any run
    assert session.describe()["test_progress"] is None
    running = asyncio.create_task(session.run_test("sale", "TestSaleOrder"))
    await asyncio.sleep(0.25)
    progress = session.describe()["test_progress"]
    assert progress["spec"] == "sale.TestSaleOrder"
    assert progress["current"] == "TestSaleOrder.test_b"
    assert progress["started"] == 2
    assert progress["failures"] == 1
    assert progress["errors"] == 0
    assert progress["skipped"] == 1
    assert isinstance(progress["started_at"], float)
    await running
    assert session.describe()["test_progress"] is None
    await session.close()


async def test_spec_names_the_method_when_one_was_asked_for(tmp_path):
    session = await make_session(tmp_path, script=PROGRESS_FAKE)
    await session.start()
    running = asyncio.create_task(session.run_test("sale", "TestSaleOrder", "test_a"))
    await asyncio.sleep(0.25)
    assert session.describe()["test_progress"]["spec"] == "sale.TestSaleOrder.test_a"
    await running
    await session.close()


async def test_progress_events_fire_per_change_not_per_line(tmp_path):
    seen = []
    session = await make_session(tmp_path, script=PROGRESS_FAKE,
                                 on_event=lambda event: seen.append(event))
    await session.start()
    await session.run_test("sale", "TestSaleOrder")
    progress = [event for event in seen if event["kind"] == "test_progress"]
    # One when the run starts, one per matched line (4), one clearing it.
    assert len(progress) == 6
    assert progress[0]["progress"]["started"] == 0
    assert progress[-2]["progress"]["skipped"] == 1
    assert progress[-1]["progress"] is None
    assert all(event["session"] == "s1" for event in progress)
    await session.close()


async def test_exec_does_not_count_test_lines(tmp_path):
    seen = []
    session = await make_session(tmp_path, script=PROGRESS_FAKE,
                                 on_event=lambda event: seen.append(event))
    await session.start()
    await session.execute("1")
    assert not [event for event in seen if event["kind"] == "test_progress"]
    assert session.describe()["test_progress"] is None
    await session.close()


FAILED_FAKE = r"""
import json, sys, time
L = "2026-10-01 10:00:00,123 42 %s db odoo.addons.sale.tests.test_sale_order: %s\n"
# A fixture that fails outside any test is logged under the suite, not the
# test: odoo.tests.suite from 16, unittest.suite in 15.
S = "2026-10-01 10:00:00,123 42 %s db odoo.tests.suite: %s\n"
U = "2026-10-01 10:00:00,123 42 %s db unittest.suite: %s\n"
sys.stdout.write(json.dumps({"t":"hello","protocol":1,"odoo":"19.0","python":"3.12.0",
                             "db":"db","uid":1,"pid":4242}) + "\n")
sys.stdout.flush()
for line in sys.stdin:
    frame = json.loads(line)
    if frame["t"] == "close":
        sys.stdout.write(json.dumps({"t":"bye","id":frame["id"]}) + "\n")
        sys.stdout.flush()
        break
    for template, message in (
            (L, ("ERROR", "FAIL: TestSaleOrder.test_b")),
            (L, ("ERROR", "ERROR: TestSaleOrder.test_c")),
            (L, ("ERROR", "FAIL: Subtest TestSaleOrder.test_e (n=2)")),
            (L, ("ERROR", "FAIL: Subtest TestSaleOrder.test_e (n=3)")),
            (S, ("ERROR", "ERROR: setUpClass (odoo.addons.sale.tests.test_sale_order.TestBroken)")),
            (U, ("ERROR", "ERROR: setUpModule (odoo.addons.sale.tests.test_sale_flow)")),
            (L, ("ERROR", "ERROR: something Odoo calls a test"))):
        sys.stderr.write(template % message)
        sys.stderr.flush()
    time.sleep(0.3)
    sys.stdout.write(json.dumps({"t":"result","id":frame["id"],"stdout":"",
                                 "stdout_truncated":False,"result":None,
                                 "result_truncated":False,"error":None,"duration":0.01}) + "\n")
    sys.stdout.flush()
"""


async def test_a_run_names_what_failed_as_specs_to_rerun(tmp_path):
    """A whole module's log is thousands of lines; this is what the agent needs
    from it — each failure as something os_run_test takes back."""
    session = await make_session(tmp_path, script=FAILED_FAKE)
    await session.start()
    result = await session.run_test("sale")
    assert result["failed"] == [
        {"test": "sale.TestSaleOrder.test_b", "kind": "failure"},
        {"test": "sale.TestSaleOrder.test_c", "kind": "error"},
        # Both subtests are one method: rerunning it reruns both.
        {"test": "sale.TestSaleOrder.test_e", "kind": "failure"},
        # A class that could not even set up is rerun as the class — and
        # Odoo logs that one under the suite, not under the test.
        {"test": "sale.TestBroken", "kind": "error"},
        # A test file that could not set up: no tag names a file, so the
        # narrowest rerun that still holds it is the module.
        {"test": "sale", "kind": "error"},
        # Nothing to derive a spec from: say what Odoo said.
        {"test": "something Odoo calls a test", "kind": "error"},
    ]
    await session.close()


async def test_the_failed_list_is_journalled_with_the_result(tmp_path):
    session = await make_session(tmp_path, script=FAILED_FAKE)
    await session.start()
    await session.run_test("sale", "TestSaleOrder")
    results = [r for r in session.journal.records() if r["kind"] == "result"]
    assert results[-1]["failed"][0] == {"test": "sale.TestSaleOrder.test_b", "kind": "failure"}
    await session.close()


async def test_a_clean_run_reports_an_empty_failed_list(tmp_path):
    session = await make_session(tmp_path, script=PROGRESS_FAKE)
    await session.start()
    result = await session.run_test("sale", "TestSaleOrder")
    # PROGRESS_FAKE does log one FAIL, so use the plain fake for the clean case.
    assert result["failed"] == [{"test": "sale.TestSaleOrder.test_b", "kind": "failure"}]
    await session.close()
    clean = await make_session(tmp_path, script=FAKE)
    await clean.start()
    assert (await clean.run_test("sale"))["failed"] == []
    await clean.close()


async def test_a_module_run_describes_its_spec_as_the_module(tmp_path):
    session = await make_session(tmp_path, script=PROGRESS_FAKE)
    await session.start()
    running = asyncio.create_task(session.run_test("sale"))
    await asyncio.sleep(0.25)
    assert session.describe()["test_progress"]["spec"] == "sale"
    await running
    await session.close()


@pytest.mark.parametrize(
    ("description", "spec"),
    [
        ("TestSaleOrder.test_x", "sale.TestSaleOrder.test_x"),
        ("Subtest TestSaleOrder.test_x (n=2)", "sale.TestSaleOrder.test_x"),
        ("setUpClass (odoo.addons.sale.tests.test_sale_order.TestBroken)", "sale.TestBroken"),
        ("tearDownClass (odoo.addons.sale.tests.test_sale_order.TestBroken)", "sale.TestBroken"),
        # The last part of a module fixture is a file, which no tag can name:
        # `sale.test_sale_flow` would select a class that does not exist and
        # run nothing at all.
        ("setUpModule (odoo.addons.sale.tests.test_sale_flow)", "sale"),
        ("tearDownModule (odoo.addons.sale.tests.test_sale_flow)", "sale"),
        ("something Odoo calls a test", "something Odoo calls a test"),
    ],
)
def test_rerun_spec_names_what_a_tag_can_select(description, spec):
    assert rerun_spec("sale", description) == spec


async def test_a_remote_handover_round_trip_brings_back_no_grant(tmp_path):
    """Taking a remote session back used to come home able to commit: the
    handover set the right from the owner's kind alone, so human meant yes —
    on someone else's instance, where a human has to be granted it too."""
    for stage in ("staging", "production"):
        session = await make_session(tmp_path, target=oosh_target(stage))
        await session.start()
        try:
            assert session.allow_commit is False
            session.transfer_owner({"kind": "agent", "label": "claude"})
            assert session.allow_commit is False
            session.transfer_owner({"kind": "human", "label": "browser"})
            assert session.allow_commit is False, stage
            assert session.describe()["allow_commit"] is False, stage
            if stage == "staging":
                assert session._may_commit() is False
            else:
                with pytest.raises(CommitForbidden):
                    session._may_commit()
        finally:
            await session.kill()


async def test_a_local_handover_still_gives_a_human_their_commit_back(tmp_path):
    session = await make_session(tmp_path)
    await session.start()
    try:
        session.transfer_owner({"kind": "agent", "label": "claude"})
        assert session.allow_commit is False
        session.transfer_owner({"kind": "human", "label": "browser"})
        assert session.allow_commit is True
    finally:
        await session.kill()


async def test_keys_remember_who_they_were_issued_to(tmp_path):
    """Granting commit is a human's act. An agent holds a key `held_by`
    accepts too — its own — so that alone cannot be what decides."""
    session = await make_session(tmp_path)
    await session.start()
    try:
        human_key = session.write_key
        assert session.held_by_human(human_key)
        agent_key = session.transfer_owner({"kind": "agent", "label": "claude"})
        assert session.held_by(agent_key)
        assert not session.held_by_human(agent_key)
        assert session.held_by_human(human_key), "the one who handed it over"
        back = session.transfer_owner({"kind": "human", "label": "browser"})
        assert session.held_by_human(back)
        assert not session.held_by_human(agent_key)
        assert not session.held_by_human("a-key-from-nowhere")
        assert not session.held_by_human(None)
        assert session.key_status(back) == "owner"
        assert session.key_status(agent_key) == "former_owner"
        assert session.key_status("a-key-from-nowhere") == "invalid"
        assert session.key_status(None) == "invalid"
    finally:
        await session.kill()


async def test_a_session_that_dies_before_hello_says_why(tmp_path):
    """The cause is in what Odoo logged on the way out. "process ended" alone
    left an agent with nothing to act on."""
    script = (
        "import sys\n"
        "sys.stderr.write('psycopg2.OperationalError: database \"nope\" does not exist\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(1)\n"
    )
    session = await make_session(tmp_path, script=script)
    with pytest.raises(SessionDead) as excinfo:
        await session.start()
    message = str(excinfo.value)
    assert "before the session started" in message
    assert 'database "nope" does not exist' in message


async def test_closing_a_busy_session_never_passes_through_ready(tmp_path):
    """A close cut the running command short and briefly reported the session
    free: ready, then busy with `close`, then ready again, then closed — and
    the ready in between cleared a live test run's card."""
    seen = []
    session = await make_session(
        tmp_path,
        on_event=lambda event: seen.append(event) if event["kind"] == "state" else None,
    )
    await session.start()
    running = asyncio.create_task(session.execute("SLEEP"))
    await wait_for_state(session, SessionState.BUSY)
    await session.close()
    with pytest.raises(SessionDead):
        await running
    states = [(event["state"], event["activity"]) for event in seen]
    after_busy = states[states.index(("busy", "exec")) + 1:]
    assert after_busy == [("closed", None)], states
    assert session.state is SessionState.CLOSED


async def test_closing_an_idle_session_goes_straight_to_closed(tmp_path):
    seen = []
    session = await make_session(
        tmp_path,
        on_event=lambda event: seen.append(event) if event["kind"] == "state" else None,
    )
    await session.start()
    await session.close()
    states = [event["state"] for event in seen]
    assert states == ["ready", "busy", "closed"], states


async def test_an_exec_that_lands_after_its_timeout_still_counts_as_pending(
    tmp_path, monkeypatch
):
    """It ran to the end in the container and may have written: the next
    handover or test run has to know there is work in the transaction."""

    async def fake_signal(container, pid, name):
        pass

    monkeypatch.setattr("odoo_sheller.session.send_signal", fake_signal)
    session = await make_session(tmp_path, script=LATE_RESULT_FAKE)
    await session.start()
    try:
        with pytest.raises(TimeoutError):
            await session.execute("FIRST", timeout=0.05)
        assert session.pending_commands == 0
        await wait_for_state(session, SessionState.READY)
        assert session.pending_commands == 1
    finally:
        await session.kill()


SLOW_BOUNDARY_FAKE = r"""
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
    if frame["t"] in ("commit", "rollback"):
        time.sleep(0.2)
    sys.stdout.write(json.dumps({"t":"result","id":frame["id"],"stdout":"",
                                 "stdout_truncated":False,"result":None,
                                 "result_truncated":False,"error":None,
                                 "duration":0.01}) + "\n")
    sys.stdout.flush()
"""


async def test_a_commit_that_lands_after_its_timeout_is_journalled_as_one(
    tmp_path, monkeypatch
):
    """A commit that finally got its lock did commit. Without the record the
    journal read "discarded" over a write that happened, and the work it
    wrote still counted as pending."""

    async def fake_signal(container, pid, name):
        pass

    monkeypatch.setattr("odoo_sheller.session.send_signal", fake_signal)
    session = await make_session(tmp_path, script=SLOW_BOUNDARY_FAKE)
    await session.start()
    try:
        await session.execute("1")
        assert session.pending_commands == 1
        with pytest.raises(TimeoutError):
            await session.commit(timeout=0.05)
        await wait_for_state(session, SessionState.READY)
        assert session.pending_commands == 0
    finally:
        await session.kill()

    commits = [r for r in session.journal.records() if r["kind"] == "commit"]
    assert len(commits) == 1
    assert commits[0]["late"] is True
    assert commits[0]["error"] is None


async def test_a_read_only_exec_is_journalled_but_not_pending(tmp_path):
    """os_source reads through exec. Counting it as work made a handover warn
    about, and a test run report as discarded, a transaction holding nothing."""
    session = await make_session(tmp_path)
    await session.start()
    try:
        await session.execute("1", read_only=True)
        assert session.pending_commands == 0
        await session.execute("2")
        assert session.pending_commands == 1
    finally:
        await session.close()
    execs = [r for r in session.journal.records() if r["kind"] == "exec"]
    assert execs[0]["read_only"] is True
    assert "read_only" not in execs[1]


async def test_the_database_the_shell_opened_is_read_from_hello(tmp_path):
    """A server card may name no database: the shell takes it from the config.
    Hello says which one it was, and from then on the session, its journal and
    every screen that shows it say that rather than nothing."""
    target = Target(
        kind="ssh", label="acme", host="srv.example.com", stage="staging", database=None
    )
    session = await make_session(tmp_path, target=target)
    await session.start()
    assert session.describe()["database"] == "db"
    opened = next(r for r in session.journal.records() if r["kind"] == "session_open")
    assert opened["database"] == "db"
    await session.close()


async def test_a_database_the_card_named_is_not_replaced_by_hello(tmp_path):
    session = await make_session(
        tmp_path,
        target=Target(container="c", database="the-card-said-so", odoo_bin="/odoo-bin"),
    )
    await session.start()
    assert session.describe()["database"] == "the-card-said-so"
    await session.close()


async def test_describe_names_the_card_a_remote_session_was_opened_from(tmp_path):
    """The name on a card is free text and can equal a container's, so a screen
    cannot tell whose session this is by name. The card's id can be told apart —
    and survives a rename of the card."""
    target = Target(
        kind="ssh", label="acme", host="srv.example.com", stage="staging",
        database="db", card_id="ssh-1a2b3c4d",
    )
    session = await make_session(tmp_path, target=target)
    try:
        assert session.describe()["target_id"] == "ssh-1a2b3c4d"
    finally:
        await session.kill()

    local = await make_session(tmp_path)
    try:
        assert local.describe()["target_id"] is None
    finally:
        await local.kill()
