"""Spawning the process that runs the shell, and signalling it.

Two kinds of place, one mechanism. Locally that is `docker exec -i` into a
container; on odoo.sh it is `ssh -T` into a build. Everything above this
module — the frame protocol, the session state machine, the bootstrap itself
— is the same either way, because the design never depended on anything the
two do differently.

Signals are the clearest case. Neither `docker exec -i` nor `ssh` without a
tty forwards a signal to the far side, so an interrupt was never sent down
the pipe: it goes as a separate `kill -<SIG> <pid>` using the pid the
bootstrap reported in its hello frame. Built for Docker, it transferred to
SSH unchanged.
"""

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path

from odoo_sheller.protocol import FRAME_LINE_LIMIT

HEREDOC_MARKER = "OSBOOT"
_BOOTSTRAP_PATH = Path(__file__).with_name("bootstrap.py")

DOCKER = "docker"
ODOOSH = "odoosh"

# What odoo.sh calls its instances. Only this one refuses a commit outright.
PRODUCTION = "production"

# Multiplexed connections live here, beside the journals and the admin key.
_SSH_CONTROL = Path.home() / ".odoo-sheller" / "ssh-%C"

SSH_OPTS = (
    # No tty: a pty would merge the frame stream into the log stream, and the
    # split between them is the whole reason stdout can carry frames at all.
    "-T",
    # The daemon has no terminal to answer a password prompt on. Fail fast
    # rather than hang forever waiting for one.
    "-o", "BatchMode=yes",
    # A dead link should become EOF, which the session already treats as an
    # ordinary process death, instead of a wait with no end.
    "-o", "ServerAliveInterval=15",
    # Interrupt and kill each open a second connection. Measured against a
    # real odoo.sh build, a fresh handshake costs ~1.1s and a multiplexed one
    # ~0.13s — and 1.1s is precisely the latency this tool exists to remove.
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath={_SSH_CONTROL}",
    "-o", "ControlPersist=300",
)


@dataclass(frozen=True)
class Target:
    """Where a session runs.

    One identity slot, read differently by kind: a container name locally, a
    build id on odoo.sh. `database` and `odoo_bin` are the local half's to
    choose; on odoo.sh the instance dictates both and there is nothing to
    pick.
    """

    kind: str = DOCKER
    container: str | None = None
    database: str | None = None
    odoo_bin: str | None = None
    build: str | None = None
    host: str | None = None
    # What the instance said it is: staging, production. Read from the build
    # itself at open time, never from whoever asked for the session — the two
    # differ by the digits in a build id, and this is what the commit guard
    # turns on.
    stage: str | None = None

    @property
    def name(self) -> str | None:
        """The identity slot — a container name, or a build id."""

        return self.build if self.kind == ODOOSH else self.container

    @property
    def is_remote(self) -> bool:
        """Someone else's machine — so being the owner is not enough to write."""

        return self.kind != DOCKER

    @property
    def ssh_dest(self) -> str:
        """odoo.sh authenticates as the build; the host only routes there."""

        return f"{self.build}@{self.host}"


def bootstrap_source() -> str:

    return _BOOTSTRAP_PATH.read_text(encoding="utf-8")


def _script(target: Target, source: str) -> str:
    """The shell one-liner that hands stdin to the bootstrap on fd 3.

    `exec 3<&0` duplicates the command pipe before Odoo replaces its own
    stdin with the heredoc; Odoo reads the bootstrap, hits EOF, executes it,
    and the command channel survives on fd 3 for the life of the process.
    """
    if any(line.strip() == HEREDOC_MARKER for line in source.splitlines()):
        raise ValueError(f"bootstrap source contains the heredoc marker {HEREDOC_MARKER}")
    if target.kind == ODOOSH:
        # odoo.sh's own odoo-bin wrapper appends --database, --config,
        # --workers=0 and --no-http *after* whatever it is given, so a -d of
        # ours would be shadowed anyway. Passing one would only imply a
        # choice that does not exist: the build has exactly one database.
        launch = "odoo-bin shell"
    else:
        launch = (
            f"{shlex.quote(target.odoo_bin)} shell "
            f"-d {shlex.quote(target.database)} --no-http"
        )

    return (
        f'exec 3<&0; exec {launch} <<"{HEREDOC_MARKER}"\n'
        f"{source}\n{HEREDOC_MARKER}\n"
    )


def build_command(target: Target, source: str) -> list[str]:
    script = _script(target, source)
    if target.kind == ODOOSH:
        # `docker exec … sh -c script` hands argv straight over, but `ssh host
        # a b c` joins its arguments and the remote login shell parses the
        # result again. Quote once, for that one extra parse — the heredoc
        # does not survive getting it wrong in either direction.
        return ["ssh", *SSH_OPTS, target.ssh_dest, f"sh -c {shlex.quote(script)}"]

    return ["docker", "exec", "-i", target.container, "sh", "-c", script]


def signal_command(target: Target, pid: int, signal_name: str) -> list[str]:
    signal = [f"-{signal_name}", str(pid)]
    if target.kind == ODOOSH:

        return ["ssh", *SSH_OPTS, target.ssh_dest, "kill", *signal]

    return ["docker", "exec", target.container, "kill", *signal]


async def spawn(argv: list[str]) -> asyncio.subprocess.Process:

    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=FRAME_LINE_LIMIT,
    )


async def send_signal(target: Target, pid: int, signal_name: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *signal_command(target, pid, signal_name),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
