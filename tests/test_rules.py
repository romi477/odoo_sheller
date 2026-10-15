import re

import pytest

from odoo_sheller import rules
from odoo_sheller.discovery import SUPPORTED_MAJORS


@pytest.mark.parametrize(
    ("version", "major"),
    [
        ("17.0", 17),
        ("17.0+e", 17),
        ("saas~17.2", 17),
        ("19.0-20251014", 19),
        ("unknown", None),
        ("", None),
        (None, None),
    ],
)
def test_major_of_reads_what_the_bootstrap_reports(version, major):
    assert rules.major_of(version) == major


def test_every_supported_major_has_its_rules():
    assert set(SUPPORTED_MAJORS) <= set(rules.majors())


def test_rules_for_a_major_lead_with_the_common_part():
    text = rules.for_major(17)
    assert text.startswith(rules.common())
    assert "# Odoo 17" in text


def test_a_major_without_a_file_has_no_rules():
    assert rules.for_major(14) is None
    assert rules.for_major(None) is None


@pytest.mark.parametrize("major", SUPPORTED_MAJORS)
def test_each_file_names_its_own_major_and_no_other_heading(major):
    """One file is one version. A `# Odoo 18` heading inside 17.md would tell
    an agent on 17 that it is reading 18's rules."""
    text = (rules.RULES / f"{major}.md").read_text()
    assert re.findall(r"^# .*$", text, re.MULTILINE) == [f"# Odoo {major}"]


@pytest.mark.parametrize("major", SUPPORTED_MAJORS)
def test_each_file_covers_python_and_xml(major):
    text = (rules.RULES / f"{major}.md").read_text()
    assert "## Python" in text
    assert "## XML" in text


@pytest.mark.parametrize(
    ("major", "says"),
    [
        # The spellings that move, pinned where an agent most often guesses.
        (15, "env['base'].flush()"),
        (16, "env.flush_all()"),
        (17, 'Since 17.0, the "attrs" and "states" attributes are no longer used.'),
        (18, "<list"),
        (19, "models.Constraint"),
        (20, "ir.access"),
        (20, '<i class="oi" data-icon='),
        (20, "addons/web/tooling/icons/icons_wishlist.txt"),
    ],
)
def test_the_spellings_that_move_are_where_they_belong(major, says):
    assert says in (rules.RULES / f"{major}.md").read_text()


def test_the_common_part_says_how_module_code_is_written_on_any_major():
    """The approach, not a spelling: what holds on every major and is easy to
    skip in a hurry. The shell is superuser, so a missing access row or a
    `sudo()` that should not be there never shows in a session."""
    common = rules.common()
    section = re.search(r"^## How module code is written\n(.*?)(?=^## |\Z)", common, re.MULTILINE | re.DOTALL)
    assert section is not None
    text = section.group(1)
    for says in (
        "_inherit", "xpath", "UserError", "ValidationError", "_()",
        "@api.constrains", "access row", "_logger", "os_run_test",
        "blank line before the final `return`",
    ):
        assert says in text, says
    assert common.index("## How module code is written") < common.index("## On every major")


def test_the_common_part_names_no_file_that_a_major_renames():
    """It goes in front of every major. 20 has no `ir.model.access.csv` and no
    `ir.rule`; naming them here would contradict the file that follows."""
    common = rules.common()
    for gone_in_20 in ("ir.model.access.csv", "ir.rule"):
        assert gone_in_20 not in common, gone_in_20


def test_odoo_20_says_font_awesome_draws_nothing():
    """A `fa fa-*` class is no error on 20, only an empty space where the icon
    was: the one change an agent cannot notice from a log."""
    text = (rules.RULES / "20.md").read_text()
    for says in ("fa fa-", "icon=", "oi-filled", "oi-fw", "snake_case", ".oi", "grep -rnE"):
        assert says in text, says
