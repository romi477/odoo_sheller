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
three cases add the window to it — drop them into the same `case`, keeping
`$dir` as it is there:

```zsh
    app)
      # The daemon stays shellerd's, not the window's: quitting the app then
      # leaves it running, and status/log/restart keep working on it.
      if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        shellerd start || return 1
      fi
      # Wait for it to hold the port. The app probes on launch, and a probe
      # that finds 8765 free spawns a second daemon that then loses the bind.
      for _ in $(seq 1 40); do
        curl -fsS -m 1 http://127.0.0.1:8765/health >/dev/null 2>&1 && break
        sleep 0.25
      done
      local applog=~/.odoo-sheller/app.log apppid=~/.odoo-sheller/app.pid
      local wpid; wpid=$(cat "$apppid" 2>/dev/null)
      if [ -n "$wpid" ] && kill -0 "$wpid" 2>/dev/null; then
        echo "window already running (pid $wpid)"; return 1
      fi
      ( umask 077; : >"$applog" )
      # `cargo tauri dev` is a foreground watcher; detached it keeps rebuilding
      # on a Rust change, and its build output goes to the log instead of here.
      ( cd "$dir/odoo-sheller-app" || exit
        PATH="$HOME/.cargo/bin:$PATH" nohup cargo tauri dev >>"$applog" 2>&1 &
        echo $! >"$apppid" )
      echo "window starting (pid $(cat "$apppid")) — first build takes minutes"
      echo "build output: shellerd applog   |   quit with Cmd+Q"
      ;;
    quit)
      local apppid=~/.odoo-sheller/app.pid
      local wpid; wpid=$(cat "$apppid" 2>/dev/null)
      if [ -z "$wpid" ] || ! kill -0 "$wpid" 2>/dev/null; then
        rm -f "$apppid"; echo "window not running"; return 1
      fi
      # The window first, while it is still a child of the watcher: after the
      # watcher dies its children are reparented and harder to find, and
      # `tauri dev` does not reliably take the window down with itself.
      local kid
      for kid in $(pgrep -P "$wpid" 2>/dev/null); do kill "$kid" 2>/dev/null; done
      kill "$wpid" 2>/dev/null
      for _ in $(seq 1 20); do kill -0 "$wpid" 2>/dev/null || break; sleep 0.25; done
      rm -f "$apppid"
      # Nothing is lost here: the daemon is not the window's to stop, so the
      # sessions it holds outlive this and the quit dialog has nothing to ask.
      echo "window closed"
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
