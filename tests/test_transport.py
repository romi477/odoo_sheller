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


def test_build_command_uses_the_docker_override(monkeypatch):
    monkeypatch.setenv("ODOO_SHELLER_DOCKER", "/custom/bin/docker")
    argv = build_command(TARGET, "print('boot')")
    assert argv[0] == "/custom/bin/docker"
    assert argv[1:4] == ["exec", "-i", "integra19"]


def test_signal_command_uses_the_docker_override(monkeypatch):
    monkeypatch.setenv("ODOO_SHELLER_DOCKER", "/custom/bin/docker")
    assert signal_command(TARGET, 42, "INT")[0] == "/custom/bin/docker"


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


async def test_a_signal_that_cannot_be_delivered_does_not_hang(monkeypatch):
    """Interrupt — and the timeout path that sends one — waited for as long
    as a wedged Engine or a dead link stayed that way."""
    import time

    from odoo_sheller import transport

    monkeypatch.setattr(transport, "signal_command", lambda *args: ["sleep", "5"])
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await transport.send_signal(Target(container="c"), 42, "INT", timeout=0.2)
    assert time.monotonic() - started < 2


# --- a build and a host are names, not arguments --------------------------

BAD_NAMES = [
    "-oProxyCommand=touch /tmp/x",
    "-oProxyCommand=touch${IFS}/tmp/x",
    "a b",
    "a;b",
    "$(id)",
    "a/b",
    "a@b",
    "a\n",
    "-1",
    ".hidden",
    "",
]


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_a_build_that_is_not_a_name_never_reaches_ssh(bad):
    """`ssh -G -oProxyCommand=… host` shows ssh reads such an argument as an
    option, and the option runs a command on this machine. What kept it from
    happening was a space in a neighbouring argument, which is luck."""
    target = Target(kind="odoosh", build=bad, host="build.dev.odoo.com")
    with pytest.raises(ValueError, match="build"):
        build_command(target, "pass")
    with pytest.raises(ValueError, match="build"):
        signal_command(target, 42, "INT")


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_a_host_that_is_not_a_name_never_reaches_ssh(bad):
    target = Target(kind="odoosh", build="36887345", host=bad)
    with pytest.raises(ValueError, match="host"):
        build_command(target, "pass")
    with pytest.raises(ValueError, match="host"):
        signal_command(target, 42, "INT")


@pytest.mark.parametrize("build", ["36887345", "b-1", "a.b-c"])
def test_an_ordinary_build_is_a_name(build):
    argv = build_command(Target(kind="odoosh", build=build, host="h-1.dev.odoo.com"), "pass")
    assert f"{build}@h-1.dev.odoo.com" in argv


def test_the_destination_follows_a_double_dash():
    """Belt over the braces above: whatever the name was, ssh is told that the
    options are over."""
    dest = "36887345@build-36887345.dev.odoo.com"
    for argv in (build_command(OOSH, "pass"), signal_command(OOSH, 42, "INT")):
        assert argv[argv.index(dest) - 1] == "--"


# --- an Odoo installed on a server, reached by ssh --------------------------


def ssh_target(
    access="ssh -i /k/key.pem ubuntu@srv.example.com sudo -n -u odoo -H",
    launch="/opt/odoo/env/bin/python /opt/odoo/odoo-bin shell -c /opt/odoo/odoo.conf",
    stage="production",
):
    from odoo_sheller.recipe import parse_access, parse_launch

    parsed = parse_access(access, is_file=lambda path: True)
    return Target(
        kind="ssh",
        label="acme-prod",
        access=parsed,
        launch=parse_launch(launch),
        host=parsed.host,
        stage=stage,
    )


def test_an_ssh_target_is_identified_by_the_name_on_its_card():
    target = ssh_target()
    assert target.name == "acme-prod"
    assert target.is_remote
    assert target.host == "srv.example.com"


def test_the_whole_command_is_the_cards_prefix_then_sh_c(tmp_path):
    import shlex

    argv = build_command(ssh_target(), "print('boot')")
    assert argv[0] == "ssh"
    dash = argv.index("--")
    assert argv[dash + 1] == "ubuntu@srv.example.com"
    assert argv[dash - 2 : dash] == ["-i", "/k/key.pem"], "the card's options sit before --"
    assert len(argv) == dash + 3, "one remote command, quoted once, for ssh's own re-parse"
    remote = shlex.split(argv[-1])
    assert remote[:7] == ["sudo", "-n", "-u", "odoo", "-H", "sh", "-c"]
    script = remote[7]
    assert len(remote) == 8
    assert script.index("exec 3<&0") < script.index("odoo-bin"), "the fd is kept before Odoo"
    assert "print('boot')" in script
    assert script.count(HEREDOC_MARKER) == 2


def test_the_ssh_options_every_kind_of_remote_needs_are_still_there():
    argv = build_command(ssh_target(), "pass")
    flat = " ".join(argv)
    assert "-T" in argv and "BatchMode=yes" in flat and "ServerAliveInterval" in flat
    assert "ControlMaster" in flat and "ControlPersist" in flat


def test_the_launch_is_run_exactly_as_written():
    import shlex

    script = shlex.split(build_command(ssh_target(), "pass")[-1])[7]
    assert "odoo-bin shell -c /opt/odoo/odoo.conf <<" in script
    for added in ("--no-http", " -d ", "--workers"):
        assert added not in script


def test_a_database_the_launch_names_is_run_as_written_and_nothing_is_appended():
    import shlex

    named = ssh_target(
        launch="/opt/odoo/env/bin/python /opt/odoo/odoo-bin shell -c /opt/odoo/odoo.conf -d acme"
    )
    script = shlex.split(build_command(named, "pass")[-1])[7]
    assert "odoo-bin shell -c /opt/odoo/odoo.conf -d acme <<" in script
    bare = shlex.split(build_command(ssh_target(), "pass")[-1])[7]
    assert " -d " not in bare, "no database is chosen for a launch that does not choose one"


def test_an_ssh_target_without_a_become_has_no_prefix():
    import shlex

    target = ssh_target(access="ssh -i /k/key.pem ubuntu@srv.example.com")
    assert shlex.split(build_command(target, "pass")[-1])[:2] == ["sh", "-c"]


def test_an_interrupt_arrives_as_the_user_the_session_runs_as():
    import shlex

    argv = signal_command(ssh_target(), 4242, "INT")
    assert argv[0] == "ssh"
    assert argv[argv.index("--") + 1] == "ubuntu@srv.example.com"
    assert shlex.split(argv[-1]) == ["sudo", "-n", "-u", "odoo", "-H", "kill", "-INT", "4242"]


def test_an_ssh_bootstrap_that_would_close_the_heredoc_is_refused():
    with pytest.raises(ValueError):
        build_command(ssh_target(), f"x = 1\n{HEREDOC_MARKER}\n")


def test_a_token_that_looks_like_shell_is_one_argument_to_odoo():
    """Run for real, in a local `sh`: the launch is data, never code."""
    import subprocess

    from odoo_sheller.recipe import launch_script, parse_launch

    launch = parse_launch("/bin/echo shell 'a;b' '$(id)' '`id`' '> /tmp/never'")
    script = launch_script(launch, f"ignored\n{HEREDOC_MARKER}\n")
    out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True).stdout
    assert out == "shell a;b $(id) `id` > /tmp/never\n"


# The ssh and sudo of the chain, as shims. They exist to make the unit test
# honest about the one thing that matters: `sudo` closes every descriptor above
# 2. The real `sudo` is exercised against a container in tests/test_e2e_ssh.py.

FAKE_SSH = """#!/bin/sh
# Skip our options, `--` and the destination; what is left is joined and
# re-parsed by a shell, as the remote login shell would.
while [ "$1" != "--" ]; do shift; done
shift; shift
exec sh -c "$*"
"""

FAKE_SUDO = """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in -n|-H) shift ;; -u) shift 2 ;; *) break ;; esac
done
for fd in 3 4 5 6 7 8 9; do eval "exec $fd>&-"; done
exec "$@"
"""

FAKE_ODOO = """#!/bin/sh
# Reads its stdin (the bootstrap, as a heredoc) to the end, then answers on
# stdout whatever arrives on fd 3 — which only works if fd 3 is still there.
cat > /dev/null
cat <&3
"""


@pytest.fixture
def shims(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ssh", FAKE_SSH), ("sudo", FAKE_SUDO)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    odoo = tmp_path / "odoo-bin"
    odoo.write_text(FAKE_ODOO, encoding="utf-8")
    odoo.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    return str(odoo)


async def test_the_command_pipe_survives_the_user_switch(shims):
    """The whole chain, with a sudo that closes fd 3: the pipe is still there
    because `exec 3<&0` runs after the switch, not before."""
    target = ssh_target(launch=f"{shims} shell")
    process = await spawn(build_command(target, "print('boot')"))
    out, err = await process.communicate(b"ping\n")
    assert out == b"ping\n", err.decode()


async def test_the_shim_really_does_close_the_descriptor(shims):
    """A control: the order the transport refuses to use fails here, so the
    test above is not passing for want of a sudo that does anything."""
    import asyncio

    process = await asyncio.create_subprocess_exec(
        "sh", "-c", "exec 3<&0; sudo -n -u odoo sh -c 'cat <&3'",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate(b"ping\n")
    assert out == b""
    assert b"Bad file descriptor" in err
