# The desktop app: implementation

How to build what [architecture.md](architecture.md) describes: the work in
order, what "done" means for each piece, what has to change in this repository,
and the questions that need an answer before the work starts.

This is written for someone who has not been in the conversations that produced
the design. Read [architecture.md](architecture.md) first; read the four
invariants below before writing anything.

## Invariants

Breaking any of these is a design error, not a trade-off. Each is documented
where it is enforced.

1. **One API.** The browser, the agent and the app all speak the same HTTP/WS
   API. The app adds no second one and proxies nothing
   ([../architecture.md](../architecture.md)).
2. **Loopback only.** The daemon binds `127.0.0.1` and has no authentication.
   The app never sets `--host`, never offers a network switch
   ([../security.md](../security.md)).
3. **Sessions die with the daemon.** Nothing in the app may pretend otherwise —
   no reconnect that claims to restore a session, no silent restart of the
   daemon under a live window.
4. **A remote target is a human's to open.** The MCP tools take neither a host
   nor a build. The app introduces no path around that
   ([../agent-guide.md](../agent-guide.md)).
5. **The terminal is the desktop app's alone.** It lives in the shell page,
   never in the daemon's web UI, and the capability never declares a `remote`
   origin. Those are the
   same rule seen from two sides: the page on 8765 is served to browsers and
   to a window Tauri gives no IPC, so a terminal there is either impossible or
   a shell handed to whatever can reach the port
   ([../security.md](../security.md#the-desktop-terminal)).

## Decisions still open

None. The last question was whether the terminal ships in this release;
stage 6 is that answer.

## Decided

| # | Question | Answer |
|---|---|---|
| 2 | `GET /health`, or poll `GET /api/sessions`? | **`GET /health`.** Liveness and version, no session data, no admin key. `/api/sessions` stays as the fallback fingerprint for a daemon older than the route. Shipped. |
| 4 | May the app stop a daemon it did not start? | **No**, and not behind a confirmation either. The developer running `shellerd` in a terminal is the ordinary case, not an obstacle. |
| 6 | Does the app live in this repository? | **Yes**, in `odoo-sheller-app/`, `Cargo.lock` committed. It is versioned with the daemon it drives, and the repository-side changes it needs (`ODOO_SHELLER_DOCKER`, `/health`) are reviewed in the same diff. |
| 7 | Does the app refuse a daemon older than itself? | **No version gate.** All it needs from an attached daemon is `/web`, which every version serves. The version from `/health` is recorded, not enforced — see [architecture.md](architecture.md#the-port-is-fixed-at-8765). |
| 9 | Paid Developer ID, or ad-hoc? | **Ad-hoc** (`signingIdentity: "-"`). There is no Apple Developer Program membership. Gatekeeper refuses a downloaded copy until the person clears it in System Settings → Privacy & Security → Open Anyway. |
| 10 | Apple Silicon only, or universal? | **arm64 only** for now. CI builds `aarch64-apple-darwin`; an Intel Mac gets nothing. Revisit when someone actually has one — a universal build also needs universal2 wheels for `pydantic-core`, `uvloop` and `httptools`. |
| 1 | onefile or onedir for the daemon? | **onedir.** onefile unpacks on every launch and fights the hardened runtime. onedir is not a Tauri sidecar (`externalBin` wants one file), so it ships under `Contents/Resources/` and is spawned by absolute path. |
| 5 | Where does the MCP binary live? | **Inside the bundle**, next to the daemon, with a link at `~/.odoo-sheller/bin/odoo-sheller-mcp` pointed at it. The link is the answer to what the question was really about: a copy in Application Support goes stale on an update and the bundle path is 88 characters of hand-typed config that breaks if the app moves. The name never changes; the target is refreshed on every launch and whenever **Settings…** is opened. |
| 11 | Does the app write agent config files? | **No, and not behind a confirmation.** It shows the entry and copies it to the clipboard; the person pastes it where they want it. Those files are the user's, they hold servers we know nothing about, `~/.claude.json` is live state a running Claude Code rewrites, and creating the rest invents configs for apps that are not installed. Nothing in the app opens them for writing. |
| 8 | How does the packaged daemon run without the window? | **Document the stable path** and point a shell function at it. No menu-bar extra surface. |
| 3 | Terminal in the first release, or after it? | **In this release.** It is the one expansion of the attack surface, documented in [../security.md](../security.md#the-desktop-terminal). Tabs, `$SHELL -l`, a dock under the framed UI in the same window, so xterm.js never enters the Python package. |

## Work in this repository

Not everything is Tauri. These land in odoo-sheller itself, under the same bar
as the rest of it: a test per change, `uv run python -m pytest`, `ruff` clean.

- **`ODOO_SHELLER_DOCKER`** — *done.* Reads the docker binary from the
  environment, defaulting to `"docker"`, through `transport.docker_bin()`.
  Without it the app cannot fix the `PATH` problem from the outside.
- **`GET /health`** — *done.* No session data, no admin key, just liveness and
  version. The version call is guarded: `importlib.metadata.version` raises in
  a frozen build with no dist-info, and the app's liveness probe must not
  depend on packaging.
- **A graceful shutdown** — *done.* The daemon closes its live sessions on
  `SIGTERM`, so their journals end with `session_close` instead of simply
  stopping. Before this the daemon had no shutdown hook at all, which was
  survivable while it was stopped by hand and is not once quitting the app is
  how a working day ends.
- **A frozen-binary smoke test** — *done.* `uv run pytest tests/test_frozen.py -v -m frozen`
  after `packaging/freeze.sh`. Builds are not in the unit suite; the marker
  sits beside `e2e`. The daemon is started on an ephemeral port so a
  developer's 8765 is left alone.
- **Packaging metadata** — *done.* `packaging/bundle.py` holds `--add-data`
  for `odoo_sheller/web` and `bootstrap.py`, plus `--copy-metadata odoo-sheller`
  and `--collect-submodules uvicorn`. Invoked by `packaging/freeze.sh`, which
  `tauri build` runs as `beforeBuildCommand`.
- **`packaging/app.sh`** — *done.* `build`, `dmg`, `install`. The one step
  worth a reviewed script is `install`: it deletes a directory tree. It quits
  a running copy first, and refuses any destination whose
  `CFBundleIdentifier` is not ours, so a typo in `APP_DEST` cannot take
  something else with it.
- **Console scripts** — *done.* `odoo-sheller` runs the daemon and
  `odoo-sheller-mcp` the agent server, so neither is spelled as a `python -m`
  invocation in a config file or a shell function. They are named after the
  package rather than `shellerd`: a shell function of that name is the
  documented way to run the daemon detached, and a function shadows a command
  on `PATH`.

## Stages

Each stage ends in something runnable. Do not carry two stages at once.

### Stage 1 — a window over the daemon

The app lives in `odoo-sheller-app/`: `cargo tauri dev` from that directory.
Tauri 2, single-instance plugin, dialog plugin. Startup policy from
[architecture.md](architecture.md#the-port-is-fixed-at-8765): probe 8765, attach
or spawn, never move to another port. Splash screen until the health check
passes, then navigate to `http://127.0.0.1:8765/web`. Exit semantics: confirm
when sessions are live, stop our daemon, never a foreign one.

Two things that are easy to get wrong and are part of "done", not polish:

- **The startup must not run in `setup()`.** That hook runs before the main
  loop, so nothing can paint while it blocks — the splash would be invisible
  for exactly the cold start it exists for. Start the daemon on a background
  thread and navigate from the handle.
- **A replaced app menu owes ⌘Q an accelerator.** Setting a custom menu drops
  the standard one, and a quit item built without an accelerator leaves ⌘Q
  inert — which quietly makes the confirm-before-quit path unreachable by the
  only route anyone uses.
- **`ExitRequested` is not every way out.** A Quit Apple event — Dock,
  right-click, Quit — tears the app down without raising it, and the daemon we
  started was left holding 8765 with launchd for a parent. Stopping it in
  `RunEvent::Exit` as well is what makes "quitting stops that process" true on
  every path. The confirmation is not recoverable there: on that path macOS
  never offers us the chance to refuse, so ⌘Q and the menu item ask about live
  sessions and Dock → Quit does not. Worth knowing before someone reads the
  list below as unconditional.

Done when:

- `tauri dev` opens a window showing the existing UI, with no browser involved.
- With `shellerd` already running, the app attaches: no second process appears
  in `ps`, and quitting the app leaves that daemon running — a browser tab on
  8765 keeps working.
- With nothing running, the app spawns the daemon, and quitting it stops that
  process (verify with `lsof -i :8765` after quit).
- With something else on 8765, the app says so and starts nothing.
- Quitting with a live session that has pending commands asks first, and
  cancelling really cancels.
- Launching the app twice focuses the existing window rather than starting a
  second daemon.
- ⌘Q, not only the menu item, reaches the confirmation.
- The splash is on screen within a second of launch, and stays legible for the
  whole cold start.
- A startup failure shows its reason on the splash with a working **Try
  again** — not a modal over a window that never changes.
- Quitting with a live session leaves `session_close` in that session's
  journal (`~/.odoo-sheller/journals/`), not a transcript that just stops.
- Outside a checkout, with no `ODOO_SHELLER_ROOT`, the app says it cannot find
  the repository. It must not fall back to a path baked in at compile time.

### Stage 2 — PATH and docker

Resolve `docker` in Rust and pass it to the daemon; surface "Docker is not
running" from the probe rather than showing an empty list.

Done when:

- A build launched from Finder (not from a terminal) lists containers.
- With Docker Desktop stopped, the UI shows the probe's own error text.

### Stage 3 — ad-hoc signing, on an empty app

Do this before there is any Python in the bundle. There is no Apple Developer
Program membership and no notarization. The Mac App Store is out: a `.dmg`
someone downloads or is sent, signed ad-hoc (`codesign -s -`). Gatekeeper will
warn on the first launch of a download. That is accepted.

The bundle config is in the repository:

- `odoo-sheller-app/src-tauri/Entitlements.plist` — not sandboxed and empty.
  Not even `disable-library-validation`: the frozen daemon is a separate
  process with its own PyInstaller ad-hoc signature and does not inherit this
  app's hardened runtime. Verified by signing a bundle with an empty plist and
  watching it spawn the daemon, serve `/health` and probe containers.
- `tauri.conf.json > bundle > macOS` — `hardenedRuntime: true`, entitlements
  path, `signingIdentity: "-"`. The `-` is Tauri's ad-hoc identity
  ([docs](https://v2.tauri.app/distribute/sign/macos/#ad-hoc-signing)): it
  signs each nested binary, then the bundle, with the hardened runtime. Do
  not add `--deep` — Apple deprecated it and it signs less than it appears
  to. The bundle identifier is `com.odoo-sheller.desktop`, not `*.app` —
  that suffix is the bundle extension on macOS, and Tauri refuses to
  recommend it.
- `.github/workflows/desktop-macos.yml` — Apple Silicon `.dmg` on
  `desktop-v*` tags and on demand. No Apple secrets. The tag sets the bundle
  version: `tauri.conf.json` carries the app's own version, which is not the
  daemon's, and without this step a `desktop-v0.2.0` tag would ship a file
  named `..._0.1.0_aarch64.dmg`.

```bash
packaging/app.sh dmg        # or: build, install
```

The `.dmg` lands in
`odoo-sheller-app/src-tauri/target/release/bundle/dmg/`. Drag the app onto
Applications.

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

**A locally built copy proves nothing about any of this.** Gatekeeper acts on
`com.apple.quarantine`, an extended attribute that the *downloader* sets —
Safari, Mail, Messages, AirDrop. A `.dmg` you built yourself has no such
attribute, so the app opens with no questions asked and the whole path a
recipient will walk is left untested. To test it, use a quarantined copy:
download the artifact from the Release, or mark one by hand.

```bash
xattr -w com.apple.quarantine "0081;00000000;manual;" /Applications/odoo-sheller.app
xattr -p com.apple.quarantine /Applications/odoo-sheller.app   # confirm it took
```

Done when:

- `packaging/app.sh dmg` produces the artifact and
  `codesign -dvvv` reports `flags=0x10002(adhoc,runtime)`.
- `codesign -d --entitlements -` on the bundle prints an empty set.
- A **quarantined** copy, on a machine that did not build it, opens after
  System Settings → Privacy & Security → Open Anyway.
- The window paints its own background from the first frame: no white flash
  between the splash and the daemon's UI.

### Stage 4 — package the daemon

PyInstaller onedir per decision 1. Specs in `packaging/`. Tauri copies the
trees with map-syntax `bundle.resources` so they land at a stable path, not
under `_up_`:

```
Contents/Resources/odoo-sheller/odoo-sheller
Contents/Resources/odoo-sheller/_internal/
Contents/Resources/odoo-sheller-mcp/odoo-sheller-mcp
Contents/Resources/odoo-sheller-mcp/_internal/
```

That first executable is also the headless daemon (decision 8). Spawn order:
bundled binary if present, else `uv run python -m odoo_sheller` from a
checkout (`tauri dev`), else an error. No compile-time path. `std::process::Command`
is enough; `tauri-plugin-shell` stays out — onedir is not a sidecar.

Headless, with no window and no checkout:

```bash
/Applications/odoo-sheller.app/Contents/Resources/odoo-sheller/odoo-sheller
```

`SKIP_FREEZE=1` skips PyInstaller when both trees are already in
`packaging/dist/`. For iteration on the Rust side only: nothing checks that
those trees match the current Python, so a build made that way can ship
yesterday's daemon without saying so. Release builds do not set it.

Done when:

- The built `.app`, with no Python on the machine's `PATH`, opens a session on a
  real container and runs a command. This is the test that proves
  `bootstrap.py` shipped as data.
- `/web` serves the UI from the frozen daemon, including `/vendor`.
- `GET /health` reports the real version, not `"unknown"` — that is what proves
  `--copy-metadata` took.
- The bundled daemon's path inside the `.app` is the one written above.
- `uv run pytest tests/test_frozen.py -v -m frozen` is the one-command smoke
  test.

### Stage 5 — MCP integration

The MCP binary lives at `Contents/Resources/odoo-sheller-mcp/odoo-sheller-mcp`
(decision 5). **Settings… → MCP** in the application menu — not a button in
the web UI, which is a remote origin with no Tauri IPC — renders the entry
with that path filled in, in both shapes (`mcpServers` for Claude Desktop,
Claude Code and Cursor; `context_servers` for Zed), lists where those files
live, and puts the `mcpServers` form on the clipboard. Under `tauri dev` the
command is `uv --directory <checkout> run python -m odoo_sheller.mcp` instead.

It is drawn by the shell page, not by a native alert. An alert renders its
text proportionally, which collapses the indentation of a JSON block, and it
has one button. The page gives each form a monospace block and its own
**Copy**; Rust still decides what the entry is and puts the first form on the
clipboard when the item is opened.

**It writes nothing** (decision 11). No merge, no backup, no atomic rename —
there is no write path in the code at all. Agent configs hold servers we know
nothing about, `~/.claude.json` is live state that a running Claude Code
rewrites under us, and creating the others would invent configs for apps that
are not installed. No secrets either way: `admin.key` stays in
`~/.odoo-sheller/`.

Done when:

- The menu item shows the bundled binary's absolute path, and the same item
  under `tauri dev` shows the `uv` form.
- The snippet pasted into a config with other servers in it leaves them alone
  — because pasting is all that happens.
- After pasting, an agent with no prior setup can call `os_list_containers`.
- `grep -rn "fs::write\|fs::rename\|OpenOptions" odoo-sheller-app/src-tauri/src/mcp.rs`
  finds nothing. That is the invariant, and it is cheaper to check than to
  argue about.

### Stage 6 — the terminal

Per [architecture.md](architecture.md#the-terminal). A dock at the bottom of
the main window, under the framed UI — not a panel inside `/web`, which is a
remote origin with no Tauri IPC, and not a window of its own, which is not
what "attached to the app" means. Tabs inside the dock. `$SHELL -l`. Output
batched at 32 KiB or 16 ms, whichever first. Closing a tab or quitting the app
reaps the process. The bar at the bottom is always there; **Window → Show
Terminal** (Ctrl+`) toggles the dock, **New Terminal Tab** (⌘T) adds one,
**Previous / Next Terminal Tab** (⌃⌘←, ⌃⌘→) move between them as a ring,
and **Taller / Shorter
Terminal** (⌃⌘↑, ⌃⌘↓) resize the dock in steps. Ctrl and Cmd together for the
last pair because Ctrl+↑ and Ctrl+↓ are Mission Control and App Windows, which
the system takes first — and the same pair on the arrows beside them, so the
dock answers to one family of keys. ⌘W closes a tab and has no menu item: an
item would take that key from the window even with no terminal open, and the
framed UI binds ⌘W for closing a session. Two documents, one key, and whichever
holds focus answers.

**Previous / Next Screen** (⌥⌘←, ⌥⌘→) belong to the same menu even though the
screens are the daemon's UI, not ours. A key reaches a page only while that
page holds focus, and here focus is as often in the terminal below; macOS
offers a key equivalent to the menu before any web view, so the menu is the
only place a window-wide shortcut can live. The framed UI is a remote origin,
so the step crosses to it as a `postMessage` — the page acts on it only when
it comes from its own parent, and the message can do nothing but change which
screen is shown. Unframed, in a browser tab, the page handles the keys itself
— where the browser leaves them alone, which for ⌥⌘arrow it usually does not.
`odoo-sheller-app/src/vendor/` holds `@xterm/xterm` 5.5.0 and
`@xterm/addon-fit` 0.10.0.

Done when:

- `vim` and `htop` draw correctly, including after resizing the window.
- `cat` on a 50 MB file does not freeze the UI.
- Closing a tab leaves no process behind (`pgrep -P <app pid>`), and neither
  does quitting.
- Dragging the dock's top edge resizes it and the shell reflows; the framed UI
  keeps its own scroll position.
- [../security.md](../security.md#the-desktop-terminal) has a section on what
  the terminal widens.

## Test matrix

Run these against a build, not `tauri dev`. Most of them are about a state the
developer's machine is never in.

| Situation | Expected |
|---|---|
| First launch, no `~/.odoo-sheller/` | Directory and `admin.key` created; app starts |
| `shellerd` already running | Attach; quitting the app leaves it alive |
| Something else on 8765 | Clear error naming the port; nothing spawned |
| Docker not running | Probe's error is visible in the UI |
| Docker installed in `~/.docker/bin` only | Containers still listed |
| Quit with a live session | Confirmation naming sessions and pending commands |
| Quit with a foreign daemon | The daemon survives |
| App restarts while an agent holds a session | Agent sees `session_gone`, not a hang |
| Browser tab open on 8765 alongside the app | Both views stay in sync; the one without the key is a watcher |
| Machine with no Python | Everything works |
| Gatekeeper enabled, first run from a download | Refused as an unidentified developer; opens after System Settings → Privacy & Security → Open Anyway |
| Locally built copy, never downloaded | Opens with no prompt — it carries no `com.apple.quarantine`, so this proves nothing about the row above |
| Terminal tab closed, or the app quit | No leftover `$SHELL` (`pgrep -P <app pid>`) |

## Reviewing the result

The review will check the invariants first, then the stages' "done when" lists,
then the test matrix. Two things are worth knowing in advance, because they are
where this design is most likely to be wrong:

- **The port policy.** If attaching to a foreign daemon was skipped because it
  looked like an edge case, the app will stop daemons out from under people who
  use `shellerd`.
- **Data files in the frozen build.** An app that launches proves nothing. The
  first session is the test.

If something in [architecture.md](architecture.md) turns out to be wrong when it
meets the machine, say so and change it there — the document is the design of
record, and it has been wrong before: the port question and the CORS question
were both settled the other way in the first draft.
