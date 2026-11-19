"""Finding odoo-bin and its config inside a container that keeps them somewhere
of its own — a dev container with the checkout under /workspace, say.

LOCATE_SOURCE runs in the container's Python, so it is exec'd here into a
namespace and its functions are called against a tree under tmp_path.
"""

import json
import subprocess
import sys

import pytest

from odoo_sheller import discovery
from odoo_sheller.psql import RUNNER_SOURCE


@pytest.fixture
def locate():
    namespace = {}
    exec(discovery.LOCATE_SOURCE, namespace)  # noqa: S102

    return namespace


def checkout(root, major=20):
    """An Odoo source checkout: odoo-bin with the odoo package beside it."""
    root.mkdir(parents=True, exist_ok=True)
    odoo_bin = root / "odoo-bin"
    odoo_bin.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    release = root / "odoo" / "release.py"
    release.parent.mkdir(exist_ok=True)
    release.write_text(f"version_info = ({major}, 0, 0, 'final', 0, '')\n", encoding="utf-8")

    return odoo_bin


def module(path):
    path.mkdir(parents=True)
    (path / "__manifest__.py").write_text("{'name': 'x'}\n", encoding="utf-8")

    return path


def test_a_checkout_under_the_workspace_is_found(locate, tmp_path):
    workspace = tmp_path / "workspace"
    odoo_bin = checkout(workspace / "odoo")
    found, _ = locate["search"]([str(workspace)])
    assert found == str(odoo_bin)


def test_a_file_named_odoo_bin_without_odoo_beside_it_is_not_odoo(locate, tmp_path):
    stray = tmp_path / "scripts" / "odoo-bin"
    stray.parent.mkdir()
    stray.write_text("echo hi\n", encoding="utf-8")
    found, _ = locate["search"]([str(tmp_path)])
    assert found is None


def test_an_addon_is_not_walked_into(locate, tmp_path):
    """A workspace's addons are worktrees full of modules: thousands of files,
    and never where odoo-bin is."""
    checkout(module(tmp_path / "addons" / "some_module") / "deep")
    found, _ = locate["search"]([str(tmp_path)])
    assert found is None


def test_the_shallowest_checkout_wins(locate, tmp_path):
    near = checkout(tmp_path / "odoo")
    checkout(tmp_path / "old" / "versions" / "odoo")
    found, _ = locate["search"]([str(tmp_path)])
    assert found == str(near)


def test_the_search_stops_at_its_depth(locate, tmp_path):
    checkout(tmp_path / "a" / "b" / "c" / "d" / "e" / "odoo")
    found, _ = locate["search"]([str(tmp_path)])
    assert found is None


def test_the_search_reports_configs_with_an_options_section(locate, tmp_path):
    checkout(tmp_path / "odoo")
    good = tmp_path / "odoo.conf"
    good.write_text("[options]\ndb_host = postgres\n", encoding="utf-8")
    (tmp_path / "nginx.conf").write_text("server {}\n", encoding="utf-8")
    _, configs = locate["search"]([str(tmp_path)])
    assert configs == [str(good)]


def _process(proc, pid, *args, cwd=None):
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "cmdline").write_bytes(b"".join(str(a).encode() + b"\0" for a in args))
    if cwd is not None:
        (proc / str(pid) / "cwd").symlink_to(cwd)


def test_a_running_odoo_names_its_binary_and_config(locate, tmp_path):
    proc = tmp_path / "proc"
    odoo_bin = checkout(tmp_path / "workspace" / "odoo")
    _process(proc, 1, "/bin/sh", "-c", "while sleep 1000; do :; done")
    _process(proc, 77, "python3", odoo_bin, "-c", "/workspace/odoo.conf", "--dev=all")
    assert locate["running"](str(proc)) == (str(odoo_bin), "/workspace/odoo.conf")


@pytest.mark.parametrize("flag", [["--config=/w/o.conf"], ["--config", "/w/o.conf"]])
def test_a_running_odoo_config_flag_in_either_spelling(locate, tmp_path, flag):
    odoo_bin = checkout(tmp_path / "w")
    _process(tmp_path / "proc", 9, odoo_bin, *flag)
    assert locate["running"](str(tmp_path / "proc")) == (str(odoo_bin), "/w/o.conf")


def test_a_user_named_odoo_is_not_the_binary(locate, tmp_path):
    """`gosu odoo …`, `su odoo`, `chown -R odoo /data`: the word is a user, and
    the scan has to go on to the argument, or the process, that is Odoo."""
    proc = tmp_path / "proc"
    odoo_bin = checkout(tmp_path / "opt" / "odoo")
    _process(proc, 1, "su", "odoo", cwd=tmp_path)
    _process(proc, 2, "chown", "-R", "odoo", "/data", cwd=tmp_path)
    _process(proc, 3, "gosu", "odoo", odoo_bin, "-c", "/x.conf", cwd=tmp_path)
    assert locate["running"](str(proc)) == (str(odoo_bin), "/x.conf")


def test_a_relative_config_is_the_process_s_own(locate, tmp_path):
    """`cd /srv/odoo && ./odoo-bin -c odoo.conf`: relative to where it ran,
    not to where docker exec happens to start."""
    srv = tmp_path / "srv"
    odoo_bin = checkout(srv)
    _process(tmp_path / "proc", 5, "./odoo-bin", "-c", "odoo.conf", cwd=srv)
    assert locate["running"](str(tmp_path / "proc")) == (str(odoo_bin), str(srv / "odoo.conf"))


def test_the_running_server_s_config_beats_the_image_s(locate, tmp_path, monkeypatch):
    """The image ships /etc/odoo/odoo.conf; compose runs `-c custom.conf`. The
    shell belongs beside the server, with its addons and its database."""
    monkeypatch.delenv("ODOO_RC", raising=False)
    shipped = tmp_path / "etc" / "odoo.conf"
    shipped.parent.mkdir()
    shipped.write_text("[options]\n", encoding="utf-8")
    custom = tmp_path / "etc" / "custom.conf"
    custom.write_text("[options]\n", encoding="utf-8")
    odoo_bin = checkout(tmp_path / "opt" / "odoo")
    _process(tmp_path / "proc", 4, odoo_bin, "-c", custom)
    found = locate["locate"](
        standard=[str(odoo_bin)], configs=[str(shipped)], roots=[], proc=str(tmp_path / "proc"),
    )
    assert found == (str(odoo_bin), str(custom))


def test_odoo_rc_beats_the_running_server(locate, tmp_path, monkeypatch):
    """What Odoo itself would read first."""
    rc = tmp_path / "rc.conf"
    rc.write_text("[options]\n", encoding="utf-8")
    monkeypatch.setenv("ODOO_RC", str(rc))
    custom = tmp_path / "custom.conf"
    custom.write_text("[options]\n", encoding="utf-8")
    odoo_bin = checkout(tmp_path / "opt" / "odoo")
    _process(tmp_path / "proc", 4, odoo_bin, "-c", custom)
    found = locate["locate"](
        standard=[str(odoo_bin)], configs=[], roots=[], proc=str(tmp_path / "proc"),
    )
    assert found == (str(odoo_bin), str(rc))


def test_no_proc_is_no_running_odoo(locate, tmp_path):
    assert locate["running"](str(tmp_path / "nowhere")) == (None, None)


def test_locate_falls_back_to_the_search(locate, tmp_path):
    workspace = tmp_path / "workspace"
    odoo_bin = checkout(workspace / "odoo")
    conf = workspace / "odoo.conf"
    conf.write_text("[options]\ndb_host = postgres\n", encoding="utf-8")
    found = locate["locate"](
        standard=[str(tmp_path / "opt" / "odoo-bin")], configs=[],
        roots=[str(workspace)], proc=str(tmp_path / "proc"),
    )
    assert found == (str(odoo_bin), str(conf))


def test_locate_prefers_a_standard_path_and_never_searches_then(locate, tmp_path):
    standard = tmp_path / "opt" / "odoo-bin"
    standard.parent.mkdir()
    standard.write_text("", encoding="utf-8")
    conf = tmp_path / "etc.conf"
    conf.write_text("[options]\n", encoding="utf-8")
    checkout(tmp_path / "workspace" / "odoo")
    found = locate["locate"](
        standard=[str(standard)], configs=[str(conf)],
        roots=[str(tmp_path / "workspace")], proc=str(tmp_path / "proc"),
    )
    assert found == (str(standard), str(conf))


def test_locate_takes_the_running_odoo_before_searching(locate, tmp_path):
    proc = tmp_path / "proc"
    (proc / "5").mkdir(parents=True)
    running = checkout(tmp_path / "src" / "odoo")
    conf = tmp_path / "src" / "dev.conf"
    conf.write_text("[options]\n", encoding="utf-8")
    (proc / "5" / "cmdline").write_bytes(f"{running}\0-c\0{conf}\0".encode())
    checkout(tmp_path / "workspace" / "odoo")
    found = locate["locate"](
        standard=[], configs=[], roots=[str(tmp_path / "workspace")], proc=str(proc),
    )
    assert found == (str(running), str(conf))


def test_a_config_beside_the_checkout_beats_one_further_away(locate, tmp_path):
    workspace = tmp_path / "workspace"
    odoo_bin = checkout(workspace / "odoo")
    (workspace / "deploy").mkdir()
    (workspace / "deploy" / "prod.conf").write_text("[options]\n", encoding="utf-8")
    near = workspace / "odoo" / "debian.conf"
    near.write_text("[options]\n", encoding="utf-8")
    found = locate["locate"](
        standard=[], configs=[], roots=[str(workspace)], proc=str(tmp_path / "proc"),
    )
    assert found == (str(odoo_bin), str(near))


def test_the_walk_stops_at_the_first_root_that_answers_both(locate, tmp_path):
    first = tmp_path / "first"
    checkout(first / "odoo")
    (first / "odoo.conf").write_text("[options]\n", encoding="utf-8")
    second = tmp_path / "second"
    second.mkdir()
    (second / "other.conf").write_text("[options]\n", encoding="utf-8")
    _, configs = locate["search"]([str(first), str(second)])
    assert configs == [str(first / "odoo.conf")]


def test_the_roots_can_be_named_in_the_environment(locate, tmp_path, monkeypatch):
    """For a container whose checkout lies off every usual root."""
    elsewhere = tmp_path / "srv2" / "src"
    odoo_bin = checkout(elsewhere / "odoo")
    monkeypatch.setenv("ODOO_SHELLER_SEARCH_ROOTS", f"{tmp_path / 'none'}:{elsewhere}")
    found = locate["locate"](standard=[], configs=[], proc=str(tmp_path / "proc"))
    assert found == (str(odoo_bin), None)


def test_the_probe_finds_a_checkout_in_the_working_directory(tmp_path):
    """docker exec starts in the container's WorkingDir — /workspace in a
    compose file that keeps the checkout there."""
    workspace = tmp_path / "workspace"
    odoo_bin = checkout(workspace / "odoo", major=19)
    conf = workspace / "odoo.conf"
    conf.write_text("[options]\ndb_host = nowhere.invalid\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", discovery.PROBE_SOURCE],
        capture_output=True, text=True, timeout=30, cwd=workspace,
        # The working directory only: the usual roots are this machine's own.
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "ODOO_SHELLER_SEARCH_ROOTS": str(workspace)},
        check=False,
    )
    payload = json.loads([line for line in proc.stdout.splitlines() if line.startswith("{")][-1])
    assert payload["odoo_bin"] == str(odoo_bin)
    assert payload["config"] == str(conf)
    assert payload["odoo_major"] == 19


@pytest.mark.parametrize(
    "source",
    [discovery.PROBE_SOURCE, discovery.LIST_TESTS_SOURCE, RUNNER_SOURCE],
    ids=["probe", "list_tests", "psql"],
)
def test_every_script_in_the_container_finds_odoo_the_same_way(source):
    assert discovery.LOCATE_SOURCE in source
    assert "locate()" in source


def test_a_standard_odoo_bin_never_takes_a_config_from_a_stranger(locate, tmp_path):
    """With odoo-bin in a usual place, a .conf found by walking /mnt is some
    other instance's: its databases listed, its host dialled."""
    standard = tmp_path / "usr" / "bin" / "odoo"
    standard.parent.mkdir(parents=True)
    standard.write_text("", encoding="utf-8")
    elsewhere = tmp_path / "mnt" / "backup"
    elsewhere.mkdir(parents=True)
    (elsewhere / "other.conf").write_text("[options]\ndb_host = prod\n", encoding="utf-8")
    found = locate["locate"](
        standard=[str(standard)], configs=[], roots=[str(tmp_path / "mnt")],
        proc=str(tmp_path / "proc"),
    )
    assert found == (str(standard), None)


def test_a_standard_odoo_bin_takes_a_config_beside_it_or_one_up(locate, tmp_path):
    standard = tmp_path / "opt" / "odoo" / "odoo-bin"
    standard.parent.mkdir(parents=True)
    standard.write_text("", encoding="utf-8")
    up = tmp_path / "opt" / "server.conf"
    up.write_text("[options]\n", encoding="utf-8")
    found = locate["locate"](
        standard=[str(standard)], configs=[], roots=[], proc=str(tmp_path / "proc"),
    )
    assert found == (str(standard), str(up))


def test_a_devcontainer_s_config_is_found(locate, tmp_path):
    """The case all this is for keeps its config in .devcontainer."""
    workspace = tmp_path / "workspace"
    odoo_bin = checkout(workspace / "odoo")
    conf = workspace / ".devcontainer" / "odoo.conf"
    conf.parent.mkdir()
    conf.write_text("[options]\ndb_host = postgres\n", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "x.conf").write_text("[options]\n", encoding="utf-8")
    found = locate["locate"](
        standard=[], configs=[], roots=[str(workspace)], proc=str(tmp_path / "proc"),
    )
    assert found == (str(odoo_bin), str(conf))


def test_a_project_with_a_release_script_of_its_own_is_walked(locate, tmp_path):
    """Only Odoo's own package is passed over — a directory named odoo."""
    project = tmp_path / "project"
    odoo_bin = checkout(project)
    (project / "release.py").write_text("print('ship it')\n", encoding="utf-8")
    found, _ = locate["search"]([str(tmp_path)])
    assert found == str(odoo_bin)
