import pytest

from odoo_sheller.psql import PsqlRefused, check_sql


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
