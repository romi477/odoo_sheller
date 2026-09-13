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

## Decisions still open

These change the work, so they need an answer before the stage that depends on
them. They are the owner's to make, not the implementer's. A question that gets
an answer moves down to **Decided** with its reason, so what is still open is
readable at a glance.

| # | Question | Why it matters | Blocks |
|---|---|---|---|
| 1 | onefile or onedir for the daemon? | onefile is one artifact and plugs straight into `externalBin`, but unpacks on every launch and rubs against the hardened runtime. onedir starts faster and signs cleanly, but is not a sidecar — it ships under `resources/` and is spawned by absolute path. | Stage 4 |
| 3 | Terminal in the first release, or after it? | It is the only piece that widens the attack surface, and it is independent of everything else. | Stage 6 |
| 5 | Where does the MCP binary live: a copy in Application Support, or a path inside the bundle? | A copy survives moving the `.app` but goes stale on update; a bundle path updates for free but breaks if the app is moved. | Stage 5 |
| 8 | How does the packaged daemon run **without the window**? | The daemon is needed headless: an agent over MCP and a browser tab on 8765 are both ordinary clients of it, and neither wants a window. Today a shell function covers this by running the checkout's `odoo-sheller` script. After stage 4 a user with no checkout has no way to do it at all — the app *is* the window. Three answers: document a stable path to the bundled daemon and let a shell function point at it; give the app a windowless mode (a menu-bar item holding the daemon), which is a new surface and not in the design; or leave headless use to people with a checkout, and say so. | Stage 4 |

## Decided

| # | Question | Answer |
|---|---|---|
| 2 | `GET /health`, or poll `GET /api/sessions`? | **`GET /health`.** Liveness and version, no session data, no admin key. `/api/sessions` stays as the fallback fingerprint for a daemon older than the route. Shipped. |
| 4 | May the app stop a daemon it did not start? | **No**, and not behind a confirmation either. The developer running `shellerd` in a terminal is the ordinary case, not an obstacle. |
| 6 | Does the app live in this repository? | **Yes**, in `odoo-sheller-app/`, `Cargo.lock` committed. It is versioned with the daemon it drives, and the repository-side changes it needs (`ODOO_SHELLER_DOCKER`, `/health`) are reviewed in the same diff. |
| 7 | Does the app refuse a daemon older than itself? | **No version gate.** All it needs from an attached daemon is `/web`, which every version serves. The version from `/health` is recorded, not enforced — see [architecture.md](architecture.md#the-port-is-fixed-at-8765). |
| 9 | Paid Developer ID, or ad-hoc? | **Ad-hoc** (`signingIdentity: "-"`). There is no Apple Developer Program membership. Gatekeeper refuses a downloaded copy until the person clears it in System Settings → Privacy & Security → Open Anyway. |
| 10 | Apple Silicon only, or universal? | **arm64 only** for now. CI builds `aarch64-apple-darwin`; an Intel Mac gets nothing. Revisit when someone actually has one — a universal build also needs universal2 wheels for `pydantic-core`, `uvloop` and `httptools`. |

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
- **A frozen-binary smoke test**: build with PyInstaller, run the artifact, open
  a session against a container, close it. This is the only thing that catches
  `bootstrap.py` or `web/` not being bundled as data. It cannot live in the unit
  suite (it needs Docker and a build); it belongs beside `tests/test_e2e.py`
  with its own marker.
- **Packaging metadata**: the `--add-data` list for `odoo_sheller/web` and
  `odoo_sheller/bootstrap.py`, plus `--copy-metadata odoo-sheller`, kept in the
  repository rather than in someone's build script, so it is reviewed when
  those paths change.
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

- `odoo-sheller-app/src-tauri/Entitlements.plist` — JIT for the WebView, client
  network for `127.0.0.1:8765`. Not sandboxed. The PyInstaller entitlements
  (`disable-library-validation`, sometimes `allow-dyld-environment-variables`)
  wait for Stage 4.
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
cd odoo-sheller-app
cargo tauri build --bundles dmg
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

- `cargo tauri build --bundles dmg` produces the artifact and
  `codesign -dvvv` reports `flags=0x10002(adhoc,runtime)`.
- `codesign -d --entitlements -` on the bundle prints an empty set.
- A **quarantined** copy, on a machine that did not build it, opens after
  System Settings → Privacy & Security → Open Anyway.
- The window paints its own background from the first frame: no white flash
  between the splash and the daemon's UI.

### Stage 4 — package the daemon

PyInstaller per decision 1, `--add-data` for `web/` and `bootstrap.py`,
`--copy-metadata odoo-sheller`, hidden imports for uvicorn. Sign the nested
binary ad-hoc the same way as the empty app, then rebuild the `.dmg`. This is
also where `tauri-plugin-shell` comes back if the daemon is shipped as a
sidecar — it was removed after Stage 1 rather than left initialised and unused,
since Stage 1 spawns with `std::process::Command`.

Done when:

- The built `.app`, with no Python on the machine's `PATH`, opens a session on a
  real container and runs a command. This is the test that proves
  `bootstrap.py` shipped as data.
- `/web` serves the UI from the frozen daemon, including `/vendor`.
- `GET /health` reports the real version, not `"unknown"` — that is what proves
  `--copy-metadata` took.
- The bundled daemon's path inside the `.app` is written down in this document,
  whatever decision 8 turns out to be. Both answers need it, and a path found by
  poking around a bundle is a path that breaks on the next release.
- The smoke test from "Work in this repository" runs in one command.

### Stage 5 — MCP integration

Place the MCP binary per decision 5. A command in the **application menu** —
not a button in the web UI, which is a remote origin with no Tauri IPC — that
merges our entry into the agent configs: Claude Desktop
(`~/Library/Application Support/Claude/claude_desktop_config.json`, `mcpServers`),
Claude Code (`~/.claude.json`, or `.mcp.json` in a project), Cursor
(`~/.cursor/mcp.json`), Zed (`settings.json`, `context_servers`, a different
shape). Back up before writing; write atomically (temp file, rename). No secrets
in those files: `admin.key` stays in `~/.odoo-sheller/`.

Done when:

- Running the command twice leaves one entry, not two, and leaves every other
  server in the file untouched.
- A config with a syntax error is reported, not overwritten.
- After the merge, an agent with no prior setup can call `os_list_containers`.

### Stage 6 — the terminal

Per [architecture.md](architecture.md#the-terminal). Tabs, `$SHELL -l`,
throttled output, processes reaped on close and on exit.

Done when:

- `vim` and `htop` draw correctly, including after resizing the window.
- `cat` on a 50 MB file does not freeze the UI.
- Closing a tab leaves no process behind (`pgrep -P <app pid>`), and neither
  does quitting.
- [../security.md](../security.md) has a section on what the terminal widens.

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
