"""Append-only JSONL record of everything a session did.

Journals are unmasked: they can contain credentials read out of the database.
They stay local, out of git, and are reviewed before being shared.
"""

import json
import logging
import re
import threading
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

JOURNAL_ROOT = Path.home() / ".odoo-sheller" / "journals"

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slug(text: str) -> str:
    return _SAFE.sub("_", text)[:40]


def journal_path(
    root: Path, session_id: str, container: str, database: str, opened_at: datetime
) -> Path:
    stamp = opened_at.strftime("%Y-%m-%dT%H-%M-%S")

    return root / f"{stamp}-{_slug(container)}-{_slug(database)}-{session_id}.jsonl"


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, **fields) -> None:
        record = {"ts": datetime.now(UTC).isoformat(), "kind": kind}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def iter_records(self) -> Iterator[dict]:
        """The records one at a time, skipping any line that is not one.

        A daemon killed mid-write, or a full disk, leaves a last line cut in
        half. One such line used to raise out of every reader: the journal
        list, every closed session's history and every export answered 500
        until the file was found and fixed by hand. The rest of the file is
        still the record, so the broken line is passed over, not trusted.
        """
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    logger.warning("%s:%d is not a journal record; skipped", self.path, number)
                    continue
                if isinstance(record, dict):
                    yield record

    def records(self) -> list[dict]:

        return list(self.iter_records())

    def first(self, kind: str) -> dict | None:
        """The first record of a kind, reading no further than it.

        `session_open` sits in the first lines of a file that can reach a
        hundred megabytes; where a session ran is a question for its head.
        """
        for record in self.iter_records():
            if record.get("kind") == kind:

                return record

        return None


def _duration_seconds(first_ts: str | None, last_ts: str | None) -> float | None:
    if not first_ts or not last_ts:
        return None
    try:
        first = datetime.fromisoformat(first_ts)
        last = datetime.fromisoformat(last_ts)
    except (TypeError, ValueError):
        return None

    return (last - first).total_seconds()


def session_meta(records: Iterable[dict], session_id: str | None = None) -> dict:
    """Who and what this journal is about.

    Every way of getting a journal out — the API history, the JSONL export, the
    Markdown transcript — carries this, so a transcript is never an anonymous
    wall of commands.

    One pass over any iterable, so a listing can stream a file through it
    rather than hold the whole of it in memory.
    """
    opened: dict = {}
    closed: dict = {}
    owners: list[dict] = []
    first_ts = last_ts = None
    seen_any = False
    commands = 0
    committed = False
    for record in records:
        if not seen_any:
            first_ts = record.get("ts")
            seen_any = True
        last_ts = record.get("ts")
        kind = record.get("kind")
        if kind == "session_open" and not opened:
            opened = record
            if record.get("owner"):
                owners.insert(0, dict(record["owner"]))
        elif kind in ("session_close", "session_died"):
            closed = record
        elif kind == "owner_changed" and record.get("to"):
            owners.append(dict(record["to"]))
        elif kind in ("exec", "run_test"):
            commands += 1
        elif kind == "commit" and not record.get("error"):
            # A commit that failed wrote nothing; counting its record made the
            # journal list say "committed" over a transaction that was not.
            committed = True

    return {
        "session_id": session_id,
        "owner": owners[-1] if owners else None,
        "owners_seen": owners,
        "allow_commit": opened.get("allow_commit"),
        "container": opened.get("container"),
        "database": opened.get("database"),
        "odoo_bin": opened.get("odoo_bin"),
        "odoo": opened.get("odoo"),
        "python": opened.get("python"),
        "pid": opened.get("pid"),
        "opened_at": opened.get("ts"),
        "closed_at": closed.get("ts"),
        "ended_as": closed.get("kind"),
        "duration": _duration_seconds(first_ts, last_ts),
        "commands": commands,
        "committed": committed,
        "unmasked": True,
    }


def session_id_of(path: Path) -> str:
    """The session id a journal's filename ends in — the one way it is read."""

    return path.stem.rsplit("-", 1)[-1]


def find_journal(root: Path, session_id: str) -> Path | None:
    """The journal file of one session, by its name alone.

    Opening every journal to find one is what made a closed session's history
    cost seconds on a well-used machine: two thousand files, 1.4 GB, parsed
    in full to answer a question the filename already answers.
    """
    for path in root.glob("*.jsonl"):
        if session_id_of(path) == session_id:

            return path

    return None


# path -> ((mtime_ns, size), entry). A journal is append-only, so a file whose
# size and mtime have not moved summarises the same as last time; only the
# live ones and the new ones are read again.
_LISTING: dict[str, tuple[tuple[int, int], dict]] = {}
_LISTING_LOCK = threading.Lock()


def _summarise(path: Path, size: int) -> dict:
    lines = 0

    def counted(records):
        nonlocal lines
        for record in records:
            lines += 1
            yield record

    meta = session_meta(counted(Journal(path).iter_records()), session_id_of(path))

    return {
        **meta,
        "path": str(path),
        "container": meta["container"] or "?",
        "database": meta["database"] or "?",
        "odoo": meta["odoo"] or "?",
        # The file, not the session: what deleting it frees. Kept out of
        # session_meta, which every export carries.
        "lines": lines,
        "bytes": size,
    }


def list_journals(root: Path) -> list[dict]:
    entries = []
    seen = set()
    for path in sorted(root.glob("*.jsonl"), reverse=True):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue  # deleted between the glob and here
        key = str(path)
        seen.add(key)
        stamp = (stat.st_mtime_ns, stat.st_size)
        with _LISTING_LOCK:
            cached = _LISTING.get(key)
        if cached is not None and cached[0] == stamp:
            entry = cached[1]
        else:
            entry = _summarise(path, stat.st_size)
            with _LISTING_LOCK:
                _LISTING[key] = (stamp, entry)
        entries.append(dict(entry))
    with _LISTING_LOCK:
        for key in [key for key in _LISTING if Path(key).parent == root and key not in seen]:
            del _LISTING[key]

    return entries


def delete_journal(root: Path, session_id: str) -> Path:
    """Unlink the on-disk file for a finished session. Raises if it is gone.

    The id is compared, never globbed: interpolating it into a pattern let a
    `*` in the URL match every journal and unlink an unrelated one. The id is
    derived from the filename exactly as `list_journals` derives it, so the row
    a caller saw and the file that goes away are the same file.
    """
    path = find_journal(root, session_id)
    if path is None:
        raise FileNotFoundError(session_id)
    path.unlink()
    with _LISTING_LOCK:
        _LISTING.pop(str(path), None)

    return path


def _markdown_header(meta: dict) -> list[str]:
    rows = [
        ("Session", meta.get("session_id")),
        ("Container", meta.get("container")),
        ("Database", meta.get("database")),
        ("Odoo", meta.get("odoo")),
        ("Python", meta.get("python")),
        ("Opened", meta.get("opened_at")),
        ("Closed", meta.get("closed_at")),
        ("Ended as", meta.get("ended_as")),
        ("Commands", meta.get("commands")),
        ("Committed", "yes" if meta.get("committed") else "no"),
    ]
    title = (
        f"# Session {meta.get('session_id') or '?'}"
        f" — {meta.get('container') or '?'} / {meta.get('database') or '?'}\n"
    )
    lines = [title, "| | |", "|---|---|"]
    lines += [f"| {label} | {value} |" for label, value in rows if value is not None]
    lines.append(
        "\n> Unmasked transcript: it can contain credentials read from the database.\n"
    )

    return lines


def _named(actor: dict) -> str:
    """What to call an owner in a transcript.

    The daemon names an owner that arrives without a label after its kind, but
    journals written before it did are on disk and are never rewritten. They
    read `by agent (None)`, which names nobody and looks like a hole in the
    record rather than in the record-keeping.
    """

    return actor.get("label") or actor.get("kind") or "unknown"


def to_markdown(records: list[dict], meta: dict | None = None) -> str:
    lines = list(_markdown_header(meta)) if meta else []
    ordinals = {}
    next_ordinal = 0
    pending_log: list[str] | None = None

    def flush_log():
        nonlocal pending_log
        if pending_log:
            lines.append("Odoo log:\n")
            lines.append("```\n" + "\n".join(pending_log).rstrip() + "\n```\n")
        pending_log = None

    for record in records:
        kind = record["kind"]
        stamp = record.get("ts", "")
        if kind != "stderr":
            # A run of log lines belongs where it happened, so it is written
            # out before whatever came next.
            flush_log()
        if kind == "session_open":
            if meta:  # already stated in the header
                continue
            lines.append(f"# Session on {record.get('container')} / {record.get('database')}")
            lines.append(f"Odoo {record.get('odoo')} — opened {stamp}\n")
        elif kind == "exec":
            next_ordinal += 1
            ordinals[record.get("id")] = next_ordinal
            actor = record.get("actor") or {}
            by = f" by {actor.get('kind')} ({_named(actor)})" if actor else ""
            lines.append(f"## Command {next_ordinal}{by} — {stamp}\n")
            lines.append(f"```python\n{record.get('code', '').rstrip()}\n```\n")
        elif kind == "run_test":
            next_ordinal += 1
            ordinals[record.get("id")] = next_ordinal
            actor = record.get("actor") or {}
            by = f" by {actor.get('kind')} ({_named(actor)})" if actor else ""
            spec = ".".join(
                part for part in (
                    record.get("module"), record.get("test_class"), record.get("test_method"),
                ) if part
            )
            lines.append(f"## Test {next_ordinal}{by} — `{spec}` — {stamp}\n")
        elif kind in ("result", "abandoned_result"):
            if kind == "abandoned_result":
                n = ordinals.get(record.get("id"), record.get("id"))
                lines.append(
                    f"**Late result of command {n}**, abandoned at timeout "
                    f"— {stamp}\n"
                )
            if record.get("stdout"):
                lines.append(f"```\n{record['stdout'].rstrip()}\n```\n")
            if record.get("result"):
                lines.append(f"Result: `{record['result']}`\n")
            if record.get("test"):
                t = record["test"]
                lines.append(
                    f"Tests: {t.get('tests_run')} run, {t.get('failures')} failed, "
                    f"{t.get('errors')} errors, {t.get('skipped')} skipped — "
                    f"{'PASS' if t.get('success') else 'FAIL'}\n"
                )
            if record.get("error"):
                error = record["error"]
                if not isinstance(error, dict):
                    error = {"message": str(error)}
                trace = (error.get("traceback") or "").rstrip()
                if trace:
                    lines.append(f"```\n{trace}\n```\n")
                else:
                    # A timeout, a refused test runner, a frame too large:
                    # no traceback, so the message is the whole story — and
                    # an empty block used to be all the transcript kept.
                    lines.append(
                        f"**{error.get('type') or 'Error'}**: {error.get('message') or ''}\n"
                    )
            lines.append(f"_{record.get('duration', 0):.3f}s_\n")
        elif kind in ("commit", "rollback"):
            lines.append(f"**Transaction {kind}** — {stamp}\n")
        elif kind == "interrupt":
            lines.append(f"**Interrupted** — {stamp}\n")
        elif kind == "owner_changed":
            was = record.get("from") or {}
            now = record.get("to") or {}
            lines.append(
                f"**Ownership moved** from {was.get('kind')} ({_named(was)}) "
                f"to {now.get('kind')} ({_named(now)}), "
                f"{record.get('pending_commands', 0)} command(s) pending — {stamp}\n"
            )
        elif kind == "policy_changed":
            allowed = "granted" if record.get("allow_commit") else "revoked"
            lines.append(f"**Commit right {allowed}** — {stamp}\n")
        elif kind == "timeout":
            n = ordinals.get(record.get("id"), record.get("id"))
            lines.append(
                f"**Command {n} exceeded {record.get('seconds')}s "
                f"and was interrupted** — {stamp}\n"
            )
        elif kind == "stderr":
            # Markdown is the default export, and dropping these made the
            # transcript claim Odoo logged nothing — including the tracebacks
            # Odoo logs rather than raises. Consecutive lines are one block.
            line = record.get("line", "")
            if pending_log is None:
                pending_log = [line]
            else:
                pending_log.append(line)
            continue
        elif kind in ("session_close", "session_died"):
            lines.append(f"**{kind.replace('_', ' ').title()}** — {stamp}\n")
    flush_log()

    return "\n".join(lines)


_FEED_SKIP = frozenset({
    "session_open",
    "session_close",
    "session_died",
    "stderr",
    "interrupt",
})
_RESULT_DROP = frozenset({"kind", "t", "ts"})
_TIMEOUT_ERROR = {
    "type": "TimeoutError",
    "message": "Command exceeded its ceiling and was interrupted.",
    "traceback": "",
}


def target_from_records(records: list[dict]) -> dict | None:
    """Where a session ran, so a replacement can be opened on the same target.

    `kind` comes along because a remote target must not be rebuilt from
    fields that merely look right: a journal records the identity slot, not
    how to reach a build over SSH.
    """
    opened = next((r for r in records if r.get("kind") == "session_open"), None)
    if not opened or not opened.get("container") or not opened.get("database"):

        return None

    return {
        "container": opened["container"],
        "database": opened["database"],
        "odoo_bin": opened.get("odoo_bin"),
        "target_kind": opened.get("target_kind") or "docker",
        "host": opened.get("host"),
    }


FEED_LOG_TAIL = 2000


def feed_from_records(
    records: list[dict], include_logs: bool = False, log_tail: int | None = FEED_LOG_TAIL
) -> dict:
    """Rebuild editor history and feed entries from a session journal.

    `log_tail` caps how many stderr lines come back: a long session, or one run
    at debug level, journals them without limit and the browser wants the end.
    """
    history = []
    entries = []
    logs = []
    pending = {}
    # One counter for both command kinds. Counting `history` (exec only) and
    # `entries` (which commit and handovers also grow) separately let the two
    # drift into duplicate numbers, and disagree with `to_markdown`.
    commands = 0
    for record in records:
        kind = record.get("kind")
        if kind == "stderr":
            if include_logs:
                logs.append({"ts": record.get("ts"), "line": record.get("line", "")})
            continue
        if kind in _FEED_SKIP:
            continue
        if kind == "exec":
            request_id = record.get("id")
            commands += 1
            entry = {
                "kind": "exec",
                "id": request_id,
                "ordinal": commands,
                # When it was sent. The feed shows it, and a session restored
                # from its journal would otherwise have no date at all.
                "ts": record.get("ts"),
                "code": record.get("code", ""),
                "status": "running",
                "result": None,
                "actor": record.get("actor"),
            }
            entries.append(entry)
            history.append(entry["code"])
            pending[request_id] = entry
            continue
        if kind == "run_test":
            # Not code, so it never joins `history` — only exec buffers feed
            # the editor's up/down recall.
            request_id = record.get("id")
            commands += 1
            entry = {
                "kind": "run_test",
                "id": request_id,
                "ordinal": commands,
                "ts": record.get("ts"),
                "module": record.get("module"),
                "test_class": record.get("test_class"),
                "test_method": record.get("test_method"),
                "status": "running",
                "result": None,
                "actor": record.get("actor"),
            }
            entries.append(entry)
            pending[request_id] = entry
            continue
        if kind in ("result", "abandoned_result"):
            entry = pending.get(record.get("id"))
            if entry is None:
                continue
            result = {key: value for key, value in record.items() if key not in _RESULT_DROP}
            entry["result"] = result
            entry["status"] = "error" if result.get("error") else "done"
            if kind == "abandoned_result":
                # The payload is the real one, but the command had already blown
                # its ceiling; without this the rebuilt cell would read as an
                # ordinary success.
                entry["abandoned"] = True
            continue
        if kind == "timeout":
            entry = pending.get(record.get("id"))
            if entry is None:
                continue
            entry["timed_out"] = True
            if entry["status"] != "running":
                continue
            entry["status"] = "error"
            entry["result"] = {"error": dict(_TIMEOUT_ERROR)}
            continue
        if kind in ("commit", "rollback") and not record.get("error"):
            entries.append({"kind": kind, "actor": record.get("actor")})
            continue
        if kind == "owner_changed":
            entries.append({
                "kind": "owner_changed",
                "from": record.get("from"),
                "to": record.get("to"),
            })
            continue
        if kind == "policy_changed":
            entries.append({"kind": "policy_changed", "allow_commit": record.get("allow_commit")})

    feed = {"history": history, "entries": entries}
    if include_logs:
        feed["logs"] = logs[-log_tail:] if log_tail else logs
        feed["logs_truncated"] = bool(log_tail) and len(logs) > log_tail

    return feed
