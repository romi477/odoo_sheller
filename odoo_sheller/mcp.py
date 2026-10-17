"""MCP server: the same HTTP API, exposed as tools for an agent.

Runs over stdio, which carries JSON-RPC — so nothing here may write to stdout.
Use `logging` (it writes to stderr) and never `print`.

This module holds no session logic. Every tool is one call to the daemon, which
must already be running: Claude Desktop restarts its MCP servers freely, and a
daemon started as a child of this process would take every live session down
with it on each restart.
"""

import ast
import asyncio
import json
import logging
import os
import secrets
from typing import Any

import httpx2 as httpx
from mcp.server import MCPServer
from mcp_types import ToolAnnotations

from odoo_sheller import guidance, rules
from odoo_sheller.protocol import spec_of

logger = logging.getLogger(__name__)

def daemon_url() -> str:
    """Where the daemon is.

    ODOO_SHELLER_URL wins when set: it is the only one of the two that can name
    a host. Otherwise follow ODOO_SHELLER_PORT, because in the container image
    that variable is what moves the listener — and this server usually runs
    inside that same container, started by `docker exec`. The two used to
    disagree in silence, and the first sign of it was every tool answering
    daemon_unreachable about a daemon that was running two lines away.
    """
    explicit = os.environ.get("ODOO_SHELLER_URL")
    if explicit:

        return explicit

    return f"http://127.0.0.1:{os.environ.get('ODOO_SHELLER_PORT', '8765')}"


DAEMON_URL = daemon_url()
AGENT_LABEL = "mcp-agent"
# How long a call waits for the daemon to answer. MCP hosts cut a tool call off
# at around a minute. This is our patience only: the daemon's own ceiling for
# a command stays five minutes, and a command that outlasts this wait keeps
# running — see `os_exec`.
CALL_WAIT = 40.0
# os_run_test's ceiling when none is passed, by what the spec names: one test
# is usually seconds, a class minutes, a module tens of minutes.
DEFAULT_TEST_TIMEOUTS = {"method": 30.0, "class": 300.0, "module": 1800.0}
# `Session.start` waits this long for the bootstrap's hello. Giving up on the
# open call any earlier strands a session the daemon then goes on to register,
# whose write key was only ever in the response we stopped waiting for.
SESSION_START_CEILING = 90.0
MAX_TEST_TIMEOUT = 3600.0  # matches the API's own ceiling on RunTestBody
# How long one tool call may block. MCP hosts cut a call off at around a
# minute, and nothing we set on our side changes that — so a long run has to
# be handed back as "still going" rather than held onto until the host kills
# it. Override for a host with a different patience.
MCP_CALL_BUDGET = float(os.environ.get("ODOO_SHELLER_MCP_BUDGET", "40"))
# The floor for the run leg. If opening the session already ate the budget we
# still have to send the request — otherwise nothing runs at all and the
# session id we hand back points at an idle session.
RUN_START_GRACE = 5.0
TEST_RESULT_POLL = 2.0  # how often os_test_result looks while it waits
# Hosts deliver only the first 2048 characters of a server's instructions —
# measured on two of them, cut mid-sentence at exactly this offset. Everything
# past it never reaches the model, so INSTRUCTIONS has to fit and the rest
# lives in HELP, behind os_help.
INSTRUCTION_CAP = 2048
MAX_STDOUT = 4000
# A source read is what an agent would otherwise get with `sed`, so the budget
# is larger than a command's output — but still a budget: a whole file is
# rarely the question, and a range is one argument away.
MAX_SOURCE = 8000
# A module listing is counted in files, not characters: a truncated path is a
# path that leads nowhere.
MAX_LISTING = 400
MAX_RESULT = 2000
# A transcript page. A module run's journal is near a megabyte and the largest
# reach a hundred; a whole one in an answer is context nothing else can use.
MAX_JOURNAL = 20000

# The texts live in `guidance`, where a tool is a marker; these are the same
# texts with the markers spelled as this server's tools.
INSTRUCTIONS = guidance.render_for_mcp(guidance.INSTRUCTIONS)
HELP: dict[str, str] = {topic: guidance.render_for_mcp(text) for topic, text in guidance.HELP.items()}
HELP_ALIASES = guidance.HELP_ALIASES


mcp = MCPServer("odoo-sheller", instructions=INSTRUCTIONS)

# session id -> write key, for the sessions this server may type into.
_keys: dict[str, str] = {}

# The majors whose rules this server has already handed over, so the second
# session on a major gets a pointer, not the same few thousand characters
# again. A server is not always one conversation — Claude Desktop keeps one
# for all its chats — so the pointer says how to get them, not that they were
# read.
_rules_given: set[int] = set()

RULES_TOPIC = guidance.RULES_TOPIC


def _rules_topic(topic: str) -> str | None:
    text = guidance.rules_topic(topic)

    return None if text is None else guidance.render_for_mcp(text)


def _with_rules(described: dict) -> dict:
    """Hand over the rules for the major this session runs, once per major."""
    major = rules.major_of(described.get("odoo"))
    text = rules.for_major(major)
    text = None if text is None else guidance.render_for_mcp(text)
    if text is None:
        described["rules"] = {
            "topic": None,
            "note": (
                f"no rules for Odoo {described.get('odoo')}; check every name in "
                "the instance with os_source before relying on it"
            ),
        }

        return described
    topic = RULES_TOPIC.format(major)
    if major in _rules_given:
        described["rules"] = {
            "topic": topic,
            "note": (
                f"handed over with an earlier session of this server; call "
                f"os_help('{topic}') unless you read them in this conversation "
                "and still have them"
            ),
        }

        return described
    _rules_given.add(major)
    described["rules"] = {"topic": topic, "text": text}

    return described


def _clip(text: str | None, limit: int) -> tuple[str | None, bool]:
    """Agent context is the scarce resource here; the journal keeps the rest."""
    if text is None or len(text) <= limit:

        return text, False

    return text[:limit], True


def _clip_tail(text: str | None, limit: int) -> tuple[str | None, bool]:
    """Keep the end, not the beginning.

    For a test run's log the last line is the one worth having — the summary,
    or the failure that ended it. Clipping from the front hands back the test
    framework's boot chatter and drops the answer.
    """
    if text is None or len(text) <= limit:

        return text, False

    return text[-limit:], True


def _default_session(session_id: str | None) -> str | Any:
    if session_id:

        return session_id
    if len(_keys) == 1:

        return next(iter(_keys))
    if not _keys:

        return {
            "error": "no_session",
            "recovery": "call os_open_session first, or os_attach_session with an id and key",
        }

    return {
        "error": "ambiguous_session",
        "sessions": sorted(_keys),
        "recovery": "pass session_id explicitly: this server owns more than one session",
    }


async def _call(
    method: str,
    path: str,
    session_id: str | None = None,
    client_timeout: float | None = None,
    **kwargs,
) -> Any:
    headers = {}
    if session_id and session_id in _keys:
        headers["X-OS-Session-Key"] = _keys[session_id]
    try:
        async with httpx.AsyncClient(
            base_url=DAEMON_URL, timeout=client_timeout or CALL_WAIT
        ) as client:
            response = await client.request(method, path, headers=headers, **kwargs)
    except httpx.TimeoutException:
        # The daemon is still working — it never stopped answering. Telling
        # the agent to go start it would be wrong, and a retry would kick off
        # a brand-new run instead of checking on the slow one already going.
        logger.warning("request timed out waiting for the daemon")

        return {
            "error": "request_timed_out",
            "recovery": (
                "the daemon is still working, not down — call os_history or "
                "os_journal on the session to see whether it finished, rather "
                "than retrying this call"
            ),
        }
    except Exception as exc:  # noqa: BLE001 - any transport failure means the same thing
        logger.warning("daemon unreachable: %s", exc)

        return {
            "error": "daemon_unreachable",
            "url": DAEMON_URL,
            "recovery": "ask the human to start it: uv run python -m odoo_sheller",
        }

    if response.status_code < 400:
        if response.headers.get("content-type", "").startswith("application/json"):

            return response.json()

        return {"text": response.text}

    detail = response.json().get("detail") if response.headers.get(
        "content-type", ""
    ).startswith("application/json") else response.text
    if isinstance(detail, dict):

        return detail

    return {"error": f"http_{response.status_code}", "message": detail}


@mcp.tool(
    description="List running Docker containers with what the probe found in each.",
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_list_containers() -> Any:
    containers = await _call("GET", "/api/containers")
    if isinstance(containers, dict):

        return containers
    probed = []
    for container in containers:
        probe = await _call("POST", "/api/probe", json={"container": container["name"]})
        probed.append({**container, "probe": probe})

    return {"containers": probed, "count": len(probed)}


@mcp.tool(
    description="List every live session with its owner and state. Read-only.",
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_list_sessions() -> Any:
    sessions = await _call("GET", "/api/sessions")
    if isinstance(sessions, dict):

        return sessions

    # Test sessions close themselves, so keys outlive the sessions they were
    # for. Drop the ones the daemon no longer has rather than reporting them.
    live = {session["id"] for session in sessions}
    for lost in [held for held in _keys if held not in live]:
        del _keys[lost]

    listed = [{**session, "yours": session["id"] in _keys} for session in sessions]

    return {
        "sessions": listed,
        "count": len(listed),
        "yours": sorted(_keys),
    }


@mcp.tool(
    description=(
        "Open a session you own. Pass replace=<lost session id> to reopen on the same "
        "target as a session that is gone; its variables are not restored."
    ),
)
async def os_open_session(
    container: str | None = None,
    database: str | None = None,
    odoo_bin: str | None = None,
    replace: str | None = None,
) -> Any:
    opened = await _call(
        "POST",
        "/api/sessions",
        json={
            "container": container,
            "database": database,
            "odoo_bin": odoo_bin,
            "replace": replace,
            "owner": {"kind": "agent", "label": AGENT_LABEL},
            # Left out, not false: false is an opt-out the daemon honours, and
            # would keep the right from a container a human switched on.
        },
    )
    if opened.get("error"):

        return opened
    _keys[opened["id"]] = opened.pop("write_key")

    return _with_rules(opened)


@mcp.tool(
    description=(
        "Current state of a session: owner, whether it is ready, and whether "
        "commit has been granted. Read-only. After asking for Grant commit, "
        "call this until allow_commit is true — do not wait for a chat message."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_session(session_id: str | None = None) -> Any:
    target = _default_session(session_id)
    if isinstance(target, dict):

        return target

    return await _call("GET", f"/api/sessions/{target}", target)


@mcp.tool(
    description=(
        "Adopt a session a human handed over, using the id and write key they gave "
        "you. The id alone grants nothing; never attach with a key you were not given."
    ),
)
async def os_attach_session(session_id: str, write_key: str) -> Any:
    _keys[session_id] = write_key
    described = await _call("GET", f"/api/sessions/{session_id}", session_id=session_id)
    if described.get("error"):
        _keys.pop(session_id, None)

        return described
    # The daemon says what the key is worth here. A wrong one used to attach
    # without complaint and fail on the first command instead.
    status = described.pop("key_status", "owner")
    if status != "owner":
        _keys.pop(session_id, None)

        return {
            "error": "not_owner",
            "session_id": session_id,
            "key_status": status,
            "owner": described.get("owner"),
            "recovery": (
                "that key no longer types here — ownership moved since it was "
                "issued; ask the human for the current one"
                if status == "former_owner" else
                "that key does not open this session; ask the human to hand it "
                "over again — a key is shown once, at the handover"
            ),
        }

    return _with_rules(described)


@mcp.tool(
    description=(
        "Run Python in a session you own. Blocks until the command finishes. "
        "Variables persist between calls; one command runs at a time. Returns "
        "stdout, the returned value, any error, and `stderr_lines` — how many "
        "lines Odoo logged while the command ran. Pass stderr=True to get "
        "those lines back as `stderr` (clipped from the end); they cost "
        "context, so ask when the log is what you are after. Either way the "
        "log is kept: read it with os_journal instead of running the command "
        "again. Never install a logging handler to capture it."
    ),
)
async def os_exec(
    code: str, session_id: str | None = None, stderr: bool = False
) -> Any:
    target = _default_session(session_id)
    if isinstance(target, dict):

        return target
    result = await _call("POST", f"/api/sessions/{target}/exec", target, json={"code": code})
    if result.get("error") == "request_timed_out":
        # Our wait ran out, not the command: it runs on in the container.

        return {
            **result,
            "session_id": target,
            "recovery": (
                "the command is still running and the session stays busy until "
                "it ends, or until the daemon interrupts it at five minutes; do "
                "not run it again — os_history shows when it finished and what "
                "it returned, os_interrupt stops it now"
            ),
        }
    if result.get("error") and "stdout" not in result:

        return result

    stdout, stdout_clipped = _clip(result.get("stdout"), MAX_STDOUT)
    value, value_clipped = _clip(result.get("result"), MAX_RESULT)
    log = result.get("stderr") or []
    clipped = stdout_clipped or value_clipped
    answer = {
        "stdout": stdout,
        "result": value,
        "error": result.get("error"),
        # One number rather than the lines: it says a log exists — which is
        # the thing an agent cannot guess — without billing the context for
        # content nobody asked for.
        "stderr_lines": len(log),
        "duration": result.get("duration"),
    }
    if stderr:
        # Clipped from the end, as in os_run_test: on a long command the last
        # line is the one that says what happened.
        text, stderr_clipped = _clip_tail("\n".join(log), MAX_STDOUT)
        clipped = clipped or stderr_clipped
        answer["stderr"] = text
        answer["stderr_truncated"] = bool(result.get("stderr_truncated"))
    answer["truncated"] = clipped
    # The journal is the way back to a log that was not asked for, and to
    # anything the clipping dropped — without running the command again.
    answer["journal"] = (
        f"/api/journals/{target}" if clipped or (log and not stderr) else None
    )

    return answer


@mcp.tool(
    description=(
        "Read Odoo source from inside the running instance, instead of "
        "shelling into the container — that reads a disk this session may not "
        "run from, and cannot say which override is in effect. Three forms. "
        "A directory: os_source(path='sale') lists every file in the module "
        "with its line count. A file: os_source(path='sale/models/sale_order"
        ".py', first=100, last=180) — module-relative or absolute inside an "
        "addons directory, 1-based inclusive, anything outside refused. A "
        "method: os_source(model='sale.order', method='action_confirm') "
        "returns the source that actually runs plus `overrides`, the modules "
        "defining it in resolution order — first is the one that runs, last "
        "is the base. Add module='sale' to read that link instead. Needs a "
        "session you own; leaves no names in its namespace."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_source(
    path: str | None = None,
    model: str | None = None,
    method: str | None = None,
    module: str | None = None,
    first: int | None = None,
    last: int | None = None,
    session_id: str | None = None,
) -> Any:
    if path and model:

        return {
            "error": "ambiguous_request",
            "recovery": "pass path (a file) or model (the code that runs), not both",
        }
    if not path and not model:

        return {
            "error": "nothing_to_read",
            "recovery": (
                "pass path='module/dir/file.py' with an optional first/last line "
                "range, or model='res.partner' with an optional method"
            ),
        }
    if method and not model:

        return {"error": "method_without_model", "recovery": "pass model= as well"}
    if module and not method:

        return {
            "error": "module_without_method",
            "recovery": (
                "module= picks one link out of a method's override chain, so "
                "name the method too"
            ),
        }
    target = _default_session(session_id)
    if isinstance(target, dict):

        return target

    code = _source_snippet(path, model, method, first, last, module)
    # A read: journalled like any command, but not counted as work in the
    # transaction — or a handover would warn about it, and a test run report
    # it as discarded.
    result = await _call(
        "POST", f"/api/sessions/{target}/exec", target,
        json={"code": code, "read_only": True},
    )
    if result.get("error") and "stdout" not in result:

        return result
    failure = result.get("error")
    if failure:

        return {
            "error": "unreadable",
            "reason": f"{failure.get('type')}: {failure.get('message')}",
            "recovery": (
                "a path must be inside an addons directory and a model must be "
                "in the registry; os_help('code') has the rules"
            ),
        }
    try:
        # The session hands back the repr of the JSON string the snippet
        # returned, which is exactly one literal.
        payload = json.loads(ast.literal_eval(result.get("result") or "'{}'"))
    except (ValueError, SyntaxError):

        return {"error": "unreadable", "reason": "the read produced no payload"}
    if payload.get("error"):

        return payload
    if payload.get("directory"):

        return _clip_listing(payload)
    text, clipped = _clip(payload.get("text") or "", MAX_SOURCE)
    payload["text"] = text
    payload["truncated"] = clipped
    if clipped:
        payload["recovery"] = (
            "narrow it with first= and last=, or read one method by model="
        )

    return payload


def _clip_listing(payload: dict) -> dict:
    """A module can hold hundreds of files; a listing is still an answer."""
    entries = payload.get("entries") or []
    if len(entries) > MAX_LISTING:
        payload["entries"] = entries[:MAX_LISTING]
        payload["truncated"] = True
        payload["recovery"] = (
            f"{len(entries)} files: ask for a subdirectory, e.g. "
            f"path='{payload.get('path')}/models'"
        )
    else:
        payload["truncated"] = False

    return payload


def _source_snippet(path, model, method, first, last, module=None) -> str:
    """Python for the session to run, returning the payload as a JSON string.

    Everything happens inside one function so its imports and locals never
    reach the session, and the function pops its own name on the way out: a
    session is the human's workspace and a read has no business appearing in
    it. The value is returned rather than printed — this server's stdout is
    JSON-RPC, and a habit of printing is how that gets broken.
    """
    if path:
        # Python literals, not JSON ones: `json.dumps(None)` is `null`, which
        # is a NameError in the container and nowhere else — a mocked session
        # never runs this, so only a live one ever said so.
        opening = repr(int(first)) if first else "1"
        closing = repr(int(last)) if last else "len(lines)"
        body = f"""\
    import os
    from odoo.tools import file_open, file_path
    target = file_path({path!r})
    if os.path.isdir(target):
        # Entries are rooted at the module, not at the target's parent, so
        # every path that comes back can be handed straight back in.
        root = target
        while root != os.path.dirname(root):
            if os.path.exists(os.path.join(root, "__manifest__.py")):
                break
            root = os.path.dirname(root)
        base = os.path.dirname(root)
        # "What is in this module" is the question that comes before "read me
        # line 363", and it must not be the one thing left to the shell.
        entries = []
        for root, dirs, names in os.walk(target):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(names):
                if name.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, base)
                try:
                    with open(full, "rb") as handle:
                        count = sum(1 for _ in handle)
                except OSError:
                    count = None
                entries.append({{"path": rel, "lines": count}})
        payload = {{"path": {path!r}, "directory": True,
                   "entries": entries, "files": len(entries)}}
    else:
        with file_open({path!r}) as handle:
            lines = handle.read().splitlines()
        first = {opening}
        last = min({closing}, len(lines))
        payload = {{"path": {path!r}, "total_lines": len(lines),
                   "first": first, "last": last,
                   "text": chr(10).join(lines[first - 1:last])}}
"""
    else:
        # Every class in the MRO extends the model; almost none of them define
        # the method. On account.move.action_post that is 3 classes out of 52,
        # and the other 49 are noise in the answer.
        body = f"""\
    import inspect
    cls = type(env[{model!r}])
    method = {method!r}
    wanted = {module!r}

    def addon(klass):
        parts = klass.__module__.split(".")
        return parts[2] if klass.__module__.startswith("odoo.addons.") else parts[0]

    def link(klass):
        fn = klass.__dict__[method]
        try:
            line = inspect.getsourcelines(fn)[1]
        except (OSError, TypeError):
            line = None
        return {{"module": addon(klass), "where": klass.__module__,
                "file": inspect.getsourcefile(klass), "line": line}}

    if method:
        definers = [klass for klass in cls.__mro__ if method in klass.__dict__]
        chain = [link(klass) for klass in definers]
        if wanted:
            picked = [klass for klass in definers if addon(klass) == wanted]
            if not picked:
                payload = {{"error": "not_in_that_module", "model": {model!r},
                           "method": method, "module": wanted, "overrides": chain,
                           "recovery": "pick one of the modules in overrides, "
                                       "or drop module= for the one that runs"}}
                return json.dumps(payload)
            obj = picked[0].__dict__[method]
            owner = picked[0]
        else:
            obj = getattr(cls, method)
            owner = definers[0] if definers else cls
        payload = {{"model": {model!r}, "method": method, "module": wanted,
                   "file": inspect.getsourcefile(obj),
                   "line": inspect.getsourcelines(obj)[1],
                   "defined_in": getattr(owner, "__module__", None),
                   "overrides": chain,
                   "text": inspect.getsource(obj)}}
    else:
        payload = {{"model": {model!r}, "method": None,
                   "file": inspect.getsourcefile(cls),
                   "line": inspect.getsourcelines(cls)[1],
                   "defined_in": cls.__module__,
                   "overrides": [addon(klass) for klass in cls.__mro__
                                 if klass.__module__.startswith("odoo.addons.")],
                   "text": inspect.getsource(cls)}}
"""

    return (
        "def _os_read(_os_ns=globals()):\n"
        # First, not last: the call has already resolved the object, and a
        # read that raises must not leave the name behind either.
        '    _os_ns.pop("_os_read", None)\n'
        "    import json\n"
        + body
        + "    return json.dumps(payload)\n"
        "\n"
        "_os_read()"
    )


def _topics() -> list[str]:

    return list(HELP) + [RULES_TOPIC.format(major) for major in rules.majors()]


@mcp.tool(
    description=(
        "The parts of this server's instructions a host did not deliver. "
        "Hosts cut server instructions at 2048 characters, which is a small "
        "fraction of what there is to say, so the rest is here by topic: "
        "sessions, ownership, commit, remote_server, limits, orm, code, "
        "log, records, modules, jobs, tests, watching — and odoo_<major>, the "
        "Python and XML that Odoo major expects (odoo_17, odoo_19). Call with "
        "no topic for the list. Read-only, no session, no side effects — cheaper than working "
        "around a rule you were never shown."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_help(topic: str | None = None) -> Any:
    if topic is None:

        return {
            "topics": _topics(),
            "instructions_truncated_at": INSTRUCTION_CAP,
            "recovery": "call os_help(topic) for one of these",
        }
    topic = HELP_ALIASES.get(topic, topic)
    text = HELP.get(topic) or _rules_topic(topic)
    if text is None:

        return {
            "error": "no_such_topic",
            "topic": topic,
            "topics": _topics(),
            "recovery": "call os_help(topic) with one of these, or os_help() for the list",
        }

    return {"topic": topic, "text": text}


@mcp.tool(
    description=(
        "List test classes and methods in one addon, already shaped as "
        "os_run_test specs (module.TestClass / module.TestClass.test_method). "
        "One module per call. This is files on disk, not 'installed in this "
        "database'. Read-only; does not open a session."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_list_tests(module: str, container: str | None = None) -> Any:
    target = container
    if not target:
        session_id = _default_session(None)
        if isinstance(session_id, dict):
            refusal = dict(session_id)
            if refusal.get("error") == "no_session":
                refusal["recovery"] = "pass container, or open a session first"
            elif refusal.get("error") == "ambiguous_session":
                refusal["recovery"] = (
                    "pass container explicitly: this server owns more than one session"
                )

            return refusal
        described = await _call("GET", f"/api/sessions/{session_id}", session_id)
        if described.get("error"):

            return described
        if described.get("kind", "docker") != "docker":
            # The catalogue is read from a local container's disk; a build id,
            # or a card's name, in that slot would go to `docker exec` and fail
            # obscurely.

            return {
                "error": "not_a_container",
                "session_id": session_id,
                "recovery": (
                    "os_list_tests reads a local container's files; on a remote "
                    f"instance, list the module's tests with os_source(path='{module}/tests') "
                    "and read the file that holds the class"
                ),
            }
        target = described["container"]

    return await _call(
        "GET",
        f"/api/containers/{target}/tests",
        params={"module": module},
    )


RUN_TEST_DESCRIPTION = (
    "Run Odoo tests by name: 'module' for every standard test in that module, "
    "'module.TestClass' for one class, 'module.TestClass.test_method' for one "
    "method. A whole module answers with counts and `failed` — each failure as "
    "a spec to pass back here — and leaves its log in the journal; a class or "
    "a method also returns its log. Always opens its "
    "own brand-new session (owner agent, allow_commit false) rather than "
    "reusing one you already have, so there is never anything pending to "
    "lose, and that session closes itself once the run settles — there is "
    "nothing to clean up. Pass session_id instead to run in a session a "
    "human handed you — the only way onto a remote instance — and then it "
    "is theirs, closed neither by you nor by itself. "
    "A run too long to answer in one call "
    "comes back as {\"status\": \"running\", \"session_id\": ...}, which is "
    "not a failure: call os_test_result(session_id) to wait for it, and "
    "never call this tool again for the same run. Without a timeout the "
    "ceiling follows the spec — 30s for a method, 300s for a class, 1800s "
    "for a module, at most 3600 — and it is the ceiling for the run itself, "
    "not for this call. `params` goes on the test tag as `[params]`: Hoot's "
    "JS suites take it as a filter, e.g. "
    "('web.WebSuite.test_unit_desktop', params='@my_module'), and then the "
    "answer carries `hoot` with Hoot's own counts and failed test ids — "
    "os_help('tests')."
)


@mcp.tool(description=RUN_TEST_DESCRIPTION)
async def os_run_test(
    test: str,
    container: str | None = None,
    database: str | None = None,
    odoo_bin: str | None = None,
    timeout: float | None = None,
    session_id: str | None = None,
    params: str | None = None,
) -> Any:
    if timeout is None:
        timeout = DEFAULT_TEST_TIMEOUTS[_spec_form(test)]
        if params:
            # A run with params is a browser suite more often than not, and
            # Chrome alone takes about ten seconds to start: a method's thirty
            # is too few. Raised to a class's, never lowered from a module's.
            timeout = max(timeout, DEFAULT_TEST_TIMEOUTS["class"])
    if session_id and (container or database or odoo_bin):
        # One says where to run, the other says where to open, and the two can
        # name different places. Refuse rather than silently pick.
        return {
            "error": "ambiguous_target",
            "recovery": (
                "pass session_id to run in a session you were handed, or a "
                "container to open a fresh one — not both"
            ),
        }
    if not 0 < timeout <= MAX_TEST_TIMEOUT:
        # Checked before anything is opened: a doomed ceiling would otherwise
        # cost a whole session start to earn a raw validation error.
        return {
            "error": "invalid_timeout",
            "timeout": timeout,
            "recovery": (
                f"pass a timeout between 0 and {MAX_TEST_TIMEOUT:.0f} seconds; "
                "a whole class usually wants a few hundred"
            ),
        }

    # The host times the whole tool call, so the budget has to cover opening
    # the session as well as waiting for the run. Spending it on the run alone,
    # after a registry load that already took seconds, overshoots and the call
    # is killed before it can hand back the session id.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MCP_CALL_BUDGET

    if session_id:
        # A session a human handed over — the only way onto a remote instance,
        # since no tool here can name one. Not ours to close, and not opened
        # with autoclose, so it stays exactly as it was lent.
        return await _run_test_in(session_id, test, timeout, deadline, ours=False, params=params)

    # Container and database do not identify a session — several may be
    # opening on the same target at once — so the token is what finds this one
    # again if the open call is the thing that times out.
    client_token = f"{AGENT_LABEL}-{secrets.token_urlsafe(8)}"
    opened = await _call(
        "POST",
        "/api/sessions",
        # Bounded by the same budget as everything else: waiting out the
        # daemon's full ceiling only means the host kills the call first, and
        # then even the client_token below never reaches the caller.
        client_timeout=min(SESSION_START_CEILING + 10, MCP_CALL_BUDGET),
        json={
            "container": container,
            "database": database,
            "odoo_bin": odoo_bin,
            "owner": {"kind": "agent", "label": AGENT_LABEL},
            "allow_commit": False,
            "client_token": client_token,
            # It exists to run one test. Nobody has to remember to close it.
            "autoclose": True,
        },
    )
    if opened.get("error"):
        if opened.get("error") == "request_timed_out":

            return {
                **opened,
                "client_token": client_token,
                "recovery": (
                    "the session may still have opened — find it with "
                    "os_list_sessions by this client_token before opening "
                    "another, and ask the human to close it: this server never "
                    "received its write key"
                ),
            }

        return opened
    session_id = opened["id"]
    _keys[session_id] = opened.pop("write_key")

    return await _run_test_in(session_id, test, timeout, deadline, ours=True, params=params)


def _spec_form(test: str) -> str:
    """What a test spec names: a module, a class in it, or one method."""

    return ("module", "class", "method")[min(test.count("."), 2)]


async def _run_test_in(
    session_id: str, test: str, timeout: float, deadline: float, ours: bool,
    params: str | None = None,
) -> Any:
    """Run one test in a session and shape the outcome.

    `ours` says whether this server opened the session. It changes nothing
    about the run, and everything about what the answer may promise: a session
    we opened closes itself, and one we were lent is the human's to end.
    """
    loop = asyncio.get_running_loop()
    # Whatever is left of the budget, and never more: a margin added on top of
    # a cap defeats the cap. The daemon still gets the full ceiling the caller
    # asked for — only our own waiting is capped, so the run is never cut short.
    # Two seconds over the daemon's own ceiling keeps a short run from racing
    # its 504 against our timeout.
    run_leg = max(min(timeout + 2, deadline - loop.time()), RUN_START_GRACE)
    result = await _call(
        "POST", f"/api/sessions/{session_id}/run_test", session_id,
        client_timeout=run_leg,
        json={"test": test, "timeout": timeout, "params": params},
    )
    spec = spec_of(test, params=params)
    if result.get("error") == "request_timed_out":
        afterwards = (
            "that session closes itself when the run ends, so there is "
            "nothing to clean up"
            if ours else
            "that session is the human's: it stays open after the run, and "
            "you do not close it"
        )

        return {
            "status": "running",
            "session_id": session_id,
            "test": spec,
            "recovery": (
                "the run is still going in the container — call "
                f'os_test_result("{session_id}") to wait for it; {afterwards}'
            ),
        }
    if result.get("error") and "stdout" not in result:

        return {"session_id": session_id, **result}

    test_info = result.get("test") or {}
    stdout, stdout_clipped = _clip(result.get("stdout"), MAX_STDOUT)
    log, stderr_clipped = _test_log(test, result)
    truncated = stdout_clipped or stderr_clipped

    answer = {
        "session_id": session_id,
        "test": spec,
        "tests_run": test_info.get("tests_run"),
        "failures": test_info.get("failures"),
        "errors": test_info.get("errors"),
        "skipped": test_info.get("skipped"),
        "success": test_info.get("success"),
        "failed": result.get("failed", []),
        "stdout": stdout,
        **log,
        "error": result.get("error"),
        "duration": result.get("duration"),
        "discarded_pending": result.get("discarded_pending"),
        # Two different losses: the daemon dropping whole lines past its own
        # ceiling, and this server clipping characters to spare your context.
        "stderr_truncated": bool(result.get("stderr_truncated")),
        "truncated": truncated,
        "journal": (
            f"/api/journals/{session_id}"
            if truncated or "stderr_lines" in log else None
        ),
    }
    if "hoot" in result:
        # Hoot runs inside one Python test, so the counts above say 1 whatever
        # ran in the browser; these are what did.
        answer["hoot"] = result["hoot"]

    return answer


def _test_log(test: str, result: dict) -> tuple[dict, bool]:
    """The log of a run as an agent should get it, and whether it was clipped.

    For a class or a method the log is the answer — which test failed and
    why — so its tail comes back. A whole module logs thousands of lines that
    `failed` already distils, so it comes back as a count and stays in the
    journal; os_journal has every line.
    """
    lines = result.get("stderr") or []
    if "." not in test:

        return {"stderr_lines": len(lines)}, False
    # The tail: on a long run the last line is the summary, and clipping from
    # the front would drop it.
    stderr, clipped = _clip_tail("\n".join(lines), MAX_STDOUT)

    return {"stderr": stderr}, clipped


@mcp.tool(
    description=(
        "Wait for a test run started by os_run_test and return its outcome. "
        "Blocks while the run is still going, then answers with tests_run, "
        "failures, errors, skipped and success — or says the run is still "
        "going, in which case call it again straight away. Works after the "
        "session has closed itself, and needs no write key. Read-only."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_test_result(session_id: str) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MCP_CALL_BUDGET
    while True:
        feed = await _call("GET", f"/api/sessions/{session_id}/history")
        if feed.get("error"):

            return feed

        runs = [
            entry for entry in (feed.get("entries") or [])
            if entry.get("kind") == "run_test"
        ]
        if runs and (runs[-1].get("result") is not None):

            return {"status": "done", **_history_run_test_entry(runs[-1])}

        gone = (feed.get("session") or {}).get("state") == "gone"
        if gone:
            # The session ended without the run ever settling: the process
            # died mid-run. Saying "running" here would be a poll forever.

            return {
                "status": "lost",
                "session_id": session_id,
                "journal": f"/api/journals/{session_id}",
                "recovery": (
                    "the run died with its container process — read the "
                    "journal for the log tail, then start it again"
                ),
            }
        if loop.time() >= deadline:

            return {
                "status": "running",
                "session_id": session_id,
                "recovery": (
                    "still going — call os_test_result again straight away; "
                    "each call waits, so there is no need to pause between them"
                ),
            }
        await asyncio.sleep(TEST_RESULT_POLL)


def _boundary_result(answer: Any) -> Any:
    """Wire frames are for the daemon; an agent needs the outcome."""
    if answer.get("error") and "stdout" not in answer:

        return answer  # a refusal, already shaped

    return {"ok": not answer.get("error"), "error": answer.get("error")}


@mcp.tool(
    description=(
        "Discard the open transaction. Nothing is written to the database, and the "
        "namespace survives. This is the normal end of an experiment."
    ),
)
async def os_rollback(session_id: str | None = None) -> Any:
    target = _default_session(session_id)

    if isinstance(target, dict):

        return target

    return _boundary_result(await _call("POST", f"/api/sessions/{target}/rollback", target))


@mcp.tool(
    description=(
        "Write the open transaction to the database — the only tool here that "
        "persists anything. Refused with commit_not_allowed unless a human has "
        "granted this session the right: if refused, say what you want to write "
        "and why, then poll os_session until allow_commit is true. Retrying "
        "os_commit will not see the grant. On a session handed to you with work "
        "already pending, refused once as inherited_pending: that work is the "
        "previous owner's and a commit writes it too, so say so, then call with "
        "include_inherited=True."
    ),
    annotations=ToolAnnotations(destructive_hint=True),
)
async def os_commit(
    session_id: str | None = None, include_inherited: bool = False
) -> Any:
    target = _default_session(session_id)

    if isinstance(target, dict):

        return target
    if not include_inherited:
        # A handover carries the open transaction, so the first commit in a
        # lent session would write the human's work under the agent's name.
        # The rule was documented and an agent still had to remember it; this
        # is the same rule, spent as one refusal instead.
        described = await _call("GET", f"/api/sessions/{target}", target)
        inherited = (described or {}).get("inherited_pending") or 0
        if inherited:

            return {
                "error": "inherited_pending",
                "session_id": target,
                "inherited_pending": inherited,
                "pending_commands": described.get("pending_commands"),
                "reason": (
                    f"{inherited} command(s) in this transaction were run by the "
                    "previous owner; committing writes their work too"
                ),
                "recovery": (
                    "tell the human whose work is in the transaction and what "
                    "you are about to write, then call "
                    "os_commit(include_inherited=True) — or os_rollback to "
                    "discard all of it"
                ),
            }

    return _boundary_result(await _call("POST", f"/api/sessions/{target}/commit", target))


@mcp.tool(
    description=(
        "Interrupt the command running in a session you own. The session, its "
        "namespace and its transaction all survive."
    ),
)
async def os_interrupt(session_id: str | None = None) -> Any:
    target = _default_session(session_id)

    return target if isinstance(target, dict) else await _call(
        "POST", f"/api/sessions/{target}/interrupt", target
    )


@mcp.tool(
    description="Close a session you own. Uncommitted work is discarded.",
    annotations=ToolAnnotations(destructive_hint=True),
)
async def os_close_session(session_id: str | None = None) -> Any:
    target = _default_session(session_id)
    if isinstance(target, dict):

        return target
    closed = await _call("DELETE", f"/api/sessions/{target}", target)
    _keys.pop(target, None)

    return closed


def _actor(actor: dict | None) -> str | None:
    """`kind:label`, with the label falling back to the kind.

    It used to index `label`. A session handed over as `{"kind": "agent"}` —
    which the daemon accepted until it began naming them — made this raise
    KeyError, so os_history failed on the very session an agent was given.
    """
    if not actor:

        return None
    kind = actor.get("kind") or "unknown"

    return f"{kind}:{actor.get('label') or kind}"


def _history_run_test_entry(entry: dict) -> dict:
    """A run_test entry, in the shape os_run_test answers with.

    Journaled precisely so a transport timeout on a long class doesn't lose
    the outcome — the agent recovers it from here instead of nowhere.
    """
    result = entry.get("result") or {}
    test = result.get("test") or {}
    parts = (entry.get("module"), entry.get("test_class"), entry.get("test_method"))
    stdout, out_clipped = _clip(result.get("stdout"), MAX_STDOUT)
    # The same rule os_run_test answers by, so a run recovered later reads
    # the same as one answered at once — on the spec without its params: a
    # dot inside them is not a class.
    log, stderr_clipped = _test_log(spec_of(*parts), result)
    spec = spec_of(*parts, entry.get("params"))
    shaped = {
        "n": entry.get("ordinal"),
        "test": spec,
        "status": entry.get("status"),
        "tests_run": test.get("tests_run"),
        "failures": test.get("failures"),
        "errors": test.get("errors"),
        "skipped": test.get("skipped"),
        "success": test.get("success"),
        "failed": result.get("failed"),
        "stdout": stdout or None,
        "stderr": log.get("stderr") or None,
        "stderr_lines": log.get("stderr_lines"),
        # Whole lines the daemon dropped, as opposed to the characters clipped
        # just above. Absent from journals written before it existed.
        "stderr_truncated": result.get("stderr_truncated"),
        "error": result.get("error"),
        "duration": round(result["duration"], 3) if result.get("duration") is not None else None,
        "actor": _actor(entry.get("actor")),
    }
    if result.get("hoot"):
        shaped["hoot"] = result["hoot"]
    if out_clipped or stderr_clipped:
        shaped["truncated"] = True
    if entry.get("abandoned"):
        shaped["abandoned"] = True

    return {key: value for key, value in shaped.items() if value is not None}


def _history_entry(entry: dict) -> dict:
    """One past command, in the shape os_exec answers with.

    The journal keeps wire fields the daemon needs; an agent reading its own
    history needs the command and what came back, and pays for every other byte.
    """
    if entry.get("kind") == "run_test":

        return _history_run_test_entry(entry)
    if entry.get("kind") != "exec":

        return {"kind": entry.get("kind"), "actor": _actor(entry.get("actor"))}

    result = entry.get("result") or {}
    code, code_clipped = _clip(entry.get("code"), MAX_RESULT)
    stdout, out_clipped = _clip(result.get("stdout"), MAX_STDOUT)
    value, value_clipped = _clip(result.get("result"), MAX_RESULT)
    shaped = {
        "n": entry.get("ordinal"),
        "code": code,
        "status": entry.get("status"),
        "stdout": stdout or None,
        "result": value,
        "error": result.get("error"),
        "duration": round(result["duration"], 3) if result.get("duration") is not None else None,
        "actor": _actor(entry.get("actor")),
    }
    if code_clipped or out_clipped or value_clipped or result.get("stdout_truncated"):
        shaped["truncated"] = True
    if entry.get("abandoned"):
        shaped["abandoned"] = True

    return {key: value for key, value in shaped.items() if value is not None}


@mcp.tool(
    description="Recent commands and results of a session, rebuilt from its journal.",
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_history(session_id: str, limit: int = 20) -> Any:
    feed = await _call("GET", f"/api/sessions/{session_id}/history")
    if feed.get("error"):

        return feed

    meta = feed.get("session") or {}
    entries = [_history_entry(entry) for entry in (feed.get("entries") or [])[-limit:]]
    session = {
        key: meta.get(key)
        for key in (
            "session_id", "container", "database", "owner", "state",
            "allow_commit", "commands", "committed", "unmasked",
        )
    }
    # A closed session still hands back its transcript, so this is the only
    # place the refusal would otherwise be lost: `_call` only surfaces
    # `session_gone` out of an error status, and this one arrives with a 200.
    if meta.get("gone"):
        session["gone"] = meta["gone"]

    return {
        "session": session,
        "entries": entries,
        "journal": f"/api/journals/{session_id}",
    }


@mcp.tool(
    description=(
        "The transcript of a session, live or long finished — every command, "
        "result and line Odoo logged, in order. fmt='markdown' (default) or "
        "'jsonl' for the raw records. A long one comes back a page at a time: "
        "the end by default, any other stretch with first= and last= (1-based "
        "line numbers, inclusive), with total_lines to say how much there is."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def os_journal(
    session_id: str,
    fmt: str = "markdown",
    first: int | None = None,
    last: int | None = None,
) -> Any:
    exported = await _call("GET", f"/api/journals/{session_id}", params={"fmt": fmt})
    if "text" not in exported:

        return exported

    return _journal_page(session_id, fmt, exported["text"], first, last)


def _journal_page(
    session_id: str, fmt: str, text: str, first: int | None, last: int | None
) -> dict:
    """One stretch of a transcript, within `MAX_JOURNAL` characters.

    The whole of it when it fits. Otherwise the end, which is where a run
    says how it went — or the range asked for, cut short where the budget
    runs out. A single line longer than the budget (a record carrying a
    megabyte of stdout) is clipped rather than refused.
    """
    lines = text.splitlines()
    total = len(lines)
    if first is None and last is None:
        if len(text) <= MAX_JOURNAL:
            start, stop = 0, total
        else:
            start, stop, size = total, total, 0
            while start > 0 and size + len(lines[start - 1]) + 1 <= MAX_JOURNAL:
                start -= 1
                size += len(lines[start]) + 1
            start = min(start, max(total - 1, 0))
    else:
        start = max((first or 1) - 1, 0)
        wanted = min(last or total, total)
        stop, size = start, 0
        while stop < wanted and size + len(lines[stop]) + 1 <= MAX_JOURNAL:
            size += len(lines[stop]) + 1
            stop += 1
        stop = max(stop, min(start + 1, wanted))
    page = "\n".join(lines[start:stop])
    clipped = len(page) > MAX_JOURNAL
    if clipped:
        page = page[:MAX_JOURNAL] if first is not None or last is not None else page[-MAX_JOURNAL:]
    answer = {
        "session_id": session_id,
        "fmt": fmt,
        "total_lines": total,
        "first": start + 1 if stop > start else None,
        "last": stop if stop > start else None,
        "text": page,
        "truncated": clipped or start > 0 or stop < total,
    }
    if answer["truncated"]:
        answer["recovery"] = (
            f"{total} lines in all; pass first= and last= (1-based, inclusive) "
            "to read another stretch"
        )

    return answer


def main() -> None:
    logging.basicConfig(level=logging.INFO)  # stderr, never stdout
    mcp.run()


if __name__ == "__main__":
    main()
