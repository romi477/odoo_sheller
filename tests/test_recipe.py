"""What a person may write on an ssh card: two lines, parsed, never run.

`Access` is how to arrive as the right user; `Launch` is what to run once
there. Both are *data* here — tokenised, checked against a whitelist, and
re-quoted token by token on the way out — because a field that holds a command
line is, on the machine running the daemon, a way to run commands.
"""

import shlex

import pytest

from odoo_sheller.recipe import Access, Launch, RecipeError, describe, parse_access, parse_launch

LAUNCH = "/opt/odoo/13.0/env/bin/python /opt/odoo/13.0/odoo/odoo-bin shell -c /opt/odoo/13.0/odoo.conf"


@pytest.fixture
def key(tmp_path, monkeypatch):
    """A key file that exists, and a HOME to expand `~` against."""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    path = ssh_dir / "server.pem"
    path.write_text("not a real key", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    return str(path)


def refused(text, *, match):
    with pytest.raises(RecipeError, match=match):
        parse_access(text)


# --- Access: what is accepted, and what it becomes -------------------------


def test_the_plain_form(key):
    access = parse_access(f"ssh -i {key} ubuntu@build.example.com")
    assert isinstance(access, Access)
    assert (access.user, access.host, access.port) == ("ubuntu", "build.example.com", None)
    assert access.keys == (key,)
    assert access.become is None
    assert access.destination == "ubuntu@build.example.com"
    assert access.options == ("-i", key)


def test_a_become_makes_it_a_prefix(key):
    access = parse_access(f"ssh -i {key} ubuntu@h.example.com sudo -n -u odoo -H")
    assert access.become is not None
    assert access.become.user == "odoo"
    assert access.become.home is True
    assert access.become.argv == ("sudo", "-n", "-u", "odoo", "-H")


def test_a_become_without_a_user_or_home(key):
    access = parse_access(f"ssh -i {key} ubuntu@h.example.com sudo -n")
    assert access.become.argv == ("sudo", "-n")
    assert access.become.user is None


def test_the_order_of_sudos_flags_does_not_matter(key):
    one = parse_access(f"ssh -i {key} u@h sudo -n -H -u odoo")
    two = parse_access(f"ssh -i {key} u@h sudo -u odoo -H -n")
    assert one.become.argv == two.become.argv == ("sudo", "-n", "-u", "odoo", "-H")


def test_options_after_the_host_are_accepted_and_normalised(key):
    """`ssh host -i key -l ubuntu` is how people write it, and ssh reads it. The
    card says the same thing in one order: `-l ubuntu` becomes `ubuntu@host`."""
    access = parse_access("ssh 54.175.203.247 -i ~/.ssh/server.pem -l ubuntu")
    assert access.destination == "ubuntu@54.175.203.247"
    assert access.keys == (key,), "~ is expanded here, against this machine's home"


@pytest.mark.parametrize(
    "spelling",
    ["-p 2222", "-p2222", "-o Port=2222", "-oPort=2222", '-o "Port 2222"', "-o port=2222"],
)
def test_a_port_in_any_of_its_spellings(key, spelling):
    assert parse_access(f"ssh -i {key} {spelling} u@h.example.com").port == 2222


@pytest.mark.parametrize(
    "spelling", ["-l ubuntu", "-lubuntu", "-o User=ubuntu", "-o 'User ubuntu'"]
)
def test_a_user_in_any_of_its_spellings(key, spelling):
    assert parse_access(f"ssh -i {key} {spelling} h.example.com").destination == "ubuntu@h.example.com"


def test_a_jump_host(key):
    access = parse_access(f"ssh -i {key} -J jump@bastion.example.com:2200 u@h.example.com")
    assert access.jump == "jump@bastion.example.com:2200"
    assert access.options == ("-i", key, "-J", "jump@bastion.example.com:2200")
    same = parse_access(f"ssh -i {key} -o ProxyJump=jump@bastion.example.com:2200 u@h.example.com")
    assert same.jump == access.jump


def test_a_chain_of_jump_hosts(key):
    assert parse_access(f"ssh -i {key} -J a.example.com,b@c.example.com u@h").jump == (
        "a.example.com,b@c.example.com"
    )


def test_a_connect_timeout_and_identities_only(key):
    access = parse_access(
        f"ssh -i {key} -o ConnectTimeout=10 -o IdentitiesOnly=yes u@h.example.com"
    )
    assert "ConnectTimeout=10" in access.options
    assert "IdentitiesOnly=yes" in access.options


def test_two_keys_are_two_keys(key, tmp_path):
    other = tmp_path / "other.pem"
    other.write_text("x", encoding="utf-8")
    access = parse_access(f"ssh -i {key} -i {other} u@h")
    assert access.keys == (key, str(other))


def test_the_same_thing_said_twice_is_fine(key):
    access = parse_access(f"ssh -i {key} -l ubuntu -o User=ubuntu -p 22 -o Port=22 ubuntu@h")
    assert (access.user, access.port) == ("ubuntu", 22)


def test_quotes_are_the_shells_not_ours(key):
    assert parse_access(f'ssh -i "{key}" "ubuntu@h.example.com"').host == "h.example.com"


# --- Access: what is refused, and why --------------------------------------


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("ls -la", "ssh"),
        ("/usr/bin/ssh u@h", "ssh"),
        ("ssh", "destination"),
        ("ssh -i", "needs a value"),
        ("ssh u@h\nsudo su", "one line"),
        ("ssh u@h; id", "one command"),
        ("ssh u@h && id", "one command"),
        ("ssh u@h | cat", "one command"),
        ("ssh 'unterminated", "quote"),
    ],
)
def test_what_is_not_one_ssh_command(text, match):
    refused(text, match=match)


@pytest.mark.parametrize(
    ("option", "match"),
    [
        ("-F /tmp/config", "config"),
        ("-t", "pty|tty"),
        ("-tt", "pty|tty"),
        ("-T", "not allowed"),
        ("-A", "agent"),
        ("-X", "X11"),
        ("-Y", "X11"),
        ("-f", "background"),
        ("-N", "not allowed"),
        ("-L 8080:localhost:80", "forward"),
        ("-R 8080:localhost:80", "forward"),
        ("-D 1080", "forward"),
        ("-w 0:0", "not allowed"),
        ("-W host:22", "not allowed"),
        ("-v", "not allowed"),
        ("-M", "not allowed"),
        ("-S /tmp/sock", "not allowed"),
        ("-E /tmp/log", "not allowed"),
        ("-b 127.0.0.1", "not allowed"),
        ("-e ~", "not allowed"),
        ("-o ProxyCommand=touch/tmp/x", "ProxyCommand"),
        ("-oProxyCommand=touch/tmp/x", "ProxyCommand"),
        ('-o "ProxyCommand touch /tmp/x"', "ProxyCommand"),
        ("-o proxycommand=x", "ProxyCommand"),
        ("-o LocalCommand=touch/tmp/x", "LocalCommand"),
        ("-o PermitLocalCommand=yes", "PermitLocalCommand"),
        ("-o LocalForward=1:h:2", "forward"),
        ("-o RemoteForward=1:h:2", "forward"),
        ("-o DynamicForward=1080", "forward"),
        ("-o ForwardAgent=yes", "agent|Forward"),
        ("-o ForwardX11=yes", "Forward"),
        ("-o StrictHostKeyChecking=no", "host key"),
        ("-o UserKnownHostsFile=/dev/null", "host key|known"),
        ("-o Include=/tmp/x", "not allowed"),
        ("-o SendEnv=LD_PRELOAD", "not allowed"),
        ("-o SetEnv=A=B", "not allowed"),
        ("-o RequestTTY=yes", "not allowed"),
        ("-o KnownHostsCommand=x", "not allowed"),
        ("-o Tunnel=yes", "not allowed"),
        ("-o Match=all", "not allowed"),
        ("-o", "needs a value"),
        ("-o Port", "needs a value|Port"),
        ("-o =x", "not allowed|needs"),
    ],
)
def test_options_that_would_do_something_else_on_this_machine(key, option, match):
    refused(f"ssh -i {key} {option} u@h.example.com", match=match)


@pytest.mark.parametrize(
    "destination",
    [
        "-oProxyCommand=x@h",
        "-evil",
        "u@-h",
        "u@h;id",
        "u@h$(id)",
        "u@`id`",
        "u@h&id",
        "u@h|id",
        "u@h>x",
        "u@h:22",
        "ssh://u@h",
        "u@@h",
        "a@b@c",
        "u@",
        "@h",
        "u/x@h",
        "u@h/x",
        "'u@h\\x'",
        "u@[::1]",
        "u@h.example.com.\u202e",
        "u@h*",
        "u@h?x",
        "~u@h",
        "u@%h",
    ],
)
def test_a_destination_is_a_name_and_nothing_else(key, destination):
    # `--`-less on purpose: the grammar reads the token, it does not trust where
    # it sits. A leading `-` is an option or an error, never a destination.
    with pytest.raises(RecipeError):
        parse_access(f"ssh -i {key} {destination}")


@pytest.mark.parametrize(
    ("tail", "match"),
    [
        ("id", "command|Launch"),
        ("ls /", "command|Launch"),
        ("sudo su", "sudo"),
        ("sudo -n su", "Launch|command|sudo"),
        ("sudo -n -u odoo id", "Launch|command|sudo"),
        ("sudo -n -u odoo -H /opt/x shell", "Launch|command|sudo"),
        ("sudo -u odoo", "-n"),
        ("sudo -H", "-n"),
        ("sudo", "-n"),
        ("sudo -n -S", "not allowed"),
        ("sudo -n -i", "not allowed"),
        ("sudo -n -s", "not allowed"),
        ("sudo -n -E", "not allowed"),
        ("sudo -n -g wheel", "not allowed"),
        ("sudo -n -u", "needs a value"),
        ("sudo -n -u -H", "user"),
        ("sudo -n -u '#0'", "user"),
        ("sudo -n -u 'od oo'", "user"),
        ("sudo -n -u ''", "user"),
        ("sudo -n -u odoo;id", "user|one command"),
        ("sudo -n -u '$(id)'", "user"),
        ("sudo -n -u odoo -u root", "once|twice|two"),
        ("su odoo", "su"),
        ("su - odoo", "su"),
        ("sudo su odoo", "sudo"),
        ("runuser -u odoo --", "runuser"),
        ("doas -u odoo", "doas"),
        ("bash", "command|Launch"),
        ("sudo -n -u odoo -H sudo -n -u root", "Launch|command|sudo"),
    ],
)
def test_after_the_destination_only_a_sudo_prefix(key, tail, match):
    refused(f"ssh -i {key} u@h.example.com {tail}", match=match)


def test_nested_shells_become_one_prefix_and_the_message_says_so(key):
    """`sudo su`, then `su odoo`, is how it is typed into a terminal. There is
    no terminal: say what to write instead."""
    with pytest.raises(RecipeError) as error:
        parse_access(f"ssh -i {key} u@h sudo su")
    assert "sudo -n -u" in str(error.value)


@pytest.mark.parametrize("port", ["0", "65536", "-1", "abc", "22.5", "", "2222;id"])
def test_a_port_is_a_port(key, port):
    refused(f"ssh -i {key} -p '{port}' u@h", match="port|Port")


@pytest.mark.parametrize("value", ["0", "-1", "abc", "9999", "1;id"])
def test_a_connect_timeout_is_seconds(key, value):
    refused(f"ssh -i {key} -o ConnectTimeout='{value}' u@h", match="ConnectTimeout")


@pytest.mark.parametrize("value", ["maybe", "ask", "", "yes;id"])
def test_identities_only_is_yes_or_no(key, value):
    refused(f"ssh -i {key} -o IdentitiesOnly='{value}' u@h", match="IdentitiesOnly")


@pytest.mark.parametrize(
    "jump",
    ["-oProxyCommand=x", "a;b", "a b@c", "$(id)", "a@", ",", "a,,b", "a@b@c", "h:99999", "h:x"],
)
def test_a_jump_is_a_list_of_hosts(key, jump):
    with pytest.raises(RecipeError):
        parse_access(f"ssh -i {key} -J '{jump}' u@h")


def test_a_user_said_twice_differently_is_refused(key):
    refused(f"ssh -i {key} -l root ubuntu@h", match="two users|user")
    refused(f"ssh -i {key} -l a -o User=b h", match="two users|user")


def test_a_port_said_twice_differently_is_refused(key):
    refused(f"ssh -i {key} -p 22 -o Port=2222 u@h", match="port")


def test_a_key_that_is_not_there_is_named(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    refused("ssh -i ~/.ssh/missing.pem u@h", match="missing.pem")


def test_a_key_that_is_a_directory_is_not_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "d").mkdir()
    refused(f"ssh -i {tmp_path / 'd'} u@h", match="not a file|missing|exist")


def test_a_key_path_is_checked_whichever_way_it_is_given(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    refused("ssh -o IdentityFile=/nonexistent/key u@h", match="nonexistent")


def test_double_dash_is_ours_to_add(key):
    refused(f"ssh -i {key} -- u@h", match="--")


def test_a_second_destination_is_a_command(key):
    refused(f"ssh -i {key} a.example.com b.example.com", match="command|Launch")


def test_an_ssh_without_a_key_is_allowed_because_an_agent_or_config_may_hold_it():
    """No `-i` is not an error: the login may use the agent or ~/.ssh/config.
    Only a key that is *named* has to exist."""
    assert parse_access("ssh ubuntu@h.example.com").keys == ()


# --- Launch ----------------------------------------------------------------


def test_a_launch_is_taken_as_written():
    launch = parse_launch(LAUNCH + " --xmlrpc-port 8068")
    assert isinstance(launch, Launch)
    assert launch.argv[0] == "/opt/odoo/13.0/env/bin/python"
    assert launch.argv[-2:] == ("--xmlrpc-port", "8068")
    assert launch.argv.count("shell") == 1


def test_a_launch_is_not_modified_nothing_is_appended():
    assert parse_launch(LAUNCH).argv == tuple(shlex.split(LAUNCH))
    assert "--no-http" not in parse_launch(LAUNCH).argv


def test_a_launch_with_an_env_prefix_is_still_an_absolute_path():
    launch = parse_launch("/usr/bin/env PYTHONPATH=/opt/odoo /usr/bin/python3 /opt/odoo/odoo-bin shell -c /etc/odoo.conf")
    assert launch.argv[0] == "/usr/bin/env"


def test_a_launch_path_with_a_space_is_one_token():
    assert parse_launch('"/opt/my odoo/bin/odoo-bin" shell').argv[0] == "/opt/my odoo/bin/odoo-bin"


def test_the_config_is_read_off_the_launch():
    assert parse_launch(LAUNCH).config == "/opt/odoo/13.0/odoo.conf"
    assert parse_launch("/x/odoo-bin shell --config=/etc/o.conf").config == "/etc/o.conf"
    assert parse_launch("/x/odoo-bin shell -c/etc/o.conf").config == "/etc/o.conf"
    assert parse_launch("/x/odoo-bin shell").config is None


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("odoo-bin shell", "absolute"),
        ("./odoo-bin shell", "absolute"),
        ("~/odoo/odoo-bin shell", "absolute"),
        ("shell -c x", "absolute"),
        ("/opt/odoo-bin -c /etc/odoo.conf", "shell"),
        ("/opt/odoo-bin shellx", "shell"),
        ("/opt/odoo-bin --shell", "shell"),
        ("/opt/odoo-bin shell\nid", "one line"),
        ("/opt/odoo-bin shell; id", "one command"),
        ("/opt/odoo-bin shell && id", "one command"),
        ("/opt/odoo-bin shell | cat", "one command"),
        ("/opt/odoo-bin shell 'x", "quote"),
        ("/opt/odoo-bin shell \x00", "NUL|null"),
    ],
)
def test_what_is_not_a_launch(text, match):
    with pytest.raises(RecipeError, match=match):
        parse_launch(text)


def test_shell_as_an_option_value_does_not_count():
    """`-c shell` is a config file called shell, not the subcommand."""
    with pytest.raises(RecipeError, match="shell"):
        parse_launch("/opt/odoo-bin -c shell")


# --- Database: one place ---------------------------------------------------


def test_a_database_is_appended_to_the_launch_not_written_into_it():
    launch = parse_launch(LAUNCH, database="acme")
    assert launch.argv[-2:] == ("-d", "acme")
    assert launch.database == "acme"


def test_no_database_leaves_the_launch_alone():
    assert parse_launch(LAUNCH, database=None).argv == tuple(shlex.split(LAUNCH))
    assert parse_launch(LAUNCH, database="").argv == tuple(shlex.split(LAUNCH))


@pytest.mark.parametrize("flag", ["-d x", "-dx", "--database x", "--database=x"])
def test_a_database_written_in_both_places_is_an_error(flag):
    with pytest.raises(RecipeError, match="one place|twice|both"):
        parse_launch(f"{LAUNCH} {flag}", database="acme")


@pytest.mark.parametrize("flag", ["-d x", "-dx", "--database x", "--database=x"])
def test_a_database_in_the_launch_alone_is_fine_and_is_read(flag):
    launch = parse_launch(f"{LAUNCH} {flag}")
    assert launch.database_in_launch == "x"
    assert launch.argv == tuple(shlex.split(f"{LAUNCH} {flag}"))


@pytest.mark.parametrize("name", ["-oops", "a\nb", "a\x00b", "x" * 64, "a;b" * 30])
def test_a_database_name_that_cannot_be_one(name):
    with pytest.raises(RecipeError, match="database"):
        parse_launch(LAUNCH, database=name)


@pytest.mark.parametrize("name", ["acme", "integra_db_19", "a-b.c", "integra_db_19ś", "x" * 63])
def test_ordinary_database_names(name):
    assert parse_launch(LAUNCH, database=name).database == name


# --- the decoded breakdown a human reads before saving ---------------------


def test_the_breakdown_says_who_where_and_what(key):
    access = parse_access(f"ssh -i {key} -p 2200 ubuntu@h.example.com sudo -n -u odoo -H")
    breakdown = describe(access, parse_launch(LAUNCH + " --no-http", database="acme"))
    assert breakdown["login"] == {"user": "ubuntu", "host": "h.example.com", "port": 2200}
    assert breakdown["become"] == {"user": "odoo", "home": True}
    assert breakdown["runs_as"] == "odoo"
    assert breakdown["launch"]["executable"] == "/opt/odoo/13.0/env/bin/python"
    assert breakdown["launch"]["config"] == "/opt/odoo/13.0/odoo.conf"
    assert breakdown["database"] == "acme"
    assert breakdown["warnings"] == []
    assert breakdown["keys"] == [key]


def test_without_a_become_it_runs_as_the_login_user(key):
    breakdown = describe(parse_access(f"ssh -i {key} ubuntu@h"), parse_launch(LAUNCH))
    assert breakdown["runs_as"] == "ubuntu"
    assert breakdown["become"] is None


def test_the_breakdown_shows_the_assembled_form_so_a_prefix_does_not_look_like_a_command(key):
    access = parse_access(f"ssh -i {key} ubuntu@h sudo -n -u odoo -H")
    assembled = describe(access, parse_launch(LAUNCH))["assembled"]
    assert assembled.startswith(f"ssh -i {key} -- ubuntu@h sudo -n -u odoo -H sh -c ")
    assert "exec 3<&0" in assembled
    assert "odoo-bin shell" in assembled


@pytest.mark.parametrize(
    "tail", ["sudo -n -u root -H", "sudo -n"]
)
def test_becoming_root_warns_rather_than_forbids(key, tail):
    """Odoo as root writes root-owned files into the filestore. Not ours to forbid."""
    breakdown = describe(parse_access(f"ssh -i {key} ubuntu@h {tail}"), parse_launch(LAUNCH))
    root_warned = any("root" in warning for warning in breakdown["warnings"])
    assert root_warned == ("root" in tail or tail == "sudo -n")


def test_logging_in_as_root_warns(key):
    breakdown = describe(parse_access(f"ssh -i {key} root@h"), parse_launch(LAUNCH))
    assert any("root" in warning for warning in breakdown["warnings"])


def test_no_become_and_a_config_the_login_user_may_not_read_is_not_ours_to_guess(key):
    """The probe finds that out; the grammar has no opinion on the server."""
    launch = parse_launch(LAUNCH + " --no-http")
    assert describe(parse_access(f"ssh -i {key} ubuntu@h"), launch)["warnings"] == []


# --- assembly: data, not code ----------------------------------------------


def test_the_remote_command_survives_exactly_one_parse(key):
    access = parse_access(f"ssh -i {key} ubuntu@h sudo -n -u odoo -H")
    argv = access.ssh_argv(["sh", "-c", "exec 3<&0; echo 'a b'; echo $(id) `id` ; x"], base=("-T",))
    assert argv[:3] == ["ssh", "-T", "-i"]
    assert argv[argv.index("--") + 1] == "ubuntu@h"
    remote = argv[-1]
    assert shlex.split(remote) == [
        "sudo", "-n", "-u", "odoo", "-H", "sh", "-c",
        "exec 3<&0; echo 'a b'; echo $(id) `id` ; x",
    ]


def test_the_options_come_before_the_double_dash(key):
    access = parse_access(f"ssh -i {key} -p 2222 -J j.example.com ubuntu@h")
    argv = access.ssh_argv(["true"], base=("-T", "-o", "BatchMode=yes"))
    dash = argv.index("--")
    assert argv[:dash] == [
        "ssh", "-T", "-o", "BatchMode=yes", "-i", key, "-p", "2222", "-J", "j.example.com",
    ]
    assert argv[dash:] == ["--", "ubuntu@h", "true"]


def test_a_card_cannot_smuggle_an_argument_into_the_remote_command(key):
    """The launch is data. A token that looks like shell stays one argument."""
    launch = parse_launch("/opt/odoo-bin shell -c '/etc/odoo.conf; touch /tmp/pwned' '--x=$(id)'")
    from odoo_sheller.recipe import quote_all

    quoted = quote_all(launch.argv)
    assert shlex.split(quoted) == list(launch.argv)


def test_odoo_bin_is_the_token_before_shell():
    assert parse_launch(LAUNCH).odoo_bin == "/opt/odoo/13.0/odoo/odoo-bin"
    assert parse_launch("/usr/bin/odoo shell").odoo_bin == "/usr/bin/odoo"
    assert parse_launch("/usr/bin/env A=b /x/py /x/odoo-bin shell -c /c").odoo_bin == "/x/odoo-bin"


def test_shell_may_follow_a_leading_addons_path():
    """Odoo reads `--addons-path=` before the subcommand — it is the one option
    that may come first (`odoo/cli/command.py`) — so a launch written that way
    runs, and `odoo-bin` is still the script, not the option."""
    launch = parse_launch(
        "/venv/bin/python /odoo/odoo-bin --addons-path=/a,/b shell -c /etc/odoo.conf"
    )
    assert launch.odoo_bin == "/odoo/odoo-bin"
    assert launch.config == "/etc/odoo.conf"
    assert launch.argv.count("shell") == 1
    assert launch.argv[2:4] == ("--addons-path=/a,/b", "shell")


@pytest.mark.parametrize(
    "text",
    [
        # Odoo takes any other leading option as the legacy server command, and
        # `shell` after it is not a subcommand.
        "/venv/bin/python /odoo/odoo-bin --db_host=x shell",
        "/venv/bin/python /odoo/odoo-bin --db_host=x --addons-path=/a shell",
    ],
)
def test_no_other_option_may_come_before_shell(text):
    with pytest.raises(RecipeError, match="no `shell`"):
        parse_launch(text)


def test_the_missing_shell_message_says_where_options_go():
    with pytest.raises(RecipeError) as refused:
        parse_launch("/venv/bin/python /odoo/odoo-bin --db_host=x shell")
    assert "after `shell`" in str(refused.value)


# --- HTTP: the one thing a server's config can do to `shell` -------------------
#
# `shell` starts no HTTP server and no cron when the config says `workers = 0`.
# When it says more, Odoo takes the prefork path, which binds the HTTP port
# *before* the shell starts: beside the running service that is "Address
# already in use". `--no-http` or `--workers=0` prevents it.


def http_warnings(launch_text, key):
    access = parse_access(f"ssh -i {key} ubuntu@h")

    return [w for w in describe(access, parse_launch(launch_text))["warnings"] if "HTTP" in w]


@pytest.mark.parametrize(
    "flags",
    ["--no-http", "--no-xmlrpc", "--workers=0", "--workers 0", "--no-http --workers=2"],
)
def test_a_launch_that_turns_http_off_is_not_warned_about_it(key, flags):
    assert parse_launch(f"{LAUNCH} {flags}").http_off is True
    assert http_warnings(f"{LAUNCH} {flags}", key) == []


@pytest.mark.parametrize("flags", ["", "--workers=4", "--workers 2", "--log-level=debug"])
def test_a_launch_that_leaves_http_alone_is_warned_about_the_port(key, flags):
    launch = parse_launch(f"{LAUNCH} {flags}")
    assert launch.http_off is False
    warnings = http_warnings(f"{LAUNCH} {flags}", key)
    assert len(warnings) == 1
    assert "Address already in use" in warnings[0]
    assert "--no-http" in warnings[0]


@pytest.mark.parametrize(
    "flags",
    ["--xmlrpc-port 8068", "--http-port 8070", "--http-port=8070", "-p 8070", "-p8070"],
)
def test_moving_the_port_is_not_the_same_as_turning_http_off(key, flags):
    """It works for as long as nothing else listens on the new port, which is a
    thing to keep true rather than a thing that cannot go wrong."""
    launch = parse_launch(f"{LAUNCH} {flags}")
    assert launch.http_off is False
    assert launch.http_port == "8068" if "8068" in flags else launch.http_port == "8070"
    warnings = http_warnings(f"{LAUNCH} {flags}", key)
    assert len(warnings) == 1
    assert "moves" in warnings[0]
    assert "--no-http" in warnings[0]


def test_an_http_flag_before_shell_is_not_one_of_the_launchs_arguments():
    """`shell` is the subcommand; what follows it is Odoo's, what precedes it is
    the interpreter's own."""
    assert parse_launch("/x/py -p /x/odoo-bin shell").http_off is False
