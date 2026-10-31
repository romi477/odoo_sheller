"""One transaction through `psql`, for when the Odoo shell cannot help.

`env.cr.execute` in a session stays the normal way to run SQL. This is for a
registry that does not load and a session that is stuck. `psql` is a much bigger
hammer than the shell, so what reaches it is read here first.

`check_sql` is an accident guard, not a security boundary: daemon and agent run
as the same user, and a function already in the database can do what no reading
of the text would show.
"""

import asyncio
import logging
import re
import secrets

from odoo_sheller.transport import DOCKER, Target, docker_bin, send_signal, spawn

logger = logging.getLogger(__name__)


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


MAX_OUTPUT = 2 * 1024 * 1024
MAX_ERROR = 64 * 1024
_READ_SIZE = 65536
# A `docker exec … kill` answers at once. The deadline it serves has already
# passed, and the MCP side stops waiting soon after.
_SIGNAL_TIMEOUT = 2.0


class PsqlUnavailable(Exception):
    """Something the call needs is not in the container."""

    def __init__(self, missing: str):
        super().__init__(missing)
        self.missing = missing


class PsqlTargetGone(Exception):
    """The container is not there, or not running."""


class PsqlTimedOut(Exception):
    """`committed` is None when a COMMIT had been sent: nobody can say any more."""

    def __init__(self, committed: bool | None, output: str):
        super().__init__("deadline passed")
        self.committed = committed
        self.output = output


class _Call:
    """One process, fed in two steps.

    The agent's SQL goes first, with a marker after it. The marker coming back
    means every statement passed, since `ON_ERROR_STOP` ends `psql` at the first
    error. Only then is `COMMIT` written, so nothing already in `psql`'s input
    can reach one: if the daemon dies, stdin closes and the transaction goes.
    """

    def __init__(self, proc, sql, read_only, commit, nonce, signal):
        self.proc = proc
        self.sql = sql
        self.read_only = read_only
        self.commit = commit
        self.sql_marker = f"{nonce}-sql".encode()
        self.commit_marker = f"{nonce}-commit".encode()
        self.signal = signal
        self.decided = asyncio.Event()
        self.pid: int | None = None
        self.missing: str | None = None
        self.started = False
        self.past_sql = False
        self.commit_sent = False
        self.commit_seen = False
        self.truncated = False
        self.output = bytearray()
        self.errors = bytearray()
        self.begin_stripped = False

    def text(self) -> str:

        return bytes(self.output).decode("utf-8", errors="replace")

    def _take(self, line: bytes) -> None:
        """One whole line of stdout, newline included."""
        bare = line.rstrip(b"\n")
        if not self.started:
            self.started = True
            text = bare.decode("utf-8", errors="replace")
            if text.startswith(MISSING_PREFIX):
                self.missing = text[len(MISSING_PREFIX) :]

                return
            if text.startswith(PID_PREFIX):
                self.pid = int(text[len(PID_PREFIX) :])

                return
        if self.past_sql:
            self.commit_seen = self.commit_seen or bare == self.commit_marker

            return
        if bare == self.sql_marker:
            self.past_sql = True
            self.decided.set()

            return
        if not self.begin_stripped and bare == b"BEGIN":
            self.begin_stripped = True

            return
        room = MAX_OUTPUT - len(self.output)
        self.output += line[:room]
        if len(line) > room:
            self.truncated = True

    async def read_stdout(self) -> None:
        held = b""
        try:
            while chunk := await self.proc.stdout.read(_READ_SIZE):
                held += chunk
                *lines, held = held.split(b"\n")
                for line in lines:
                    self._take(line + b"\n")
                    if self.truncated:

                        return
                if len(held) > MAX_OUTPUT:
                    self._take(held)

                    return
            if held:
                self._take(held)
        finally:
            self.decided.set()

    async def read_stderr(self) -> None:
        while chunk := await self.proc.stderr.read(_READ_SIZE):
            self.errors += chunk[: MAX_ERROR - len(self.errors)]

    async def _write(self, text: str) -> None:
        self.proc.stdin.write(text.encode("utf-8", errors="surrogateescape"))
        await self.proc.stdin.drain()

    async def feed(self) -> None:
        begin = "BEGIN READ ONLY;" if self.read_only else "BEGIN;"
        marker = self.sql_marker.decode()
        try:
            # The newline and the `;` close the agent's text whatever it ended
            # in, a comment or no semicolon. Left open, `psql` would hold it in
            # its buffer past the meta-command and send it joined to `COMMIT`.
            await self._write(
                f"\\set AUTOCOMMIT off\n{begin}\n{self.sql}\n;\n\\echo {marker}\n"
            )
            await self.decided.wait()
            if not self.past_sql or self.truncated:

                return
            if self.commit:
                self.commit_sent = True
                await self._write(f"COMMIT;\n\\echo {self.commit_marker.decode()}\n")
            else:
                await self._write("ROLLBACK;\n")
        except (BrokenPipeError, ConnectionResetError):

            return
        finally:
            self.proc.stdin.close()

    async def stop(self) -> None:
        """Killing a `docker exec` client leaves its process running, so `psql` first."""
        if self.pid is not None and self.proc.returncode is None:
            try:
                await self.signal(self.pid, "TERM")
            except Exception:  # noqa: BLE001 - the client is killed below regardless
                logger.warning("could not signal psql %s", self.pid)
        if self.proc.returncode is None:
            self.proc.kill()
        await self.proc.wait()

    def outcome(self) -> dict:
        code = self.proc.returncode
        errors = bytes(self.errors).decode("utf-8", errors="replace").strip()
        if self.missing:
            raise PsqlUnavailable(self.missing)
        if self.pid is None:
            if "No such container" in errors or "is not running" in errors:
                raise PsqlTargetGone(errors)
            if code in (125, 126, 127) and "python3" in errors:
                raise PsqlUnavailable("python3")
        if self.commit_seen and code == 0:
            committed: bool | None = True
        elif self.commit_sent:
            committed = None
        else:
            committed = False

        return {
            "output": self.text(),
            "error": errors if code and not self.truncated else None,
            "committed": committed,
            "rolled_back": committed is False,
            "truncated": self.truncated,
            "mode": "read_only" if self.read_only else "read_write",
        }


async def run_transaction(
    target: Target,
    sql: str,
    *,
    read_only: bool,
    commit: bool,
    timeout: float,
    argv: list[str] | None = None,
    nonce: str | None = None,
    signal=None,
) -> dict:
    """One call: one process, one transaction. No path leaves a live process."""
    argv = argv or build_command(target, read_only, timeout)
    signal = signal or (
        lambda pid, name: send_signal(target, pid, name, timeout=_SIGNAL_TIMEOUT)
    )
    try:
        proc = await spawn(argv)
    except FileNotFoundError:
        raise PsqlUnavailable("docker") from None
    call = _Call(proc, sql, read_only, commit, nonce or secrets.token_hex(16), signal)
    tasks = [
        asyncio.create_task(call.read_stdout()),
        asyncio.create_task(call.read_stderr()),
        asyncio.create_task(call.feed()),
    ]
    reader = tasks[0]

    async def finished() -> None:
        await reader
        if call.truncated:
            await call.stop()
        await asyncio.gather(*tasks)
        await proc.wait()

    try:
        await asyncio.wait_for(finished(), timeout)
    except TimeoutError:
        raise PsqlTimedOut(None if call.commit_sent else False, call.text()) from None
    finally:
        await call.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    return call.outcome()
