//! Window and process supervisor around the daemon.

mod daemon;
mod docker;
mod mcp;
mod pty;

use std::process::Child;
use std::sync::Mutex;
use std::time::Duration;

use daemon::{PortState, occupied_message, quit_message};
use serde::Serialize;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{AppHandle, Emitter, Manager, RunEvent, State, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use pty::{OnExit, OnOutput, PtyHub};

/// How long the daemon gets to shut down cleanly before it is killed. Longer
/// than the daemon's own `timeout_graceful_shutdown`, so the sessions it
/// closes on the way out have time to be journalled.
const STOP_GRACE: Duration = Duration::from_secs(8);

/// What the splash screen is showing. The window is created before any of
/// this is known, so the page asks for the current phase on load and listens
/// for the rest — an event alone would race the page it is meant to reach.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "phase", rename_all = "lowercase")]
enum Startup {
    Starting,
    Ready,
    Error { message: String },
}

#[derive(Default)]
struct Supervisor {
    /// The daemon we started, if we started one. `None` means we attached to
    /// somebody else's, and quitting must leave it alone.
    child: Mutex<Option<Child>>,
    startup: Mutex<Option<Startup>>,
    /// An attempt is in flight. Retry is a button; two of them at once would
    /// race for the same port.
    starting: Mutex<bool>,
}

impl Supervisor {
    fn ours(&self) -> bool {
        self.child.lock().expect("daemon lock").is_some()
    }

    fn stop(&self) {
        if let Some(mut child) = self.child.lock().expect("daemon lock").take() {
            daemon::stop_child(&mut child, STOP_GRACE);
        }
    }
}

fn set_startup(app: &AppHandle, startup: Startup) {
    if let Some(state) = app.try_state::<Supervisor>() {
        *state.startup.lock().expect("startup lock") = Some(startup.clone());
    }
    let _ = app.emit("startup", startup);
}

#[tauri::command]
fn startup_state(state: State<'_, Supervisor>) -> Startup {
    state
        .startup
        .lock()
        .expect("startup lock")
        .clone()
        .unwrap_or(Startup::Starting)
}

#[tauri::command]
fn retry_startup(app: AppHandle) {
    begin_startup(app);
}

#[tauri::command]
fn quit_app(app: AppHandle) {
    app.exit(0);
}

/// Where the page should point its frame. Asked for rather than written into
/// the page, so the fixed port has one definition and `daemon.rs` keeps it.
#[tauri::command]
fn ui_url() -> String {
    daemon::ui_url()
}

#[tauri::command]
fn pty_create(app: AppHandle, hub: State<'_, PtyHub>) -> Result<String, String> {
    let out_app = app.clone();
    let exit_app = app.clone();
    let on_output: OnOutput = std::sync::Arc::new(move |id, bytes| {
        let _ = out_app.emit(&format!("pty-output-{id}"), STANDARD.encode(bytes));
    });
    let on_exit: OnExit = std::sync::Arc::new(move |id| {
        let _ = exit_app.emit(&format!("pty-exit-{id}"), ());
    });

    hub.create(on_output, on_exit)
}

#[tauri::command]
fn pty_write(id: String, data: String, hub: State<'_, PtyHub>) -> Result<(), String> {
    hub.write(&id, data.as_bytes())
}

#[tauri::command]
fn pty_resize(id: String, cols: u16, rows: u16, hub: State<'_, PtyHub>) -> Result<(), String> {
    hub.resize(&id, cols, rows)
}

#[tauri::command]
fn pty_close(id: String, hub: State<'_, PtyHub>) {
    hub.close(&id);
}

#[tauri::command]
fn shell_name() -> String {
    pty::shell_name()
}

fn daemon_is_up(app: &AppHandle) -> bool {
    app.try_state::<Supervisor>()
        .map(|state| {
            matches!(
                *state.startup.lock().expect("startup lock"),
                Some(Startup::Ready)
            )
        })
        .unwrap_or(false)
}

fn confirm_quit(app: &AppHandle, message: &str) -> bool {
    app.dialog()
        .message(message)
        .title("odoo-sheller")
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Quit".into(),
            "Cancel".into(),
        ))
        .blocking_show()
}

/// Probe, attach or spawn. Blocking, and slow enough to matter: a cold start
/// pays for `uv` plus uvicorn, which is why nobody calls this on the main
/// thread — the splash has to be on screen while it runs.
fn start_daemon(app: &AppHandle) -> Result<(), String> {
    match daemon::probe().state {
        PortState::OurDaemon => Ok(()),
        PortState::Occupied => Err(occupied_message()),
        PortState::Free => {
            let resource = app.path().resource_dir().ok();
            let kind = daemon::resolve_daemon(
                resource.as_deref(),
                daemon::repo_root().as_deref(),
            )?;
            let docker = docker::resolve_docker();
            let child = daemon::spawn_daemon(&kind, docker.as_deref())?;
            // Recorded before the wait, not after: a quit during startup has
            // to find this child, or it outlives the app that started it.
            let state = app.state::<Supervisor>();
            *state.child.lock().expect("daemon lock") = Some(child);
            if let Err(err) = daemon::wait_until_ready() {
                state.stop();

                return Err(err);
            }

            Ok(())
        }
    }
}

fn begin_startup(app: AppHandle) {
    {
        let state = app.state::<Supervisor>();
        let mut starting = state.starting.lock().expect("starting lock");
        if *starting {
            return;
        }
        *starting = true;
    }
    set_startup(&app, Startup::Starting);
    std::thread::spawn(move || {
        // `Ready` is the whole signal: the page frames the daemon's UI itself.
        // Navigating the window there instead would leave no local page to
        // host the terminal, and no way back to the splash on a later error.
        match start_daemon(&app) {
            Ok(()) => set_startup(&app, Startup::Ready),
            Err(message) => set_startup(&app, Startup::Error { message }),
        }
        *app.state::<Supervisor>()
            .starting
            .lock()
            .expect("starting lock") = false;
    });
}

fn should_allow_exit(app: &AppHandle) -> bool {
    let Some(state) = app.try_state::<Supervisor>() else {
        return true;
    };
    if !state.ours() {
        return true;
    }
    let terminals = app
        .try_state::<PtyHub>()
        .map(|hub| hub.count())
        .unwrap_or(0);
    if let Ok(sessions) = daemon::list_sessions() {
        if !sessions.is_empty() || terminals > 0 {
            let pending = sessions.iter().map(|session| session.pending_commands).sum();
            let message = quit_message(sessions.len(), pending, terminals);
            if !confirm_quit(app, &message) {
                return false;
            }
        }
    }
    state.stop();

    true
}

fn focus_main(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn build_menu(app: &AppHandle) -> tauri::Result<()> {
    // The custom app menu replaces the standard one, so everything the
    // platform would have given us has to be put back by hand. The
    // accelerator most of all: without it Cmd+Q does nothing, and the
    // confirm-before-quit path is reachable only by opening the menu.
    let quit = MenuItem::with_id(
        app,
        "quit",
        "Quit odoo-sheller",
        true,
        Some("CmdOrCtrl+Q"),
    )?;
    let configure = MenuItem::with_id(
        app,
        "mcp-config",
        "MCP Configuration…",
        true,
        None::<&str>,
    )?;
    let app_menu = Submenu::with_items(
        app,
        "odoo-sheller",
        true,
        &[
            &PredefinedMenuItem::hide(app, None)?,
            &PredefinedMenuItem::hide_others(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &configure,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;
    let edit = Submenu::with_items(
        app,
        "Edit",
        true,
        &[
            &PredefinedMenuItem::undo(app, None)?,
            &PredefinedMenuItem::redo(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::cut(app, None)?,
            &PredefinedMenuItem::copy(app, None)?,
            &PredefinedMenuItem::paste(app, None)?,
            &PredefinedMenuItem::select_all(app, None)?,
        ],
    )?;
    // The page comes from the daemon over HTTP, so reloading it is how a
    // change to `web/` — or a daemon restarted underneath the window — is
    // picked up. Nothing else in the app offers it: a replaced menu has no
    // View, and the WebView's own shortcut is not bound.
    let reload = MenuItem::with_id(app, "reload", "Reload", true, Some("CmdOrCtrl+R"))?;
    let view = Submenu::with_items(app, "View", true, &[&reload])?;
    // Ctrl+` the way every editor binds it, and Cmd+T for a new tab the way
    // every browser does. Not CmdOrCtrl for the toggle: Cmd+` is already
    // "cycle windows" on macOS.
    let terminal = MenuItem::with_id(
        app,
        "terminal",
        "Show Terminal",
        true,
        Some("Ctrl+Backquote"),
    )?;
    let new_tab = MenuItem::with_id(
        app,
        "terminal-new-tab",
        "New Terminal Tab",
        true,
        Some("CmdOrCtrl+T"),
    )?;
    let window = Submenu::with_items(
        app,
        "Window",
        true,
        &[
            &PredefinedMenuItem::minimize(app, None)?,
            &PredefinedMenuItem::fullscreen(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &terminal,
            &new_tab,
        ],
    )?;
    let menu = Menu::with_items(app, &[&app_menu, &edit, &view, &window])?;
    app.set_menu(menu)?;

    Ok(())
}

fn show_mcp_config(app: &AppHandle) {
    let resource = app.path().resource_dir().ok();
    let launch = match daemon::resolve_mcp(resource.as_deref(), daemon::repo_root().as_deref()) {
        Ok(launch) => launch,
        Err(message) => {
            app.dialog()
                .message(message)
                .title("odoo-sheller")
                .kind(MessageDialogKind::Error)
                .blocking_show();
            return;
        }
    };
    let mut text = mcp::instructions(&launch);
    // Copying is the whole convenience here, so a failure has to be visible:
    // a dialog that claims the clipboard holds something it does not is worse
    // than no clipboard at all.
    if let Err(err) = mcp::copy_to_clipboard(&launch.snippet("mcpServers")) {
        text.push_str(&format!("\n(clipboard unavailable: {err})"));
    }
    app.dialog()
        .message(text)
        .title("MCP Configuration")
        .kind(MessageDialogKind::Info)
        .blocking_show();
}

/// The terminal is a panel in the main window, so the menu only says so and
/// the page decides what that means — open the dock, or add a tab to it.
fn tell_shell(app: &AppHandle, event: &str) {
    focus_main(app);
    let _ = app.emit(event, ());
}

fn reap_terminals(app: &AppHandle) {
    if let Some(hub) = app.try_state::<PtyHub>() {
        hub.close_all();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            focus_main(app);
        }))
        .plugin(tauri_plugin_dialog::init())
        .manage(Supervisor::default())
        .manage(PtyHub::default())
        .invoke_handler(tauri::generate_handler![
            startup_state,
            retry_startup,
            quit_app,
            ui_url,
            pty_create,
            pty_write,
            pty_resize,
            pty_close,
            shell_name
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            build_menu(&handle)?;
            app.on_menu_event(|app, event| match event.id().0.as_str() {
                "quit" => app.exit(0),
                "mcp-config" => show_mcp_config(app),
                "terminal" => tell_shell(app, "terminal-toggle"),
                "terminal-new-tab" => tell_shell(app, "terminal-new-tab"),
                // Reload the framed UI, not the shell: reloading the shell
                // would take every terminal tab with it. With no daemon
                // behind the frame there is nothing to reload, so it is a
                // retry instead.
                "reload" => {
                    if daemon_is_up(app) {
                        let _ = app.emit("reload-ui", ());
                    } else {
                        begin_startup(app.clone());
                    }
                }
                _ => {}
            });
            // `setup` runs before the main loop, so the window does not exist
            // yet and nothing can paint. Starting the daemon here would hold
            // the splash off the screen for exactly as long as the user most
            // needs to see it.
            begin_startup(handle);

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| match event {
            RunEvent::ExitRequested { api, .. } => {
                if !should_allow_exit(app) {
                    api.prevent_exit();
                }
            }
            // The last word, and not a duplicate of the branch above: a Quit
            // Apple event — Dock, right-click, Quit — tears the app down
            // without ever raising ExitRequested, and the daemon we started
            // was left holding 8765 with launchd as its parent. `stop` takes
            // the child out of the handle, so the ordinary path runs it once
            // and this finds nothing.
            RunEvent::Exit => {
                reap_terminals(app);
                if let Some(state) = app.try_state::<Supervisor>() {
                    state.stop();
                }
            }
            #[cfg(target_os = "macos")]
            RunEvent::Reopen { .. } => focus_main(app),
            _ => {}
        });
}
