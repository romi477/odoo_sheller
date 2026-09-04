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


class FakeEnv:
    """Odoo 16+ shape by default; OS_FAKE_ENV=15 drops what 15 does not have."""

    def __init__(self, flavour=None):
        self.cr = FakeCursor()
        self.uid = 1
        if flavour == "15":
            # 15 has neither flush_all nor invalidate_all: the boundary is
            # env['base'].flush() and env.clear().
            del FakeEnv.flush_all
            del FakeEnv.invalidate_all

    def __getitem__(self, name):
        assert name == "base", name

        return FakeModel()

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

    if flavour == "15":
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
    module("odoo.tests.loader", make_suite=make_suite, run_suite=run_suite)
    if flavour == "15":
        # 15 keeps the result object in odoo/tests/runner.py; there is no
        # odoo/tests/result.py at all.
        module("odoo.tests.runner", OdooTestResult=FakeResult15)
    else:
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


def _dump():
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
