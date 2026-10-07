"""The cards a person writes down: where a remote instance is, kept in a file.

One JSON file, `~/.odoo-sheller/targets.json`, beside the admin key and the
journals — a handful of records, never queried, so a database would be weight
for nothing. One key per kind of target:

    {"version": 1,
     "odoosh": [{"build": "36887345", "host": "build-36887345.dev.odoo.com"}],
     "ssh": [{"id": "ssh-3f9a01cc", "name": "acme prod",
              "access": "ssh -i ~/.ssh/acme.pem ubuntu@acme.example.com sudo -n -u odoo -H",
              "launch": "/opt/odoo/env/bin/python /opt/odoo/odoo-bin shell -c /opt/odoo/odoo.conf",
              "database": null, "stage": "production"}]}

An ssh card holds what its author *wrote*, not what it parses to: they edit it
later, and `~` is theirs. It is parsed (`recipe.py`) when it is saved, to refuse
what the grammar refuses, and again wherever it is used.

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
import secrets
import tempfile
from collections.abc import Callable
from pathlib import Path

from odoo_sheller.journal import JOURNAL_ROOT
from odoo_sheller.names import check_ssh_name
from odoo_sheller.recipe import Access, Launch, parse_access, parse_launch

TARGETS_PATH = JOURNAL_ROOT.parent / "targets.json"
VERSION = 1

ODOOSH_PREFIX = "odoosh-"
SSH_PREFIX = "ssh-"

# What a human may declare a plain server to be. Nothing on one says what it
# is, so the default is the dangerous one: production refuses a commit outright.
STAGES = ("production", "staging", "development")
DEFAULT_STAGE = "production"
MAX_NAME = 60
SSH_FIELDS = ("name", "access", "launch", "database", "stage")


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


def _ssh_card(entry: dict) -> dict:

    return {
        "id": entry["id"],
        "kind": "ssh",
        "name": entry["name"],
        "access": entry["access"],
        "launch": entry["launch"],
        "database": entry["database"],
        "stage": entry["stage"],
    }


class TargetStore:
    def __init__(self, path: Path = TARGETS_PATH, is_file: Callable[[str], bool] = os.path.isfile):
        self.path = Path(path)
        # Whether a key the card names exists: the one thing the grammar asks
        # this machine, and so the one thing a test replaces.
        self._is_file = is_file

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
        for entry in data["ssh"]:
            if not (
                isinstance(entry, dict)
                and isinstance(entry.get("id"), str)
                and entry["id"].startswith(SSH_PREFIX)
                and all(isinstance(entry.get(key), str) for key in ("name", "access", "launch"))
                and (entry.get("database") is None or isinstance(entry["database"], str))
                and entry.get("stage") in STAGES
            ):
                raise self._refuse("has an ssh entry that is not a card")
            entry.setdefault("database", None)

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
        """Every card — odoo.sh, then ssh — each kind newest first. Reading never
        creates the file."""
        data = self._read()

        return [_card(e) for e in data["odoosh"]] + [_ssh_card(e) for e in data["ssh"]]

    def get(self, target_id: str) -> dict:
        data = self._read()
        if target_id.startswith(SSH_PREFIX):
            for entry in data["ssh"]:
                if entry["id"] == target_id:

                    return _ssh_card(entry)
            raise KeyError(target_id)
        build = self._build_of(target_id)
        for entry in data["odoosh"]:
            if entry["build"] == build:

                return _card(entry)
        raise KeyError(target_id)

    def parse_access(self, text: str) -> Access:
        """Parse an Access field, asking this machine whether the key is there."""

        return parse_access(text, is_file=self._is_file)

    def parse_launch(self, text: str, database: str | None = None) -> Launch:

        return parse_launch(text, database=database)

    def recipe(self, target_id: str) -> tuple[Access, Launch]:
        """An ssh card, parsed. A card that stopped parsing — a key file that
        has gone — says so here, when it is about to be used."""
        if not target_id.startswith(SSH_PREFIX):
            raise KeyError(target_id)
        card = self.get(target_id)

        return self.parse_access(card["access"]), self.parse_launch(card["launch"], card["database"])

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

    def _checked_ssh(self, data: dict, fields: dict, own_id: str | None) -> dict:
        """The fields of an ssh card, or ValueError saying which is wrong."""
        name = fields["name"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError("an ssh card needs a name")
        name = name.strip()
        if len(name) > MAX_NAME or not name.isprintable():
            raise ValueError(
                f"the card's name is at most {MAX_NAME} printable characters, on one line"
            )
        for other in data["ssh"]:
            if other["id"] != own_id and other["name"].casefold() == name.casefold():
                raise ValueError(f"a card named {other['name']!r} already exists")
        if fields["stage"] not in STAGES:
            raise ValueError(f"stage must be one of {', '.join(STAGES)}")
        database = fields.get("database") or None
        self.parse_access(fields["access"])
        self.parse_launch(fields["launch"], database)

        return {
            "name": name,
            "access": fields["access"],
            "launch": fields["launch"],
            "database": database.strip() if isinstance(database, str) else None,
            "stage": fields["stage"],
        }

    def add_ssh(
        self,
        name: str,
        access: str,
        launch: str,
        database: str | None = None,
        stage: str = DEFAULT_STAGE,
    ) -> dict:
        data = self._read()
        fields = self._checked_ssh(
            data,
            {
                "name": name, "access": access, "launch": launch,
                "database": database, "stage": stage,
            },
            None,
        )
        entry = {"id": f"{SSH_PREFIX}{secrets.token_hex(4)}", **fields}
        data["ssh"] = [entry, *data["ssh"]]
        self._write(data)

        return _ssh_card(entry)

    def update(self, target_id: str, fields: dict) -> dict:
        """What can change on a card. An odoo.sh card's host; the build is its
        identity, so a different build is a different card — deleted and added,
        not renamed. An ssh card's fields, checked together afterwards: a
        change that makes the whole recipe refused changes nothing."""
        data = self._read()
        if target_id.startswith(SSH_PREFIX):
            entry = next((e for e in data["ssh"] if e["id"] == target_id), None)
            if entry is None:
                raise KeyError(target_id)
            unknown = sorted(set(fields) - set(SSH_FIELDS))
            if unknown:
                raise ValueError(f"an ssh card has no {unknown[0]!r} to change")
            merged = self._checked_ssh(data, {**entry, **fields}, target_id)
            entry.update(merged)
            self._write(data)

            return _ssh_card(entry)
        build = self._build_of(target_id)
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
        data = self._read()
        if target_id.startswith(SSH_PREFIX):
            kept = [entry for entry in data["ssh"] if entry["id"] != target_id]
            if len(kept) == len(data["ssh"]):
                raise KeyError(target_id)
            data["ssh"] = kept
        else:
            build = self._build_of(target_id)
            kept = [entry for entry in data["odoosh"] if entry["build"] != build]
            if len(kept) == len(data["odoosh"]):
                raise KeyError(target_id)
            data["odoosh"] = kept
        self._write(data)
