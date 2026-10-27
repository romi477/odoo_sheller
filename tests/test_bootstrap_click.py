"""The clickbot's pure parts of the bootstrap, called as functions.

The bootstrap is a script, not a module: it ends by entering its loop. These
tests exec its source without that last line and call what it defined.
"""

import copy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "odoo_sheller" / "bootstrap.py"
ENTRY = "_os_main(globals())"


def bootstrap_namespace():
    source = BOOTSTRAP.read_text(encoding="utf-8")
    lines = source.splitlines()
    assert lines.count(ENTRY) == 1, "the bootstrap's entry point moved"
    namespace = {"__name__": "os_bootstrap_under_test"}
    exec(compile("\n".join(line for line in lines if line != ENTRY), str(BOOTSTRAP), "exec"),
         namespace)

    return namespace


NS = bootstrap_namespace()


def menu(menu_id, xmlid, children=(), action=False):
    return {"id": menu_id, "xmlid": xmlid, "name": xmlid.split(".")[1],
            "children": list(children), "action": action}


def sample():
    """A foreign app holding one of ours, an app of our own, and a URL menu."""
    menus = {
        "root": {"id": False, "name": "root", "children": [1, 10]},
        1: menu(1, "sale.menu_root", [2]),
        2: menu(2, "sale.menu_sales", [3, 4]),
        3: menu(3, "my.menu_a", action="ir.actions.act_window,7"),
        4: menu(4, "sale.menu_orders", action="ir.actions.act_window,8"),
        10: menu(10, "my.menu_app", [11, 13], action="ir.actions.act_window,9"),
        11: menu(11, "my.menu_cfg", [12]),
        12: menu(12, "my.menu_b", action="ir.actions.act_window,10"),
        13: menu(13, "my.menu_url", action="ir.actions.act_url,5"),
    }
    own = {"my.menu_a", "my.menu_app", "my.menu_cfg", "my.menu_b", "my.menu_url", "my.menu_gone"}

    return menus, own


def test_targets_are_own_leaves_with_an_action():
    menus, own = sample()
    assert NS["_os_click_tree"](menus, own, False)["targets"] == [3, 12]
    assert NS["_os_click_tree"](menus, own, True)["targets"] == [3, 10, 12]


def test_an_edited_foreign_menu_is_not_the_modules():
    menus, own = sample()
    tree = NS["_os_click_tree"](menus, own, True)
    assert 4 not in tree["targets"]
    assert 4 not in tree["allowed"]
    assert "sale.menu_orders" not in tree["hidden"] + tree["unreached_static"]


def test_ancestors_are_the_way_in():
    menus, own = sample()
    tree = NS["_os_click_tree"](menus, own, False)
    assert {1, 2, 3, 10, 11, 12} <= tree["allowed"]
    assert 4 not in tree["allowed"]
    assert tree["roots"] == [1, 10]
    assert tree["root_action"] == {1: False, 10: True}


def test_menus_load_menus_hid_are_hidden():
    menus, own = sample()
    assert NS["_os_click_tree"](menus, own, False)["hidden"] == ["my.menu_gone"]


def test_a_url_action_is_never_a_target():
    menus, own = sample()
    tree = NS["_os_click_tree"](menus, own, True)
    assert 13 not in tree["targets"]
    assert 13 not in tree["allowed"]
    assert tree["unreached_static"] == ["my.menu_url"]


def test_the_action_model_form_is_read_too():
    """19 and 20 hand out `action_model` and `action_id`, not `action`."""
    menus, own = sample()
    for entry in list(menus.values())[1:]:
        action = entry.pop("action")
        entry["action_model"] = action.split(",")[0] if action else False
        entry["action_id"] = int(action.split(",")[1]) if action else False
    tree = NS["_os_click_tree"](menus, own, True)
    assert tree["targets"] == [3, 10, 12]
    assert tree["unreached_static"] == ["my.menu_url"]


def test_an_app_root_without_an_xmlid_cannot_be_started():
    """A root made in the UI has no xmlid, and the clickbot starts an app by one.

    `startClickEverywhere('')` would walk every app the browser is shown.
    """
    menus, own = sample()
    menus[1]["xmlid"] = ""
    tree = NS["_os_click_tree"](menus, own, False)
    assert tree["targets"] == [12]
    assert tree["roots"] == [10]
    assert 1 not in tree["allowed"]
    assert tree["unreached_static"] == ["my.menu_a", "my.menu_url"]


def test_a_root_with_no_target_left_is_not_a_root():
    menus, own = sample()
    tree = NS["_os_click_tree"](menus, own - {"my.menu_a"}, False)
    assert tree["roots"] == [10]
    assert 1 not in tree["allowed"]


def test_trim_is_a_copy_and_leaves_the_cache_entry_alone():
    menus, own = sample()
    before = copy.deepcopy(menus)
    tree = NS["_os_click_tree"](menus, own, False)
    trimmed = NS["_os_click_trim"](menus, tree["allowed"])
    assert menus == before
    assert trimmed["root"]["children"] == [1, 10]
    assert trimmed[2]["children"] == [3]
    assert trimmed[10]["children"] == [11]
    assert 4 not in trimmed
    trimmed[3]["name"] = "changed"
    assert menus[3]["name"] == "menu_a"


def test_shrink_drops_done_targets_and_empty_roots():
    menus, own = sample()
    tree = NS["_os_click_tree"](menus, own, False)
    allowed = NS["_os_click_shrink"](tree, {3})
    assert 1 not in allowed
    assert 3 not in allowed
    assert {10, 11, 12} <= allowed
    assert NS["_os_click_shrink"](tree, {3, 12}) == set()


@pytest.mark.parametrize("root_is_target", [False, True])
def test_an_own_root_stays_while_a_target_under_it_does(root_is_target):
    menus, own = sample()
    tree = NS["_os_click_tree"](menus, own, root_is_target)
    done = {3, 10} if root_is_target else {3}
    assert 10 in NS["_os_click_shrink"](tree, done)


@pytest.mark.parametrize(("line", "xmlid"), [
    # 15 and 16: console.log("Testing menu", description), joined by a space.
    ("Testing menu Sales Orders sale.menu_sale_order", "sale.menu_sale_order"),
    # 17 to 19: `Testing menu ${menuDescription}`, the same text.
    ("Testing menu Orders my.menu_a", "my.menu_a"),
    # 15 to 19 name the app root first.
    ("Testing app menu: sale.menu_root", "sale.menu_root"),
    # 20: `Testing menu ${menu.name} (${menu.xmlid})`.
    ("Testing menu Sales Orders (sale.menu_sale_order)", "sale.menu_sale_order"),
    ("Testing app: Sales (sale.menu_root)", "sale.menu_root"),
])
def test_each_versions_menu_line_is_read(line, xmlid):
    assert NS["_os_click_line"](line) == ("menu", xmlid)


def test_an_error_line_names_its_menu():
    assert NS["_os_click_line"]("Error while testing Orders my.menu_a") == ("error", "my.menu_a")
    block = ("Error found:\n - Current testing app is Sales (sale.menu_root)\n"
             " - Current testing menu is Orders (my.menu_a)\nThe error is :\nboom")
    assert NS["_os_click_line"](block) == ("error", "my.menu_a")


@pytest.mark.parametrize(("line", "key", "count"), [
    # Read off each event, every version, whether or not the run got to its end.
    ("Testing 9 filters", "filters", 9),  # 15 and 16 join the parts by a space
    ("Testing 0 filters", "filters", 0),
    ("Testing view switch: kanban", "views", 1),
    ("Modal detected: Configure my.menu_cfg", "modals", 1),  # 17 and later
    ("Modal detected: Configure (my.menu_cfg)", "modals", 1),  # 20
])
def test_counts_are_read_off_each_event(line, key, count):
    assert NS["_os_click_line"](line) == ("count", key, count)


@pytest.mark.parametrize("line", [
    # The totals: printed only by a run that got to its end, so a run restarted
    # past a failure would lose what its failed browsers clicked.
    "Successfully tested 14 menus",
    "Successfully tested 2  apps",
    "Tested 21 views",
    "Error found while testing 2 menus",
    "Clicking on: menu toggler",
    "test successful",
    "",
])
def test_unrelated_lines_are_none(line):
    assert NS["_os_click_line"](line) is None


def blame(logged, root, done=(), root_is_target=False):
    menus, own = sample()
    tree = NS["_os_click_tree"](menus, own, root_is_target)

    return NS["_os_click_blame"](logged, root, tree, menus, set(done))


def test_the_last_target_logged_is_blamed():
    assert blame(["sale.menu_root", "my.menu_a"], 1) == 3


def test_a_root_without_action_blames_its_first_open_target():
    assert blame(["sale.menu_root"], 1) == 3


def test_a_root_with_action_owns_its_failure():
    assert blame(["my.menu_app"], 10) is None


def test_an_own_root_that_is_a_target_is_blamed_itself():
    assert blame(["my.menu_app"], 10, root_is_target=True) == 10


def test_nothing_logged_ends_the_root():
    assert blame([], 1) is None


def test_blame_skips_targets_already_done():
    assert blame(["my.menu_app", "my.menu_b"], 10, done={12}) is None
    assert blame(["sale.menu_root"], 1, done={3}) is None


def test_two_roots_blame_independently():
    assert blame(["sale.menu_root", "my.menu_a"], 1) == 3
    assert blame(["my.menu_app", "my.menu_b"], 10, done={3}) == 12


def test_the_budget_ends_at_one_second():
    assert NS["_os_click_budget"](100.0, 40.0) == 60.0
    assert NS["_os_click_budget"](100.0, 99.0) is None
    assert NS["_os_click_budget"](100.0, 120.0) is None


class Base:
    def load_menus(self, debug):
        menus, _ = sample()

        return menus


class Model(Base):
    pass


class Own:
    def load_menus(self, debug):
        return {}


def install(cls, holder):
    before = NS["_os_click_before"](cls)
    original = cls.load_menus
    cls.load_menus = NS["_os_click_wrapper"](original, holder)

    return before


def test_an_inherited_method_is_deleted_not_set_back():
    before = install(Model, {"allowed": {1}})
    assert "load_menus" in Model.__dict__
    NS["_os_click_undo"](Model, before)
    assert "load_menus" not in Model.__dict__
    assert NS["_os_click_is_undone"](Model, before)


def test_an_own_method_is_put_back_as_the_same_object():
    original = Own.__dict__["load_menus"]
    before = install(Own, {"allowed": set()})
    assert Own.__dict__["load_menus"] is not original
    NS["_os_click_undo"](Own, before)
    assert Own.__dict__["load_menus"] is original
    assert NS["_os_click_is_undone"](Own, before)


def test_undo_twice_is_harmless():
    before = install(Model, {"allowed": set()})
    NS["_os_click_undo"](Model, before)
    NS["_os_click_undo"](Model, before)
    assert "load_menus" not in Model.__dict__
    assert "load_menus" in Base.__dict__


def test_is_undone_sees_a_leftover():
    before = NS["_os_click_before"](Model)
    Model.load_menus = Base.load_menus
    try:
        assert not NS["_os_click_is_undone"](Model, before)
    finally:
        del Model.load_menus
    own_before = NS["_os_click_before"](Own)
    saved = Own.__dict__["load_menus"]
    Own.load_menus = lambda self, debug: {}
    try:
        assert not NS["_os_click_is_undone"](Own, own_before)
    finally:
        Own.load_menus = saved


def test_the_wrapper_trims_to_what_the_holder_says_now():
    menus, own = sample()
    tree = NS["_os_click_tree"](menus, own, False)
    holder = {"allowed": tree["allowed"]}
    returned = []

    def original(self, debug):
        returned.append(sample()[0])

        return returned[-1]

    wrapper = NS["_os_click_wrapper"](original, holder)
    first = wrapper(None, "1")
    holder["allowed"] = NS["_os_click_shrink"](tree, {3})
    second = wrapper(None, "1")
    assert first["root"]["children"] == [1, 10]
    assert second["root"]["children"] == [10]
    assert returned[-1] == sample()[0]


SOURCES = Path("/opt/odoo-src")
START_17 = ("odoo.loader.modules.get('@web/webclient/clickbot/clickbot_loader')"
            ".startClickEverywhere('%s');")
FLAVOURS = {
    15: {"start": "odoo.__DEBUG__.services['web.clickEverywhere']('%s');", "url": "/web",
         "signal": None, "options": False, "error_checker": False, "own_browser": True,
         "root_is_target": True},
    17: {"start": START_17, "url": "/web", "signal": None, "options": False,
         "error_checker": False, "own_browser": False, "root_is_target": True},
    18: {"start": START_17, "url": "/odoo", "signal": "clickbot test succeeded",
         "options": False, "error_checker": False, "own_browser": False,
         "root_is_target": True},
    20: {"start": ("odoo.loader.modules.get('@web/webclient/clickbot/clickbot_loader')"
                   ".startClickEverywhere({ xmlId: '%s', logger: true });"),
         "url": "/odoo", "signal": "clickbot test succeeded", "options": True,
         "error_checker": True, "own_browser": False, "root_is_target": False},
}
FLAVOURS[16] = FLAVOURS[15]
FLAVOURS[19] = FLAVOURS[18]


def web_sources(major):
    web = SOURCES / f"odoo{major}" / "addons" / "web"
    loader = web / "static" / "src" / "webclient" / "clickbot" / "clickbot_loader.js"
    test = web / "tests" / "test_click_everywhere.py"
    if not loader.exists() or not test.exists():
        pytest.skip(f"no Odoo {major} source under {SOURCES}")

    return loader.read_text(encoding="utf-8"), test.read_text(encoding="utf-8")


@pytest.mark.parametrize("major", sorted(FLAVOURS))
def test_each_version_is_recognised(major):
    assert NS["_os_click_flavour"](*web_sources(major)) == FLAVOURS[major]


def test_an_unknown_loader_is_refused():
    loader, test = web_sources(19)
    with pytest.raises(ValueError, match="unknown clickbot"):
        NS["_os_click_flavour"](loader, "class TestMenusAdmin:\n    pass\n")
    with pytest.raises(ValueError, match="unknown clickbot"):
        NS["_os_click_flavour"]("export function somethingElse() {}", test)


def test_a_dialogs_html_is_read_as_text():
    line = 'Error: Error dialog detected<header class="x"><h4>Oops!</h4></header><main>Bad</main>'
    assert NS["_os_click_plain"](line) == "Error: Error dialog detected Oops! Bad"


def test_the_servers_error_comes_first_in_a_reason():
    run = NS["_os_click_new_run"]()
    run["server"] = "UndefinedColumn: column sale_integration.shared_stock does not exist"
    run["browser"] = ["Error while testing Shops my.menu_a",
                      "Error: Error dialog detected<h4>Oops!</h4>"]
    assert NS["_os_click_reason"](run, "The test code failed") == (
        "UndefinedColumn: column sale_integration.shared_stock does not exist; "
        "Error: Error dialog detected Oops!")


def test_the_line_that_ended_the_run_is_the_last_resort():
    run = NS["_os_click_new_run"]()
    assert NS["_os_click_reason"](run, "Script timeout exceeded") == "Script timeout exceeded"
    run["browser"] = ["Error while testing Shops my.menu_a"]
    assert NS["_os_click_reason"](run, "x") == "Error while testing Shops my.menu_a"
    run["browser"] = ["y" * 2000]
    assert len(NS["_os_click_reason"](run, "x")) == 800


def test_a_reason_leaves_the_js_stack_out():
    run = NS["_os_click_new_run"]()
    run["browser"] = ["Error: Error dialog detected<h4>Oops!</h4>\n    at errorDialog (x.js:16:7)"]
    assert NS["_os_click_reason"](run, "x") == "Error: Error dialog detected Oops!"


def error_record(name):
    import logging
    import sys

    try:
        raise ValueError("column x does not exist")
    except ValueError:
        exc_info = sys.exc_info()

    return logging.LogRecord(name, logging.ERROR, __file__, 1, "boom", None, exc_info)


def test_only_a_request_the_server_failed_is_a_reason():
    """Odoo logs a request it failed on `odoo.http`; other threads' errors are not the menu's."""
    state = {"run": NS["_os_click_new_run"](), "clickbot": {}}
    handler = NS["_OsClickHandler"](state)
    handler.emit(error_record("odoo.addons.bus.websocket"))
    handler.emit(error_record("odoo.service.server"))
    assert state["run"]["server"] is None
    handler.emit(error_record("odoo.http"))
    assert state["run"]["server"] == "ValueError: column x does not exist"
