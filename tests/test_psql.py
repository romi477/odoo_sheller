import ast
import asyncio
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from odoo_sheller.discovery import PROBE_SOURCE
from odoo_sheller.psql import (
    MAX_OUTPUT,
    MISSING_PREFIX,
    PID_PREFIX,
    RUNNER_SOURCE,
    PsqlNotAvailable,
    PsqlRefused,
    PsqlTargetGone,
    PsqlTimedOut,
    PsqlUnavailable,
    build_command,
    check_sql,
    run_transaction,
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
        ("select dblink_connect_u('x')", "dblink_connect_u"),
        ("select dblink_send_query('c','x')", "dblink_send_query"),
        ("select pg_stat_file('/etc/passwd')", "pg_stat_file"),
        ("select pg_ls_logdir()", "pg_ls_logdir"),
        ("select pg_file_write('/tmp/x','y',false)", "pg_file_write"),
        ("copy t to '/tmp/x'", "copy"),
        ("copy t to program 'ls'", "copy"),
        ("copy t from stdin", "copy"),
        ("copy (select 1) to program 'ls'", "copy"),
        ("do $$ begin null; end $$", "do"),
        ("select 'unterminated", "unterminated"),
        ("select $a$ x", "unterminated"),
        ('select "x', "unterminated"),
        ("/* x", "unterminated"),
        # What psql reads differently from a first attempt at reading it.
        ("select 1; -- x\rcommit;", "commit"),
        ("select 1 -- x\r\\! id", "\\"),
        ("select 1 as €$a$; commit; -- $a$", "commit"),
        ("select 1 as\u00a0x$a$; commit; -- $a$", "commit"),
        ("select $€$ ' $€$; commit; -- '", "commit"),
        ("select 1; \0 /*\ncommit; -- */", "control character"),
        ("select '\0'", "control character"),
        ('select U&"dblink\\005fexec"(1)', 'U&"'),
        ('select u&"set!005fconfig" UESCAPE \'!\' (1)', 'U&"'),
        ("select E'a'\n'\\'  ' ; commit ; --'", "string continuation"),
        ("select E'a' /* x */ 'b'", "string continuation"),
        # psql holds these back past the daemon's `\\echo`, so the marker would come
        # first and say nothing about them.
        ("select (1", "unclosed parenthesis"),
        ("insert into t values (1;", "unclosed parenthesis"),
        (") (", "unclosed parenthesis"),
        ("create function f() returns int begin atomic select 1;", "begin atomic"),
        ("create or replace procedure p() begin atomic select 1;", "begin atomic"),
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
        "select (1)",
        "select '('",
        "select 1)",
        "create function f() returns int as $$ begin return 1; end $$ language plpgsql",
        "select 1 as €",
        # `1`, then a dollar quote, as psql reads it too: the commit is in the quote.
        "select 1$a$; commit; -- $a$",
        "select $€$ x $€$",
        "select 1 -- x\r\nfrom t",
        "select\t1\f",
        "select U&'d\\0061t'",
        "select 'a'\n'b'",
        "select E'a', 'b'",
    ],
)
def test_allowed(sql):
    check_sql(sql)


PG_ENV = re.compile(r"^(PG[A-Z]+)=(.*)$", re.MULTILINE)


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
        check=False,
    )


CONFIG = (
    "[options]\ndb_host = dbhost\ndb_port = 5433\ndb_user = someone\n"
    "db_password = s3cret%word\n"
)


def _recorded(log) -> dict:
    text = log.read_text(encoding="utf-8")

    return {
        "pid": re.search(r"^pid=(\d+)$", text, re.MULTILINE).group(1),
        "argv": re.search(r"^argv=(.*)$", text, re.MULTILINE).group(1),
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
    assert "-c standard_conforming_strings=on" in options


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


DOUBLE = [sys.executable, str(Path(__file__).parent / "psql_double.py")]
LOCAL = Target(kind=DOCKER, container="odoo17", database="somedb")


class Double:
    def __init__(self, tmp_path):
        self.log = tmp_path / "double.log"
        self.signals = []
        self.signal_delay = 0.0

    async def signal(self, pid, name):
        self.signals.append((pid, name))
        await asyncio.sleep(self.signal_delay)

    def lines(self):

        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def run(self, sql, read_only=True, commit=False, timeout=10.0):

        return asyncio.run(
            run_transaction(
                LOCAL, sql, read_only=read_only, commit=commit, timeout=timeout,
                argv=DOUBLE, nonce="N", signal=self.signal,
            )
        )  # fmt: skip


@pytest.fixture
def double(tmp_path, monkeypatch):
    made = Double(tmp_path)
    monkeypatch.setenv("PSQL_DOUBLE_LOG", str(made.log))
    monkeypatch.delenv("PSQL_DOUBLE_MODE", raising=False)

    return made


def test_rollback_is_the_end_of_a_plain_call(double):
    done = double.run("select 1;")
    assert double.lines()[-1] == "ROLLBACK;"
    assert done["committed"] is False
    assert done["rolled_back"] is True
    assert done["error"] is None
    assert done["mode"] == "read_only"


def test_read_only_call_begins_read_only(double):
    double.run("select 1;")
    assert "BEGIN READ ONLY;" in double.lines()


def test_writing_call_begins_plainly(double):
    done = double.run("select 1;", read_only=False)
    assert "BEGIN;" in double.lines()
    assert done["mode"] == "read_write"


def test_autocommit_is_off_before_anything_else(double):
    double.run("select 1;")
    assert double.lines()[0] == "\\set AUTOCOMMIT off"


def test_commit_is_sent_only_after_the_marker(double):
    done = double.run("HOLD;", read_only=False, commit=True)
    lines = double.lines()
    assert "early" not in lines
    assert lines.index("\\echo N-sql") < lines.index("COMMIT;")
    assert lines[-1] == "\\echo N-commit"
    assert done["committed"] is True
    assert done["rolled_back"] is False


def test_failed_statement_never_sees_commit(double):
    done = double.run("FAIL;", read_only=False, commit=True)
    assert "COMMIT;" not in double.lines()
    assert "boom" in done["error"]
    assert done["committed"] is False
    assert done["rolled_back"] is True


def test_sql_without_a_semicolon_stays_separate(double):
    done = double.run("select 1", read_only=False, commit=True)
    assert "merged" not in double.lines()
    assert done["committed"] is True


def test_sql_ending_in_a_comment_stays_separate(double):
    done = double.run("select 1 -- the end", read_only=False, commit=True)
    assert "merged" not in double.lines()
    assert done["committed"] is True


def test_forged_marker_does_not_count(double):
    done = double.run("FORGE N-sql-old\n;\nFORGE x N-sql\n;\nselect 1;")
    assert "N-sql-old" in done["output"]
    assert "x N-sql" in done["output"]
    assert double.lines()[-1] == "ROLLBACK;"


def test_large_sql_does_not_deadlock(double):
    sql = "-- padding padding padding padding\n" * 30000 + "select 1;"
    done = double.run(sql, timeout=20.0)
    assert done["output"].count("-- padding") == 30000
    assert done["truncated"] is False


def test_invalid_utf8_comes_back_replaced(double):
    done = double.run("BINARY;")
    assert "�" in done["output"]


def test_output_cap_stops_and_never_commits(double):
    done = double.run("BIG;", read_only=False, commit=True, timeout=20.0)
    assert done["truncated"] is True
    assert done["committed"] is False
    assert done["rolled_back"] is True
    assert "COMMIT;" not in double.lines()
    assert len(done["output"].encode()) <= MAX_OUTPUT
    assert double.signals[0][1] == "TERM"


def test_deadline_signals_inside_and_raises(double):
    with pytest.raises(PsqlTimedOut) as timed_out:
        double.run("HANG;", timeout=0.5)
    pid, name = double.signals[0]
    assert name == "TERM"
    assert timed_out.value.committed is False
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_lost_commit_marker_is_unknown(double):
    done = double.run("NOCOMMITMARK;", read_only=False, commit=True)
    assert "COMMIT;" in double.lines()
    assert done["committed"] is None
    assert done["rolled_back"] is False


def test_missing_psql_is_named(double, monkeypatch):
    monkeypatch.setenv("PSQL_DOUBLE_MODE", "missing")
    with pytest.raises(PsqlUnavailable) as unavailable:
        double.run("select 1;")
    assert unavailable.value.missing == "psql"


def test_missing_python_is_named(double, monkeypatch):
    monkeypatch.setenv("PSQL_DOUBLE_MODE", "no_python")
    with pytest.raises(PsqlUnavailable) as unavailable:
        double.run("select 1;")
    assert unavailable.value.missing == "python3"


def test_gone_container_is_named(double, monkeypatch):
    monkeypatch.setenv("PSQL_DOUBLE_MODE", "gone")
    with pytest.raises(PsqlTargetGone):
        double.run("select 1;")


def test_output_excludes_the_daemons_own_lines(double):
    done = double.run("FORGE hello\n;")
    assert done["output"] == "hello\n"


def test_a_lone_surrogate_in_the_sql_does_not_crash_the_feed(double):
    # JSON can carry one, and a raised write would strand the call.
    done = double.run("select '\ud800';")
    assert double.lines()[-1] == "ROLLBACK;"
    assert done["error"] is None


def test_docker_saying_something_else_is_an_error_not_output(double, monkeypatch):
    monkeypatch.setenv("PSQL_DOUBLE_MODE", "docker_other")
    done = double.run("select 1;")
    assert "cannot exec" in done["error"]


def test_endless_output_is_truncated_not_timed_out(double):
    # Past the cap nobody needs the bytes, but somebody has to read them: a pipe
    # left unread never reaches its end, and the call waits out its deadline.
    double.signal_delay = 0.3
    started = time.monotonic()
    done = double.run("ENDLESS;", read_only=False, commit=True, timeout=15.0)
    assert done["truncated"] is True
    assert done["committed"] is False
    assert time.monotonic() - started < 10


def test_a_flood_during_the_deadline_cannot_hang_the_call(double):
    double.signal_delay = 1.0
    started = time.monotonic()
    with pytest.raises(PsqlTimedOut):
        double.run("LATEFLOOD;", timeout=0.3)
    assert time.monotonic() - started < 10
