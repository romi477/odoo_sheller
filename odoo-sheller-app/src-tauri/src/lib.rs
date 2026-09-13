//! Window and process supervisor around the daemon.

mod daemon;
mod docker;

use std::process::Child;
use std::sync::Mutex;
use std::time::Duration;

use daemon::{PortState, occupied_message, quit_message};
use serde::Serialize;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{AppHandle, Emitter, Manager, RunEvent, State, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

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

fn navigate_to_ui(app: &AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or_else(|| "main window missing".to_string())?;
    let url: tauri::Url = daemon::ui_url().parse().map_err(|err| format!("{err}"))?;
    window.navigate(url).map_err(|err| err.to_string())
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
            let root = daemon::repo_root().ok_or_else(|| {
                "could not find the odoo-sheller repository (no pyproject.toml above \
                 this binary). Set ODOO_SHELLER_ROOT to the checkout."
                    .to_string()
            })?;
            let docker = docker::resolve_docker();
            let child = daemon::spawn_daemon(root.as_path(), docker.as_deref())?;
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
        let outcome = start_daemon(&app).and_then(|()| navigate_to_ui(&app));
        match outcome {
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
    if let Ok(sessions) = daemon::list_sessions() {
        if !sessions.is_empty() {
            let pending = sessions.iter().map(|session| session.pending_commands).sum();
            let message = quit_message(sessions.len(), pending);
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
    let app_menu = Submenu::with_items(
        app,
        "odoo-sheller",
        true,
        &[
            &PredefinedMenuItem::hide(app, None)?,
            &PredefinedMenuItem::hide_others(app, None)?,
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
    let window = Submenu::with_items(
        app,
        "Window",
        true,
        &[
            &PredefinedMenuItem::minimize(app, None)?,
            &PredefinedMenuItem::fullscreen(app, None)?,
        ],
    )?;
    let menu = Menu::with_items(app, &[&app_menu, &edit, &view, &window])?;
    app.set_menu(menu)?;

    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            focus_main(app);
        }))
        .plugin(tauri_plugin_dialog::init())
        .manage(Supervisor::default())
        .invoke_handler(tauri::generate_handler![
            startup_state,
            retry_startup,
            quit_app
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            build_menu(&handle)?;
            app.on_menu_event(|app, event| match event.id().0.as_str() {
                "quit" => app.exit(0),
                // A reload while the daemon is missing is a retry, not a
                // navigation to a port with nothing behind it.
                "reload" => {
                    if navigate_to_ui(app).is_err() || !daemon_is_up(app) {
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
            #[cfg(target_os = "macos")]
            RunEvent::Reopen { .. } => focus_main(app),
            _ => {}
        });
}
