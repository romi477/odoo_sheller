# Agent access (MCP)

`odoo_sheller/mcp.py` gives an AI agent the same sessions a human uses in the
browser, over the [Model Context Protocol](https://modelcontextprotocol.io/),
via stdio. It is a thin client: every tool call is the same HTTP request the
web UI would make against the daemon. No session state, no pipes, no
container access lives in the MCP process itself.

This assumes you've read how [ownership and sessions](architecture.md#ownership-and-agent-access)
work in general. This page is the agent-specific half: the tool list, the
defaults chosen for a model instead of a human, and how to wire it into
Claude Desktop.

## Two ways an agent gets a session

- **Opens its own.** `os_open_session` creates a session with `owner: agent`
  and `allow_commit: false`. It behaves exactly like a human-opened session
  from the daemon's point of view — same registry, same journal, same rules.
- **Is handed one.** A human opens a session in the browser, works in it,
  then presses **Grant access**. That copies `{"session_id", "write_key"}`
  for the agent to use with `os_attach_session`. The process, its namespace,
  and any open transaction survive the handover — the agent picks up exactly
  where the human left off, variables included.

Either way, committing anything still requires the human to flip **Grant
commit** in the session keyboard after watching what the agent did. There is
no way for an agent to grant itself write access.

## Tools

| Tool | Arguments | Notes |
|---|---|---|
| `os_list_containers` | — | running containers with their probe results |
| `os_open_session` | `container`, `database`, `odoo_bin`, `replace=None` | opens as `agent`, `allow_commit=False`; stores the write key for you |
| `os_attach_session` | `session_id`, `write_key` | adopts a session a human handed over |
| `os_list_sessions` | — | every session with its owner and state (read-only) |
| `os_session` | `session_id=None` | one session's state, including `allow_commit` (read-only) |
| `os_exec` | `code`, `session_id=None` | blocks; returns stdout, result, error, duration |
| `os_list_tests` | `module`, `container=None` | classes and methods in one addon, as `os_run_test` specs; disk catalogue, no session |
| `os_run_test` | `test`, `container=None`, `database=None`, `odoo_bin=None`, `timeout=30.0` | runs one Odoo test method or a whole class; opens its own new session, which then closes itself |
| `os_test_result` | `session_id` | waits for a run started by `os_run_test` and returns its outcome (read-only, no key needed) |
| `os_rollback` | `session_id=None` | discards the open transaction |
| `os_commit` | `session_id=None` | fails unless the human has granted commit |
| `os_interrupt` | `session_id=None` | stops a running command |
| `os_close_session` | `session_id=None` | ends the session |
| `os_history` | `session_id`, `limit=20` | recent commands and results, rebuilt from the journal |
| `os_journal` | `session_id`, `fmt="markdown"` | the full transcript |

`session_id` defaults to the one session the server currently holds a key
for; it becomes required once it holds more than one.

## Defaults chosen for a model, not a human

- **The exec ceiling is 30 seconds**, not the API's usual five minutes.
  Most MCP clients give up well before that, and a client that stops waiting
  while the daemon is still holding the command open is exactly the kind of
  confusion the timeout-handling rules in the architecture doc exist to
  avoid. On timeout, the tool says plainly that the session is still busy.
- **Output is truncated hard**: 4 KB of stdout, 2 KB of the returned value.
  An agent's context window is the scarce resource here, not disk — the
  untruncated text is one `os_journal` call away.
- Every refusal comes back as structured data — a code, a reason, and a
  concrete next step — never a bare error string. `session_busy`,
  `session_gone`, `not_owner`, and `commit_not_allowed` are the ones worth
  recognizing by name; the server's own instructions (visible to the model)
  spell out what to do for each.

## What the server tells the model

The MCP server ships its own instructions text, which an agent using it will
see directly. In short:

- Commit writes to a real database — only call `os_commit` after the human
  has granted it. A grant happens in the UI and is not announced in chat:
  poll `os_session` until `allow_commit` is true, then commit. Once granted,
  later commits in that session need no further check-in.
- `with_delay()` enqueues a job; this session will not run the queue. Put
  `queue_job__no_delay=True` on the environment context to run delayed
  methods inline.
- Close a session with `os_close_session` when the work is finished and you
  do not plan to continue. Leaving it open across many steps is fine.
- Work only in sessions you opened yourself or were explicitly handed. Never
  attach with a write key you weren't given.
- Never touch `~/.odoo-sheller/` directly and never call the daemon's admin
  endpoints — everything an agent needs is one of the tools above.
- Rollback is cheap and is the right way to end an experiment. Prefer it.

## Running a test

`os_run_test("module.TestClass")` or `os_run_test("module.TestClass.test_method")`
runs through Odoo's own shell-native test runner
(`odoo.tests.shell.run_tests`) — the same mechanism `odoo-bin shell` itself
would use, not a reimplementation. Unlike every other tool here, it always
opens a **brand-new session** first, rather than running against one the
caller already has: a fresh session has no pending transaction for Odoo's own
test setup to silently discard. That session then **closes itself**: it is
opened with `autoclose`, so once the run settles and is journalled the daemon
closes it —
no `os_close_session` to remember, no container process left running, no test
HTTP daemon holding a port. Run several classes by calling `os_run_test` once
per class, in turn. In the web UI that
session's badge reads `testing` and its tab shows a blinking lamp, so the
human watching can tell a test apart from ordinary `exec`.

When the class name is unknown, call `os_list_tests(module)` rather than
inventing names. Prefer running `module.TestClass` — one class, one
`os_run_test`. Do not open a session per method unless a single
method is the point. Do not fire the whole list as parallel `os_run_test`
calls.

The response separates what the test printed (`stdout`) from what Odoo logged
while it ran (`stderr`), plus `tests_run`, `failures`, `errors`, `skipped` and
`success`. `tests_run: 0` needs its own check — `success` reads `true` for a
name that matched nothing at all, which otherwise looks exactly like a pass.
`stderr` is clipped from the *end* — on a long run the last line is the one
worth having (`Tests passed: …`, or the failure that ended it), and clipping
from the front would return the framework's boot chatter instead. Two flags
report two different losses: `stderr_truncated` means the daemon dropped
whole lines past its own ceiling, `truncated` means this server clipped
characters to spare your context. The journal always has the rest.

`os_list_tests(module)` answers what is runnable in an addon, already shaped
as specs. It mirrors Odoo's own loader rather than guessing from names: any
class with a `test_*` method counts (a `TransactionCase` subclass need not be
called `Test`-something), and only modules that `tests/__init__.py` actually
imports are read — a file Odoo never loads would otherwise be offered as a
spec that comes back `tests_run: 0`.

Odoo's test runner rolls back whatever transaction a session's own cursor is
holding before it tests. This is invisible the first time (a fresh session has
nothing to lose), but running a test a second time in the *same* returned
session, after using `os_exec` in it meanwhile, discards that work — the
response's `discarded_pending` field says whether that happened.

The default `timeout` is 30 seconds, matching `os_exec` — generous for one
test, short for a whole class. Pass a larger one explicitly
(`os_run_test(..., timeout=600)`) when deliberately running a slower class.

A run longer than about a minute cannot be answered in one call, whatever
`timeout` says. MCP hosts cut a tool call off at around that mark and nothing
on this side changes it, so `os_run_test` stops waiting at `MCP_CALL_BUDGET`
(40 seconds, `ODOO_SHELLER_MCP_BUDGET` to override) and answers:

```json
{"status": "running", "session_id": "ab12…", "test": "qbo.TestBig",
 "recovery": "call os_test_result(\"ab12…\") …"}
```

That is not a failure and the run is not cut short — the daemon still has the
full `timeout` the caller asked for; only this server's own waiting is capped.

`os_test_result(session_id)` then waits in turn, up to the same budget,
polling the journal-backed history. It answers one of three ways: the
finished outcome; `status: "running"` again, meaning call it again straight
away (each call waits, so there is nothing to pause between them); or
`status: "lost"`, meaning the run died with its container process — which is
what stops an agent polling a vanished run forever. None of it needs a write
key, so it survives a restart of this server.

Pad a class-sized `timeout` a little further still: Odoo's own test framework
can add up to 10 seconds per test class if one leaves a subprocess running
(`odoo/tests/common.py`'s `check_remaining_processes` — harmless, logged as a
warning, unrelated to odoo-sheller).

## Running under Claude Desktop

Claude Desktop starts the server itself, from
`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "odoo-sheller": {
      "command": "uv",
      "args": ["--directory", "<project path>", "run", "python", "-m", "odoo_sheller.mcp"]
    }
  }
}
```

Absolute paths for `command` and `--directory` are safer in practice: Desktop
launches its servers with a minimal `PATH`, so a bare `uv` can fail to
resolve. `uv run` syncs the environment from `uv.lock` before starting, so
the MCP SDK lives in this project's own dependencies rather than a separate
environment.

**The daemon is not started by the MCP server, on purpose.** Desktop restarts
its MCP servers freely — on a config change, a crash, even a quit. If the
daemon were a child of that server, every one of those restarts would kill
every live session, including the human's own. Kept separate, an MCP server
restart is harmless for the human's own sessions: they stay up in the daemon.
An agent's own sessions are a different matter — the write key lives only in
this server's memory, so a restart loses the right to type into them. Test
sessions close themselves and need no key to read back (`os_test_result` and
`os_history` work from the journal), but a long-lived `os_open_session`
workspace has to be handed over again by the human after a restart.

By default the server talks to `http://127.0.0.1:8765`. If the daemon is
running on a different host or port, set `ODOO_SHELLER_URL` (in the
`mcpServers` entry's own `env`, or the shell that starts Desktop) rather than
changing code.

When the daemon isn't reachable at all, every tool returns the same shape
instead of a raw transport error:

```json
{
  "error": "daemon_unreachable",
  "url": "http://127.0.0.1:8765",
  "recovery": "ask the human to start it: uv run python -m odoo_sheller"
}
```
