import ast
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "odoo_sheller" / "bootstrap.py"
HARNESS = ROOT / "tests" / "bootstrap_harness.py"

ALLOWED_IMPORTS = {
    "ast", "copy", "importlib", "io", "json", "logging", "os", "signal", "socket", "sys", "time",
    "traceback", "unittest",
}


def run_bootstrap(frames, timeout=15, fake_odoo=None, fake_js=None, fake_click=None):
    """Feed frames over stdin, return (out_frames, stderr_text, transaction_calls)."""
    stdin = "".join(json.dumps(frame) + "\n" for frame in frames)
    env = {"OS_CMD_FD": "0", "PATH": "/usr/bin:/bin"}
    if fake_odoo:
        env["OS_FAKE_ODOO"] = fake_odoo
    if fake_js is not None:
        env["OS_FAKE_JS"] = fake_js
    if fake_click is not None:
        env["OS_FAKE_CLICK"] = json.dumps(fake_click)
    proc = subprocess.run(
        [sys.executable, str(HARNESS), str(BOOTSTRAP)],
        input=stdin,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
        env=env,
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


def test_bootstrap_parses_as_python_3_6():
    """13 declares python_requires >= 3.6, and a server running it was walked at
    3.6.9. The bootstrap runs in whatever interpreter Odoo does."""
    ast.parse(BOOTSTRAP.read_text(encoding="utf-8"), feature_version=(3, 6))


def test_hello_comes_first():
    out, _, _ = run_bootstrap([{"t": "close", "id": 1}])
    assert out[0]["t"] == "hello"
    assert out[0]["protocol"] == 1
    assert out[0]["db"] == "testdb"
    assert out[0]["uid"] == 1
    assert isinstance(out[0]["pid"], int)
    assert out[-1] == {"t": "bye", "id": 1}


@pytest.mark.parametrize(
    ("has", "js"),
    [
        ("chrome,websocket", {"chrome": True, "websocket": True}),
        ("websocket", {"chrome": False, "websocket": True}),
        ("chrome", {"chrome": True, "websocket": False}),
        # 15 and 16 look for Chrome inside a ChromeBrowser method; there is no
        # module-level finder to ask, and they run QUnit anyway.
        ("chrome,websocket,no_finder", {"chrome": None, "websocket": True}),
        # Odoo's own `executable` treats any exception as "not found".
        ("websocket,broken_finder", {"chrome": False, "websocket": True}),
    ],
)
def test_hello_says_whether_a_browser_suite_can_run_here(has, js):
    """Asked the way Odoo's ChromeBrowser asks, once, without starting one."""
    out, _, _ = run_bootstrap([{"t": "close", "id": 1}], fake_odoo="17", fake_js=has)
    assert out[0]["t"] == "hello"
    assert out[0]["js"] == js


def test_hello_survives_a_browser_check_that_cannot_even_import():
    """No odoo.tests.common at all: the session still starts, and says why."""
    out, _, _ = run_bootstrap([{"t": "close", "id": 1}])
    assert out[0]["t"] == "hello"
    assert set(out[0]["js"]) == {"error"}
    assert "odoo" in out[0]["js"]["error"]
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


def test_free_port_is_probed_on_the_interface_the_server_binds():
    """http_spawn binds config['http_interface'] or '0.0.0.0'. A port proved
    free only on loopback can still be taken there — the exact Errno 98 this
    code exists to avoid."""
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert '"127.0.0.1", 0' not in source, "loopback is not where the server binds"
    assert "http_interface" in source


def test_run_test_without_a_real_odoo_reports_a_structured_error_not_a_crash():
    """The harness's FakeEnv has no real `odoo` package on sys.path — the one
    failure mode a unit test can actually exercise without a container."""
    out, _, _ = run_bootstrap([
        {"t": "run_test", "id": 1, "module": "sale", "test_class": "TestSaleOrder",
         "test_method": None},
        {"t": "exec", "id": 2, "code": "'alive'"},
    ])
    result = out[1]
    assert result["t"] == "result"
    assert result["id"] == 1
    assert result["test"] is None
    assert result["error"]["type"] == "ModuleNotFoundError"
    # the loop must still be usable afterwards
    assert out[2]["result"] == "'alive'"


def test_run_test_calls_odoos_own_shell_runner_when_there_is_one():
    """17 and up ship odoo/tests/shell.py; nothing may replace it there."""
    out, _, calls = run_bootstrap([
        {"t": "run_test", "id": 1, "module": "sale", "test_class": "TestSaleOrder",
         "test_method": None},
    ], fake_odoo="17")
    result = out[1]
    assert result["error"] is None, result["error"]
    assert result["test"]["tests_run"] == 2
    assert "shell.run_tests(*/sale:TestSaleOrder)" in calls
    assert not [call for call in calls if call.startswith("make_suite")], (
        "the backport must stay out of the way when the real runner exists"
    )


def test_run_test_forgets_this_processs_asset_file_lists_first():
    """Each bundle's file list is cached per process (`_get_asset_paths`, in
    the `assets` cache). A session that runs a browser suite, gains a test
    file and runs it again would rerun the old list — and Hoot runs every test
    it has when a filter names one it lacks."""
    _, _, calls = run_bootstrap([
        {"t": "run_test", "id": 1, "module": "web", "test_class": "WebSuite",
         "test_method": "test_unit_desktop", "params": "@web"},
    ], fake_odoo="17")
    assert "registry.clear_cache(assets)" in calls
    assert calls.index("registry.clear_cache(assets)") < calls.index(
        "shell.run_tests(*/web:WebSuite.test_unit_desktop[@web])"
    )


def test_run_test_leaves_caches_alone_where_none_is_named():
    """16 and older have only `clear_caches()`, every ormcache at once: not
    worth it for a JS file list, so nothing is cleared."""
    _, _, calls = run_bootstrap([
        {"t": "run_test", "id": 1, "module": "sale", "test_class": "TestSaleOrder",
         "test_method": None},
    ], fake_odoo="16")
    assert not [call for call in calls if call.startswith("registry.clear_cache")]


def test_run_test_falls_back_to_the_primitives_when_shell_is_absent():
    """Odoo 16 has no odoo/tests/shell.py, but every piece it was built from.

    The fallback is that file's body against those same primitives — the tag
    DSL, make_suite, run_suite, OdooTestResult — not a runner of our own.
    """
    out, _, calls = run_bootstrap([
        {"t": "run_test", "id": 1, "module": "sale", "test_class": "TestSaleOrder",
         "test_method": "test_amount"},
    ], fake_odoo="16")
    result = out[1]
    assert result["error"] is None, result["error"]
    assert result["test"]["tests_run"] == 2
    assert result["test"]["success"] is True
    assert "make_suite(sale,at_install)" in calls
    assert "make_suite(sale,post_install)" in calls
    # The tag spec and the enable flag have to be in place while the suite runs.
    assert "run_suite(tags=*/sale:TestSaleOrder.test_amount,enable=True)" in calls
    # Same registry handling as Odoo's own runner, and the flags put back after.
    unloaded = calls.index("registry.loaded=False")
    assert "registry.loaded=True" in calls[unloaded:], "the flags must go back"
    assert "http_spawn" in " ".join(calls)


def test_the_fallback_restores_the_test_flags_it_set():
    """A session goes on being used after a test run; config is process-wide."""
    out, _, _ = run_bootstrap([
        {"t": "run_test", "id": 1, "module": "sale", "test_class": "TestSaleOrder",
         "test_method": None},
        {"t": "exec", "id": 2, "code": (
            "c = __import__('odoo').tools.config\n(c['test_enable'], c['test_tags'])"
        )},
    ], fake_odoo="16")
    assert out[1]["error"] is None
    assert out[2]["result"] == "(None, None)", out[2]


def test_the_boundaries_use_what_the_version_has():
    """15 has neither flush_all nor invalidate_all; the boundary must still be
    flush-then-commit, and invalidate-without-flushing before a rollback."""
    _, _, calls = run_bootstrap([
        {"t": "exec", "id": 1, "code": "1"},
        {"t": "commit", "id": 2},
        {"t": "rollback", "id": 3},
    ], fake_odoo="15")
    assert calls == ["base.flush", "cr.commit", "env.clear", "env.clear", "cr.rollback"], calls


def test_the_boundaries_prefer_the_explicit_api_when_it_exists():
    _, _, calls = run_bootstrap([
        {"t": "commit", "id": 1},
        {"t": "rollback", "id": 2},
    ], fake_odoo="16")
    assert calls[:3] == ["flush_all", "cr.commit", "invalidate_all(flush=False)"], calls


def test_run_test_on_fifteen_reads_the_result_it_is_given():
    """15's result object lives in odoo.tests.runner and counts in lists, and
    its run_suite takes no global_report."""
    out, _, calls = run_bootstrap([
        {"t": "run_test", "id": 1, "module": "sale", "test_class": "TestSaleOrder",
         "test_method": None},
    ], fake_odoo="15")
    result = out[1]
    assert result["error"] is None, result["error"]
    assert result["test"]["tests_run"] == 2
    assert result["test"]["failures"] == 0
    assert result["test"]["errors"] == 0
    assert result["test"]["skipped"] == 0
    assert result["test"]["success"] is True
    assert "make_suite(sale,at_install)" in calls


def test_a_whole_module_runs_its_standard_tests_only():
    """`/module`, not `*/module`: no tag means `standard` to Odoo's selector,
    the same set `--test-tags /module` runs. `*` would pull in tests tagged
    `-standard` or `external`, which may call real third-party services."""
    out, _, calls = run_bootstrap([
        {"t": "run_test", "id": 1, "module": "sale", "test_class": None,
         "test_method": None},
    ], fake_odoo="17")
    assert out[1]["error"] is None, out[1]["error"]
    assert "shell.run_tests(/sale)" in calls
    assert out[1]["test"]["test_class"] is None


def test_params_ride_on_the_tag_in_brackets():
    """How Odoo hands a test its `_test_params`: Hoot reads them as filters."""
    out, _, calls = run_bootstrap([
        {"t": "run_test", "id": 1, "module": "web", "test_class": "WebSuite",
         "test_method": "test_unit_desktop", "params": "@web/core/utils/strings"},
    ], fake_odoo="17")
    assert out[1]["error"] is None, out[1]["error"]
    assert "shell.run_tests(*/web:WebSuite.test_unit_desktop[@web/core/utils/strings])" in calls


def test_a_whole_module_on_the_fallback_uses_the_same_tag():
    out, _, calls = run_bootstrap([
        {"t": "run_test", "id": 1, "module": "sale", "test_class": None,
         "test_method": None},
    ], fake_odoo="16")
    assert out[1]["error"] is None, out[1]["error"]
    assert "run_suite(tags=/sale,enable=True)" in calls


class LiveBootstrap:
    """The bootstrap under the harness, kept running so it can be signalled
    mid-command — the way the daemon's Interrupt and timeout reach it."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(HARNESS), str(BOOTSTRAP)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={"OS_CMD_FD": "0", "PATH": "/usr/bin:/bin"},
            cwd=ROOT,
        )
        assert json.loads(self.proc.stdout.readline())["t"] == "hello"

    def send(self, frame):
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()

    def answer(self):
        line = self.proc.stdout.readline()
        assert line, "the bootstrap died: " + self.proc.stderr.read()[-500:]

        return json.loads(line)

    def interrupt_after(self, seconds):
        time.sleep(seconds)
        self.proc.send_signal(signal.SIGINT)

    def close(self):
        if self.proc.poll() is None:
            self.send({"t": "close", "id": 999})
        try:
            self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate()


@pytest.fixture
def live():
    bootstrap = LiveBootstrap()
    yield bootstrap
    bootstrap.close()


def test_an_interrupt_stops_a_running_command_and_keeps_the_session(live):
    live.send({"t": "exec", "id": 1, "code": "import time\ntime.sleep(5)"})
    live.interrupt_after(0.5)
    assert live.answer()["error"]["type"] == "KeyboardInterrupt"
    live.send({"t": "exec", "id": 2, "code": "'alive'"})
    assert live.answer()["result"] == "'alive'"


def test_an_interrupt_while_the_result_renders_keeps_the_session(live):
    """The command had finished; only its repr was running — a million-record
    recordset's takes seconds, long enough to look stuck. The interrupt used
    to land outside every handler and end the process, namespace and open
    transaction with it."""
    code = (
        "import time\n"
        "class Big:\n"
        "    def __repr__(self):\n"
        "        time.sleep(3)\n"
        "        return 'big'\n"
        "kept = 41\n"
        "Big()"
    )
    live.send({"t": "exec", "id": 1, "code": code})
    live.interrupt_after(0.5)
    answer = live.answer()
    assert answer["error"] is None, "the command itself ran to the end"
    assert answer["result"] == "<unrepresentable Big: KeyboardInterrupt>"
    live.send({"t": "exec", "id": 2, "code": "kept + 1"})
    assert live.answer()["result"] == "42", "the namespace survived"


def test_an_interrupt_between_commands_means_nothing(live):
    live.proc.send_signal(signal.SIGINT)
    time.sleep(0.3)
    assert live.proc.poll() is None
    live.send({"t": "exec", "id": 1, "code": "'alive'"})
    assert live.answer()["result"] == "'alive'"


def test_an_interrupt_while_a_frame_is_written_never_cuts_it(live):
    """Half a frame is a line the daemon cannot parse: the result never
    arrives and the session stays busy forever. The reader here is held
    back so the write blocks on a full pipe, and the signal lands inside it."""
    live.send({"t": "exec", "id": 1, "code": "print('x' * 900_000)"})
    live.interrupt_after(0.5)
    answer = live.answer()
    assert answer["id"] == 1
    assert len(answer["stdout"]) == 900_001
    live.send({"t": "exec", "id": 2, "code": "'alive'"})
    assert live.answer()["result"] == "'alive'"


# --- Odoo 13 and 14: not fully tested, and honest about what is not there ------


@pytest.mark.parametrize("flavour", ["13", "14"])
def test_the_boundaries_of_thirteen_and_fourteen_are_fifteens(flavour):
    """Neither has flush_all or invalidate_all; both have `env['base'].flush()`
    and `Environment.clear()`, which drops the cache and discards `tocompute` and
    `towrite` — so a rollback discards rather than flushes."""
    _, _, calls = run_bootstrap([
        {"t": "exec", "id": 1, "code": "1"},
        {"t": "commit", "id": 2},
        {"t": "rollback", "id": 3},
    ], fake_odoo=flavour)
    assert calls == ["base.flush", "cr.commit", "env.clear", "env.clear", "cr.rollback"], calls


def test_run_test_on_fourteen_is_fifteens():
    """14 has odoo/tests/loader.py and runner.py with the same shapes as 15."""
    out, _, calls = run_bootstrap([
        {"t": "run_test", "id": 1, "module": "sale", "test_class": "TestSaleOrder",
         "test_method": None},
    ], fake_odoo="14")
    assert out[1]["error"] is None, out[1]["error"]
    assert out[1]["test"]["tests_run"] == 2
    assert "make_suite(sale,at_install)" in calls


def test_run_test_on_thirteen_says_it_is_not_there_and_starts_nothing():
    """13 keeps its test runner in odoo.modules.module, and there is no loader to
    build a suite from. Say so, rather than an ImportError out of a fallback —
    and before an HTTP daemon is spawned for a run that cannot happen."""
    out, _, calls = run_bootstrap([
        {"t": "run_test", "id": 1, "module": "sale", "test_class": "TestSaleOrder",
         "test_method": None},
        {"t": "exec", "id": 2, "code": "'alive'"},
    ], fake_odoo="13")
    result = out[1]
    assert result["test"] is None
    assert result["error"]["type"] == "TestRunnerUnsupported"
    assert "13" in result["error"]["message"]
    assert not [call for call in calls if call.startswith("http_spawn")], calls
    assert out[2]["result"] == "'alive'", "the session is still usable"


def click_world(**changes):
    """A foreign app holding one menu of `my`, and an app of `my`'s own."""
    def menu(menu_id, xmlid, children=(), action=False):
        return {"id": menu_id, "xmlid": xmlid, "name": xmlid.split(".")[1],
                "children": list(children), "action": action}

    world = {
        "modules": {"my": "installed", "sale": "installed", "draft": "uninstalled"},
        "users": ["admin", "demo"],
        "menus": {
            "root": {"id": False, "name": "root", "children": [1, 10]},
            "1": menu(1, "sale.menu_root", [2]),
            "2": menu(2, "sale.menu_sales", [3, 4]),
            "3": menu(3, "my.menu_a", action="ir.actions.act_window,7"),
            "4": menu(4, "sale.menu_orders", action="ir.actions.act_window,8"),
            "10": menu(10, "my.menu_app", [11], action="ir.actions.act_window,9"),
            "11": menu(11, "my.menu_cfg", [12]),
            "12": menu(12, "my.menu_b", action="ir.actions.act_window,10"),
        },
        "data": [["my", "menu_a", 3], ["my", "menu_app", 10], ["my", "menu_cfg", 11],
                 ["my", "menu_b", 12], ["sale", "menu_root", 1]],
    }
    world.update(changes)

    return world


def click(world, module="my", login=None, debug=True):
    frame = protocol_click_frame(module, login, debug)
    out, stderr, calls = run_bootstrap([frame, {"t": "close", "id": 2}], fake_odoo="16",
                                       fake_click=world)
    assert out[1]["id"] == 1, stderr

    return out[1], calls, stderr


def protocol_click_frame(module, login, debug):
    from odoo_sheller import protocol

    return protocol.click_module_frame(1, module, login, debug, 120.0)


def test_an_unknown_module_is_refused_before_any_server_starts():
    answer, calls, _ = click(click_world(), module="nope")
    assert answer["clickbot"]["refused"] == "unknown_module"
    assert answer["test"] is None
    assert not any(call.startswith(("http_spawn", "browser_js")) for call in calls)


def test_a_module_that_is_not_installed_is_refused():
    answer, calls, _ = click(click_world(), module="draft")
    assert answer["clickbot"]["refused"] == "module_not_installed"
    assert not any(call.startswith("http_spawn") for call in calls)


def test_an_unknown_login_is_refused():
    answer, calls, _ = click(click_world(), login="ghost")
    assert answer["clickbot"]["refused"] == "unknown_login"
    assert answer["error"] is None
    assert not any(call.startswith("http_spawn") for call in calls)


def test_a_missing_admin_is_an_unknown_login_too():
    answer, _, _ = click(click_world(admin="someone@example.com"))
    assert answer["clickbot"]["refused"] == "unknown_login"


def test_a_module_without_menus_is_refused():
    answer, calls, _ = click(click_world(), module="sale", login="admin")
    # sale owns only its root here, which holds no target of its own.
    assert answer["clickbot"]["refused"] == "no_visible_menus"
    answer, calls, _ = click(click_world(data=[]))
    assert answer["clickbot"]["refused"] == "no_menus"
    assert not any(call.startswith("http_spawn") for call in calls)


def test_a_dangling_menu_xmlid_is_skipped():
    world = click_world()
    world["data"].append(["my", "menu_deleted", 99])
    answer, _, _ = click(world)
    assert answer["error"] is None, answer["error"]
    assert "my.menu_deleted" not in answer["clickbot"]["hidden"]
    assert answer["clickbot"]["refused"] is None


def test_a_container_running_workers_is_refused_as_run_test_refuses_it():
    answer, calls, _ = click(click_world(workers=2))
    assert answer["error"]["type"] == "TestRunnerRefused"
    assert "workers=0" in answer["error"]["message"]
    assert not any(call.startswith(("http_spawn", "browser_js")) for call in calls)


def test_click_module_shares_run_tests_preparation():
    answer, calls, stderr = click(click_world())
    assert answer["error"] is None, answer["error"]
    spawn = next(i for i, call in enumerate(calls) if call.startswith("http_spawn"))
    first = next(i for i, call in enumerate(calls) if call.startswith("browser_js"))
    assert spawn < first
    assert "after:own_load_menus=False" in calls
    clickbot = answer["clickbot"]
    assert clickbot["restored"] is True
    assert clickbot["login"] == "admin"
    assert answer["test"]["success"] is True
    assert answer["test"]["tests_run"] == 1
    assert clickbot["clicked"] == ["my.menu_a", "my.menu_app", "my.menu_b"]
    assert clickbot["failed"] == [] and clickbot["unreached"] == []
    assert [root["xmlid"] for root in clickbot["roots"]] == ["sale.menu_root", "my.menu_app"]
    # Two filters for each of the four menus clicked: the roots and the targets.
    assert clickbot["filters"] == 8
    assert "apps" not in clickbot and "menus" not in clickbot


def test_the_browser_is_shown_only_the_modules_tree_in_debug():
    _, calls, _ = click(click_world())
    shown = [call for call in calls if call.startswith("browser_js")]
    assert shown[0].startswith("browser_js(/odoo?debug=1,")
    assert "startClickEverywhere('sale.menu_root')" in shown[0]
    assert "load_menus('1')" in calls


def test_without_debug_the_tree_and_browser_agree():
    _, calls, _ = click(click_world(), debug=False)
    assert "load_menus('')" in calls
    assert any(call.startswith("browser_js(/odoo,") for call in calls)


def test_a_failing_menu_is_blamed_and_the_rest_still_clicked():
    answer, calls, _ = click(click_world(fail=["my.menu_a"]))
    clickbot = answer["clickbot"]
    assert [failure["xmlid"] for failure in clickbot["failed"]] == ["my.menu_a"]
    assert clickbot["failed"][0]["root"] == "sale.menu_root"
    assert "RPC_ERROR in my.menu_a" in clickbot["failed"][0]["reason"]
    assert "my.menu_b" in clickbot["clicked"]
    assert answer["test"]["success"] is False
    assert clickbot["roots"][0]["success"] is False
    assert clickbot["roots"][1]["success"] is True
    assert "after:own_load_menus=False" in calls


def test_an_own_root_that_fails_ends_its_app():
    """15 to 19 click the app root first on every run: nothing under it is reached."""
    answer, calls, _ = click(click_world(fail=["my.menu_app"]))
    clickbot = answer["clickbot"]
    assert [failure["xmlid"] for failure in clickbot["failed"]] == ["my.menu_app"]
    assert clickbot["roots"][1] == {"xmlid": "my.menu_app", "success": False, "error": None}
    assert clickbot["unreached"] == ["my.menu_b"]
    assert len([call for call in calls if "startClickEverywhere('my.menu_app')" in call]) == 1


def test_a_foreign_root_failing_blames_its_first_target():
    answer, calls, _ = click(click_world(fail=["sale.menu_root"]))
    clickbot = answer["clickbot"]
    assert clickbot["failed"][0]["xmlid"] == "my.menu_a"
    assert clickbot["failed"][0]["root"] == "sale.menu_root"
    assert clickbot["roots"][0]["error"] is None


def test_a_browser_that_breaks_otherwise_is_a_failure_with_its_reason():
    """Not every way a browser_js ends is its own fail(): Chrome dies, a call times out."""
    answer, _, _ = click(click_world(crash=["my.menu_a"]))
    clickbot = answer["clickbot"]
    assert [failure["xmlid"] for failure in clickbot["failed"]] == ["my.menu_a"]
    assert "RuntimeError: Chrome died under my.menu_a" in clickbot["failed"][0]["reason"]
    assert "my.menu_b" in clickbot["clicked"]
    assert answer["test"]["success"] is False


def test_counts_take_in_what_a_restarted_root_clicked_before_it_failed():
    world = click_world()
    world["menus"]["10"]["children"] = [11, 13]
    world["menus"]["13"] = {"id": 13, "xmlid": "my.menu_c", "name": "menu_c", "children": [],
                            "action": "ir.actions.act_window,11"}
    world["data"].append(["my", "menu_c", 13])
    answer, _, _ = click(dict(world, fail=["my.menu_b"]))
    clickbot = answer["clickbot"]
    assert [failure["xmlid"] for failure in clickbot["failed"]] == ["my.menu_b"]
    assert "my.menu_c" in clickbot["clicked"]
    # sale's root and my.menu_a; then my.menu_app before my.menu_b failed, and
    # my.menu_app and my.menu_c in the run after: five menus, two filters each.
    assert clickbot["filters"] == 10


def test_two_failures_under_two_roots_are_both_found():
    answer, _, _ = click(click_world(fail=["my.menu_a", "my.menu_b"]))
    failed = answer["clickbot"]["failed"]
    assert [(failure["xmlid"], failure["root"]) for failure in failed] == [
        ("my.menu_a", "sale.menu_root"), ("my.menu_b", "my.menu_app")]
    assert answer["clickbot"]["unreached"] == []


def test_a_skipped_browser_is_counted_as_a_skip():
    answer, _, _ = click(click_world(skip=True))
    assert answer["test"]["skipped"] >= 1
    assert answer["clickbot"]["restored"] is True


def test_the_password_check_is_patched_where_authenticate_does_not():
    answer, _, _ = click(click_world())
    assert "a real password check" not in json.dumps(answer)


def test_the_password_check_is_off_only_inside_authenticate():
    """18 and later patch it around the one call; 15 to 17 get the same, no more.

    The test's HTTP server listens on every interface for the whole run: a
    patch that outlived `authenticate` would let anyone log in as anyone.
    """
    answer, calls, _ = click(click_world())
    assert answer["test"]["success"] is True, answer
    assert "password_check_outside_authenticate=real" in calls
    assert "password_check_outside_authenticate=off" not in calls
    assert "after:own_check_credentials=False" in calls


def test_an_interrupt_inside_authenticate_leaves_the_password_check_real():
    answer, calls, _ = click(click_world(interrupt_in_auth=True))
    assert answer["error"]["type"] == "KeyboardInterrupt"
    assert "after:own_check_credentials=False" in calls
    assert answer["clickbot"]["restored"] is True


def test_15_stops_its_browser_after_every_app():
    answer, calls, _ = click(click_world(flavour="15", fail=["my.menu_a"]))
    assert answer["clickbot"]["failed"][0]["xmlid"] == "my.menu_a"
    shown = [i for i, call in enumerate(calls) if call.startswith("browser_js(/web?debug=1,")]
    assert shown and all(calls[i + 1] == "terminate_browser" for i in shown)


def test_20_goes_on_past_a_failure_in_one_run():
    answer, calls, _ = click(click_world(flavour="20", fail=["my.menu_a", "my.menu_b"]))
    clickbot = answer["clickbot"]
    assert [failure["xmlid"] for failure in clickbot["failed"]] == ["my.menu_a", "my.menu_b"]
    assert "Current testing menu is menu_b (my.menu_b)" in clickbot["failed"][1]["reason"]
    assert calls.count("error_checker=True") == 2
    assert all(root["error"] is None for root in clickbot["roots"])
    # 20 clicks an app root only when nothing is under it: not a target there.
    assert "my.menu_app" not in clickbot["unreached"] + clickbot["clicked"]
    assert "menus" not in clickbot
