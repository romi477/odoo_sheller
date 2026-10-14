"""What an Odoo major expects of the Python and XML an agent writes.

The same model writes code for 15 and for 19 in one afternoon, and a name it
remembers from one is a mistake on the other: `attrs` is gone in 17, `<tree>`
in 18, `_sql_constraints` in 19. Each major is one self-contained file here,
`common.md` goes in front of it, and the MCP server hands the pair over with
the first session it opens on that major. Prose, not code: nothing here is
parsed beyond picking the file.
"""

import re
from pathlib import Path

RULES = Path(__file__).resolve().parent

# What the bootstrap reports is `release.version`: `17.0`, `17.0+e`, an
# online `saas~17.2`, a nightly with a date behind it. The major is the first
# number in front of a dot; an online minor reads as the major it branched
# from, which is closer than the one after it.
_MAJOR = re.compile(r"(\d+)\.\d+")


def major_of(version: str | None) -> int | None:
    match = _MAJOR.search(version or "")

    return int(match.group(1)) if match else None


def majors() -> list[int]:

    return sorted(int(path.stem) for path in RULES.glob("*.md") if path.stem.isdigit())


def common() -> str:

    return (RULES / "common.md").read_text()


def for_major(major: int | None) -> str | None:
    """`common.md` and the major's own file, or None when there is no file."""
    if major is None:

        return None
    path = RULES / f"{major}.md"
    if not path.is_file():

        return None

    return common() + "\n" + path.read_text()
