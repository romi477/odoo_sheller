"""Run bootstrap.py the way odoo-bin shell runs it, with a fake Odoo env.

Usage: python3 tests/bootstrap_harness.py odoo_sheller/bootstrap.py
Writes a PTLOG line to stderr at exit listing the transaction calls it saw.
"""

import atexit
import json
import sys

CALLS = []


class FakeCursor:
    dbname = "testdb"

    def commit(self):
        CALLS.append("cr.commit")

    def rollback(self):
        CALLS.append("cr.rollback")


class FakeEnv:
    def __init__(self):
        self.cr = FakeCursor()
        self.uid = 1

    def flush_all(self):
        CALLS.append("flush_all")

    def invalidate_all(self, flush=True):
        CALLS.append(f"invalidate_all(flush={flush})")


def _dump():
    sys.stderr.write("PTLOG:" + json.dumps(CALLS) + "\n")
    sys.stderr.flush()


atexit.register(_dump)

path = sys.argv[1]
with open(path, encoding="utf-8") as handle:
    source = handle.read()

namespace = {"env": FakeEnv(), "self": None, "odoo": None, "__name__": "__main__"}
exec(compile(source, path, "exec"), namespace)  # noqa: S102
