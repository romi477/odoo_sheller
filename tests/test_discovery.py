import json

from odoo_sheller import discovery


def fake_runner(responses):
    calls = []

    async def runner(argv, stdin=None):
        calls.append((argv, stdin))

        return responses.pop(0)

    runner.calls = calls

    return runner


async def test_list_containers_parses_docker_ps_json_lines():
    payload = (
        '{"Names":"integra19","Image":"odoo:19","Status":"Up 2 hours","ID":"abc"}\n'
        '{"Names":"pg","Image":"postgres:16","Status":"Up 2 hours","ID":"def"}\n'
    )
    runner = fake_runner([(0, payload, "")])
    containers = await discovery.list_containers(runner=runner)
    assert [c["name"] for c in containers] == ["integra19", "pg"]
    assert containers[0]["image"] == "odoo:19"
    assert containers[0]["status"] == "Up 2 hours"
    assert runner.calls[0][0][:3] == ["docker", "ps", "--format"]


async def test_list_containers_reports_docker_failure():
    runner = fake_runner([(1, "", "Cannot connect to the Docker daemon")])
    try:
        await discovery.list_containers(runner=runner)
    except RuntimeError as exc:
        assert "Docker daemon" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


async def test_probe_returns_what_the_container_reported():
    payload = json.dumps({
        "ok": True,
        "odoo_bin": "/opt/odoo/odoo-bin",
        "odoo_version": "19.0",
        "odoo_major": 19,
        "python": "3.12.3",
        "config": "/etc/odoo/odoo.conf",
        "db_name": "integra_db_19",
        "databases": ["integra_db_19", "demo"],
        "error": None,
    })
    runner = fake_runner([(0, payload, "some odoo log noise")])
    result = await discovery.probe("integra19", runner=runner)
    assert result["ok"] is True
    assert result["supported"] is True
    assert result["db_name"] == "integra_db_19"
    assert result["databases"] == ["integra_db_19", "demo"]
    argv, stdin = runner.calls[0]
    assert argv[:4] == ["docker", "exec", "-i", "integra19"]
    assert stdin == discovery.PROBE_SOURCE


def test_probe_source_reads_version_and_db_name_from_a_fake_container(tmp_path):
    """Run the probe script itself against a fake Odoo tree on this machine."""
    import subprocess
    import sys

    fake_bin = tmp_path / "odoo-bin"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    release = tmp_path / "odoo" / "release.py"
    release.parent.mkdir()
    release.write_text("version_info = (19, 0, 0, 'final', 0, '')\n", encoding="utf-8")
    conf = tmp_path / "odoo.conf"
    conf.write_text(
        "[options]\ndb_name = acme_dev\ndb_host = nowhere.invalid\ndb_user = odoo\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-c", discovery.PROBE_SOURCE],
        capture_output=True, text=True, timeout=30,
        env={"ODOO_RC": str(conf), "PATH": f"{tmp_path}:/usr/bin:/bin"},
        check=False,
    )
    payload = json.loads([line for line in proc.stdout.splitlines() if line.startswith("{")][-1])
    assert payload["odoo_bin"] == str(fake_bin)
    assert payload["odoo_version"] == "19.0"
    assert payload["odoo_major"] == 19
    assert payload["config"] == str(conf)
    assert payload["db_name"] == "acme_dev"
    assert payload["ok"] is True
    # No reachable Postgres here, so the list is empty and the reason is stated.
    assert payload["databases"] == []
    assert "database list unavailable" in payload["error"]


async def test_probe_refuses_other_major_versions():
    payload = json.dumps({
        "ok": True, "odoo_bin": "/opt/odoo/odoo-bin", "odoo_version": "17.0",
        "odoo_major": 17, "python": "3.10.0", "config": None, "databases": [], "error": None,
    })
    result = await discovery.probe("old", runner=fake_runner([(0, payload, "")]))
    assert result["supported"] is False
    assert "19" in result["error"]


async def test_probe_reports_a_container_without_odoo():
    payload = json.dumps({
        "ok": False, "odoo_bin": None, "odoo_version": None, "odoo_major": None,
        "python": "3.12.3", "config": None, "databases": [], "error": "odoo-bin not found",
    })
    result = await discovery.probe("pg", runner=fake_runner([(0, payload, "")]))
    assert result["ok"] is False
    assert result["error"] == "odoo-bin not found"


async def test_probe_survives_unparsable_output():
    runner = fake_runner([(126, "", "OCI runtime exec failed")])
    result = await discovery.probe("weird", runner=runner)
    assert result["ok"] is False
    assert "OCI runtime exec failed" in result["error"]


def test_probe_source_is_stdlib_plus_psycopg2_only():
    assert "import psycopg2" in discovery.PROBE_SOURCE
    assert "import odoo" not in discovery.PROBE_SOURCE  # importing Odoo is slow and can fail
