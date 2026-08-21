import sys

import pytest

from odoo_sheller.transport import (
    HEREDOC_MARKER,
    Target,
    bootstrap_source,
    build_command,
    signal_command,
    spawn,
)

TARGET = Target(container="integra19", database="integra_db_19", odoo_bin="/opt/odoo/odoo-bin")


def test_command_keeps_the_pipe_on_fd_3_before_odoo_takes_stdin():
    argv = build_command(TARGET, "print('boot')")
    assert argv[:4] == ["docker", "exec", "-i", "integra19"]
    assert argv[4] == "sh"
    assert argv[5] == "-c"
    script = argv[6]
    assert script.index("exec 3<&0") < script.index("odoo-bin")
    assert "--no-http" in script
    assert script.count(HEREDOC_MARKER) == 2
    assert f'<<"{HEREDOC_MARKER}"' in script


def test_command_quotes_the_target_fields():
    hostile = Target(container="c", database="db; rm -rf /", odoo_bin="/opt/odoo bin/odoo-bin")
    script = build_command(hostile, "pass")[6]
    assert "'db; rm -rf /'" in script
    assert "'/opt/odoo bin/odoo-bin'" in script


def test_command_refuses_a_bootstrap_that_would_close_the_heredoc():
    with pytest.raises(ValueError):
        build_command(TARGET, f"x = 1\n{HEREDOC_MARKER}\n")


def test_bootstrap_source_is_the_real_file():
    source = bootstrap_source()
    assert "_os_main(globals())" in source
    assert "OS_CMD_FD" in source


def test_signal_command_targets_the_in_container_pid():
    assert signal_command("integra19", 42, "INT") == [
        "docker", "exec", "integra19", "kill", "-INT", "42",
    ]


async def test_spawn_reads_a_line_over_asyncio_default_limit():
    """Bootstrap clips still exceed 64 KiB; the default StreamReader dies on them."""
    proc = await spawn([
        sys.executable,
        "-c",
        "import sys; sys.stdout.write('x' * 80000 + '\\n'); sys.stdout.flush(); sys.stdin.read(1)",
    ])
    try:
        line = await proc.stdout.readline()
        assert len(line) == 80001
    finally:
        proc.stdin.write(b"x")
        await proc.stdin.drain()
        await proc.wait()


async def test_spawn_gives_working_pipes():
    proc = await spawn(["sh", "-c", "read line; echo \"got:$line\"; echo err >&2"])
    proc.stdin.write(b"ping\n")
    await proc.stdin.drain()
    assert (await proc.stdout.readline()).strip() == b"got:ping"
    assert (await proc.stderr.readline()).strip() == b"err"
    await proc.wait()


async def test_spawn_reports_a_missing_binary():
    with pytest.raises(FileNotFoundError):
        await spawn(["definitely-not-a-real-binary-xyz"])
