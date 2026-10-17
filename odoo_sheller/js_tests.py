"""Browser suites: whether a session can run one, and how a run that did not says so.

Odoo does not fail a browser test it cannot run, it skips it — and a skipped
suite counts as a pass to anyone who reads only the counts. So the session
says up front what the container lacks, and a run that was skipped anyway is
reported as what it is. Both name the cause from the one closed list below.
"""

from odoo_sheller import rules

# Why a session cannot run a browser suite, or why a run did not.
REASONS = (
    "runner_not_supported",
    "chrome_not_found",
    "websocket_client_missing",
    "probe_failed",
)

# The suite the shell can drive: Hoot, whose runner arrived in 18. Before
# that the browser tests are QUnit, which this daemon lists and never runs.
HOOT_MAJORS = range(18, 21)
QUNIT_MAJORS = range(15, 18)
# How far this daemon's own runs go. 20 is read from source, not run.
VERIFIED = {18: "18-19", 19: "18-19", 20: "unverified"}


def runner_for(major: int | None) -> str | None:
    if major in HOOT_MAJORS:

        return "hoot"
    if major in QUNIT_MAJORS:

        return "qunit"

    return None


def capability(hello: dict | None) -> dict:
    """The session's `js_tests`, from the major and what the bootstrap found."""
    major = rules.major_of((hello or {}).get("odoo"))
    runner = runner_for(major)
    found = (hello or {}).get("js")
    described = {"runner": runner, "available": False, "reason": None, "tested": VERIFIED.get(major)}
    if runner != "hoot":
        described["reason"] = "runner_not_supported"
    elif not isinstance(found, dict) or "error" in found or not isinstance(found.get("chrome"), bool):
        described["reason"] = "probe_failed"
    # ChromeBrowser checks `websocket` before it looks for Chrome.
    elif not found.get("websocket"):
        described["reason"] = "websocket_client_missing"
    elif not found["chrome"]:
        described["reason"] = "chrome_not_found"
    else:
        described["available"] = True

    return described


# The Python test that drives Hoot in a browser, and the filter that narrows
# it to one module: `docs/agent-guide.md`, "Hoot, headless".
HOOT_TEST = "web.WebSuite.test_unit_desktop"


def listing(module: str, major: int | None, files: list) -> dict:
    """The `js` part of a module's test listing: its files, and a ready run."""
    runner = runner_for(major)
    run = {"test": HOOT_TEST, "params": f"@{module}"} if runner == "hoot" and files else None

    return {"runner": runner, "files": files, "run": run}


# What Odoo's ChromeBrowser raises SkipTest with, as `addSkip` logs it at the
# end of `skipped <test> : <reason>` (`odoo/tests/result.py`, 16 to 20).
_SKIPS = {
    "Chrome executable not found": "chrome_not_found",
    "websocket-client module is not installed": "websocket_client_missing",
}


def skip_reason(line: str) -> str | None:
    """The reason from REASONS when this skip line is a browser that could not start."""
    for message, reason in _SKIPS.items():
        if line.rstrip().endswith(" : " + message):

            return reason

    return None
