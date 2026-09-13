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

pub fn instructions(launch: &Launch) -> String {
    let mut text = String::from(
        "Paste this into the agent's config yourself. Nothing here writes to \
         those files: they are yours, they hold other servers, and one of them \
         is rewritten by a running Claude Code.\n\n\
         Claude Desktop, Claude Code, Cursor:\n\n",
    );
    text.push_str(&launch.snippet("mcpServers"));
    text.push_str("\n\nZed — same entry, different parent key:\n\n");
    text.push_str(&launch.snippet("context_servers"));
    text.push_str("\n\nWhere they live:\n");
    for (label, path) in LOCATIONS {
        text.push_str(&format!("  {label}: {path}\n"));
    }
    text.push_str("\nThe mcpServers form is on the clipboard.");

    text
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
    fn the_instructions_name_every_config_and_say_nothing_is_written() {
        let text = instructions(&bundled());
        for (label, path) in LOCATIONS {
            assert!(text.contains(label), "{label} missing");
            assert!(text.contains(path), "{path} missing");
        }
        assert!(text.contains("Nothing here writes"));
        assert!(text.contains("mcpServers"));
        assert!(text.contains("context_servers"));
    }
}
