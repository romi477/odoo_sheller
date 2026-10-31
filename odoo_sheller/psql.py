"""One transaction through `psql`, for when the Odoo shell cannot help.

`env.cr.execute` in a session stays the normal way to run SQL. This is for a
registry that does not load and a session that is stuck. `psql` is a much bigger
hammer than the shell, so what reaches it is read here first.

`check_sql` is an accident guard, not a security boundary: daemon and agent run
as the same user, and a function already in the database can do what no reading
of the text would show.
"""

import re

from odoo_sheller.transport import DOCKER, Target, docker_bin


class PsqlRefused(Exception):
    """The text holds something that would defeat the transaction."""

    def __init__(self, word: str):
        super().__init__(word)
        self.word = word


# Refused as the first word of a statement. `SET` inside an `UPDATE` and `END`
# inside a `CASE` are the reason it is only the first word.
_CONTROL = frozenset({"begin", "start", "commit", "end", "abort", "set", "reset", "do"})
# Refused anywhere as a call. Each leaves the transaction or touches the server;
# the role in `db_user` is often a superuser in a local Postgres image.
_FUNCTIONS = frozenset(
    {
        "set_config",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "lo_import",
        "lo_export",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
    }
)

_IDENT = re.compile(r"[^\W\d][\w$]*")
_NUMBER = re.compile(r"\d[\w$.]*")
_DOLLAR_TAG = re.compile(r"\$(?:[^\W\d]\w*)?\$")

# What one token is, once its content no longer matters.
_LITERAL = "'"
_OPEN = "("
_CLOSE = ")"
_END = ";"


def _skip_quoted(sql: str, start: int, quote: str, escapes: bool = False) -> int:
    """The offset past the closing quote, a doubled quote being part of the text."""
    at = start
    while at < len(sql):
        char = sql[at]
        if escapes and char == "\\":
            at += 2
        elif char != quote:
            at += 1
        elif sql[at + 1 : at + 2] == quote:
            at += 2
        else:

            return at + 1
    raise PsqlRefused("unterminated")


def _skip_block_comment(sql: str, start: int) -> int:
    """Past a `/* */` that opened before `start`. Postgres nests them."""
    depth = 1
    at = start
    while at < len(sql):
        if sql.startswith("/*", at):
            depth += 1
            at += 2
        elif sql.startswith("*/", at):
            depth -= 1
            at += 2
            if not depth:

                return at
        else:
            at += 1
    raise PsqlRefused("unterminated")


def _tokens(sql: str) -> list[str]:
    """Words in lower case and the marks above; literals and comments gone.

    A backslash outside all of those is a `psql` meta-command, whatever follows.
    """
    tokens: list[str] = []
    at = 0
    while at < len(sql):
        char = sql[at]
        if char.isspace():
            at += 1
        elif sql.startswith("--", at):
            newline = sql.find("\n", at)
            at = len(sql) if newline < 0 else newline + 1
        elif sql.startswith("/*", at):
            at = _skip_block_comment(sql, at + 2)
        elif char == "\\":
            raise PsqlRefused("\\")
        elif char == "'":
            # `E'` is read as a word and a quote: the word is what says so.
            escapes = bool(tokens) and tokens[-1] == "e" and sql[at - 1] in "eE"
            if escapes:
                tokens.pop()
            at = _skip_quoted(sql, at + 1, "'", escapes)
            tokens.append(_LITERAL)
        elif char == '"':
            end = _skip_quoted(sql, at + 1, '"')
            tokens.append(sql[at + 1 : end - 1].lower())
            at = end
        elif char == "$" and (tag := _DOLLAR_TAG.match(sql, at)):
            closing = sql.find(tag.group(), tag.end())
            if closing < 0:
                raise PsqlRefused("unterminated")
            at = closing + len(tag.group())
            tokens.append(_LITERAL)
        elif ident := _IDENT.match(sql, at):
            tokens.append(ident.group().lower())
            at = ident.end()
        elif number := _NUMBER.match(sql, at):
            at = number.end()
            tokens.append(_LITERAL)
        else:
            if char in (_OPEN, _CLOSE, _END):
                tokens.append(char)
            at += 1

    return tokens


def _statements(tokens: list[str]) -> list[list[str]]:
    statements: list[list[str]] = [[]]
    for token in tokens:
        if token == _END:
            statements.append([])
        else:
            statements[-1].append(token)

    return [statement for statement in statements if statement]


def _top_level(statement: list[str]) -> list[str]:
    """The words outside any parentheses."""
    depth = 0
    kept = []
    for token in statement:
        if token == _OPEN:
            depth += 1
        elif token == _CLOSE:
            depth -= 1
        elif not depth:
            kept.append(token)

    return kept


def _check_keywords(statement: list[str]) -> None:
    first = statement[0]
    rest = statement[1:3]
    if first in _CONTROL:
        raise PsqlRefused(first)
    if first == "rollback":
        # A savepoint cannot leave the transaction; the rest of `rollback` can.
        if "to" not in rest:
            raise PsqlRefused(first)
    elif first == "prepare" and rest[:1] == ["transaction"]:
        raise PsqlRefused("prepare transaction")
    elif first == "copy":
        words = _top_level(statement)
        allowed = any(a == "to" and b == "stdout" for a, b in zip(words, words[1:]))
        if not allowed or "from" in words or "program" in words:
            raise PsqlRefused(first)


def check_sql(sql: str) -> None:
    """Raises `PsqlRefused` unless this text may be handed to `psql`."""
    tokens = _tokens(sql)
    statements = _statements(tokens)
    if not statements:
        raise PsqlRefused("empty")
    for statement in statements:
        _check_keywords(statement)
    for token, following in zip(tokens, tokens[1:]):
        if following == _OPEN and token in _FUNCTIONS:
            raise PsqlRefused(token)


class PsqlNotAvailable(Exception):
    """This kind of target has no `psql` tool, whatever it is called."""


MISSING_PREFIX = "OSPSQL-MISSING "
PID_PREFIX = "OSPSQL-PID "

# Runs in the container: stdlib only, and nothing in its argv is a secret. It
# goes in argv rather than on stdin, unlike the probe, because stdin stays open
# for `psql`. `execve` keeps the pid, so the one printed is the one to signal.
# The config's rules are the probe's, and a test keeps the candidates in step.
RUNNER_SOURCE = r'''
import configparser, os, shutil, sys

def missing(what):
    print("OSPSQL-MISSING " + what, flush=True)
    raise SystemExit(0)

psql = shutil.which("psql")
if not psql:
    missing("psql")

odoo_bin = None
for path in [shutil.which("odoo-bin"), "/opt/odoo/odoo-bin", "/usr/bin/odoo",
             "/odoo/odoo-bin", "/mnt/odoo/odoo-bin", "/usr/lib/python3/dist-packages/odoo-bin"]:
    if path and os.path.exists(path):
        odoo_bin = path
        break

config = None
for path in [os.environ.get("ODOO_RC"), "/etc/odoo/odoo.conf", "/etc/odoo.conf",
             "/opt/odoo.conf", os.path.join(os.path.dirname(odoo_bin or ""), "odoo.conf"),
             os.path.expanduser("~/.odoorc")]:
    if path and os.path.exists(path):
        config = path
        break
if not config:
    missing("odoo config")

def clean(value):
    if value is None or value in ("False", "false", ""):

        return None

    return value

# Raw: a `%` in a password is a password, not an interpolation. An error names
# its type only, because a parser's message quotes the file.
try:
    parser = configparser.RawConfigParser()
    parser.read(config)
    options = parser["options"] if parser.has_section("options") else {}
    host = clean(options.get("db_host")) or "localhost"
    port = clean(options.get("db_port")) or "5432"
    user = clean(options.get("db_user")) or "odoo"
    password = clean(options.get("db_password"))
except Exception as exc:
    missing("odoo config (%s)" % type(exc).__name__)

database, read_only, statement_ms, lock_ms, idle_ms = sys.argv[1:6]
pg_options = ("-c statement_timeout=%s -c lock_timeout=%s "
              "-c idle_in_transaction_session_timeout=%s" % (statement_ms, lock_ms, idle_ms))
if read_only == "1":
    pg_options += " -c default_transaction_read_only=on"

env = dict(os.environ)
env.update({"PGHOST": host, "PGPORT": port, "PGUSER": user, "PGDATABASE": database,
            "PGOPTIONS": pg_options})
env.pop("PGPASSWORD", None)
if password:
    env["PGPASSWORD"] = password

print("OSPSQL-PID %d" % os.getpid(), flush=True)
os.execve(psql, ["psql", "-X", "-w", "-P", "pager=off", "-v", "ON_ERROR_STOP=1"], env)
'''


def build_command(target: Target, read_only: bool, timeout: float) -> list[str]:
    """The command for one call, by the target's kind and never by a name.

    Past the daemon's own deadline by two seconds, so that it fires first and the
    server still clears up shortly after if nothing else does.
    """
    if target.kind != DOCKER:
        raise PsqlNotAvailable(target.kind)
    server_ms = str(int((timeout + 2) * 1000))
    lock_ms = str(int(timeout * 500))

    return [
        docker_bin(), "exec", "-i", target.container, "python3", "-c", RUNNER_SOURCE,
        target.database, "1" if read_only else "0", server_ms, lock_ms, server_ms,
    ]  # fmt: skip
