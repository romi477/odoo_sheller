"""The loop that runs inside the container.

Executed by odoo-bin shell's non-tty branch (shell.py:80-82) with a namespace
that already holds `env` and `self`. Stdlib only — this runs in the container's
interpreter and must never import from this project.
"""

import ast
import importlib
import io
import json
import os
import socket
import sys
import time
import traceback

_PT_PROTOCOL = 1
_OS_MAX_STDOUT = 1000000
_OS_MAX_RESULT = 100000


def _os_clip(text, limit):
    if len(text) <= limit:

        return text, False

    return text[:limit], True


def _os_error(exc, cell):
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename != cell:
        tb = tb.tb_next
    lines = traceback.format_exception(type(exc), exc, tb)

    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(lines),
    }


def _os_safe_repr(value):
    try:

        return repr(value)
    except Exception as exc:  # noqa: BLE001 - a broken __repr__ must not kill the session

        return f"<unrepresentable {type(value).__name__}: {exc}>"


def _os_run(namespace, code, cell):
    captured = io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = captured
    started = time.time()
    error = None
    value = None
    try:
        tree = ast.parse(code, filename=cell, mode="exec")
        tail = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            tail = ast.Expression(tree.body.pop().value)
            ast.fix_missing_locations(tail)
        exec(compile(tree, cell, "exec"), namespace)  # noqa: S102
        if tail is not None:
            value = eval(compile(tail, cell, "eval"), namespace)
    # BaseException on purpose: an interrupted command is an ordinary result
    # frame, not a reason to lose the session.
    except BaseException as exc:  # noqa: BLE001
        error = _os_error(exc, cell)
    finally:
        sys.stdout = saved_stdout
    duration = time.time() - started
    stdout, stdout_truncated = _os_clip(captured.getvalue(), _OS_MAX_STDOUT)
    result = None
    result_truncated = False
    if value is not None:
        result, result_truncated = _os_clip(_os_safe_repr(value), _OS_MAX_RESULT)

    return {
        "stdout": stdout,
        "stdout_truncated": stdout_truncated,
        "result": result,
        "result_truncated": result_truncated,
        "error": error,
        "duration": duration,
    }


def _os_test_tags(module, test_class, test_method):
    spec = f"*/{module}:{test_class}"
    if test_method:
        spec += f".{test_method}"

    return spec


def _os_free_port(interface):
    """A port nothing is listening on yet, for the test HTTP daemon to bind.

    The container's own Odoo is usually already on config['http_port'] (e.g.
    8069) — odoo.tests.shell.run_tests() spawns a second HTTP daemon in this
    process on that same configured port unconditionally, which fails with
    "Address already in use" and takes the whole session down with it.

    Probed on the interface `http_spawn` will actually bind, not on loopback:
    a port free on 127.0.0.1 can still be held on another interface, which
    would raise the very error this exists to avoid.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((interface, 0))

        return sock.getsockname()[1]
    finally:
        sock.close()


def _os_run_test(env, module, test_class, test_method):
    captured = io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = captured
    started = time.time()
    error = None
    test = None
    try:
        shell = importlib.import_module("odoo.tests.shell")
        server = importlib.import_module("odoo.service.server").server
        if server.httpd is None:
            # Only before the first spawn in this process: run_tests() itself
            # skips spawning a second time, and clobbering the port afterwards
            # would desync it from the daemon actually already listening.
            # `server.port` is what http_spawn() actually binds — it was set
            # from config['http_port'] back when this shell process started
            # (odoo/cli/shell.py calls server.start() before our loop ever
            # runs), so mutating the config dict alone here has no effect;
            # the server object's own attribute has to change too.
            config = importlib.import_module("odoo.tools").config
            free_port = _os_free_port(server.interface or config["http_interface"]
                                      or "0.0.0.0")
            server.port = free_port
            config["http_port"] = free_port
        test_tags = _os_test_tags(module, test_class, test_method)
        report = shell.run_tests(env, test_tags, modules=[module], reload_tests=True)
        if report is None:
            error = {
                "type": "TestRunnerRefused",
                "message": (
                    "odoo.tests.shell.run_tests refused to run: the container's "
                    "Odoo config must have workers=0 (threaded mode)"
                ),
                "traceback": "",
            }
        else:
            test = {
                "module": module,
                "test_class": test_class,
                "test_method": test_method,
                "tests_run": report.testsRun,
                "failures": report.failures_count,
                "errors": report.errors_count,
                "skipped": report.skipped,
                "success": report.wasSuccessful(),
            }
    # BaseException on purpose: an interrupted test run is an ordinary result
    # frame, not a reason to lose the session.
    except BaseException as exc:  # noqa: BLE001
        error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
    finally:
        sys.stdout = saved_stdout
    duration = time.time() - started
    stdout, stdout_truncated = _os_clip(captured.getvalue(), _OS_MAX_STDOUT)

    return {
        "stdout": stdout,
        "stdout_truncated": stdout_truncated,
        "result": None,
        "result_truncated": False,
        "error": error,
        "duration": duration,
        "test": test,
    }


def _os_commit(env):
    env.flush_all()
    env.cr.commit()
    env.invalidate_all(flush=False)


def _os_rollback(env):
    env.invalidate_all(flush=False)
    env.cr.rollback()


def _os_send(frames, frame):
    frames.write(json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n")
    frames.flush()


def _os_hello(namespace):
    env = namespace["env"]
    odoo = namespace.get("odoo")
    release = getattr(odoo, "release", None)

    return {
        "t": "hello",
        "protocol": _PT_PROTOCOL,
        "odoo": getattr(release, "version", "unknown"),
        "python": f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}",
        "db": env.cr.dbname,
        "uid": env.uid,
        "pid": os.getpid(),
    }


def _os_main(namespace):
    frames = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)  # anything else writing to fd 1 now lands in stderr
    commands = os.fdopen(int(os.environ.get("OS_CMD_FD", "3")), "r", encoding="utf-8")
    env = namespace["env"]

    _os_send(frames, _os_hello(namespace))

    while True:
        try:
            line = commands.readline()
        except KeyboardInterrupt:
            continue  # a signal between commands means nothing
        if not line:
            break
        text = line.strip()
        if not text:
            continue
        try:
            frame = json.loads(text)
        except ValueError:
            continue
        kind = frame.get("t")
        request_id = frame.get("id")
        if kind == "exec":
            answer = _os_run(namespace, frame.get("code", ""), f"<os-cell-{request_id}>")
            answer["t"] = "result"
            answer["id"] = request_id
            _os_send(frames, answer)
        elif kind == "run_test":
            answer = _os_run_test(
                env,
                frame.get("module", ""),
                frame.get("test_class", ""),
                frame.get("test_method"),
            )
            answer["t"] = "result"
            answer["id"] = request_id
            _os_send(frames, answer)
        elif kind in ("commit", "rollback"):
            started = time.time()
            error = None
            try:
                if kind == "commit":
                    _os_commit(env)
                else:
                    _os_rollback(env)
            # BaseException on purpose: a failed transaction boundary must be
            # reported, not fatal.
            except BaseException as exc:  # noqa: BLE001
                error = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                }
            _os_send(frames, {
                "t": "result",
                "id": request_id,
                "stdout": "",
                "stdout_truncated": False,
                "result": None,
                "result_truncated": False,
                "error": error,
                "duration": time.time() - started,
            })
        elif kind == "close":
            _os_send(frames, {"t": "bye", "id": request_id})
            break
        else:
            _os_send(frames, {
                "t": "result",
                "id": request_id,
                "stdout": "",
                "stdout_truncated": False,
                "result": None,
                "result_truncated": False,
                "error": {
                    "type": "UnknownFrame",
                    "message": f"unknown frame type {kind!r}",
                    "traceback": "",
                },
                "duration": 0.0,
            })


_os_main(globals())
