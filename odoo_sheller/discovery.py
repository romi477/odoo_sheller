"""Live discovery of targets: containers, then one probe inside a chosen one."""

import asyncio
import json

SUPPORTED_MAJOR = 19

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


async def probe(container: str, runner=None) -> dict:
    runner = runner or _docker
    argv = ["docker", "exec", "-i", container, "python3", "-"]
    code, out, err = await runner(argv, PROBE_SOURCE)
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
            "ok": False, "odoo_bin": None, "odoo_version": None, "odoo_major": None,
            "python": None, "config": None, "db_name": None, "databases": [],
            "supported": False,
            "error": (err.strip() or out.strip() or f"probe failed with code {code}"),
        }
    payload["supported"] = payload.get("odoo_major") == SUPPORTED_MAJOR
    if payload.get("ok") and not payload["supported"]:
        payload["error"] = (
            f"Odoo {payload.get('odoo_version')} found; only {SUPPORTED_MAJOR} is supported"
        )
    payload.setdefault("db_name", None)

    return payload
