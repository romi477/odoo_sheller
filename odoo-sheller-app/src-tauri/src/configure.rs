//! The dialogs behind **Configuration**, and the one rule they share: they
//! show, they never write.
//!
//! Both are text that belongs in a file this app does not own — an agent's
//! config, or `~/.ssh/config`. Merging into either means reformatting
//! somebody else's file, racing whatever else edits it, and guessing at
//! settings for tools that may not be installed. So the app renders the block,
//! puts it on the clipboard, and the person pastes it where they want it.

use std::io::Write;
use std::process::{Command, Stdio};

use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct Location {
    pub app: String,
    pub path: String,
}

/// One block of text to paste, with what it is for above it.
#[derive(Debug, Clone, Serialize)]
pub struct Block {
    pub label: String,
    pub hint: Option<String>,
    pub text: String,
}

/// One section of Settings. The prose lives here rather than in the markup
/// because the markup is one skeleton for all of them, and what differs
/// between sections is entirely words.
#[derive(Debug, Clone, Serialize)]
pub struct Section {
    /// Its tab in the dialog, and what the page keys the panel on.
    pub id: String,
    pub title: String,
    pub intro: String,
    pub blocks: Vec<Block>,
    /// A line under the blocks, when there is something more to say about them.
    pub note: Option<String>,
    pub locations_title: Option<String>,
    pub locations: Vec<Location>,
}

/// Everything under **Settings**. Nothing here is stored or applied: every
/// section is text for a file this app does not own, and the copy button on
/// each block is the only thing that leaves the window.
#[derive(Debug, Clone, Serialize)]
pub struct Settings {
    pub sections: Vec<Section>,
}

/// The About sheet: what this app is, in the same frame as Settings rather
/// than the platform's panel, which cannot say any of it.
#[derive(Debug, Clone, Serialize)]
pub struct About {
    pub name: String,
    pub version: String,
    pub tagline: String,
    pub paragraphs: Vec<String>,
    pub facts: Vec<Location>,
}

pub fn about(daemon_url: &str) -> About {
    About {
        name: "odoo-sheller".into(),
        version: env!("CARGO_PKG_VERSION").into(),
        tagline: "A persistent Odoo shell, and the window around it.".into(),
        paragraphs: vec![
            "One Odoo process stays loaded and answers command after command, \
             so a session costs its registry once instead of once per call. \
             The same session is reachable from this window, from a browser tab \
             on the daemon's port, and from an agent over MCP — one daemon \
             behind all three, and no second API."
                .into(),
            "Rollback is the default: nothing is written until a commit, and a \
             commit is always a deliberate act. The terminal below this window \
             is your own machine, not the Odoo shell."
                .into(),
        ],
        facts: vec![
            Location {
                app: "Daemon".into(),
                path: daemon_url.into(),
            },
            Location {
                app: "Source".into(),
                path: "github.com/romi477/odoo_sheller".into(),
            },
        ],
    }
}

/// What `ssh` needs to know before an odoo.sh build can be opened without a
/// terminal to answer to.
///
/// The daemon reaches a build with `ssh -T`. The first connection to a host it
/// has never seen asks whether the key is right, and there is nobody to ask:
/// the session dies on a question the UI never shows. `accept-new` answers it
/// once, for a host with no key on record, and keeps refusing a host whose key
/// has changed — which is the case that means something.
pub fn ssh() -> Section {
    Section {
        id: "ssh".into(),
        title: "SSH Configuration".into(),
        intro: "An odoo.sh build is reached over ssh, with no terminal behind \
                it. The first connection to a build asks whether its host key \
                is right, nobody is there to answer, and the session dies on \
                the question."
            .into(),
        blocks: vec![Block {
            label: "~/.ssh/config".into(),
            hint: Some("— odoo.sh builds".into()),
            text: "Host *.odoo.com\n    StrictHostKeyChecking accept-new\n".into(),
        }],
        note: Some(
            "`accept-new` takes the key of a host it has never seen and records \
             it; a host whose key has changed since is still refused, which is \
             the case worth refusing. The first connection is trusted without \
             being verified — that is the trade, and it is the same one a \
             person makes by typing yes at the prompt."
                .into(),
        ),
        locations_title: Some("Where it goes".into()),
        locations: vec![Location {
            app: "OpenSSH".into(),
            path: "~/.ssh/config  (create it if it is not there; chmod 600)".into(),
        }],
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

    #[test]
    fn the_ssh_block_is_what_openssh_reads() {
        let section = ssh();
        let block = &section.blocks[0];
        assert!(block.text.starts_with("Host *.odoo.com\n"));
        assert!(block.text.contains("    StrictHostKeyChecking accept-new"));
        // Indented, because OpenSSH reads a keyword as belonging to the Host
        // above it either way, and the indentation is what says so to a reader.
        assert!(block.text.ends_with('\n'), "a config file ends in a newline");
        assert_eq!(section.locations.len(), 1);
        assert!(section.locations[0].path.starts_with("~/.ssh/config"));
        // The trade is stated, not hidden behind the convenience.
        let note = section.note.clone().unwrap();
        assert!(note.contains("changed"));
        assert!(note.contains("without being verified"));
    }
}
