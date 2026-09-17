# odoo-sheller desktop

A macOS window around the daemon this repository already is. The design of
record is [docs/desktop-app/architecture.md](../docs/desktop-app/architecture.md);
the work is staged in
[docs/desktop-app/implementation.md](../docs/desktop-app/implementation.md).

## What you need

| | |
|---|---|
| Xcode command line tools | `xcode-select --install` — the linker and the macOS SDK |
| Rust | [rustup](https://rustup.rs); `~/.cargo/bin` on `PATH` |
| Tauri CLI | `cargo install tauri-cli --locked` — gives `cargo tauri` |
| uv | already needed for the daemon; the freeze runs PyInstaller through it |

Nothing else. There is no npm, no bundler and no build step for the frontend:
`src/` is the page, and `src/vendor/` holds xterm.js as downloaded.

```bash
cargo tauri --version      # the CLI is found
uv sync --group dev        # pulls PyInstaller in for the freeze
```

## Running it from source

```bash
cd odoo-sheller-app && cargo tauri dev
```

That rebuilds the Rust side on every save and reloads the page. The daemon it
talks to comes from this checkout, not from a bundle. The `shellerd` shell
function in the main README wraps both:

```bash
shellerd app        # daemon if needed, then the window, detached
```

Port **8765** is fixed. If a daemon is already there, the app attaches and
does not kill it on quit. If the port is free, it spawns the bundled
`Contents/Resources/odoo-sheller/odoo-sheller` when that file exists, otherwise
`uv run python -m odoo_sheller` from this repository. If something else holds
the port, it says so and starts nothing.

The window holds a local shell page: the daemon's UI in an `<iframe>`, and a
terminal dock under it. There is no second API and no bundled copy of the UI —
the frame loads `http://127.0.0.1:8765/web` like any browser tab. The frame
rather than a navigation, because only a local page can reach the Tauri
commands, and the terminal has to be in the same window.

**About odoo-sheller** is a sheet of its own, in Settings' frame: the version
straight from the crate, what this app is in two paragraphs, and the daemon's
address. The platform's panel takes a line of metadata and shows a version;
this takes a little more saying.

**Settings…** (⌘,) shows text that belongs in a file this app does not own,
each block with a button that copies it. **MCP** is the entry to paste into an
agent's config, with the bundled binary's path filled in. **SSH** is the two
lines that let an odoo.sh build be opened without a terminal to answer its
host-key question. Nothing there is saved or applied: those files are yours,
they hold settings this app knows nothing about, and one of them is rewritten
by a running Claude Code. The button says Close for that reason.

The framed UI is asked for as `/web?app=1`, and that is the only thing it is
told about being framed: it drops the `swagger` link. `/docs` leads out of the
UI with no way back — this window has no address bar — and it is the one page
served here that needs the network.

The dock holds `$SHELL -l` tabs. That is your machine, not the Odoo REPL.

| | |
|---|---|
| Ctrl+` | show or hide the dock |
| ⌘T | new tab |
| ⌃⌘← / ⌃⌘→ | previous, next tab |
| ⌘W | close the tab |
| ⌃⌘↑ / ⌃⌘↓ | taller, shorter |

And for the UI above it:

| | |
|---|---|
| ⌥⌘← / ⌥⌘→ | previous, next screen |
| ⌃⇧← / ⌃⇧→ | previous, next session |
| ⌘W | close the session |
| ⌘, | Settings |
| ⌘R | reload the UI |

Every one of those is a menu item, which is what makes it work wherever the
focus is: macOS offers a key equivalent to the menu before any web view sees
it. The screens live in the framed UI, a remote origin, so the app forwards the
step to it as a message — the one thing that crosses that boundary.

The top edge drags too, and **Window** holds all of it as menu items.

## Building

One script, so the step that deletes something is reviewed rather than
retyped:

```bash
packaging/app.sh build      # the .app
packaging/app.sh dmg        # the .dmg to hand someone
packaging/app.sh install    # build, then replace /Applications/odoo-sheller.app
```

`build` is `cargo tauri build --bundles app`, which runs `packaging/freeze.sh`
as `beforeBuildCommand` and copies both onedir trees into the bundle. There is
no separate PyInstaller step to forget. What it leaves behind:

```
odoo-sheller-app/src-tauri/target/release/bundle/macos/odoo-sheller.app
odoo-sheller-app/src-tauri/target/release/bundle/dmg/odoo-sheller_<version>_aarch64.dmg
```

The first build compiles the whole Tauri dependency tree and takes minutes;
later ones are incremental. `packaging/dist/` holds the frozen daemon between
builds and is gitignored.

`install` quits a running copy first — replacing a bundle underneath itself
leaves the old process alive holding a deleted tree — and refuses to delete
anything whose `CFBundleIdentifier` is not `com.odoo-sheller.desktop`.
`APP_DEST` overrides where it goes.

`SKIP_FREEZE=1` reuses `packaging/dist/` instead of running PyInstaller. For
iterating on the Rust side only: nothing checks that those trees match the
current Python, so a build made that way can ship yesterday's daemon.

## After a change

Day to day you do not build at all. `shellerd app` runs the daemon from the
checkout and the window from `cargo tauri dev`; building is for the artifact,
or to test what dev mode cannot show — a minimal `PATH`, the hardened runtime,
the frozen daemon.

| Changed | In `shellerd app` | In the installed `.app` |
|---|---|---|
| `odoo_sheller/web/**` | ⌘R | ⌘R — the daemon serves them |
| `odoo_sheller/**.py` | `shellerd restart`, then ⌘R | `packaging/app.sh install` |
| `odoo-sheller-app/src-tauri/**.rs` | nothing, the watcher rebuilds | `packaging/app.sh install` |
| `odoo-sheller-app/src/**` | nothing, the watcher reloads | `packaging/app.sh install` |

The right-hand column is for a bundle that carries its own frozen daemon. An
installed app with nothing frozen in it attaches to whatever daemon is on
8765, so a Python change reaches it after `shellerd restart` like anywhere
else.

Restarting the daemon kills every live session, by design: the daemon owns the
pipes. ⌘Q says so first when any session is open.

The reload matters after a daemon restart and not only for looks: the registry
socket reconnects on its own after three seconds, but tabs for sessions that
died with the old process are pruned on page load.

The app deliberately does not pass `--reload` to the daemon. It restarts on
every saved file, and each restart takes the live sessions with it.

## One command for both

The main README's [`shellerd`
function](../README.md#keeping-it-in-the-background) manages the daemon. These
cases add the window to it: drop them into the same `case`, and add `app`,
`quit`, `down` and `applog` to its help text. They use `$dir`, `$health`,
`$ours` and `$up` from the top of that function.

```zsh
    app)
      # The daemon stays ours, not the window's: quitting the app then leaves
      # it running, and status/log/restart keep working on it. A daemon that
      # is already there — anyone's — is attached to rather than duplicated.
      if [ -z "$ours" ] && [ -z "$up" ]; then
        shellerd start || return 1
        # Wait for it to hold the port. The app probes on launch, and a probe
        # that finds 8765 free spawns a second daemon that loses the bind.
        local i
        for i in $(seq 1 40); do
          curl -fsS -m 1 "$health" >/dev/null 2>&1 && break
          sleep 0.25
        done
      fi
      local applog=~/.odoo-sheller/app.log apppid=~/.odoo-sheller/app.pid
      local wpid; wpid=$(cat "$apppid" 2>/dev/null)
      if [ -n "$wpid" ] && kill -0 "$wpid" 2>/dev/null; then
        echo "window already running (pid $wpid)"; return 1
      fi
      ( umask 077; : >"$applog" )
      # `cargo tauri dev` is a foreground watcher; detached it keeps rebuilding
      # on a Rust change, and its build output goes to the log instead of here.
      # Cmd+Q ends both; `shellerd quit` does it from here.
      ( cd "$dir/odoo-sheller-app" || exit
        PATH="$HOME/.cargo/bin:$PATH" nohup cargo tauri dev >>"$applog" 2>&1 &
        echo $! >"$apppid" )
      echo "window starting (pid $(cat "$apppid")) — first build takes minutes"
      echo "build output: shellerd applog   |   quit with Cmd+Q"
      ;;
    quit)
      local apppid=~/.odoo-sheller/app.pid
      local wpid; wpid=$(cat "$apppid" 2>/dev/null)
      if [ -n "$wpid" ] && kill -0 "$wpid" 2>/dev/null; then
        # The window first, while it is still a child of the watcher: after
        # the watcher dies its children are reparented and harder to find,
        # and `tauri dev` does not reliably take the window down with itself.
        local kid
        for kid in $(pgrep -P "$wpid" 2>/dev/null); do kill "$kid" 2>/dev/null; done
        kill "$wpid" 2>/dev/null
        for _ in $(seq 1 20); do kill -0 "$wpid" 2>/dev/null || break; sleep 0.25; done
        rm -f "$apppid"
        # Nothing is lost: the daemon is not this window's to stop, so the
        # sessions it holds outlive this and the quit dialog has nothing to ask.
        echo "window closed"; return 0
      fi
      rm -f "$apppid"
      # No watcher of ours, so this is a bundled app. Ask it the way the menu
      # does — a real Quit event — so it can warn about live sessions and stop
      # the daemon it started itself. Killing it would skip both.
      if pgrep -f "odoo-sheller.app/Contents/MacOS/" >/dev/null 2>&1; then
        osascript -e 'quit app id "com.odoo-sheller.desktop"' >/dev/null 2>&1
        echo "asked the app to quit"; return 0
      fi
      echo "window not running"; return 1
      ;;
    down) shellerd quit; shellerd stop ;;
    applog) tail -f ~/.odoo-sheller/app.log ;;
```

| Command | Effect |
|---|---|
| `shellerd app` | daemon if needed, then the window, detached |
| `shellerd applog` | follow the build output |
| `shellerd quit` | close the window; the daemon keeps running |
| `shellerd down` | close the window and stop the daemon |
| `shellerd restart` | restart the daemon **under** an open window — then ⌘R |

`stop` and `restart` deliberately leave the window open. They are the Python
edit cycle, and a restart that closed the window would make it unusable.

### An app launched as an app

A bundle opened from Finder or Spotlight writes no pid files, so the function
cannot see it the way it sees its own `cargo tauri dev`. Two things follow.

`shellerd quit` falls back to a real Quit event
(`osascript -e 'quit app id "com.odoo-sheller.desktop"'`), which is what the
menu item does: the app gets to warn about live sessions and to stop the
daemon it started itself. Killing the process would skip both.

`start`, `stop` and `status` look at the port as well as at the pid file, so a
daemon that belongs to such an app is reported rather than stepped on —
`status` says "running on 8765, but not ours", and `start` refuses instead of
launching a second daemon that would die on the bind and leave a pid file
pointing at a corpse.

## A `.dmg` to send someone

The Mac App Store is out. There is no paid Apple Developer account. The
build is ad-hoc signed: drag the app onto Applications, or download it
from a GitHub Release.

```bash
packaging/app.sh dmg
```

`signingIdentity` is `"-"` in `tauri.conf.json`, so this needs no
certificate.

A copy someone downloads carries `com.apple.quarantine`, and Gatekeeper
refuses it as an unidentified developer. Clearing that is **System Settings
→ Privacy & Security → Open Anyway** — Control-click → Open stopped working
for this in macOS 15. Or drop the flag before launching:

```bash
xattr -dr com.apple.quarantine /Applications/odoo-sheller.app
```

`-d com.apple.quarantine`, not `-c`: `-c` clears every extended attribute
the file has.

A build you made yourself has no quarantine attribute and opens with no
prompt — which is exactly why it is no test of the paragraph above.

After that it opens normally. Tagged `desktop-v*` pushes build the same
`.dmg` on GitHub Actions.
