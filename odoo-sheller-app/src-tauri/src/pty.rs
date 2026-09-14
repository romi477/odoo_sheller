//! One PTY and one `$SHELL -l` per terminal tab.

use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use portable_pty::{CommandBuilder, NativePtySystem, PtySize, PtySystem};

/// Batch PTY output so a 50 MB `cat` is tens of events, not tens of millions.
pub const FLUSH_BYTES: usize = 32 * 1024;
pub const FLUSH_WAIT: Duration = Duration::from_millis(16);

pub fn shell_argv() -> (PathBuf, Vec<String>) {
    let shell = std::env::var_os("SHELL").map(PathBuf::from).unwrap_or_else(|| {
        PathBuf::from("/bin/zsh")
    });

    (shell, vec!["-l".into()])
}

/// What to call the tab: `zsh`, `bash`, whatever `$SHELL` turned out to be.
/// The tab says the shell's own name rather than a hardcoded one, because the
/// shell is the user's choice and the label would otherwise lie on any machine
/// that chose differently.
pub fn shell_name() -> String {
    let (shell, _) = shell_argv();

    shell
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| "shell".to_string())
}

pub struct OutputBatcher {
    buf: Vec<u8>,
    deadline: Option<Instant>,
}

impl OutputBatcher {
    pub fn new() -> Self {
        Self {
            buf: Vec::new(),
            deadline: None,
        }
    }

    /// Push bytes. Returns a batch when the size cap is hit.
    pub fn push(&mut self, chunk: &[u8]) -> Option<Vec<u8>> {
        self.buf.extend_from_slice(chunk);
        if self.buf.len() >= FLUSH_BYTES {
            self.deadline = None;

            return Some(self.take());
        }
        if self.deadline.is_none() {
            self.deadline = Some(Instant::now() + FLUSH_WAIT);
        }

        None
    }

    pub fn wait(&self) -> Duration {
        match self.deadline {
            Some(at) => at.saturating_duration_since(Instant::now()).max(Duration::from_millis(1)),
            None => Duration::from_secs(60),
        }
    }

    pub fn due(&self) -> bool {
        self.deadline
            .is_some_and(|at| Instant::now() >= at && !self.buf.is_empty())
    }

    pub fn take(&mut self) -> Vec<u8> {
        self.deadline = None;
        std::mem::take(&mut self.buf)
    }

    pub fn is_empty(&self) -> bool {
        self.buf.is_empty()
    }
}

pub type OnOutput = Arc<dyn Fn(&str, &[u8]) + Send + Sync>;
pub type OnExit = Arc<dyn Fn(&str) + Send + Sync>;

struct Session {
    writer: Mutex<Box<dyn Write + Send>>,
    master: Mutex<Box<dyn portable_pty::MasterPty + Send>>,
    child: Mutex<Box<dyn portable_pty::Child + Send + Sync>>,
}

#[derive(Default)]
pub struct PtyHub {
    sessions: Mutex<HashMap<String, Session>>,
    next: AtomicU64,
}

impl PtyHub {
    pub fn create(&self, on_output: OnOutput, on_exit: OnExit) -> Result<String, String> {
        let (shell, args) = shell_argv();

        self.create_with(&shell, &args, PtySize { rows: 24, cols: 80, pixel_width: 0, pixel_height: 0 }, on_output, on_exit)
    }

    pub fn create_with(
        &self,
        program: &Path,
        args: &[String],
        size: PtySize,
        on_output: OnOutput,
        on_exit: OnExit,
    ) -> Result<String, String> {
        let pair = NativePtySystem::default()
            .openpty(size)
            .map_err(|err| err.to_string())?;
        let mut cmd = CommandBuilder::new(program);
        for arg in args {
            cmd.arg(arg);
        }
        if let Some(home) = std::env::var_os("HOME") {
            cmd.cwd(home);
        }
        cmd.env("TERM", "xterm-256color");
        cmd.env("COLORTERM", "truecolor");
        let child = pair
            .slave
            .spawn_command(cmd)
            .map_err(|err| err.to_string())?;
        let reader = pair
            .master
            .try_clone_reader()
            .map_err(|err| err.to_string())?;
        let writer = pair
            .master
            .take_writer()
            .map_err(|err| err.to_string())?;
        let id = format!("t{}", self.next.fetch_add(1, Ordering::Relaxed) + 1);
        {
            let mut sessions = self.sessions.lock().expect("pty lock");
            sessions.insert(
                id.clone(),
                Session {
                    writer: Mutex::new(writer),
                    master: Mutex::new(pair.master),
                    child: Mutex::new(child),
                },
            );
        }
        self.pump(id.clone(), reader, on_output, on_exit);

        Ok(id)
    }

    fn pump(
        &self,
        id: String,
        mut reader: Box<dyn Read + Send>,
        on_output: OnOutput,
        on_exit: OnExit,
    ) {
        let (tx, rx) = mpsc::channel::<Option<Vec<u8>>>();
        std::thread::spawn(move || {
            let mut buf = vec![0u8; 8192];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) | Err(_) => {
                        let _ = tx.send(None);
                        break;
                    }
                    Ok(n) => {
                        if tx.send(Some(buf[..n].to_vec())).is_err() {
                            break;
                        }
                    }
                }
            }
        });
        let id_for_flush = id.clone();
        std::thread::spawn(move || {
            let mut batch = OutputBatcher::new();
            loop {
                match rx.recv_timeout(batch.wait()) {
                    Ok(Some(chunk)) => {
                        if let Some(full) = batch.push(&chunk) {
                            on_output(&id_for_flush, &full);
                        }
                    }
                    Ok(None) => {
                        if !batch.is_empty() {
                            on_output(&id_for_flush, &batch.take());
                        }
                        on_exit(&id_for_flush);
                        break;
                    }
                    Err(RecvTimeoutError::Timeout) => {
                        if batch.due() {
                            on_output(&id_for_flush, &batch.take());
                        }
                    }
                    Err(RecvTimeoutError::Disconnected) => {
                        if !batch.is_empty() {
                            on_output(&id_for_flush, &batch.take());
                        }
                        on_exit(&id_for_flush);
                        break;
                    }
                }
            }
        });
    }

    pub fn write(&self, id: &str, data: &[u8]) -> Result<(), String> {
        let sessions = self.sessions.lock().expect("pty lock");
        let session = sessions.get(id).ok_or_else(|| format!("no terminal {id}"))?;
        let mut writer = session.writer.lock().expect("pty writer");
        writer.write_all(data).map_err(|err| err.to_string())?;
        writer.flush().map_err(|err| err.to_string())?;

        Ok(())
    }

    pub fn resize(&self, id: &str, cols: u16, rows: u16) -> Result<(), String> {
        let sessions = self.sessions.lock().expect("pty lock");
        let session = sessions.get(id).ok_or_else(|| format!("no terminal {id}"))?;
        session
            .master
            .lock()
            .expect("pty master")
            .resize(PtySize {
                cols,
                rows,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|err| err.to_string())?;

        Ok(())
    }

    pub fn close(&self, id: &str) {
        let Some(session) = self.sessions.lock().expect("pty lock").remove(id) else {
            return;
        };
        reap(session);
    }

    pub fn close_all(&self) {
        let sessions: HashMap<_, _> = std::mem::take(&mut *self.sessions.lock().expect("pty lock"));
        for (_, session) in sessions {
            reap(session);
        }
    }

    pub fn count(&self) -> usize {
        self.sessions.lock().expect("pty lock").len()
    }
}

fn reap(session: Session) {
    if let Ok(mut child) = session.child.lock() {
        let _ = child.kill();
        let _ = child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    #[test]
    fn the_shell_is_the_users_and_a_login_shell() {
        let (shell, args) = shell_argv();
        assert!(args == ["-l"]);
        if let Some(from_env) = std::env::var_os("SHELL") {
            assert_eq!(shell, PathBuf::from(from_env));
        }
    }

    #[test]
    fn the_tab_is_named_after_the_shell_itself() {
        let name = shell_name();
        assert!(!name.is_empty());
        assert!(!name.contains('/'), "a basename, not a path: {name}");
        let (shell, _) = shell_argv();
        assert!(shell.to_string_lossy().ends_with(&name));
    }

    #[test]
    fn a_large_push_flushes_in_one_batch() {
        let mut batch = OutputBatcher::new();
        let chunk = vec![b'x'; FLUSH_BYTES];
        let flushed = batch.push(&chunk).expect("size cap");
        assert_eq!(flushed.len(), FLUSH_BYTES);
        assert!(batch.is_empty());
    }

    #[test]
    fn small_writes_wait_for_the_deadline() {
        let mut batch = OutputBatcher::new();
        assert!(batch.push(b"hi").is_none());
        assert!(!batch.is_empty());
        assert!(!batch.due());
        std::thread::sleep(FLUSH_WAIT + Duration::from_millis(5));
        assert!(batch.due());
        assert_eq!(batch.take(), b"hi");
    }

    #[test]
    fn a_real_shell_echoes_and_close_reaps() {
        let hub = PtyHub::default();
        let output = Arc::new(Mutex::new(Vec::<u8>::new()));
        let captured = output.clone();
        let id = hub
            .create_with(
                Path::new("/bin/sh"),
                &[],
                PtySize {
                    rows: 24,
                    cols: 80,
                    pixel_width: 0,
                    pixel_height: 0,
                },
                Arc::new(move |_id, bytes| captured.lock().unwrap().extend_from_slice(bytes)),
                Arc::new(move |_id| {}),
            )
            .expect("spawn sh");
        assert_eq!(hub.count(), 1);
        hub.write(&id, b"printf 'pt-pty-marker\\n'; exit\n")
            .expect("write");
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            let captured = output.lock().unwrap().clone();
            let text = String::from_utf8_lossy(&captured);
            if text.contains("pt-pty-marker") {
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        let captured = output.lock().unwrap().clone();
        let text = String::from_utf8_lossy(&captured);
        assert!(text.contains("pt-pty-marker"), "got: {text:?}");
        hub.close(&id);
        assert_eq!(hub.count(), 0);
    }

    #[test]
    fn close_all_reaps_every_tab() {
        let hub = PtyHub::default();
        let noop_out: OnOutput = Arc::new(|_, _| {});
        let noop_exit: OnExit = Arc::new(|_| {});
        hub.create_with(
            Path::new("/bin/sh"),
            &[],
            PtySize {
                rows: 24,
                cols: 80,
                pixel_width: 0,
                pixel_height: 0,
            },
            noop_out.clone(),
            noop_exit.clone(),
        )
        .unwrap();
        hub.create_with(
            Path::new("/bin/sh"),
            &[],
            PtySize {
                rows: 24,
                cols: 80,
                pixel_width: 0,
                pixel_height: 0,
            },
            noop_out,
            noop_exit,
        )
        .unwrap();
        assert_eq!(hub.count(), 2);
        hub.close_all();
        assert_eq!(hub.count(), 0);
    }
}
