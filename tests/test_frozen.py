"""The frozen daemon: health, /web, and one real session.

This is the only test that catches bootstrap.py or web/ missing from the
bundle. It needs a PyInstaller build and a running Odoo container, so it is
not part of the unit suite.

    uv run pytest tests/test_frozen.py -v -m frozen

Override the binary with ODOO_SHELLER_FROZEN. The daemon is started on an
ephemeral port so a developer's 8765 is left alone.
"""

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx2 as httpx
import pytest

from odoo_sheller.discovery import SUPPORTED_MAJORS

CONTAINER = os.environ.get("PT_E2E_CONTAINER", "integra19")
# No pinned database. The container's own config names one, and a name pinned
# here goes stale the first time somebody renames a sandbox — which turns the
# one test that proves bootstrap.py shipped into a silent skip. Everything
# this file runs is a read.
DATABASE = os.environ.get("PT_E2E_DB")
ODOO_BIN = os.environ.get("PT_E2E_ODOO_BIN")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FROZEN = ROOT / "packaging" / "dist" / "odoo-sheller" / "odoo-sheller"

pytestmark = pytest.mark.frozen


def _binary() -> Path:
    override = os.environ.get("ODOO_SHELLER_FROZEN")
    path = Path(override) if override else DEFAULT_FROZEN
    if not path.is_file():
        pytest.skip(f"no frozen daemon at {path}")

    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))

        return sock.getsockname()[1]


def _wait_health(base: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base}/health", timeout=1.0)
            if response.status_code == 200:
                body = response.json()
                if body.get("ok") is True:

                    return body
        except httpx.HTTPError as exc:
            last = exc
        time.sleep(0.15)
    raise AssertionError(f"frozen daemon did not answer /health: {last}")


@pytest.fixture
def frozen_daemon():
    binary = _binary()
    port = _free_port()
    log = subprocess.PIPE
    proc = subprocess.Popen(
        [str(binary), "--port", str(port)],
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
    )
    base = f"http://127.0.0.1:{port}"
    try:
        health = _wait_health(base)
        yield base, health, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


def test_frozen_health_reports_the_real_version(frozen_daemon):
    _base, health, _proc = frozen_daemon
    assert health["version"] != "unknown"
    assert health["version"]


def test_frozen_web_includes_vendor(frozen_daemon):
    base, _health, _proc = frozen_daemon
    page = httpx.get(f"{base}/web", timeout=5.0)
    assert page.status_code == 200
    assert "<html" in page.text.lower()
    vendor = httpx.get(f"{base}/vendor/codemirror.min.js", timeout=5.0)
    assert vendor.status_code == 200
    assert len(vendor.content) > 10_000


def _target(base: str) -> dict:
    """Ask the frozen daemon what the container holds.

    Skipping belongs here and nowhere later: no container is a reason not to
    run, a container that refuses to open a session is a failure.
    """
    probed = httpx.post(f"{base}/api/probe", json={"container": CONTAINER}, timeout=60.0)
    if probed.status_code >= 400:
        pytest.skip(f"container {CONTAINER} not reachable: {probed.text[:200]}")
    report = probed.json()
    if not report.get("supported"):
        pytest.skip(f"container {CONTAINER} is not a supported Odoo: {report.get('error')}")

    return {
        "container": CONTAINER,
        "database": DATABASE or report["db_name"],
        "odoo_bin": ODOO_BIN or report["odoo_bin"],
    }


def test_frozen_daemon_opens_a_session_and_runs_a_command(frozen_daemon):
    """bootstrap.py shipped as data: a launch-only check would not catch this."""
    base, _health, _proc = frozen_daemon
    opened = httpx.post(f"{base}/api/sessions", json=_target(base), timeout=90.0)
    assert opened.status_code == 200, opened.text[:600]
    body = opened.json()
    session_id = body["id"]
    write_key = body["write_key"]
    major = str(body["odoo"]).split(".")[0]
    assert major.isdigit()
    assert int(major) in SUPPORTED_MAJORS
    try:
        executed = httpx.post(
            f"{base}/api/sessions/{session_id}/exec",
            json={"code": "len(env['res.partner'].search([], limit=1))"},
            headers={"X-OS-Session-Key": write_key},
            timeout=60.0,
        )
        assert executed.status_code == 200, executed.text
        result = executed.json()
        assert result.get("error") is None, result
        assert result.get("result") is not None
    finally:
        httpx.delete(
            f"{base}/api/sessions/{session_id}",
            headers={"X-OS-Session-Key": write_key},
            timeout=30.0,
        )
