"""Append-only JSONL record of everything a session did.

Journals are unmasked: they can contain credentials read out of the database.
They stay local, out of git, and are reviewed before being shared.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

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

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []

        with self.path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


def _duration_seconds(records: list[dict]) -> float | None:
    if not records:
        return None
    try:
        first = datetime.fromisoformat(records[0]["ts"])
        last = datetime.fromisoformat(records[-1]["ts"])
    except (KeyError, ValueError):
        return None

    return (last - first).total_seconds()


def session_meta(records: list[dict], session_id: str | None = None) -> dict:
    """Who and what this journal is about.

    Every way of getting a journal out — the API history, the JSONL export, the
    Markdown transcript — carries this, so a transcript is never an anonymous
    wall of commands.
    """
    opened = next((r for r in records if r.get("kind") == "session_open"), {})
    closed = next(
        (r for r in reversed(records) if r.get("kind") in ("session_close", "session_died")),
        {},
    )

    owners = [dict(opened["owner"])] if opened.get("owner") else []
    for record in records:
        if record.get("kind") == "owner_changed" and record.get("to"):
            owners.append(dict(record["to"]))

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
        "duration": _duration_seconds(records),
        "commands": sum(1 for r in records if r.get("kind") in ("exec", "run_test")),
        "committed": any(r.get("kind") == "commit" for r in records),
        "unmasked": True,
    }


def list_journals(root: Path) -> list[dict]:
    entries = []
    for path in sorted(root.glob("*.jsonl"), reverse=True):
        records = Journal(path).records()
        meta = session_meta(records, path.stem.rsplit("-", 1)[-1])
        entries.append({
            **meta,
            "path": str(path),
            "container": meta["container"] or "?",
            "database": meta["database"] or "?",
            "odoo": meta["odoo"] or "?",
        })

    return entries


def delete_journal(root: Path, session_id: str) -> Path:
    """Unlink the on-disk file for a finished session. Raises if it is gone.

    The id is compared, never globbed: interpolating it into a pattern let a
    `*` in the URL match every journal and unlink an unrelated one. The id is
    derived from the filename exactly as `list_journals` derives it, so the row
    a caller saw and the file that goes away are the same file.
    """
    for path in root.glob("*.jsonl"):
        if path.stem.rsplit("-", 1)[-1] == session_id:
            path.unlink()

            return path

    raise FileNotFoundError(session_id)


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
            by = f" by {actor.get('kind')} ({actor.get('label')})" if actor else ""
            lines.append(f"## Command {next_ordinal}{by} — {stamp}\n")
            lines.append(f"```python\n{record.get('code', '').rstrip()}\n```\n")
        elif kind == "run_test":
            next_ordinal += 1
            ordinals[record.get("id")] = next_ordinal
            actor = record.get("actor") or {}
            by = f" by {actor.get('kind')} ({actor.get('label')})" if actor else ""
            spec = f"{record.get('module', '')}.{record.get('test_class', '')}"
            if record.get("test_method"):
                spec += f".{record['test_method']}"
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
                lines.append(f"```\n{record['error'].get('traceback', '').rstrip()}\n```\n")
            lines.append(f"_{record.get('duration', 0):.3f}s_\n")
        elif kind in ("commit", "rollback"):
            lines.append(f"**Transaction {kind}** — {stamp}\n")
        elif kind == "interrupt":
            lines.append(f"**Interrupted** — {stamp}\n")
        elif kind == "owner_changed":
            was = record.get("from") or {}
            now = record.get("to") or {}
            lines.append(
                f"**Ownership moved** from {was.get('kind')} ({was.get('label')}) "
                f"to {now.get('kind')} ({now.get('label')}), "
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
    """Where a session ran, so a replacement can be opened on the same target."""
    opened = next((r for r in records if r.get("kind") == "session_open"), None)
    if not opened or not opened.get("container") or not opened.get("database"):

        return None

    return {
        "container": opened["container"],
        "database": opened["database"],
        "odoo_bin": opened.get("odoo_bin"),
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
