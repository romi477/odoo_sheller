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
    ],
)
def test_the_spellings_that_move_are_where_they_belong(major, says):
    assert says in (rules.RULES / f"{major}.md").read_text()
