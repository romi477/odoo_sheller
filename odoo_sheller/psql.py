"""One transaction through `psql`, for when the Odoo shell cannot help.

`env.cr.execute` in a session stays the normal way to run SQL. This is for a
registry that does not load and a session that is stuck. `psql` is a much bigger
hammer than the shell, so what reaches it is read here first.

`check_sql` is an accident guard, not a security boundary: daemon and agent run
as the same user, and a function already in the database can do what no reading
of the text would show.
"""

import asyncio
import itertools
import logging
import re
import secrets

from odoo_sheller.transport import DOCKER, Target, docker_bin, send_signal, spawn

logger = logging.getLogger(__name__)


class PsqlError(Exception):
    """A call that did not get an answer. `journal_id` is set once there is a file."""

    code = "psql_error"
    journal_id: str | None = None


class PsqlRefused(PsqlError):
    """The text holds something that would defeat the transaction."""

    code = "psql_refused"

    def __init__(self, word: str):
        super().__init__(word)
        self.word = word


# No rule for this exists to reuse. It travels by environment and never as an
# argument, so this is about a name that could not be one.
# Unicode: a real one here is called `integra_db_19ś`.
DATABASE_RE = re.compile(r"\w[\w.-]{0,62}")

# Refused as the first word of a statement. `SET` inside an `UPDATE` and `END`
# inside a `CASE` are the reason it is only the first word.
_CONTROL = frozenset({"begin", "start", "commit", "end", "abort", "set", "reset", "do", "load"})
# A routine's body is one literal to the reading below, so what it calls is never
# seen: `set_config` in one lifts the server's deadline for the rest of the
# session, and so does a routine's own `SET` clause.
_ROUTINES = frozenset({"function", "procedure", "routine"})
# `ALTER … SET` on these outlives the call, and with commit=True lifts the
# ceiling for every later one.
_SETTINGS_HOLDERS = frozenset({"database", "role", "user", "system"})
# Refused anywhere as a call. Each leaves the transaction or touches the server;
# the role in `db_user` is often a superuser in a local Postgres image. Families by
# prefix, because the whole of `dblink` commits on a connection of its own and new
# ones keep arriving; still not a list that claims to be complete.
_FUNCTIONS = frozenset(
    {
        "set_config",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
    }
)
_FUNCTION_PREFIXES = ("dblink", "pg_read_", "pg_ls_", "pg_file_")

# psql's own classes (`psqlscan.l`), not Python's: it reads bytes, every byte from
# 0x80 up is an identifier character, and only ASCII is space. Where the two
# disagree, a `€` or a no-break space hides a statement from this reading.
_SPACE = " \t\n\r\f"
_IDENT_START = r"A-Za-z_\x80-\U0010FFFF"
_IDENT = re.compile(rf"[{_IDENT_START}][{_IDENT_START}0-9$]*")
_NUMBER = re.compile(r"[0-9][0-9.]*")
_DOLLAR_TAG = re.compile(rf"\$(?:[{_IDENT_START}][{_IDENT_START}0-9]*)?\$")
# psql reads a line with `fgets` and keeps it as a C string: a NUL drops the rest
# of the line, so what this sees after one is not what runs.
_CONTROL_CHAR = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")

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


def _skip_line_comment(sql: str, start: int) -> int:
    """It ends at a carriage return as well, so `; -- x\\rcommit;` is a commit."""
    at = start
    while at < len(sql) and sql[at] not in "\n\r":
        at += 1

    return at


def _refuse_continuation(sql: str, start: int) -> None:
    """A quote after an `E''` and a newline continues it, still reading backslashes.

    So `E'a'\\n'\\'...'` closes somewhere other than where a second, plain string
    would. Refused rather than followed: nobody needs it here. Comments count too,
    which is stricter than psql, and no one is the worse for it.
    """
    at = start
    while at < len(sql):
        if sql[at] in _SPACE:
            at += 1
        elif sql.startswith("--", at):
            at = _skip_line_comment(sql, at)
        elif sql.startswith("/*", at):
            at = _skip_block_comment(sql, at + 2)
        else:
            break
    if sql[at : at + 1] == "'":
        raise PsqlRefused("string continuation")


def _tokens(sql: str) -> list[str]:
    """Words in lower case and the marks above; literals and comments gone.

    A backslash outside all of those is a `psql` meta-command, whatever follows.
    """
    if _CONTROL_CHAR.search(sql):
        raise PsqlRefused("control character")
    tokens: list[str] = []
    at = 0
    while at < len(sql):
        char = sql[at]
        if char in _SPACE:
            at += 1
        elif sql.startswith("--", at):
            at = _skip_line_comment(sql, at)
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
            if escapes:
                _refuse_continuation(sql, at)
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
            # Its name is spelled in escapes: `U&"dblink\\005fexec"`. Not decoded,
            # because nothing here needs one.
            if ident.group() in ("u", "U") and sql.startswith('&"', ident.end()):
                raise PsqlRefused('U&"')
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


def _check_parentheses(tokens: list[str]) -> None:
    """Counted as psql counts them, where a `)` with nothing open changes nothing.

    With one left open, `psql` holds every line back, past a meta-command. The
    daemon's marker would then come back first, and mean nothing.
    """
    depth = 0
    for token in tokens:
        if token == _OPEN:
            depth += 1
        elif token == _CLOSE and depth:
            depth -= 1
    if depth:
        raise PsqlRefused("unclosed parenthesis")


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
    if first == "create" and {"function", "procedure"} & set(statement[1:4]) and "begin" in statement:
        raise PsqlRefused("begin atomic")
    if first == "create" and (kind := next((w for w in statement[1:4] if w in _ROUTINES), None)):
        raise PsqlRefused(f"create {kind}")
    if (
        first == "alter"
        and rest[:1]
        and (rest[0] in _ROUTINES or rest[0] in _SETTINGS_HOLDERS)
        and {"set", "reset"} & set(_top_level(statement))
    ):
        raise PsqlRefused(f"alter {rest[0]}")
    if first == "rollback":
        # A savepoint cannot leave the transaction; the rest of `rollback` can.
        if "to" not in rest:
            raise PsqlRefused(first)
    elif first == "prepare" and rest[:1] == ["transaction"]:
        raise PsqlRefused("prepare transaction")
    elif first == "copy":
        words = _top_level(statement)
        allowed = any(a == "to" and b == "stdout" for a, b in itertools.pairwise(words))
        if not allowed or "from" in words or "program" in words:
            raise PsqlRefused(first)


def _check_settings_write(tokens: list[str]) -> None:
    """`UPDATE pg_settings` is `set_config` by another name: a rule rewrites it into one.

    With the switch on it would lift the server's own deadline, so a statement
    goes on running after the daemon has stopped `psql`, or turn off
    `standard_conforming_strings`, after which `psql` reads the lines that follow
    differently from the reading above. Reading the view is untouched.
    """
    for at, token in enumerate(tokens):
        if token != "update":
            continue
        target = [word for word in tokens[at + 1 : at + 4] if word not in ("only", "pg_catalog")]
        if target[:1] == ["pg_settings"]:
            raise PsqlRefused("pg_settings")


def check_sql(sql: str) -> None:
    """Raises `PsqlRefused` unless this text may be handed to `psql`."""
    tokens = _tokens(sql)
    statements = _statements(tokens)
    if not statements:
        raise PsqlRefused("empty")
    _check_parentheses(tokens)
    for statement in statements:
        _check_keywords(statement)
    _check_settings_write(tokens)
    for token, following in itertools.pairwise(tokens):
        if following == _OPEN and (token in _FUNCTIONS or token.startswith(_FUNCTION_PREFIXES)):
            raise PsqlRefused(token)


class PsqlNotAvailable(PsqlError):
    """This kind of target has no `psql` tool, whatever it is called."""

    code = "psql_not_available"


class PsqlCommitNotAllowed(PsqlError):
    """`commit=True` on a container no human has opened."""

    code = "commit_not_allowed"


class PsqlBusy(PsqlError):
    """A call is running on this container. Rejected, never queued."""

    code = "psql_busy"


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
              "-c idle_in_transaction_session_timeout=%s "
              "-c standard_conforming_strings=on" % (statement_ms, lock_ms, idle_ms))
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


# Past this the MCP side has stopped waiting, and a lost answer to a commit is the
# one thing an agent could not recover from. Long work is `exec`'s.
MAX_TIMEOUT = 35.0
MAX_OUTPUT = 2 * 1024 * 1024
MAX_ERROR = 64 * 1024
_READ_SIZE = 65536
# A `docker exec … kill` answers at once, and a killed client ends at once. The
# deadline these serve has already passed, and `MAX_TIMEOUT` plus both has to stay
# inside the MCP side's wait.
_SIGNAL_TIMEOUT = 1.0
_STOP_TIMEOUT = 2.0
_EXIT_SCRIPT_ERROR = 3


class PsqlUnavailable(PsqlError):
    """Something the call needs is not in the container."""

    code = "psql_unavailable"

    def __init__(self, missing: str):
        super().__init__(missing)
        self.missing = missing


class PsqlTargetGone(PsqlError):
    """The container is not there, or not running."""

    code = "not_found"


class PsqlTimedOut(PsqlError):
    """`committed` is None when a COMMIT had been sent: nobody can say any more."""

    code = "psql_timeout"

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
        self.rollback_sent = False
        self.reaped = False
        self.commit_tag = False
        self.commit_seen = False
        self.truncated = False
        self.output = bytearray()
        self.errors = bytearray()
        self.begin_stripped = False
        self.stopping: asyncio.Task | None = None

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
            # `\\echo` runs only after COMMIT has answered, and the tag says how:
            # Postgres answers ROLLBACK to a COMMIT in an aborted transaction.
            self.commit_tag = self.commit_tag or bare == b"COMMIT"
            self.commit_seen = self.commit_seen or (self.commit_tag and bare == self.commit_marker)

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
            self.decided.set()

    async def read_stdout(self) -> None:
        held = b""
        try:
            while chunk := await self.proc.stdout.read(_READ_SIZE):
                # Past the cap the bytes are thrown away, but they are still read:
                # a pipe left unread is paused, never reaches its end, and then
                # waiting for the process waits for ever.
                if self.truncated:
                    continue
                held += chunk
                *lines, held = held.split(b"\n")
                for line in lines:
                    self._take(line + b"\n")
                    if self.truncated:
                        held = b""
                        self.stopping = asyncio.create_task(self.stop())
                        break
                if len(held) > MAX_OUTPUT:
                    self._take(held)
                    held = b""
                    self.stopping = asyncio.create_task(self.stop())
            if held:
                self._take(held)
        finally:
            self.decided.set()

    async def read_stderr(self) -> None:
        while chunk := await self.proc.stderr.read(_READ_SIZE):
            self.errors += chunk[: MAX_ERROR - len(self.errors)]

    async def _write(self, text: str) -> None:
        self.proc.stdin.write(text.encode("utf-8", errors="replace"))
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
                self.rollback_sent = True
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
        # Bounded: a caller is waiting behind this, and a container stays busy
        # until it returns.
        try:
            await asyncio.wait_for(self.proc.wait(), _STOP_TIMEOUT)
        except TimeoutError:
            logger.warning("docker exec for psql %s did not end", self.pid)

    def settled(self) -> bool:
        """Whether the outcome is known whatever happens to the process now.

        A commit the server answered stays; a transaction told to roll back, or
        killed before it got there, goes either way.
        """

        return self.commit_seen or (self.rollback_sent and not self.truncated)

    def committed(self) -> bool | None:
        """True on the server's word, False if it never got there or said no."""
        if self.commit_seen:

            return True
        if not self.commit_sent:

            return False

        # 3 is `ON_ERROR_STOP` ending the script on an error from the server, so a
        # COMMIT that failed. Anything else, such as a lost connection, says nothing.
        return False if self.proc.returncode == _EXIT_SCRIPT_ERROR else None

    def outcome(self) -> dict:
        code = self.proc.returncode
        errors = bytes(self.errors).decode("utf-8", errors="replace").strip()
        if self.missing:
            raise PsqlUnavailable(self.missing)
        if self.pid is None:
            # The script never ran, so this is Docker talking, and it says some of
            # this on stdout.
            said = f"{errors}\n{self.text()}"
            if "No such container" in said or "is not running" in said:
                raise PsqlTargetGone(said.strip())
            if code in (125, 126, 127) and "python3" in said:
                raise PsqlUnavailable("python3")
            errors = said.strip()
        committed = self.committed()

        return {
            "output": self.text(),
            "error": errors if code and not self.truncated and not self.reaped else None,
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
    """One call: one process, one transaction. No path leaves a live process.

    The deadline counts from here, not from the spawn: the caller's wait has
    already started, and starting `docker exec` can take seconds.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
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

    async def finished() -> None:
        await asyncio.gather(*tasks)
        await proc.wait()

    # `asyncio.wait`, not `wait_for`: a deadline must not cancel the readers, since
    # the process has yet to be stopped and its pipes have to keep draining.
    waiting = asyncio.create_task(finished())
    try:
        done, _pending = await asyncio.wait({waiting}, timeout=max(deadline - loop.time(), 0))
    finally:
        await call.stop()
        for task in (*tasks, waiting, call.stopping):
            if task is not None:
                task.cancel()
        await asyncio.gather(*tasks, waiting, return_exceptions=True)
    if not done:
        if not call.settled():
            raise PsqlTimedOut(
                True if call.commit_seen else None if call.commit_sent else False, call.text()
            )
        # The work was over and only `docker exec` was slow to go: an answer, with
        # nothing to say about the way it was stopped.
        call.reaped = True
    else:
        waiting.result()

    return call.outcome()
