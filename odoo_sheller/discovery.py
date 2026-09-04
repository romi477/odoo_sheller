"""Live discovery of targets: containers, then one probe inside a chosen one."""

import asyncio
import json
import re

from odoo_sheller.transport import SSH_OPTS

# 15 through 19. Everything the bootstrap rests on is the same in all five:
# the non-tty branch of `console()`, the names `env` and `self`, the rollback
# around it, SIGINT, the cursor that commits on a clean exit. What moved since
# 15 — `flush_all`/`invalidate_all`, `odoo/tests/shell.py`, where the test
# result object lives, `run_suite`'s signature — the bootstrap feature-detects
# rather than switching on the number here. See docs/architecture.md.
SUPPORTED_MAJORS = (15, 16, 17, 18, 19)
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

async def _docker(argv: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin.encode("utf-8") if stdin is not None else None)

    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def list_containers(runner=None) -> list[dict]:
    runner = runner or _docker
    code, out, err = await runner(["docker", "ps", "--format", "json"], None)
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


def _unreadable(code: int, out: str, err: str) -> dict:

    return {
        "ok": False, "odoo_bin": None, "odoo_version": None, "odoo_major": None,
        "python": None, "config": None, "db_name": None, "databases": [],
        "stage": None, "supported": False,
        "error": (err.strip() or out.strip() or f"probe failed with code {code}"),
    }


def _gate_on_version(payload: dict) -> dict:
    """Refuse an unsupported major here, not on the first command."""
    payload["supported"] = payload.get("odoo_major") in SUPPORTED_MAJORS
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
    argv = ["docker", "exec", "-i", container, "python3", "-"]
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
    argv = ["ssh", *SSH_OPTS, f"{build}@{host}", "python3 -"]
    code, out, err = await runner(argv, OOSH_PROBE_SOURCE)
    payload = _last_json_line(out)

    return _gate_on_version(payload) if payload else _unreadable(code, out, err)


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
    argv = ["docker", "exec", "-i", container, "python3", "-", module]
    code, out, err = await runner(argv, LIST_TESTS_SOURCE)
    payload = None
    for line in reversed(out.splitlines()):
        if line.strip().startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
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
