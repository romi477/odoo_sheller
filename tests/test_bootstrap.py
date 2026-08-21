import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "odoo_sheller" / "bootstrap.py"
HARNESS = ROOT / "tests" / "bootstrap_harness.py"

ALLOWED_IMPORTS = {"ast", "io", "json", "os", "sys", "time", "traceback"}


def run_bootstrap(frames, timeout=15):
    """Feed frames over stdin, return (out_frames, stderr_text, transaction_calls)."""
    stdin = "".join(json.dumps(frame) + "\n" for frame in frames)
    proc = subprocess.run(
        [sys.executable, str(HARNESS), str(BOOTSTRAP)],
        input=stdin,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
        env={"OS_CMD_FD": "0", "PATH": "/usr/bin:/bin"},
        cwd=ROOT,
    )
    out = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    calls = []
    for line in proc.stderr.splitlines():
        if line.startswith("PTLOG:"):
            calls = json.loads(line[len("PTLOG:"):])

    return out, proc.stderr, calls


def test_bootstrap_imports_stdlib_only():
    tree = ast.parse(BOOTSTRAP.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= ALLOWED_IMPORTS, f"unexpected imports: {names - ALLOWED_IMPORTS}"


def test_hello_comes_first():
    out, _, _ = run_bootstrap([{"t": "close", "id": 1}])
    assert out[0]["t"] == "hello"
    assert out[0]["protocol"] == 1
    assert out[0]["db"] == "testdb"
    assert out[0]["uid"] == 1
    assert isinstance(out[0]["pid"], int)
    assert out[-1] == {"t": "bye", "id": 1}


def test_stdout_is_captured_and_frames_stay_clean():
    out, _, _ = run_bootstrap([{"t": "exec", "id": 1, "code": "print('hello')"}])
    result = out[1]
    assert result["t"] == "result"
    assert result["stdout"] == "hello\n"
    assert result["result"] is None
    assert result["error"] is None
    assert result["duration"] >= 0


def test_last_expression_is_returned_as_repr():
    out, _, _ = run_bootstrap([{"t": "exec", "id": 1, "code": "x = 2\nx * 21"}])
    assert out[1]["result"] == "42"


def test_namespace_persists_between_commands():
    out, _, _ = run_bootstrap([
        {"t": "exec", "id": 1, "code": "counter = 1"},
        {"t": "exec", "id": 2, "code": "counter += 1\ncounter"},
    ])
    assert out[2]["result"] == "2"


def test_error_is_structured_and_traceback_holds_only_user_frames():
    out, _, _ = run_bootstrap([{"t": "exec", "id": 1, "code": "def f():\n    1 / 0\nf()"}])
    error = out[1]["error"]
    assert error["type"] == "ZeroDivisionError"
    assert "division by zero" in error["message"]
    assert "os-cell-1" in error["traceback"]
    assert "bootstrap.py" not in error["traceback"]


def test_syntax_error_is_reported_not_fatal():
    out, _, _ = run_bootstrap([
        {"t": "exec", "id": 1, "code": "def ("},
        {"t": "exec", "id": 2, "code": "'alive'"},
    ])
    assert out[1]["error"]["type"] == "SyntaxError"
    assert out[2]["result"] == "'alive'"


def test_commit_and_rollback_use_the_required_order():
    _, _, calls = run_bootstrap([{"t": "commit", "id": 1}, {"t": "rollback", "id": 2}])
    assert calls == [
        "flush_all",
        "cr.commit",
        "invalidate_all(flush=False)",
        "invalidate_all(flush=False)",
        "cr.rollback",
    ]


def test_unknown_frame_type_is_answered_not_fatal():
    out, _, _ = run_bootstrap([
        {"t": "sing", "id": 1},
        {"t": "exec", "id": 2, "code": "'alive'"},
    ])
    assert out[1]["error"]["type"] == "UnknownFrame"
    assert out[2]["result"] == "'alive'"


def test_large_output_is_truncated_with_a_flag():
    out, _, _ = run_bootstrap([{"t": "exec", "id": 1, "code": "print('x' * 2_000_000)"}])
    assert out[1]["stdout_truncated"] is True
    assert len(out[1]["stdout"]) <= 1_000_001


def test_bootstrap_clips_stay_inside_the_daemon_line_limit():
    from odoo_sheller.protocol import FRAME_LINE_LIMIT, MAX_RESULT, MAX_STDOUT

    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert f"_OS_MAX_STDOUT = {MAX_STDOUT}" in text
    assert f"_OS_MAX_RESULT = {MAX_RESULT}" in text
    assert MAX_STDOUT + MAX_RESULT < FRAME_LINE_LIMIT


def test_eof_on_the_command_channel_ends_the_loop():
    out, _, _ = run_bootstrap([])
    assert out[0]["t"] == "hello"
    assert all(frame["t"] != "result" for frame in out)


@pytest.mark.parametrize("code", ["print('a')", "'b'"])
def test_every_command_answers_exactly_once(code):
    out, _, _ = run_bootstrap([{"t": "exec", "id": 9, "code": code}])
    results = [frame for frame in out if frame["t"] == "result"]
    assert len(results) == 1
    assert results[0]["id"] == 9
