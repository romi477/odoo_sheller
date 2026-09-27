"""The claims in the docs that would be dangerous if they went stale.

Not a spellcheck. The CHANGELOG once carried "one session, one close" beside
"closes itself" for the same version, so the things pinned here are the ones a
reader would act on: what is guarded, and what tools exist.
"""

import json
import re
from pathlib import Path

import pytest

from odoo_sheller import mcp as server

DOCS = Path(__file__).resolve().parent.parent / "docs"
README = Path(__file__).resolve().parent.parent / "README.md"


def read(name):

    return (DOCS / name).read_text(encoding="utf-8")


def test_security_no_longer_rests_on_local_docker_alone():
    """That sentence justified having no production guard and no masking. A
    remote target exists now, so the reasoning has to be restated, not kept."""
    text = read("security.md")
    assert "assumes local Docker only" not in text
    assert "odoo.sh" in text


def test_security_documents_the_production_refusal():
    """The doc used to say plainly that no such guard existed."""
    text = read("security.md")
    assert "There is no production-database guard" not in text
    assert "commit_forbidden" in text


def test_the_production_guard_the_docs_promise_is_real():
    """Pin the doc to the code, not to itself."""
    from odoo_sheller.session import CommitForbidden, CommitNotAllowed
    from odoo_sheller.transport import PRODUCTION

    assert PRODUCTION == "production"
    assert issubclass(CommitForbidden, CommitNotAllowed)


def test_security_says_the_desktop_app_reads_the_admin_key():
    """An endpoint that returned it would hand it to any localhost client.
    The window reading the file is the other channel, and it must not create
    a second key."""
    text = read("security.md")
    assert "never served by any API" in text
    assert "endpoint:" in text
    assert "desktop app" in text.lower()
    assert "never creates the file" in text
    assert "postMessage" in text
    assert "/health" in text


@pytest.mark.asyncio
async def test_the_agent_guide_lists_every_tool_that_exists():
    """A tool added without a row here is a tool nobody knows about."""
    table = read("agent-guide.md")
    for tool in await server.mcp.list_tools():
        assert f"`{tool.name}`" in table, tool.name


def test_the_readme_api_table_lists_every_session_route():
    """The table reads as exhaustive, so a missing row understates the API."""
    from odoo_sheller.api import create_app

    text = README.read_text(encoding="utf-8")
    app = create_app()
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/sessions/{session_id}/"):
            leaf = path.rsplit("/", 1)[-1]
            assert leaf in text, path


DESKTOP = Path(__file__).resolve().parent.parent / "odoo-sheller-app" / "src-tauri"


def test_desktop_entitlements_grant_nothing():
    """Every entitlement is a hole in the hardened runtime, and none is needed.

    The frozen daemon is the candidate that looks necessary and is not:
    PyInstaller ad-hoc signs its own tree, and the daemon is a separate
    process that does not inherit this app's hardened runtime. A bundle signed
    with an empty plist spawns it and serves. Checked as a set, so the test
    says "nothing was granted" rather than naming a key to argue about.
    """
    import plistlib

    granted = plistlib.loads((DESKTOP / "Entitlements.plist").read_bytes())
    assert granted == {}, f"the hardened runtime was widened: {sorted(granted)}"


def test_the_desktop_bundle_is_signed_with_the_hardened_runtime():
    """The one part of stage 3 a test can hold: codesign reads this config."""
    conf = json.loads((DESKTOP / "tauri.conf.json").read_text(encoding="utf-8"))
    macos = conf["bundle"]["macOS"]
    assert macos["hardenedRuntime"] is True
    assert macos["signingIdentity"] == "-"
    assert macos["entitlements"].endswith("Entitlements.plist")
    # The white flash before the first paint is a window setting, not CSS.
    window = conf["app"]["windows"][0]
    assert window["backgroundColor"] == "#04030e"


def test_the_desktop_docs_say_how_gatekeeper_is_cleared():
    """Control-click to open was removed in macOS 15. A stale instruction here
    is one a person follows and is refused by."""
    gestures = ("right-click → open", "right-click the app", "control-click → open")
    for name in ("desktop-app/architecture.md", "desktop-app/implementation.md"):
        text = read(name)
        assert "Open Anyway" in text, name
        lowered = text.lower()
        for gesture in gestures:
            # The gesture may be named — saying it stopped working is the
            # useful part. What must not happen is naming it as the way in.
            # So every mention has to sit next to its own retraction.
            at = lowered.find(gesture)
            while at != -1:
                window = lowered[at : at + len(gesture) + 60]
                assert "stopped" in window or "no longer" in window, f"{name}: {gesture}"
                at = lowered.find(gesture, at + 1)


def test_the_desktop_bundle_ships_the_frozen_onedir():
    """Map syntax, not a relative glob: `../` in an array lands under `_up_`."""
    conf = json.loads((DESKTOP / "tauri.conf.json").read_text(encoding="utf-8"))
    resources = conf["bundle"]["resources"]
    assert resources["../../packaging/dist/odoo-sheller"] == "odoo-sheller"
    assert resources["../../packaging/dist/odoo-sheller-mcp"] == "odoo-sheller-mcp"
    keep = DESKTOP.parent.parent / "packaging" / "dist" / "odoo-sheller" / ".gitkeep"
    assert keep.is_file(), "empty onedir dirs must exist so cargo test can compile"
    # Not a pinned string: the point is that the path resolves from the
    # directory tauri runs it in, which is the app dir, not src-tauri. The
    # previous spelling was two levels up and left every build at exit 127.
    command = conf["build"]["beforeBuildCommand"]
    assert command.startswith("sh ")
    script = (DESKTOP.parent / command.removeprefix("sh ")).resolve()
    assert script.is_file(), f"beforeBuildCommand does not resolve: {script}"
    assert script == (DESKTOP.parent.parent / "packaging" / "freeze.sh").resolve()
    freeze = (DESKTOP.parent.parent / "packaging" / "freeze.sh").read_text(encoding="utf-8")
    assert "pyinstaller" in freeze
    assert "odoo-sheller.spec" in freeze
    assert "odoo-sheller-mcp.spec" in freeze


def test_packaging_copies_metadata_and_bootstrap():
    """A freeze that forgets these launches, then fails on the first session
    or reports version unknown. The list lives in the repo so it is reviewed."""
    text = (DESKTOP.parent.parent / "packaging" / "bundle.py").read_text(encoding="utf-8")
    assert "copy_metadata" in text
    assert "odoo-sheller" in text
    assert "bootstrap.py" in text
    assert "collect_data_files" in text
    assert "collect_submodules" in text
    assert "uvicorn" in text


def test_the_desktop_docs_name_the_one_entitlement():
    """An empty-plist instruction here would ship a .app that dies at launch."""
    for name in ("desktop-app/architecture.md", "desktop-app/implementation.md"):
        text = read(name)
        assert "disable-library-validation" in text, name


def test_the_bundled_daemon_path_is_written_down():
    """A path found by poking around a bundle is a path that breaks next release."""
    for name in ("desktop-app/architecture.md", "desktop-app/implementation.md"):
        text = read(name)
        assert "Contents/Resources/odoo-sheller/odoo-sheller" in text, name
        assert "Contents/Resources/odoo-sheller-mcp/odoo-sheller-mcp" in text, name


def test_security_documents_the_desktop_terminal():
    """The terminal is the one expansion of the attack surface. If the section
    drifts into 'it is still only Odoo', the threat model is lying."""
    text = read("security.md")
    assert "## The desktop terminal" in text
    assert "$SHELL" in text
    assert "Rollback" in text or "rollback" in text


def test_xterm_is_vendored_in_the_app_not_the_daemon():
    """Putting xterm.js in odoo_sheller/web would load it on a remote origin
    with no Tauri IPC, which is why the shell page hosts it instead."""
    app_src = DESKTOP.parent / "src"
    assert (app_src / "vendor" / "xterm.min.js").stat().st_size > 10_000
    assert (app_src / "vendor" / "addon-fit.min.js").stat().st_size > 500
    assert "pty_resize" in (app_src / "shell.js").read_text(encoding="utf-8")
    web = DESKTOP.parent.parent / "odoo_sheller" / "web"
    assert not (web / "vendor" / "xterm.min.js").exists()


def test_the_window_frames_the_daemon_rather_than_navigating_to_it():
    """The terminal has to share the window with the UI, and only a local page
    can reach the Tauri commands. So the window stays on the shell page and
    the daemon's UI is framed — no copy of it in the app, nothing proxied, and
    the frame is a remote origin with no IPC of its own."""
    app_src = DESKTOP.parent / "src"
    markup = (app_src / "index.html").read_text(encoding="utf-8")
    assert "<iframe" in markup
    assert "dock" in markup, "the terminal is a panel in the same window"
    shell = (app_src / "shell.js").read_text(encoding="utf-8")
    # The fixed port has one definition, in daemon.rs; the page asks for it.
    assert "127.0.0.1:8765" not in shell
    assert 'invoke("ui_url")' in shell
    assert "fn ui_url()" in (DESKTOP / "src" / "lib.rs").read_text(encoding="utf-8")


def test_the_desktop_app_reads_the_admin_key_and_does_not_create_it():
    """The file is the daemon's. An invented copy would 403 against the one
    in memory. The frame has no Tauri IPC, so the value travels as a parent
    message — the same channel as screen steps, not an HTTP endpoint."""
    daemon = (DESKTOP / "src" / "daemon.rs").read_text(encoding="utf-8")
    lib = (DESKTOP / "src" / "lib.rs").read_text(encoding="utf-8")
    shell = (DESKTOP.parent / "src" / "shell.js").read_text(encoding="utf-8")
    assert "fn read_admin_key_at" in daemon
    assert "fs::read_to_string" in daemon
    assert 'join(".odoo-sheller/admin.key")' in daemon
    assert "fn read_admin_key()" in lib
    assert 'invoke("read_admin_key")' in shell
    assert 'type: "os-admin-key"' in shell
    assert "loadNativeAdminKey" in shell
    # After Ready, which is after /health, not on the splash.
    ready = shell.split('state.phase === "ready"')[1].split("return;")[0]
    assert "loadNativeAdminKey" in ready
    # A write in the reader would be the invented-key bug.
    reader = daemon.split("pub fn read_admin_key_at")[1].split("pub fn")[0]
    assert "fs::write" not in reader
    assert "create_dir" not in reader


def test_the_capability_covers_the_one_window_there_is():
    cap = json.loads((DESKTOP / "capabilities" / "default.json").read_text(encoding="utf-8"))
    assert cap["windows"] == ["main"]


def test_the_capability_grants_nothing_to_a_remote_origin():
    """The one line between the daemon's page and a shell.

    `main` loads `http://127.0.0.1:8765/web`, a remote origin. Tauri matches a
    command's context against the caller's origin — `Origin::matches` pairs
    Local with Local and Remote only with a declared URL pattern — so a
    capability without `remote` is what stops that page from calling
    `pty_create`. Adding one for any reason hands it a shell.
    """
    cap = json.loads((DESKTOP / "capabilities" / "default.json").read_text(encoding="utf-8"))
    assert "remote" not in cap, "a remote origin would reach pty_create"
    assert cap.get("local", True) is True


def test_the_terminal_never_reaches_the_web_ui():
    """Desktop only, by decision. The daemon's page is served to browsers and
    to a window with no IPC; a terminal there would be either impossible or a
    shell handed to whatever can reach port 8765."""
    web = DESKTOP.parent.parent / "odoo_sheller" / "web"
    for name in ("app.js", "index.html", "style.css"):
        text = (web / name).read_text(encoding="utf-8").lower()
        assert "xterm" not in text, name
        assert "pty_" not in text, name


def test_mcp_config_still_writes_nothing():
    """Stage 5 invariant: the snippet is pasted, never merged into a live file."""
    text = (DESKTOP / "src" / "mcp.rs").read_text(encoding="utf-8")
    assert "fs::write" not in text
    assert "fs::rename" not in text
    assert "OpenOptions" not in text


def test_the_readme_api_table_lists_health():
    """The table reads as exhaustive; a missing /health row hides the liveness probe."""
    from odoo_sheller.api import create_app

    text = README.read_text(encoding="utf-8")
    paths = {getattr(route, "path", "") for route in create_app().routes}
    assert "/health" in paths
    assert "| `GET` | `/health` |" in text


def test_the_readme_no_longer_says_the_daemon_cannot_be_containerized():
    """That line was a tested perimeter, not an architectural fact, and there
    is now an image that disproves it."""
    text = README.read_text(encoding="utf-8")
    assert "daemon is not containerized" not in text


def test_the_readme_publishes_the_port_to_loopback_only():
    """In this mode the published port is the entire security boundary. An
    example without the 127.0.0.1 prefix is an example that offers
    unauthenticated code execution to the network."""
    text = README.read_text(encoding="utf-8")
    assert "-p 127.0.0.1:8765:8765" in text
    assert "-p 8765:8765" not in text


def test_the_readme_requires_the_state_bind_mount():
    text = README.read_text(encoding="utf-8")
    assert "/data/.odoo-sheller" in text


def test_security_says_what_the_socket_mount_grants():
    """A container looks like isolation and provides none here."""
    text = read("security.md")
    assert "docker.sock" in text


def test_the_container_contract_documents_what_a_caller_codes_against():
    """A caller that has to read the Dockerfile to learn the label or the exit
    codes does not have a contract, it has an example."""
    text = read("container.md")
    assert "io.github.romi477.odoo-sheller" in text
    assert "State.Health.Status" in text
    assert "-p 127.0.0.1:8765:8765" in text


def test_the_container_contract_matches_the_entrypoint():
    """Pin the document to the script, not to itself."""
    entrypoint = (
        Path(__file__).resolve().parent.parent
        / "packaging" / "docker" / "entrypoint.sh"
    ).read_text(encoding="utf-8")
    text = read("container.md")
    for code in ("exit 1", "exit 2"):
        assert code in entrypoint
    assert "| 1 | No socket" in text
    assert "| 2 | The state directory does not exist" in text


def test_the_compact_width_cap_matches_the_page_it_caps():
    """`Window → Compact Width` stops where the page stops growing, and that
    number lives in the daemon's stylesheet while the window that obeys it is
    Rust. Nothing links them but this test: widen `.app` and the window would
    go on sizing itself to the old canvas, leaving a margin nobody asked for.
    """
    css = (Path(__file__).resolve().parent.parent
           / "odoo_sheller" / "web" / "style.css").read_text(encoding="utf-8")
    canvas = re.search(r"\.app \{[^}]*?width: min\((\d+)px", css, re.DOTALL)
    assert canvas is not None, "the canvas no longer declares a maximum width"
    padding = re.search(r"^body \{[^}]*?\n  padding: (\d+)px;", css, re.DOTALL | re.MULTILINE)
    assert padding is not None
    border = re.search(r"^\.shell \{[^}]*?\n  border: (\d+)px", css, re.DOTALL | re.MULTILINE)
    assert border is not None
    wanted = int(canvas.group(1)) + 2 * int(padding.group(1)) + 2 * int(border.group(1))

    lib = (DESKTOP / "src" / "lib.rs").read_text(encoding="utf-8")
    declared = re.search(r"const CANVAS_CSS_WIDTH: f64 = ([^;]+);", lib)
    assert declared is not None, "the window has no cap to compare"
    assert abs(eval(declared.group(1).replace("_", "")) - wanted) < 0.5, (  # noqa: S307
        f"the stylesheet says {wanted}px, lib.rs says {declared.group(1).strip()}"
    )
