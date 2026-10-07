"""HTTP for commands, WebSocket for what arrives on its own."""

import asyncio
import contextlib
import json
import os
import re
import socket
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from odoo_sheller import discovery, journal
from odoo_sheller.guard import LoopbackOnly
from odoo_sheller.names import check_ssh_name
from odoo_sheller.paths import web_dir
from odoo_sheller.recipe import RecipeError, check_database, describe
from odoo_sheller.registry import EVENT_BACKLOG, Registry, load_admin_key
from odoo_sheller.session import (
    CommitForbidden,
    CommitNotAllowed,
    SessionBusy,
    SessionDead,
    SessionNotReady,
    SessionState,
)
from odoo_sheller.targets import DEFAULT_STAGE, TargetsError

WEB = web_dir()
NO_STORE = {"Cache-Control": "no-store"}


class NoCacheStaticFiles(StaticFiles):
    """The UI is a local daemon; a cached stylesheet lies about the current theme."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = FileResponse(
            full_path, status_code=status_code, stat_result=stat_result, headers=NO_STORE
        )

        return response


def _export_headers(path: Path, suffix: str) -> dict:
    """Name the download after the journal, so meta survives outside the body."""

    return {"content-disposition": f'attachment; filename="{path.stem}.{suffix}"'}


# `module` alone runs that module's standard tests, the way `--test-tags
# /module` does; a class or a method narrows it.
TEST_SPEC_RE = re.compile(r"^(\w+)(?:\.(\w+)(?:\.(\w+))?)?$")


class ExecBody(BaseModel):
    code: str
    # The caller's word that this code writes nothing. It is journalled with
    # the command and keeps it out of `pending_commands` — for reads such as
    # os_source, which would otherwise read as work at risk in a handover.
    read_only: bool = False


class RunTestBody(BaseModel):
    test: str
    # A zero or negative ceiling would send the frame and abandon it in the
    # same breath: the run really starts, and the session stays busy for its
    # whole length with nobody waiting. An hour is past any sane test class.
    timeout: float | None = Field(default=None, gt=0, le=3600)


class Owner(BaseModel):
    """Who a session belongs to.

    `label` is the name that reaches the owner badge, the journal transcript
    and os_history. It used to be whatever a caller put in a free-form dict,
    including nothing at all: a program driving this daemon over plain HTTP
    handed a session over as `{"kind": "agent"}`, the daemon stored exactly
    that, and the session then read `watching · undefined` in the UI, `by
    agent (None)` in its own transcript, and raised KeyError in os_history.

    The kind is a usable name on its own, so an absent label becomes it — a
    caller that has nothing better to say is not made to invent something.
    The kind itself is closed: it decides whether commit is gated, and a typo
    must not pass as one more kind nobody has heard of.
    """

    kind: Literal["human", "agent"]
    label: str | None = None

    @model_validator(mode="after")
    def _named(self):
        if not self.label:
            self.label = self.kind

        return self


class OpenBody(BaseModel):
    container: str | None = None
    database: str | None = None
    odoo_bin: str | None = None
    owner: Owner | None = None
    allow_commit: bool | None = None
    replace: str | None = None
    client_token: str | None = None
    # A session opened to run one test and then close itself. Never for a
    # human: the browser's session is theirs until they end it.
    autoclose: bool = False
    # Where this runs. A local container needs container/database/odoo_bin.
    # A remote instance is a card a human wrote down (`/api/targets`), opened
    # by its id: the card says where it is, and the instance says what it is.
    target_id: str | None = None


class ProbeBody(BaseModel):
    container: str


class OdooshCardBody(BaseModel):
    """An odoo.sh build to write down, or to probe without writing it down."""

    kind: Literal["odoosh"]
    build: str
    host: str

    @field_validator("build", "host")
    @classmethod
    def _a_name_for_ssh(cls, value: str, info) -> str:
        """Both end up in an ssh argument; see `names.check_ssh_name`."""

        return check_ssh_name(info.field_name, value)


class SshCardBody(BaseModel):
    """A server someone wrote down: how to arrive, and what to run there.

    Not checked here — the grammar in `recipe.py` is, and says which field."""

    kind: Literal["ssh"]
    name: str
    access: str
    launch: str
    database: str | None = None
    # Nothing on a plain server says what it is, so the default is the one
    # that refuses a commit outright, and a human says otherwise.
    stage: str = DEFAULT_STAGE


class SshProbeBody(BaseModel):
    kind: Literal["ssh"]
    access: str
    launch: str
    database: str | None = None


class RecipeBody(BaseModel):
    access: str
    launch: str
    database: str | None = None


CardBody = Annotated[OdooshCardBody | SshCardBody, Field(discriminator="kind")]
ProbeTargetBody = Annotated[OdooshCardBody | SshProbeBody, Field(discriminator="kind")]


class CardChangeBody(BaseModel):
    """What may change. Only what is sent changes — `null` clears a database."""

    host: str | None = None
    # Accepted only so that asking for it can be refused in words: a card's
    # build is its identity.
    build: str | None = None
    name: str | None = None
    access: str | None = None
    launch: str | None = None
    database: str | None = None
    stage: str | None = None


class OwnerBody(BaseModel):
    owner: Owner


class PolicyBody(BaseModel):
    allow_commit: bool


IN_CONTAINER_ENV = "ODOO_SHELLER_IN_CONTAINER"


def mcp_launch() -> dict | None:
    """How to start an MCP server that talks to *this* daemon.

    Over HTTP it makes no difference whether a daemon is native or in a
    container — a caller finds one on the port and stops caring. The MCP
    server is the one thing that does differ, because it is not reached over
    the network at all: a client spawns the process and speaks to its stdin.
    A containerized daemon's process has to be spawned *inside that
    container*, and only this daemon knows which one it is in.

    Containerized, the container's own id is its hostname, which works as
    `docker exec`'s target whatever name the container was started under.
    Frozen, the MCP server is a sibling tree beside the daemon's. Otherwise it
    is this interpreter with `-m`.

    `None` means we cannot say — a frozen daemon whose sibling is missing.
    Better than a command that would fail in the caller's hands.
    """
    if os.environ.get(IN_CONTAINER_ENV):

        return {
            "command": "docker",
            "args": ["exec", "-i", socket.gethostname(), "python", "-m", "odoo_sheller.mcp"],
        }

    if getattr(sys, "frozen", False):
        sibling = Path(sys.executable).resolve().parent.parent / "odoo-sheller-mcp"
        binary = sibling / "odoo-sheller-mcp"
        if binary.is_file():

            return {"command": str(binary), "args": []}

        return None

    return {"command": sys.executable, "args": ["-m", "odoo_sheller.mcp"]}


def daemon_version() -> str:
    """Our own version, or `"unknown"` when nothing declares it.

    A frozen build carries no dist-info unless the packaging step asks for it,
    and `version()` raises there rather than returning a blank. `/health` is
    the desktop app's liveness probe, so it must answer even then: the app
    needs `ok`, and treats the version as a courtesy.
    """
    try:

        return version("odoo-sheller")
    except PackageNotFoundError:

        return "unknown"


async def _forward(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Send what arrives until the client leaves or the registry hangs up.

    `None` is the hang-up: the session closed, or this socket fell too far
    behind to be worth catching up — a reconnect resyncs from the API.
    """
    try:
        while True:
            event = await queue.get()
            if event is None:
                await websocket.close()

                return
            await websocket.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass


def _markdown_export(path: Path, session_id: str) -> str:
    records = journal.Journal(path).records()

    return journal.to_markdown(records, journal.session_meta(records, session_id))


def _jsonl_export(path: Path, session_id: str) -> str:
    # The file on disk is untouched; the export gets one extra first line so
    # the stream says what session it belongs to.
    meta = journal.session_meta(journal.Journal(path).iter_records(), session_id)

    return (
        json.dumps({"kind": "export_meta", **meta}, ensure_ascii=False) + "\n"
        + path.read_text(encoding="utf-8", errors="replace")
    )


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """Close the live sessions on the way down.

    Sessions cannot outlive the daemon — their container-side processes are
    our children — but dying without saying so leaves every journal ending
    mid-transcript, with no `session_close` and no cause. The desktop app
    stops the daemon on quit, which makes this the normal way a session ends,
    not an edge case. The timeout is short on purpose: this runs inside
    uvicorn's own graceful-shutdown budget.
    """
    yield
    for session_id in list(app.state.registry.sessions):
        with contextlib.suppress(Exception):
            await app.state.registry.close(session_id, timeout=1.5)


def create_app(registry: Registry | None = None) -> FastAPI:
    app = FastAPI(title="odoo-sheller", docs_url=None, lifespan=lifespan)
    app.add_middleware(LoopbackOnly)
    app.state.registry = (
        registry if registry is not None else Registry(admin_key=load_admin_key())
    )
    # Read once, at start. An editable install answers `version()` from the
    # metadata on disk, which a bump rewrites under a daemon still running the
    # old code — and then /health names a version that is not the one running.
    running_version = daemon_version()

    def gone(session_id: str, reason: str, records: list[dict] | None = None) -> dict:
        """What a caller needs to carry on after losing a session.

        The namespace died with the process, so recovery is never automatic —
        but everything needed to open a replacement is right here. Pass
        `records` when the journal is already open: rescanning the whole
        journal directory to answer the same question costs a second pass.
        """
        target = (
            journal.target_from_records(records)
            if records is not None
            else app.state.registry.target_of_past_session(session_id)
        )

        return {
            "error": "session_gone",
            "session_id": session_id,
            "reason": reason,
            "target": target,
            "journal": f"/api/journals/{session_id}" if target else None,
            "recovery": "open a new session on the same target; variables are lost",
        }

    def session_or_404(session_id: str):
        try:

            return app.state.registry.get(session_id)
        except KeyError:
            raise HTTPException(
                status_code=404, detail=gone(session_id, "not registered")
            ) from None

    def is_admin(admin_key: str | None) -> bool:

        return bool(admin_key) and admin_key == app.state.registry.admin_key

    def require_owner(session, session_key: str | None):
        """Only the owner types into a session. Watching needs no key."""
        if not session_key or session_key != session.write_key:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "not_owner",
                    "owner": session.describe()["owner"],
                    "recovery": "ask the owner to hand the session over, or open your own",
                },
            )

    def require_owner_or_admin(session, session_key: str | None, admin_key: str | None):
        if is_admin(admin_key):

            return
        require_owner(session, session_key)

    def require_admin(admin_key: str | None):
        if not is_admin(admin_key):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "admin_only",
                    "recovery": (
                        "this needs the admin key, which the daemon keeps in "
                        "~/.odoo-sheller/admin.key"
                    ),
                },
            )

    def require_human_or_admin(session, session_key, admin_key, act: str):
        """Granting commit, or giving a session to a human, is a human's act.

        A key `held_by` accepts is not enough for either: the agent a session
        was handed to holds one too. With it, an agent refused a commit could
        grant itself the right, or hand the session to "a human" and keep the
        key that came back — and commit as one.
        """
        if is_admin(admin_key) or session.held_by_human(session_key):

            return
        raise HTTPException(
            status_code=403,
            detail={
                "error": "needs_a_human",
                "act": act,
                "recovery": (
                    "a human does this, from the UI or with the admin key; an "
                    "agent says what it wants and why, and waits for it"
                ),
            },
        )

    def translate(exc: Exception, session_id: str | None = None) -> HTTPException:
        if isinstance(exc, (SessionBusy, SessionNotReady)):

            return HTTPException(status_code=409, detail=str(exc))
        if isinstance(exc, CommitForbidden):
            # Checked before CommitNotAllowed, which it subclasses. The codes
            # have to differ: `commit_not_allowed` means "ask the human", and
            # an agent told to poll for a grant that will never come would
            # poll forever.

            return HTTPException(
                status_code=423,
                detail={
                    "error": "commit_forbidden",
                    "message": str(exc),
                    "recovery": (
                        "nothing grants this — end with rollback, or open a "
                        "session on a staging build if you need to write"
                    ),
                },
            )
        if isinstance(exc, CommitNotAllowed):

            return HTTPException(
                status_code=423,
                detail={
                    "error": "commit_not_allowed",
                    "message": str(exc),
                    "recovery": "ask the human to grant commit for this session",
                },
            )
        if isinstance(exc, SessionDead):
            if session_id:

                return HTTPException(status_code=410, detail=gone(session_id, str(exc)))

            return HTTPException(status_code=410, detail=str(exc))
        if isinstance(exc, TimeoutError):

            return HTTPException(status_code=504, detail=str(exc))

        return HTTPException(status_code=500, detail=str(exc))

    @app.get("/health")
    def health():
        """Liveness, and the one thing a caller cannot work out for itself.

        No session data and no admin key. `mcp` names a command to spawn an
        agent-side server for this daemon, which differs between a native and
        a containerized one; `container` says which this is. On a native
        daemon the command holds a local path, which is a filename on a
        loopback-only endpoint — the same posture as everything else here.
        """

        return {
            "ok": True,
            "version": running_version,
            "container": bool(os.environ.get(IN_CONTAINER_ENV)),
            "mcp": mcp_launch(),
        }

    @app.get("/api/containers")
    async def containers():
        try:

            return await discovery.list_containers()
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @app.post("/api/probe")
    async def probe(body: ProbeBody):

        return await discovery.probe(body.container)

    def cards():
        """The store, or a 500 that says which file is wrong and leaves it be."""

        return app.state.registry.targets

    def present(card: dict) -> dict:
        """A card as the API shows it. An ssh card also says, in a few words,
        where it goes and who it runs as — read from its recipe, so a card can be
        recognised before anything has been probed. A recipe that has stopped
        parsing (a key file that has gone) says so there and stays listed: the
        card is how its owner finds out, and edits it."""
        if card["kind"] != "ssh":

            return card
        try:
            access = cards().parse_access(card["access"])
            launch = cards().parse_launch(card["launch"], card["database"])
        except RecipeError as exc:

            return {**card, "summary": {
                "destination": None, "port": None, "runs_as": None,
                "database": None, "error": str(exc),
            }}
        runs_as = (access.become.user or "root") if access.become else access.user

        return {**card, "summary": {
            "destination": access.destination,
            "port": access.port,
            "runs_as": runs_as,
            "database": launch.database,
            "error": None,
        }}

    def unusable(exc: TargetsError) -> HTTPException:

        return HTTPException(
            status_code=500,
            detail={
                "error": "targets_file_unusable",
                "message": str(exc),
                "recovery": "fix the file by hand, or remove it and write the cards again",
            },
        )

    @app.get("/api/targets")
    async def list_targets():
        """Every card, odoo.sh then ssh, each newest first.

        No key to read: opening a session on a card needs none either, so the
        text of a card is not what stands between a local caller and the
        server. An ssh card carries a `summary` of where its recipe goes.
        """
        try:

            return [present(card) for card in cards().list()]
        except TargetsError as exc:
            raise unusable(exc) from None

    def recipe_refused(exc: RecipeError) -> HTTPException:
        """A recipe the grammar refuses, with the field it is about."""

        return HTTPException(
            status_code=422,
            detail={"error": "invalid_recipe", "field": exc.field, "message": str(exc)},
        )

    @app.post("/api/targets")
    async def add_target(body: CardBody, x_os_admin_key: str | None = Header(None)):
        """Write a card down, or update the odoo.sh one already there.

        A card says where a remote instance is, and that is an instruction to
        this machine to open an ssh connection there: only a human writes one,
        and the admin key is how the daemon tells.
        """
        require_admin(x_os_admin_key)
        try:
            if body.kind == "odoosh":

                return cards().add_odoosh(body.build, body.host)

            return present(
                cards().add_ssh(body.name, body.access, body.launch, body.database, body.stage)
            )
        except RecipeError as exc:
            raise recipe_refused(exc) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except TargetsError as exc:
            raise unusable(exc) from None

    @app.put("/api/targets/{target_id}")
    async def change_target(
        target_id: str, body: CardChangeBody, x_os_admin_key: str | None = Header(None)
    ):
        require_admin(x_os_admin_key)
        try:

            return present(cards().update(target_id, body.model_dump(exclude_unset=True)))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no target {target_id!r}") from None
        except RecipeError as exc:
            raise recipe_refused(exc) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except TargetsError as exc:
            raise unusable(exc) from None

    @app.delete("/api/targets/{target_id}")
    async def delete_target(target_id: str, x_os_admin_key: str | None = Header(None)):
        require_admin(x_os_admin_key)
        try:
            cards().delete(target_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no target {target_id!r}") from None
        except TargetsError as exc:
            raise unusable(exc) from None

        return {"deleted": target_id}

    @app.post("/api/targets/parse")
    async def parse_target(body: RecipeBody, x_os_admin_key: str | None = Header(None)):
        """What a recipe means, decoded for the person about to save it.

        Pure: it reaches no network, and nothing is stored. Every field that is
        wrong is reported at once — a form being typed is not an error — and
        it is the admin's act only because it asks this machine whether a named
        key file exists.
        """
        require_admin(x_os_admin_key)
        errors: dict[str, str] = {}
        access = launch = None
        try:
            access = cards().parse_access(body.access)
        except RecipeError as exc:
            errors[exc.field or "access"] = str(exc)
        database = (body.database or "").strip() or None
        if database:
            try:
                check_database(database)
            except RecipeError as exc:
                errors["database"] = str(exc)
                # Reported on its own, so that a launch which is also wrong
                # still gets its message.
                database = None
        try:
            launch = cards().parse_launch(body.launch, database)
        except RecipeError as exc:
            errors[exc.field or "launch"] = str(exc)
        if errors:

            return {"ok": False, "errors": errors}

        return {"ok": True, "breakdown": describe(access, launch)}

    @app.post("/api/targets/probe")
    async def probe_target(body: ProbeTargetBody, x_os_admin_key: str | None = Header(None)):
        """What an instance says it is, before anything is written or opened.

        There is no listing for odoo.sh — a build is entered, not discovered —
        and a plain server is whatever its card says, so this is the whole of
        target discovery for both. For odoo.sh `stage` is the field to read; for
        a server it is declared on the card, and what is probed is who the
        recipe lands as, where, and which Odoo. Nothing is stored. It is the
        admin's act because it makes this machine reach out to a host somebody
        named.
        """
        require_admin(x_os_admin_key)
        if body.kind == "odoosh":

            return await discovery.probe_odoosh(body.build, body.host)
        try:
            access = cards().parse_access(body.access)
            launch = cards().parse_launch(body.launch, body.database)
        except RecipeError as exc:
            raise recipe_refused(exc) from None
        probe = await discovery.probe_ssh(access, launch)

        return {**probe, "breakdown": describe(access, launch)}

    @app.get("/api/containers/{container}/tests")
    async def container_tests(container: str, module: str | None = Query(None)):
        result = await discovery.list_tests(container, module or "")
        if result.get("error_code") == "invalid_module_name":
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_module_name",
                    "module": module,
                    "recovery": result.get("recovery")
                    or (
                        "pass an addon technical name (letters, digits, underscore), "
                        "not a test spec"
                    ),
                },
            )
        if result.get("error_code") == "module_not_found":
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "module_not_found",
                    "module": result.get("module") or module,
                    "recovery": (
                        "check the addon technical name; this catalogue is "
                        "files on disk, not installed modules"
                    ),
                },
            )
        if not result.get("ok"):
            raise HTTPException(
                status_code=502,
                detail=result.get("error") or "list tests failed",
            )

        return {
            "module": result["module"],
            "path": result["path"],
            "classes": result["classes"],
        }

    @app.post("/api/sessions")
    async def open_session(body: OpenBody, x_os_admin_key: str | None = Header(None)):
        if (
            body.allow_commit
            and body.owner is not None
            and body.owner.kind == "agent"
            and not is_admin(x_os_admin_key)
        ):
            # The grant ritual, skipped at the door: an agent's session that
            # opens with the right already in it was never granted anything.
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "needs_a_human",
                    "act": "open an agent's session with commit already granted",
                    "recovery": (
                        "open it with allow_commit false; a human grants the "
                        "right later, from the UI"
                    ),
                },
            )
        try:
            session = await app.state.registry.open(
                container=body.container,
                database=body.database,
                odoo_bin=body.odoo_bin,
                owner=body.owner.model_dump() if body.owner else None,
                allow_commit=body.allow_commit,
                replace=body.replace,
                client_token=body.client_token,
                autoclose=body.autoclose,
                target_id=body.target_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except RecipeError as exc:
            raise recipe_refused(exc) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except SessionDead as exc:
            raise HTTPException(
                status_code=410,
                detail={
                    "error": "session_did_not_start",
                    "message": str(exc),
                    "recovery": (
                        "the message ends with what Odoo logged as it exited — "
                        "a database that does not exist, a broken config, a "
                        "module that fails to import; fix that and open again"
                    ),
                },
            ) from None
        except Exception as exc:  # noqa: BLE001 - API boundary maps failures to HTTP.
            raise translate(exc) from None

        # The only response that carries the key. It is never listed again.
        return {**session.describe(), "write_key": session.write_key}

    @app.get("/api/sessions")
    async def list_sessions():

        return [session.describe() for session in app.state.registry.sessions.values()]

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, x_os_session_key: str | None = Header(None)):
        session = session_or_404(session_id)
        described = session.describe()
        if x_os_session_key:
            # What the key presented is worth here — `owner`, `former_owner`
            # or `invalid` — and nothing about anyone else's. An agent handed
            # a key checks it once, rather than learning from its first
            # command that it was given the wrong one.
            described["key_status"] = session.key_status(x_os_session_key)

        return described

    @app.get("/api/sessions/{session_id}/history")
    async def session_history(
        session_id: str,
        logs: bool = Query(
            False,
            description=(
                "Include journalled Odoo stderr as a sibling `logs` array. Off by "
                "default: the command feed does not need them, and debug-level "
                "sessions journal stderr without limit."
            ),
        ),
        log_tail: int = Query(
            journal.FEED_LOG_TAIL,
            ge=0,
            le=20000,
            description=(
                "Only applies when `logs` is true: keep the last N stderr lines "
                f"(default {journal.FEED_LOG_TAIL}). 0 returns the whole journal. "
                "Ignored when `logs` is false — there is then no `logs` field at all."
            ),
        ),
    ):
        """Rebuild the command feed from the session journal.

        Live session or a closed one that still has a journal file. A closed one
        answers `200` with the transcript, `session.state: "gone"` and a
        `session.gone` object carrying the target and how to recover. Reading a
        dead session's history is useful; being told it is dead only by the next
        `exec` failing is not, so the signal rides in the body rather than in the
        status code — and under `session`, where everything about the session is.

        `logs` and `log_tail` are a pair. `logs=false` (the default) omits stderr
        entirely so a caller who only wants exec/commit history does not pay for
        it. `logs=true` adds `logs: [{ts, line}, …]` in journal order, then
        `log_tail` slices that array from the end — a long session, or one run at
        debug level, can be hundreds of thousands of lines; the UI wants the
        tail. `log_tail=0` disables the cap. The response also sets
        `logs_truncated` when the cap dropped lines.
        """
        try:
            session = app.state.registry.get(session_id)
        except KeyError:
            session = None
        if session is not None:
            source = session.journal
        else:
            source = app.state.registry.journal_file_for(session_id)
            if source is None:
                raise HTTPException(
                    status_code=404, detail=gone(session_id, "not registered")
                )

        def rebuild():
            records = source.records()
            feed = journal.feed_from_records(records, include_logs=logs, log_tail=log_tail)

            return records, feed, journal.session_meta(records, session_id)

        # Off the event loop: a long session's journal runs to a hundred
        # megabytes, and parsing it here stopped every other session's
        # frames for as long as that took.
        records, feed, meta = await asyncio.to_thread(rebuild)
        if session is not None:
            meta.update({
                key: value
                for key, value in session.describe().items()
                if key != "id" and value is not None
            })
        else:
            meta["state"] = "gone"
            meta["gone"] = gone(session_id, "not registered", records)
        feed["session"] = meta

        return feed

    @app.post("/api/sessions/{session_id}/exec")
    async def exec_code(
        session_id: str,
        body: ExecBody,
        x_os_session_key: str | None = Header(None),
    ):
        session = session_or_404(session_id)
        require_owner(session, x_os_session_key)
        kwargs = {"read_only": True} if body.read_only else {}
        try:

            return await session.execute(body.code, **kwargs)
        except Exception as exc:  # noqa: BLE001 - API boundary maps failures to HTTP.
            raise translate(exc, session_id) from None

    @app.post("/api/sessions/{session_id}/run_test")
    async def run_test(
        session_id: str,
        body: RunTestBody,
        x_os_session_key: str | None = Header(None),
    ):
        session = session_or_404(session_id)
        require_owner(session, x_os_session_key)
        match = TEST_SPEC_RE.match(body.test)
        if not match:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_test_spec",
                    "test": body.test,
                    "recovery": (
                        "use 'module', 'module.TestClass' or "
                        "'module.TestClass.test_method'"
                    ),
                },
            )
        module, test_class, test_method = match.groups()
        kwargs = {"timeout": body.timeout} if body.timeout is not None else {}
        try:

            return await session.run_test(module, test_class, test_method, **kwargs)
        except Exception as exc:  # noqa: BLE001 - API boundary maps failures to HTTP.
            raise translate(exc, session_id) from None

    @app.post("/api/sessions/{session_id}/commit")
    async def commit(session_id: str, x_os_session_key: str | None = Header(None)):
        session = session_or_404(session_id)
        require_owner(session, x_os_session_key)
        try:

            return await session.commit()
        except Exception as exc:  # noqa: BLE001 - API boundary maps failures to HTTP.
            raise translate(exc, session_id) from None

    @app.post("/api/sessions/{session_id}/rollback")
    async def rollback(session_id: str, x_os_session_key: str | None = Header(None)):
        session = session_or_404(session_id)
        require_owner(session, x_os_session_key)
        try:

            return await session.rollback()
        except Exception as exc:  # noqa: BLE001 - API boundary maps failures to HTTP.
            raise translate(exc, session_id) from None

    @app.post("/api/sessions/{session_id}/interrupt")
    async def interrupt(
        session_id: str,
        x_os_session_key: str | None = Header(None),
        x_os_admin_key: str | None = Header(None),
    ):
        session = session_or_404(session_id)
        # Stopping work creates nothing and saves nothing, so the admin may do
        # it too — it is strictly milder than the kill they already have.
        require_owner_or_admin(session, x_os_session_key, x_os_admin_key)
        try:
            await session.interrupt()
        except Exception as exc:  # noqa: BLE001 - API boundary maps failures to HTTP.
            raise translate(exc) from None

        return {"ok": True}

    @app.delete("/api/sessions/{session_id}")
    async def close_session(
        session_id: str,
        force: bool = False,
        x_os_session_key: str | None = Header(None),
        x_os_admin_key: str | None = Header(None),
    ):
        session = session_or_404(session_id)
        # A dead session is a corpse: its process is gone, so reaping it takes
        # nothing from anyone. Demanding a key here only strands the tab.
        # A live one closes for its owner, for whoever owned it before handing
        # it over, or for the admin.
        if (
            session.state is not SessionState.DEAD
            and not session.held_by(x_os_session_key)
        ):
            require_owner_or_admin(session, x_os_session_key, x_os_admin_key)
        await app.state.registry.close(session_id, force=force)

        return {"ok": True}

    @app.post("/api/sessions/{session_id}/owner")
    async def set_owner(
        session_id: str,
        body: OwnerBody,
        x_os_session_key: str | None = Header(None),
        x_os_admin_key: str | None = Header(None),
    ):
        session = session_or_404(session_id)
        # Owning it, or having owned it, is authority enough: handing a session
        # over is giving up the right to type, not the session. Taking one from
        # someone you never gave it to is the admin act. Giving it to a human
        # is a human's: the key that comes back types as one.
        if not session.held_by(x_os_session_key):
            require_admin(x_os_admin_key)
        elif body.owner.kind == "human":
            require_human_or_admin(
                session, x_os_session_key, x_os_admin_key, "hand the session to a human"
            )
        pending = session.pending_commands
        write_key = session.transfer_owner(body.owner.model_dump())

        # The new key is returned once, here. The previous one is already dead.
        return {
            "owner": session.describe()["owner"],
            "allow_commit": session.allow_commit,
            "write_key": write_key,
            "pending_commands": pending,
        }

    @app.post("/api/sessions/{session_id}/policy")
    async def set_policy(
        session_id: str,
        body: PolicyBody,
        x_os_session_key: str | None = Header(None),
        x_os_admin_key: str | None = Header(None),
    ):
        session = session_or_404(session_id)
        # Deciding whether a session may write is a decision about your own
        # session: whoever opened it, or handed it over, can make it. Only a
        # session you never owned needs the admin key — and a grant needs a
        # human, so the agent it would free cannot make it for itself.
        if not session.held_by(x_os_session_key):
            require_admin(x_os_admin_key)
        elif body.allow_commit:
            require_human_or_admin(
                session, x_os_session_key, x_os_admin_key, "grant commit"
            )
        # Locally the right only gates an agent: a human owner confirms each
        # commit in the UI and `Session._may_commit` lets them through
        # regardless, so storing a revocation would journal `policy_changed`
        # and answer `allow_commit: false` while the next commit went through
        # anyway. On a remote target the flag gates the human too, so there a
        # revocation is a real act and refusing it would be the lie instead.
        described = session.describe()
        local_human = (
            session.owner.get("kind") == "human"
            and described.get("kind", "docker") == "docker"
        )
        if not body.allow_commit and local_human:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "policy_not_applicable",
                    "session_id": session_id,
                    "owner": dict(session.owner),
                    "recovery": (
                        "commit rights only gate an agent; a human owner confirms "
                        "each commit instead"
                    ),
                },
            )
        try:
            session.set_allow_commit(body.allow_commit)
        except CommitForbidden as exc:
            raise translate(exc, session_id) from None

        return {"allow_commit": session.allow_commit}

    @app.get("/api/sessions/{session_id}/logs")
    async def logs(session_id: str, tail: int = 200):

        return {"lines": session_or_404(session_id).stderr_tail(tail)}

    @app.get("/api/journals")
    async def journals():
        # Off the event loop. The first listing after a start reads every file
        # — seconds, on a machine that has kept a few thousand — and those were
        # seconds in which no live session heard from its process.

        return await asyncio.to_thread(
            journal.list_journals, app.state.registry.journal_root
        )

    @app.get("/api/journals/{session_id}")
    async def journal_export(session_id: str, fmt: str = "jsonl"):
        path = journal.find_journal(app.state.registry.journal_root, session_id)
        if path is None:
            raise HTTPException(status_code=404, detail=f"no journal for {session_id}")
        if fmt == "markdown":

            return PlainTextResponse(
                await asyncio.to_thread(_markdown_export, path, session_id),
                media_type="text/markdown",
                headers=_export_headers(path, "md"),
            )

        return PlainTextResponse(
            await asyncio.to_thread(_jsonl_export, path, session_id),
            media_type="application/x-ndjson",
            headers=_export_headers(path, "jsonl"),
        )

    @app.delete("/api/journals/{session_id}")
    async def journal_delete(
        session_id: str,
        x_os_admin_key: str | None = Header(None),
    ):
        """Unlink one journal file. Irreversible, so it takes the admin key.

        The only destructive file operation in the API. A live session keeps its
        journal: close it first.
        """
        require_admin(x_os_admin_key)
        if session_id in app.state.registry.sessions:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "session_live",
                    "session_id": session_id,
                    "recovery": "close the session first",
                },
            )
        try:
            journal.delete_journal(app.state.registry.journal_root, session_id)
        except FileNotFoundError:
            raise HTTPException(
                status_code=404, detail=f"no journal for {session_id}"
            ) from None

        return {"deleted": session_id}

    @app.websocket("/ws/sessions")
    async def registry_events(websocket: WebSocket):
        """Sessions coming and going, so a watcher never needs to reload."""
        await websocket.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=EVENT_BACKLOG)
        app.state.registry.watch(queue)
        try:
            await _forward(websocket, queue)
        finally:
            app.state.registry.unwatch(queue)

    @app.websocket("/ws/sessions/{session_id}")
    async def events(websocket: WebSocket, session_id: str):
        await websocket.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=EVENT_BACKLOG)
        try:
            app.state.registry.subscribe(session_id, queue)
        except KeyError:
            await websocket.send_json({"error": f"no session {session_id}"})
            await websocket.close(code=1008)

            return
        try:
            await _forward(websocket, queue)
        finally:
            app.state.registry.unsubscribe(session_id, queue)

    @app.get("/")
    async def root():

        return RedirectResponse("/web", status_code=307)

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui():
        """Swagger UI over `/openapi.json`.

        The one page here that is not self-contained: the swagger-ui bundle
        comes from jsdelivr, so `/docs` needs network even though the daemon
        does not. FastAPI's default also pulled its favicon from
        fastapi.tiangolo.com; this serves our own instead.
        """

        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title="odoo-sheller API",
            swagger_favicon_url="/static/logo.svg",
        )

    @app.get("/web")
    async def index():

        return FileResponse(WEB / "index.html", headers=NO_STORE)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():

        return FileResponse(WEB / "logo.svg", media_type="image/svg+xml", headers=NO_STORE)

    if (WEB / "vendor").exists():
        app.mount("/vendor", StaticFiles(directory=WEB / "vendor"), name="vendor")
    app.mount("/static", NoCacheStaticFiles(directory=WEB), name="static")

    return app
