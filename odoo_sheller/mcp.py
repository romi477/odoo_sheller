"""MCP server: the same HTTP API, exposed as tools for an agent.

Runs over stdio, which carries JSON-RPC — so nothing here may write to stdout.
Use `logging` (it writes to stderr) and never `print`.

This module holds no session logic. Every tool is one call to the daemon, which
must already be running: Claude Desktop restarts its MCP servers freely, and a
daemon started as a child of this process would take every live session down
with it on each restart.
"""

import logging
import os
from typing import Any

import httpx2 as httpx
from mcp.server import MCPServer
from mcp_types import ToolAnnotations

logger = logging.getLogger(__name__)

DAEMON_URL = os.environ.get("ODOO_SHELLER_URL", "http://127.0.0.1:8765")
AGENT_LABEL = "mcp-agent"
EXEC_TIMEOUT = 30.0  # MCP clients give up long before the API's five minutes
MAX_STDOUT = 4000
MAX_RESULT = 2000

INSTRUCTIONS = """\
odoo-sheller gives you a live Odoo shell inside a local Docker container, shared
with the human who runs it. `env` and `self` are Odoo's own shell namespace, and
variables persist between commands, so a session is a workspace rather than a
series of one-off scripts.

## Sessions

Open one with os_open_session, or adopt one the human hands you with
os_attach_session(session_id, write_key). You will normally be told both in a
message: the id alone is public and grants nothing, the write key is what allows
you to run code there. This server keeps your keys; you never pass them again.

One command runs at a time in a session. A second one is refused with
`session_busy` rather than queued — wait for the first to finish, or stop it with
os_interrupt. A command that exceeds the 30-second ceiling keeps the session busy
until it really ends: it is still holding the container.

If a session is gone (`session_gone`), its process, namespace and variables died
with it. Nothing is restored by reopening. The refusal carries the target and a
journal link; open a replacement with os_open_session(replace=<old id>) and
decide for yourself which earlier commands are safe to run again — some of them
wrote to the database, and some were never meant to run twice. os_history is the
one tool a dead session still answers: it returns the transcript, and says so in
`session.state` and `session.gone`. Read the transcript, treat the session as
over.

## Ownership

Every session has one owner. Yours are owned by you; the human's are owned by
them, and you may read those but not type in them. A refusal reads `not_owner`:
stop, and ask the human to hand the session over rather than opening a second one
behind their back.

A handover moves the right to type without disturbing the session: the process,
the namespace and the open transaction all survive, so you continue exactly where
the human left off — their variables are still there. Ownership can move back the
same way, at any time, without warning to you.

The human is the admin. They can watch any session live, interrupt a command,
close or kill a session, hand ownership around and grant commit rights. You
cannot do any of that to a session you do not own, and you must not try.

## Transactions and commit

Rollback is the default state of the world here. Everything you do lives in an
open transaction; closing the session, killing it or losing it discards the lot.
That is the safety of this tool, not an inconvenience: experiment freely and end
with os_rollback.

Writing to the database is a separate, granted right, and it gates os_commit
only — running code is never gated by it. os_exec always works once you own a
ready session; the only question is whether persisting it is allowed yet.

Your sessions open with the right off, and os_commit answers
`commit_not_allowed` until it is granted. The first time that happens:

1. Stop. Do not retry the commit — the answer will not change by itself.
2. Tell the human, in plain words, what you want to write and why: which records,
   how many, and what would be wrong if it were rolled back instead.
3. Wait. They read the session's feed — they see every command you ran — and
   grant the right in the UI (session header keyboard, Grant commit).
4. Only then call os_commit again.

Once granted, the right stays granted — call os_commit directly for any later
commit in this same session, with no need to repeat this ritual first. It
lasts until the human revokes it, and does not survive a handover: check
again after one, the same way as the first time.

Uncommitted work travels with a session when ownership moves. If the human hands
you a session with pending commands, a commit you make would write their work
too. Say so before committing anything you did not do yourself.

## What you cannot do, and must not attempt

- Grant yourself commit rights, or call the daemon's admin endpoints.
- Take a session you were not handed, or use a key you were not given.
- Read or write ~/.odoo-sheller/ directly. Everything you need is in these tools.

These are not enforced by the keys you hold — they are the terms of using this
tool at all.

## Writing idiomatic ORM code

A recordset is iterable, but a Python loop to pull one field, or to sum a
column, throws away idioms the ORM gives you for free — and reads worse to
whoever is watching the transcript.

- `records.mapped('name')` returns each record's `name` as a plain list.
  Prefer it over `[r.name for r in records]`.
- A dotted path follows a relation: `records.mapped('partner_id.bank_ids')`
  returns the union of every partner's banks, already de-duplicated, as one
  recordset — not a list of lists.
- Pass a callable for anything else: `records.mapped(lambda r: r.a + r.b)`,
  or `sum(records.mapped('qty'))` for a total.

`filtered()` narrows a recordset the same way — a function, a domain, or a
list of field names — and `sorted()` orders one, by a key function, a field
string, or the model's own default order with no argument at all. Neither
loads more from the database than `mapped()` does; none of the three is a
Python-side substitute for a real search domain.

Push filtering into `search()` itself rather than fetching broadly and
filtering in Python: `env['res.partner'].search([('is_company', '=', True)])`
reads and returns only what matches. `search_count(domain)` answers "how
many" without materializing any records at all — reach for it over
`len(records.search(domain))` whenever the records themselves are not needed.

Recordsets support the usual set operations directly — `|` union, `&`
intersection, `-` difference, `in` membership, `<=` / `<` / `>=` / `>` for
subset and superset — so two recordsets are combined or compared with an
operator, not a loop over ids with a manual dict to deduplicate.

Iterating a recordset yields one-record recordsets, not raw rows: `for r in
records: r.name` already works, no `r['name']` and no re-`browse()`ing an id
out of a dict. A helper that is only meant to run against a single record
should open with `self.ensure_one()` rather than assuming — it raises
immediately and clearly instead of the ambiguous behavior of reading a
multi-record field.

## Being watched

Everything is journalled with its author: every command, its output, every
transaction boundary and every handover. The human watches sessions live and
reads the transcript afterwards. Write commands that are legible on their own,
prefer small steps over one large opaque script, and say what you are doing when
it is not obvious from the code.
"""

mcp = MCPServer("odoo-sheller", instructions=INSTRUCTIONS)

# session id -> write key, for the sessions this server may type into.
_keys: dict[str, str] = {}


def _clip(text: str | None, limit: int) -> tuple[str | None, bool]:
    """Agent context is the scarce resource here; the journal keeps the rest."""
    if text is None or len(text) <= limit:

        return text, False

    return text[:limit], True


def _default_session(session_id: str | None) -> str | Any:
    if session_id:

        return session_id
    if len(_keys) == 1:

        return next(iter(_keys))
    if not _keys:

        return {
            "error": "no_session",
            "recovery": "call os_open_session first, or os_attach_session with an id and key",
        }

    return {
        "error": "ambiguous_session",
        "sessions": sorted(_keys),
        "recovery": "pass session_id explicitly: this server owns more than one session",
    }


async def _call(method: str, path: str, session_id: str | None = None, **kwargs) -> Any:
    headers = {}
    if session_id and session_id in _keys:
        headers["X-OS-Session-Key"] = _keys[session_id]
    try:
        async with httpx.AsyncClient(base_url=DAEMON_URL, timeout=EXEC_TIMEOUT + 10) as client:
            response = await client.request(method, path, headers=headers, **kwargs)
    except Exception as exc:  # noqa: BLE001 - any transport failure means the same thing
        logger.warning("daemon unreachable: %s", exc)

        return {
            "error": "daemon_unreachable",
            "url": DAEMON_URL,
            "recovery": "ask the human to start it: uv run python -m odoo_sheller",
        }

    if response.status_code < 400:
        if response.headers.get("content-type", "").startswith("application/json"):

            return response.json()

        return {"text": response.text}

    detail = response.json().get("detail") if response.headers.get(
        "content-type", ""
    ).startswith("application/json") else response.text
    if isinstance(detail, dict):

        return detail

    return {"error": f"http_{response.status_code}", "message": detail}


@mcp.tool(
    description="List running Docker containers with what the probe found in each.",
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_list_containers() -> Any:
    containers = await _call("GET", "/api/containers")
    if isinstance(containers, dict):

        return containers
    probed = []
    for container in containers:
        probe = await _call("POST", "/api/probe", json={"container": container["name"]})
        probed.append({**container, "probe": probe})

    return {"containers": probed, "count": len(probed)}


@mcp.tool(
    description="List every live session with its owner and state. Read-only.",
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_list_sessions() -> Any:
    sessions = await _call("GET", "/api/sessions")
    if isinstance(sessions, dict):

        return sessions

    listed = [{**session, "yours": session["id"] in _keys} for session in sessions]

    return {
        "sessions": listed,
        "count": len(listed),
        "yours": sorted(_keys),
    }


@mcp.tool(
    description=(
        "Open a session you own. Pass replace=<lost session id> to reopen on the same "
        "target as a session that is gone; its variables are not restored."
    ),
)
async def os_open_session(
    container: str | None = None,
    database: str | None = None,
    odoo_bin: str | None = None,
    replace: str | None = None,
) -> Any:
    opened = await _call(
        "POST",
        "/api/sessions",
        json={
            "container": container,
            "database": database,
            "odoo_bin": odoo_bin,
            "replace": replace,
            "owner": {"kind": "agent", "label": AGENT_LABEL},
            "allow_commit": False,
        },
    )
    if opened.get("error"):

        return opened
    _keys[opened["id"]] = opened.pop("write_key")

    return opened


@mcp.tool(
    description=(
        "Adopt a session a human handed over, using the id and write key they gave "
        "you. The id alone grants nothing; never attach with a key you were not given."
    ),
)
async def os_attach_session(session_id: str, write_key: str) -> Any:
    _keys[session_id] = write_key
    described = await _call("GET", f"/api/sessions/{session_id}", session_id=session_id)
    if described.get("error"):
        _keys.pop(session_id, None)

    return described


@mcp.tool(
    description=(
        "Run Python in a session you own. Blocks until the command finishes. "
        "Variables persist between calls; one command runs at a time."
    ),
)
async def os_exec(code: str, session_id: str | None = None) -> Any:
    target = _default_session(session_id)
    if isinstance(target, dict):

        return target
    result = await _call("POST", f"/api/sessions/{target}/exec", target, json={"code": code})
    if result.get("error") and "stdout" not in result:

        return result

    stdout, stdout_clipped = _clip(result.get("stdout"), MAX_STDOUT)
    value, value_clipped = _clip(result.get("result"), MAX_RESULT)

    return {
        "stdout": stdout,
        "result": value,
        "error": result.get("error"),
        "duration": result.get("duration"),
        "truncated": stdout_clipped or value_clipped,
        "journal": f"/api/journals/{target}" if stdout_clipped or value_clipped else None,
    }


def _boundary_result(answer: Any) -> Any:
    """Wire frames are for the daemon; an agent needs the outcome."""
    if answer.get("error") and "stdout" not in answer:

        return answer  # a refusal, already shaped

    return {"ok": not answer.get("error"), "error": answer.get("error")}


@mcp.tool(
    description=(
        "Discard the open transaction. Nothing is written to the database, and the "
        "namespace survives. This is the normal end of an experiment."
    ),
)
async def os_rollback(session_id: str | None = None) -> Any:
    target = _default_session(session_id)

    if isinstance(target, dict):

        return target

    return _boundary_result(await _call("POST", f"/api/sessions/{target}/rollback", target))


@mcp.tool(
    description=(
        "Write the open transaction to the database — the only tool here that "
        "persists anything. Refused with commit_not_allowed unless a human has "
        "granted this session the right: if refused, say what you want to write "
        "and why, and wait for them to grant it. Retrying will not help."
    ),
    annotations=ToolAnnotations(destructive_hint=True),
)
async def os_commit(session_id: str | None = None) -> Any:
    target = _default_session(session_id)

    if isinstance(target, dict):

        return target

    return _boundary_result(await _call("POST", f"/api/sessions/{target}/commit", target))


@mcp.tool(
    description=(
        "Interrupt the command running in a session you own. The session, its "
        "namespace and its transaction all survive."
    ),
)
async def os_interrupt(session_id: str | None = None) -> Any:
    target = _default_session(session_id)

    return target if isinstance(target, dict) else await _call(
        "POST", f"/api/sessions/{target}/interrupt", target
    )


@mcp.tool(
    description="Close a session you own. Uncommitted work is discarded.",
    annotations=ToolAnnotations(destructive_hint=True),
)
async def os_close_session(session_id: str | None = None) -> Any:
    target = _default_session(session_id)
    if isinstance(target, dict):

        return target
    closed = await _call("DELETE", f"/api/sessions/{target}", target)
    _keys.pop(target, None)

    return closed


def _actor(actor: dict | None) -> str | None:

    return f"{actor['kind']}:{actor['label']}" if actor else None


def _history_entry(entry: dict) -> dict:
    """One past command, in the shape os_exec answers with.

    The journal keeps wire fields the daemon needs; an agent reading its own
    history needs the command and what came back, and pays for every other byte.
    """
    if entry.get("kind") != "exec":

        return {"kind": entry.get("kind"), "actor": _actor(entry.get("actor"))}

    result = entry.get("result") or {}
    code, code_clipped = _clip(entry.get("code"), MAX_RESULT)
    stdout, out_clipped = _clip(result.get("stdout"), MAX_STDOUT)
    value, value_clipped = _clip(result.get("result"), MAX_RESULT)
    shaped = {
        "n": entry.get("ordinal"),
        "code": code,
        "status": entry.get("status"),
        "stdout": stdout or None,
        "result": value,
        "error": result.get("error"),
        "duration": round(result["duration"], 3) if result.get("duration") is not None else None,
        "actor": _actor(entry.get("actor")),
    }
    if code_clipped or out_clipped or value_clipped or result.get("stdout_truncated"):
        shaped["truncated"] = True
    if entry.get("abandoned"):
        shaped["abandoned"] = True

    return {key: value for key, value in shaped.items() if value is not None}


@mcp.tool(
    description="Recent commands and results of a session, rebuilt from its journal.",
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_history(session_id: str, limit: int = 20) -> Any:
    feed = await _call("GET", f"/api/sessions/{session_id}/history")
    if feed.get("error"):

        return feed

    meta = feed.get("session") or {}
    entries = [_history_entry(entry) for entry in (feed.get("entries") or [])[-limit:]]
    session = {
        key: meta.get(key)
        for key in (
            "session_id", "container", "database", "owner", "state",
            "allow_commit", "commands", "committed", "unmasked",
        )
    }
    # A closed session still hands back its transcript, so this is the only
    # place the refusal would otherwise be lost: `_call` only surfaces
    # `session_gone` out of an error status, and this one arrives with a 200.
    if meta.get("gone"):
        session["gone"] = meta["gone"]

    return {
        "session": session,
        "entries": entries,
        "journal": f"/api/journals/{session_id}",
    }


@mcp.tool(
    description="The full transcript of a session, live or long finished.",
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_journal(session_id: str, fmt: str = "markdown") -> Any:

    return await _call("GET", f"/api/journals/{session_id}", params={"fmt": fmt})


def main() -> None:
    logging.basicConfig(level=logging.INFO)  # stderr, never stdout
    mcp.run()


if __name__ == "__main__":
    main()
