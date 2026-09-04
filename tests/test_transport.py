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


# --- odoo.sh over SSH ---------------------------------------------------

OOSH = Target(kind="odoosh", build="36887345", host="build-36887345.dev.odoo.com")


def test_an_odoosh_target_is_identified_by_its_build():
    """The slot that holds a container name locally holds a build id here."""
    assert OOSH.name == "36887345"
    assert TARGET.name == "integra19"


def test_odoosh_command_goes_over_ssh_to_build_at_host():
    argv = build_command(OOSH, "print('boot')")
    assert argv[0] == "ssh"
    assert "36887345@build-36887345.dev.odoo.com" in argv


def test_odoosh_command_survives_exactly_one_shell_parse():
    """`docker exec … sh -c script` hands argv straight over, but `ssh host a b`
    joins its arguments and the remote login shell parses the result again. Quote
    once too few and the heredoc falls apart; once too many and it never runs."""
    import shlex

    remote = build_command(OOSH, "print('boot')")[-1]
    parsed = shlex.split(remote)
    assert parsed[0] == "sh"
    assert parsed[1] == "-c"
    script = parsed[2]
    assert script.index("exec 3<&0") < script.index("odoo-bin")
    assert script.count(HEREDOC_MARKER) == 2
    assert "print('boot')" in script


def test_odoosh_command_leaves_the_arguments_to_the_wrapper():
    """odoo.sh's odoo-bin appends --database/--config/--workers=0/--no-http after
    ours, so a -d of ours would be shadowed — implying a choice that is not there."""
    script = shlex_last_script(OOSH)
    assert "odoo-bin shell" in script
    assert " -d " not in script
    assert "--no-http" not in script


def shlex_last_script(target):
    import shlex

    return shlex.split(build_command(target, "pass")[-1])[2]


def test_ssh_options_that_each_earn_their_place():
    argv = build_command(OOSH, "pass")
    flat = " ".join(argv)
    # no tty, or a pty would merge the frame stream into the log stream
    assert "-T" in argv
    # the daemon has no terminal to answer a password prompt: fail, never hang
    assert "BatchMode=yes" in flat
    # a dead link becomes EOF, an outcome the session already handles
    assert "ServerAliveInterval" in flat
    # without multiplexing every interrupt pays a fresh handshake (~1.1s)
    assert "ControlMaster" in flat and "ControlPersist" in flat


def test_odoosh_command_refuses_a_bootstrap_that_would_close_the_heredoc():
    with pytest.raises(ValueError):
        build_command(OOSH, f"x = 1\n{HEREDOC_MARKER}\n")


def test_signal_command_dispatches_on_the_kind_of_place():
    assert signal_command(TARGET, 42, "INT") == [
        "docker", "exec", "integra19", "kill", "-INT", "42",
    ]
    ssh = signal_command(OOSH, 42, "INT")
    assert ssh[0] == "ssh"
    assert ssh[-3:] == ["kill", "-INT", "42"]
    assert "36887345@build-36887345.dev.odoo.com" in ssh
