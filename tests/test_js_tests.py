"""What a session says about running a browser suite, from what hello found."""

import pytest

from odoo_sheller import js_tests

BOTH = {"chrome": True, "websocket": True}


def hello(odoo, js=BOTH):

    return {"t": "hello", "odoo": odoo, "js": js}


@pytest.mark.parametrize("odoo", ["18.0", "19.0+e", "saas~18.3"])
def test_hoot_with_a_browser_is_available_and_verified(odoo):
    assert js_tests.capability(hello(odoo)) == {
        "runner": "hoot", "available": True, "reason": None, "tested": "18-19",
    }


def test_twenty_can_run_and_says_it_is_not_known_to_work():
    """A client must be able to tell "can run" from "known to work"."""
    assert js_tests.capability(hello("20.0")) == {
        "runner": "hoot", "available": True, "reason": None, "tested": "unverified",
    }


@pytest.mark.parametrize("odoo", ["15.0", "16.0", "17.0"])
def test_qunit_is_reported_and_not_driven(odoo):
    assert js_tests.capability(hello(odoo)) == {
        "runner": "qunit", "available": False, "reason": "runner_not_supported", "tested": None,
    }


def test_an_unknown_major_has_no_runner():
    described = js_tests.capability(hello("unknown"))
    assert described["runner"] is None
    assert described["available"] is False
    assert described["reason"] == "runner_not_supported"


def test_no_chrome_is_named():
    described = js_tests.capability(hello("19.0", {"chrome": False, "websocket": True}))
    assert (described["available"], described["reason"]) == (False, "chrome_not_found")


def test_no_websocket_client_is_named_first_as_odoo_checks_it_first():
    """ChromeBrowser looks at `websocket` before it looks for Chrome, so that is
    the skip a run would hit."""
    described = js_tests.capability(hello("19.0", {"chrome": False, "websocket": False}))
    assert (described["available"], described["reason"]) == (False, "websocket_client_missing")


@pytest.mark.parametrize(
    "js",
    [{"error": "ImportError: no odoo.tests"}, None, "garbage", {"chrome": None, "websocket": True}],
)
def test_a_check_that_did_not_answer_is_probe_failed(js):
    described = js_tests.capability(hello("19.0", js))
    assert (described["available"], described["reason"]) == (False, "probe_failed")
    assert described["runner"] == "hoot"


def test_every_reason_is_in_the_closed_list():
    seen = {
        js_tests.capability(hello(odoo, js))["reason"]
        for odoo in ("15.0", "19.0", "x")
        for js in (BOTH, {"chrome": False, "websocket": True}, {"chrome": True, "websocket": False}, None)
    }
    assert seen - {None} <= set(js_tests.REASONS)
