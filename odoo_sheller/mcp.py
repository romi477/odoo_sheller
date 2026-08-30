"""MCP server: the same HTTP API, exposed as tools for an agent.

Runs over stdio, which carries JSON-RPC — so nothing here may write to stdout.
Use `logging` (it writes to stderr) and never `print`.

This module holds no session logic. Every tool is one call to the daemon, which
must already be running: Claude Desktop restarts its MCP servers freely, and a
daemon started as a child of this process would take every live session down
with it on each restart.
"""

import asyncio
import logging
import os
import secrets
from typing import Any

import httpx2 as httpx
from mcp.server import MCPServer
from mcp_types import ToolAnnotations

logger = logging.getLogger(__name__)

DAEMON_URL = os.environ.get("ODOO_SHELLER_URL", "http://127.0.0.1:8765")
AGENT_LABEL = "mcp-agent"
EXEC_TIMEOUT = 30.0  # MCP clients give up long before the API's five minutes
# `Session.start` waits this long for the bootstrap's hello. Giving up on the
# open call any earlier strands a session the daemon then goes on to register,
# whose write key was only ever in the response we stopped waiting for.
SESSION_START_CEILING = 90.0
MAX_TEST_TIMEOUT = 3600.0  # matches the API's own ceiling on RunTestBody
# How long one tool call may block. MCP hosts cut a call off at around a
# minute, and nothing we set on our side changes that — so a long run has to
# be handed back as "still going" rather than held onto until the host kills
# it. Override for a host with a different patience.
MCP_CALL_BUDGET = float(os.environ.get("ODOO_SHELLER_MCP_BUDGET", "40"))
# The floor for the run leg. If opening the session already ate the budget we
# still have to send the request — otherwise nothing runs at all and the
# session id we hand back points at an idle session.
RUN_START_GRACE = 5.0
TEST_RESULT_POLL = 2.0  # how often os_test_result looks while it waits
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

A session may stay open for as long as you have more steps to run in it —
that is what a workspace is. When the work is finished and you do not plan
to continue, close it with os_close_session. An idle session still holds a
process inside the container. Closing discards uncommitted work, the same
as rollback.

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

1. Stop. Do not retry os_commit — spinning on it will not see the grant.
2. Tell the human, in plain words, what you want to write and why: which records,
   how many, and what would be wrong if it were rolled back instead.
3. They grant the right in the UI (session header keyboard, Grant commit).
   You will not be told in chat: call os_session and look at allow_commit.
   Repeat until it is true, then call os_commit.
4. Do not ask them whether they have granted it. os_session is how you know.

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

## Delayed jobs

`with_delay()` enqueues a job; this session will not run the queue for you.
To run a delayed method inline instead of enqueueing it, put
`queue_job__no_delay` on the environment context. Nested `with_delay()`
calls inherit it, so the whole chain runs on the spot:

    env = env(context=dict(env.context, queue_job__no_delay=True))
    record.with_delay().do_work()

A recordset you already hold still has the old context: call
`.with_context(queue_job__no_delay=True)` on it before `with_delay()`.

## Running a test

os_run_test runs one Odoo test method or a whole test class:
`module.TestClass` or `module.TestClass.test_method`. Unlike os_exec, it always
opens its own brand-new session first — it never reuses a session you already
have — so there is nothing of yours it can discard. That session closes itself
the moment the run settles: do not call os_close_session on it, and do not
count it against yourself in os_list_sessions. Run several classes by calling
os_run_test once per class, in turn.

When the class name is unknown, call os_list_tests(module) rather than inventing
names. Prefer running module.TestClass (one class, one os_run_test, one close).
Do not open a session per method unless a single method is the point. Do not
fire the whole list as parallel os_run_test calls.

Odoo's own test runner rolls back whatever transaction a session's cursor is
holding before it runs tests. This only matters if you call os_run_test's
underlying session a second time after using os_exec in it — the response's
`discarded_pending` field says whether that happened, so watch it rather than
assume nothing was lost.

The default timeout is short (30s) because a single test usually is. Pass a
larger `timeout` yourself when you deliberately run a whole class, which can
take minutes — pad your estimate: Odoo's own test framework can add up to 10
extra seconds per test class if one leaves a subprocess running.

A run longer than about a minute cannot be answered in one call: the host
cuts a tool call off well before that, whatever timeout you passed. So
os_run_test hands back `{"status": "running", "session_id": ...}` instead —
that is not a failure, and the run is still going in the container.

When you see it, call os_test_result(session_id). That waits too, and answers
either with the finished outcome or with `status: "running"` again — in which
case call it again straight away. There is nothing to pause between calls,
and nothing to clean up afterwards: the session closes itself.

Never answer a `status: "running"` by calling os_run_test again. That starts
a second, duplicate run on top of the first.

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


def _clip_tail(text: str | None, limit: int) -> tuple[str | None, bool]:
    """Keep the end, not the beginning.

    For a test run's log the last line is the one worth having — the summary,
    or the failure that ended it. Clipping from the front hands back the test
    framework's boot chatter and drops the answer.
    """
    if text is None or len(text) <= limit:

        return text, False

    return text[-limit:], True


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


async def _call(
    method: str,
    path: str,
    session_id: str | None = None,
    client_timeout: float | None = None,
    **kwargs,
) -> Any:
    headers = {}
    if session_id and session_id in _keys:
        headers["X-OS-Session-Key"] = _keys[session_id]
    try:
        async with httpx.AsyncClient(
            base_url=DAEMON_URL, timeout=client_timeout or (EXEC_TIMEOUT + 10)
        ) as client:
            response = await client.request(method, path, headers=headers, **kwargs)
    except httpx.TimeoutException:
        # The daemon is still working — it never stopped answering. Telling
        # the agent to go start it would be wrong, and a retry would kick off
        # a brand-new run instead of checking on the slow one already going.
        logger.warning("request timed out waiting for the daemon")

        return {
            "error": "request_timed_out",
            "recovery": (
                "the daemon is still working, not down — call os_history or "
                "os_journal on the session to see whether it finished, rather "
                "than retrying this call"
            ),
        }
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

    # Test sessions close themselves, so keys outlive the sessions they were
    # for. Drop the ones the daemon no longer has rather than reporting them.
    live = {session["id"] for session in sessions}
    for lost in [held for held in _keys if held not in live]:
        del _keys[lost]

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
        "Current state of a session: owner, whether it is ready, and whether "
        "commit has been granted. Read-only. After asking for Grant commit, "
        "call this until allow_commit is true — do not wait for a chat message."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_session(session_id: str | None = None) -> Any:
    target = _default_session(session_id)
    if isinstance(target, dict):

        return target

    return await _call("GET", f"/api/sessions/{target}", target)


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


@mcp.tool(
    description=(
        "List test classes and methods in one addon, already shaped as "
        "os_run_test specs (module.TestClass / module.TestClass.test_method). "
        "One module per call. This is files on disk, not 'installed in this "
        "database'. Read-only; does not open a session."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_list_tests(module: str, container: str | None = None) -> Any:
    target = container
    if not target:
        session_id = _default_session(None)
        if isinstance(session_id, dict):
            refusal = dict(session_id)
            if refusal.get("error") == "no_session":
                refusal["recovery"] = "pass container, or open a session first"
            elif refusal.get("error") == "ambiguous_session":
                refusal["recovery"] = (
                    "pass container explicitly: this server owns more than one session"
                )

            return refusal
        described = await _call("GET", f"/api/sessions/{session_id}", session_id)
        if described.get("error"):

            return described
        target = described["container"]

    return await _call(
        "GET",
        f"/api/containers/{target}/tests",
        params={"module": module},
    )


@mcp.tool(
    description=(
        "Run one Odoo test method or a whole test class by name: "
        "'module.TestClass' or 'module.TestClass.test_method'. Always opens its "
        "own brand-new session (owner agent, allow_commit false) rather than "
        "reusing one you already have, so there is never anything pending to "
        "lose, and that session closes itself once the run settles — there is "
        "nothing to clean up. stdout and the Odoo log lines produced during "
        "the run come back separated. A run too long to answer in one call "
        "comes back as {\"status\": \"running\", \"session_id\": ...}, which is "
        "not a failure: call os_test_result(session_id) to wait for it, and "
        "never call this tool again for the same run. The default timeout is "
        "short (a single test is usually fast) — pass a larger one for a whole "
        "class; it is the ceiling for the run itself, not for this call."
    ),
)
async def os_run_test(
    test: str,
    container: str | None = None,
    database: str | None = None,
    odoo_bin: str | None = None,
    timeout: float = 30.0,
) -> Any:
    if not 0 < timeout <= MAX_TEST_TIMEOUT:
        # Checked before anything is opened: a doomed ceiling would otherwise
        # cost a whole session start to earn a raw validation error.
        return {
            "error": "invalid_timeout",
            "timeout": timeout,
            "recovery": (
                f"pass a timeout between 0 and {MAX_TEST_TIMEOUT:.0f} seconds; "
                "a whole class usually wants a few hundred"
            ),
        }

    # The host times the whole tool call, so the budget has to cover opening
    # the session as well as waiting for the run. Spending it on the run alone,
    # after a registry load that already took seconds, overshoots and the call
    # is killed before it can hand back the session id.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MCP_CALL_BUDGET

    # Container and database do not identify a session — several may be
    # opening on the same target at once — so the token is what finds this one
    # again if the open call is the thing that times out.
    client_token = f"{AGENT_LABEL}-{secrets.token_urlsafe(8)}"
    opened = await _call(
        "POST",
        "/api/sessions",
        # Bounded by the same budget as everything else: waiting out the
        # daemon's full ceiling only means the host kills the call first, and
        # then even the client_token below never reaches the caller.
        client_timeout=min(SESSION_START_CEILING + 10, MCP_CALL_BUDGET),
        json={
            "container": container,
            "database": database,
            "odoo_bin": odoo_bin,
            "owner": {"kind": "agent", "label": AGENT_LABEL},
            "allow_commit": False,
            "client_token": client_token,
            # It exists to run one test. Nobody has to remember to close it.
            "autoclose": True,
        },
    )
    if opened.get("error"):
        if opened.get("error") == "request_timed_out":

            return {
                **opened,
                "client_token": client_token,
                "recovery": (
                    "the session may still have opened — find it with "
                    "os_list_sessions by this client_token before opening "
                    "another, and ask the human to close it: this server never "
                    "received its write key"
                ),
            }

        return opened
    session_id = opened["id"]
    _keys[session_id] = opened.pop("write_key")

    # Whatever is left of the budget, and never more: a margin added on top of
    # a cap defeats the cap. The daemon still gets the full ceiling the caller
    # asked for — only our own waiting is capped, so the run is never cut short.
    # Two seconds over the daemon's own ceiling keeps a short run from racing
    # its 504 against our timeout.
    run_leg = max(min(timeout + 2, deadline - loop.time()), RUN_START_GRACE)
    result = await _call(
        "POST", f"/api/sessions/{session_id}/run_test", session_id,
        client_timeout=run_leg,
        json={"test": test, "timeout": timeout},
    )
    if result.get("error") == "request_timed_out":

        return {
            "status": "running",
            "session_id": session_id,
            "test": test,
            "recovery": (
                "the run is still going in the container — call "
                f'os_test_result("{session_id}") to wait for it; that session '
                "closes itself when the run ends, so there is nothing to clean up"
            ),
        }
    if result.get("error") and "stdout" not in result:

        return {"session_id": session_id, **result}

    test_info = result.get("test") or {}
    stdout, stdout_clipped = _clip(result.get("stdout"), MAX_STDOUT)
    stderr, stderr_clipped = _clip_tail("\n".join(result.get("stderr") or []), MAX_STDOUT)
    truncated = stdout_clipped or stderr_clipped

    return {
        "session_id": session_id,
        "tests_run": test_info.get("tests_run"),
        "failures": test_info.get("failures"),
        "errors": test_info.get("errors"),
        "skipped": test_info.get("skipped"),
        "success": test_info.get("success"),
        "stdout": stdout,
        "stderr": stderr,
        "error": result.get("error"),
        "duration": result.get("duration"),
        "discarded_pending": result.get("discarded_pending"),
        # Two different losses: the daemon dropping whole lines past its own
        # ceiling, and this server clipping characters to spare your context.
        "stderr_truncated": bool(result.get("stderr_truncated")),
        "truncated": truncated,
        "journal": f"/api/journals/{session_id}" if truncated else None,
    }


@mcp.tool(
    description=(
        "Wait for a test run started by os_run_test and return its outcome. "
        "Blocks while the run is still going, then answers with tests_run, "
        "failures, errors, skipped and success — or says the run is still "
        "going, in which case call it again straight away. Works after the "
        "session has closed itself, and needs no write key. Read-only."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_test_result(session_id: str) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MCP_CALL_BUDGET
    while True:
        feed = await _call("GET", f"/api/sessions/{session_id}/history")
        if feed.get("error"):

            return feed

        runs = [
            entry for entry in (feed.get("entries") or [])
            if entry.get("kind") == "run_test"
        ]
        if runs and (runs[-1].get("result") is not None):

            return {"status": "done", **_history_run_test_entry(runs[-1])}

        gone = (feed.get("session") or {}).get("state") == "gone"
        if gone:
            # The session ended without the run ever settling: the process
            # died mid-run. Saying "running" here would be a poll forever.

            return {
                "status": "lost",
                "session_id": session_id,
                "journal": f"/api/journals/{session_id}",
                "recovery": (
                    "the run died with its container process — read the "
                    "journal for the log tail, then start it again"
                ),
            }
        if loop.time() >= deadline:

            return {
                "status": "running",
                "session_id": session_id,
                "recovery": (
                    "still going — call os_test_result again straight away; "
                    "each call waits, so there is no need to pause between them"
                ),
            }
        await asyncio.sleep(TEST_RESULT_POLL)


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
        "and why, then poll os_session until allow_commit is true. Retrying "
        "os_commit will not see the grant."
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


def _history_run_test_entry(entry: dict) -> dict:
    """A run_test entry, in the shape os_run_test answers with.

    Journaled precisely so a transport timeout on a long class doesn't lose
    the outcome — the agent recovers it from here instead of nowhere.
    """
    result = entry.get("result") or {}
    test = result.get("test") or {}
    spec = f"{entry.get('module')}.{entry.get('test_class')}"
    if entry.get("test_method"):
        spec += f".{entry['test_method']}"
    stdout, out_clipped = _clip(result.get("stdout"), MAX_STDOUT)
    # The same tail rule os_run_test uses: on a long run the last line is the
    # summary, and clipping from the front would drop it.
    stderr, stderr_clipped = _clip_tail("\n".join(result.get("stderr") or []), MAX_STDOUT)
    shaped = {
        "n": entry.get("ordinal"),
        "test": spec,
        "status": entry.get("status"),
        "tests_run": test.get("tests_run"),
        "failures": test.get("failures"),
        "errors": test.get("errors"),
        "skipped": test.get("skipped"),
        "success": test.get("success"),
        "stdout": stdout or None,
        "stderr": stderr or None,
        # Whole lines the daemon dropped, as opposed to the characters clipped
        # just above. Absent from journals written before it existed.
        "stderr_truncated": result.get("stderr_truncated"),
        "error": result.get("error"),
        "duration": round(result["duration"], 3) if result.get("duration") is not None else None,
        "actor": _actor(entry.get("actor")),
    }
    if out_clipped or stderr_clipped:
        shaped["truncated"] = True
    if entry.get("abandoned"):
        shaped["abandoned"] = True

    return {key: value for key, value in shaped.items() if value is not None}


def _history_entry(entry: dict) -> dict:
    """One past command, in the shape os_exec answers with.

    The journal keeps wire fields the daemon needs; an agent reading its own
    history needs the command and what came back, and pays for every other byte.
    """
    if entry.get("kind") == "run_test":

        return _history_run_test_entry(entry)
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
