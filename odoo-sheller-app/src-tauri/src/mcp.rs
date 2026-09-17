//! What to put in an agent's config, and nothing more.
//!
//! This module deliberately cannot write. Agent config files are the user's:
//! `~/.claude.json` is live state that Claude Code rewrites while it runs,
//! and the rest hold servers we know nothing about. Merging into them means
//! reformatting a file someone else owns, racing a process that is editing
//! it, and inventing configs for apps that are not installed. The app shows
//! the snippet and puts it on the clipboard; the person pastes it where they
//! want it.

use std::path::Path;

use serde_json::{Map, Value};

use crate::configure::{Block, Location, Section};

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

/// The MCP section of Settings. An alert was the one place a JSON snippet
/// could not be shown — proportional text swallows the indentation, and there
/// is nothing to press but OK.
pub fn section(launch: &Launch) -> Section {
    let linked = launch.args.is_empty() && launch.command.contains("/.odoo-sheller/bin/");

    Section {
        id: "mcp".into(),
        title: "MCP".into(),
        intro: "Paste this into the agent's config yourself. Nothing here \
                writes to those files: they are yours, they hold other servers, \
                and one of them is rewritten by a running Claude Code."
            .into(),
        blocks: vec![
            Block {
                label: "Claude Desktop, Claude Code, Cursor".into(),
                hint: None,
                text: launch.snippet("mcpServers"),
            },
            Block {
                label: "Zed".into(),
                hint: Some("— the same entry, another parent key".into()),
                text: launch.snippet("context_servers"),
            },
        ],
        note: linked.then(|| {
            "That command is a link kept beside the daemon's own state and \
             pointed at the app that is installed now, so the entry survives an \
             update or a move."
                .into()
        }),
        locations_title: Some("Where they live".into()),
        locations: LOCATIONS
            .iter()
            .map(|(app, path)| Location {
                app: (*app).into(),
                path: (*path).into(),
            })
            .collect(),
    }
}

/// What Settings shows when the MCP server cannot be found at all: the section
/// still exists, and says why it is empty. SSH beside it is unaffected, and a
/// dialog that refuses to open over one broken half helps nobody.
pub fn unavailable(reason: &str) -> Section {
    Section {
        id: "mcp".into(),
        title: "MCP".into(),
        intro: format!("The MCP server could not be located: {reason}"),
        blocks: Vec::new(),
        note: None,
        locations_title: None,
        locations: Vec::new(),
    }
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
    fn the_section_carries_both_snippets_and_every_location() {
        let dialog = section(&bundled());
        assert!(dialog.blocks[0].text.contains("mcpServers"));
        assert!(dialog.blocks[1].text.contains("context_servers"));
        assert_eq!(dialog.locations.len(), LOCATIONS.len());
        for (label, path) in LOCATIONS {
            let found = dialog
                .locations
                .iter()
                .find(|item| item.app == *label)
                .unwrap_or_else(|| panic!("{label} missing"));
            assert_eq!(found.path, *path);
        }
    }

    #[test]
    fn only_the_linked_command_says_so() {
        assert!(section(&bundled()).note.is_none(), "a path into the bundle is not the link");
        let linked = Launch::bundled(Path::new("/Users/someone/.odoo-sheller/bin/odoo-sheller-mcp"));
        assert!(section(&linked).note.unwrap().contains("link"));
    }
}
