# odoo-sheller

Persistent Odoo REPL proxy (15 through 20): a local Python daemon keeps `odoo-bin shell`
alive — in a local Docker container, or on an odoo.sh build over SSH — and
exposes it over HTTP/WebSocket.

A web UI (stage 1) and later an AI agent (stage 2) run ORM code without paying
registry startup on every call. Both clients use the same API.

There is also a **macOS app** — a window and a supervisor around that same
daemon, with a terminal docked under the UI. It adds no second API, and a
browser tab on 8765 keeps working beside it:
[docs/desktop-app/architecture.md](docs/desktop-app/architecture.md).

## Table of Contents

- [Background](#background)
- [The desktop app](#the-desktop-app)
- [Security](#security)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [Tests](#tests)
- [API](#api)
- [Journals](#journals)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## Background

Running `docker exec … odoo-bin shell < script.py` per attempt costs ~1.4s of
registry load plus boilerplate (temp file, flags, output markers, log
filtering). Across a debugging session that dominates.

odoo-sheller starts Odoo once, keeps the namespace, and makes commit/rollback
explicit. Rollback is the default; nothing is written until you confirm a
commit. Odoo 15 through 20.

```
browser ──HTTP/WS──> daemon (macOS) ──pipe──> docker exec ──> odoo-bin shell
                     127.0.0.1:8765          stdin/fd 3 + stdout
                                             └──> ssh ──> odoo-bin shell
                                                          (an odoo.sh build)
```

The saving is larger on a remote build, not smaller: `ssh host 'odoo-bin
shell' < script.py` pays that 1.4s *and* a round trip on every call, while a
held session pays the load once and one round trip per command.

Full documentation: [docs/](docs/README.md) — architecture, the UI guide,
agent access, the security model, and an FAQ in
[English](docs/faq.md) and [Russian](docs/faq-ru.md). What shipped when:
[CHANGELOG.md](CHANGELOG.md).

## Security

This tool executes arbitrary Python as `SUPERUSER_ID` against a real database.

- The daemon binds **`127.0.0.1:8765` only** and has **no authentication**.
  Anyone who can reach the port can run code. Never publish or reverse-proxy it.
- **Commit is always explicit.** Close, kill, and process death discard
  uncommitted work.
- **Journals are unmasked.** They can contain credentials and API keys read
  from the database. They live in `~/.odoo-sheller/journals/`, stay out of git, and
  must be reviewed before sharing. A remote session's journal holds that
  instance's data, on your machine.
- **A remote target is a human's to open.** No MCP tool takes a host or a
  build, so an agent can only ever be handed one. There, commit is off until
  granted — for a human owner too — and on a `production` instance it is
  refused outright.

The reasoning behind each of these, and what they don't cover, is in
[docs/security.md](docs/security.md).

## Requirements

| | |
|---|---|
| OS | macOS (daemon is not containerized) |
| Python | 3.12 or newer |
| Package manager | [uv](https://docs.astral.sh/uv/) (preferred) or pip + venv |
| Docker | Docker CLI; a running Odoo **15 through 20** container |
| odoo.sh (optional) | SSH access to a build; nothing else — no key files to configure here, an alias from your own `~/.ssh/config` works |

Nothing is installed on the far side, container or build. The bootstrap needs
`sh` and Odoo's own Python.

## Install

Clone the repository, then sync the environment from the lockfile.

### With uv (preferred)

```bash
uv sync
```

`uv.lock` pins the full dependency tree (transitive packages, wheels, hashes)
so installs are reproducible. Do not edit it by hand; regenerate with `uv lock`
after changing `pyproject.toml`.

### With pip

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

The `dev` extra (pytest, pytest-asyncio, httpx2, ruff) matches uv's default
`dev` group. It is `httpx2`, not `httpx`: `starlette.testclient` imports
`httpx2` and warns that `httpx` is deprecated there.

## Usage

Start the daemon from the project root:

```bash
uv run python -m odoo_sheller
```

Without uv: `.venv/bin/python -m odoo_sheller`. An install also puts two
commands on the venv's `bin`: `odoo-sheller` for the daemon and
`odoo-sheller-mcp` for the agent server. They are named after the package
rather than `shellerd`, because a shell function by that name is the
documented way to run the daemon detached — and a function shadows a command
on `PATH`, so one of the two would silently never run.

Flags: `--host` (default `127.0.0.1`), `--port` (default `8765`), and
`--reload` for development. **Reload kills every live session** — the daemon
owns the pipes, so the container-side processes die with it. Files under
`odoo_sheller/web/` are excluded from the watch: reload the browser page instead.

Then open <http://127.0.0.1:8765/web>. Swagger for the HTTP API is at
<http://127.0.0.1:8765/docs>. Typical loop:

1. **Connect** — running containers are listed and probed automatically.
   Unsupported Odoo majors are refused. Pick a database (config `db_name` is
   the default) and start a session. Last used target is remembered in
   `localStorage`. The last toolbar tab (Connect / Sessions / Journals) is
   remembered the same way, so a refresh stays on that screen.
2. **Sessions** — type Python in the editor, run with `⌘+Enter` / `Ctrl+Enter`.
   `env` and `self` are Odoo's shell namespace; variables persist between
   commands. One command at a time (`409` if busy).
3. **Transaction** — **Rollback** discards, **Commit** keeps. Both are explicit;
   nothing is written otherwise.
4. **Journals** — past sessions, exportable as JSONL or Markdown.

### Keeping it in the background

The daemon in a terminal tab is fine until you want that tab back. This shell
function detaches it and gives you the few things worth having: a pid you can
trust, a log, and a stop that does not lose data. Copy it into `~/.zshrc` (it
works in bash too) and point `ODOO_SHELLER_ROOT` at your checkout:

```zsh
# Your odoo-sheller checkout. Exported, not just set: the desktop app reads
# the same variable, and so would anything else launched from this shell.
export ODOO_SHELLER_ROOT=${ODOO_SHELLER_ROOT:-$HOME/odoo-sheller}

# The daemon in the foreground, exactly as the Usage section runs it.
alias sheller='uv --project="$ODOO_SHELLER_ROOT" run python -m odoo_sheller'

# The same daemon, detached. Runs the venv's console script rather than
# `uv run`, so the pid we save is the daemon itself and not a wrapper whose
# child would survive the kill and keep holding the port.
shellerd() {
  local dir=${ODOO_SHELLER_ROOT:?set ODOO_SHELLER_ROOT to your odoo-sheller checkout}
  local pidfile=~/.odoo-sheller/daemon.pid log=~/.odoo-sheller/daemon.log
  local health=http://127.0.0.1:8765/health
  local pid; pid=$(cat "$pidfile" 2>/dev/null)
  # Two halves of the truth. The pid file says what this function started; the
  # port says whether a daemon is there at all. They come apart — the desktop
  # app runs a daemon of its own and writes no pid file — so "we have no pid"
  # and "nothing is running" are different answers. Confusing them is how you
  # start a second daemon that dies on the bind and leaves a pid file pointing
  # at a corpse.
  local ours=""; [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && ours=$pid
  local up="";   curl -fsS -m 1 "$health" >/dev/null 2>&1 && up=1
  case "$1" in
    start)
      if [ -n "$ours" ]; then echo "already running (pid $ours)"; return 1; fi
      if [ -n "$up" ]; then
        echo "a daemon already answers on 8765 and this function did not start it."
        echo "It belongs to something else — use it, or stop that first."
        return 1
      fi
      rm -f "$pidfile"
      if [ ! -x "$dir/.venv/bin/odoo-sheller" ]; then
        echo "no odoo-sheller in $dir/.venv — point ODOO_SHELLER_ROOT at your"
        echo "checkout and run: uv sync"; return 1
      fi
      mkdir -p ~/.odoo-sheller; ( umask 077; touch "$log" )
      nohup "$dir/.venv/bin/odoo-sheller" >>"$log" 2>&1 &
      echo $! >"$pidfile"
      echo "started (pid $!) — http://127.0.0.1:8765/web"
      ;;
    stop)
      if [ -z "$ours" ]; then
        rm -f "$pidfile"
        if [ -n "$up" ]; then
          echo "a daemon answers on 8765, but this function did not start it."
          echo "It goes down with whatever owns it, not from here."
        else
          echo "not running"
        fi
        return 1
      fi
      kill "$ours" 2>/dev/null
      # SIGTERM is the graceful one: the daemon closes its live sessions and
      # journals them on the way down, which takes a few seconds. Escalate
      # only if it really will not go — a kill -9 loses those records.
      for _ in $(seq 1 20); do kill -0 "$ours" 2>/dev/null || break; sleep 0.5; done
      if kill -0 "$ours" 2>/dev/null; then echo "stuck, forcing"; kill -9 "$ours"; fi
      rm -f "$pidfile"; echo "stopped"
      ;;
    restart) shellerd stop; shellerd start ;;
    status)
      if [ -n "$ours" ]; then echo "running (pid $ours)"
      elif [ -n "$up" ]; then echo "running on 8765, but not ours"
      else echo "not running"; fi
      ;;
    log) tail -f "$log" ;;
    key) cat ~/.odoo-sheller/admin.key ;;
    help|-h|--help|"")
      cat <<'SHELLERD_HELP'
shellerd — odoo-sheller daemon

  start      start it in the background, detached from this terminal
  stop       ask it to stop, wait for it, force it only if it will not go
  restart    stop then start — live sessions do not survive this
  status     ours, somebody else's on 8765, or nothing
  log        follow ~/.odoo-sheller/daemon.log
  key        print the admin key the web UI asks for
  help       this list
SHELLERD_HELP
      ;;
    *) echo "shellerd: no such command: $1"; shellerd help; return 1 ;;
  esac
}
```

It expects the venv to exist — `uv sync` once in the checkout. Two details are
deliberate rather than incidental:

- **`restart` takes every live session with it.** The daemon owns the pipes, so
  the container-side processes die with it. That is the same rule as `--reload`,
  and it is why the daemon is not restarted casually.
- **`stop` is `SIGTERM` first.** The daemon closes its sessions on the way down
  and writes `session_close` to each journal. `kill -9` skips that, and the
  transcript then simply stops with no record of how it ended. The escalation
  after ten seconds is the last resort, not the normal path.

## The desktop app

`odoo-sheller-app/` builds a macOS `.app`: the same daemon, a window around
it, and a terminal dock at the bottom for the machine the session runs on.
The daemon and the MCP server travel frozen inside the bundle, so the *built
app* needs no Python, no `uv` and no checkout — building it, of course, needs
all three.

Building it also needs the Xcode command line tools, Rust and the Tauri CLI;
[odoo-sheller-app/README.md](odoo-sheller-app/README.md) lists them and says
how to run it from source without building anything.

```bash
packaging/app.sh build       # the .app
packaging/app.sh dmg         # a .dmg to hand someone
packaging/app.sh install     # build, then replace /Applications/odoo-sheller.app
```

One command does everything: `cargo tauri build` runs `packaging/freeze.sh` as
its `beforeBuildCommand`, so PyInstaller cannot be forgotten and the bundle
cannot ship yesterday's daemon.

It is ad-hoc signed and not notarized — a downloaded copy is cleared in System
Settings → Privacy & Security → Open Anyway — and Apple Silicon only. The
design is [docs/desktop-app/architecture.md](docs/desktop-app/architecture.md),
the staged work and its decisions
[implementation.md](docs/desktop-app/implementation.md), and what the terminal
widens [docs/security.md](docs/security.md#the-desktop-terminal).

Day to day you do not need it: `python -m odoo_sheller` and a browser tab are
the whole tool.

## Agent access (MCP)

Full tool list and defaults: [docs/agent-guide.md](docs/agent-guide.md). Short
version:

`odoo_sheller/mcp.py` exposes the same API to an agent over stdio. The agent's
host starts it: Claude Desktop from
`~/Library/Application Support/Claude/claude_desktop_config.json`, Claude Code
from `~/.claude.json`, Cursor from `~/.cursor/mcp.json`, Zed from
`~/.config/zed/settings.json` — the same entry, under `context_servers`
instead of `mcpServers`.

Three ways to name the command, by how much the machine has to have on it.

**From the installed app** — no Python, no `uv`, no checkout:

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

That is a link the app points at its own bundled server, refreshed every time
the app starts, so the entry survives an update or a move. **MCP
Configuration…** prints it with your real home directory filled in — these
files are read by programs that do not expand `~`. The binary it points at
lives at
`/Applications/odoo-sheller.app/Contents/Resources/odoo-sheller-mcp/odoo-sheller-mcp`, if you would rather name it directly.

**From a checkout**, the console script `uv sync` installs:

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

**From a checkout, through uv**, which syncs the environment from `uv.lock`
before starting:

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

Absolute paths on purpose: these hosts launch their servers with a minimal
`PATH`, so a bare `uv` can fail to resolve.

The desktop app's **MCP Configuration…** menu prints the first of these with
the real path filled in and copies it to the clipboard. It never writes to an
agent's config: those files are yours, they hold other servers, and one of
them is live state a running Claude Code rewrites.

The daemon is **not** started by the MCP server — a host restarts its servers
freely, and a daemon that died with them would take every live session down
too.

An agent opens its own sessions (`owner: agent`, no commit right), or you hand
yours over from the session keyboard: **Grant access** rotates the key, shows
`{"session_id","write_key"}` once, copies it on OK, and drops your browser to
watching. Press it again to take the session back. **Grant commit** is a latch
on that keyboard — amber while the agent may write, cyan when it may not —
after you have read what it did.
Uncommitted work travels with the session — roll back first if that matters.

## Controls

Every control below, with what it guarantees: [docs/ui-guide.md](docs/ui-guide.md).

### Connect

| Control | What it does |
|---|---|
| **↻** (beside the title) | Re-runs `docker ps` and probes every container again. Turns while it works, until the last probe answers |
| **re-probe** | Probes this container only — after a restart or a config change |
| **Open session** | Expands the card's database picker. Disabled when the probe found no Odoo 19. A click on the card outside the picker collapses it again |
| **Start** | Opens the session on the chosen database. While Odoo loads its registry the button spins, same as **New**, and the card shows the container's stderr as it arrives. Scroll back in the well and it holds position; a **Refresh** mid-start keeps the well |
| **Close session** | Shown on a container that already has a session; closes it without leaving the screen. With several sessions on that container it reads **Close N sessions** and closes all of them — the card stands for the target, not for one session |

A container may hold more than one session — a second one on another database is
legitimate.

### Session keyboard

A compact two-row grid flush with the top and bottom of the session header,
inset from the right by the same 16px as the editor. Keys stay in place:
when a control does not apply it is disabled, not hidden. The hover hint has
the full name and what the key does.

| Key | What it does |
|---|---|
| **Grant commit** | Latch, only enabled while an agent owns the session. Amber when granted, cyan when not. Turning it on asks for confirmation naming the database; turning it off does not |
| **Grant access** | Latch. Amber while an agent owns the session. Press to hand the session over (you keep watching); press again to take it back. OK copies `{"session_id","write_key"}` |
| **Close** | Ends the session in order: the bootstrap leaves its loop, Odoo rolls back and closes the cursor, the process exits. If the process has not exited after ten seconds — a long command can hold it — the daemon escalates to a kill by itself |
| **Kill** | `SIGKILL` now, no waiting and no Odoo teardown. Postgres rolls the transaction back on its own. The journal records `killed`, so an ordinary close stays distinguishable from one that had to be forced |
| **Interrupt** | Enabled while busy. Sends `SIGINT` to the in-container pid, so the command ends as `KeyboardInterrupt`. The session and its namespace survive |
| **Rollback** | Discards the open transaction and invalidates the ORM cache. Leaves a marker in the feed |
| **Commit** | The only control that writes. Asks for confirmation naming the database, then flushes, commits, and invalidates the cache |
| **New** | Opens another session on the same container and database. Stay on this tab; the twin appears as another tab |

Close and Force kill both discard uncommitted work — the difference is how the
process dies, not what survives. Prefer Close; reach for Force kill when a
command ignores `SIGINT` and you do not want to wait out the escalation.

A dead session has no Reconnect — press **New** for another session on the same
target, then Close or Kill this tab.

The state badge is the thing to read before acting: `ready` / `busy` /
`testing` / `starting` / `dead`. A test run shows `testing` (rose) instead of
cyan `busy`, and that session's tab grows a blinking rose lamp. Close and
Force kill still warn if uncommitted commands are pending.

### Session tabs

| Control | What it does |
|---|---|
| Tab **×** | Ordinary Close, same as Close on the keyboard |
| **⌥**-click on **×** | Force kill |

While a test is running, the tab keeps its cyan/amber color and shows a
blinking rose lamp next to the name (no blink if the OS asks for reduced
motion). A short run still holds the lamp for one pulse.

While the request is in flight the tab reads `closing…` or `killing…` and its `×`
stops responding; a close already under way can still be escalated with **Force
kill**.

### Editor and cells

| Control | What it does |
|---|---|
| **⌘+Enter** / **Ctrl+Enter** | Runs the buffer. Does nothing unless this browser owns the session and it is `ready` |
| **↑ / ↓** | Walks command history when the caret is at the top or bottom of the buffer |
| **taller / shorter** | Doubles the editor height and back. Not remembered across reloads |
| **CELLS** fold dot | Collapses or expands every card at once |
| Per-card fold dot, or the card header | Collapses one card to its header, and opens it again. Clicking anywhere on the header does it — the dot is the affordance, not the only target; **copy code**, **copy output** and **re-run** are exempt. Agent-authored cards start collapsed; yours start open. Remembered until reload |
| **Copy code**, **Copy output**, **Re-run** | Per card. Re-run sends the same code as a new command |

One command at a time. A second one is refused, never queued. A command that
exceeds its five-minute ceiling is interrupted, but keeps the session busy until
it really ends — it still owns the container.

### Logs and journals

| Control | What it does |
|---|---|
| **Logs (odoo stderr)** | Expands Odoo's log tail under the cells. Empty feed: logs go up to the editor. Many cells: the feed keeps a 40% strip |
| **expand / collapse** (log header) | Right edge of the open log header, next to the level filter. Cyan plus hides the editor and the cell feed so the log fills the whole pane; amber minus brings both back. Hidden while logs are collapsed. Closing logs also leaves focus |
| **×** (log header) | Closes the log panel. Same × as on a journal transcript. Hidden while logs are collapsed |
| Level filter | Replaces the new-line count on the right of the log header while expanded |
| Scrolling | The tail follows only while you are at the bottom; scroll up to read and it holds position |
| **Journals / N rows** | The heading counts the list under it. Gone when there is nothing to count |
| Journal group heading | Click `container / database` to expand or collapse that group’s rows. Groups start collapsed |
| Journal row | Click the line to preview the transcript. Columns are named by a sticky header: opened, owner, session, duration, commands, outcome, export. The header is dropped on a narrow window, where the row wraps onto two lines |
| **expand / collapse** (journal) | In the transcript header: one mark. Cyan plus hides the list and gives the transcript the whole pane; the same mark turns amber minus and brings the list back. × dismisses the transcript |
| **.jsonl** / **.md** | Export one session; both warn first, because journals are unmasked |
| **copy** | Copies the whole transcript to the clipboard — the same unmasked text the exports carry |
| Row trash | Deletes that journal file immediately. Hidden while the session is still live. Unlinking a file takes the admin key, so the first delete of a session asks for it once |
| Group trash | On the group heading when any row is finished. Confirms, then deletes every non-live journal in the group. A refusal does not stop the batch: the rest are deleted and the failures are named once at the end |

The daemon stays in the foreground. Stop it with `Ctrl+C`. Sessions cannot
outlive the daemon: when it dies, the pipe dies, and the in-container process
dies.

## Tests

`uv sync` already installs the development group.

```bash
# Unit tests (no Docker required)
uv run pytest -m "not e2e and not frozen"

# Live Odoo 19 container
uv run pytest tests/test_e2e.py -v -m e2e

# Frozen daemon (after packaging/freeze.sh)
uv run pytest tests/test_frozen.py -v -m frozen
```

Without uv, use `.venv/bin/pytest` the same way.

A bare `pytest` also collects the e2e and frozen tests. They need a running
target (defaults below). Creating and deleting records there is expected;
tests use the prefix `pt-e2e-` and clean up after themselves. Frozen tests
also need `packaging/dist/odoo-sheller/odoo-sheller` (or `ODOO_SHELLER_FROZEN`).

| Variable | Default |
|---|---|
| `PT_E2E_CONTAINER` | `integra19` |
| `PT_E2E_DB` | `integra_db_19_presta` |
| `PT_E2E_ODOO_BIN` | `/opt/odoo/odoo-bin` |
| `PT_E2E_NON_ODOO` | `odoo-postgres` |

```bash
PT_E2E_CONTAINER=integra19 \
PT_E2E_DB=integra_db_19_presta \
PT_E2E_ODOO_BIN=/opt/odoo/odoo-bin \
PT_E2E_NON_ODOO=odoo-postgres \
uv run pytest tests/test_e2e.py -v -m e2e
```

Lint:

```bash
uv run ruff check odoo_sheller tests
```

## API

Interactive docs: [Swagger UI](http://127.0.0.1:8765/docs) (`/docs`), schema at
`/openapi.json`. The daemon prints both URLs at startup.

`/docs` is the one page here that is **not** self-contained: the swagger-ui
bundle is loaded from `cdn.jsdelivr.net`, so it needs network even though the
daemon does not. Offline, the page renders blank — read `/openapi.json`
instead. Everything under `/web` is local, CodeMirror included.

There is one HTTP/WebSocket API. The UI is a client of it; stage 2 must not
grow a second surface. `exec` blocks until the command finishes (default
ceiling five minutes, then `SIGINT` and `504`). `409` means busy or still
starting; `410` means the session is dead.

Writes carry the session's write key in `X-OS-Session-Key`; the key is returned
once, when the session is opened or handed over, and never listed again. Acting
on a session you own — including handing it over — needs nothing else.

Handing a session over leaves the old key valid for closing that session and for
taking it back — whoever started it can always stop it or reclaim it. It cannot
type: that check is against the current key alone.

`X-OS-Admin-Key` is for acting on a session you never owned — closing or
reclaiming somebody else's. Your own sessions, including ones you handed to an
agent, need nothing beyond the key you already hold: close, take back and grant
commit all work with it. The daemon prints it at startup and
keeps it in `~/.odoo-sheller/admin.key`; no endpoint serves it, because the UI sits
behind the same unauthenticated API and would hand it to anything that can fetch
a page. Read it with `cat ~/.odoo-sheller/admin.key` and paste it once when the UI
asks. A dead session is exempt: reaping a corpse needs no key at all.
`403` means the key is missing or wrong; `423` means commit is not granted for
that session. See [docs/agent-guide.md](docs/agent-guide.md).

A `504` does not free the session. `SIGINT` is a request, not a guarantee, so
the abandoned command may still hold the container: the session stays busy and
answers `409` until that command's result finally arrives. Use `interrupt`, or
`DELETE` to close or kill.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | liveness and version; no session data, no admin key |
| `GET` | `/api/containers` | running containers |
| `POST` | `/api/probe` | probe one container |
| `POST` | `/api/probe/odoosh` | probe an odoo.sh build (`{"build", "host"}`); answers `stage`, `db_name`, version |
| `GET` | `/api/containers/{container}/tests` | test classes/methods in one addon (`?module=`) |
| `POST` | `/api/sessions` | open a session, wait for `hello`. `kind: "odoosh"` with `build`/`host` opens on an odoo.sh build instead of a container; the instance dictates the database, and its stage is read from the build itself, not from the request. Optional `client_token` is echoed back in the session description, so a client recognises its own `session_starting` among several |
| `GET` | `/api/sessions` | live sessions |
| `GET` | `/api/sessions/{id}` | one session |
| `POST` | `/api/sessions/{id}/exec` | run code |
| `POST` | `/api/sessions/{id}/run_test` | run one test method or a whole class (`{"test": "module.TestClass[.test_method]"}`, optional `timeout`); stdout and Odoo's log lines come back separated |
| `POST` | `/api/sessions/{id}/commit` | keep the transaction |
| `POST` | `/api/sessions/{id}/rollback` | discard the transaction |
| `POST` | `/api/sessions/{id}/interrupt` | `SIGINT` |
| `DELETE` | `/api/sessions/{id}` | close; `?force=true` kills |
| `POST` | `/api/sessions/{id}/owner` | hand the session over; rotates the write key (admin) |
| `POST` | `/api/sessions/{id}/policy` | grant or revoke `allow_commit` (admin). `409` on revoking from a human owner: the right only gates an agent |
| `GET` | `/api/sessions/{id}/logs` | stderr tail |
| `GET` | `/api/sessions/{id}/history` | feed from the journal; a closed session answers `200` with `session.state: "gone"` and a `session.gone` object; `?logs=true` adds journalled stderr |
| `GET` | `/api/journals` | past sessions |
| `GET` | `/api/journals/{id}` | export (`?fmt=jsonl` or `markdown`) |
| `DELETE` | `/api/journals/{id}` | unlink the file (admin); `409` if the session is still live. The id is matched exactly, never globbed |
| `WS` | `/ws/sessions` | `session_starting` (id assigned, still waiting for `hello`), `session_failed` (with `reason` — a start that never reached `hello`), `session_opened`, `session_closed`, plus owner/policy/state (`activity` on state events) |
| `WS` | `/ws/sessions/{id}` | state changes (`activity` names `exec` / `run_test` / `null`), process death, stderr |

Example (after the daemon is up and a session exists):

```bash
curl -sS http://127.0.0.1:8765/api/containers
curl -sS -X POST http://127.0.0.1:8765/api/probe \
  -H 'Content-Type: application/json' \
  -d '{"container":"integra19"}'
curl -sS -X POST http://127.0.0.1:8765/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"container":"integra19","database":"integra_db_19_presta","odoo_bin":"/opt/odoo/odoo-bin"}'
```

## Journals

Append-only JSONL per session:

`~/.odoo-sheller/journals/<date>-<container>-<db>-<id>.jsonl`

The file outlives both the session and the daemon. Export from the Journals
tab or `GET /api/journals/{id}`. See [Security](#security) before sharing.

Every way out carries the same metadata — session id, container, database, Odoo
and Python versions, in-container pid, opened/closed timestamps, command count,
whether anything was committed, and an `unmasked` flag:

| Route | Where the metadata sits |
|---|---|
| `GET /api/journals` | one object per session in the list |
| `GET /api/sessions/{id}/history` | the `session` key, journal facts overlaid with live state |
| `GET /api/journals/{id}?fmt=jsonl` | a first line `{"kind": "export_meta", …}`; the journal's own records follow untouched |
| `GET /api/journals/{id}?fmt=markdown` | a table at the top of the transcript |

Both exports also set `Content-Disposition`, so a saved file keeps the journal's
name rather than the session id alone.

## Development

How it works internally — protocol, session state machine, journal format:
[docs/architecture.md](docs/architecture.md). Layout:

| Path | Role |
|---|---|
| `odoo_sheller/protocol.py` | frame encode/decode |
| `odoo_sheller/bootstrap.py` | loop inside the container (stdlib only) |
| `odoo_sheller/transport.py` | `docker exec`, pipes, signals |
| `odoo_sheller/session.py` | one live session |
| `odoo_sheller/registry.py` | sessions by id |
| `odoo_sheller/journal.py` | JSONL journal |
| `odoo_sheller/discovery.py` | `docker ps` and in-container probe |
| `odoo_sheller/api.py` | HTTP + WebSocket |
| `odoo_sheller/web/` | UI, no build step |
| `tests/` | unit tests; `tests/test_e2e.py` is live |

Code, comments, and technical docs are in English. After changing
dependencies, run `uv lock` and commit both `pyproject.toml` and `uv.lock`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Session hangs with no frames | Missing `flush()` after a bootstrap write, or a `print` that hit fd 1 before the dup |
| Interrupt does nothing | Signals must go to the in-container pid (`docker exec <container> kill -INT <pid>`), not the local `docker exec` client |
| Rollback raises a constraint error | `invalidate_all` was called with the default `flush=True` |
| Probe says not Odoo | Expected for non-Odoo containers (e.g. Postgres). The card stays; Open is disabled |
| UI shows daemon unreachable | The process on port 8765 is down; start it again with `uv run python -m odoo_sheller` |
