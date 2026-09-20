"""The parts of the Dockerfile that other things depend on.

Not a lint. Each of these is load-bearing somewhere else: HOME is how the
journals, the admin key and the SSH control sockets all relocate without a
code change; the venv on PATH is what makes the MCP escalation path work from
a bare `docker exec`; the in-container flag is what stops the daemon printing
a warning that is false here.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "packaging" / "docker" / "Dockerfile").read_text(encoding="utf-8")


def test_home_relocates_the_state_directory():
    """journal.py, registry.py and transport.py all build paths from
    Path.home(), which reads HOME. Setting it moves all three at once."""
    assert "HOME=/data" in DOCKERFILE
    assert "/root/.odoo-sheller" not in DOCKERFILE


def test_the_venv_leads_the_path():
    """`docker exec -i odoo-sheller python -m odoo_sheller.mcp` is the
    documented way a human reaches a stuck module. It needs the synced
    environment, not the system interpreter."""
    assert "PATH=/app/.venv/bin:$PATH" in DOCKERFILE


def test_the_daemon_knows_it_is_containerized():
    from odoo_sheller.__main__ import IN_CONTAINER_ENV

    assert f"{IN_CONTAINER_ENV}=1" in DOCKERFILE


def test_dependencies_come_from_the_lockfile():
    assert "uv sync --frozen --no-dev" in DOCKERFILE
    assert "pip install" not in DOCKERFILE


def test_external_artifacts_are_pinned():
    """A floating tag turns a rebuild of an old commit into a different
    image."""
    assert "ARG UV_VERSION=" in DOCKERFILE
    assert "ARG DOCKER_VERSION=" in DOCKERFILE
    assert "uv:latest" not in DOCKERFILE
    assert "stable/latest" not in DOCKERFILE


def test_the_entrypoint_is_the_script_we_test():
    assert "entrypoint.sh" in DOCKERFILE
    assert 'ENTRYPOINT ["/usr/local/bin/odoo-sheller-entrypoint"]' in DOCKERFILE


def test_the_healthcheck_reads_the_port_it_probes():
    """The entrypoint honours ODOO_SHELLER_PORT. A healthcheck that assumed
    8765 would call a working container unhealthy forever."""
    assert "ODOO_SHELLER_PORT" in DOCKERFILE
    assert "127.0.0.1:8765/api/sessions" not in DOCKERFILE
