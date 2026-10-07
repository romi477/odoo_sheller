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
import signal
import socket
import sys
import time
import traceback

_PT_PROTOCOL = 1
_OS_MAX_STDOUT = 1000000
_OS_MAX_RESULT = 100000

# Whether a SIGINT may raise right now: only while code the caller asked for
# runs. Everywhere else — between commands, while a frame is being written —
# an interrupt would escape the loop and end the process, and with it the
# namespace and the open transaction: the opposite of what Interrupt
# promises. A one-item list so the handler and the loop share it without a
# `global`.
_OS_LIVE = [False]


def _os_on_sigint(signum, frame):
    if _OS_LIVE[0]:
        raise KeyboardInterrupt


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
    # BaseException: a broken __repr__ must not kill the session, and neither
    # may an interrupt that lands while a large value is being rendered — the
    # command itself has already run to the end by then.
    except BaseException as exc:  # noqa: BLE001

        return f"<unrepresentable {type(value).__name__}: {str(exc) or type(exc).__name__}>"


def _os_run(namespace, code, cell):
    captured = io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = captured
    started = time.time()
    error = None
    result = None
    result_truncated = False
    try:
        _OS_LIVE[0] = True
        tree = ast.parse(code, filename=cell, mode="exec")
        tail = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            tail = ast.Expression(tree.body.pop().value)
            ast.fix_missing_locations(tail)
        exec(compile(tree, cell, "exec"), namespace)  # noqa: S102
        if tail is not None:
            value = eval(compile(tail, cell, "eval"), namespace)
            if value is not None:
                # Still interruptible: a repr is the value's own code, and one
                # of a large recordset takes seconds — long enough to look
                # stuck and be interrupted. It used to run outside this span,
                # where the interrupt killed the session instead.
                result, result_truncated = _os_clip(_os_safe_repr(value), _OS_MAX_RESULT)
        _OS_LIVE[0] = False
    # BaseException on purpose: an interrupted command is an ordinary result
    # frame, not a reason to lose the session.
    except BaseException as exc:  # noqa: BLE001
        _OS_LIVE[0] = False
        error = _os_error(exc, cell)
    finally:
        sys.stdout = saved_stdout
    duration = time.time() - started
    stdout, stdout_truncated = _os_clip(captured.getvalue(), _OS_MAX_STDOUT)

    return {
        "stdout": stdout,
        "stdout_truncated": stdout_truncated,
        "result": result,
        "result_truncated": result_truncated,
        "error": error,
        "duration": duration,
    }


def _os_test_tags(module, test_class, test_method):
    if not test_class:
        # No tag means `standard` to Odoo's selector: what `--test-tags
        # /module` runs. `*` would also take tests tagged `-standard` or
        # `external`, which may call real third-party services.
        return f"/{module}"
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
    if not getattr(server, "httpd", None):
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


class TestRunnerUnsupported(Exception):
    """This Odoo's test runner is not one the bootstrap knows how to drive."""


def _os_run_test(env, module, test_class, test_method):
    captured = io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = captured
    started = time.time()
    error = None
    test = None
    try:
        _OS_LIVE[0] = True
        try:
            shell = importlib.import_module("odoo.tests.shell")
        except ImportError:
            # Odoo 16 and older: no shell runner to call, only the primitives
            # it was built from.
            shell = None
        if shell is None:
            try:
                importlib.import_module("odoo.tests.loader")
            except ImportError as exc:
                if getattr(exc, "name", None) != "odoo.tests.loader":
                    # No Odoo at all, or a module of it that is broken: that is
                    # a failure to report as it is, not a version to name.
                    raise
                # Odoo 13 keeps its runner in odoo.modules.module
                # (`run_unit_tests`) and has nothing to build a suite from.
                # Said here, before an HTTP daemon is spawned for a run that
                # cannot happen, rather than as an ImportError out of the
                # fallback.
                raise TestRunnerUnsupported(
                    "running tests is not supported on this Odoo: it has no "
                    "odoo.tests.loader (Odoo 13 keeps its test runner in "
                    "odoo.modules.module)"
                ) from None
        server = importlib.import_module("odoo.service.server").server
        # `getattr`, not `server.httpd`: 20 moved that attribute to
        # GeventServer only, and in shell mode this is a ThreadedServer.
        if getattr(server, "httpd", None) is None:
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
            if not hasattr(server, "httpd"):
                # `httpd` is Odoo's reference to the running HTTP daemon, not
                # a flag. Through 19 `ThreadedServer.http_spawn()` stored the
                # WSGI server there so `stop()` could shut it down. In 20 the
                # socket lives inside `http_server_thread` under a `with`, so
                # there is nothing left to hold and the attribute is gone from
                # that class — only `GeventServer` still has one.
                #
                # `odoo/tests/shell.py` was not updated with it and still does
                # `if not server.httpd`, so on a threaded server — which is
                # what shell mode runs — the runner dies of AttributeError
                # before a single test executes. That is an upstream bug; this
                # is the shim.
                #
                # Setting it to None is not enough: `http_spawn()` here never
                # assigns it back, so the guard would stay false and every
                # later run would start another daemon on the same port. So
                # spawn once and answer the guard's real question, "is one up
                # already", truthfully.
                #
                # Assigning a non-server value is safe because nothing ever
                # dereferences it on this class: in the whole 20 tree the only
                # reads are `tests/shell.py:31` (truthiness) and
                # `service/server.py:656-794`, all inside `GeventServer`. A
                # sentence rather than `True` so a traceback that ever prints
                # it says where it came from.
                #
                # Both ways this can age are harmless: restore the attribute
                # on `ThreadedServer` and `hasattr` sends us past this block;
                # fix `tests/shell.py` instead and we set something nobody
                # reads.
                server.http_spawn()
                server.httpd = "spawned by odoo-sheller"
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
        _OS_LIVE[0] = False
    # BaseException on purpose: an interrupted test run is an ordinary result
    # frame, not a reason to lose the session.
    except BaseException as exc:  # noqa: BLE001
        _OS_LIVE[0] = False
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


def _os_plain(request_id, error, duration):
    """A result frame that carries nothing but its outcome."""

    return {
        "t": "result",
        "id": request_id,
        "stdout": "",
        "stdout_truncated": False,
        "result": None,
        "result_truncated": False,
        "error": error,
        "duration": duration,
    }


def _os_boundary(env, kind, request_id):
    started = time.time()
    error = None
    try:
        # Interruptible: a commit waiting on a lock is exactly what someone
        # reaches for Interrupt to stop.
        _OS_LIVE[0] = True
        if kind == "commit":
            _os_commit(env)
        else:
            _os_rollback(env)
        _OS_LIVE[0] = False
    # BaseException on purpose: a failed transaction boundary must be
    # reported, not fatal.
    except BaseException as exc:  # noqa: BLE001
        _OS_LIVE[0] = False
        error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }

    return _os_plain(request_id, error, time.time() - started)


def _os_answer(namespace, env, frame):
    """The frame that answers one command. `bye` answers a close."""
    kind = frame.get("t")
    request_id = frame.get("id")
    if kind == "exec":
        answer = _os_run(namespace, frame.get("code", ""), f"<os-cell-{request_id}>")
    elif kind == "run_test":
        answer = _os_run_test(
            env,
            frame.get("module", ""),
            frame.get("test_class", ""),
            frame.get("test_method"),
        )
    elif kind in ("commit", "rollback"):

        return _os_boundary(env, kind, request_id)
    elif kind == "close":

        return {"t": "bye", "id": request_id}
    else:

        return _os_plain(request_id, {
            "type": "UnknownFrame",
            "message": f"unknown frame type {kind!r}",
            "traceback": "",
        }, 0.0)
    answer["t"] = "result"
    answer["id"] = request_id

    return answer


def _os_main(namespace):
    frames = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)  # anything else writing to fd 1 now lands in stderr
    commands = os.fdopen(int(os.environ.get("OS_CMD_FD", "3")), "r", encoding="utf-8")
    env = namespace["env"]

    # Odoo's shell makes every SIGINT a KeyboardInterrupt (shell.py:77). Ours
    # raises only while a command runs — see `_OS_LIVE`. Signal handlers can
    # only be set from the main thread, which is where odoo-bin shell runs
    # its console; anywhere else the interrupt keeps the meaning Odoo gave it.
    try:
        previous = signal.signal(signal.SIGINT, _os_on_sigint)
        installed = True
    except ValueError:
        previous = None
        installed = False

    try:
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
            if not isinstance(frame, dict):
                continue
            try:
                answer = _os_answer(namespace, env, frame)
            except KeyboardInterrupt:
                # Every command catches its own interrupt. This is the sliver
                # between a command's last line and the flag that closes its
                # span: still an interrupted command, never a lost session.
                _OS_LIVE[0] = False
                answer = _os_plain(frame.get("id"), {
                    "type": "KeyboardInterrupt",
                    "message": "",
                    "traceback": "",
                }, 0.0)
            # Never interruptible: half a frame on the pipe is a line the
            # daemon cannot read, and a session busy forever.
            _os_send(frames, answer)
            if answer.get("t") == "bye":
                break
    finally:
        if installed and previous is not None:
            signal.signal(signal.SIGINT, previous)


_os_main(globals())
