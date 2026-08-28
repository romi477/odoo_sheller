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
restart is harmless: sessions stay up in the daemon, and the agent
re-attaches by id.

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
