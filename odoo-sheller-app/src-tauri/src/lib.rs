//! Window and process supervisor around the daemon.

mod configure;
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
use tauri::{
    AppHandle, Emitter, Manager, PhysicalPosition, PhysicalSize, RunEvent, State, WindowEvent,
};
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

/// The file the daemon created before it listened. None if it is missing or
/// empty — the framed UI then keeps the paste prompt. Never written here.
#[tauri::command]
fn read_admin_key() -> Option<String> {
    daemon::read_admin_key()
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
        // Before the daemon, and whether or not it comes up: the link is about
        // the MCP binary, and an agent config that names it should work from
        // the first launch rather than from the first visit to a menu.
        let _ = mcp_launch(&app);
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

/// A window rectangle, in physical pixels, the way macOS reports both a
/// display's usable area and a window's own frame.
#[derive(Clone, Copy, PartialEq, Debug)]
struct Frame {
    x: i32,
    y: i32,
    width: u32,
    height: u32,
}

/// How wide a compact window is, as a share of the display's usable width.
/// Narrow enough that an editor beside it keeps a readable line length, wide
/// enough that this UI's own cards do not start wrapping.
const COMPACT_SHARE: f64 = 0.38;

/// Past this the window grows but the page does not: `.app` in the daemon's
/// stylesheet is `width: min(1180px, 100%)` and centred, inside 24px of body
/// padding and the 1px border of `.shell` on each side. Everything wider is
/// the window's own empty margin, which is what a share of a 4K display buys.
/// In CSS pixels, so a display's scale factor applies before it is compared
/// with anything the window system reports.
const CANVAS_CSS_WIDTH: f64 = 1180.0 + 2.0 * 24.0 + 2.0 * 1.0;

/// How far off an edge a window may sit and still count as parked there.
/// `set_position` and `outer_position` disagree by a pixel or two on a scaled
/// display, and an exact comparison would break the toggle on exactly the
/// machines this is for.
const EDGE_SLACK: i32 = 8;

/// Where a compact window goes on a given display.
///
/// `cap` is the widest the page can actually use. A share of a big display
/// overshoots it — on a 4K panel 38% is wider than the canvas will ever fill,
/// and the difference is empty margin inside the window rather than anything
/// to read.
///
/// The left edge first: whatever is read beside it — an editor, a browser —
/// is the wider of the two and belongs where the eye starts. Called again on
/// a window that is already parked there, it crosses to the right edge, so
/// one menu item reaches both sides and there is no second one to remember.
///
/// Height is never a question: a compact window is full height, because the
/// point is a column beside something else, not a smaller window.
fn compact_frame(area: Frame, current: Frame, cap: u32) -> Frame {
    let width = ((f64::from(area.width) * COMPACT_SHARE).round() as u32).min(cap);
    let right = area.x + area.width as i32 - width as i32;
    let parked_left = (current.x - area.x).abs() <= EDGE_SLACK
        && (current.width as i32 - width as i32).abs() <= EDGE_SLACK;

    Frame {
        x: if parked_left { right } else { area.x },
        y: area.y,
        width,
        height: area.height,
    }
}

/// Put the window into a column against one edge of the display it is on.
fn compact_window(app: &AppHandle) {
    let Some(window) = app.get_webview_window("main") else {

        return;
    };
    // Nothing to resize inside a full-screen space, and the call would be
    // swallowed without a word.
    if window.is_fullscreen().unwrap_or(false) {
        let _ = window.set_fullscreen(false);
    }
    let _ = window.unmaximize();
    let Ok(Some(monitor)) = window.current_monitor() else {

        return;
    };
    let work = monitor.work_area();
    let area = Frame {
        x: work.position.x,
        y: work.position.y,
        width: work.size.width,
        height: work.size.height,
    };
    let (Ok(at), Ok(size)) = (window.outer_position(), window.outer_size()) else {

        return;
    };
    // The cap is a CSS measurement and the frame is in physical pixels, so it
    // only means the same thing on a 1x display until the scale is applied.
    let cap = (CANVAS_CSS_WIDTH * window.scale_factor().unwrap_or(1.0)).round() as u32;
    let next = compact_frame(
        area,
        Frame { x: at.x, y: at.y, width: size.width, height: size.height },
        cap,
    );
    let _ = window.set_size(PhysicalSize::new(next.width, next.height));
    let _ = window.set_position(PhysicalPosition::new(next.x, next.y));
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
    // One item, on the key macOS has always used for it. What is behind it is
    // two sections — an agent's config, and ssh's — and nothing it can save:
    // every section is text for a file this app does not own.
    let configure = MenuItem::with_id(app, "settings", "Settings…", true, Some("CmdOrCtrl+,"))?;
    // Our own sheet, not the platform's panel: that one takes a paragraph of
    // metadata and shows a version, and what this app is takes a little more
    // saying than that. In the order macOS has always used — About, then
    // settings, then the hide pair, then Quit.
    let about = MenuItem::with_id(app, "about", "About odoo-sheller", true, None::<&str>)?;
    let app_menu = Submenu::with_items(
        app,
        "odoo-sheller",
        true,
        &[
            &about,
            &PredefinedMenuItem::separator(app)?,
            &configure,
            &PredefinedMenuItem::separator(app)?,
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
    // Connect / Sessions / Journals, on the keys a browser moves between tabs
    // with. They belong in the menu rather than in the framed page: the page is
    // a remote origin in an iframe, so it sees a key only while it holds focus,
    // and the terminal below it holds focus half the time. macOS gives the menu
    // the key first whatever is focused, and the shell page passes it on.
    let prev_screen = MenuItem::with_id(
        app,
        "screen-prev",
        "Previous Screen",
        true,
        Some("Alt+Command+ArrowLeft"),
    )?;
    let next_screen = MenuItem::with_id(
        app,
        "screen-next",
        "Next Screen",
        true,
        Some("Alt+Command+ArrowRight"),
    )?;
    // The session tabs inside that screen, one modifier away from the screens
    // themselves: Ctrl+Shift rather than Option+Command.
    let prev_session = MenuItem::with_id(
        app,
        "session-prev",
        "Previous Session",
        true,
        Some("Control+Shift+ArrowLeft"),
    )?;
    let next_session = MenuItem::with_id(
        app,
        "session-next",
        "Next Session",
        true,
        Some("Control+Shift+ArrowRight"),
    )?;
    let view = Submenu::with_items(
        app,
        "View",
        true,
        &[
            &reload,
            &prev_screen,
            &next_screen,
            &prev_session,
            &next_session,
        ],
    )?;
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
    // Ctrl+Cmd, not Ctrl alone: Ctrl+Up and Ctrl+Down are Mission Control and
    // App Windows, and the system takes those before any app sees them. The
    // same modifiers as the two above, for the same dock.
    let taller = MenuItem::with_id(
        app,
        "terminal-taller",
        "Taller Terminal",
        true,
        Some("Control+Command+ArrowUp"),
    )?;
    let shorter = MenuItem::with_id(
        app,
        "terminal-shorter",
        "Shorter Terminal",
        true,
        Some("Control+Command+ArrowDown"),
    )?;
    // Ctrl+Cmd and an arrow is the dock's whole family: left and right move
    // between its tabs, up and down resize it. Option+Cmd+Arrow belongs to the
    // screens above. Closing a tab has no item at all: the page's own Cmd+W
    // does it, and as a menu item that key would be taken from the window even
    // with no terminal open, where Cmd+W has to keep meaning something else.
    let prev_tab = MenuItem::with_id(
        app,
        "terminal-prev",
        "Previous Terminal Tab",
        true,
        Some("Control+Command+ArrowLeft"),
    )?;
    let next_tab = MenuItem::with_id(
        app,
        "terminal-next",
        "Next Terminal Tab",
        true,
        Some("Control+Command+ArrowRight"),
    )?;
    // A column against one edge, for working beside an editor. macOS's own
    // Move & Resize does halves and quarters; this is the narrower stop it
    // does not offer, and it is here rather than in the page's header because
    // that header is a framed remote origin with no Tauri commands at all.
    let compact = MenuItem::with_id(
        app,
        "compact",
        "Compact Width",
        true,
        Some("Alt+Command+C"),
    )?;
    let window = Submenu::with_items(
        app,
        "Window",
        true,
        &[
            &PredefinedMenuItem::minimize(app, None)?,
            &PredefinedMenuItem::fullscreen(app, None)?,
            &compact,
            &PredefinedMenuItem::separator(app)?,
            &terminal,
            &new_tab,
            &prev_tab,
            &next_tab,
            &taller,
            &shorter,
        ],
    )?;
    let menu = Menu::with_items(app, &[&app_menu, &edit, &view, &window])?;
    app.set_menu(menu)?;
    // Naming a submenu "Window" is not what makes it the Window menu. AppKit
    // fills that one itself — the list of open windows, Bring All to Front,
    // and the Move & Resize items the system's own tiling shortcuts are bound
    // to — but only for the submenu it has been handed. Without this the
    // custom menu replaced all of that with nothing, and fn+Control+arrow
    // pressed in this app reached no menu item and did nothing at all.
    #[cfg(target_os = "macos")]
    window.set_as_windows_menu_for_nsapp()?;

    Ok(())
}

/// How an agent should start the MCP server, and the link that makes it short.
///
/// A bundled server gets a stable name in `~/.odoo-sheller/bin/` pointed at
/// it: the path into the bundle is 83 characters and is pasted by hand into as
/// many as four config files. Refreshed here, which is both at startup and
/// whenever the menu is opened — an entry written once must keep working after
/// the app is updated or moved, and it would not if the link only appeared
/// when somebody remembered to open a menu.
fn mcp_launch(app: &AppHandle) -> Result<mcp::Launch, String> {
    let resource = app.path().resource_dir().ok();
    let launch = daemon::resolve_mcp(resource.as_deref(), daemon::repo_root().as_deref())?;

    Ok(match launch.single_command() {
        Some(exe) => match daemon::refresh_mcp_link(&exe) {
            Some(link) => mcp::Launch::bundled(&link),
            None => launch,
        },
        None => launch,
    })
}

/// Drawn by the page, not by an alert: a block of config needs a monospace
/// column to keep its indentation and a button of its own to copy it, and a
/// native alert has neither. Nothing is copied on the way in — opening a
/// window is not asking for anything.
fn show_settings(app: &AppHandle) {
    let mcp = match mcp_launch(app) {
        Ok(launch) => mcp::section(&launch),
        // One half missing is not a reason to withhold the other.
        Err(reason) => mcp::unavailable(&reason),
    };
    focus_main(app);
    let _ = app.emit(
        "settings",
        configure::Settings {
            sections: vec![mcp, configure::ssh()],
        },
    );
}

/// The copy buttons in that dialog. `pbcopy` again rather than the webview's
/// clipboard API, so there is one way to the pasteboard and no permission
/// prompt in a window that already owns the menu bar.
#[tauri::command]
fn copy_text(text: String) -> Result<(), String> {
    configure::copy_to_clipboard(&text)
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
            read_admin_key,
            pty_create,
            pty_write,
            pty_resize,
            pty_close,
            shell_name,
            copy_text
        ])
        .setup(|app| {
            let handle = app.handle().clone();
            build_menu(&handle)?;
            app.on_menu_event(|app, event| match event.id().0.as_str() {
                "quit" => app.exit(0),
                "about" => {
                    focus_main(app);
                    // The address a browser would use, without the marker the
                    // frame carries: `?app=1` is between the window and the
                    // page, and means nothing to anyone reading this.
                    let _ = app.emit(
                        "about",
                        configure::about(&daemon::loopback_url(daemon::UI_PATH)),
                    );
                }
                "settings" => show_settings(app),
                "terminal" => tell_shell(app, "terminal-toggle"),
                "terminal-new-tab" => tell_shell(app, "terminal-new-tab"),
                "session-prev" => tell_shell(app, "session-prev"),
                "session-next" => tell_shell(app, "session-next"),
                "screen-prev" => tell_shell(app, "screen-prev"),
                "screen-next" => tell_shell(app, "screen-next"),
                "terminal-prev" => tell_shell(app, "terminal-prev"),
                "terminal-next" => tell_shell(app, "terminal-next"),
                "terminal-taller" => tell_shell(app, "terminal-taller"),
                "terminal-shorter" => tell_shell(app, "terminal-shorter"),
                "compact" => compact_window(app),
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

#[cfg(test)]
mod tests {
    use super::*;

    const SCREEN: Frame = Frame { x: 0, y: 25, width: 1920, height: 1055 };

    // Wider than any display in these tests, so the share is what decides
    // unless a test says otherwise.
    const NO_CAP: u32 = 100_000;

    #[test]
    fn a_compact_window_takes_the_left_edge_and_the_full_height() {
        let now = Frame { x: 320, y: 300, width: 1280, height: 800 };
        let next = compact_frame(SCREEN, now, NO_CAP);
        assert_eq!(next.x, 0);
        assert_eq!(next.y, 25);
        assert_eq!(next.height, 1055);
        assert_eq!(next.width, 730); // 38% of 1920, rounded
    }

    #[test]
    fn a_second_call_sends_it_to_the_other_edge() {
        let left = compact_frame(SCREEN, Frame { x: 320, y: 300, width: 1280, height: 800 }, NO_CAP);
        let right = compact_frame(SCREEN, left, NO_CAP);
        assert_eq!(right.width, left.width);
        assert_eq!(right.x, 1920 - left.width as i32);
        // And back, so one menu item reaches both sides.
        assert_eq!(compact_frame(SCREEN, right, NO_CAP), left);
    }

    #[test]
    fn a_window_that_is_merely_near_the_edge_is_not_treated_as_compact() {
        // Dragged roughly into place by hand: the point of the item is to
        // make that exact, not to bounce it across the screen.
        let sloppy = Frame { x: 4, y: 40, width: 1100, height: 900 };
        assert_eq!(compact_frame(SCREEN, sloppy, NO_CAP).x, 0);
    }

    #[test]
    fn a_few_pixels_of_drift_still_counts_as_already_compact() {
        // set_position and outer_position disagree by a pixel or two on a
        // scaled display; an exact comparison would break the toggle there.
        let left = compact_frame(SCREEN, Frame { x: 0, y: 25, width: 1280, height: 800 }, NO_CAP);
        let drifted = Frame { x: left.x + 2, y: left.y, width: left.width - 1, height: left.height };
        assert!(compact_frame(SCREEN, drifted, NO_CAP).x > 0, "should have gone right");
    }

    #[test]
    fn a_wide_display_stops_at_the_width_the_page_can_use() {
        // 38% of a 4K display is more than the page's canvas will ever fill,
        // so the rest of it would be the window's own empty margins.
        let uhd = Frame { x: 0, y: 25, width: 3840, height: 2135 };
        let next = compact_frame(uhd, Frame { x: 900, y: 300, width: 1600, height: 900 }, 1230);
        assert_eq!(next.width, 1230, "38% would have been 1459");
        assert_eq!(next.x, 0);
        assert_eq!(next.height, 2135, "the cap is on width alone");
    }

    #[test]
    fn a_narrow_display_is_still_decided_by_the_share() {
        let next = compact_frame(SCREEN, Frame { x: 320, y: 0, width: 1280, height: 800 }, 1230);
        assert_eq!(next.width, 730, "38% of 1920 is under the cap");
    }

    #[test]
    fn the_other_edge_is_measured_from_the_capped_width() {
        let uhd = Frame { x: 0, y: 25, width: 3840, height: 2135 };
        let left = compact_frame(uhd, Frame { x: 900, y: 300, width: 1600, height: 900 }, 1230);
        let right = compact_frame(uhd, left, 1230);
        assert_eq!(right.x, 3840 - 1230, "flush right, not 38% from the left");
        assert_eq!(compact_frame(uhd, right, 1230), left);
    }

    #[test]
    fn a_second_display_is_measured_by_its_own_work_area() {
        // work_area positions are global, so an external display to the right
        // of the built-in one starts at a non-zero x.
        let external = Frame { x: 1920, y: 0, width: 2560, height: 1440 };
        let next = compact_frame(external, Frame { x: 2000, y: 100, width: 1280, height: 800 }, NO_CAP);
        assert_eq!(next.x, 1920);
        assert_eq!(next.width, 973); // 38% of 2560, rounded
        assert_eq!(next.height, 1440);
    }
}
