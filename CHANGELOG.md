# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [1.0.0] — 2026-08-21

A persistent Odoo 19 REPL behind an HTTP/WebSocket API: the daemon keeps
`odoo-bin shell` alive inside a local Docker container, so the registry loads
once instead of once per command. A web UI drives it; an MCP server gives an
agent the same API. Rollback is the default everywhere and commit is always an
explicit, confirmed act.

### Daemon and protocol

- Persistent session over `docker exec`: the bootstrap runs inside the
  container's own Python and takes over the point where `odoo-bin shell` would
  start an interactive console. Nothing is installed in the container and
  nothing is copied into it — the bootstrap arrives as a heredoc.
- `exec 3<&0` keeps the command pipe alive on fd 3 after Odoo replaces its own
  stdin with that heredoc.
- Frames on stdout, Odoo's logs on stderr — split by stream, not by markers.
  The bootstrap takes a private dup of fd 1 and points fd 1 at stderr, so a
  stray `print` from a background thread cannot corrupt the stream.
- Payload ceilings (`MAX_STDOUT`, `MAX_RESULT`) shared between `protocol.py` and
  the bootstrap, with the subprocess reader raised to match: a clipped frame is
  still far larger than asyncio's default 64 KiB line limit, and a reader that
  dies there would leave the session busy forever.
- Live target discovery: `docker ps` for containers, then a one-shot probe for
  odoo-bin, Odoo and Python versions, config path and database list. Odoo 19 is
  verified and supported; anything else is refused at connect time.
- Binds `127.0.0.1` only, with no authentication. An admin key guards actions on
  sessions you do not own — an accident guard, not a security boundary.

### Sessions

- One command at a time per session. A second is refused, never queued.
- `ready` is reached on the bootstrap's `hello` frame, never on a timer.
- Explicit transaction control. Commit is `flush_all()`, `cr.commit()`,
  `invalidate_all(flush=False)`; rollback is `invalidate_all(flush=False)` then
  `cr.rollback()`. The `flush=False` matters: the default would write out
  exactly what the rollback is about to discard.
- `SIGINT` interrupt that surfaces as `KeyboardInterrupt` inside the command.
- A command that blows its ceiling keeps the session busy until it really ends —
  `SIGINT` is a request, not a guarantee — and its late result is journalled as
  `abandoned_result`. `close` is accepted even while busy.
- Process death is a normal outcome: EOF on stdout moves the session to `dead`
  and pending requests fail with the tail of stderr attached.
- Many sessions, keyed by id, each with its own target. Sessions cannot outlive
  the daemon.

### HTTP and WebSocket

- `POST /api/sessions` blocks until `hello`. Watchers hear `session_starting`
  as soon as the session is registered, then `session_opened` when it is ready.
  A start that never reaches `hello` emits `session_failed` with the reason, so
  a watcher does not keep a session that never became ready and never went away.
- `POST /api/sessions` accepts an opaque `client_token`, echoed back in
  `describe()`. Container and database do not identify a session — a container
  may hold several, and an agent can be opening the same target — so the token
  is how a client recognises its own `session_starting`.

### Ownership and agent access

- Every session has an owner (`human` or `agent`) and a write key returned once,
  at open or at handover, and required by `exec`, `commit` and `rollback`.
- Handover moves the right to type without disturbing the process, the namespace
  or the open transaction. The human stays admin: they watch any session,
  interrupt, close, kill, hand it around and grant commit.
- `allow_commit` gates an agent only; a human owner confirms each commit in the
  UI instead.
- `odoo_sheller/mcp.py` — the agent's client over stdio, with no logic of its own.
  Refusals carry what the agent needs to act on them: `session_busy`,
  `session_gone`, `not_owner`, `commit_not_allowed`.
- The server's instructions cover idiomatic ORM usage: `mapped()` instead of a
  hand-rolled loop — including the non-obvious part, that a dotted path like
  `partner_id.bank_ids` returns the de-duplicated union as a recordset, not a
  list of lists — `filtered()` / `sorted()`, pushing a filter into `search()`
  instead of fetching broadly and filtering in Python, `search_count()` over
  `len(search(...))` when only a count is needed, the set operators (`|` `&`
  `-` `in` `<=` `<` `>=` `>`) recordsets support directly, and `ensure_one()`
  for a helper meant to run against a single record.
- The instructions are explicit that `allow_commit` gates `os_commit` only —
  `os_exec` is never gated by it — and that once the human grants commit, the
  agent calls `os_commit` directly for every later commit in that session; the
  ask-first ritual is for the first refusal, not a standing requirement to
  check in before every commit.

### Web UI

- Three screens — Connect, Sessions, Journals — served from `odoo_sheller/web/`
  with no build step. CodeMirror is vendored; the page makes no external
  requests. The last screen and the last used target are remembered in
  `localStorage`.
- Session pane: editor, cell feed, and an Odoo stderr split with a level
  filter, a tail that follows only while you are at the bottom, and a focus
  mode that hides the editor and the cell feed so the log fills the whole
  pane — its top border lines up exactly where the editor's did, rather than
  sitting right under the session keyboard.
- Cells fold to their header — the whole header is the target, not just the dot.
  Agent-authored cells arrive folded; your own arrive open.
- A two-row session keyboard where keys stay in place: a control that does not
  apply is disabled, never hidden.
- Connect's **Start** streams the container's stderr into a 12-line log well
  while Odoo loads its registry, paced into the well rather than dumped in one
  paint. A failed start keeps the well so the last lines stay readable next to
  the error. The well follows the tail only while you are at the bottom; scroll
  back to read a traceback and a re-render leaves you there. **Refresh** while a
  session is opening no longer takes the container list down with it: the picker
  waits for the re-probe, the log well does not.
- Journals screen: rows grouped by container and database, a sticky column
  header, a transcript preview, and per-row and per-group deletion.
- Assets are served `no-store`, so there are no hand-maintained cache busters.

### Journals

- Append-only JSONL per session under `~/.odoo-sheller/journals/`: open with target
  and versions, every command with its code, every result in full, transaction
  boundaries, interrupts, process death, and Odoo's stderr interleaved by time.
  The file on disk is never rewritten.
- Outlives both the session and the daemon. Exportable as raw JSONL or as a
  Markdown transcript, both carrying session metadata.
- Truncation is a display concern only: the bootstrap caps payloads to protect
  the pipe, the journal stores whatever arrived in full, and the API shortens it
  for the UI.
- `/api/sessions/{id}/history` answers for a closed session too, with
  `session.state: "gone"` and a `session.gone` object saying how to recover.
- Deleting a journal takes the admin key and matches the id exactly — the one
  irreversible file operation in the API.
- **Journals are unmasked.** They can contain credentials read from the
  database. They stay local, stay out of git, and must be reviewed before being
  shared.

### Documentation

- A `docs/` folder, tracked in git: `architecture.md` (protocol, session state
  machine, journal format), `ui-guide.md` (every screen and control),
  `agent-guide.md` (MCP tool list, Claude Desktop wiring), `security.md` (the
  actual threat model, and why journals aren't masked), and an FAQ in English
  and Russian. `docs/README.md` indexes all of it.
- Replaces the working notes this project used while it was being built —
  design specs and task-by-task plans written for an AI pair-programming
  session, not for someone reading the repository afterward. Those stay out
  of git; nothing they said that still matters was left out of the new pages.
- README and CLAUDE.md point at the new pages instead of the old ones.

### Cleanup

- The rebrand from py-tunnel missed three environment variables: `PT_TUNNEL_URL`
  is now `ODOO_SHELLER_URL`, and the bootstrap's `PT_CMD_FD` (a test-only knob,
  never set in a real run) is now `OS_CMD_FD`. `PT_AGENT_LABEL` is gone
  entirely — it was never actually overridden, so the agent's session label is
  now just the constant `"mcp-agent"`.

### Known limitations

- Odoo 19 and local Docker only. Remote hosts over SSH and odoo.sh are out.
- `/docs` is the one page that needs network: the swagger-ui bundle comes from a
  CDN. `/openapi.json` is served locally.
- Journal masking is not implemented, and there are no production-database
  guards — local Docker only, by definition.
- Deferred: outgoing HTTP tracing, `changed` record diffing, synchronous
  `with_delay`, and live streaming of output while a command runs.

[Unreleased]: https://github.com/romi477/odoo_sheller/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/romi477/odoo_sheller/releases/tag/v1.0.0
