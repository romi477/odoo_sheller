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

Both of them are local containers. An agent cannot open a session on a remote
instance at all — see [Sessions it did not open](#sessions-it-did-not-open).

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
no way for an agent to grant itself write access: the daemon refuses a grant
made with an agent's own key, a session opened for an agent with the right
already in it, and a handover to "a human" made with an agent's key — all as
`needs_a_human`. The key that comes back from such a handover would type as a
human, and a local human commits without asking anyone.

## Tools

| Tool | Arguments | Notes |
|---|---|---|
| `os_list_containers` | — | running containers with their probe results |
| `os_open_session` | `container`, `database`, `odoo_bin=None`, `replace=None` | opens as `agent`, `allow_commit=False`; stores the write key for you. Leave `odoo_bin` out: the daemon probes the container for it |
| `os_attach_session` | `session_id`, `write_key` | adopts a session a human handed over; a key that does not type there is refused at once as `not_owner`, with `key_status` saying whether it was never this session's or was before ownership moved |
| `os_list_sessions` | — | every session with its owner and state (read-only) |
| `os_session` | `session_id=None` | one session's state, including `allow_commit` (read-only) |
| `os_exec` | `code`, `session_id=None`, `stderr=False` | blocks; returns stdout, result, error, duration and `stderr_lines`. `stderr=True` adds the log lines themselves |
| `os_list_tests` | `module`, `container=None` | classes and methods in one addon, as `os_run_test` specs; disk catalogue, no session. A local container's disk only: on a remote build it answers `not_a_container` and points at `os_source` |
| `os_run_test` | `test`, `container=None`, `database=None`, `odoo_bin=None`, `timeout=None`, `session_id=None` | runs a whole module's standard tests (`module`), one class or one method; answers with counts and `failed`, each a spec to rerun; opens its own new session (which then closes itself), or runs in one it was handed. Without `timeout` the ceiling follows the spec: 30s, 300s, 1800s |
| `os_test_result` | `session_id` | waits for a run started by `os_run_test` and returns its outcome (read-only, no key needed) |
| `os_rollback` | `session_id=None` | discards the open transaction |
| `os_commit` | `session_id=None`, `include_inherited=False` | fails unless the human has granted commit; refused once as `inherited_pending` on a session handed over with work already in its transaction |
| `os_interrupt` | `session_id=None` | stops a running command |
| `os_close_session` | `session_id=None` | ends the session |
| `os_history` | `session_id`, `limit=20` | recent commands and results, rebuilt from the journal |
| `os_journal` | `session_id`, `fmt="markdown"`, `first=None`, `last=None` | the transcript, a page at a time: the end by default, any stretch by 1-based line range, with `total_lines` (`fmt="jsonl"` for the raw records) |
| `os_source` | `path`/`model`, `method=None`, `module=None`, `first=None`, `last=None`, `session_id=None` | Odoo source from inside the instance: a module's file tree, a file's line range, or the method that actually runs plus the chain of modules overriding it. Journalled as a read, so it never counts as pending work |
| `os_help` | `topic=None` | the parts of this server's instructions the host did not deliver; no topic lists them (read-only, no session) |

`session_id` defaults to the one session the server currently holds a key
for; it becomes required once it holds more than one.

## Defaults chosen for a model, not a human

- **`os_exec` waits about 40 seconds; the command gets five minutes.**
  MCP hosts cut a tool call off at around a minute, so the server stops
  waiting before that and answers `request_timed_out`. The daemon's own
  ceiling is unchanged: the command runs on, and the session stays busy,
  until it ends or is interrupted at five minutes. The answer says exactly
  that — still running, do not run it again, `os_history` for when it
  finished, `os_interrupt` to stop it now. These docs once promised a
  30-second ceiling; there never was one on the daemon's side, and an agent
  believing it would have read a running command as a stopped one.
- **The Odoo log is one flag away, and its existence is free.** Every
  `os_exec` reports `stderr_lines`, a count; `stderr=True` returns the lines
  as `stderr`, clipped from the end. The split is deliberate: an agent told
  nothing about the log builds a `logging` handler of its own to scrape it
  out of the process — one did — while a log attached to every response is
  context spent on output nobody asked for. The count is the part that
  cannot be guessed, so it is always sent, and the lines are on request.
  `os_run_test` sends a class's or a method's log unasked, because there the
  log is the answer. A whole module's is a count, like `os_exec`'s: its log
  runs to thousands of lines, and `failed` already says what to look at.
  Either way the full log is on the journal, `kind: "stderr"` records
  interleaved with the commands by time, so a log wanted after the fact is
  read rather than re-produced — re-running a command that wrote to the
  database writes again. `os_journal` hands it back a page at a time — the
  end by default, any other stretch by line range — because a module run's
  journal is near a megabyte and whole it is context nothing else can use.
- **Output is truncated hard**: 4 KB of stdout, 2 KB of the returned value.
  An agent's context window is the scarce resource here, not disk — the
  untruncated text is one `os_journal` call away.
- Every refusal comes back as structured data — a code, a reason, and a
  concrete next step — never a bare error string. `session_busy`,
  `session_gone`, `not_owner`, and `commit_not_allowed` are the ones worth
  recognizing by name; the server's own instructions (visible to the model)
  spell out what to do for each.

## What the server tells the model

**A host delivers only the first 2048 characters of a server's instructions.**
Measured, not assumed: two different hosts cut this server's text
mid-sentence at exactly that offset and marked it truncated. At 16 kB of
instructions that meant 88% never reached the model — and it showed. One
agent wrote its own `logging` handler to scrape a log the instructions
already explained; another reached for `Job.load` instead of the documented
`queue_job__no_delay`. Both were reading everything they had.

So `INSTRUCTIONS` is now under the cap (1.8 kB) and holds only what an agent
would otherwise break unknowingly: one command at a time, rollback as the
default, how commit is granted and where it is refused outright, whose
session it is, and what never to touch. Everything longer than a rule moved
into `HELP`, addressed by topic and fetched with `os_help(topic)` — a
read-only call that opens nothing. The instructions name every topic, so the
existence of the guidance survives any truncation even if the guidance itself
does not.

Tool descriptions are delivered whole (this server's longest is ~1 kB), so
anything that belongs to one tool lives in its description rather than in the
shared text.

The topics: `sessions`, `ownership`, `commit`, `remote_server`, `limits`,
`orm`, `code`, `log`, `records`, `modules`, `jobs`, `tests`, `watching`.
`remote` still answers as an alias for `remote_server`, so an agent that read
the old name — or a transcript that recorded it — does not land on
`no_such_topic`.

In short, what the instructions and the topics between them say:

- A commit on a session handed over with work already pending would write the
  previous owner's work too. `os_session` reports `inherited_pending`, and the
  first `os_commit` is refused with that code and the count, so the agent has
  to say whose work it is before passing `include_inherited=True`. Any commit
  or rollback clears the count. This was documented before and an agent still
  had to remember it; one refusal costs less than one wrong commit.
- Commit writes to a real database — only call `os_commit` after the human
  has granted it. A grant happens in the UI and is not announced in chat:
  poll `os_session` until `allow_commit` is true, then commit. Once granted,
  later commits in that session need no further check-in.
- `with_delay()` enqueues a job; this session will not run the queue. Put
  `queue_job__no_delay=True` on the environment context to run delayed
  methods inline.
- Close a session with `os_close_session` when the work is finished and you
  do not plan to continue. Leaving it open across many steps of the *same*
  work is fine; opening a second one for the next piece while the first is
  still there is not. One at a time, and `os_list_sessions` reports what is
  held under `yours`.
- Editing the project's Python ends the session you have. The interpreter
  imported those files when it started and nothing makes it read them again,
  so the session keeps running the old code however the file on disk now
  reads. Close it and open a new one. Data, views and schema are the
  opposite case — an upgrade picks those up in place.
- The transaction boundaries are tools and only tools: never `env.cr.commit()`
  or `env.cr.rollback()` inside exec'd code. `os_commit` is `flush_all()`,
  `cr.commit()`, `invalidate_all(flush=False)`; a bare `cr.commit()` skips
  both halves, leaving `env` holding stale values, and draws no boundary in
  the journal — so it is a write to a real database that the human watching
  never sees. `env.cr.savepoint()` is the one that belongs in code: nested,
  self-rolling-back, and it persists nothing.
- A record with an XML ID is reached by it, not searched for:
  `env.ref("base.module_integration")`, module and id joined by a dot, with
  `raise_if_not_found=False` for an empty recordset instead of `ValueError`.
- Work only in sessions you opened yourself or were explicitly handed. Never
  attach with a write key you weren't given.
- Never touch `~/.odoo-sheller/` directly and never call the daemon's admin
  endpoints — everything an agent needs is one of the tools above.
- Rollback is cheap and is the right way to end an experiment. Prefer it.
- To see what a record holds, read it whole — `record.read()[0]` with no
  arguments returns every readable field, computed ones included, so a field
  the agent did not think to name is there anyway. The return is always a
  list, one dict per record: `[0]` for one record, the list itself for a
  recordset read in a single call. Relations are not
  followed: a many2one comes back as `(id, display_name)`, an x2m as a list
  of ids, so a level deeper is a second read. A wide model read whole is
  expensive, so it is one record to learn the shape, then `search_read` with
  the fields that mattered.
- To pick up a module's changed data, views or schema in a session:
  `env['ir.module.module'].search([('name','=','sale')]).button_immediate_upgrade()`,
  and `button_immediate_install()` for a module that is not installed yet
  (dependencies come with it; an empty recordset means `update_list()` has
  not been run since the module appeared on disk).
  Either commits by itself — a write the commit gate does not cover — and
  that is deliberately *not* a reason to ask permission: the agent changed
  the code, so the database has to catch up, and an upgrade is the
  consequence of its own edit rather than a decision to put to the human.
  Agents were asking, every time, and the answer was always yes. The one
  exception is a session running on someone else's instance, where access
  was lent and the database was not: never there. It is also how the
  module's migration scripts run, which is usually the point: they are
  selected by `installed_version < script version <= manifest version`, so a
  database that already records the manifest's version redoes schema and data
  but runs no script. Edited Python is not
  picked up at all: the process already imported it, so that needs a new
  session.
- To see *which* code is running, read the loaded registry rather than the
  filesystem: the model's `__mro__` names every module that extends it, in
  resolution order, and `inspect.getsource` on the winning method gives the
  code with the file it came from. On disk every override sits side by side
  and nothing says which is in effect. For views, data and manifests — not
  Python, so `inspect` cannot reach them — `odoo.tools.file_open` reads
  inside the addons paths and refuses a path that escapes them.

## Running a test

`os_run_test("module")`, `os_run_test("module.TestClass")` or
`os_run_test("module.TestClass.test_method")` runs through Odoo's own shell-native test runner
(`odoo.tests.shell.run_tests`) — the same mechanism `odoo-bin shell` itself
would use, not a reimplementation. Unlike every other tool here, it always
opens a **brand-new session** first, rather than running against one the
caller already has: a fresh session has no pending transaction for Odoo's own
test setup to silently discard. That session then **closes itself**: it is
opened with `autoclose`, so once the run settles and is journalled the daemon
closes it —
no `os_close_session` to remember, no container process left running, no test
HTTP daemon holding a port. To run everything a module has, pass the module
rather than one call per class. In the web UI that
session's badge reads `testing` and its tab shows a blinking lamp, so the
human watching can tell a test apart from ordinary `exec`.

When the class name is unknown, call `os_list_tests(module)` rather than
inventing names. Do not open a session per method unless a single
method is the point. Do not fire a list of classes as parallel `os_run_test`
calls: one module run covers them in one session.

A bare module runs what Odoo calls its standard tests, at_install and
post_install — the set `--test-tags /module` runs, not `*/module`. A test
tagged `-standard` or `external` is left out on purpose: those are the ones
that call real third-party services, and running one should be a decision.
Name its class to run it.

Every answer carries `failed`, one entry per failing test:
`{"test": "module.TestClass.test_method", "kind": "failure" | "error"}`. The
daemon reads it off Odoo's own `FAIL:` / `ERROR:` log lines while the run
goes, so `test` is a spec to pass straight back to `os_run_test` — a failed
subtest as its method, a class whose `setUpClass` failed as the class, a test
file whose `setUpModule` failed as the whole module (no tag can name a file),
and anything Odoo named some other way as Odoo named it. Odoo logs a failed
fixture under the suite rather than under the test — `odoo.tests.suite`, or
`unittest.suite` in 15 — and the daemon reads both. The intended loop is a
module run, then one run per failed spec: a class or a method answers with
its log tail, which is where the reason is. A module run's log comes back as
`stderr_lines`, a count, and stays on the journal.

The response separates what the test printed (`stdout`) from what Odoo logged
while it ran (`stderr` for a class or a method, `stderr_lines` for a module),
plus `tests_run`, `failures`, `errors`, `skipped` and
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

Without a `timeout`, the ceiling follows what the spec names: 30 seconds for
a method, 300 for a class, 1800 for a module. One default for every form used
to stop a whole module at 30 seconds unless the agent remembered to say
otherwise. Pass a `timeout` only to go past those — the most is 3600.

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

## Sessions it did not open

A session may run somewhere other than a local container — on an odoo.sh
build, or on a server with Odoo installed in the system, both reached over SSH.
An agent never opens one of those, and the reason is worth being precise about:
it is first the absence of a parameter. `os_open_session` takes `container`,
`database` and `odoo_bin`, and no host, build or card; so does `os_run_test`. There
is no way for an agent to *name* a remote instance, staging or production, and
nothing to forget to enforce. The daemon adds a second refusal for the agent that
goes around the tools: `POST /api/sessions` with a card is `403 needs_a_human`
unless it comes from the UI's own page or carries the admin key, which the agent does
not have and must not read. It reaches a remote instance only through a handover a
human performed after looking at what the instance said it was.

Reopening one is refused too: `os_open_session(replace=...)` on a session
that ran remotely says so in as many words. It used to fail with "container,
database and odoo_bin are required" — which reads as a serialisation
complaint, and an agent that met it went looking for a way around the rule
rather than asking for a handover. A refusal that names the rule is the only
version of that rule an agent can act on.

Once handed one, two things differ, and `os_session` reports both as `kind`
and `stage`:

- **Commit is off for everyone until granted**, not only for the agent.
  Locally a human owner may commit at will because they confirm each one in
  the UI; owning a session is not the same as being entitled to write to
  someone's instance, so there the human grants it the same way they grant it
  to an agent. A handover resets the right in any case.
- **On `production`, an agent never writes**: its commit is `commit_forbidden`
  rather than `commit_not_allowed`. The distinction is the point: the first
  means ask and then watch `allow_commit`, the second means nothing will ever
  grant it *to you*. An agent that treated them alike would poll forever. `exec`
  and `rollback` are untouched — reading a production instance is the
  legitimate case. A human may take the session back and commit it themselves:
  they type the instance's name, and the grant is for one commit, so what you
  prepared and handed over is written by someone who has looked at it.

Tests are the usual reason to be handed a remote session, and
`os_run_test(session_id=...)` runs in one rather than opening its own. That
session is not the agent's: it does not close itself, and the agent does not
close it. `discarded_pending` matters there — a fresh session has nothing to
lose, but a lent one may hold the human's work, and Odoo's own runner rolls
back a mid-transaction cursor before it tests.

## Running under Claude Desktop

Claude Desktop starts the server itself, from
`~/Library/Application Support/Claude/claude_desktop_config.json`. Claude Code
reads `~/.claude.json`, Cursor `~/.cursor/mcp.json`, Zed
`~/.config/zed/settings.json` — the same `command`/`args` entry, under
`context_servers` rather than `mcpServers`, and `args` is required there even
when empty.

The command can be spelled four ways. **From the installed app**, which needs
nothing else on the machine:

```json
{
  "mcpServers": {
    "odoo-sheller": {
      "command": "/Users/<you>/.odoo-sheller/bin/odoo-sheller-mcp",
      "args": []
    }
  }
}
```

`bin/odoo-sheller-mcp` is a link the app points at whatever copy of itself is
installed now, refreshed on every launch — so an update or a move leaves the
entry correct, and the link is there from the first start rather than from the
first visit to a menu. The real path,
`/Applications/odoo-sheller.app/Contents/Resources/odoo-sheller-mcp/odoo-sheller-mcp`,
works too and is what the menu falls back to if the link cannot be made. Write
the home directory out: these files are read by programs that do not expand
`~`.

**From a checkout**, the console script `uv sync` installs — one process
rather than a wrapper around one:

```json
{
  "mcpServers": {
    "odoo-sheller": {
      "command": "<project path>/.venv/bin/odoo-sheller-mcp",
      "args": []
    }
  }
}
```

**From a checkout, through uv**, when you would rather the environment be
synced from `uv.lock` at every start:

```json
{
  "mcpServers": {
    "odoo-sheller": {
      "command": "<absolute path to uv>",
      "args": ["--directory", "<project path>", "run", "python", "-m", "odoo_sheller.mcp"]
    }
  }
}
```

**From a containerized daemon**, when the machine has no odoo-sheller
installed at all and the daemon is the image from
[container.md](container.md):

```json
{
  "mcpServers": {
    "odoo-sheller": {
      "command": "docker",
      "args": ["exec", "-i", "odoo-sheller", "python", "-m", "odoo_sheller.mcp"]
    }
  }
}
```

Nothing starts that server: the image carries it, and the client spawns it on
demand. `-i` is what keeps stdin open, and `-t` must not be added — a
pseudo-terminal would merge the frame stream into the log stream. The server
finds the daemon inside its own container, so no address is configured.

Rather than filling in the container name by hand, ask the daemon:
`GET /health` returns the command under `mcp`, with the container's own id in
it, and that answer is correct whatever the container was named. A native
daemon answers the same field with its own interpreter, so one question covers
both cases.

Absolute paths for `command` and `--directory` are safer in practice: hosts
launch their servers with a minimal `PATH`, so a bare `uv` can fail to
resolve. `docker` is the exception worth naming: if the host cannot resolve it,
spell that one out too — `/usr/local/bin/docker` on most machines.

The desktop app's **Settings… → MCP** shows the first form with the
real path filled in, in both shapes, and copies it to the clipboard. It never
writes to an agent's config: those files are yours, they hold servers this
project knows nothing about, and `~/.claude.json` is live state that a running
Claude Code rewrites under you.

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
listening on a different port, set `ODOO_SHELLER_URL` (in the
`mcpServers` entry's own `env`, or the shell that starts Desktop) rather than
changing code. The name must stay a loopback one — `127.0.0.1`, `localhost` or
`[::1]`: the daemon refuses a request addressed to any other, as `foreign_host`
(see [security.md](security.md)).

When the daemon isn't reachable at all, every tool returns the same shape
instead of a raw transport error:

```json
{
  "error": "daemon_unreachable",
  "url": "http://127.0.0.1:8765",
  "recovery": "ask the human to start it: uv run python -m odoo_sheller"
}
```
