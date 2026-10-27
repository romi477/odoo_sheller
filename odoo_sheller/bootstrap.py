"""The loop that runs inside the container.

Executed by odoo-bin shell's non-tty branch (shell.py:80-82) with a namespace
that already holds `env` and `self`. Stdlib only — this runs in the container's
interpreter and must never import from this project.
"""

import ast
import copy
import importlib
import io
import json
import logging
import os
import signal
import socket
import sys
import time
import traceback
import unittest

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


def _os_test_tags(module, test_class, test_method, params=None):
    if not test_class:
        # No tag means `standard` to Odoo's selector: what `--test-tags
        # /module` runs. `*` would also take tests tagged `-standard` or
        # `external`, which may call real third-party services.
        spec = "/" + module
    else:
        spec = "*/" + module + ":" + test_class
        if test_method:
            spec += "." + test_method
    if params:
        # The selector hands what is in brackets to the test as
        # `_test_params` (16+); Hoot's suites read them as filters. The daemon
        # has already refused unpaired brackets and backslashes inside them.
        spec += "[" + params + "]"

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


def _os_run_suite(loader, suite, report, module_name=None):
    """`run_suite` gained `global_report` in 16; 15 only returns its result.

    Read off the function rather than guessed from a version: a TypeError from
    a wrong keyword and a TypeError from inside a test look the same, and the
    second must not be retried as if it were the first. 15 takes the module
    under test instead, and serves its translations to the browser by it.
    """
    code = getattr(loader.run_suite, "__code__", None)
    names = getattr(code, "co_varnames", ()) if code is not None else ()
    if "global_report" in names:

        return loader.run_suite(suite, global_report=report)
    if module_name is not None and "module_name" in names:

        return loader.run_suite(suite, module_name=module_name)

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


def _os_release_cursor(env):
    try:
        ready = importlib.import_module("psycopg2.extensions").STATUS_READY
        if env.cr._cnx.status != ready:
            # A cursor holding a lock deadlocks the suite. Odoo's own runner
            # rolls back here too; the session reports it as discarded work.
            env.cr.rollback()
    except Exception:  # noqa: BLE001, S110 - the check is an optimisation
        pass


_OS_RUNNER_REFUSED = (
    "the test runner refused to run: the container's Odoo config must have "
    "workers=0 (threaded mode)"
)


class TestRunnerRefused(Exception):
    """A container running workers, where Odoo's test framework cannot work."""


def _os_threaded_runner(env):
    """Odoo's suite runner, ready to run here: `(loader, results, config)`.

    None on the refusal Odoo's own `run_tests` makes, a container running
    workers — checked before anything is started. Otherwise this process's
    HTTP daemon is up, on a port of its own, and `env.cr` holds no lock.
    """
    config = importlib.import_module("odoo.tools").config
    if config["workers"] != 0:

        return None
    loader = importlib.import_module("odoo.tests.loader")
    try:
        results = importlib.import_module("odoo.tests.result")
    except ImportError:
        # 15 keeps OdooTestResult in odoo/tests/runner.py.
        results = importlib.import_module("odoo.tests.runner")
    server = _os_prepare_test_run(env)
    if not getattr(server, "httpd", None):
        # Some tests need the http daemon; its port was just moved off the
        # container's own.
        server.http_spawn()
    _os_release_cursor(env)

    return loader, results, config


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
    runner = _os_threaded_runner(env)
    if runner is None:

        return None
    loader, results, config = runner
    registries = importlib.import_module("odoo.modules.registry")

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


def _os_forget_asset_lists(env):
    """Drop this process's cached asset file lists before a run.

    Each bundle's files are globbed once per process and kept in the
    `assets` ormcache (`ir.asset._get_asset_paths`). A session that ran a
    browser suite, then gained a JS test file, would run the old list — and
    Hoot runs every test it has when a filter names one it lacks. In memory
    and in this process: other processes hear of it only if code here later
    calls `signal_changes()`, which is what os_help('tests') asks for anyway
    when the web process should see a new file. 17 to 19 name
    that cache; 20 keeps its caches per transaction and 16 and older have
    only `clear_caches()`, every cache at once, so those are left alone.
    """
    clear = getattr(env.registry, "clear_cache", None)
    if clear is None:
        return
    try:
        clear("assets")
    except (AssertionError, KeyError):
        # A registry that names its caches but not this one.
        pass


def _os_prepare_test_run(env):
    """What every test run needs from this process before its runner starts.

    The HTTP server's port moved off the container's own, Odoo 20's missing
    `httpd` supplied, the asset lists forgotten. Returns the server.
    """
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
    _os_forget_asset_lists(env)

    return server


def _os_run_test(env, module, test_class, test_method, params=None):
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
        test_tags = _os_test_tags(module, test_class, test_method, params)
        if shell is None:
            report = _os_run_tests_fallback(env, test_tags, [module])
        else:
            _os_prepare_test_run(env)
            report = shell.run_tests(env, test_tags, modules=[module], reload_tests=True)
        if report is None:
            error = {"type": "TestRunnerRefused", "message": _OS_RUNNER_REFUSED, "traceback": ""}
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
        error = _os_error_of(exc)
    finally:
        sys.stdout = saved_stdout

    return _os_run_frame(captured, started, error, test)


def _os_error_of(exc):
    """An exception as a result frame carries it. Called inside its `except`."""

    return {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}


def _os_run_frame(captured, started, error, test):
    """What a run answers: its stdout, its error, its time, its test counts."""
    stdout, stdout_truncated = _os_clip(captured.getvalue(), _OS_MAX_STDOUT)

    return {
        "stdout": stdout,
        "stdout_truncated": stdout_truncated,
        "result": None,
        "result_truncated": False,
        "error": error,
        "duration": time.time() - started,
        "test": test,
    }


# The clickbot over one module's menus. Odoo's clickbot walks whatever navbar
# the web client is given, and the web client is given `ir.ui.menu.load_menus`.
# These functions find the module's part of that tree and cut the rest away,
# on a copy: what `load_menus` returns is its ormcache entry.

def _os_click_action_model(entry):
    """The model of a menu's action, or None.

    15 to 18 read the reference field (`"ir.actions.act_window,7"`); 19 and
    20 hand out `action_model` and `action_id` instead.
    """
    if "action_model" in entry:

        return entry["action_model"] or None
    action = entry.get("action")
    if not action:

        return None

    return action.split(",")[0]


def _os_click_tree(menus, own, root_is_target):
    """The module's targets in `menus` and the way in to each.

    A target is a menu of the module with an action and no visible children:
    nothing else is ever clicked. The module's own app root is one too where
    the clickbot clicks roots before their menus (`root_is_target`). A URL
    action would take the page away from the clickbot, so it is set aside, and
    so is a menu under an app root with no xmlid (one made in the UI): the
    clickbot starts an app by its xmlid, and an empty one starts every app.
    """
    parents = {}
    for key, entry in menus.items():
        for child in entry.get("children") or ():
            parents[child] = key
    order = []
    seen = set()
    pending = list(reversed(menus["root"].get("children") or []))
    while pending:
        menu_id = pending.pop()
        if menu_id not in menus or menu_id in seen:
            continue
        seen.add(menu_id)
        order.append(menu_id)
        pending.extend(reversed(menus[menu_id].get("children") or []))
    present = set()
    targets = []
    unreached = []
    for menu_id in order:
        entry = menus[menu_id]
        xmlid = entry.get("xmlid") or ""
        present.add(xmlid)
        if xmlid not in own:
            continue
        model = _os_click_action_model(entry)
        children = [child for child in entry.get("children") or () if child in menus]
        is_root = parents.get(menu_id) == "root"
        if not model or children and not (is_root and root_is_target):
            continue
        root = menu_id
        while parents.get(root, "root") != "root":
            root = parents[root]
        if model == "ir.actions.act_url" or not menus[root].get("xmlid"):
            unreached.append(xmlid)
            continue
        targets.append(menu_id)
    tree = {
        "parents": parents,
        "targets": targets,
        "hidden": sorted(xmlid for xmlid in own if xmlid not in present),
        "unreached_static": unreached,
    }
    tree["allowed"] = _os_click_shrink(tree, set())
    tree["roots"] = [menu_id for menu_id in menus["root"].get("children") or ()
                     if menu_id in tree["allowed"]]
    tree["root_action"] = {
        menu_id: _os_click_action_model(menus[menu_id]) is not None
        for menu_id in tree["roots"]
    }

    return tree


def _os_click_shrink(tree, done):
    """The menus to show once `done` are clicked: what is left, and its way in."""
    allowed = set()
    parents = tree["parents"]
    for menu_id in tree["targets"]:
        if menu_id in done:
            continue
        while menu_id != "root" and menu_id not in allowed and menu_id is not None:
            allowed.add(menu_id)
            menu_id = parents.get(menu_id)

    return allowed


def _os_click_trim(menus, allowed):
    """A copy of `menus` holding only `allowed`. `menus` itself is not touched."""
    trimmed = {}
    for key, entry in menus.items():
        if key != "root" and key not in allowed:
            continue
        kept = copy.deepcopy(entry)
        kept["children"] = [child for child in entry.get("children") or () if child in allowed]
        trimmed[key] = kept

    return trimmed



def _os_click_line(message):
    """What one line of the clickbot's console says, or None.

    15 to 19 log `Testing menu <name> <xmlid>`, `Testing app menu: <xmlid>`,
    `Error while testing <name> <xmlid>`. 20 logs `Testing menu <name>
    (<xmlid>)`, `Testing app: <name> (<xmlid>)` and one `Error found:` block
    per failing menu naming it. The counts are read off each event — `Testing
    N filters`, `Testing view switch: <type>`, `Modal detected: …` — and not
    off the totals at the end, which only a run that got there prints: a root
    restarted past a failure would lose what its failed runs clicked.
    """
    lines = (message or "").splitlines()
    if not lines:

        return None
    first = lines[0].strip()
    if first == "Error found:":
        marker = " - Current testing menu is "
        for line in lines[1:]:
            if line.startswith(marker) and line.rstrip().endswith(")"):

                return ("error", line.rstrip()[:-1].rsplit("(", 1)[-1])

        return ("error", None)
    for prefix, kind in (("Testing app menu: ", "menu"), ("Testing app: ", "menu"),
                         ("Testing menu ", "menu"), ("Error while testing ", "error")):
        if first.startswith(prefix):
            rest = first[len(prefix):].strip()
            if rest.endswith(")") and "(" in rest:

                return (kind, rest[:-1].rsplit("(", 1)[-1])
            words = rest.split()

            return (kind, words[-1]) if words else None
    if first.startswith("Testing view switch: "):

        return ("count", "views", 1)
    if first.startswith("Modal detected: "):

        return ("count", "modals", 1)
    words = first.split()
    if len(words) == 3 and words[0] == "Testing" and words[1].isdigit() and words[2] == "filters":

        return ("count", "filters", int(words[1]))

    return None


def _os_click_root_of(tree, menu_id):
    parents = tree["parents"]
    while parents.get(menu_id, "root") != "root":
        menu_id = parents[menu_id]

    return menu_id


def _os_click_blame(logged, root_id, tree, menus, done):
    """The target a failed browser_js is blamed on, or None to end the root.

    The last menu the clickbot named is the one it was in. A foreign app root
    with no action of its own runs its first target's action when clicked, so
    that target is the one at fault. A root with an action owns its failure.
    """
    ids = {entry.get("xmlid"): key for key, entry in menus.items() if key != "root"}
    last = None
    for xmlid in reversed(logged):
        if xmlid in ids:
            last = ids[xmlid]
            break
    if last is None:

        return None
    if last in tree["targets"] and last not in done:

        return last
    if last == root_id and not tree["root_action"].get(root_id):
        for menu_id in tree["targets"]:
            if menu_id not in done and _os_click_root_of(tree, menu_id) == root_id:

                return menu_id

    return None


def _os_click_budget(deadline, now):
    """Seconds left for the next browser_js; None once there is no real time."""
    left = deadline - now

    return left if left > 1 else None



def _os_click_before(cls, name="load_menus"):
    """Whether `cls` has `name` of its own, and which, before a run."""
    own = name in cls.__dict__

    return (name, own, cls.__dict__[name] if own else None)


def _os_click_undo(cls, before):
    """Put `cls` back as `before` found it.

    `load_menus` is inherited on a registry class: it is not in the class's
    own `__dict__`, and setting the original back would leave an attribute
    that was never there. So it is deleted, as `mock.patch.object` does.
    """
    name, own, original = before
    if not own:
        if name in cls.__dict__:
            delattr(cls, name)
    elif cls.__dict__.get(name) is not original:
        setattr(cls, name, original)


def _os_click_is_undone(cls, before):
    name, own, original = before
    if not own:

        return name not in cls.__dict__

    return cls.__dict__.get(name) is original


def _os_click_wrapper(original, holder):
    """`load_menus` answering only `holder["allowed"]`, read at every call."""
    def load_menus(self, debug):

        return _os_click_trim(original(self, debug), holder["allowed"])

    return load_menus



def _os_click_text(node):
    """The text of a string literal node, on every `ast` this may run under."""
    if type(node).__name__ in ("Str", "Constant"):
        # `value` from 3.8, `s` before it (and deprecated after).
        value = node.value if hasattr(node, "value") else node.s
        if isinstance(value, str):

            return value

    return None


def _os_click_flavour(loader_js, test_py):
    """How this Odoo starts its clickbot, read off its own test and loader.

    Odoo's `test_click_everywhere.py` holds the call per app: the URL, the
    line that starts the clickbot (`%s` where the app's xmlid goes) and the
    success signal, if not the default one. 15 and 16 keep one browser for
    the class and stop it after each app. 20's loader builds a
    `ClickbotLauncher`: that clickbot goes on past a failing menu, logs an
    `Error found:` block for each, and clicks an app root only when it has no
    menus under it.
    """
    try:
        nodes = list(ast.walk(ast.parse(test_py)))
    except SyntaxError:
        nodes = []
    call = None
    own_browser = False
    for node in nodes:
        if type(node).__name__ != "Call":
            continue
        name = getattr(node.func, "attr", None)
        if name == "terminate_browser":
            own_browser = True
        if name != "browser_js" or call is not None or len(node.args) < 2:
            continue
        start = node.args[1]
        if type(start).__name__ == "BinOp" and "%s" in (_os_click_text(start.left) or ""):
            call = node
    if call is None or "startClickEverywhere" not in loader_js:
        raise ValueError("unknown clickbot: no per-app browser_js call in "
                         "test_click_everywhere.py, or no startClickEverywhere")
    url = _os_click_text(call.args[0])
    start = _os_click_text(call.args[1].left)
    signal_text = None
    for keyword in call.keywords:
        if keyword.arg == "success_signal":
            signal_text = _os_click_text(keyword.value)
    if url is None:
        raise ValueError("unknown clickbot: the browser_js URL is not a literal")
    launcher = "ClickbotLauncher" in loader_js

    return {
        "start": start,
        "url": url,
        "signal": signal_text,
        "options": "startClickEverywhere({" in start,
        "error_checker": launcher,
        "own_browser": own_browser,
        "root_is_target": not launcher,
    }


def _os_click_sources(odoo):
    """The text of the two files `_os_click_flavour` reads, from `web` itself."""
    web = odoo.modules.module.get_module_path("web")
    texts = []
    for parts in (("static", "src", "webclient", "clickbot", "clickbot_loader.js"),
                  ("tests", "test_click_everywhere.py")):
        with open(os.path.join(web, *parts), encoding="utf-8") as handle:
            texts.append(handle.read())

    return texts[0], texts[1]



_OS_CLICK_REASON = 800
_OS_CLICK_START = "odoo.isReady === true"


def _os_click_accept(self, password, env):
    """`_check_credentials` inside `authenticate` on 15 to 17, as 18 patches it."""


def _os_click_authenticate(original, users_cls):
    """`authenticate` with the password check off for its own call only.

    That is the whole of what 18 and later do, with a `with patch(...)` around
    the one call. Off for the whole run instead, the test's HTTP server —
    listening on every interface — would let anyone log in as anyone while the
    clickbot clicks. A `finally`, so not even an interrupt leaves it off.
    """
    def authenticate(*args, **kwargs):
        before = _os_click_before(users_cls, "_check_credentials")
        users_cls._check_credentials = _os_click_accept
        try:

            return original(*args, **kwargs)
        finally:
            _os_click_undo(users_cls, before)

    return authenticate


def _os_click_needs_credentials(http_case):
    """Whether `authenticate` checks a real password: 15 to 17 do.

    18 and later patch `_check_credentials` inside it with a function named
    `patched_check_credentials`, which is a code constant of `authenticate`.
    """
    code = getattr(getattr(http_case, "authenticate", None), "__code__", None)
    for const in getattr(code, "co_consts", ()):
        if getattr(const, "co_name", None) == "patched_check_credentials":

            return False

    return True


def _os_click_plain(message):
    """A console line without the HTML an error dialog logs itself as."""
    out = []
    depth = 0
    for char in message:
        if char == "<":
            depth += 1
        elif char == ">" and depth:
            depth -= 1
            out.append(" ")
        elif not depth:
            out.append(char)

    return " ".join("".join(out).split())


class _OsClickHandler(logging.Handler):
    """Hears the browser's console and the server's errors while the clickbot runs.

    The test must know which menu a failed `browser_js` was in before it
    starts the next one; the session reads stderr only after the run. On the
    root logger, so the error behind a dialog — logged by the server, not the
    browser — can be the reason too. Only `odoo.http`'s: that is where every
    version logs a request it failed (`Exception during request handling`),
    and any other thread's error has nothing to do with the menu.
    """

    def __init__(self, state):
        logging.Handler.__init__(self)
        self.state = state

    def emit(self, record):
        run = self.state["run"]
        if not record.name.endswith(".browser"):
            if record.name != "odoo.http":

                return
            exc = record.exc_info[1] if record.exc_info else None
            if record.levelno >= logging.ERROR and exc is not None and run["server"] is None:
                text = str(exc).strip().splitlines()
                run["server"] = f"{type(exc).__name__}: {text[0] if text else ''}"
            return
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a record that will not format says nothing
            return
        late = "Error received after termination: "
        if message.startswith(late):
            # The dialog the clickbot saw comes after the line that ended the
            # run; it is the reason, not a menu.
            if record.levelno >= logging.ERROR:
                run["browser"].append(message[len(late):])
            return
        if record.levelno >= logging.ERROR:
            run["browser"].append(message)
        line = _os_click_line(message)
        if line is None:
            return
        if line[0] == "menu":
            run["logged"].append(line[1])
        elif line[0] == "error":
            run["errors"].append((line[1], message))
        else:
            counts = self.state["clickbot"]
            counts[line[1]] = counts.get(line[1], 0) + line[2]


def _os_click_reason(run, fallback):
    """The server's error behind a failure, then what the browser said of it."""
    said = [line for line in run["browser"] if not line.startswith("Error while testing ")]
    first = (said or run["browser"] or [fallback])[0]
    # A console error carries its JS stack below the message.
    browser = _os_click_plain((first.splitlines() or [""])[0])
    parts = [part for part in (run["server"], browser) if part]

    return _os_clip("; ".join(parts), _OS_CLICK_REASON)[0]


def _os_click_lookup(env, module, login):
    """(refusal, user, own xmlids) before anything is started."""
    found = env["ir.module.module"].sudo().search([("name", "=", module)], limit=1)
    if not found:

        return "unknown_module", None, None
    if found.state != "installed":

        return "module_not_installed", None, None
    if login is None:
        admin = env.ref("base.user_admin", raise_if_not_found=False)
        login = admin.login if admin else None
    user = env["res.users"].sudo().search([("login", "=", login)], limit=1) if login else None
    if not user:

        return "unknown_login", None, None
    rows = env["ir.model.data"].sudo().search_read(
        [("model", "=", "ir.ui.menu"), ("module", "=", module)], ["name", "res_id"])
    # A row can outlive its menu; it is no menu of the module's.
    existing = set(env["ir.ui.menu"].sudo().with_context(active_test=False).browse(
        [row["res_id"] for row in rows]).exists().ids)
    own = {module + "." + row["name"] for row in rows if row["res_id"] in existing}
    if not own:

        return "no_menus", user, None

    return None, user, own


def _os_click_new_run():

    return {"logged": [], "errors": [], "browser": [], "server": None}


def _os_click_root(case, state, root_id, entry):
    """Every browser_js one app root takes; restarts past a failure on 15 to 19."""
    tree = state["tree"]
    menus = state["menus"]
    flavour = state["flavour"]
    done = state["done"]
    ids = {menus[key].get("xmlid"): key for key in tree["targets"]}
    extra = {}
    if flavour["signal"]:
        extra["success_signal"] = flavour["signal"]
    if flavour["error_checker"]:
        # 20's clickbot logs one `Error found:` block per failing menu and goes
        # on; browser_js would stop at the first. Every other error line ends
        # the run, and browser_js fails the test on all of them at its end.
        extra["error_checker"] = lambda message: not message.startswith("Error found:")
    url = flavour["url"] + ("?debug=1" if state["debug"] else "")
    start = flavour["start"] % entry["xmlid"]
    failed_before = len(state["failed"])
    while True:
        left = [menu_id for menu_id in tree["targets"]
                if menu_id not in done and _os_click_root_of(tree, menu_id) == root_id]
        if not left:
            break
        budget = _os_click_budget(state["deadline"], time.time())
        if budget is None:
            entry["error"] = "timeout"
            break
        run = _os_click_new_run()
        state["run"] = run
        text = None
        try:
            case.browser_js(url, start, _OS_CLICK_START, login=state["login"],
                            timeout=budget, **extra)
        except AssertionError as exc:
            text = str(exc) or type(exc).__name__
        except unittest.SkipTest:
            # No browser at all: the run's counts say so, not a menu.
            raise
        except Exception as exc:  # noqa: BLE001 - Chrome gone, a devtools call timed out
            text = f"{type(exc).__name__}: {exc}"
        finally:
            if flavour["own_browser"]:
                case.terminate_browser()
        for xmlid in run["logged"]:
            if xmlid in state["own"] and xmlid not in state["clicked"]:
                state["clicked"].append(xmlid)
        logged = {ids[xmlid] for xmlid in run["logged"] if xmlid in ids}
        if text is None:
            done.update(logged)
            break
        reason = _os_click_reason(run, text)
        if _os_click_budget(state["deadline"], time.time()) is None:
            done.update(logged)
            entry["error"] = "timeout"
            break
        if flavour["error_checker"]:
            done.update(logged)
            for xmlid, message in run["errors"]:
                if xmlid:
                    state["failed"].append({"xmlid": xmlid, "root": entry["xmlid"],
                                            "reason": _os_clip(message, _OS_CLICK_REASON)[0]})
            if len(state["failed"]) == failed_before:
                entry["error"] = reason
            break
        culprit = _os_click_blame(run["logged"], root_id, tree, menus, done)
        done.update(logged)
        if culprit is None:
            entry["error"] = reason
            break
        done.add(culprit)
        state["failed"].append({"xmlid": menus[culprit]["xmlid"], "root": entry["xmlid"],
                                "reason": reason})
        if culprit == root_id:
            # The module's own app root fails as it is clicked, and every run
            # clicks it first: what is under it cannot be reached.
            break
        state["holder"]["allowed"] = _os_click_shrink(tree, done)
    entry["success"] = entry["error"] is None and len(state["failed"]) == failed_before
    if not entry["success"]:
        named = entry["error"] or ", ".join(
            failure["xmlid"] for failure in state["failed"][failed_before:])
        case.fail(f"clickbot failed under {entry['xmlid']}: {named}")


def _os_click_drive(case, state):
    """The body of the one test method."""
    env = case.env
    menu_cls = type(env["ir.ui.menu"])
    # Undone by the test's own cleanups; the bootstrap checks it after.
    case.patch(menu_cls, "load_menus", _os_click_wrapper(menu_cls.load_menus, state["holder"]))
    if state["patch_credentials"]:
        # On the test only: nothing to undo on any class.
        case.authenticate = _os_click_authenticate(case.authenticate, type(env["res.users"]))
    user = env["res.users"].browse(state["uid"])
    if "tour_enabled" in user._fields:
        user.tour_enabled = False
    logger = logging.getLogger(type(case).__module__)
    level = logger.level
    handler = _OsClickHandler(state)
    # A container logging above INFO would drop the console lines before
    # any handler saw them.
    logger.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
    try:
        for root_id in state["tree"]["roots"]:
            entry = {"xmlid": state["menus"][root_id].get("xmlid"), "success": False,
                     "error": None}
            state["clickbot"]["roots"].append(entry)
            with case.subTest(root=entry["xmlid"]):
                _os_click_root(case, state, root_id, entry)
    finally:
        logging.getLogger().removeHandler(handler)
        logger.setLevel(level)


def _os_click_case(http_case, module, state):
    def test_click_module(self):
        _os_click_drive(self, state)

    attrs = {
        # BaseCase reads its module and tags off a name under odoo.addons, and
        # HttpCase names its loggers after it.
        "__module__": f"odoo.addons.{module}.tests.os_click_module",
        "test_click_module": test_click_module,
    }

    return type(http_case)("OsClickModule", (http_case,), attrs)


def _os_click_module(env, module, login, debug, timeout=900.0):
    captured = io.StringIO()
    saved_stdout = sys.stdout
    sys.stdout = captured
    started = time.time()
    error = None
    test = None
    clickbot = {
        "module": module, "login": login, "debug": bool(debug), "roots": [], "clicked": [],
        "failed": [], "unreached": [], "hidden": [], "refused": None, "restored": True,
    }
    try:
        _OS_LIVE[0] = True
        refused, user, own = _os_click_lookup(env, module, login)
        if user:
            clickbot["login"] = user.login
        tree = None
        menus = None
        flavour = None
        if refused is None:
            odoo = importlib.import_module("odoo")
            importlib.import_module("odoo.modules.module")
            flavour = _os_click_flavour(*_os_click_sources(odoo))
            menus = env["ir.ui.menu"].with_user(user).load_menus("1" if debug else "")
            tree = _os_click_tree(menus, own, flavour["root_is_target"])
            clickbot["hidden"] = tree["hidden"]
            if not tree["targets"]:
                refused = "no_visible_menus"
        clickbot["refused"] = refused
        if refused is None:
            test = _os_click_run(env, module, clickbot, {
                "tree": tree, "menus": menus, "flavour": flavour, "own": own,
                "uid": user.id, "login": user.login, "debug": bool(debug),
                "deadline": started + timeout - min(60.0, timeout * 0.1),
                "holder": {"allowed": tree["allowed"]}, "done": set(),
                "failed": clickbot["failed"], "clicked": clickbot["clicked"],
                "clickbot": clickbot, "run": _os_click_new_run(),
            })
            failed = {failure["xmlid"] for failure in clickbot["failed"]}
            clickbot["unreached"] = [
                menus[menu_id]["xmlid"] for menu_id in tree["targets"]
                if menus[menu_id]["xmlid"] not in clickbot["clicked"]
                and menus[menu_id]["xmlid"] not in failed
            ] + tree["unreached_static"]
        _OS_LIVE[0] = False
    # BaseException on purpose: an interrupted run is an ordinary result.
    except BaseException as exc:  # noqa: BLE001
        _OS_LIVE[0] = False
        error = _os_error_of(exc)
    finally:
        sys.stdout = saved_stdout
    frame = _os_run_frame(captured, started, error, test)
    frame["clickbot"] = clickbot

    return frame


def _os_click_run(env, module, clickbot, state):
    """One HttpCase of one test through Odoo's own suite runner."""
    common = importlib.import_module("odoo.tests.common")
    runner = _os_threaded_runner(env)
    if runner is None:
        raise TestRunnerRefused(_OS_RUNNER_REFUSED)
    loader, results, config = runner
    state["patch_credentials"] = _os_click_needs_credentials(common.HttpCase)
    menu_cls = type(env["ir.ui.menu"])
    before = _os_click_before(menu_cls)
    case = _os_click_case(common.HttpCase, module, state)
    try:
        # 16 and later run class fixtures their own way; 15 has unittest's.
        suite_class = importlib.import_module("odoo.tests.suite").OdooSuite
    except ImportError:
        suite_class = unittest.TestSuite
    # Odoo's result asks `inspect` for the test's file, and a class built
    # here has none unless its module is somewhere to be found.
    holder = type(sys)(case.__module__)
    holder.__file__ = "<odoo-sheller clickbot>"
    sys.modules[case.__module__] = holder
    config["test_enable"] = True
    try:
        report = results.OdooTestResult()
        suite = suite_class([case("test_click_module")])
        report.update(_os_run_suite(loader, suite, report, module))
    finally:
        config["test_enable"] = None
        sys.modules.pop(case.__module__, None)
        # The test's cleanups undo the patch; an interrupt can leave unittest
        # before they run.
        _os_click_undo(menu_cls, before)
        clickbot["restored"] = _os_click_is_undone(menu_cls, before)
    failures, errors, skipped = _os_test_counts(report)

    return {
        "module": module,
        "test_class": None,
        "test_method": None,
        "tests_run": report.testsRun,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "success": report.wasSuccessful(),
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


def _os_js_probe():
    """Whether a browser suite could run in this process, asked as Odoo asks.

    ChromeBrowser skips its test when `websocket` did not import into
    odoo.tests.common or when `_find_executable` finds no Chrome; these are
    those two reads, and nothing is started. 15 and 16 keep the lookup inside
    a ChromeBrowser method, so there it is `None`: not asked, not absent.
    """
    try:
        common = importlib.import_module("odoo.tests.common")
        find = getattr(common, "_find_executable", None)
        chrome = None
        if find is not None:
            try:
                find()
                chrome = True
            # Odoo's own `executable` takes any exception as "not found".
            except Exception:  # noqa: BLE001
                chrome = False

        return {"chrome": chrome, "websocket": getattr(common, "websocket", None) is not None}
    # Never the reason a session fails to start.
    except BaseException as exc:  # noqa: BLE001

        return {"error": f"{type(exc).__name__}: {exc}"}


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
        "js": _os_js_probe(),
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
            frame.get("params"),
        )
    elif kind == "click_module":
        answer = _os_click_module(
            env,
            frame.get("module", ""),
            frame.get("login"),
            frame.get("debug", True),
            frame.get("timeout") or 900.0,
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
