"""What a person may write on an ssh card: two lines, parsed, never run.

A remote server can be set up any way at all — which user runs Odoo, which
interpreter (a venv, usually), where `odoo-bin` and its config are — so the
daemon does not guess. The person who can log in writes it down, in two fields:

    Access   how to arrive as the right user
             ssh -i ~/.ssh/server.pem ubuntu@host sudo -n -u odoo -H
    Launch   what to run once there
             /opt/odoo/env/bin/python /opt/odoo/odoo-bin shell -c /opt/odoo/odoo.conf

Access is a *prefix*: it ends where a command may follow, and the daemon puts
`sh -c '<script>'` after it. That is not a style choice. `sudo` closes every
file descriptor above 2, and the whole transport rests on `exec 3<&0` keeping
the command pipe alive, so the script that does it has to run *inside* the user
switch. Typed into a terminal, "ssh, then sudo su, then su odoo" are three
commands in sequence; here they nest, and the equivalent is one line.

Both fields are **data, not code**. Each is tokenised, checked against a
whitelist — not a blacklist — and every token is quoted again on the way out;
nothing a person typed is interpolated into a script or handed to a local
shell. A field that holds a command line is, on the machine running the daemon,
a way to run commands (`ssh -o ProxyCommand=…` runs one locally), so the grammar
refuses what ssh would do on this machine rather than on the server.

What this is not for: the person who writes a recipe can already run anything
on their server, and the grammar does not stop them. It protects them from
typos and from text they did not write — a recipe pasted from a chat or a wiki —
and protects this machine from whatever else can reach the daemon.
"""

import os
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from odoo_sheller.names import check_ssh_name

HEREDOC_MARKER = "OSBOOT"

USER_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
# `[user@]host[:port]`, the form `-J` takes for each hop.
HOP_RE = re.compile(
    r"(?:(?P<user>[A-Za-z0-9_][A-Za-z0-9._-]*)@)?"
    r"(?P<host>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?::(?P<port>[0-9]{1,5}))?"
)
OPERATORS = set("();<>|&")
MAX_DATABASE = 63

# The only `-o` keys, lowercase. Each is something ssh needs to *reach* the
# server; none of them makes ssh do anything on this machine.
ALLOWED_OPTIONS = {
    "port": "Port",
    "identityfile": "IdentityFile",
    "user": "User",
    "proxyjump": "ProxyJump",
    "connecttimeout": "ConnectTimeout",
    "identitiesonly": "IdentitiesOnly",
}
_ALLOWED_LIST = ", ".join(ALLOWED_OPTIONS.values())

WHY_NOT = {
    # `-o` keys, lowercase.
    "proxycommand": "ProxyCommand runs a command on this machine",
    "localcommand": "LocalCommand runs a command on this machine",
    "permitlocalcommand": "PermitLocalCommand lets ssh run a command on this machine",
    "stricthostkeychecking": (
        "host key checking is not ours to weaken: an unknown host key has to "
        "fail, loudly — connect once from a terminal to trust it"
    ),
    "userknownhostsfile": (
        "the host key file is not ours to redirect: an unknown host key has to "
        "fail, loudly — connect once from a terminal to trust it"
    ),
}
# Short options, by letter.
SHORT_WHY_NOT = {
    "F": "-F reads a config file, whose options (ProxyCommand, LocalCommand…) are what this grammar refuses",
    "t": "-t allocates a pty, which would merge the frame stream into the log stream",
    "A": "-A forwards your ssh agent to the server",
    "X": "-X forwards X11",
    "Y": "-Y forwards X11",
    "f": "-f sends ssh to the background",
    "L": "-L is port forwarding",
    "R": "-R is port forwarding",
    "D": "-D is port forwarding",
}

# What a person types when they mean "become that user", and which of them
# keeps fd 3 is not verified — so only sudo is accepted.
OTHER_BECOMES = ("su", "runuser", "doas", "pkexec", "machinectl", "sg", "newgrp")


class RecipeError(ValueError):
    """A recipe that is not accepted, and why. The message is for the person.

    `field` says which of the card's fields it is about — access, launch or
    database — so a form can put the message beside the right one.
    """

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


def quote_all(tokens: Sequence[str]) -> str:
    """One string that a POSIX shell reads back as exactly these tokens."""

    return " ".join(shlex.quote(token) for token in tokens)


@dataclass(frozen=True)
class Become:
    user: str | None
    home: bool

    @property
    def argv(self) -> tuple[str, ...]:
        argv = ["sudo", "-n"]
        if self.user:
            argv += ["-u", self.user]
        if self.home:
            argv.append("-H")

        return tuple(argv)


@dataclass(frozen=True)
class Access:
    options: tuple[str, ...]
    user: str | None
    host: str
    port: int | None
    keys: tuple[str, ...]
    jump: str | None
    become: Become | None

    @property
    def destination(self) -> str:

        return f"{self.user}@{self.host}" if self.user else self.host

    def ssh_argv(self, remote: Sequence[str], base: Sequence[str] = ()) -> list[str]:
        """The local argv: ssh, our own options, the card's, `--`, the host, and
        the remote command as ONE string.

        `ssh host a b c` joins its arguments and the remote login shell parses
        the result again, so the remote command is quoted once, token by token,
        for that one parse. `--` ends the options before the destination.
        """
        tokens = [*(self.become.argv if self.become else ()), *remote]

        return ["ssh", *base, *self.options, "--", self.destination, quote_all(tokens)]


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    database: str | None
    database_in_launch: str | None
    config: str | None
    # The token just before `shell`: the script Odoo is started from, which is
    # where its own `odoo/release.py` is looked for.
    odoo_bin: str


def _split(text: object, field: str) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        raise RecipeError(f"{field} is empty")
    if "\x00" in text:
        raise RecipeError(f"{field} holds a NUL character")
    if "\n" in text or "\r" in text:
        raise RecipeError(
            f"{field} is one line: one command, not a script. Steps typed one "
            "after another into a terminal (ssh, then sudo su, then su odoo) "
            "nest here — they become a single `sudo -n -u odoo -H`"
        )
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # A `#` is part of a path or a password-ish token here, never a comment.
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise RecipeError(f"{field} has an unbalanced quote ({exc})") from None
    for token in tokens:
        if token and set(token) <= OPERATORS:
            raise RecipeError(
                f"{field} is one command: {token!r} would start another, and "
                "nothing here is run through a shell. Quote it if it is data"
            )

    return tokens


def _option_value(rest: list[str], index: int, flag: str) -> tuple[str, int]:
    """The value of `-X value` or `-Xvalue`, and where the next token is."""
    attached = rest[index][2:]
    if attached:

        return attached, index + 1
    if index + 1 >= len(rest):
        raise RecipeError(f"-{flag} needs a value")

    return rest[index + 1], index + 2


def _port(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535:
        raise RecipeError(f"{value!r} is not a port number (1-65535)")

    return int(value)


def _user(value: str, what: str = "user") -> str:
    if not USER_RE.fullmatch(value) or len(value) > 32:
        raise RecipeError(
            f"{what} {value!r} is not a user name: letters, digits, '.', '_' and '-'"
        )

    return value


def _key(value: str, is_file: Callable[[str], bool]) -> str:
    path = os.path.expanduser(value)
    if not os.path.isabs(path):
        raise RecipeError(
            f"key {value!r} is not an absolute path: write it in full, or "
            "starting with ~"
        )
    if "%" in path:
        raise RecipeError(f"key {value!r}: ssh would read % in a path as a token")
    if not is_file(path):
        raise RecipeError(f"key file {path} does not exist, or is not a file")

    return path


def _jump(value: str) -> str:
    hops = value.split(",")
    for hop in hops:
        match = HOP_RE.fullmatch(hop)
        if match is None:
            raise RecipeError(
                f"jump host {hop!r} is not [user@]host[:port] (several, comma-separated)"
            )
        if match["port"]:
            _port(match["port"])

    return value


class _Collected:
    def __init__(self):
        self.users: list[str] = []
        self.ports: list[int] = []
        self.keys: list[str] = []
        self.jump: str | None = None
        self.timeout: int | None = None
        self.identities_only: str | None = None


def _apply_named(collected: _Collected, name: str, value: str, is_file) -> None:
    """One `-o name=value`, or the long form of a short option."""
    key = name.lower()
    if key in WHY_NOT:
        raise RecipeError(f"-o {name} is not allowed: {WHY_NOT[key]}")
    if "forward" in key:
        raise RecipeError(
            f"-o {name} is not allowed: forwarding opens a channel or a port "
            "(or hands over an agent) that nothing here asked for"
        )
    if key not in ALLOWED_OPTIONS:
        raise RecipeError(
            f"-o {name} is not allowed here. Allowed: {_ALLOWED_LIST}"
        )
    if key == "port":
        collected.ports.append(_port(value))
    elif key == "user":
        collected.users.append(_user(value))
    elif key == "identityfile":
        collected.keys.append(_key(value, is_file))
    elif key == "proxyjump":
        collected.jump = _jump(value)
    elif key == "connecttimeout":
        if not re.fullmatch(r"[0-9]{1,3}", value) or not 1 <= int(value) <= 300:
            raise RecipeError(f"ConnectTimeout {value!r} is not a number of seconds (1-300)")
        collected.timeout = int(value)
    elif key == "identitiesonly":
        if value.lower() not in ("yes", "no"):
            raise RecipeError(f"IdentitiesOnly must be yes or no, not {value!r}")
        collected.identities_only = value.lower()


def _parse_dash_o(collected: _Collected, setting: str, is_file) -> None:
    match = re.fullmatch(r"\s*([A-Za-z0-9]+)\s*(?:[=\s]\s*(.*?))?\s*", setting)
    if match is None:
        raise RecipeError("-o needs a value in the form KEY=VALUE")
    name, value = match.group(1), match.group(2)
    if not value:
        raise RecipeError(f"-o {name} needs a value (-o {name}=VALUE)")
    _apply_named(collected, name, value, is_file)


def _parse_option(rest: list[str], index: int, collected: _Collected, is_file) -> int:
    token = rest[index]
    flag = token[1]
    if flag in SHORT_WHY_NOT:
        raise RecipeError(f"{token}: {SHORT_WHY_NOT[flag]}")
    if flag == "o":
        value, index = _option_value(rest, index, "o")
        _parse_dash_o(collected, value, is_file)

        return index
    if flag in "iplJ":
        value, index = _option_value(rest, index, flag)
        if flag == "i":
            collected.keys.append(_key(value, is_file))
        elif flag == "p":
            collected.ports.append(_port(value))
        elif flag == "l":
            collected.users.append(_user(value))
        else:
            collected.jump = _jump(value)

        return index
    raise RecipeError(
        f"{token}: ssh option -{flag} is not allowed here. Allowed: -i, -p, -l, -J, "
        f"and -o with {_ALLOWED_LIST}"
    )


def _parse_destination(token: str) -> tuple[str | None, str]:
    parts = token.split("@")
    if len(parts) > 2:
        raise RecipeError(f"destination {token!r} has more than one @")
    user = _user(parts[0], "destination user") if len(parts) == 2 else None
    try:
        host = check_ssh_name("host", parts[-1])
    except ValueError as exc:
        raise RecipeError(
            f"destination {token!r}: {exc}. Write a port with -p, not host:port"
        ) from None

    return user, host


def _parse_become(tokens: list[str]) -> Become:
    head = tokens[0]
    if head in OTHER_BECOMES:
        raise RecipeError(
            f"{head!r} is not supported: only sudo is, because only sudo has been "
            "checked to keep the command pipe (file descriptor 3) alive. Write "
            "`sudo -n -u USER -H`"
        )
    if head != "sudo":
        raise RecipeError(
            f"after the destination only a sudo prefix may follow, but {head!r} "
            "would be a command — what to run goes in the Launch field"
        )
    user: str | None = None
    home = False
    non_interactive = False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-") or token == "-":
            raise RecipeError(
                f"{token!r} after sudo would be a command. sudo takes only -n, "
                "-u USER and -H here — the command goes in the Launch field, and "
                "nested shells (`sudo su`, `su odoo`) become one `sudo -n -u odoo -H`"
            )
        if token == "-u" or (token.startswith("-u") and len(token) > 2):
            if user is not None:
                raise RecipeError("sudo -u is given twice")
            if token == "-u":
                if index + 1 >= len(tokens):
                    raise RecipeError("sudo -u needs a value")
                user = _user(tokens[index + 1], "sudo user")
                index += 2
            else:
                user = _user(token[2:], "sudo user")
                index += 1
            continue
        flags = token[1:]
        if flags and set(flags) <= {"n", "H"}:
            non_interactive = non_interactive or "n" in flags
            home = home or "H" in flags
            index += 1
            continue
        raise RecipeError(
            f"sudo {token} is not allowed here. Only -n, -u USER and -H are"
        )
    if not non_interactive:
        raise RecipeError(
            "sudo needs -n: there is nobody to type a password, and without it "
            "sudo would wait for one"
        )

    return Become(user, home)


def _single(values: list, what: str):
    distinct = list(dict.fromkeys(values))
    if len(distinct) > 1:
        shown = " and ".join(repr(value) for value in distinct)
        raise RecipeError(f"two different {what} given: {shown}")

    return distinct[0] if distinct else None


def parse_access(text: str, *, is_file: Callable[[str], bool] = os.path.isfile) -> Access:
    try:

        return _parse_access(text, is_file)
    except RecipeError as exc:
        exc.field = exc.field or "access"
        raise


def _parse_access(text: str, is_file: Callable[[str], bool]) -> Access:
    """Parse the Access field, or say why it is not accepted.

    `is_file` is a parameter so the one thing here that looks at this machine —
    does a named key exist — can be answered by a test.
    """
    tokens = _split(text, "access")
    if tokens[0] != "ssh":
        raise RecipeError(
            "access starts with ssh: `ssh [options] [user@]host [sudo -n -u USER -H]`"
        )
    rest = tokens[1:]
    collected = _Collected()
    destination: str | None = None
    become: Become | None = None
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            raise RecipeError("-- is added by the daemon, before the destination")
        if token.startswith("-") and token != "-":
            index = _parse_option(rest, index, collected, is_file)
            continue
        if destination is None:
            destination = token
            index += 1
            continue
        become = _parse_become(rest[index:])
        break
    if destination is None:
        raise RecipeError("access has no destination: ssh [options] [user@]host")
    user_in_destination, host = _parse_destination(destination)
    user = _single([*collected.users, *([user_in_destination] if user_in_destination else [])], "users")
    port = _single(collected.ports, "ports")
    keys = tuple(dict.fromkeys(collected.keys))

    options: list[str] = []
    for key in keys:
        options += ["-i", key]
    if port is not None:
        options += ["-p", str(port)]
    if collected.jump:
        options += ["-J", collected.jump]
    if collected.timeout is not None:
        options += ["-o", f"ConnectTimeout={collected.timeout}"]
    if collected.identities_only:
        options += ["-o", f"IdentitiesOnly={collected.identities_only}"]

    return Access(tuple(options), user, host, port, keys, collected.jump, become)


def _database_flag(tokens: Sequence[str]) -> str | None:
    """The value of `-d`/`--database` among Odoo's arguments, if there is one."""
    for index, token in enumerate(tokens):
        if token in ("-d", "--database"):

            return tokens[index + 1] if index + 1 < len(tokens) else ""
        if token.startswith("--database="):

            return token.split("=", 1)[1]
        if token.startswith("-d") and not token.startswith("--"):

            return token[2:]

    return None


def _config_flag(tokens: Sequence[str]) -> str | None:
    for index, token in enumerate(tokens):
        if token in ("-c", "--config"):

            return tokens[index + 1] if index + 1 < len(tokens) else None
        if token.startswith("--config="):

            return token.split("=", 1)[1]
        if token.startswith("-c") and not token.startswith("--") and len(token) > 2:

            return token[2:]

    return None


def check_database(name: str) -> str:
    if (
        len(name) > MAX_DATABASE
        or name.startswith("-")
        or "\x00" in name
        or "\n" in name
        or "\r" in name
    ):
        raise RecipeError(
            f"database {name!r} cannot be a database name (at most {MAX_DATABASE} "
            "characters, not starting with '-')",
            field="database",
        )

    return name


def parse_launch(text: str, database: str | None = None) -> Launch:
    try:

        return _parse_launch(text, database)
    except RecipeError as exc:
        exc.field = exc.field or "launch"
        raise


def _parse_launch(text: str, database: str | None) -> Launch:
    """Parse the Launch field, and the optional Database that goes with it.

    The Launch is taken as written: nothing is added — not `--no-http`, not
    `-d`, not `--workers`. Odoo's `shell` does not start an HTTP server, so
    there is nothing to protect from one. The one thing that is appended is
    `-d DATABASE`, and only when the Database field is filled.
    """
    tokens = _split(text, "launch")
    if not tokens[0].startswith("/"):
        raise RecipeError(
            f"launch starts with an absolute path, not {tokens[0]!r}: nothing here "
            "searches PATH on the server, and ~ is not expanded there"
        )
    shell_at = next(
        (
            index
            for index, token in enumerate(tokens)
            if index >= 1 and token == "shell" and not tokens[index - 1].startswith("-")
        ),
        None,
    )
    if shell_at is None:
        raise RecipeError(
            "launch has no `shell`: it runs Odoo's shell, e.g. "
            "`/path/to/python /path/to/odoo-bin shell -c /path/to/odoo.conf`"
        )
    wanted = None
    if isinstance(database, str) and database.strip():
        wanted = check_database(database.strip())
    in_launch = _database_flag(tokens[shell_at + 1 :])
    if wanted and in_launch is not None:
        raise RecipeError(
            "the database is written in both places, -d in the launch and the "
            "Database field: keep one place",
            field="database",
        )
    argv = tuple(tokens) + (("-d", wanted) if wanted else ())

    return Launch(
        argv=argv,
        database=wanted or in_launch or None,
        database_in_launch=in_launch or None,
        config=_config_flag(tokens[shell_at + 1 :]),
        odoo_bin=tokens[shell_at - 1],
    )


def launch_script(launch: Launch, tail: str) -> str:
    """The script the remote shell runs: keep the command pipe, then Odoo.

    `exec 3<&0` duplicates the command pipe before Odoo replaces its own stdin
    with the heredoc; Odoo reads the bootstrap, hits EOF, executes it, and the
    pipe survives on fd 3. It runs inside the user switch because `sudo` would
    have closed it.
    """

    return f'exec 3<&0; exec {quote_all(launch.argv)} <<"{HEREDOC_MARKER}"\n{tail}'


def describe(access: Access, launch: Launch) -> dict:
    """What the recipe means, decoded for the person who is about to save it."""
    if access.become:
        runs_as = access.become.user or "root"
    else:
        runs_as = access.user
    warnings = []
    if runs_as == "root":
        warnings.append(
            "this runs Odoo as root: it writes root-owned files into the "
            "filestore, which the odoo user then cannot read"
        )
    script = launch_script(launch, "… the session's bootstrap …\nOSBOOT\n")
    # How it reads typed into a terminal, which is not quite how it is sent:
    # ssh joins the remote command into one string (`ssh_argv`). The shape is
    # the same, and the point of showing it is that the Access field alone is a
    # prefix, not a command.
    become = access.become.argv if access.become else ()
    assembled = quote_all(
        ["ssh", *access.options, "--", access.destination, *become, "sh", "-c", script]
    )

    return {
        "login": {"user": access.user, "host": access.host, "port": access.port},
        "keys": list(access.keys),
        "jump": access.jump,
        "become": (
            {"user": access.become.user, "home": access.become.home} if access.become else None
        ),
        "runs_as": runs_as,
        "launch": {
            "executable": launch.argv[0],
            "config": launch.config,
            "argv": list(launch.argv),
        },
        "database": launch.database,
        "database_source": (
            "field" if launch.database and not launch.database_in_launch
            else "launch" if launch.database_in_launch else None
        ),
        "warnings": warnings,
        "assembled": assembled,
    }
