import ast
import os
import re
import subprocess
import sys

import pytest

from odoo_sheller.discovery import PROBE_SOURCE
from odoo_sheller.psql import (
    MISSING_PREFIX,
    PID_PREFIX,
    RUNNER_SOURCE,
    PsqlNotAvailable,
    PsqlRefused,
    build_command,
    check_sql,
)
from odoo_sheller.transport import DOCKER, ODOOSH, SSH, Target, docker_bin


@pytest.mark.parametrize(
    ("sql", "word"),
    [
        ("\\! ls", "\\"),
        ("select 1 \\gexec", "\\"),
        ("select 1;\n\\set AUTOCOMMIT on", "\\"),
        ("BEGIN", "begin"),
        ("select 1; commit", "commit"),
        ("  /* x */ END", "end"),
        ("start transaction", "start"),
        ("abort", "abort"),
        ("rollback", "rollback"),
        ("rollback work", "rollback"),
        ("prepare transaction 'x'", "prepare transaction"),
        ("commit prepared 'x'", "commit"),
        ("rollback prepared 'x'", "rollback"),
        ("SET x = 1", "set"),
        ("set local x = 1", "set"),
        ("reset all", "reset"),
        ("select set_config('a','b',false)", "set_config"),
        ("select PUBLIC.dblink_exec('x','y')", "dblink_exec"),
        ('select "dblink"(\'x\')', "dblink"),
        ("select pg_read_file ('/etc/passwd')", "pg_read_file"),
        ("select pg_terminate_backend(1)", "pg_terminate_backend"),
        ("copy t to '/tmp/x'", "copy"),
        ("copy t to program 'ls'", "copy"),
        ("copy t from stdin", "copy"),
        ("copy (select 1) to program 'ls'", "copy"),
        ("do $$ begin null; end $$", "do"),
        ("select 'unterminated", "unterminated"),
        ("select $a$ x", "unterminated"),
        ('select "x', "unterminated"),
        ("/* x", "unterminated"),
        ("", "empty"),
        ("  -- only a comment\n", "empty"),
        (";;", "empty"),
    ],
)
def test_refused(sql, word):
    with pytest.raises(PsqlRefused) as refused:
        check_sql(sql)

    assert refused.value.word == word


@pytest.mark.parametrize(
    "sql",
    [
        "select 1",
        "select 1;",
        "select 'a\\b'",
        "select E'it\\'s \\\\'",
        "select 'COMMIT; \\!'",
        "select $$ commit; \\! $$",
        "select $t$ $$ \\ $t$",
        "select a$b$c from t",
        "select $1",
        "-- commit\nselect 1",
        "/* /* commit */ \\ */ select 1",
        'select "commit\\" from t',
        "update t set a = 1",
        "select case when a then 1 end from t",
        "select start from cal",
        "savepoint s; rollback to s",
        "rollback to savepoint s",
        "rollback work to s",
        "prepare p as select 1",
        "copy t to stdout",
        "copy (select 1) to stdout with csv",
        "explain analyze select 1",
        "select pg_stat_get_activity(1)",
        "select dblink from t",
        # Postgres's and the switch's business, not the guard's: the spec says so.
        "drop table t",
        "with a as (select 1) select * from a",
    ],
)
def test_allowed(sql):
    check_sql(sql)


PG_ENV = re.compile(r"^(PG[A-Z]+)=(.*)$", re.M)


def _fake_psql(tmp_path, body=None):
    """A `psql` first on PATH that writes down what it was given."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "psql"
    log = tmp_path / "psql.log"
    fake.write_text(
        body
        or (
            "#!/bin/sh\n"
            f'{{ echo "pid=$$"; echo "argv=$*"; /usr/bin/env; }} > "{log}"\n'
        ),
        encoding="utf-8",
    )
    fake.chmod(0o755)

    return bin_dir, log


def _run_runner(tmp_path, bin_dir, config=None, read_only="1"):
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    if config is not None:
        path = tmp_path / "odoo.conf"
        path.write_text(config, encoding="utf-8")
        env["ODOO_RC"] = str(path)

    return subprocess.run(
        [sys.executable, "-I", "-c", RUNNER_SOURCE, "somedb", read_only, "32000", "15000", "32000"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


CONFIG = (
    "[options]\ndb_host = dbhost\ndb_port = 5433\ndb_user = someone\n"
    "db_password = s3cret%word\n"
)


def _recorded(log) -> dict:
    text = log.read_text(encoding="utf-8")

    return {
        "pid": re.search(r"^pid=(\d+)$", text, re.M).group(1),
        "argv": re.search(r"^argv=(.*)$", text, re.M).group(1),
        "env": dict(PG_ENV.findall(text)),
    }


def test_runner_hands_the_config_to_psql_by_environment(tmp_path):
    bin_dir, log = _fake_psql(tmp_path)
    done = _run_runner(tmp_path, bin_dir, CONFIG)
    assert done.returncode == 0, done.stderr
    seen = _recorded(log)
    assert seen["env"]["PGHOST"] == "dbhost"
    assert seen["env"]["PGPORT"] == "5433"
    assert seen["env"]["PGUSER"] == "someone"
    assert seen["env"]["PGDATABASE"] == "somedb"
    assert seen["env"]["PGPASSWORD"] == "s3cret%word"
    assert "-X" in seen["argv"].split()
    assert "-w" in seen["argv"].split()
    assert "ON_ERROR_STOP=1" in seen["argv"]


def test_runner_keeps_the_password_out_of_argv(tmp_path):
    bin_dir, log = _fake_psql(tmp_path)
    done = _run_runner(tmp_path, bin_dir, CONFIG)
    assert "s3cret" not in _recorded(log)["argv"]
    assert "s3cret" not in done.stdout


def test_runner_treats_false_in_the_config_as_unset(tmp_path):
    bin_dir, log = _fake_psql(tmp_path)
    _run_runner(tmp_path, bin_dir, "[options]\ndb_host = False\ndb_password = False\n")
    seen = _recorded(log)
    assert seen["env"]["PGHOST"] == "localhost"
    assert seen["env"]["PGPORT"] == "5432"
    assert seen["env"]["PGUSER"] == "odoo"
    assert "PGPASSWORD" not in seen["env"]


def test_read_only_call_sets_the_default_and_timeouts(tmp_path):
    bin_dir, log = _fake_psql(tmp_path)
    _run_runner(tmp_path, bin_dir, CONFIG, read_only="1")
    options = _recorded(log)["env"]["PGOPTIONS"]
    assert "-c default_transaction_read_only=on" in options
    assert "-c statement_timeout=32000" in options
    assert "-c lock_timeout=15000" in options
    assert "-c idle_in_transaction_session_timeout=32000" in options


def test_writing_call_leaves_the_read_only_default_out(tmp_path):
    bin_dir, log = _fake_psql(tmp_path)
    _run_runner(tmp_path, bin_dir, CONFIG, read_only="0")
    options = _recorded(log)["env"]["PGOPTIONS"]
    assert "read_only" not in options
    assert "-c statement_timeout=32000" in options


def test_runner_prints_the_pid_psql_will_have(tmp_path):
    bin_dir, log = _fake_psql(tmp_path)
    done = _run_runner(tmp_path, bin_dir, CONFIG)
    assert done.stdout.strip() == f"{PID_PREFIX}{_recorded(log)['pid']}"


def test_runner_says_when_psql_is_missing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    done = _run_runner(tmp_path, empty, CONFIG)
    assert done.stdout.strip() == f"{MISSING_PREFIX}psql"


@pytest.mark.skipif(
    any(os.path.exists(p) for p in ("/etc/odoo/odoo.conf", "/etc/odoo.conf", "/opt/odoo.conf")),
    reason="this machine has an Odoo config of its own",
)
def test_runner_says_when_no_config_is_found(tmp_path):
    bin_dir, _log = _fake_psql(tmp_path)
    done = _run_runner(tmp_path, bin_dir)
    assert done.stdout.strip() == f"{MISSING_PREFIX}odoo config"


def test_runner_never_prints_a_config_it_cannot_parse(tmp_path):
    bin_dir, _log = _fake_psql(tmp_path)
    done = _run_runner(tmp_path, bin_dir, "db_password = s3cret\nnot a config\n")
    assert "s3cret" not in done.stdout + done.stderr
    assert done.stdout.startswith(MISSING_PREFIX)


CONFIG_PATH = re.compile(r"""["'](/[^"']*\.conf|~/\.odoorc)["']""")


def test_runner_uses_the_same_config_candidates_as_the_probe():
    assert set(CONFIG_PATH.findall(RUNNER_SOURCE)) == set(CONFIG_PATH.findall(PROBE_SOURCE))
    assert set(CONFIG_PATH.findall(RUNNER_SOURCE))


def test_runner_uses_only_the_standard_library():
    names = set()
    for node in ast.walk(ast.parse(RUNNER_SOURCE)):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module.split(".")[0])
    assert names <= sys.stdlib_module_names, names - sys.stdlib_module_names


def test_build_command_for_docker():
    target = Target(kind=DOCKER, container="odoo17", database="somedb")
    argv = build_command(target, read_only=True, timeout=30)
    assert argv[:6] == [docker_bin(), "exec", "-i", "odoo17", "python3", "-c"]
    assert argv[6] == RUNNER_SOURCE
    assert argv[7:] == ["somedb", "1", "32000", "15000", "32000"]
    assert build_command(target, read_only=False, timeout=30)[8] == "0"


@pytest.mark.parametrize(
    "target",
    [
        Target(kind=ODOOSH, build="12345", host="x.odoo.com"),
        Target(kind=SSH, label="odoo17"),
        # A card's name is free text and can equal a container's.
        Target(kind=SSH, label="odoo17", container="odoo17", database="somedb"),
    ],
)
def test_build_command_refuses_odoosh_and_ssh(target):
    with pytest.raises(PsqlNotAvailable):
        build_command(target, read_only=True, timeout=30)
