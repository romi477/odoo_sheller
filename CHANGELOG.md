# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] — 2026-08-28

The MCP server now spells out the working habits that were left as guesswork:
how to run delayed jobs in the shell, when to close a session, and how to
notice Grant commit without being told in chat. An agent can list one addon's
tests and run a class or method by name in its own session. The Sessions
screen tells a test run apart from ordinary `busy`, and a session clock sits
on the header.

### Agent access

- Instructions: put `queue_job__no_delay=True` on the environment context so
  `with_delay()` (and nested delays) run inline instead of enqueueing. This
  session still does not run the job queue for you.
- Instructions: a session may stay open across many steps; call
  `os_close_session` when the work is finished and you do not plan to
  continue.
- Instructions: after `commit_not_allowed`, explain the write, then poll
  `os_session` until `allow_commit` is true. Do not wait for a chat
  confirmation, and do not spin on `os_commit`. Once granted, later commits
  in that session need no further check-in.
- `os_session` — GET of the current session, including `allow_commit`. That
  is how an agent sees a grant flipped in the UI.
- `os_run_test` — runs one Odoo test method or a whole test class
  (`module.TestClass` / `module.TestClass.test_method`) through Odoo's own
  shell-native test runner (`odoo.tests.shell.run_tests`), not a
  reimplementation. Always opens its own brand-new session first, so it never
  has a pending transaction to lose; the session is left open afterwards.
  stdout and the Odoo log lines produced during the run come back separated.
  `tests_run: 0` is reported explicitly — Odoo's own result object reads as
  "successful" even when a name matched nothing. Calling it a second time in
  the same session after `os_exec` left work pending discards that work
  (Odoo's own test runner rolls back a mid-transaction cursor before testing);
  the response's `discarded_pending` field says so. The bootstrap picks a
  free port for the test HTTP daemon `odoo.tests.shell.run_tests()` spawns
  the first time it runs in a session, and sets it on the server object
  itself (not just `config['http_port']`, which is already too late to
  matter — the object's own `port` attribute was fixed at shell startup) —
  otherwise it collides with the container's own Odoo on the configured
  port and takes the session down with it.
- A `run_test` journal entry is now recognized by `os_history` and
  `os_journal` (previously only `exec` was) — its outcome (`tests_run`,
  `failures`, `errors`, `skipped`, `success`, stdout/stderr, duration) is
  shaped the same way `os_run_test` answers with, so a result is still
  recoverable after a client-side transport timeout on a long-running test
  class, the same way an abandoned `exec` already was.
- A client-side read timeout is now reported as `request_timed_out`, not
  `daemon_unreachable` — the daemon is still working, not down. The
  instructions tell the agent to poll `os_history`/`os_journal` on the
  session instead of retrying the call, which would otherwise start a
  second, duplicate run on top of the one already in flight.
- Instructions: one `os_run_test` call, one session, one close — running
  several test classes in a row means opening, reading, and closing that
  many sessions in turn, not leaving several open at once.
- `os_list_tests` — list test classes and methods in one addon (`module`,
  optional `container`), already shaped as `os_run_test` specs. Disk
  catalogue via `GET /api/containers/{container}/tests?module=`; read-only,
  opens no session. It lists what Odoo's own loader would run: any class
  carrying a `test_*` method (not only ones named `Test*`), and only in the
  modules `tests/__init__.py` actually imports, so a spec it hands out is
  never one that comes back `tests_run: 0` for having never been loaded.
- `run_test` collects the Odoo log lines of its own run as they arrive rather
  than slicing the session's rolling stderr tail. That tail holds 2000 lines;
  a real test class logs many times more, so the window used to come back
  empty on exactly the runs worth reading. Output past `RUN_STDERR_LIMIT`
  keeps the tail and sets `stderr_truncated`.
- The rebuilt command feed numbers `exec` and `run_test` on one counter.
  Two separate counters could hand the same number to two commands, and
  disagreed with the Markdown transcript for the same journal.
- `os_run_test` waits out the daemon's own registry-load ceiling when opening
  its session, instead of giving up first and stranding a session whose write
  key it never received. If the open does time out, the refusal carries the
  `client_token` to find that session by.
- A session opened by `os_run_test` closes itself once the run has settled
  and been journalled — nothing to remember, no container process or test
  HTTP daemon left holding on. Never set for a human's session. A run that
  blew its ceiling is not settled yet, so the close waits for the late
  result rather than killing a run still in progress; a process that died
  is reaped the same way.
- `os_test_result(session_id)` — waits for a run started by `os_run_test`
  and answers with its outcome, or `running` (call again), or `lost` if the
  run died with its process. Read-only and needs no write key, so it works
  after the session has closed itself and after this server restarts.
- `os_run_test` stops waiting at `MCP_CALL_BUDGET` (40s, override with
  `ODOO_SHELLER_MCP_BUDGET`) and answers `status: "running"` with the
  session id instead of being killed by the host mid-call. The daemon still
  gets the full `timeout` the caller asked for — only this server's own
  waiting is capped, so the run itself is never cut short. The budget covers
  the whole call, opening the session included: spending it on the run leg
  alone, on top of a registry load that already took seconds, overshot the
  host's own limit and the call died before it could hand back the session
  id. No leg may add a margin on top of the cap either — a margin over a cap
  defeats the cap.
- `os_run_test` clips `stderr` from the end rather than the beginning. The
  line worth reading is the last one (`Tests passed: …`, or the failure that
  ended the run); clipping from the front returned the test framework's boot
  chatter and dropped the answer. It also forwards the daemon's own
  `stderr_truncated`, which reports dropped *lines* and is a different loss
  from this server's character clip.
- `os_list_sessions` drops keys for sessions the daemon no longer has, so
  `yours` stops listing ids of sessions that already closed themselves.
- The same stderr tail rule and `stderr_truncated` flag now apply when a run
  is read back through `os_test_result` or `os_history`, not only when
  `os_run_test` answers directly — both go through the journal, where the
  clip was still taking the head.
- `run_test` rejects a timeout of zero or less (and anything over an hour):
  it would send the frame and abandon it in the same breath, leaving the
  session busy for the whole real length of the run.

### Web UI

- Session header meta line: local start time and a live age in seconds
  (`14:42:07 (18s)`). The tick updates that span only. A session this tab
  opened is stamped immediately; any other uses history `opened_at`.
- Cell feed hides its scrollbar (same pattern as the journal list). The
  CELLS heading stays put; cards size to their content so unfold still
  works when the feed is long.
- While a test is running, the session badge reads `testing` in Journals-warning
  rose. The session tab keeps its usual cyan/amber color and grows a blinking
  rose lamp instead (no animation when the OS asks for reduced motion). A short
  run still holds both for one pulse so they are readable. Ordinary `exec`
  stays cyan `busy`. The daemon names the in-flight work as `activity` on
  `describe()` and on WebSocket `state` events (`run_test` / `exec` / `null`).
  An empty cell feed during a test run says to open Logs rather than
  offering `⌘+Enter`. A `run_test` journal entry is not drawn as a
  transaction marker.
- Watching a session, the first open of the log keeps the editor's gap under
  the session keyboard (same restack as log focus). Previously it sat flush
  against the keys until expand/collapse was clicked.

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

[Unreleased]: https://github.com/romi477/odoo_sheller/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/romi477/odoo_sheller/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/romi477/odoo_sheller/releases/tag/v1.0.0
