"""Run bootstrap.py the way odoo-bin shell runs it, with a fake Odoo env.

Usage: python3 tests/bootstrap_harness.py odoo_sheller/bootstrap.py
Writes a PTLOG line to stderr at exit listing the transaction calls it saw.
"""

import atexit
import json
import os
import sys

CALLS = []


class FakeConnection:
    status = 1  # psycopg2's STATUS_READY


class FakeCursor:
    dbname = "testdb"
    _cnx = FakeConnection()

    def commit(self):
        CALLS.append("cr.commit")

    def rollback(self):
        CALLS.append("cr.rollback")


class FakeModel:
    def flush(self, *args, **kwargs):
        CALLS.append("base.flush")


class FakeEnvRegistry:
    """`env.registry` as far as the bootstrap touches it before a run."""

    def clear_caches(self):
        CALLS.append("registry.clear_caches")


class FakeNamedCachesRegistry(FakeEnvRegistry):
    """17 to 19 name their ormcaches; 20 keeps them per transaction instead."""

    def clear_cache(self, *names):
        CALLS.append(f"registry.clear_cache({','.join(names)})")


class FakeEnv:
    """Odoo 16+ shape by default; OS_FAKE_ENV=15 drops what 15 does not have."""

    def __init__(self, flavour=None):
        self.cr = FakeCursor()
        self.uid = 1
        self.registry = (
            FakeNamedCachesRegistry() if flavour == "17" else FakeEnvRegistry()
        )
        if flavour in ("13", "14", "15"):
            # 13 through 15 have neither flush_all nor invalidate_all: the
            # boundary is env['base'].flush() and env.clear().
            del FakeEnv.flush_all
            del FakeEnv.invalidate_all

    def __getitem__(self, name):
        if CLICK and name in CLICK_MODELS:

            return CLICK_MODELS[name]
        assert name == "base", name

        return FakeModel()

    def ref(self, xmlid, raise_if_not_found=True):
        assert xmlid == "base.user_admin", xmlid
        users = CLICK_MODELS["res.users"].search([("login", "=", CLICK.get("admin", "admin"))])

        return users or None

    def clear(self):
        CALLS.append("env.clear")

    def flush_all(self):
        CALLS.append("flush_all")

    def invalidate_all(self, flush=True):
        CALLS.append(f"invalidate_all(flush={flush})")


# --- a fake Odoo, only as far as the test runner reaches ---------------------
#
# OS_FAKE_ODOO=16 installs the shape of Odoo 16: `odoo.tests.loader`,
# `odoo.tests.result` and `odoo.modules.registry`, but no `odoo.tests.shell`
# — that file arrived in 17. OS_FAKE_ODOO=17 adds the shell wrapper on top.
# Nothing here reimplements Odoo; it is only enough surface for the bootstrap
# to pick a path and be seen doing it.


class FakeSuite:
    def __init__(self, count):
        self._count = count

    def countTestCases(self):
        return self._count

    def __call__(self, result):
        result.testsRun += self._count


class FakeResult:
    def __init__(self, global_report=None):
        self.testsRun = 0
        self.failures_count = 0
        self.errors_count = 0
        self.skipped = 0

    def update(self, other):
        self.testsRun += other.testsRun
        self.failures_count += other.failures_count
        self.errors_count += other.errors_count
        self.skipped += other.skipped

    def wasSuccessful(self):
        return not (self.failures_count or self.errors_count)


class FakeResult15(FakeResult):
    """15's OdooTestResult is unittest's: the counters are lists."""

    def __init__(self):
        self.testsRun = 0
        self.failures = []
        self.errors = []
        self.skipped = []

    def update(self, other):
        self.testsRun += other.testsRun
        self.failures.extend(other.failures)
        self.errors.extend(other.errors)
        self.skipped.extend(other.skipped)

    def wasSuccessful(self):
        return not (self.failures or self.errors)


def _install_fake_odoo(flavour):
    import threading
    import types

    def module(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        # A real import binds the submodule on its parent too, and code that
        # reaches `odoo.tools.config` by attribute depends on that.
        parent, _, leaf = name.rpartition(".")
        if parent in sys.modules:
            setattr(sys.modules[parent], leaf, mod)

        return mod

    config = {
        "workers": 0, "http_port": 8069, "http_interface": "",
        "test_tags": None, "test_enable": None,
    }

    class FakeServer:
        httpd = None
        port = 8069
        interface = ""

        def http_spawn(self):
            CALLS.append(f"http_spawn(port={self.port})")
            FakeServer.httpd = object()

    class FakeRegistry:
        _lock = threading.Lock()

        def __init__(self, dbname):
            CALLS.append(f"Registry({dbname})")
            self.loaded = True
            self.ready = True

        def __setattr__(self, name, value):
            if name in ("loaded", "ready"):
                CALLS.append(f"registry.{name}={value}")
            object.__setattr__(self, name, value)

    def make_suite(modules, position="at_install"):
        CALLS.append(f"make_suite({','.join(modules)},{position})")

        return FakeSuite(2 if position == "at_install" else 0)

    if flavour in ("14", "15"):
        def run_suite(suite, module_name=None):
            CALLS.append(
                f"run_suite(tags={config['test_tags']},enable={config['test_enable']})"
            )
            result = FakeResult15()
            suite(result)

            return result
    else:
        def run_suite(suite, global_report=None):
            CALLS.append(
                f"run_suite(tags={config['test_tags']},enable={config['test_enable']})"
            )
            result = FakeResult()
            suite(result)

            return result

    module("odoo")
    module("odoo.cli", COMMAND="shell")
    module("odoo.tools", config=config)
    module("odoo.service")
    module("odoo.service.server", server=FakeServer())
    module("odoo.modules")
    module("odoo.modules.registry", Registry=FakeRegistry)
    module("odoo.tests")
    if flavour == "13":
        # 13 has no odoo/tests/loader.py and no runner of its own: its tests are
        # run by odoo.modules.module.run_unit_tests, which this does not model.
        pass
    else:
        module("odoo.tests.loader", make_suite=make_suite, run_suite=run_suite)
    if flavour in ("14", "15"):
        # 14 and 15 keep the result object in odoo/tests/runner.py; there is no
        # odoo/tests/result.py at all.
        module("odoo.tests.runner", OdooTestResult=FakeResult15)
    elif flavour != "13":
        module("odoo.tests.result", OdooTestResult=FakeResult)
    module("psycopg2")
    module("psycopg2.extensions", STATUS_READY=1)
    if flavour == "17":
        def run_tests(env, test_tags, modules=None, reload_tests=False):
            CALLS.append(f"shell.run_tests({test_tags})")
            report = FakeResult()
            report.testsRun = 2

            return report

        module("odoo.tests.shell", run_tests=run_tests)
    js = os.environ.get("OS_FAKE_JS")
    if js is not None:
        _install_fake_browser(module, set(filter(None, js.split(","))))
    if CLICK:
        _install_fake_click(module)


def _install_fake_browser(module, has):
    """`odoo.tests.common` as far as the browser check reads it.

    OS_FAKE_JS lists what is there: `chrome`, `websocket`. `no_finder` drops
    `_find_executable`, which 15 and 16 do not have at module level;
    `broken_finder` makes it raise something other than SkipTest.
    """
    import unittest

    def find_executable():
        if "broken_finder" in has:
            raise RuntimeError("platform.system() exploded")
        if "chrome" not in has:
            raise unittest.SkipTest("Chrome executable not found")

        return "/usr/bin/chromium"

    attrs = {"websocket": object() if "websocket" in has else None}
    if "no_finder" not in has:
        attrs["_find_executable"] = find_executable
    module("odoo.tests.common", **attrs)


# --- a fake Odoo for the clickbot --------------------------------------------
#
# OS_FAKE_CLICK (JSON) on top of OS_FAKE_ODOO: the modules, users, menu rows
# and `load_menus` tree the bootstrap looks up, an HttpCase whose browser_js
# plays a 19 clickbot over whatever `load_menus` answers at that moment, and
# the two source files the bootstrap reads its call form from.
#   modules: {name: state}      users: [login]       admin: login of user_admin
#   menus: load_menus output, ids as strings       data: [[module, name, res_id]]
#   fail: [xmlid] the browser fails on              skip: true, Chrome is missing

CLICK = json.loads(os.environ.get("OS_FAKE_CLICK") or "null")
CLICK_MODELS = {}

LOADER_JS = """export async function startClickEverywhere(xmlId, light, currentState) {}"""
CLICK_TEST_PY = """
class TestMenusAdmin(odoo.tests.HttpCase):
    def test_01_click_everywhere_as_admin(self):
        for app_id in menus['root']['children']:
            with self.subTest(app=menus[app_id]['name']):
                self.browser_js("/odoo", "odoo.loader.modules.get('@web/webclient/clickbot/clickbot_loader').startClickEverywhere('%s');" % menus[app_id]['xmlid'], "odoo.isReady === true", login="admin", timeout=1200, success_signal="clickbot test succeeded")
"""


LOADER_JS_20 = LOADER_JS + "\nconst launcher = new ClickbotLauncher(env, options);"
CLICK_TEST_PY_20 = CLICK_TEST_PY.replace(
    "startClickEverywhere('%s');", "startClickEverywhere({ xmlId: '%s', logger: true });")
CLICK_TEST_PY_15 = CLICK_TEST_PY.replace(
    "\"/odoo\", \"odoo.loader.modules.get('@web/webclient/clickbot/clickbot_loader')"
    ".startClickEverywhere('%s');\"",
    "\"/web\", \"odoo.__DEBUG__.services['web.clickEverywhere']('%s');\"",
).replace(', success_signal="clickbot test succeeded")', ")\n                self.terminate_browser()")


class FakeRecords:
    """A recordset as far as the clickbot's lookups read one."""

    def __init__(self, model, rows):
        self.model = model
        self.rows = rows

    def __bool__(self):
        return bool(self.rows)

    def __getattr__(self, name):
        if name in ("ids", "_ids"):
            return [row["id"] for row in self.rows]
        if name == "_fields":
            return {}
        if name == "id":
            return self.rows[0]["id"]

        return self.rows[0][name]

    def sudo(self):
        return self

    def with_context(self, **kwargs):
        return self

    def with_user(self, user):
        CALLS.append(f"{self.model.name}.with_user({user.login})")

        return self

    def exists(self):
        return FakeRecords(self.model, [row for row in self.rows if row.get("exists", True)])


class FakeClickModel(FakeRecords):
    def __init__(self, name, rows):
        FakeRecords.__init__(self, self, rows)
        self.name = name

    def search(self, domain, limit=None):
        found = [row for row in self.rows
                 if all(row.get(field) == value for field, _, value in domain)]

        return FakeRecords(self, found[:limit] if limit else found)

    def search_read(self, domain, fields):
        return [dict(row) for row in self.search(domain).rows]

    def browse(self, ids):
        ids = [ids] if isinstance(ids, int) else ids
        rows = [row for row in self.rows if row["id"] in ids]
        known = set(row["id"] for row in rows)

        return FakeRecords(self, rows + [{"id": i, "exists": False} for i in ids if i not in known])


class FakeMenuBase(FakeClickModel):
    def load_menus(self, debug):
        CALLS.append(f"load_menus({debug!r})")
        tree = {}
        for key, entry in CLICK["menus"].items():
            tree["root" if key == "root" else int(key)] = json.loads(json.dumps(entry))

        return tree


class FakeMenu(FakeMenuBase):
    """The registry class: `load_menus` is inherited, as on a real registry."""


class FakeUsersBase(FakeClickModel):
    def _check_credentials(self, password, env):
        raise AssertionError("a real password check")


class FakeUsers(FakeUsersBase):
    pass


def _install_fake_click(module):
    import logging
    import tempfile
    import unittest
    from unittest import mock

    CLICK_MODELS["ir.module.module"] = FakeClickModel("ir.module.module", [
        {"id": i, "name": name, "state": state}
        for i, (name, state) in enumerate(sorted(CLICK.get("modules", {}).items()), 1)])
    CLICK_MODELS["res.users"] = FakeUsers("res.users", [
        {"id": i, "login": login} for i, login in enumerate(CLICK.get("users", []), 2)])
    CLICK_MODELS["ir.model.data"] = FakeClickModel("ir.model.data", [
        {"id": i, "model": "ir.ui.menu", "module": mod, "name": name, "res_id": res_id}
        for i, (mod, name, res_id) in enumerate(CLICK.get("data", []), 1)])
    CLICK_MODELS["ir.ui.menu"] = FakeMenu("ir.ui.menu", [
        {"id": int(key)} for key in CLICK["menus"] if key != "root"])

    web = tempfile.mkdtemp(prefix="os-fake-web-")
    version = CLICK.get("flavour")
    loader_js = LOADER_JS_20 if version == "20" else LOADER_JS
    test_py = {"20": CLICK_TEST_PY_20, "15": CLICK_TEST_PY_15}.get(version, CLICK_TEST_PY)
    for parts, text in (
        (("static", "src", "webclient", "clickbot", "clickbot_loader.js"), loader_js),
        (("tests", "test_click_everywhere.py"), test_py),
    ):
        os.makedirs(os.path.join(web, *parts[:-1]), exist_ok=True)
        with open(os.path.join(web, *parts), "w", encoding="utf-8") as handle:
            handle.write(text)

    def get_module_path(name):
        assert name == "web", name

        return web

    class HttpCase(unittest.TestCase):
        env = FakeEnv()

        def patch(self, obj, key, val):
            patcher = mock.patch.object(obj, key, val)
            patcher.start()
            self.addCleanup(patcher.stop)

        def authenticate(self, user, password):
            # 15 to 17: no patched_check_credentials constant in here.
            CLICK_MODELS["res.users"]._check_credentials(password, None)

        def terminate_browser(self):
            CALLS.append("terminate_browser")

        def browser_js(self, url, code, ready="", login=None, timeout=60, **kw):
            if CLICK.get("skip"):
                raise unittest.SkipTest("Chrome executable not found")
            self.authenticate(login, login)
            menus = CLICK_MODELS["ir.ui.menu"].load_menus("1" if "debug=1" in url else "")
            CALLS.append(f"browser_js({url},{code},roots={menus['root']['children']})")
            log = logging.getLogger(f"{type(self).__module__}.{type(self).__name__}"
                                    f".{self._testMethodName}.browser")
            if "xmlId: '" in code:
                app = code.split("xmlId: '")[1].split("'")[0]
            else:
                app = code.rsplit("('", 1)[1].split("')")[0]
            root = next(key for key in menus["root"]["children"] if menus[key]["xmlid"] == app)
            if version == "20":
                self._clickbot_20(log, menus, root, kw)
                return
            log.info("Testing app menu: %s", app)
            pending = [root]
            while pending:
                key = pending.pop(0)
                entry = menus[key]
                if entry["children"] and key != root:
                    pending = list(entry["children"]) + pending
                    continue
                log.info("Testing menu %s %s", entry["name"], entry["xmlid"])
                if entry["xmlid"] in CLICK.get("fail", []):
                    log.error("RPC_ERROR in %s", entry["xmlid"])
                    raise AssertionError("The test code failed")
                if key == root:
                    pending = list(entry["children"])
            log.info("Successfully tested 1 apps")
            log.info("clickbot test succeeded")

    def clickbot_20(self, log, menus, root, kw):
        """20's walks the leaves, logs a block per failing menu and goes on."""
        CALLS.append(f"error_checker={'error_checker' in kw}")
        failed = False
        pending = [root]
        while pending:
            key = pending.pop(0)
            entry = menus[key]
            if entry["children"]:
                pending = list(entry["children"]) + pending
                continue
            log.info("Testing menu %s (%s)", entry["name"], entry["xmlid"])
            if entry["xmlid"] in CLICK.get("fail", []):
                block = ("Error found:\n - Current testing app is %s (%s)\n"
                         " - Current testing menu is %s (%s)\nThe error is :\nboom"
                         % (menus[root]["name"], menus[root]["xmlid"], entry["name"],
                            entry["xmlid"]))
                log.error("%s", block)
                failed = True
                if not kw["error_checker"](block):
                    continue
                raise AssertionError(block)
        log.info("Tested 2 menus")
        if failed:
            log.error("clickbot test failed")
            raise AssertionError("clickbot test failed")

    HttpCase._clickbot_20 = clickbot_20
    module("odoo.modules.module", get_module_path=get_module_path)
    module("odoo.tests.common", HttpCase=HttpCase)

    class Result(unittest.TestResult):
        def update(self, other):
            self.testsRun += other.testsRun
            self.failures.extend(other.failures)
            self.errors.extend(other.errors)
            self.skipped.extend(other.skipped)

    def run_suite(suite, global_report=None):
        CALLS.append("run_suite(click)")
        result = Result()
        suite(result)
        for _, text in result.errors + result.failures:
            sys.stderr.write("CLICKTEST: " + text + "\n")

        return result

    sys.modules["odoo.tests.loader"].run_suite = run_suite
    sys.modules["odoo.tests.result"].OdooTestResult = Result


def _dump():
    if CLICK:
        CALLS.append(f"after:own_load_menus={'load_menus' in FakeMenu.__dict__}")
    sys.stderr.write("PTLOG:" + json.dumps(CALLS) + "\n")
    sys.stderr.flush()


atexit.register(_dump)

flavour = os.environ.get("OS_FAKE_ODOO")
if flavour:
    _install_fake_odoo(flavour)

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    source = handle.read()

namespace = {"env": FakeEnv(flavour), "self": None, "odoo": None, "__name__": "__main__"}
exec(compile(source, path, "exec"), namespace)  # noqa: S102
