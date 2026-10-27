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
    ("Successfully tested 2  apps", "apps", 2),  # 15 joins " apps" with a space
    ("Successfully tested 14 menus", "menus", 14),
    ("Successfully tested 3 modals", "modals", 3),
    ("Successfully tested 9 filters", "filters", 9),
    ("Tested 21 views", "views", 21),
    ("Tested 4 form views", "form_views", 4),
    ("Tested 5 new record views", "new_record_views", 5),
])
def test_counts_are_read_from_both_wordings(line, key, count):
    assert NS["_os_click_line"](line) == ("count", key, count)


@pytest.mark.parametrize("line", [
    "Testing 9 filters",
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
