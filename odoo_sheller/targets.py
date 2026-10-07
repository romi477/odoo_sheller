"""The cards a person writes down: where a remote instance is, kept in a file.

One JSON file, `~/.odoo-sheller/targets.json`, beside the admin key and the
journals — a handful of records, never queried, so a database would be weight
for nothing. One key per kind of target:

    {"version": 1,
     "odoosh": [{"build": "36887345", "host": "build-36887345.dev.odoo.com"}],
     "ssh": []}

It holds no secrets: no key material, no passwords, not the admin key. It
replaces what the UI used to keep in `localStorage`, which a desktop-app frame
does not reliably keep.

Two rules about the file itself. It is written whole to a temporary file in the
same directory and moved over the old one, so a crash leaves either version and
never half of one; the temporary file is created private, so it is never
readable by anyone else for even an instant. And a file this code cannot read —
a hand edit gone wrong, a version from the future — is reported and left alone:
writing over it would turn a typo into lost cards.

Everything here is synchronous and awaits nothing, so on the daemon's one event
loop a read-change-write cannot be interleaved with another.
"""

import contextlib
import json
import os
import tempfile
from pathlib import Path

from odoo_sheller.journal import JOURNAL_ROOT
from odoo_sheller.transport import check_ssh_name

TARGETS_PATH = JOURNAL_ROOT.parent / "targets.json"
VERSION = 1

ODOOSH_PREFIX = "odoosh-"


class TargetsError(Exception):
    """The file exists and cannot be used as it is. It has not been touched."""


def _card(entry: dict) -> dict:
    """What the API calls an odoo.sh card. The id is derived: a build is unique."""

    return {
        "id": f"{ODOOSH_PREFIX}{entry['build']}",
        "kind": "odoosh",
        "name": entry["build"],
        "build": entry["build"],
        "host": entry["host"],
    }


class TargetStore:
    def __init__(self, path: Path = TARGETS_PATH):
        self.path = Path(path)

    def _refuse(self, why: str) -> TargetsError:

        return TargetsError(
            f"{self.path.name} {why}; it has been left as it is — fix it by hand "
            f"or remove {self.path}"
        )

    def _read(self) -> dict:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:

            return {"version": VERSION, "odoosh": [], "ssh": []}
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
        for key in ("odoosh", "ssh"):
            if not isinstance(data.setdefault(key, []), list):
                raise self._refuse(f"has a {key!r} that is not a list")
        for entry in data["odoosh"]:
            if not (
                isinstance(entry, dict)
                and isinstance(entry.get("build"), str)
                and isinstance(entry.get("host"), str)
            ):
                raise self._refuse("has an odoosh entry without a build and a host")

        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp creates the file 0600, which is the mode it keeps.
        fd, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=".targets-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise

    def _build_of(self, target_id: str) -> str:
        if not target_id.startswith(ODOOSH_PREFIX):
            raise KeyError(target_id)

        return target_id[len(ODOOSH_PREFIX):]

    def list(self) -> list[dict]:
        """Newest first. Reading never creates the file."""

        return [_card(entry) for entry in self._read()["odoosh"]]

    def get(self, target_id: str) -> dict:
        build = self._build_of(target_id)
        for entry in self._read()["odoosh"]:
            if entry["build"] == build:

                return _card(entry)
        raise KeyError(target_id)

    def add_odoosh(self, build: str, host: str) -> dict:
        """Adds a build, or updates the one already there and moves it first."""
        check_ssh_name("build", build)
        check_ssh_name("host", host)
        data = self._read()
        data["odoosh"] = [
            {"build": build, "host": host},
            *(entry for entry in data["odoosh"] if entry["build"] != build),
        ]
        self._write(data)

        return _card(data["odoosh"][0])

    def update(self, target_id: str, fields: dict) -> dict:
        """The host of a build. The build is the card's identity: a different
        build is a different card, so it is deleted and added, not renamed."""
        build = self._build_of(target_id)
        data = self._read()
        entry = next((e for e in data["odoosh"] if e["build"] == build), None)
        if entry is None:
            raise KeyError(target_id)
        if fields.get("build", build) != build:
            raise ValueError(
                "a card's build is its identity and cannot change: delete the "
                "card and add another"
            )
        unknown = sorted(set(fields) - {"build", "host"})
        if unknown:
            raise ValueError(f"an odoo.sh card has no {unknown[0]!r} to change")
        if "host" in fields:
            entry["host"] = check_ssh_name("host", fields["host"])
            self._write(data)

        return _card(entry)

    def delete(self, target_id: str) -> None:
        build = self._build_of(target_id)
        data = self._read()
        kept = [entry for entry in data["odoosh"] if entry["build"] != build]
        if len(kept) == len(data["odoosh"]):
            raise KeyError(target_id)
        data["odoosh"] = kept
        self._write(data)
