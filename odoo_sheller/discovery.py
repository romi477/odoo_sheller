"""Live discovery of targets: containers, then one probe inside a chosen one."""

import asyncio
import contextlib
import json
import re

from odoo_sheller.recipe import Access, Launch
from odoo_sheller.transport import SSH_OPTS, docker_bin, ssh_destination

# 15 through 20. Everything the bootstrap rests on is the same in all six:
# the non-tty branch of `console()`, the names `env` and `self`, the rollback
# around it, SIGINT, the cursor that commits on a clean exit. What moved since
# 15 — `flush_all`/`invalidate_all`, `odoo/tests/shell.py`, where the test
# result object lives, `run_suite`'s signature — the bootstrap feature-detects
# rather than switching on the number here. See docs/architecture.md.
#
# 20 reports itself as 19.5 until it ships, so a master container already
# passes this gate as a 19; the entry here is for the day the number changes.
# It is verified on such a container, and the reading was not enough: sessions
# and transactions worked untouched, but `run_tests` died of AttributeError
# because 20 dropped `httpd` from ThreadedServer while `odoo/tests/shell.py`
# still reads it. See `_os_run_test` in bootstrap.py.
SUPPORTED_MAJORS = (15, 16, 17, 18, 19, 20)
MODULE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

PROBE_SOURCE = r'''
import configparser, json, os, re, shutil, sys

result = {"ok": False, "odoo_bin": None, "odoo_version": None, "odoo_major": None,
          "python": "%d.%d.%d" % sys.version_info[:3], "config": None,
          "db_name": None, "databases": [], "error": None}

candidates = [shutil.which("odoo-bin"), "/opt/odoo/odoo-bin", "/usr/bin/odoo",
              "/odoo/odoo-bin", "/mnt/odoo/odoo-bin", "/usr/lib/python3/dist-packages/odoo-bin"]
for path in candidates:
    if path and os.path.exists(path):
        result["odoo_bin"] = path
        break

if not result["odoo_bin"]:
    result["error"] = "odoo-bin not found"
    print(json.dumps(result))
    raise SystemExit(0)

# Read the version from release.py as text: importing odoo is slow and can fail
# for reasons that have nothing to do with whether this is an Odoo container.
release = None
roots = [os.path.dirname(result["odoo_bin"]), "/usr/lib/python3/dist-packages"]
for root in roots:
    guess = os.path.join(root, "odoo", "release.py")
    if os.path.exists(guess):
        release = guess
        break
if release:
    text = open(release, encoding="utf-8").read()
    match = re.search(r"version_info\s*=\s*\(([^)]*)\)", text)
    if match:
        parts = [p.strip().strip("'\"") for p in match.group(1).split(",")]
        result["odoo_version"] = ".".join(parts[:2])
        try:
            result["odoo_major"] = int(parts[0])
        except ValueError:
            result["odoo_major"] = None

for path in [os.environ.get("ODOO_RC"), "/etc/odoo/odoo.conf", "/etc/odoo.conf",
             "/opt/odoo.conf", os.path.join(os.path.dirname(result["odoo_bin"]), "odoo.conf"),
             os.path.expanduser("~/.odoorc")]:
    if path and os.path.exists(path):
        result["config"] = path
        break

# Odoo writes "False" into the config for unset values; treat that as unset.
def clean(value):
    if value is None or value in ("False", "false", ""):

        return None

    return value

params = {"host": "localhost", "port": 5432, "user": "odoo", "password": None}
if result["config"]:
    parser = configparser.ConfigParser()
    parser.read(result["config"])
    if parser.has_section("options"):
        options = parser["options"]
        result["db_name"] = clean(options.get("db_name"))
        params["host"] = clean(options.get("db_host")) or params["host"]
        params["port"] = int(clean(options.get("db_port")) or 5432)
        params["user"] = clean(options.get("db_user")) or params["user"]
        params["password"] = clean(options.get("db_password"))

try:
    import psycopg2
    conn = psycopg2.connect(dbname="postgres", connect_timeout=5, **params)
    cur = conn.cursor()
    cur.execute("SELECT datname FROM pg_database WHERE datistemplate = false "
                "AND datname <> 'postgres' ORDER BY datname")
    result["databases"] = [row[0] for row in cur.fetchall()]
    conn.close()
except Exception as exc:
    result["error"] = "database list unavailable: %s" % exc

result["ok"] = True
print(json.dumps(result))
'''

LIST_TESTS_SOURCE = r'''
import ast, configparser, json, os, shutil, sys

result = {"ok": False, "module": None, "path": None, "classes": [],
          "error": None, "error_code": None}

def fail(code, message):
    result["error_code"] = code
    result["error"] = message
    print(json.dumps(result))
    raise SystemExit(0)

if len(sys.argv) < 2 or not sys.argv[1]:
    fail("invalid_module_name", "module is required")

module = sys.argv[1]
result["module"] = module

odoo_bin = None
candidates = [shutil.which("odoo-bin"), "/opt/odoo/odoo-bin", "/usr/bin/odoo",
              "/odoo/odoo-bin", "/mnt/odoo/odoo-bin",
              "/usr/lib/python3/dist-packages/odoo-bin"]
for path in candidates:
    if path and os.path.exists(path):
        odoo_bin = path
        break
if not odoo_bin:
    fail("odoo_bin_not_found", "odoo-bin not found")

config = None
for path in [os.environ.get("ODOO_RC"), "/etc/odoo/odoo.conf", "/etc/odoo.conf",
             "/opt/odoo.conf", os.path.join(os.path.dirname(odoo_bin), "odoo.conf"),
             os.path.expanduser("~/.odoorc")]:
    if path and os.path.exists(path):
        config = path
        break

def clean(value):
    if value is None or value in ("False", "false", ""):

        return None

    return value

roots = []
if config:
    parser = configparser.ConfigParser()
    parser.read(config)
    if parser.has_section("options"):
        raw = clean(parser["options"].get("addons_path"))
        if raw:
            roots.extend([part.strip() for part in raw.split(",") if part.strip()])
roots.append(os.path.join(os.path.dirname(odoo_bin), "odoo", "addons"))
roots.append(os.path.dirname(odoo_bin))

seen = set()
ordered = []
for root in roots:
    if root not in seen:
        seen.add(root)
        ordered.append(root)

found = None
for root in ordered:
    candidate = os.path.join(root, module)
    if os.path.isfile(os.path.join(candidate, "__manifest__.py")) or \
            os.path.isfile(os.path.join(candidate, "__openerp__.py")):
        found = candidate
        break
if not found:
    fail("module_not_found", "module %s not on addons path" % module)

result["path"] = found


def read_source(path):
    handle = open(path, encoding="utf-8")
    try:

        return handle.read()
    finally:
        handle.close()


def imported_test_modules(tests_dir):
    """The test modules Odoo will actually load.

    odoo/tests/loader.py imports `<addon>.tests` and then walks its *module
    members* whose name starts with `test_`. A file becomes a member only by
    being imported in tests/__init__.py, so anything else on disk — a stale
    file, one in a subdirectory — is never run, and offering its spec would
    just come back `tests_run: 0`.
    """
    init = os.path.join(tests_dir, "__init__.py")
    if not os.path.isfile(init):

        return []
    try:
        tree = ast.parse(read_source(init))
    except SyntaxError:

        return []
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[-1])

    return sorted(name for name in names if name.startswith("test_"))


def is_test_class(node):
    """A class Odoo's loader would collect.

    It selects `issubclass(obj, TestCase)`, which no AST can resolve across
    imports — but every such class carries at least one `test_*` method, and
    that is checkable here. Keying off the class *name* instead would drop
    perfectly runnable classes that simply are not called Test-something.
    """
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and item.name.startswith("test_"):

            return True

    return False


collected = {}
tests_root = os.path.join(found, "tests")
if os.path.isdir(tests_root):
    for module_name in imported_test_modules(tests_root):
        filepath = os.path.join(tests_root, module_name + ".py")
        if not os.path.isfile(filepath):
            continue
        try:
            tree = ast.parse(read_source(filepath))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or not is_test_class(node):
                continue
            methods = collected.setdefault(node.name, set())
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and item.name.startswith("test_"):
                    methods.add(item.name)

classes = []
for class_name in sorted(collected):
    methods = []
    for method_name in sorted(collected[class_name]):
        methods.append({
            "name": method_name,
            "spec": "%s.%s.%s" % (module, class_name, method_name),
        })
    classes.append({
        "name": class_name,
        "spec": "%s.%s" % (module, class_name),
        "methods": methods,
    })
result["classes"] = classes
result["ok"] = True
print(json.dumps(result))
'''


OOSH_PROBE_SOURCE = r'''
import json, os, shutil, sys

# An odoo.sh build answers everything about itself from its own environment,
# so this asks rather than guesses: no hunting for odoo-bin's real directory,
# no config candidates, and no database list. The build has exactly one
# database, and the instance user cannot read pg_database anyway.
result = {"ok": False, "odoo_bin": None, "odoo_version": None, "odoo_major": None,
          "python": "%d.%d.%d" % sys.version_info[:3], "config": None,
          "db_name": None, "databases": [], "stage": None, "error": None}

result["odoo_bin"] = shutil.which("odoo-bin")

version = os.environ.get("ODOO_VERSION") or ""
result["odoo_version"] = version or None
try:
    result["odoo_major"] = int(version.split(".")[0])
except ValueError:
    result["odoo_major"] = None

# staging / production. Read before a session is ever opened, because this is
# the word the commit guard turns on.
result["stage"] = os.environ.get("ODOO_STAGE") or None

database = os.environ.get("PGDATABASE") or None
result["db_name"] = database
result["databases"] = [database] if database else []

for candidate in [os.environ.get("ODOO_RC"),
                  os.path.expanduser("~/.config/odoo/odoo.conf")]:
    if candidate and os.path.exists(candidate):
        result["config"] = candidate
        break

missing = [name for name, value in (("odoo-bin on PATH", result["odoo_bin"]),
                                    ("ODOO_VERSION", result["odoo_version"]),
                                    ("PGDATABASE", database)) if not value]
if missing:
    result["error"] = "not an odoo.sh build: missing " + ", ".join(missing)
else:
    result["ok"] = True

print(json.dumps(result))
'''

# How long one discovery command may take. A probe is a handful of file reads
# and a database list with its own 5s connect timeout; `docker ps` is less.
# A paused container or a wedged Engine used to hang the request — and with it
# the Connect screen, or an agent's whole tool call — with no end at all.
DISCOVERY_TIMEOUT = 30.0


async def _docker(
    argv: list[str], stdin: str | None = None, timeout: float = DISCOVERY_TIMEOUT
) -> tuple[int, str, str]:
    """Run one discovery command: `docker …` locally, `ssh …` for odoo.sh.

    A program that is not installed is an answer, not a crash: the image has
    no `ssh`, and a GUI's PATH may have no `docker`. Both used to surface as
    a bare 500.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:

        return 127, "", f"{argv[0]}: not installed where this daemon runs"
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode("utf-8") if stdin is not None else None),
            timeout,
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()

        return 124, "", f"{' '.join(argv[:2])} did not answer within {timeout:.0f}s"

    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def list_containers(runner=None) -> list[dict]:
    runner = runner or _docker
    code, out, err = await runner([docker_bin(), "ps", "--format", "json"], None)
    if code != 0:
        raise RuntimeError(err.strip() or "docker ps failed")
    containers = []
    for line in out.splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        containers.append({
            "id": raw.get("ID"),
            "name": raw.get("Names"),
            "image": raw.get("Image"),
            "status": raw.get("Status"),
        })

    return containers


def _last_json_line(out: str) -> dict | None:
    """The probe prints one JSON object; Odoo may print anything before it."""
    for line in reversed(out.splitlines()):
        if line.strip().startswith("{"):
            try:

                return json.loads(line)
            except ValueError:
                continue

    return None


#: A container that cannot run the probe at all, or runs it and has no Odoo in
#: it, is not a target and never will be without being rebuilt. The UI keeps
#: those out of the way instead of showing each one a failure it can do nothing
#: about — a database container sitting next to the Odoo it serves is the
#: ordinary case, not an error anyone needs to read twice.
NOT_A_TARGET = ("no_python", "no_odoo_bin")


def _unreadable(code: int, out: str, err: str) -> dict:
    raw = err.strip() or out.strip() or f"probe failed with code {code}"
    # Docker's own words when the image has no interpreter: "exec: \"python3\":
    # executable file not found in $PATH", wrapped in OCI runtime noise. The
    # code is what the UI keys off; the raw line stays for anyone debugging.
    no_python = "executable file not found" in raw and "python3" in raw

    return {
        "ok": False, "odoo_bin": None, "odoo_version": None, "odoo_major": None,
        "python": None, "config": None, "db_name": None, "databases": [],
        "stage": None, "supported": False,
        "error_code": "no_python" if no_python else None,
        "error": "no python3 in this container" if no_python else raw,
        "error_detail": raw if no_python else None,
    }


def version_refusal(version: str | None) -> str | None:
    """Why an Odoo version is not one this tool runs on, or None.

    For a version that is only known once a session says it — a server whose
    probe could not read `odoo/release.py`. A version that cannot be read at
    all is not a reason to refuse: nothing says it is wrong.
    """
    match = re.match(r"\D*(\d+)\.", version or "")
    if match is None or int(match.group(1)) in SUPPORTED_MAJORS:

        return None
    supported = ", ".join(str(major) for major in SUPPORTED_MAJORS)

    return f"Odoo {version} found; supported: {supported}"


def _gate_on_version(payload: dict) -> dict:
    """Refuse an unsupported major here, not on the first command."""
    payload["supported"] = payload.get("odoo_major") in SUPPORTED_MAJORS
    payload.setdefault("error_detail", None)
    if payload.get("error") == "odoo-bin not found":
        payload["error_code"] = "no_odoo_bin"
        payload["error"] = "no odoo-bin in this container"
    payload.setdefault("error_code", None)
    if payload.get("ok") and not payload["supported"]:
        supported = ", ".join(str(major) for major in SUPPORTED_MAJORS)
        payload["error"] = (
            f"Odoo {payload.get('odoo_version')} found; supported: {supported}"
        )
    payload.setdefault("db_name", None)
    payload.setdefault("stage", None)

    return payload


async def probe(container: str, runner=None) -> dict:
    runner = runner or _docker
    argv = [docker_bin(), "exec", "-i", container, "python3", "-"]
    code, out, err = await runner(argv, PROBE_SOURCE)
    payload = _last_json_line(out)

    return _gate_on_version(payload) if payload else _unreadable(code, out, err)


async def probe_odoosh(build: str, host: str, runner=None) -> dict:
    """What an odoo.sh build says about itself.

    A build is entered, not discovered — there is no `docker ps` for odoo.sh —
    so this is the whole of target discovery for that kind, and it is four
    environment variables rather than a search.
    """
    runner = runner or _docker
    argv = ["ssh", *SSH_OPTS, "--", ssh_destination(build, host), "python3 -"]
    code, out, err = await runner(argv, OOSH_PROBE_SOURCE)
    payload = _last_json_line(out)

    return _gate_on_version(payload) if payload else _unreadable(code, out, err)


# What a server reached by ssh says about itself. Plain POSIX sh, because the
# one thing every such server has is `sh`: the interpreter named in the card may
# be a venv that is the very thing being checked, and the system's python3 may
# not exist or may not be the one Odoo needs. What the card said arrives as
# arguments ($1 the executable, $2 the config or empty, $3 odoo-bin) and is
# only ever quoted expansions here: nothing is spliced in, nothing evaluates.
SSH_PROBE_SCRIPT = r'''
emit() { printf '%s=%s\n' "$1" "$2"; }
emit effective_user "$(id -un 2>/dev/null)"
emit login_user "${SUDO_USER:-}"
emit host "$(hostname 2>/dev/null)"
if [ -f "$1" ] && [ -x "$1" ]; then emit executable_ok 1; else emit executable_ok 0; fi
case "${1##*/}" in
  python*) emit interpreter "$("$1" --version 2>&1 | head -n 1)" ;;
esac
if [ -n "$2" ]; then
  if [ -r "$2" ]; then
    emit config_readable 1
    emit db_name "$(sed -n 's/^[[:space:]]*db_name[[:space:]]*=[[:space:]]*//p' "$2" | head -n 1)"
  else
    emit config_readable 0
  fi
fi
release="$(dirname "$3")/odoo/release.py"
if [ -r "$release" ]; then
  emit odoo_version "$(sed -n 's/^version_info *= *(\([0-9]*\), *\([0-9]*\).*/\1.\2/p' "$release" | head -n 1)"
fi
'''

# What a failure of ssh or sudo says, and what to do about it. First match wins.
_NEEDS_NOPASSWD = (
    "sudo wants a password and there is nobody to type one: the login user needs "
    "NOPASSWD sudo to that user (see `sudo -l` on the server)"
)
_SSH_HINTS = (
    (
        "REMOTE HOST IDENTIFICATION HAS CHANGED",
        "host_key_changed",
        (
            "the server's host key has changed since it was trusted: that is either a "
            "rebuilt server or someone in the way. Check which, then fix known_hosts by hand"
        ),
    ),
    (
        "Host key verification failed",
        "host_key",
        (
            "this server's host key is not trusted yet: connect once from a terminal "
            "(the same ssh, to the same destination) and accept it. The daemon does "
            "not accept an unknown host key on its own"
        ),
    ),
    (
        "Permission denied (publickey",
        "auth",
        "the server refused the key: check the user and the -i key in Access",
    ),
    (
        "Could not resolve hostname",
        "unreachable",
        "check the host name in Access — it did not resolve",
    ),
    (
        "Connection refused",
        "unreachable",
        (
            "this machine could not reach the server on that port: check the host, "
            "the port and any firewall"
        ),
    ),
    (
        "Connection timed out",
        "unreachable",
        "this machine could not reach the server: it did not answer in time",
    ),
    (
        "No route to host",
        "unreachable",
        "this machine could not reach the server: there is no route to it",
    ),
    ("a password is required", "sudo", _NEEDS_NOPASSWD),
    ("no tty present", "sudo", _NEEDS_NOPASSWD),
    ("unknown user", "sudo", "the user named after sudo -u does not exist on the server"),
    (
        "not allowed to execute",
        "sudo",
        "sudo does not let the login user become that user: see `sudo -l` on the server",
    ),
    ("not in the sudoers file", "sudo", "the login user is not in the server's sudoers"),
)


def _ssh_failure(code: int, out: str, err: str) -> dict:
    raw = err.strip() or out.strip() or f"the probe failed with code {code}"
    error, error_code = raw, None
    for needle, hint_code, hint in _SSH_HINTS:
        if needle in raw:
            error, error_code = f"{raw.splitlines()[-1]} — {hint}", hint_code
            break

    return {
        "ok": False, "supported": False, "version_known": False,
        "odoo_bin": None, "odoo_version": None, "odoo_major": None,
        "python": None, "config": None, "db_name": None, "databases": [],
        "stage": None, "error_code": error_code, "error": error, "error_detail": raw,
    }


def _facts(out: str) -> dict:
    facts = {}
    for line in out.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.isidentifier():
            facts[key] = value.strip()

    return facts


async def probe_ssh(access: Access, launch: Launch, runner=None) -> dict:
    """What a server reached by ssh says about itself, before a session opens.

    This runs the card's own Access — as the user it becomes — and reports what
    the card is about to run as: who that is, where, which interpreter, whether
    the executable and the config are there for that user, and which Odoo.
    Nothing is started and no database is touched. Reading the version out of
    `odoo/release.py` beside `odoo-bin` is a convenience, not a requirement:
    when it is not there the answer says so and the gate is applied when the
    session says what it is.
    """
    runner = runner or _docker
    argv = access.ssh_argv(
        [
            "sh", "-c", SSH_PROBE_SCRIPT, "sh",
            launch.argv[0], launch.config or "", launch.odoo_bin,
        ],
        base=SSH_OPTS,
    )
    code, out, err = await runner(argv, None)
    facts = _facts(out)
    if "effective_user" not in facts:

        return _ssh_failure(code, out, err)

    user = facts["effective_user"]
    where = facts.get("host") or access.host
    version = facts.get("odoo_version") or None
    major = int(version.split(".")[0]) if version and version.split(".")[0].isdigit() else None
    interpreter = (facts.get("interpreter") or "").split()
    db_name = facts.get("db_name") or None
    payload = {
        "ok": True,
        "effective_user": user,
        "login_user": facts.get("login_user") or access.user,
        "host": where,
        "python": interpreter[1] if len(interpreter) > 1 and interpreter[0] == "Python" else None,
        "executable": launch.argv[0],
        "executable_ok": facts.get("executable_ok") == "1",
        "config": launch.config,
        "config_readable": (
            None if not launch.config else facts.get("config_readable") == "1"
        ),
        "odoo_bin": launch.odoo_bin,
        "odoo_version": version,
        "odoo_major": major,
        "db_name": None if db_name in (None, "False", "false") else db_name,
        "databases": [],
        "stage": None,
        "error": None,
        "error_code": None,
        "error_detail": None,
        "version_known": major is not None,
    }
    if not payload["executable_ok"]:
        payload.update(
            ok=False,
            error_code="launch_not_executable",
            error=(
                f"{launch.argv[0]} is not an executable file on {where} for user {user}"
            ),
        )
    elif launch.config and not payload["config_readable"]:
        payload.update(
            ok=False,
            error_code="config_unreadable",
            error=(
                f"the config {launch.config} is not readable by {user} on {where}. "
                "A server's config is usually readable only by its Odoo user: "
                "if that is not who this is, Access has to become it "
                "(sudo -n -u USER -H)"
            ),
        )
    if major is None:
        payload["supported"] = payload["ok"]

        return payload

    return _gate_on_version(payload)


async def list_tests(container: str, module: str, runner=None) -> dict:
    if not module or MODULE_NAME_RE.match(module) is None:

        return {
            "ok": False,
            "module": module,
            "path": None,
            "classes": [],
            "error": "invalid_module_name",
            "error_code": "invalid_module_name",
            "recovery": (
                "pass an addon technical name (letters, digits, underscore), "
                "not a test spec"
            ),
        }
    runner = runner or _docker
    argv = [docker_bin(), "exec", "-i", container, "python3", "-", module]
    code, out, err = await runner(argv, LIST_TESTS_SOURCE)
    payload = _last_json_line(out)
    if payload is None:

        return {
            "ok": False,
            "module": module,
            "path": None,
            "classes": [],
            "error": (err.strip() or out.strip() or f"list tests failed with code {code}"),
            "error_code": None,
        }

    return payload
