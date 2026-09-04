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


def _os_is_addon_test_module(name):
    r"""`^odoo\.addons\.\w+\.tests` without importing `re` in here."""
    parts = name.split(".")

    return len(parts) >= 4 and parts[0] == "odoo" and parts[1] == "addons" \
        and parts[3] == "tests"


def _os_run_suite(loader, suite, report):
    """`run_suite` gained `global_report` in 16; 15 only returns its result.

    Read off the function rather than guessed from a version: a TypeError from
    a wrong keyword and a TypeError from inside a test look the same, and the
    second must not be retried as if it were the first.
    """
    code = getattr(loader.run_suite, "__code__", None)
    names = getattr(code, "co_varnames", ()) if code is not None else ()
    if "global_report" in names:

        return loader.run_suite(suite, global_report=report)

    return loader.run_suite(suite)


def _os_test_counts(report):
    """15's result is unittest's: `failures`/`errors`/`skipped` are lists.

    16 added the `*_count` integers and made `skipped` one too. Prefer those
    when they are there; fall back to the length of the list.
    """
    def count(name):
        counter = getattr(report, name + "_count", None)
        if counter is not None:

            return counter
        value = getattr(report, name, 0)

        return len(value) if isinstance(value, list) else value

    return count("failures"), count("errors"), count("skipped")


def _os_run_tests_fallback(env, test_tags, modules):
    """What `odoo/tests/shell.py` does, for the versions that do not ship it.

    That file arrived in Odoo 17. Everything it is built from is older and
    unchanged — the tag DSL, `loader.make_suite`, `loader.run_suite`,
    `result.OdooTestResult`, `Registry._lock` — so this is its body against
    those same primitives rather than a test runner of our own. `run_suite`
    lost a positional argument after 16, hence the keyword.

    Returns None on the same refusal `run_tests` returns None on: a container
    running workers, where the test framework cannot work at all.
    """
    tools = importlib.import_module("odoo.tools")
    loader = importlib.import_module("odoo.tests.loader")
    try:
        results = importlib.import_module("odoo.tests.result")
    except ImportError:
        # 15 keeps OdooTestResult in odoo/tests/runner.py.
        results = importlib.import_module("odoo.tests.runner")
    registries = importlib.import_module("odoo.modules.registry")
    config = tools.config

    if config["workers"] != 0:

        return None

    server = importlib.import_module("odoo.service.server").server
    if not server.httpd:
        # Some tests need the http daemon; the port was already moved off the
        # container's own by the caller.
        server.http_spawn()

    try:
        ready = importlib.import_module("psycopg2.extensions").STATUS_READY
        if env.cr._cnx.status != ready:
            # A cursor holding a lock deadlocks the suite. Odoo's own runner
            # rolls back here too; the session reports it as discarded work.
            env.cr.rollback()
    except Exception:  # noqa: BLE001, S110 - the check is an optimisation
        pass

    for name in list(sys.modules):
        # reload_tests=True: an edited test file must be seen on the next run.
        if _os_is_addon_test_module(name):
            del sys.modules[name]

    config["test_tags"] = test_tags
    config["test_enable"] = True
    try:
        report = results.OdooTestResult()
        with registries.Registry._lock:
            registry = registries.Registry(env.cr.dbname)
            try:
                # Best effort to restore the test environment, as Odoo does.
                registry.loaded = False
                registry.ready = False
                suite = loader.make_suite(modules, "at_install")
                if suite.countTestCases():
                    report.update(_os_run_suite(loader, suite, report))
            finally:
                registry.loaded = True
                registry.ready = True
        suite = loader.make_suite(modules, "post_install")
        if suite.countTestCases():
            report.update(_os_run_suite(loader, suite, report))
    finally:
        # Process-wide state: a session goes on being used after a test run.
        config["test_enable"] = None
        config["test_tags"] = None

    return report


def _os_run_test(env, module, test_class, test_method):
    captured = io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = captured
    started = time.time()
    error = None
    test = None
    try:
        try:
            shell = importlib.import_module("odoo.tests.shell")
        except ImportError:
            # Odoo 16 and older: no shell runner to call, only the primitives
            # it was built from.
            shell = None
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
        if shell is None:
            report = _os_run_tests_fallback(env, test_tags, [module])
        else:
            report = shell.run_tests(env, test_tags, modules=[module], reload_tests=True)
        if report is None:
            error = {
                "type": "TestRunnerRefused",
                "message": (
                    "the test runner refused to run: the container's "
                    "Odoo config must have workers=0 (threaded mode)"
                ),
                "traceback": "",
            }
        else:
            failures, errors, skipped = _os_test_counts(report)
            test = {
                "module": module,
                "test_class": test_class,
                "test_method": test_method,
                "tests_run": report.testsRun,
                "failures": failures,
                "errors": errors,
                "skipped": skipped,
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


def _os_flush(env):
    """Write out pending computations. `flush_all` arrived in 16."""
    if hasattr(env, "flush_all"):
        env.flush_all()
    else:
        env["base"].flush()


def _os_invalidate(env):
    """Drop the caches *and* the pending writes, without writing them.

    `invalidate_all(flush=False)` arrived in 16; before that `Environment.clear`
    did the same three things — invalidate the cache, drop `tocompute` and drop
    `towrite`. Both must discard rather than flush: the default `flush=True`
    would write out exactly what a rollback is about to throw away.
    """
    if hasattr(env, "invalidate_all"):
        env.invalidate_all(flush=False)
    else:
        env.clear()


def _os_commit(env):
    _os_flush(env)
    env.cr.commit()
    _os_invalidate(env)


def _os_rollback(env):
    _os_invalidate(env)
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
