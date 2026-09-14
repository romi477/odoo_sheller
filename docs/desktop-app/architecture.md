# The desktop app: architecture

A macOS application built with Tauri, wrapping the odoo-sheller daemon. The
framework choice is settled and not argued here; what follows is how the app is
put together given how odoo-sheller already works.

Read [architecture.md](../architecture.md) and [security.md](../security.md)
first. The invariants they describe are not negotiable from this side, and the
decisions below exist mostly to keep them intact.

## What the app actually contains

The app is a window and a process supervisor around the daemon we already
have. It is not a rewrite, and the daemon is not an implementation detail that
could be swapped — its properties determine most of what follows.

- **The daemon listens on `127.0.0.1:8765`** (`odoo_sheller/__main__.py`) and has
  no authentication. It executes arbitrary Python as `SUPERUSER_ID` against a
  real database. That is accepted (see [security.md](../security.md)); it means
  the port stays on loopback, always.
- **The daemon serves the web UI itself**: `odoo_sheller/web` is mounted at
  `/static` and `/web`, `no-store` (`api.py`). There is no frontend build step
  in this project and the app does not add one.
- **Sessions do not outlive the daemon.** The daemon owns the pipes; when it
  dies, the `odoo-bin shell` process in the container dies with it and
  uncommitted work is discarded. That is a guarantee, not a limitation.
- **The daemon spawns external processes**: `docker exec -i …` for local
  containers, `ssh -T …` for odoo.sh builds. Neither ships with the app.
- **State on disk lives in `~/.odoo-sheller/`**: journals, `admin.key`, SSH
  control sockets.
- **The MCP server is a thin client** of the daemon over HTTP, at
  `ODOO_SHELLER_URL`, default `http://127.0.0.1:8765` (`mcp.py`).

Three decisions follow directly: the port is fixed, the UI comes from the
daemon, and quitting the app ends every session it owns.

## Tauri, briefly

- **UI** renders in the system WebView (WKWebView on macOS), not a bundled
  Chromium, so the shell binary is small.
- **The core** is Rust, and is the only part with full OS access: processes,
  files, menus, dialogs.
- **The bridge** is `commands` (JS calls Rust, awaits a result) and `events`
  (Rust pushes to the frontend unprompted).

```
odoo-sheller-app/
├── src-tauri/
│   ├── src/lib.rs             ← commands, events, lifecycle
│   ├── src/daemon.rs          ← probe, spawn bundled onedir or uv+checkout
│   ├── src/mcp.rs             ← merge agent configs
│   ├── capabilities/          ← permissions (2.x replaced the 1.x allowlist)
│   └── tauri.conf.json
├── src/                       ← splash only; the UI comes from the daemon
packaging/                     ← PyInstaller onedir; copied into Resources/
```

**Use the 2.x API.** Most examples on the web are Tauri 1.x and will not
compile. The daemon is an onedir tree under `Contents/Resources/`, spawned
with `std::process::Command` by absolute path — not a sidecar.
`externalBin` wants one file; onedir is not one file. `tauri-plugin-shell`
is not used.

Permissions live in `src-tauri/capabilities/*.json`. This app grants the
core defaults and dialogs; spawning is `std::process::Command` in Rust, so
there is no `shell:allow-execute` sidecar permission.

## The port is fixed at 8765

Not "find a free port and tell the frontend". The MCP server reads
`ODOO_SHELLER_URL`, whose default is `http://127.0.0.1:8765`, and that address
is already in users' agent configs and in the README. A floating port would
mean rewriting every agent's config on every launch and restarting the agent —
a far heavier contract than the problem it solves.

Startup policy, in `setup()`:

1. Probe `http://127.0.0.1:8765` with a short timeout.
2. **An odoo-sheller daemon answers** → attach to it, spawn nothing, and
   remember that this daemon is not ours. This is the ordinary case: the
   developer already ran `shellerd` in a terminal.
3. **Something else holds the port** → show a clear error naming the port and
   start nothing. Silently moving to another port sends every agent to the
   wrong place.
4. **Port free** → spawn the bundled daemon (or `uv` from a checkout) and
   poll until it answers.

If a dynamic port is ever genuinely needed, it is a separate piece of work:
the chosen port has to be written into the `env` of every agent config entry
and refreshed on every restart.

`GET /health` is the probe: `{"ok": true, "version": ...}`, no session data and
no admin key. A daemon from before that route errors on it, so the fingerprint
falls back to `GET /api/sessions`, which answers `200` with a JSON array.

**There is no version gate on a daemon the app attaches to.** The only thing
the app needs from it is the UI it serves, and every daemon that has ever
existed serves `/web`. Refusing an older one would break the ordinary case —
the developer with `shellerd` already running — in exchange for nothing. The
version from `/health` is recorded because it is free, not because it decides
anything. The day the app depends on a daemon feature, that is the day the gate
gets a reason and a minimum.

The probe stops at the first answer: when `/health` identifies the daemon, the
second request is not made. This matters because the readiness poll runs it
every 150 ms while the daemon boots.

`setup()` runs **before the main loop**, so no window exists while it is
executing: Tauri's own documentation says as much, and advises spawning any
slow setup work. Probing, spawning `uv` and waiting for uvicorn is slow enough
to matter — up to the full readiness timeout on a cold start. The app therefore
starts the daemon on a background thread and lets the splash paint immediately;
the splash asks the supervisor for the current phase on load and follows it by
event afterwards, so it cannot miss a transition that happened before it was
listening. A startup failure is rendered on that same page, with **Try again**
and **Quit** — a modal over a permanently blank window is a dead end.

## The UI comes from the daemon

The window holds a local shell page — splash, the daemon's UI in an
`<iframe>`, and the terminal dock under it. The app does not bundle a copy of
the frontend, and Rust does not proxy anything: the frame talks to the daemon
directly over HTTP and WebSocket on loopback, exactly as a browser tab would.

The frame rather than a navigation, because the terminal has to live in the
same window and only a local page can reach the Tauri commands. The frame is a
remote origin, so it gets none of them — the same boundary that would have
existed between two windows, drawn inside one. The fixed port has one
definition, in `daemon.rs`; the page asks for it with `ui_url` rather than
spelling it a second time.

Bundling the UI instead looks natural and is more expensive than it looks:

- A bundled page lives on `tauri://localhost` while the API is on
  `http://127.0.0.1:8765` — **different origins**, and there is no
  `CORSMiddleware` in `api.py` at all. Either the daemon starts accepting
  cross-origin requests (a deliberate decision for an unauthenticated port), or
  every call goes through Tauri's HTTP plugin, bypassing the WebView's fetch.
- There would be two copies of the UI: the one in the Python package and the
  one in the bundle. `tests/test_web_assets.py` pins the one the daemon serves.

The cost of serving from the daemon, to be accepted knowingly:

- Nothing can render until the daemon answers, so the app needs its own splash
  screen and navigates once the health check passes.
- The page is loaded from a local HTTP server rather than from the signed
  bundle. On loopback that is acceptable, but it is a decision, not a detail.

A consequence worth keeping: a browser tab on the same port keeps working
alongside the app. Both are ordinary clients of the same API, and the registry
socket broadcasts session events to all of them, so they stay in sync. The
second view is a **watcher**: write keys live in the `localStorage` of whichever
client opened the session, so another client can follow but not type until the
session is handed over or the admin key is used.

Bundling the frontend later remains reasonable — with CORS and a single source
of truth for the assets.

One consequence reaches into later stages: **the page served by the daemon has
no Tauri IPC.** It is a remote origin as far as the WebView is concerned, so
`invoke` is not there and no capability grants it. Anything that needs the
native side — the "configure agents" action in the MCP stage, a terminal tab,
an "open the log folder" affordance — belongs in the **application menu**, not
in a button on the web UI. Only the app's own splash page can call into Rust.

## Packaging the Python side

The daemon stays exactly the Python program it is. PyInstaller freezes CPython,
our package and the dependencies into one artifact so the user needs no Python,
no `uv` and no `pip install`. Rust launches it as an ordinary child process.

Two things in our package are read as **data**, not imported, and both fail
silently if they are not bundled as such:

1. **`odoo_sheller/bootstrap.py`** is read as text at runtime
   (`odoo_sheller.paths.bootstrap_path()`). In onedir, data lives under
   `sys._MEIPASS` (`_internal/` beside the executable). If that path is
   wrong, the app starts fine and fails on the first attempt to open a
   session — which a "does it launch" check will not catch.
2. **`odoo_sheller/web`** (332 KB, of which 184 KB is the vendored editor) is
   mounted from the same helper. Without `--add-data`, `/web` returns 404.
3. **Our own dist-info.** `importlib.metadata.version("odoo-sheller")` raises
   `PackageNotFoundError` in a frozen build unless the metadata is bundled
   (`--copy-metadata odoo-sheller`). `/health` guards the call and answers
   `"unknown"` rather than `500`, because the app's liveness probe must not
   depend on packaging — but the guard is the floor, not the fix: pass the
   flag, or every build reports a version it does not have.

Also:

- `uvicorn[standard]` pulls `uvloop` and `httptools`, and uvicorn resolves
  protocol and lifespan implementations by string name — expect
  `--collect-submodules uvicorn` and hidden imports.
- Two binaries, very different weights: the daemon (FastAPI, uvicorn, pydantic)
  and the MCP server (`mcp[cli]` plus httpx2, a thin client).
- **onedir**, not onefile. onefile unpacks into `/var/folders` on every
  launch and fights the hardened runtime. The trees land at
  `Contents/Resources/odoo-sheller/odoo-sheller` and
  `Contents/Resources/odoo-sheller-mcp/odoo-sheller-mcp`. The first of those
  is also the headless daemon: no window, no checkout.
- Expect 40–70 MB of Python payload. The Tauri shell being a few megabytes does
  not change that.

## External tools and PATH

The single most likely "works in dev, broken in the `.app`" failure.

The daemon invokes `docker` by name (`discovery.py`, `transport.py`). A GUI
application on macOS inherits a minimal `PATH` (`/usr/bin:/bin:/usr/sbin:/sbin`),
while Docker Desktop installs its client into `/usr/local/bin/docker` or
`~/.docker/bin/docker`. Under `tauri dev` from a terminal everything works;
from a double-clicked `.app`, the container list is empty and nothing explains
why.

This is the same class of problem as Claude Desktop starting MCP servers with a
minimal `PATH`, which is why the README insists on absolute paths there.

Options, simplest first:

1. Rust resolves `docker` at startup from a list of known locations and passes
   it to the daemon (needs a small change on our side: read the binary from an
   environment variable, defaulting to `"docker"`).
2. Launch the daemon through a login shell so it inherits the user's `PATH`.
   Works, depends on someone else's `.zshrc`, and slows startup.
3. Ask the user for the path in settings — the fallback when neither worked.

`ssh` lives in `/usr/bin/ssh` and is on the minimal `PATH`, so it is fine. The
probe already reports "Docker not running" in a legible way; the app should
surface that rather than showing an empty list.

## Lifecycle

- Killing the daemon kills **every session** and the uncommitted work in them.
  In a browser that follows from closing a window deliberately; an app gets
  quit reflexively with ⌘Q.
- On `RunEvent::ExitRequested` with live sessions, show a dialog naming the
  number of sessions and pending commands (the API reports `pending_commands`),
  and allow `api.prevent_exit()`. Use the wording the UI already uses:
  uncommitted work will be discarded.
- **If the daemon is not ours, never kill it.** A simple check, easy to forget,
  unpleasant when forgotten: the app would silently stop a daemon the developer
  is using from a terminal.
- **Stopping our own daemon is `SIGTERM` first, `SIGKILL` only if it will not
  go.** uvicorn turns `SIGTERM` into a graceful shutdown, and that shutdown is
  where the daemon closes its live sessions and writes `session_close` to their
  journals. `SIGKILL` skips all of it and leaves every transcript ending
  mid-sentence, with no record of how it ended — and with the app, quitting
  becomes the ordinary way a session ends, not an edge case. The wait is
  bounded: a wedged daemon must not be able to hold the app open.
- The daemon that is spawned is recorded **before** the readiness wait, not
  after. A quit during a slow startup has to find that child, or it outlives
  the app that started it.
- Closing the window is not quitting: on macOS, keep the app in the dock and the
  sessions alive.
- **Single instance is mandatory** (`tauri-plugin-single-instance`, registered
  first). Two windows means two attempts to bind 8765.

## The terminal

`portable-pty` on the Rust side: one PTY and one shell process per session, a
reader thread forwarding output to a per-session event
(`pty-output-{session_id}`), sessions in a `HashMap` under a `Mutex` in
`tauri::State`, and four commands — create, write, resize, close. `xterm.js`
with `@xterm/addon-fit` on the frontend; after every `fit()` call
`resize_terminal`, or interactive programs draw incorrectly.

Beyond the mechanics:

- **Launch `$SHELL`, as a login shell** (`-l`), rather than hardcoding
  `/bin/zsh`. It respects the user's choice and gives the terminal the same
  `PATH` they see in Terminal.app.
- **Throttle output.** A large `cat` or a build emits tens of thousands of
  lines; one event per line will drown the WebView. Batch by time or size. The
  startup log well in the existing UI already does this with a paced queue.
- **Reap processes** on session close and on exit, or zombies accumulate.
- **The threat model widens.** odoo-sheller today runs Python inside Odoo; a
  terminal makes the app run anything as the user. That belongs in
  [security.md](../security.md) rather than being discovered later.
- Tabs, not split panes, in the first version. The Rust side does not care.

Since the UI is served by the daemon, the terminal cannot live inside that
page: it is a remote origin with no IPC, and putting xterm.js there would mean
vendoring it into the Python package for a feature that could not work. It
lives in the shell page instead — a dock under the frame, opened from the bar
at the bottom, from **Window → Show Terminal**, or with Ctrl+`, and resizable by
its top edge. Attached to the window rather than floating beside it: the point
is to reach the machine the session is running on without leaving the app.

The frame asks for `/web?app=1`. A remote origin cannot be told anything else
— there is no IPC across it and no stylesheet crosses it — and there is one
thing it has to know: inside this window the `swagger` link is a dead end. It
navigates the frame to `/docs`, which this window offers no way back from, and
`/docs` is the only page the daemon serves that needs the network.

## Signing and entitlements

There is no Apple Developer Program membership and no notarization. The
build is **ad-hoc**: Tauri's `signingIdentity` is `"-"`, which is
`codesign -s -` on each nested binary and then on the bundle. That is
enough for the app to run on the machine that built it, and on Apple
Silicon a signature of some kind is required at all.

- **Hardened runtime stays on** (`codesign --options runtime`). It does not
  need a paid certificate.
- **Do not use `--deep`** — Apple deprecated it and it signs less than it
  appears to. Sign nested binaries first, then the bundle; Tauri already
  does that.
- **Entitlements are empty, and stay empty.** The frozen daemon looks like
  the obvious reason to open `disable-library-validation`, and it is not one:
  PyInstaller ad-hoc signs its own tree, and the daemon is a separate process
  that does not inherit this app's hardened runtime. A bundle signed with an
  empty plist spawns it, serves `/health` and probes containers. Spawning
  `docker`, `ssh` and later a shell needs no entitlement
  outside a sandbox; `com.apple.security.network.client` does nothing without
  `app-sandbox`; and WKWebView's JIT runs in Apple's own
  `com.apple.WebKit.WebContent.xpc`, which carries `allow-jit` under Apple's
  signature, so granting it to our process would buy nothing.
- **The Mac App Store is out.** The app must spawn `docker`, `ssh` and an
  arbitrary shell; the App Store sandbox does not allow it. Distribution is
  an ad-hoc `.dmg` — handed to someone, or downloaded from GitHub.

First launch of a download is **System Settings → Privacy & Security →
Open Anyway**. Control-click → Open stopped clearing Gatekeeper in macOS 15;
Apple's own instructions are the Settings route, and the button there is
offered for about an hour after the app is first refused. The blunt
alternative is to drop the quarantine flag before launching:

```bash
xattr -dr com.apple.quarantine /Applications/odoo-sheller.app
```

`-d com.apple.quarantine`, not `-c`: `-c` clears every extended attribute the
file has, which is more than was asked for.

- **Apple Silicon only** for now: the CI builds `aarch64-apple-darwin` and
  nothing else. A universal binary is a later decision, and it needs
  universal2 wheels for the native dependencies (`pydantic-core`, `uvloop`,
  `httptools`); where a wheel is missing, build per architecture and `lipo`
  them together.

## Data, logs, privacy

- **The data directory stays `~/.odoo-sheller/`.** The MCP server, the command
  line and the documentation all look there (`admin.key`, journals, SSH control
  sockets). Moving it to Application Support would split the world in two.
- **Daemon logs** belong in `~/Library/Logs/odoo-sheller/`, written by Rust from
  the daemon child's stdout and stderr — that is where macOS and Console.app expect
  them.
- **Journals are unmasked** and can contain credentials read out of a database.
  That is documented and accepted for the web tool; an app should add a "Reveal
  journals in Finder" menu item and say so on first run. They are also now part
  of whatever backs up the home directory.

## What the desktop does not change

Tauri makes nothing worse on its own, but it changes who runs this: a signed
app is launched by people who will never open [security.md](../security.md).

- The port stays on loopback. The app must never expose a host field or a
  "share on the network" switch. The daemon has `--host`; the app must not use
  it.
- There is no authentication on the port, and the daemon is now up for as long
  as the window is open rather than for as long as someone deliberately ran it.
  That is an argument for stopping the daemon on quit rather than leaving it in
  the background.
- Remote targets stay a human's to open — enforced by the MCP tools taking
  neither a host nor a build. The app adds nothing here and must not.
- The terminal is the one real expansion of the attack surface.

## Out of scope for the first version

Auto-update, Windows and Linux, split panes, bundling the frontend, the App
Store, a dynamic port.

## Process diagram

```
odoo-sheller.app  (ad-hoc signed, hardened runtime, not notarized)
│
├── Rust core
│   ├── startup: probe 8765 → attach to a foreign daemon OR spawn our own
│   ├── spawn: Contents/Resources/odoo-sheller/odoo-sheller
│   ├── PTY: HashMap<session_id, PtyPair> — one $SHELL per terminal tab
│   ├── menu: MCP Configuration — show the entry to paste, write nothing
│   ├── menu: Show Terminal / New Terminal Tab / tab moves — events to the page
│   └── exit: confirm with live sessions; kill our daemon, never a foreign one
│
├── WKWebView
│   ├── window on http://127.0.0.1:8765/web?app=1  (the daemon serves the UI)
│   └── shell page: the frame above, xterm.js tabs of $SHELL -l below
│
└── External processes
    ├── the daemon
    │     ├── docker exec -i …   (path resolved by Rust, see PATH above)
    │     └── ssh -T …           (/usr/bin/ssh)
    ├── $SHELL × N               (one per terminal tab)
    └── the MCP binary           (started and stopped by the agent, not by us)
```
