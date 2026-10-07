"""What may stand for a build or a host by the time ssh sees it.

ssh reads an argument that begins with `-` as an option, and `-oProxyCommand=…`
is an option that runs a command on *this* machine — so a name begins with a
letter or a digit and holds nothing a shell or ssh would read as anything else.
It lives apart from `transport.py` and `recipe.py` because both need it and
neither may import the other.
"""

import re

SSH_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def check_ssh_name(label: str, value: object) -> str:
    """The value, if it is a name; ValueError saying so if it is not."""
    if not isinstance(value, str) or SSH_NAME_RE.fullmatch(value) is None:
        raise ValueError(
            f"{label} {value!r} is not a name: letters, digits, '.', '_' and '-', "
            "starting with a letter or a digit"
        )

    return value
