"""HTTP for commands, WebSocket for what arrives on its own."""

import asyncio
import json
import re
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from odoo_sheller import discovery, journal
from odoo_sheller.registry import Registry, load_admin_key
from odoo_sheller.session import (
    CommitNotAllowed,
    SessionBusy,
    SessionDead,
    SessionNotReady,
    SessionState,
)

WEB = Path(__file__).with_name("web")
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


TEST_SPEC_RE = re.compile(r"^(\w+)\.(\w+)(?:\.(\w+))?$")


class ExecBody(BaseModel):
    code: str


class RunTestBody(BaseModel):
    test: str
    # A zero or negative ceiling would send the frame and abandon it in the
    # same breath: the run really starts, and the session stays busy for its
    # whole length with nobody waiting. An hour is past any sane test class.
    timeout: float | None = Field(default=None, gt=0, le=3600)


class OpenBody(BaseModel):
    container: str | None = None
    database: str | None = None
    odoo_bin: str | None = None
    owner: dict | None = None
    allow_commit: bool | None = None
    replace: str | None = None
    client_token: str | None = None
    # A session opened to run one test and then close itself. Never for a
    # human: the browser's session is theirs until they end it.
    autoclose: bool = False


class ProbeBody(BaseModel):
    container: str


class OwnerBody(BaseModel):
    owner: dict


class PolicyBody(BaseModel):
    allow_commit: bool


def create_app(registry: Registry | None = None) -> FastAPI:
    app = FastAPI(title="odoo-sheller", docs_url=None)
    app.state.registry = (
        registry if registry is not None else Registry(admin_key=load_admin_key())
    )

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
                    "recovery": "this needs the admin key the daemon printed at startup",
                },
            )

    def translate(exc: Exception, session_id: str | None = None) -> HTTPException:
        if isinstance(exc, (SessionBusy, SessionNotReady)):

            return HTTPException(status_code=409, detail=str(exc))
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

    @app.get("/api/containers")
    async def containers():
        try:

            return await discovery.list_containers()
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None

    @app.post("/api/probe")
    async def probe(body: ProbeBody):

        return await discovery.probe(body.container)

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
    async def open_session(body: OpenBody):
        try:
            session = await app.state.registry.open(
                container=body.container,
                database=body.database,
                odoo_bin=body.odoo_bin,
                owner=body.owner,
                allow_commit=body.allow_commit,
                replace=body.replace,
                client_token=body.client_token,
                autoclose=body.autoclose,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - API boundary maps failures to HTTP.
            raise translate(exc) from None

        # The only response that carries the key. It is never listed again.
        return {**session.describe(), "write_key": session.write_key}

    @app.get("/api/sessions")
    async def list_sessions():

        return [session.describe() for session in app.state.registry.sessions.values()]

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):

        return session_or_404(session_id).describe()

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
            records = session.journal.records()
        else:
            past = app.state.registry.journal_file_for(session_id)
            if past is None:
                raise HTTPException(
                    status_code=404, detail=gone(session_id, "not registered")
                )
            records = past.records()
        feed = journal.feed_from_records(records, include_logs=logs, log_tail=log_tail)
        meta = journal.session_meta(records, session_id)
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
        try:

            return await session.execute(body.code)
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
                    "recovery": "use 'module.TestClass' or 'module.TestClass.test_method'",
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
        # someone you never gave it to is the admin act.
        if not session.held_by(x_os_session_key):
            require_admin(x_os_admin_key)
        pending = session.pending_commands
        write_key = session.transfer_owner(body.owner)

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
        # session you never owned needs the admin key.
        if not session.held_by(x_os_session_key):
            require_admin(x_os_admin_key)
        # The right only gates an agent: a human owner confirms each commit in
        # the UI and `Session._may_commit` lets them through regardless. Storing
        # a revocation here would journal `policy_changed` and answer
        # `allow_commit: false` while the next commit went through anyway.
        if not body.allow_commit and session.owner.get("kind") == "human":
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
        session.set_allow_commit(body.allow_commit)

        return {"allow_commit": session.allow_commit}

    @app.get("/api/sessions/{session_id}/logs")
    async def logs(session_id: str, tail: int = 200):

        return {"lines": session_or_404(session_id).stderr_tail(tail)}

    @app.get("/api/journals")
    async def journals():

        return journal.list_journals(app.state.registry.journal_root)

    @app.get("/api/journals/{session_id}")
    async def journal_export(session_id: str, fmt: str = "jsonl"):
        for entry in journal.list_journals(app.state.registry.journal_root):
            if entry["session_id"] != session_id:
                continue
            path = Path(entry["path"])
            records = journal.Journal(path).records()
            meta = journal.session_meta(records, session_id)
            if fmt == "markdown":

                return PlainTextResponse(
                    journal.to_markdown(records, meta),
                    media_type="text/markdown",
                    headers=_export_headers(path, "md"),
                )
            # The file on disk is untouched; the export gets one extra first
            # line so the stream says what session it belongs to.
            body = json.dumps({"kind": "export_meta", **meta}, ensure_ascii=False) + "\n"
            body += path.read_text(encoding="utf-8")

            return PlainTextResponse(
                body,
                media_type="application/x-ndjson",
                headers=_export_headers(path, "jsonl"),
            )
        raise HTTPException(status_code=404, detail=f"no journal for {session_id}")

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
        queue: asyncio.Queue = asyncio.Queue()
        app.state.registry.watch(queue)
        try:
            while True:
                await websocket.send_json(await queue.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            app.state.registry.unwatch(queue)

    @app.websocket("/ws/sessions/{session_id}")
    async def events(websocket: WebSocket, session_id: str):
        await websocket.accept()
        queue: asyncio.Queue = asyncio.Queue()
        try:
            app.state.registry.subscribe(session_id, queue)
        except KeyError:
            await websocket.send_json({"error": f"no session {session_id}"})
            await websocket.close(code=1008)

            return
        try:
            while True:
                await websocket.send_json(await queue.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
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
