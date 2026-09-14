//! What to put in an agent's config, and nothing more.
//!
//! This module deliberately cannot write. Agent config files are the user's:
//! `~/.claude.json` is live state that Claude Code rewrites while it runs,
//! and the rest hold servers we know nothing about. Merging into them means
//! reformatting a file someone else owns, racing a process that is editing
//! it, and inventing configs for apps that are not installed. The app shows
//! the snippet and puts it on the clipboard; the person pastes it where they
//! want it.

use std::io::Write;
use std::path::Path;
use std::process::{Command, Stdio};

use serde::Serialize;
use serde_json::{Map, Value};

pub const SERVER_ID: &str = "odoo-sheller";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Launch {
    pub command: String,
    pub args: Vec<String>,
}

impl Launch {
    pub fn bundled(exe: &Path) -> Self {
        Self {
            command: exe.to_string_lossy().into_owned(),
            args: Vec::new(),
        }
    }

    pub fn from_checkout(uv: &Path, root: &Path) -> Self {
        Self {
            command: uv.to_string_lossy().into_owned(),
            args: vec![
                "--directory".into(),
                root.to_string_lossy().into_owned(),
                "run".into(),
                "python".into(),
                "-m".into(),
                "odoo_sheller.mcp".into(),
            ],
        }
    }

    /// The command when it is one executable and nothing else — a frozen
    /// server. The checkout form is `uv` plus arguments and has no such thing.
    pub fn single_command(&self) -> Option<std::path::PathBuf> {
        self.args
            .is_empty()
            .then(|| std::path::PathBuf::from(&self.command))
    }

    fn entry(&self) -> Value {
        serde_json::json!({
            "command": self.command,
            "args": self.args,
        })
    }

    /// The entry under its parent key. `mcpServers` for Claude Desktop,
    /// Claude Code and Cursor; Zed calls the same shape `context_servers`.
    pub fn snippet(&self, parent_key: &str) -> String {
        let mut servers = Map::new();
        servers.insert(SERVER_ID.into(), self.entry());
        let mut root = Map::new();
        root.insert(parent_key.into(), Value::Object(servers));

        serde_json::to_string_pretty(&Value::Object(root))
            .unwrap_or_else(|err| format!("could not render the snippet: {err}"))
    }
}

/// Where each agent keeps its config. Shown, never opened.
pub const LOCATIONS: &[(&str, &str)] = &[
    (
        "Claude Desktop",
        "~/Library/Application Support/Claude/claude_desktop_config.json",
    ),
    ("Claude Code", "~/.claude.json  (or .mcp.json in a project)"),
    ("Cursor", "~/.cursor/mcp.json"),
    ("Zed", "~/.config/zed/settings.json"),
];

#[derive(Debug, Clone, Serialize)]
pub struct Location {
    pub app: String,
    pub path: String,
}

/// Everything the page needs to draw the configuration. The prose around it
/// lives in the page: this is a native alert no longer, and an alert is the
/// one place a JSON snippet cannot be shown — proportional text swallows the
/// indentation, and there is nothing to press but OK.
#[derive(Debug, Clone, Serialize)]
pub struct Config {
    /// The entry under `mcpServers`: Claude Desktop, Claude Code, Cursor.
    pub mcp_servers: String,
    /// The same entry under the key Zed uses for it.
    pub context_servers: String,
    /// The command is the stable link beside the daemon's state rather than a
    /// path into the bundle, and the page says so.
    pub linked: bool,
    pub locations: Vec<Location>,
    /// Set when the snippet could not be put on the clipboard. Copying is the
    /// whole convenience, so a failure has to be visible — a dialog that
    /// claims the clipboard holds something it does not is worse than none.
    pub clipboard_error: Option<String>,
}

pub fn config(launch: &Launch) -> Config {
    Config {
        mcp_servers: launch.snippet("mcpServers"),
        context_servers: launch.snippet("context_servers"),
        linked: launch.args.is_empty() && launch.command.contains("/.odoo-sheller/bin/"),
        locations: LOCATIONS
            .iter()
            .map(|(app, path)| Location {
                app: (*app).into(),
                path: (*path).into(),
            })
            .collect(),
        clipboard_error: None,
    }
}

/// macOS only, like the rest of this app. `pbcopy` rather than a clipboard
/// plugin: one process, no new permission, and nothing to keep in sync.
pub fn copy_to_clipboard(text: &str) -> Result<(), String> {
    let mut child = Command::new("/usr/bin/pbcopy")
        .stdin(Stdio::piped())
        .spawn()
        .map_err(|err| err.to_string())?;
    child
        .stdin
        .as_mut()
        .ok_or_else(|| "pbcopy has no stdin".to_string())?
        .write_all(text.as_bytes())
        .map_err(|err| err.to_string())?;
    let status = child.wait().map_err(|err| err.to_string())?;
    if !status.success() {
        return Err(format!("pbcopy exited with {status}"));
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bundled() -> Launch {
        Launch::bundled(Path::new(
            "/Applications/odoo-sheller.app/Contents/Resources/odoo-sheller-mcp/odoo-sheller-mcp",
        ))
    }

    #[test]
    fn the_snippet_is_the_entry_under_its_parent_key() {
        let parsed: Value = serde_json::from_str(&bundled().snippet("mcpServers")).unwrap();
        let entry = &parsed["mcpServers"][SERVER_ID];
        assert!(entry["command"]
            .as_str()
            .unwrap()
            .ends_with("odoo-sheller-mcp"));
        assert_eq!(entry["args"], serde_json::json!([]));
        assert!(parsed.get("context_servers").is_none());
    }

    #[test]
    fn zed_gets_the_same_entry_under_its_own_key() {
        let parsed: Value = serde_json::from_str(&bundled().snippet("context_servers")).unwrap();
        assert_eq!(
            parsed["context_servers"][SERVER_ID],
            serde_json::from_str::<Value>(&bundled().snippet("mcpServers")).unwrap()["mcpServers"]
                [SERVER_ID],
        );
    }

    #[test]
    fn only_a_bare_executable_can_be_linked_to() {
        assert_eq!(
            bundled().single_command().unwrap(),
            Path::new(
                "/Applications/odoo-sheller.app/Contents/Resources/odoo-sheller-mcp/odoo-sheller-mcp"
            ),
        );
        let checkout = Launch::from_checkout(Path::new("/bin/uv"), Path::new("/repo"));
        assert!(checkout.single_command().is_none(), "uv plus args is not a path");
    }

    #[test]
    fn a_checkout_runs_the_module_through_uv() {
        let launch = Launch::from_checkout(Path::new("/opt/homebrew/bin/uv"), Path::new("/repo"));
        let parsed: Value = serde_json::from_str(&launch.snippet("mcpServers")).unwrap();
        let entry = &parsed["mcpServers"][SERVER_ID];
        assert_eq!(entry["command"], "/opt/homebrew/bin/uv");
        assert_eq!(
            entry["args"],
            serde_json::json!(["--directory", "/repo", "run", "python", "-m", "odoo_sheller.mcp"]),
        );
    }

    #[test]
    fn the_config_carries_both_snippets_and_every_location() {
        let config = config(&bundled());
        assert!(config.mcp_servers.contains("mcpServers"));
        assert!(config.context_servers.contains("context_servers"));
        assert_eq!(config.locations.len(), LOCATIONS.len());
        for (label, path) in LOCATIONS {
            let found = config
                .locations
                .iter()
                .find(|item| item.app == *label)
                .unwrap_or_else(|| panic!("{label} missing"));
            assert_eq!(found.path, *path);
        }
        assert!(config.clipboard_error.is_none());
    }

    #[test]
    fn only_the_linked_command_is_reported_as_a_link() {
        assert!(!config(&bundled()).linked, "a path into the bundle is not the link");
        let linked = Launch::bundled(Path::new("/Users/someone/.odoo-sheller/bin/odoo-sheller-mcp"));
        assert!(config(&linked).linked);
    }
}
