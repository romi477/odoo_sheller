//! Resolve the docker CLI. A GUI app inherits a minimal PATH.

use std::path::{Path, PathBuf};

const KNOWN: &[&str] = &[
    "/usr/local/bin/docker",
    "/opt/homebrew/bin/docker",
];

/// Executable, not merely present. A directory named `docker` on the PATH, or
/// a non-executable leftover, would otherwise be handed to the daemon and fail
/// only on the first `docker ps`.
pub fn is_executable(path: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;

        return path
            .metadata()
            .map(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
            .unwrap_or(false);
    }
    #[cfg(not(unix))]
    {
        path.is_file()
    }
}

/// Where to look, in order. Pure so the order itself is testable: the
/// override has to win over a Docker Desktop install, and both over PATH.
pub fn candidates(
    override_path: Option<&str>,
    home: Option<&str>,
    path_var: Option<&str>,
) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Some(path) = override_path {
        out.push(PathBuf::from(path));
    }
    out.extend(KNOWN.iter().map(PathBuf::from));
    if let Some(home) = home {
        out.push(Path::new(home).join(".docker/bin/docker"));
    }
    if let Some(path_var) = path_var {
        out.extend(path_var.split(':').map(|dir| Path::new(dir).join("docker")));
    }

    out
}

pub fn resolve_docker() -> Option<PathBuf> {
    let override_path = std::env::var("ODOO_SHELLER_DOCKER").ok();
    let home = std::env::var("HOME").ok();
    let path_var = std::env::var("PATH").ok();

    candidates(
        override_path.as_deref(),
        home.as_deref(),
        path_var.as_deref(),
    )
    .into_iter()
    .find(|candidate| is_executable(candidate))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_override_is_tried_before_anything_else() {
        let found = candidates(Some("/custom/docker"), Some("/Users/x"), Some("/usr/bin"));
        assert_eq!(found.first().unwrap(), Path::new("/custom/docker"));
    }

    #[test]
    fn docker_desktop_and_homebrew_come_before_the_path() {
        let found = candidates(None, Some("/Users/x"), Some("/usr/bin"));
        let position = |needle: &str| {
            found
                .iter()
                .position(|path| path == Path::new(needle))
                .unwrap_or_else(|| panic!("{needle} not among the candidates"))
        };
        assert!(position("/usr/local/bin/docker") < position("/usr/bin/docker"));
        assert!(position("/opt/homebrew/bin/docker") < position("/usr/bin/docker"));
        assert!(position("/Users/x/.docker/bin/docker") < position("/usr/bin/docker"));
    }

    #[test]
    fn a_missing_home_and_path_leave_the_known_locations() {
        assert_eq!(
            candidates(None, None, None),
            KNOWN.iter().map(PathBuf::from).collect::<Vec<_>>(),
        );
    }

    #[test]
    fn a_directory_is_not_an_executable() {
        assert!(!is_executable(Path::new("/usr")));
    }
}
