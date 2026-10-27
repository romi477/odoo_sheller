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


@pytest.mark.parametrize("odoo", ["17.0", "18.0", "19.0", "20.0"])
def test_clickbot_capability_before_eighteen_is_not_hoots(odoo):
    """The clickbot needs Chrome, not Hoot's runner: 17 has the one and not the other."""
    assert js_tests.clickbot_capability(hello(odoo)) == {"available": True, "reason": None}


def test_unchecked_chrome_is_said_not_guessed():
    """15 and 16 keep the Chrome lookup inside ChromeBrowser: hello cannot say."""
    found = js_tests.clickbot_capability(hello("15.0", {"chrome": None, "websocket": True}))
    assert found == {"available": None, "reason": "chrome_not_checked"}
    assert "chrome_not_checked" in js_tests.REASONS


@pytest.mark.parametrize(("js", "reason"), [
    ({"chrome": False, "websocket": True}, "chrome_not_found"),
    ({"chrome": None, "websocket": False}, "websocket_client_missing"),
    ({"error": "ImportError"}, "probe_failed"),
    (None, "probe_failed"),
])
def test_clickbot_capability_names_what_is_missing(js, reason):
    assert js_tests.clickbot_capability(hello("16.0", js)) == {"available": False, "reason": reason}
