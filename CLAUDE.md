# odoo-sheller

Persistent Odoo shell proxy. Keeps a long-lived Odoo REPL alive inside a local
Docker container and exposes it over HTTP/WebSocket, so a web UI (stage 1) and
later an AI agent (stage 2) can run ORM code without paying registry startup on
every call.

**Status: implemented.** MVP runs: `python -m odoo_sheller` (add `--reload` for
development — it kills live sessions on every restart). Unit tests need no
Docker; `tests/test_e2e.py` runs against the `integra19` container.

| Document | What it holds |
|---|---|
| `docs/README.md` | index of everything below |
| `docs/architecture.md` | the design of record — protocol, session state machine, journal format |
| `docs/ui-guide.md` | UI behavior: screens, states, guarantees |
| `docs/agent-guide.md` | MCP tool list, ownership from the agent's side, Claude Desktop wiring |
| `docs/security.md` | the security model — what's protected, what isn't, and why |
| `docs/faq.md`, `docs/faq-ru.md` | plain-language FAQ, English and Russian |
| `CHANGELOG.md` | what shipped in each version, newest first |

This file is the short version for working sessions. Where it and
`docs/architecture.md` disagree, `docs/architecture.md` wins.

## Why

Running `docker exec ... odoo-bin shell < script.py` per call costs ~1.4s of
registry load plus per-call boilerplate (temp file, flags, output markers, log
filtering). Over dozens of calls in one session that dominates. A persistent
session removes both: one startup, one namespace, explicit transaction control.

## Architecture

```
browser ──HTTP/WS──> daemon (macOS, Python) ──pipe──> docker exec ──> odoo-bin shell
                     localhost:8765          stdin/fd3 + stdout      bootstrap loop
```

Only the daemon knows about pipes and framing. Browser and (later) agent speak
the same HTTP/WS API — there must never be a second API.

### Modules

| Module | Responsibility |
|---|---|
| `protocol.py` | frame encode/decode |
| `discovery.py` | list containers, probe one for odoo-bin / version / config / databases |
| `transport.py` | spawn process for a target, expose pipes, signal, kill |
| `bootstrap.py` | the loop that runs *inside* the container |
| `session.py` | one live session: state, request serialization, commit/rollback/close/interrupt |
| `registry.py` | sessions by id |
| `journal.py` | append-only JSONL record of everything, per session |
| `api.py` | HTTP + WebSocket |
| `web/index.html` | UI, no build step (behavior spec: `docs/ui-guide.md`) |

Commands go over plain HTTP request/response — `exec` blocks until the command
finishes (default ceiling 5 min, then `SIGINT` and a timeout error). WebSocket
carries only what arrives on its own: state transitions, process death, stderr
lines. Busy or still starting is `409`, dead session is `410`.

## Core mechanism

Odoo's own `odoo-bin shell` does all setup — config parsing, registry, `env`,
rollback on exit. We do not reimplement any of it. We only take over the part
where it would start an interactive console.

Reference source: `/opt/odoo-src/odoo19/odoo/cli/shell.py`.

Facts it relies on (verified against Odoo 19):

- `shell.py:80-82` — when stdin is not a tty, `console()` does
  `exec(sys.stdin.read(), local_vars)`. This is the entry point for our loop.
- `shell.py:142-143` — namespace contains `env` and `self`.
- `shell.py:149` — `cr.rollback()` runs after the console returns, so dry-run
  by default is free.
- `shell.py:77` — `SIGINT` raises `KeyboardInterrupt`, which gives us a working
  "interrupt running command" button.
- `shell.py:63-67, 89` — the `--shell-file` flag exists but is only read in the
  tty branch. Unusable for us.
- `sql_db.py:240-245` — `with registry.cursor()` commits on clean exit. Together
  with `shell.py:149` that means a normal close discards uncommitted work, and a
  bootstrap crash rolls back.

Startup command shape:

```bash
docker exec -i <container> sh -c 'exec 3<&0; exec <odoo-bin> shell -d <db> --no-http <<"OSBOOT"
<bootstrap source>
OSBOOT'
```

`exec 3<&0` duplicates the docker exec stdin pipe to fd 3 before Odoo replaces
its own stdin with the heredoc. Odoo reads the bootstrap, hits EOF immediately,
executes it; the command pipe survives on fd 3. The bootstrap is passed inline
as a heredoc — nothing is copied into the container, nothing to clean up.

### Non-obvious constraints

- **stdout is frames only, stderr is Odoo logs.** The split is by stream, not by
  markers. The bootstrap takes a private dup of fd 1 and points fd 1 at stderr,
  so stray prints from Odoo or background threads cannot corrupt the stream.
- **stdout is block-buffered when not a tty.** Flush after every frame or the
  session hangs with no visible cause.
- **`bootstrap.py` runs in a foreign interpreter.** Stdlib only, no imports from
  this project, conservative syntax — container Python versions vary. A test
  must enforce this.
- **Transaction boundaries must invalidate the ORM cache, in the right order.**
  `cr.commit()` ends the transaction but leaves `env` holding stale values, so
  changes made meanwhile through the web UI stay invisible. Commit is
  `flush_all()` then `cr.commit()` then `invalidate_all(flush=False)`; rollback
  is `invalidate_all(flush=False)` then `cr.rollback()`. The `flush=False` is
  required — the default `True` would write out exactly what a rollback is about
  to discard (`odoo/orm/environments.py:357`).
- **Odoo 19 only.** The bootstrap happens to be version-neutral by construction
  (it depends only on the non-tty `console()` branch and the names `env` /
  `self`), but only 19 is verified and supported. The probe refuses anything
  else at connect time with a clear message rather than failing later. Older
  versions are a migration target, not a requirement.

## Session model

```
starting ──(hello)──> ready ──(exec)──> busy ──(result)──> ready
    │                   │                 │
    └───(died)──────────┴─────────────────┴──> dead
                        └──(close)──> closed
```

- `ready` is reached on the bootstrap's `hello` frame, never on a timer.
- One command at a time per session. A second command while `busy` is rejected,
  not queued — a queue would lie to the caller about ordering and timing.
- A command that hits its timeout keeps the session `busy`: `SIGINT` is a
  request, not a guarantee, so the command may still hold the container. The
  session returns to `ready` only when the late result arrives (journalled as
  `abandoned_result`). `close` is accepted even while busy.
- Many sessions, keyed by id, each with its own target. The UI drives one; the
  registry is plural from day one so stage 2 does not need a rewrite.
- Process death (container restart, crash, kill) is a normal outcome: EOF on
  stdout moves the session to `dead` and pending requests fail with the tail of
  stderr attached.
- Sessions cannot outlive the daemon. Daemon dies, pipe dies, container process
  dies.

## Target discovery

Targets are discovered live, not configured. `docker ps` lists running
containers; a one-shot probe inside a chosen container reports odoo-bin path,
Odoo version, Python version, config file location, and database list (via
`psycopg2`, which any Odoo container has). Then the user picks a database and
connects. Last used target is remembered in `localStorage`.

## Journal

The daemon keeps an append-only JSONL file per session under
`~/.odoo-sheller/journals/`: session open with target and versions, every `exec`
with its code, every result in full, transaction boundaries, interrupts,
process death, and Odoo's stderr lines interleaved by time. It outlives both the
session and the daemon, and is exportable as raw JSONL or as a Markdown
transcript.

Every export carries session metadata (id, container, database, versions, pid,
timestamps, command count, committed flag): the JSONL stream gets an
`export_meta` first line, the Markdown transcript a header table, and
`/api/sessions/{id}/history` a `session` key. The file on disk is never
rewritten.

Truncation is a display concern only: the bootstrap caps payloads at a hard
ceiling to protect the pipe, the journal stores whatever arrived in full, and
the API shortens it for the UI. The full text is always recoverable from the
journal.

## MVP scope

In: discovery, connect, persistent REPL, frame protocol, commit / rollback /
close / interrupt / kill, stderr panel, web UI.

Out, deliberately deferred: outgoing HTTP tracing, `changed` record diffing,
synchronous `with_delay` execution, live streaming of output while a command
runs, MCP server, remote hosts over SSH, odoo.sh. Local Docker only.

## Conventions

- Stage 1 is the web UI; stage 2 attaches an agent to the same API.
- Docs and specs in English. Discussion in this project happens in Russian.
- The daemon is not containerized: plain venv on macOS. The only non-Python
  dependency is the `docker` CLI. Nothing is installed inside the target
  container either — the bootstrap needs only `sh` and Odoo's own Python. If a
  system utility ever becomes necessary, containerize the daemon rather than
  grow host setup steps.

## Ownership (stage 2)

Sessions have an owner (`human` or `agent`) and a write key, returned once at
open or handover and required by `exec`, `commit`, `rollback`. The human is the
admin: they watch any session, interrupt, close and kill it, hand it around and
grant `allow_commit` — but never type into a session they do not own. Handover
keeps the process, namespace and open transaction; only the right to type moves.
`odoo_sheller/mcp.py` is the agent's client, over stdio, with no logic of its own.

Keys are an accident guard, not a security boundary: daemon and agent run as the
same user. Details: `docs/agent-guide.md`, `docs/architecture.md`.

## Security posture

This tool executes arbitrary code as `SUPERUSER_ID` against a real database.
Rollback is the default everywhere; commit is always an explicit, confirmed act.

- The daemon binds `127.0.0.1` only and has no authentication. Anyone who can
  reach the port can execute code. Never expose it.
- **Journals are unmasked.** They can contain credentials and API keys read out
  of the database. Masking must key off field meaning, not dict key names — a
  name-based filter once let a webservice key through a `{"name": "key",
  "value": ...}` structure and into a transcript in plain text. Proper masking
  is out of scope for the MVP, so journals stay local, stay out of git, and must
  be reviewed before being shared.
- Production-database guards are out of scope: local Docker only, by
  definition.
