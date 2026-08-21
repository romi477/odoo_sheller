# Architecture

How odoo-sheller works internally: the process model, the wire protocol
between daemon and container, the session state machine, and the journal
format. Start with the README if you just want to run the thing — this is for
understanding or extending it.

## The three pieces

```
browser ──HTTP/WS──> daemon (macOS, Python) ──pipe──> docker exec ──> odoo-bin shell
                     127.0.0.1:8765          stdin/fd 3 + stdout      bootstrap loop
```

- **The daemon** is the only piece that knows about pipes, framing, and
  containers. It runs as a plain process on the host — not containerized —
  and speaks HTTP and WebSocket to whoever is driving it.
- **The web UI** is one client of that API, served by the daemon itself at
  `/web`. An MCP server (`odoo_sheller/mcp.py`) is a second client, for an
  agent. Both see the same surface — there is deliberately only one API.
- **The bootstrap** is a small Python loop that runs *inside* the target
  container, under Odoo's own interpreter. It has no dependency on this
  project beyond the standard library, because it runs in whatever Python the
  container happens to ship.

## Reusing `odoo-bin shell`, not reimplementing it

Odoo's own shell command already does the hard part: parse the config, build
the registry, assemble `env`, roll back on exit. odoo-sheller does not
reimplement any of that. It only takes over the one moment where `odoo-bin
shell` would hand control to an interactive console.

The reference is `odoo/cli/shell.py`. Three facts make the whole design work:

- When stdin is not a TTY, `console()` does `exec(sys.stdin.read(),
  local_vars)` — that single `exec` call is the bootstrap's entry point.
- The namespace it executes in already contains `env` and `self`.
- `cr.rollback()` runs after the console function returns, so an ordinary
  process exit is a free rollback — the safety net that makes rollback the
  default everywhere.

Because the bootstrap only depends on the non-TTY branch and the names `env`
and `self`, it happens to be version-neutral by construction. Only Odoo 19 is
verified, though — the probe (below) refuses anything else at connect time
with a clear message, rather than failing on the first command.

## Starting a session

```bash
docker exec -i <container> sh -c 'exec 3<&0; exec <odoo-bin> shell -d <db> --no-http <<"OSBOOT"
<bootstrap source>
OSBOOT'
```

`exec 3<&0` duplicates the `docker exec` stdin pipe onto file descriptor 3
before Odoo replaces its own stdin with the heredoc. Odoo reads the bootstrap
source, hits EOF immediately, and executes it; the command channel survives on
fd 3 for as long as the process lives. The bootstrap travels inline as a
heredoc — nothing is written into the container's filesystem, nothing needs
cleanup, and the running bootstrap always matches the daemon that spawned it.

The first thing the bootstrap does is claim stdout for its own frames and
close it to everyone else:

```python
frames = os.fdopen(os.dup(1), "w")   # a private handle to the real stdout
os.dup2(2, 1)                        # fd 1 now points at stderr
```

After this, any stray write to fd 1 — a `print` left in module code, a
background thread, C-level output — lands in Odoo's log stream instead of
corrupting a frame. This is why the split between "commands" and "logs" is by
*stream*, not by markers: stdout carries frames only, stderr carries
everything Odoo would normally print. `frames.flush()` after every frame is
mandatory — stdout is block-buffered when it is not a TTY, and a missing flush
hangs the session with no visible symptom.

## Wire protocol

One JSON object per line, in both directions. JSON's own string escaping
handles newlines inside user output, so the line is an unambiguous frame
boundary without inventing markers.

Daemon → bootstrap:

| `t` | Fields | Meaning |
|---|---|---|
| `exec` | `id`, `code` | run this code |
| `commit` | `id` | end the transaction, keep it |
| `rollback` | `id` | end the transaction, discard it |
| `close` | `id` | leave the loop |

Bootstrap → daemon:

| `t` | Fields |
|---|---|
| `hello` | `protocol`, `odoo`, `python`, `db`, `uid`, `pid` |
| `result` | `id`, `stdout`, `result`, `error`, `duration` |
| `bye` | `id` |

Unknown frame types are ignored on both sides, so a future addition (a
streamed `out` frame, say) does not break an older peer.

### Running a command

Code is compiled through `ast`. If the last statement is a bare expression, it
is split off: everything before it runs normally, the last expression is
evaluated separately, and its `repr()` comes back as `result` — the same thing
an interactive interpreter does when you type an expression with nothing to
capture it. `env['res.partner'].search([])` on its own line shows what it
found without a `print`.

Errors come back as structure, not text: `{"type", "message", "traceback"}`.
The bootstrap's own stack frames are stripped out first, so a traceback shows
only the user's code. Each command compiles under a synthetic filename
(`<os-cell-N>`) so line numbers in that traceback line up with what was typed.

`stdout` and `result` are capped at a fixed size (order of 1 MB) before they
go on the wire, to protect the pipe from an accidental firehose. The journal
(below) keeps the untruncated version; the API trims further for the UI and
marks what it cut.

### Committing and rolling back

The order here is deliberate and not the obvious one:

```python
# commit
env.flush_all()                  # push pending ORM writes to the database
env.cr.commit()
env.invalidate_all(flush=False)

# rollback
env.invalidate_all(flush=False)  # drop the cache before discarding anything
env.cr.rollback()
```

`Environment.invalidate_all()` defaults to `flush=True`. On the rollback path
that default would write out exactly what the rollback is about to throw
away — so `flush=False` is required there, not optional. Invalidation itself
is required on both paths: `cr.commit()` ends the transaction but leaves
`env`'s cache holding what it read before, so a change made concurrently
through the regular web UI would stay invisible to the shell. Skipping this
step doesn't fail loudly — it just quietly lies about the state of the
database.

### Interrupting a command

`docker exec -i` without a TTY does not forward signals to the process inside
the container — sending `SIGINT` to the local `docker exec` client does
nothing to the shell it's driving. So the bootstrap reports its own
in-container PID in the `hello` frame, and an interrupt is a separate call:
`docker exec <container> kill -INT <pid>`. A kill is the same shape with
`-KILL`.

`KeyboardInterrupt` is handled in two places. Raised while a command is
executing, it becomes an ordinary error frame and the session stays `ready` —
this is what makes the UI's Interrupt button work. Raised while the loop is
blocked reading the next frame, it means a signal arrived between commands; it
is swallowed silently so the loop does not exit for no reason.

## Session state machine

```
starting ──(hello)──> ready ──(exec)──> busy ──(result)──> ready
    │                   │                 │
    └───(died)──────────┴─────────────────┴──> dead
                        └──(close)──> closed
```

- `ready` is reached only on the bootstrap's `hello` frame — never assumed
  after a fixed delay. `starting` covers however long Odoo's registry load
  takes, which varies by module count.
- One command at a time, full stop: one process, one namespace, one open
  transaction. A second command arriving while `busy` is refused outright,
  never queued — a queue would misrepresent to the caller when their command
  actually ran.
- **A command that hits its timeout does not free the session.** `SIGINT` is a
  request, not a guarantee: the code on the other end can catch
  `KeyboardInterrupt`, or be blocked in a C call that ignores signals
  entirely. The session stays `busy` until the real result frame eventually
  arrives — at which point it's journalled as `abandoned_result` and the
  session returns to `ready`. Reporting `ready` any earlier would let a new
  command queue up silently behind the one that's still actually running,
  which is the exact ordering lie the "no queue" rule exists to prevent.
  `close` is the one frame still accepted while `busy`, so a stuck session is
  never unkillable.
- Many sessions can be live at once, each keyed by id with its own container
  and database. The registry that tracks them is plural from the start — the
  web UI drives several as tabs, and an agent attaches to the same registry
  rather than a separate one.
- Process death is a normal, expected outcome, not a special case: EOF on the
  pipe moves a session straight to `dead`, and anything still waiting on a
  result fails with the tail of stderr attached, so the cause is visible
  without digging through logs.
- Sessions cannot outlive the daemon. The daemon owns the pipes; when it
  exits, the container-side process loses its stdin and exits too.

## Ownership and agent access

Every session has an owner — `human` or `agent` — and a write key. The key is
returned exactly once, at open or at handover, and is required for `exec`,
`commit`, and `rollback`. Watching a session (seeing its output as it happens)
needs no key at all; only typing into it does.

A **handover** moves the right to type without disturbing anything else: the
process, its namespace, and any open transaction all survive. The previous
owner's key stops working for typing, but keeps working for `close`, `kill`,
and taking the session back — giving away the right to type is not the same
as giving away the session.

The human is always the admin: they can watch any session, interrupt it,
close or kill it, hand it to an agent, and grant or revoke `allow_commit` —
but they never type into a session they don't currently own themselves.
`allow_commit` only gates an *agent*; a human owner confirms each commit
through the UI instead, so there's nothing to grant there.

`odoo_sheller/mcp.py` is the agent's client over stdio. It holds no session
state of its own beyond ids and keys — every actual operation is the same
HTTP call the browser would make. See [agent-guide.md](agent-guide.md) for the
tool list and how a handover looks from the agent's side.

## Target discovery

Targets are found live, never configured up front. `docker ps` lists running
containers; a one-shot probe inside a chosen container reports the `odoo-bin`
path, Odoo and Python versions, the config file location, and the database
list (read via `psycopg2`, which any Odoo container already has installed —
nothing extra to add). The probe process exits the moment it has answered.
Odoo versions other than 19 are refused right here, with a specific message,
rather than accepted and left to fail on the first real command.

## Journal

The daemon keeps one append-only JSONL file per session under
`~/.odoo-sheller/journals/`. It records: the session opening (target,
versions), every `exec` with its code, every result in full (never the
truncated wire version), every commit/rollback/interrupt, process death, and
Odoo's own stderr lines — all interleaved by timestamp, so it's possible to
see which log lines a given command produced.

The file outlives both the session and the daemon process. It is never
rewritten, only appended to, and it can be exported as raw JSONL or rendered
as a Markdown transcript; both forms carry the same session metadata (id,
container, database, versions, in-container PID, timestamps, command count,
whether anything was committed).

Truncation is purely a *display* concern: the wire protocol caps payload size
to protect the pipe, but the journal stores whatever the bootstrap sent in
full, and the API shortens further only when handing something to the UI. The
complete text is always recoverable straight from the journal file.

**Journals are unmasked.** Anything your code reads out of the database —
including a credential or an API key — lands in the journal in plain text.
See [security.md](security.md) for why this isn't "fixed" and what that means
in practice.

## What's deliberately not here

Outgoing HTTP call tracing, record-level "what changed" diffs, synchronous
execution of `with_delay` jobs, live streaming of output while a command runs,
remote (non-Docker, non-local) targets, and Odoo versions other than 19. None
of these are technically precluded by the protocol or the API — they're just
not built, and the UI doesn't pretend they exist.
