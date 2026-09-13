"""The claims in the docs that would be dangerous if they went stale.

Not a spellcheck. The CHANGELOG once carried "one session, one close" beside
"closes itself" for the same version, so the things pinned here are the ones a
reader would act on: what is guarded, and what tools exist.
"""

import json
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


def test_security_says_who_may_open_a_remote_target():
    text = read("security.md")
    assert "os_open_session" in text


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
    """Every entitlement is a hole in the hardened runtime, and this app needs none.

    Spawning docker and ssh needs no entitlement outside a sandbox, and a
    sandbox entitlement without `app-sandbox` reads as protection that is not
    there. Checked as a set rather than key by key: what matters is that
    nothing was granted, not which thing.
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
    for name in ("desktop-app/architecture.md", "desktop-app/implementation.md"):
        text = read(name)
        assert "Open Anyway" in text, name
        assert "right-click" not in text.lower(), name


def test_the_readme_api_table_lists_health():
    """The table reads as exhaustive; a missing /health row hides the liveness probe."""
    from odoo_sheller.api import create_app

    text = README.read_text(encoding="utf-8")
    paths = {getattr(route, "path", "") for route in create_app().routes}
    assert "/health" in paths
    assert "| `GET` | `/health` |" in text
