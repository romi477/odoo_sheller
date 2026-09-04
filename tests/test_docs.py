"""The claims in the docs that would be dangerous if they went stale.

Not a spellcheck. The CHANGELOG once carried "one session, one close" beside
"closes itself" for the same version, so the things pinned here are the ones a
reader would act on: what is guarded, and what tools exist.
"""

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
