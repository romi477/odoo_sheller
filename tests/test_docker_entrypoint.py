"""The container entrypoint, run for real by /bin/sh.

Everything hard about the containerized mode lives here: journals must end up
owned by the person who will later read them with a natively installed daemon,
and the Engine must be reachable through a group that has nothing to do with
that person. The two are resolved from different places and are not
interchangeable, which is exactly the kind of thing a substring assertion
would not notice.

The script stops at ODOO_SHELLER_DRY_RUN=1, just before it would need gosu and
useradd, so these run on a developer's macOS as well as in CI.
"""

import os
import socket
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parent.parent / "packaging" / "docker" / "entrypoint.sh"


@pytest.fixture
def fake_socket():
    """A real unix socket: the script tests for one, not for a path.

    Bound under /tmp because pytest's tmp_path is longer than the AF_UNIX
    limit on macOS (~104 bytes).
    """
    path = Path(f"/tmp/os-ep-{os.getpid()}.sock")
    if path.exists():
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    yield path
    server.close()
    path.unlink(missing_ok=True)


def run(socket_path, state, **env):
    environment = {
        "PATH": os.environ["PATH"],
        "ODOO_SHELLER_SOCKET": str(socket_path),
        "ODOO_SHELLER_STATE": str(state),
        "ODOO_SHELLER_DRY_RUN": "1",
    }
    environment.update({k: str(v) for k, v in env.items()})

    return subprocess.run(
        ["/bin/sh", str(ENTRYPOINT)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def plan(result):
    """The one line the script prints, as a dict."""
    for line in result.stdout.splitlines():
        if line.startswith("odoo-sheller: uid="):

            return dict(part.split("=", 1) for part in line.split(" ")[1:])

    raise AssertionError(f"no plan line in: {result.stdout!r}")


def test_no_socket_is_a_refusal_not_a_slow_failure(tmp_path):
    """Without the Engine the daemon can do nothing, and starting anyway would
    surface much later as a probe that finds no containers."""
    state = tmp_path / "state"
    state.mkdir()
    result = run(tmp_path / "absent.sock", state)
    assert result.returncode != 0
    assert "docker" in result.stderr.lower()


def test_identity_comes_from_the_mounted_directory(fake_socket, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    result = run(fake_socket, state)
    assert result.returncode == 0
    assert plan(result)["uid"] == str(os.getuid())


def test_the_environment_overrides_the_directory(fake_socket, tmp_path):
    """The caller always knows the host uid; the directory only knows it once
    it exists."""
    state = tmp_path / "state"
    state.mkdir()
    result = run(fake_socket, state, ODOO_SHELLER_UID=4242, ODOO_SHELLER_GID=4243)
    assert result.returncode == 0
    assert plan(result)["uid"] == "4242"
    assert plan(result)["gid"] == "4243"


def test_the_socket_group_is_read_separately_from_the_identity(fake_socket, tmp_path):
    """Two gids, two sources. Collapsing them would either lose the Engine or
    write journals as the wrong user."""
    state = tmp_path / "state"
    state.mkdir()
    result = run(fake_socket, state, ODOO_SHELLER_UID=4242, ODOO_SHELLER_GID=4243)
    assert plan(result)["socket_gid"] != "4243"


def test_uid_zero_keeps_root(fake_socket, tmp_path):
    """macOS VirtioFS and rootless Docker both present the mount as root-owned
    and both unwind ownership on the way out. Dropping there would be wrong."""
    state = tmp_path / "state"
    state.mkdir()
    result = run(fake_socket, state, ODOO_SHELLER_UID=0, ODOO_SHELLER_GID=0)
    assert plan(result)["drop"] == "no"


def test_a_real_uid_drops_privileges(fake_socket, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    result = run(fake_socket, state, ODOO_SHELLER_UID=1000, ODOO_SHELLER_GID=1000)
    assert plan(result)["drop"] == "yes"


def test_a_missing_directory_with_no_uid_is_refused(fake_socket, tmp_path):
    """The script cannot tell a Linux host from a macOS one, and on Linux
    starting here means journals a native daemon can never append to. One
    rule for both hosts, and a message naming both ways out."""
    state = tmp_path / "state"
    result = run(fake_socket, state)
    assert result.returncode == 2
    assert not state.exists()
    assert "ODOO_SHELLER_UID" in result.stderr


def test_a_given_uid_makes_the_first_run_work(fake_socket, tmp_path):
    """The identity is what was missing, not the directory. With one in hand
    the script creates it and carries on."""
    state = tmp_path / "state"
    result = run(fake_socket, state, ODOO_SHELLER_UID=1000, ODOO_SHELLER_GID=1000)
    assert result.returncode == 0
    assert state.is_dir()
    assert plan(result)["uid"] == "1000"
