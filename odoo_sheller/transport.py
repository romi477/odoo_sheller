"""Spawning the container-side process and signalling it.

The daemon never signals the local `docker exec` client: without a tty it does
not forward signals into the container. Interrupts go through a separate
`docker exec <container> kill -<SIG> <pid>`, using the pid the bootstrap
reported in its hello frame.
"""

import asyncio
import shlex
from dataclasses import dataclass
from pathlib import Path

from odoo_sheller.protocol import FRAME_LINE_LIMIT

HEREDOC_MARKER = "OSBOOT"
_BOOTSTRAP_PATH = Path(__file__).with_name("bootstrap.py")


@dataclass(frozen=True)
class Target:
    container: str
    database: str
    odoo_bin: str


def bootstrap_source() -> str:

    return _BOOTSTRAP_PATH.read_text(encoding="utf-8")


def build_command(target: Target, source: str) -> list[str]:
    if any(line.strip() == HEREDOC_MARKER for line in source.splitlines()):
        raise ValueError(f"bootstrap source contains the heredoc marker {HEREDOC_MARKER}")
    odoo_bin = shlex.quote(target.odoo_bin)
    database = shlex.quote(target.database)
    script = (
        f'exec 3<&0; exec {odoo_bin} shell -d {database} --no-http <<"{HEREDOC_MARKER}"\n'
        f"{source}\n{HEREDOC_MARKER}\n"
    )

    return ["docker", "exec", "-i", target.container, "sh", "-c", script]


def signal_command(container: str, pid: int, signal_name: str) -> list[str]:

    return ["docker", "exec", container, "kill", f"-{signal_name}", str(pid)]


async def spawn(argv: list[str]) -> asyncio.subprocess.Process:

    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=FRAME_LINE_LIMIT,
    )


async def send_signal(container: str, pid: int, signal_name: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *signal_command(container, pid, signal_name),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
