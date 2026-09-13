//! Probe 8765, attach or spawn, never move the port.

use std::fs::{self, File};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde::Deserialize;

pub const PORT: u16 = 8765;
pub const UI_PATH: &str = "/web";

const HEALTH_PATH: &str = "/health";
const SESSIONS_PATH: &str = "/api/sessions";
const PROBE_TIMEOUT: Duration = Duration::from_millis(400);
const READY_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PortState {
    Free,
    OurDaemon,
    Occupied,
}

#[derive(Debug, Deserialize)]
struct Health {
    ok: bool,
    #[serde(default)]
    version: Option<String>,
}

/// What the port turned out to hold. The version is a courtesy: a daemon old
/// enough to have no `/health` still serves the UI this app navigates to, so
/// there is deliberately no version gate — see `docs/desktop-app/architecture.md`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Probe {
    pub state: PortState,
    pub version: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct Session {
    #[serde(default)]
    pub pending_commands: u32,
}

pub fn loopback_url(path: &str) -> String {
    format!("http://127.0.0.1:{PORT}{path}")
}

pub fn ui_url() -> String {
    loopback_url(UI_PATH)
}

fn parse_health(status: Option<u16>, body: Option<&str>) -> Option<Health> {
    if status != Some(200) {
        return None;
    }

    serde_json::from_str::<Health>(body?)
        .ok()
        .filter(|health| health.ok)
}

/// Fingerprint an HTTP pair. `tcp_open` is the last word when neither
/// body looks like ours: something is listening, or nothing is.
pub fn classify(
    health_status: Option<u16>,
    health_body: Option<&str>,
    sessions_status: Option<u16>,
    sessions_body: Option<&str>,
    tcp_open: bool,
) -> PortState {
    if parse_health(health_status, health_body).is_some() {
        return PortState::OurDaemon;
    }
    if sessions_status == Some(200) {
        if let Some(body) = sessions_body {
            if serde_json::from_str::<Vec<serde_json::Value>>(body).is_ok() {
                return PortState::OurDaemon;
            }
        }
    }
    if tcp_open {
        PortState::Occupied
    } else {
        PortState::Free
    }
}

fn get(path: &str) -> Result<(u16, String), ureq::Error> {
    let response = ureq::get(&loopback_url(path))
        .timeout(PROBE_TIMEOUT)
        .call()?;
    let status = response.status();
    let body = response.into_string().unwrap_or_default();

    Ok((status, body))
}

fn tcp_open() -> bool {
    TcpStream::connect_timeout(
        &([127, 0, 0, 1], PORT).into(),
        Duration::from_millis(150),
    )
    .is_ok()
}

pub fn probe() -> Probe {
    let health = get(HEALTH_PATH).ok();
    let health_status = health.as_ref().map(|(status, _)| *status);
    let health_body = health.as_ref().map(|(_, body)| body.as_str());
    // `/health` answering settles it. Asking anything further would double
    // every request of the readiness poll for an answer we already have.
    if let Some(health) = parse_health(health_status, health_body) {

        return Probe {
            state: PortState::OurDaemon,
            version: health.version,
        };
    }
    let sessions = get(SESSIONS_PATH).ok();

    Probe {
        state: classify(
            health_status,
            health_body,
            sessions.as_ref().map(|(status, _)| *status),
            sessions.as_ref().map(|(_, body)| body.as_str()),
            tcp_open(),
        ),
        version: None,
    }
}

pub fn list_sessions() -> Result<Vec<Session>, String> {
    let (status, body) = get(SESSIONS_PATH).map_err(|err| err.to_string())?;
    if status != 200 {
        return Err(format!("GET /api/sessions returned {status}"));
    }

    serde_json::from_str(&body).map_err(|err| err.to_string())
}

pub fn wait_until_ready() -> Result<Option<String>, String> {
    let deadline = Instant::now() + READY_TIMEOUT;
    while Instant::now() < deadline {
        let probe = probe();
        if probe.state == PortState::OurDaemon {
            return Ok(probe.version);
        }
        std::thread::sleep(Duration::from_millis(150));
    }

    Err(format!(
        "daemon did not answer on 127.0.0.1:{PORT} within {}s",
        READY_TIMEOUT.as_secs()
    ))
}

pub fn quit_message(session_count: usize, pending: u32) -> String {
    format!(
        "{session_count} live session(s). {pending} command(s) are uncommitted. \
         Uncommitted work will be discarded."
    )
}

pub fn occupied_message() -> String {
    format!(
        "Port {PORT} is in use, but it is not odoo-sheller. \
         The app will not start and will not move to another port."
    )
}

pub fn log_dir() -> PathBuf {
    home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("Library/Logs/odoo-sheller")
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME").map(PathBuf::from)
}

pub fn repo_root() -> Option<PathBuf> {
    if let Ok(root) = std::env::var("ODOO_SHELLER_ROOT") {
        return Some(PathBuf::from(root));
    }
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe.parent()?.to_path_buf();
        for _ in 0..8 {
            if dir.join("pyproject.toml").is_file() {
                return Some(dir);
            }
            if !dir.pop() {
                break;
            }
        }
    }
    // Deliberately no compile-time fallback: `CARGO_MANIFEST_DIR` would bake
    // the build machine's path into the binary and "work" everywhere the
    // developer looks. Outside a checkout this has to fail out loud.
    None
}

fn resolve_tool(name: &str, extra: &[&str]) -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = extra.iter().map(PathBuf::from).collect();
    if let Some(home) = home_dir() {
        candidates.push(home.join(".local/bin").join(name));
    }
    if let Ok(path) = std::env::var("PATH") {
        candidates.extend(path.split(':').map(|dir| Path::new(dir).join(name)));
    }

    candidates
        .into_iter()
        .find(|candidate| crate::docker::is_executable(candidate))
}

pub fn resolve_uv() -> Option<PathBuf> {
    resolve_tool(
        "uv",
        &[
            "/opt/homebrew/bin/uv",
            "/usr/local/bin/uv",
        ],
    )
}

pub fn spawn_daemon(repo_root: &Path, docker: Option<&Path>) -> Result<Child, String> {
    let uv = resolve_uv().ok_or_else(|| {
        "uv not found. Install it, or run the daemon yourself: uv run python -m odoo_sheller"
            .to_string()
    })?;
    let logs = log_dir();
    fs::create_dir_all(&logs).map_err(|err| err.to_string())?;
    let log = File::create(logs.join("daemon.log")).map_err(|err| err.to_string())?;
    let mut command = Command::new(uv);
    command
        .args(["run", "python", "-m", "odoo_sheller"])
        .current_dir(repo_root)
        .stdin(Stdio::null())
        .stdout(log.try_clone().map_err(|err| err.to_string())?)
        .stderr(log);
    if let Some(docker) = docker {
        command.env("ODOO_SHELLER_DOCKER", docker);
    }

    command.spawn().map_err(|err| err.to_string())
}

/// Ask the daemon to stop, then insist.
///
/// `SIGTERM` is what uvicorn turns into a graceful shutdown, and the shutdown
/// is what closes the live sessions and writes `session_close` to their
/// journals. Going straight to `SIGKILL` would leave every transcript ending
/// mid-sentence. The wait is bounded because a wedged daemon must not be able
/// to hold the app open.
pub fn stop_child(child: &mut Child, grace: Duration) {
    #[cfg(unix)]
    {
        // SAFETY: `kill(2)` with a pid we own and a signal number. The child
        // is still in our process table — `wait` has not been called yet — so
        // the pid cannot have been reused.
        unsafe {
            libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
        }
        let deadline = Instant::now() + grace;
        while Instant::now() < deadline {
            match child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) => std::thread::sleep(Duration::from_millis(100)),
                Err(_) => break,
            }
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn health_ok_is_ours() {
        assert_eq!(
            classify(
                Some(200),
                Some(r#"{"ok":true,"version":"1.5.0"}"#),
                None,
                None,
                true,
            ),
            PortState::OurDaemon,
        );
    }

    #[test]
    fn health_ok_false_is_not_ours() {
        assert_eq!(
            classify(Some(200), Some(r#"{"ok":false}"#), None, None, true),
            PortState::Occupied,
        );
    }

    #[test]
    fn sessions_array_identifies_an_older_daemon() {
        // A daemon from before `/health`: the request errors out rather than
        // returning 404, so the health pair arrives as `None`.
        assert_eq!(
            classify(None, None, Some(200), Some("[]"), true),
            PortState::OurDaemon,
        );
        assert_eq!(
            classify(
                None,
                None,
                Some(200),
                Some(r#"[{"id":"s1","pending_commands":2}]"#),
                true,
            ),
            PortState::OurDaemon,
        );
    }

    #[test]
    fn something_else_on_the_port_is_occupied() {
        assert_eq!(
            classify(Some(200), Some("nginx"), Some(404), Some("nope"), true),
            PortState::Occupied,
        );
    }

    #[test]
    fn nothing_listening_is_free() {
        assert_eq!(classify(None, None, None, None, false), PortState::Free);
    }

    #[test]
    fn quit_copy_names_sessions_and_pending() {
        let text = quit_message(2, 3);
        assert!(text.contains("2 live session"));
        assert!(text.contains("3 command"));
        assert!(text.contains("Uncommitted work will be discarded"));
    }

    #[test]
    fn occupied_copy_names_the_port() {
        assert!(occupied_message().contains("8765"));
    }

    #[test]
    fn the_health_version_travels_with_the_verdict() {
        let health = parse_health(Some(200), Some(r#"{"ok":true,"version":"1.6.0"}"#));
        assert_eq!(health.unwrap().version.as_deref(), Some("1.6.0"));
        assert!(parse_health(Some(200), Some(r#"{"ok":false}"#)).is_none());
        assert!(parse_health(None, None).is_none());
    }

    #[test]
    fn ui_stays_on_the_fixed_port() {
        assert_eq!(ui_url(), "http://127.0.0.1:8765/web");
    }
}
