"""The daemon in a container, driving a container beside it.

What is actually under test is the bind mount. Everything else here — open,
exec, rollback, close — is already covered against a native daemon; the reason
to pay for a container is that the journal has to land in the host's
~/.odoo-sheller, owned by the host user, or the history forks by how the
daemon happened to be started that day.

Build the image first:
    docker build -f packaging/docker/Dockerfile -t odoo-sheller:dev .

Run with:
    uv run pytest tests/test_container.py -v -m container

Override the target with PT_E2E_CONTAINER, PT_E2E_DB, PT_E2E_ODOO_BIN, and
the image with PT_CONTAINER_IMAGE.
"""

import json
import os
import socket
import subprocess
import threading
import time

import httpx2 as httpx
import pytest

IMAGE = os.environ.get("PT_CONTAINER_IMAGE", "odoo-sheller:dev")
CONTAINER = os.environ.get("PT_E2E_CONTAINER", "integra19")
DATABASE = os.environ.get("PT_E2E_DB", "integra_db_19_presta")
ODOO_BIN = os.environ.get("PT_E2E_ODOO_BIN", "/opt/odoo/odoo-bin")
HOST_SOCKET = os.environ.get("PT_CONTAINER_SOCKET", "/var/run/docker.sock")

pytestmark = pytest.mark.container


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))

        return probe.getsockname()[1]


@pytest.fixture
def daemon(tmp_path):
    """A containerized daemon with its state bind-mounted into tmp_path."""
    state = tmp_path / ".odoo-sheller"
    state.mkdir()
    port = free_port()
    name = f"odoo-sheller-test-{port}"
    started = subprocess.run(
        [
            "docker", "run", "-d", "--name", name,
            "-v", f"{HOST_SOCKET}:/var/run/docker.sock",
            "-v", f"{state}:/data/.odoo-sheller",
            "-e", f"ODOO_SHELLER_UID={os.getuid()}",
            "-e", f"ODOO_SHELLER_GID={os.getgid()}",
            "-p", f"127.0.0.1:{port}:8765",
            IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if httpx.get(f"{base}/api/sessions", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(1)
        else:
            logs = subprocess.run(
                ["docker", "logs", name], capture_output=True, text=True, check=False
            )
            raise AssertionError(f"daemon never answered:\n{logs.stdout}\n{logs.stderr}")

        yield {"base": base, "name": name, "state": state}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def open_session(daemon):
    """Open one, and return the body plus the header every write needs."""
    opened = httpx.post(
        f"{daemon['base']}/api/sessions",
        json={"container": CONTAINER, "database": DATABASE, "odoo_bin": ODOO_BIN},
        timeout=180,
    )
    assert opened.status_code == 200, opened.text
    session = opened.json()

    return session, {"X-OS-Session-Key": session["write_key"]}


def close_session(daemon, session_id, headers):
    httpx.delete(
        f"{daemon['base']}/api/sessions/{session_id}",
        params={"force": True},
        headers=headers,
        timeout=60,
    )


def test_the_entrypoint_resolved_both_identities(daemon):
    logs = subprocess.run(
        ["docker", "logs", daemon["name"]], capture_output=True, text=True, check=False
    )
    line = next(
        entry
        for entry in (logs.stdout + logs.stderr).splitlines()
        if entry.startswith("odoo-sheller: uid=")
    )
    plan = dict(part.split("=", 1) for part in line.split(" ")[1:])
    assert plan["uid"] == str(os.getuid())


def test_it_sees_the_host_containers_through_the_mounted_socket(daemon):
    """GET /api/containers answers with a bare list of dicts."""
    listed = httpx.get(f"{daemon['base']}/api/containers", timeout=30).json()
    assert CONTAINER in [entry["name"] for entry in listed]


def test_a_full_cycle_and_the_journal_lands_on_the_host(daemon):
    """The assertion that justifies the bind mount: after the session, the
    journal is a file in the caller's own directory, readable by the caller."""
    session, headers = open_session(daemon)
    try:
        result = httpx.post(
            f"{daemon['base']}/api/sessions/{session['id']}/exec",
            json={"code": "print(env['res.users'].browse(1).login)"},
            headers=headers,
            timeout=180,
        ).json()
        assert result["error"] is None, result
        assert result["stdout"].strip()

        rolled = httpx.post(
            f"{daemon['base']}/api/sessions/{session['id']}/rollback",
            headers=headers,
            timeout=60,
        )
        assert rolled.status_code == 200
    finally:
        close_session(daemon, session["id"], headers)

    journals = list((daemon["state"] / "journals").glob("*.jsonl"))
    assert journals, "no journal reached the host"
    assert os.access(journals[0], os.R_OK)
    assert journals[0].stat().st_uid == os.getuid()
    records = [json.loads(line) for line in journals[0].read_text().splitlines()]
    assert any(record.get("kind") == "exec" for record in records)


def test_interrupt_works_from_a_containerized_client(daemon):
    """`docker exec … kill -INT <pid>` should not care where the client runs.
    It is one line of reasoning and one line of proof; take the proof.

    exec blocks until the command finishes, so the interrupt has to come from
    somewhere else — hence the thread.
    """
    session, headers = open_session(daemon)
    outcome = {}

    def run_forever():
        outcome["response"] = httpx.post(
            f"{daemon['base']}/api/sessions/{session['id']}/exec",
            json={"code": "import time\nwhile True:\n    time.sleep(1)"},
            headers=headers,
            timeout=180,
        )

    worker = threading.Thread(target=run_forever)
    worker.start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            state = httpx.get(
                f"{daemon['base']}/api/sessions/{session['id']}", timeout=10
            ).json()["state"]
            if state == "busy":
                break
            time.sleep(0.5)
        else:
            raise AssertionError("the session never went busy")

        stopped = httpx.post(
            f"{daemon['base']}/api/sessions/{session['id']}/interrupt",
            headers=headers,
            timeout=60,
        )
        assert stopped.status_code == 200
        worker.join(timeout=60)
        assert not worker.is_alive(), "exec never returned after the interrupt"
        error = outcome["response"].json()["error"]
        assert error is not None
        assert error["type"] == "KeyboardInterrupt"
    finally:
        worker.join(timeout=5)
        close_session(daemon, session["id"], headers)


def test_a_uid_the_image_already_uses_is_adopted():
    """Debian ships a dozen accounts below 1000 and useradd refuses a
    duplicate. A host whose user is one of those numbers must still start,
    under that account's name — the number is what matters."""
    port = free_port()
    name = f"odoo-sheller-adopt-{port}"
    started = subprocess.run(
        [
            "docker", "run", "-d", "--name", name,
            "-v", f"{HOST_SOCKET}:/var/run/docker.sock",
            # No state mount on purpose: a host-owned directory would be
            # unwritable by uid 33 on Linux, and what is under test here is
            # the identity, not the mount.
            # www-data, present in every Debian base image.
            "-e", "ODOO_SHELLER_UID=33",
            "-e", "ODOO_SHELLER_GID=33",
            "-p", f"127.0.0.1:{port}:8765",
            IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/sessions", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(1)
        else:
            logs = subprocess.run(
                ["docker", "logs", name], capture_output=True, text=True, check=False
            )
            raise AssertionError(f"never started:\n{logs.stdout}\n{logs.stderr}")

        # No ps in a slim image; /proc is the authority anyway.
        who = subprocess.run(
            [
                "docker", "exec", name, "sh", "-c",
                "getent passwd $(awk '/^Uid:/ {print $2}' /proc/1/status) | cut -d: -f1",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert who.stdout.strip() == "www-data", who.stderr
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def test_the_running_container_is_findable_by_label(daemon):
    """How a caller finds what it started without remembering the name."""
    found = subprocess.run(
        ["docker", "ps", "-q", "--filter", "label=tech.ventor.odoo-sheller"],
        capture_output=True,
        text=True,
        check=False,
    )
    ids = found.stdout.split()
    running = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}", daemon["name"]],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert any(running.startswith(one) for one in ids), found.stdout


def test_no_state_and_no_uid_is_refused(tmp_path):
    """The container must not start into a state it cannot own. Exit code 2,
    and a message naming both ways out."""
    name = f"odoo-sheller-refuse-{free_port()}"
    attempt = subprocess.run(
        [
            "docker", "run", "--rm", "--name", name,
            "-v", f"{HOST_SOCKET}:/var/run/docker.sock",
            IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert attempt.returncode == 2, attempt.stdout + attempt.stderr
    assert "ODOO_SHELLER_UID" in attempt.stderr
