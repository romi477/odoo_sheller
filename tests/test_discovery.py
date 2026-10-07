import json

import pytest

from odoo_sheller import discovery


def fake_runner(responses):
    calls = []

    async def runner(argv, stdin=None):
        calls.append((argv, stdin))

        return responses.pop(0)

    runner.calls = calls

    return runner


async def test_list_containers_uses_the_docker_override(monkeypatch):
    monkeypatch.setenv("ODOO_SHELLER_DOCKER", "/custom/bin/docker")
    runner = fake_runner([(0, '{"Names":"c","Image":"i","Status":"Up","ID":"1"}\n', "")])
    await discovery.list_containers(runner=runner)
    assert runner.calls[0][0][0] == "/custom/bin/docker"


async def test_probe_uses_the_docker_override(monkeypatch):
    monkeypatch.setenv("ODOO_SHELLER_DOCKER", "/custom/bin/docker")
    runner = fake_runner([(1, "", "no docker")])
    await discovery.probe("integra19", runner=runner)
    assert runner.calls[0][0][0] == "/custom/bin/docker"


async def test_list_tests_uses_the_docker_override(monkeypatch):
    monkeypatch.setenv("ODOO_SHELLER_DOCKER", "/custom/bin/docker")
    runner = fake_runner([(1, "", "no docker")])
    await discovery.list_tests("integra19", "sale", runner=runner)
    assert runner.calls[0][0][0] == "/custom/bin/docker"


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


@pytest.mark.parametrize("major", discovery.SUPPORTED_MAJORS)
async def test_probe_accepts_every_supported_major(major):
    """Parametrised from the tuple itself: a major added there without a probe
    that accepts it is the failure this would otherwise miss."""
    payload = json.dumps({
        "ok": True, "odoo_bin": "/opt/odoo/odoo-bin", "odoo_version": f"{major}.0",
        "odoo_major": major, "python": "3.10.0", "config": None, "databases": [],
        "error": None,
    })
    result = await discovery.probe("box", runner=fake_runner([(0, payload, "")]))
    assert result["supported"] is True
    assert result["error"] is None


@pytest.mark.parametrize("major", discovery.SUPPORTED_MAJORS)
async def test_a_supported_major_is_not_marked_untested(major):
    payload = json.dumps({
        "ok": True, "odoo_bin": "/opt/odoo/odoo-bin", "odoo_version": f"{major}.0",
        "odoo_major": major, "python": "3.10.0", "config": None, "databases": [],
        "error": None,
    })
    result = await discovery.probe("box", runner=fake_runner([(0, payload, "")]))
    assert result["untested"] is False
    assert result["warning"] is None


@pytest.mark.parametrize("major", discovery.UNTESTED_MAJORS)
async def test_an_untested_major_opens_and_says_what_is_not_known(major):
    """13 and 14 were never run here. The source says a session, exec, commit,
    rollback and interrupt should work, and nothing says more than that — so the
    probe lets them through, with a warning, and no document says "supported"."""
    payload = json.dumps({
        "ok": True, "odoo_bin": "/opt/odoo/odoo-bin", "odoo_version": f"{major}.0",
        "odoo_major": major, "python": "3.8.0", "config": None, "databases": [], "error": None,
    })
    result = await discovery.probe("old", runner=fake_runner([(0, payload, "")]))
    assert result["supported"] is True
    assert result["untested"] is True
    assert result["error"] is None
    assert f"{major}.0" in result["warning"]
    assert "not fully tested" in result["warning"]


async def test_the_warning_for_thirteen_says_tests_do_not_run_there():
    payload = json.dumps({
        "ok": True, "odoo_bin": "/x", "odoo_version": "13.0", "odoo_major": 13,
        "python": "3.6.9", "config": None, "databases": [], "error": None,
    })
    result = await discovery.probe("old", runner=fake_runner([(0, payload, "")]))
    assert "run_test" in result["warning"]
    payload = payload.replace("13", "14")
    result = await discovery.probe("old", runner=fake_runner([(0, payload, "")]))
    assert "run_test" not in result["warning"]


async def test_probe_refuses_a_major_below_the_untested_ones():
    payload = json.dumps({
        "ok": True, "odoo_bin": "/opt/odoo/odoo-bin", "odoo_version": "12.0",
        "odoo_major": 12, "python": "3.10.0", "config": None, "databases": [], "error": None,
    })
    result = await discovery.probe("old", runner=fake_runner([(0, payload, "")]))
    assert result["supported"] is False
    assert result["untested"] is False
    # The refusal has to say what would work, not only what will not.
    assert "12.0" in result["error"]
    for major in (*discovery.SUPPORTED_MAJORS, *discovery.UNTESTED_MAJORS):
        assert str(major) in result["error"]


def test_the_hello_gate_lets_untested_majors_through_and_refuses_the_rest():
    assert discovery.version_refusal("13.0") is None
    assert discovery.version_refusal("14.0") is None
    assert discovery.version_refusal("19.0") is None
    assert discovery.version_refusal("saas~18.2") is None
    assert "12.0" in discovery.version_refusal("12.0")
    assert discovery.version_refusal(None) is None, "nothing says it is wrong"
    assert discovery.version_refusal("unknown") is None


def test_a_container_without_python_is_named_not_blamed():
    """Docker's OCI wall of text is not something a card should show."""
    raw = (
        'OCI runtime exec failed: exec failed: unable to start container '
        'process: exec: "python3": executable file not found in $PATH: unknown'
    )
    result = discovery._unreadable(126, "", raw)
    assert result["ok"] is False
    assert result["error_code"] == "no_python"
    assert result["error"] == "no python3 in this container"
    # The original stays for whoever is debugging the image itself.
    assert result["error_detail"] == raw


def test_a_probe_that_failed_for_another_reason_keeps_its_words():
    """Only the hopeless cases are reworded; the rest still have to be read."""
    result = discovery._unreadable(1, "", "Error response from daemon: container is paused")
    assert result["error_code"] is None
    assert result["error"] == "Error response from daemon: container is paused"
    assert result["error_detail"] is None


async def test_probe_reports_a_container_without_odoo():
    payload = json.dumps({
        "ok": False, "odoo_bin": None, "odoo_version": None, "odoo_major": None,
        "python": "3.12.3", "config": None, "databases": [], "error": "odoo-bin not found",
    })
    result = await discovery.probe("pg", runner=fake_runner([(0, payload, "")]))
    assert result["ok"] is False
    # Reworded for the card that shows it, and coded so the UI can fold the
    # container away instead of printing a failure nobody can act on.
    assert result["error"] == "no odoo-bin in this container"
    assert result["error_code"] == "no_odoo_bin"


async def test_probe_survives_unparsable_output():
    runner = fake_runner([(126, "", "OCI runtime exec failed")])
    result = await discovery.probe("weird", runner=runner)
    assert result["ok"] is False
    assert "OCI runtime exec failed" in result["error"]


def test_probe_source_is_stdlib_plus_psycopg2_only():
    assert "import psycopg2" in discovery.PROBE_SOURCE
    assert "import odoo" not in discovery.PROBE_SOURCE  # importing Odoo is slow and can fail


def _write_fake_odoo(tmp_path, addons_path_line, extra_files=None):
    fake_bin = tmp_path / "odoo-bin"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    (tmp_path / "odoo" / "addons").mkdir(parents=True, exist_ok=True)
    conf = tmp_path / "odoo.conf"
    conf.write_text(f"[options]\n{addons_path_line}\n", encoding="utf-8")

    return fake_bin, conf


def _run_list_tests_source(tmp_path, module, conf, extra_env=None):
    import subprocess
    import sys

    env = {
        "ODOO_RC": str(conf),
        "PATH": f"{tmp_path}:/usr/bin:/bin",
    }
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-", module],
        input=discovery.LIST_TESTS_SOURCE,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines, proc.stdout + proc.stderr

    return json.loads(lines[-1])


def test_list_tests_source_walks_a_fake_addon_tree(tmp_path):
    extra = tmp_path / "extra"
    addon = extra / "widget"
    (addon / "tests").mkdir(parents=True)
    (addon / "__manifest__.py").write_text("{'name': 'widget'}\n", encoding="utf-8")
    (addon / "tests" / "__init__.py").write_text(
        "from . import test_a\nfrom . import test_b\nfrom . import test_broken\n",
        encoding="utf-8",
    )
    (addon / "tests" / "helpers.py").write_text(
        "class TestNotDiscovered:\n    def test_skip(self):\n        pass\n",
        encoding="utf-8",
    )
    (addon / "tests" / "test_a.py").write_text(
        "class TestAlpha:\n"
        "    def test_one(self):\n        pass\n"
        "    def not_a_test(self):\n        pass\n"
        "    async def test_async(self):\n        pass\n",
        encoding="utf-8",
    )
    (addon / "tests" / "test_b.py").write_text(
        "class TestBeta:\n"
        "    def test_two(self):\n        pass\n"
        "class Helper:\n"
        "    def only_a_helper(self):\n        pass\n",
        encoding="utf-8",
    )
    (addon / "tests" / "test_broken.py").write_text("class TestBroken def\n", encoding="utf-8")
    _fake_bin, conf = _write_fake_odoo(tmp_path, f"addons_path = {extra}")
    payload = _run_list_tests_source(tmp_path, "widget", conf)
    assert payload["ok"] is True
    assert payload["error"] is None
    assert payload["module"] == "widget"
    assert payload["path"] == str(addon)
    names = [row["name"] for row in payload["classes"]]
    # helpers.py is not imported as a test module; Helper has no test_* method.
    assert names == ["TestAlpha", "TestBeta"]
    alpha = payload["classes"][0]
    assert alpha["spec"] == "widget.TestAlpha"
    assert [m["name"] for m in alpha["methods"]] == ["test_async", "test_one"]
    assert alpha["methods"][0]["spec"] == "widget.TestAlpha.test_async"
    assert payload["classes"][1]["methods"][0]["spec"] == "widget.TestBeta.test_two"


def test_list_tests_finds_classes_odoo_runs_whatever_they_are_named(tmp_path):
    """Odoo's loader selects TestCase subclasses, with no name convention. A
    `Test` prefix filter silently drops whole runnable classes."""
    extra = tmp_path / "extra"
    addon = extra / "widget"
    (addon / "tests").mkdir(parents=True)
    (addon / "__manifest__.py").write_text("{'name': 'widget'}\n", encoding="utf-8")
    (addon / "tests" / "__init__.py").write_text("from . import test_a\n", encoding="utf-8")
    (addon / "tests" / "test_a.py").write_text(
        "class QboSyncCase(TransactionCase):\n"
        "    def test_one(self):\n        pass\n"
        "class PlainHelper:\n"
        "    def helper(self):\n        pass\n",
        encoding="utf-8",
    )
    _fake_bin, conf = _write_fake_odoo(tmp_path, f"addons_path = {extra}")
    payload = _run_list_tests_source(tmp_path, "widget", conf)

    names = [row["name"] for row in payload["classes"]]
    assert names == ["QboSyncCase"], "a runnable class must not be dropped for its name"
    assert payload["classes"][0]["spec"] == "widget.QboSyncCase"
    # A class with no test_* method at all is not a test class.
    assert "PlainHelper" not in names


def test_list_tests_skips_files_odoo_never_imports(tmp_path):
    """Odoo only runs test modules reachable from tests/__init__.py. Listing
    the rest hands out specs that come back tests_run: 0."""
    extra = tmp_path / "extra"
    addon = extra / "widget"
    nested = addon / "tests" / "nested"
    nested.mkdir(parents=True)
    (addon / "__manifest__.py").write_text("{'name': 'widget'}\n", encoding="utf-8")
    (addon / "tests" / "__init__.py").write_text("from . import test_a\n", encoding="utf-8")
    (addon / "tests" / "test_a.py").write_text(
        "class TestAlpha:\n    def test_one(self):\n        pass\n", encoding="utf-8"
    )
    (addon / "tests" / "test_orphan.py").write_text(
        "class TestOrphan:\n    def test_two(self):\n        pass\n", encoding="utf-8"
    )
    (nested / "test_b.py").write_text(
        "class TestBeta:\n    def test_three(self):\n        pass\n", encoding="utf-8"
    )
    _fake_bin, conf = _write_fake_odoo(tmp_path, f"addons_path = {extra}")
    payload = _run_list_tests_source(tmp_path, "widget", conf)

    names = [row["name"] for row in payload["classes"]]
    assert names == ["TestAlpha"]
    assert "TestOrphan" not in names, "not imported in tests/__init__.py, never loaded"
    assert "TestBeta" not in names, "a nested subdir is not part of the tests package"


def test_list_tests_without_a_tests_init_reports_nothing_runnable(tmp_path):
    extra = tmp_path / "extra"
    addon = extra / "widget"
    (addon / "tests").mkdir(parents=True)
    (addon / "__manifest__.py").write_text("{'name': 'widget'}\n", encoding="utf-8")
    (addon / "tests" / "test_a.py").write_text(
        "class TestAlpha:\n    def test_one(self):\n        pass\n", encoding="utf-8"
    )
    _fake_bin, conf = _write_fake_odoo(tmp_path, f"addons_path = {extra}")
    payload = _run_list_tests_source(tmp_path, "widget", conf)

    assert payload["ok"] is True
    assert payload["classes"] == []


def test_list_tests_source_empty_tests_dir_is_ok(tmp_path):
    extra = tmp_path / "extra"
    addon = extra / "silent"
    addon.mkdir(parents=True)
    (addon / "__manifest__.py").write_text("{}\n", encoding="utf-8")
    _fake_bin, conf = _write_fake_odoo(tmp_path, f"addons_path = {extra}")
    payload = _run_list_tests_source(tmp_path, "silent", conf)
    assert payload["ok"] is True
    assert payload["classes"] == []


def test_list_tests_source_reports_module_not_found(tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    _fake_bin, conf = _write_fake_odoo(tmp_path, f"addons_path = {extra}")
    payload = _run_list_tests_source(tmp_path, "nope", conf)
    assert payload["ok"] is False
    assert payload["error_code"] == "module_not_found"


def test_list_tests_source_finds_core_addons_without_addons_path(tmp_path):
    addon = tmp_path / "odoo" / "addons" / "base"
    tests = addon / "tests"
    tests.mkdir(parents=True)
    (addon / "__manifest__.py").write_text("{}\n", encoding="utf-8")
    (tests / "__init__.py").write_text("from . import test_base\n", encoding="utf-8")
    (tests / "test_base.py").write_text(
        "class TestBase:\n    def test_ok(self):\n        pass\n",
        encoding="utf-8",
    )
    _fake_bin, conf = _write_fake_odoo(tmp_path, "db_name = x")
    payload = _run_list_tests_source(tmp_path, "base", conf)
    assert payload["ok"] is True
    assert payload["classes"][0]["spec"] == "base.TestBase"


def test_list_tests_source_is_stdlib_only():
    assert "import odoo" not in discovery.LIST_TESTS_SOURCE
    assert "import psycopg2" not in discovery.LIST_TESTS_SOURCE


async def test_list_tests_sends_the_module_as_argv():
    payload = json.dumps({
        "ok": True, "module": "widget", "path": "/x/widget",
        "classes": [], "error": None, "error_code": None,
    })
    runner = fake_runner([(0, payload, "")])
    result = await discovery.list_tests("qbo19", "widget", runner=runner)
    assert result["ok"] is True
    assert result["module"] == "widget"
    argv, stdin = runner.calls[0]
    assert argv == ["docker", "exec", "-i", "qbo19", "python3", "-", "widget"]
    assert stdin == discovery.LIST_TESTS_SOURCE


async def test_list_tests_rejects_a_dotted_spec_without_docker():
    runner = fake_runner([(0, "{}", "")])
    result = await discovery.list_tests("qbo19", "sale.TestSale", runner=runner)
    assert result["error"] == "invalid_module_name"
    assert result["error_code"] == "invalid_module_name"
    assert runner.calls == []


async def test_list_tests_rejects_an_empty_module_without_docker():
    runner = fake_runner([(0, "{}", "")])
    result = await discovery.list_tests("qbo19", "", runner=runner)
    assert result["error_code"] == "invalid_module_name"
    assert runner.calls == []


async def test_list_tests_survives_docker_exec_failure():
    runner = fake_runner([(126, "", "OCI runtime exec failed")])
    result = await discovery.list_tests("gone", "sale", runner=runner)
    assert result["ok"] is False
    assert "OCI runtime exec failed" in result["error"]


# --- odoo.sh: the instance answers from its own environment -------------


def oosh_env(**overrides):
    env = {
        "ODOO_VERSION": "19.0",
        "ODOO_STAGE": "staging",
        "PGDATABASE": "ventor-dev-demo-36887345",
    }
    env.update(overrides)

    return env


def run_oosh_probe(env, with_odoo_bin=True, tmp_path=None):
    """Run the real probe source under a faked odoo.sh environment."""
    import subprocess
    import sys

    full = {"PATH": "/usr/bin:/bin", **{k: v for k, v in env.items() if v is not None}}
    if with_odoo_bin:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / "odoo-bin"
        stub.write_text("#!/bin/sh\n", encoding="utf-8")
        stub.chmod(0o755)
        full["PATH"] = f"{bin_dir}:{full['PATH']}"
    proc = subprocess.run(
        [sys.executable, "-"], input=discovery.OOSH_PROBE_SOURCE,
        capture_output=True, text=True, timeout=30, env=full, check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines, proc.stdout + proc.stderr

    return json.loads(lines[-1])


def test_oosh_probe_source_reads_the_instance_out_of_its_environment(tmp_path):
    payload = run_oosh_probe(oosh_env(), tmp_path=tmp_path)
    assert payload["ok"] is True
    assert payload["error"] is None
    assert payload["odoo_version"] == "19.0"
    assert payload["odoo_major"] == 19
    assert payload["stage"] == "staging"
    assert payload["db_name"] == "ventor-dev-demo-36887345"
    # One build, one database: there is nothing to pick, and the instance user
    # cannot read pg_database anyway.
    assert payload["databases"] == ["ventor-dev-demo-36887345"]
    assert payload["odoo_bin"].endswith("odoo-bin")


def test_oosh_probe_source_says_what_is_missing(tmp_path):
    payload = run_oosh_probe(oosh_env(PGDATABASE=None), tmp_path=tmp_path)
    assert payload["ok"] is False
    assert "PGDATABASE" in payload["error"]


def test_oosh_probe_source_needs_no_odoo_and_no_psycopg2():
    """It never imports Odoo and never opens a connection: four variables."""
    assert "import odoo" not in discovery.OOSH_PROBE_SOURCE
    assert "psycopg2" not in discovery.OOSH_PROBE_SOURCE


async def test_probe_odoosh_goes_over_ssh_to_build_at_host():
    runner = fake_runner([(0, json.dumps({
        "ok": True, "odoo_version": "19.0", "odoo_major": 19, "stage": "staging",
        "db_name": "db", "databases": ["db"], "python": "3.12.3",
        "odoo_bin": "/opt/odoo.sh/odoosh/bin/odoo-bin", "config": None, "error": None,
    }), "")])
    result = await discovery.probe_odoosh("36887345", "build.dev.odoo.com", runner=runner)
    argv = runner.calls[0][0]
    assert argv[0] == "ssh"
    assert "36887345@build.dev.odoo.com" in argv
    assert result["supported"] is True
    assert result["stage"] == "staging"


async def test_probe_odoosh_ends_the_options_before_the_destination():
    runner = fake_runner([(0, json.dumps({
        "ok": True, "odoo_version": "19.0", "odoo_major": 19, "stage": "staging",
        "db_name": "db", "databases": ["db"], "python": "3.12.3",
        "odoo_bin": "/b", "config": None, "error": None,
    }), "")])
    await discovery.probe_odoosh("36887345", "build.dev.odoo.com", runner=runner)
    argv = runner.calls[0][0]
    assert argv[argv.index("36887345@build.dev.odoo.com") - 1] == "--"


@pytest.mark.parametrize(
    ("build", "host"),
    [
        ("-oProxyCommand=touch /tmp/x", "build.dev.odoo.com"),
        ("36887345", "-oProxyCommand=x"),
        ("a b", "h"),
        ("1", "h;id"),
    ],
)
async def test_probe_odoosh_refuses_a_build_or_host_that_is_not_a_name(build, host):
    runner = fake_runner([])
    with pytest.raises(ValueError):
        await discovery.probe_odoosh(build, host, runner=runner)
    assert runner.calls == []


async def test_probe_odoosh_refuses_another_major_the_way_docker_does():
    runner = fake_runner([(0, json.dumps({
        "ok": True, "odoo_version": "12.0", "odoo_major": 12, "stage": "staging",
        "db_name": "db", "databases": ["db"], "python": "3.10.0",
        "odoo_bin": "/x/odoo-bin", "config": None, "error": None,
    }), "")])
    result = await discovery.probe_odoosh("1", "h", runner=runner)
    assert result["supported"] is False
    assert "12.0" in result["error"]


async def test_probe_odoosh_carries_a_production_stage_through():
    """Nothing may swallow this word: it is what the commit guard reads."""
    runner = fake_runner([(0, json.dumps({
        "ok": True, "odoo_version": "19.0", "odoo_major": 19, "stage": "production",
        "db_name": "db", "databases": ["db"], "python": "3.12.3",
        "odoo_bin": "/x/odoo-bin", "config": None, "error": None,
    }), "")])
    result = await discovery.probe_odoosh("1", "h", runner=runner)
    assert result["stage"] == "production"


async def test_probe_odoosh_survives_unparsable_output():
    runner = fake_runner([(1, "ssh: Could not resolve hostname", "kex_exchange failed")])
    result = await discovery.probe_odoosh("1", "nope", runner=runner)
    assert result["ok"] is False
    assert result["supported"] is False
    assert "kex_exchange" in result["error"]


async def test_a_launcher_that_is_not_installed_is_an_answer():
    """The image has no ssh, a GUI's PATH may have no docker: both used to
    surface as a bare 500 instead of a sentence."""
    code, out, err = await discovery._docker(["definitely-not-a-real-binary-xyz", "ps"])
    assert code == 127
    assert out == ""
    assert "not installed" in err


async def test_a_command_that_never_answers_is_cut_off():
    """A paused container or a wedged Engine held the request — and an agent's
    whole tool call — with no end at all."""
    code, _out, err = await discovery._docker(["sleep", "5"], timeout=0.2)
    assert code == 124
    assert "did not answer" in err


async def test_an_odoosh_probe_without_ssh_says_what_is_missing(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    probe = await discovery.probe_odoosh("36887345", "build.dev.odoo.com")
    assert probe["supported"] is False
    assert "ssh" in probe["error"]
    assert "not installed" in probe["error"]


# --- a server reached by ssh ------------------------------------------------

SSH_LAUNCH = "/opt/odoo/13.0/env/bin/python /opt/odoo/13.0/odoo/odoo-bin shell -c /opt/odoo/13.0/odoo.conf"
SSH_FACTS = (
    "effective_user=odoo\nlogin_user=ubuntu\nhost=srv-1\nexecutable_ok=1\n"
    "interpreter=Python 3.6.9\nconfig_readable=1\ndb_name=acme\nodoo_version=13.0\n"
)


def ssh_recipe(access="ssh -i /k/key ubuntu@srv.example.com sudo -n -u odoo -H", launch=SSH_LAUNCH):
    from odoo_sheller.recipe import parse_access, parse_launch

    return parse_access(access, is_file=lambda path: True), parse_launch(launch)


async def test_probe_ssh_goes_through_the_cards_prefix_and_passes_the_launch_as_data():
    import shlex

    access, launch = ssh_recipe()
    runner = fake_runner([(0, SSH_FACTS, "")])
    await discovery.probe_ssh(access, launch, runner=runner)
    argv, stdin = runner.calls[0]
    assert argv[0] == "ssh"
    assert argv[argv.index("--") + 1] == "ubuntu@srv.example.com"
    assert stdin is None
    remote = shlex.split(argv[-1])
    assert remote[:7] == ["sudo", "-n", "-u", "odoo", "-H", "sh", "-c"]
    # The script is fixed text; what the card said arrives as arguments to it.
    assert remote[8:] == [
        "sh",
        "/opt/odoo/13.0/env/bin/python",
        "/opt/odoo/13.0/odoo.conf",
        "/opt/odoo/13.0/odoo/odoo-bin",
    ]
    assert "/opt/odoo/13.0" not in remote[7]


async def test_probe_ssh_says_who_where_and_what():
    access, launch = ssh_recipe()
    result = await discovery.probe_ssh(access, launch, runner=fake_runner([(0, SSH_FACTS, "")]))
    assert result["ok"] is True
    assert result["effective_user"] == "odoo"
    assert result["login_user"] == "ubuntu"
    assert result["host"] == "srv-1"
    assert result["python"] == "3.6.9"
    assert result["odoo_version"] == "13.0"
    assert result["odoo_major"] == 13
    assert result["db_name"] == "acme"
    assert result["stage"] is None, "a plain server does not say what it is; the card does"


async def test_a_config_the_user_cannot_read_is_named_in_terms_of_the_user():
    """The real server's config is readable only by its own user. Forgetting the
    sudo is the likely mistake, and this is how it should read."""
    access, launch = ssh_recipe()
    facts = SSH_FACTS.replace("effective_user=odoo", "effective_user=ubuntu").replace(
        "config_readable=1", "config_readable=0"
    )
    result = await discovery.probe_ssh(access, launch, runner=fake_runner([(0, facts, "")]))
    assert result["ok"] is False
    assert "/opt/odoo/13.0/odoo.conf" in result["error"]
    assert "ubuntu" in result["error"]
    assert result["error_code"] == "config_unreadable"


async def test_a_launch_that_is_not_an_executable_file_is_named():
    access, launch = ssh_recipe()
    facts = SSH_FACTS.replace("executable_ok=1", "executable_ok=0")
    result = await discovery.probe_ssh(access, launch, runner=fake_runner([(0, facts, "")]))
    assert result["ok"] is False
    assert "/opt/odoo/13.0/env/bin/python" in result["error"]
    assert result["error_code"] == "launch_not_executable"


@pytest.mark.parametrize(
    ("stderr", "code", "needle"),
    [
        ("Host key verification failed.", 255, "terminal"),
        ("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", 255, "changed"),
        ("ubuntu@h: Permission denied (publickey).", 255, "key"),
        ("ssh: Could not resolve hostname nope: Name or service not known", 255, "host name"),
        ("ssh: connect to host h port 22: Connection refused", 255, "reach"),
        ("sudo: a password is required", 1, "NOPASSWD"),
        ("sudo: sorry, you are not allowed to execute 'x' as odoo on h", 1, "sudo"),
    ],
)
async def test_a_failure_before_the_script_says_what_to_do(stderr, code, needle):
    access, launch = ssh_recipe()
    result = await discovery.probe_ssh(access, launch, runner=fake_runner([(code, "", stderr)]))
    assert result["ok"] is False
    assert result["supported"] is False
    assert needle in result["error"]
    assert stderr.strip() in result["error_detail"]


async def test_a_version_outside_the_gate_is_refused_like_any_other():
    access, launch = ssh_recipe()
    facts = SSH_FACTS.replace("odoo_version=13.0", "odoo_version=12.0")
    result = await discovery.probe_ssh(access, launch, runner=fake_runner([(0, facts, "")]))
    assert result["supported"] is False
    assert "12.0" in result["error"]


async def test_a_version_that_could_not_be_read_waits_for_the_session():
    """Not every install has `odoo/release.py` beside `odoo-bin`. It is not a
    refusal; the gate is applied again when the session says what it is."""
    access, launch = ssh_recipe()
    facts = SSH_FACTS.replace("odoo_version=13.0\n", "")
    result = await discovery.probe_ssh(access, launch, runner=fake_runner([(0, facts, "")]))
    assert result["ok"] is True
    assert result["supported"] is True
    assert result["version_known"] is False
    assert result["odoo_version"] is None


async def test_no_config_in_the_launch_is_not_a_missing_config():
    access, launch = ssh_recipe(launch="/x/py /x/odoo-bin shell")
    facts = "effective_user=odoo\nexecutable_ok=1\nodoo_version=19.0\n"
    result = await discovery.probe_ssh(access, launch, runner=fake_runner([(0, facts, "")]))
    assert result["ok"] is True
    assert result["config"] is None


async def test_an_ssh_probe_without_ssh_says_what_is_missing(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    access, launch = ssh_recipe()
    result = await discovery.probe_ssh(access, launch)
    assert result["ok"] is False
    assert "ssh" in result["error"]


def test_the_probe_script_reads_a_real_tree(tmp_path):
    """Run for real, in a local sh: the facts a server would report."""
    import subprocess

    tree = tmp_path / "odoo13"
    (tree / "odoo").mkdir(parents=True)
    (tree / "odoo" / "release.py").write_text(
        "version_info = (13, 0, 0, FINAL, 0, '')\nversion = '13.0'\n", encoding="utf-8"
    )
    (tree / "odoo-bin").write_text("#!/bin/sh\n", encoding="utf-8")
    (tree / "odoo-bin").chmod(0o755)
    conf = tmp_path / "odoo.conf"
    conf.write_text("[options]\ndb_name = acme\ndb_host = x\n", encoding="utf-8")

    def run(exe, config, odoo_bin):
        out = subprocess.run(
            ["sh", "-c", discovery.SSH_PROBE_SCRIPT, "sh", exe, config, odoo_bin],
            capture_output=True, text=True, check=True,
        ).stdout

        return dict(line.split("=", 1) for line in out.splitlines())

    facts = run(str(tree / "odoo-bin"), str(conf), str(tree / "odoo-bin"))
    assert facts["executable_ok"] == "1"
    assert facts["config_readable"] == "1"
    assert facts["db_name"] == "acme"
    assert facts["odoo_version"] == "13.0"
    assert facts["effective_user"]

    gone = run(str(tmp_path / "nope"), str(tmp_path / "nope.conf"), str(tmp_path / "nope" / "odoo-bin"))
    assert gone["executable_ok"] == "0"
    assert gone["config_readable"] == "0"
    assert "odoo_version" not in gone

    no_conf = run(str(tree / "odoo-bin"), "", str(tree / "odoo-bin"))
    assert "config_readable" not in no_conf


def test_the_probe_script_takes_its_arguments_as_data():
    """Nothing the card said is spliced into it, and nothing in it evaluates."""
    assert "eval" not in discovery.SSH_PROBE_SCRIPT
    assert '"$1"' in discovery.SSH_PROBE_SCRIPT


async def test_an_ssh_server_on_thirteen_opens_with_a_warning():
    """The one real server this was written for runs Odoo 13."""
    access, launch = ssh_recipe()
    result = await discovery.probe_ssh(access, launch, runner=fake_runner([(0, SSH_FACTS, "")]))
    assert result["ok"] is True
    assert result["supported"] is True
    assert result["untested"] is True
    assert "13.0" in result["warning"]
