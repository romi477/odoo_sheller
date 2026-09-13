# odoo-sheller desktop

A macOS window around the daemon this repository already is. The design of
record is [docs/desktop-app/architecture.md](../docs/desktop-app/architecture.md);
the work is staged in
[docs/desktop-app/implementation.md](../docs/desktop-app/implementation.md).

```bash
# from the repository root, with the Rust toolchain on PATH
cargo tauri dev --manifest-path odoo-sheller-app/src-tauri/Cargo.toml
```

Port **8765** is fixed. If a daemon is already there, the app attaches and
does not kill it on quit. If the port is free, it spawns
`uv run python -m odoo_sheller` from this repository. If something else holds
the port, it says so and starts nothing.

The window loads `http://127.0.0.1:8765/web`. There is no second API and no
bundled copy of the UI.

## After a change

Nothing here builds the daemon: it is spawned as `uv run python -m odoo_sheller`
from this checkout, so Python changes need no Rust build at all. That stops
being true at stage 4, when the daemon is frozen into the bundle.

| Changed | Do |
|---|---|
| `odoo_sheller/web/**` | **⌘R.** The daemon serves those files with `Cache-Control: no-store`, so a reload is the whole cycle. |
| `odoo_sheller/**.py` | Restart the daemon process, then **⌘R**. |
| `odoo-sheller-app/src-tauri/**.rs` | Nothing — `cargo tauri dev` rebuilds and relaunches the window itself. |
| `odoo-sheller-app/src/**` | Nothing — the dev server reloads the splash. |

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
cd odoo-sheller-app
cargo tauri build --bundles dmg
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
