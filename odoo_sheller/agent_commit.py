"""Which local containers a human has opened to every agent's commit.

One JSON file, `~/.odoo-sheller/agent-commit.json`, beside `targets.json`:

    {"version": 1,
     "containers": [{"container": "mig18", "since": "2026-10-16T10:00:00Z"}]}

A container here is a decision made ahead of time: "agents may commit on this
one". Every agent session on it holds the right without a human granting it
session by session — a migration that reopens its target before every upgrade
would otherwise need a click per session. Only a human writes this file, behind
the admin key; anyone may read it, since a client has to learn whether it was
allowed before it opens a session.

The key is the container's name as `docker ps` reports it, not its id, so a
switch outlives `docker compose down/up`. That also means a different container
started later under the same name inherits it; that was weighed and accepted.
Nothing here ever reaches a remote target: the registry matches it against
local Docker sessions only.

The file follows the rules `targets.py` sets for its own: written whole and
moved into place, private from the first byte, and left alone when it cannot be
read — overwriting it would turn a hand edit gone wrong into lost switches.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from odoo_sheller.journal import JOURNAL_ROOT
from odoo_sheller.targets import write_json_atomically

AGENT_COMMIT_PATH = JOURNAL_ROOT.parent / "agent-commit.json"
VERSION = 1

# Docker's own rule for a container name. Anything else cannot be one, and a
# name that is not checked could be a path or a flag by the time it is used.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class AgentCommitError(Exception):
    """The file exists and cannot be used as it is. It has not been touched."""


def check_container_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not a container name docker would give")

    return name


class AgentCommitStore:
    def __init__(self, path: Path = AGENT_COMMIT_PATH):
        self.path = Path(path)

    def _refuse(self, why: str) -> AgentCommitError:

        return AgentCommitError(
            f"{self.path.name} {why}; it has been left as it is — fix it by hand "
            f"or remove {self.path}"
        )

    def _read(self) -> dict:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:

            return {"version": VERSION, "containers": []}
        except OSError as exc:
            raise self._refuse(f"cannot be read ({exc})") from None
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise self._refuse(f"is not valid JSON ({exc})") from None
        if not isinstance(data, dict):
            raise self._refuse("is not a JSON object")
        if data.get("version") != VERSION:
            raise self._refuse(
                f"has version {data.get('version')!r}; this daemon reads version {VERSION}"
            )
        if not isinstance(data.setdefault("containers", []), list):
            raise self._refuse("has 'containers' that is not a list")
        for entry in data["containers"]:
            if not (
                isinstance(entry, dict)
                and isinstance(entry.get("container"), str)
                and isinstance(entry.get("since"), str)
            ):
                raise self._refuse("has an entry without a container and a since")

        return data

    def entries(self) -> list[dict]:
        """Every container switched on, by name. Reading never creates the file."""

        return sorted(
            ({"container": e["container"], "since": e["since"]} for e in self._read()["containers"]),
            key=lambda entry: entry["container"],
        )

    def is_on(self, container: str | None) -> bool:
        if not container:

            return False

        return any(e["container"] == container for e in self._read()["containers"])

    def turn_on(self, container: str) -> dict:
        """Switch a container on. On already, it keeps the time it was first set."""
        check_container_name(container)
        data = self._read()
        for entry in data["containers"]:
            if entry["container"] == container:

                return {"container": container, "since": entry["since"]}
        entry = {
            "container": container,
            "since": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        data["containers"].append(entry)
        write_json_atomically(self.path, data, prefix=".agent-commit-")

        return dict(entry)

    def turn_off(self, container: str) -> bool:
        """Switch a container off; whether it was on."""
        data = self._read()
        kept = [e for e in data["containers"] if e["container"] != container]
        if len(kept) == len(data["containers"]):

            return False
        data["containers"] = kept
        write_json_atomically(self.path, data, prefix=".agent-commit-")

        return True
