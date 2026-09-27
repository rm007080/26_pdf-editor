import asyncio
import json
import os
import sys
import time
import webbrowser
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.auth import bearer_matches, new_token
from app.export import ExportService
from app.log_store import LogStore
from app.open_pdfs import open_selected_pdfs
from app.presence import RESETTING_SOURCES, SECOND_TAB_DETAIL, EditorLease, ExitRequest, IdleWatch
from app.save_dialog import ask_open_pdfs
from app.session import Session
from app.settings import SettingsStore
from app.tray import loss_confirm_message
from app.version import read_app_version

EDITOR_URL = "http://127.0.0.1:8765/"
TOKEN_PLACEHOLDER = "__PDF_EDITOR_TOKEN__"
FALLBACK_INDEX = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="pdf-editor-token" content="__PDF_EDITOR_TOKEN__">
<title>PDF 編集</title>
</head>
<body>
<div id="root"></div>
</body>
</html>
"""


def dist_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "web" / "dist"
    return Path(__file__).resolve().parent.parent / "web" / "dist"


def open_editor_page() -> None:
    webbrowser.open(EDITOR_URL)


def _development_enabled() -> bool:
    return os.environ.get("PDF_EDITOR_ENV", "development") == "development"


async def read_json_body(request: Request, invalid_detail: str) -> tuple[object, JSONResponse | None]:
    try:
        return await request.json(), None
    except Exception:
        return None, JSONResponse(status_code=400, content={"detail": invalid_detail})


def render_index(token: str) -> str:
    index = dist_dir() / "index.html"
    if index.is_file():
        html = index.read_text(encoding="utf-8")
    else:
        html = FALLBACK_INDEX
    return html.replace(TOKEN_PLACEHOLDER, token)


LEASE_FREE = {
    "/api/editor/claim",
    "/api/editor/release",
    "/api/editor/heartbeat",
    "/api/editor/open",
}


def create_app(
    session: Session | None = None,
    settings_path: Path | None = None,
    log_dir: Path | None = None,
    clock: Callable[[], float] | None = None,
    stop: Callable[[], None] | None = None,
    present_exit: Callable[[str], None] | None = None,
) -> FastAPI:
    session = session or Session()
    settings = SettingsStore(settings_path)
    logs = LogStore(log_dir)
    development = _development_enabled()
    clock_fn = clock or time.monotonic

    def exit_message() -> str | None:
        snapshot = session.snapshot()
        return loss_confirm_message(bool(snapshot["unsaved"]), bool(snapshot["split_unexported"]))

    clean_stop: list[Callable[[], None]] = []

    def on_clean() -> None:
        if clean_stop:
            clean_stop[0]()

    exit_request = ExitRequest(
        clock_fn,
        present_exit or (lambda _text: None),
        exit_message,
        on_clean,
    )
    idle = IdleWatch(clock_fn, settings, session, stop or (lambda: None), on_tick=exit_request.poll)
    clean_stop.append(idle.shutdown_now)
    session.activity_listener = idle.on_activity
    lease = EditorLease(clock_fn)
    threaded_idle = clock is None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        session.startup()
        idle.start(threaded=threaded_idle)
        yield
        idle.close()

    app = FastAPI(
        lifespan=lifespan,
        docs_url="/docs" if development else None,
        redoc_url=None,
        openapi_url="/openapi.json" if development else None,
    )
    exporter = ExportService(session, settings)
    app.state.session = session
    app.state.exporter = exporter
    app.state.settings = settings
    app.state.logs = logs
    app.state.ask_open_pdfs = ask_open_pdfs
    app.state.version = read_app_version()
    app.state.token = new_token()
    app.state.lease = lease
    app.state.idle = idle
    app.state.exit_request = exit_request
    app.state.editor_tab_id = None

    @app.middleware("http")
    async def require_bearer(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            header = request.headers.get("authorization", "")
            if not bearer_matches(header, app.state.token):
                return JSONResponse(status_code=401, content={"detail": "認証が必要です"})
            if request.url.path not in LEASE_FREE and not lease.holds(request.headers.get("x-tab-id")):
                return JSONResponse(status_code=409, content={"detail": SECOND_TAB_DETAIL})
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    @app.get("/settings", response_class=HTMLResponse)
    @app.get("/help", response_class=HTMLResponse)
    def index() -> str:
        return render_index(app.state.token)

    @app.get("/api/about")
    def get_about() -> dict:
        return {
            "name": "PDF編集",
            "version": app.state.version,
            "cleared_leftover": session.cleared_leftover,
        }

    async def read_tab_id(request: Request) -> tuple[str | None, JSONResponse | None]:
        header = request.headers.get("x-tab-id")
        body, error = await read_json_body(request, "タブの指定が不正です")
        if error is not None:
            return None, error
        tab_id = body.get("tab_id") if isinstance(body, dict) else None
        if not isinstance(tab_id, str) or tab_id == "" or not header or header != tab_id:
            return None, JSONResponse(status_code=400, content={"detail": "タブの指定が不正です"})
        return tab_id, None

    @app.post("/api/editor/claim")
    async def post_editor_claim(request: Request) -> JSONResponse:
        tab_id, error = await read_tab_id(request)
        if error is not None or tab_id is None:
            return error
        status, payload = lease.claim(tab_id)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/api/editor/release")
    async def post_editor_release(request: Request) -> JSONResponse:
        tab_id, error = await read_tab_id(request)
        if error is not None or tab_id is None:
            return error
        was_holder = lease.holds(tab_id)
        status, payload = lease.release(tab_id)
        if was_holder:
            exit_request.on_holder_released()
        return JSONResponse(status_code=status, content=payload)

    @app.post("/api/editor/heartbeat")
    async def post_editor_heartbeat(request: Request) -> JSONResponse:
        tab_id, error = await read_tab_id(request)
        if error is not None or tab_id is None:
            return error
        holding = lease.heartbeat(tab_id)
        exit_request.note_heartbeat(holding)
        return JSONResponse(
            status_code=200,
            content={
                "idle_warning": holding and idle.warning(),
                "exit_requested": bool(holding and exit_request.requested),
                "holding": holding,
            },
        )

    @app.post("/api/editor/open")
    def post_editor_open() -> dict:
        open_editor_page()
        return {"opened": True}

    @app.post("/api/activity")
    async def post_activity(request: Request) -> JSONResponse:
        body, error = await read_json_body(request, "操作の指定が不正です")
        if error is not None:
            return error
        source = body.get("source") if isinstance(body, dict) else None
        if source == "mousemove":
            return JSONResponse(status_code=200, content={"reset": False})
        if source not in RESETTING_SOURCES:
            return JSONResponse(status_code=400, content={"detail": "操作の指定が不正です"})
        idle.reset()
        return JSONResponse(status_code=200, content={"reset": True})

    @app.post("/api/shutdown")
    async def post_shutdown(request: Request) -> JSONResponse:
        raw = await request.body()
        confirmed = False
        if raw.strip():
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                return JSONResponse(status_code=400, content={"detail": "終了の指定が不正です"})
            if not isinstance(body, dict):
                return JSONResponse(status_code=400, content={"detail": "終了の指定が不正です"})
            confirmed = body.get("confirmed") is True
        snapshot = session.snapshot()
        dirty = bool(snapshot["unsaved"] or snapshot["split_unexported"])
        if dirty and not confirmed:
            return JSONResponse(status_code=200, content={"stopped": False})
        idle.shutdown_now()
        return JSONResponse(status_code=200, content={"stopped": True})

    @app.post("/api/logs/open")
    def post_logs_open() -> dict:
        logs.open()
        return {"status": "opened"}

    @app.post("/api/dialogs/open-pdfs")
    async def post_open_pdfs() -> JSONResponse:
        status, body = await open_selected_pdfs(session, settings, app.state.ask_open_pdfs)
        return JSONResponse(status_code=status, content=body)

    @app.get("/api/settings")
    def get_settings() -> dict:
        return settings.read()

    @app.put("/api/settings")
    async def put_settings(request: Request) -> JSONResponse:
        body, error = await read_json_body(request, "設定の指定が不正です")
        if error is not None:
            return error
        status, payload = settings.write(body)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/api/settings/reset")
    async def post_settings_reset(request: Request) -> JSONResponse:
        raw = await request.body()
        if raw.strip() not in {b"", b"{}"}:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                return JSONResponse(status_code=400, content={"detail": "設定の指定が不正です"})
            if body != {}:
                return JSONResponse(status_code=400, content={"detail": "設定の指定が不正です"})
        return JSONResponse(status_code=200, content=settings.reset())

    @app.get("/api/session")
    def get_session() -> dict:
        body = session.snapshot()
        if idle.warning():
            body["idle_warning"] = True
        if exit_request.requested:
            body["exit_requested"] = True
        return body

    @app.patch("/api/session/selection")
    async def patch_selection(request: Request) -> JSONResponse:
        body, error = await read_json_body(request, "選択の指定が不正です")
        if error is not None:
            return error
        status, payload = session.update_selection(body)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/api/session/exit-cancel")
    def post_exit_cancel() -> dict:
        exit_request.cancel()
        return {"exit_requested": False}

    @app.post("/api/session/reset")
    async def post_session_reset() -> JSONResponse:
        status, payload = await asyncio.to_thread(session.reset)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/api/session/commands")
    async def post_commands(request: Request) -> JSONResponse:
        body, error = await read_json_body(request, "コマンドの指定が不正です")
        if error is not None:
            return error
        status, payload = session.apply_command(body)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/api/documents")
    async def post_document(file: UploadFile = File(...)) -> JSONResponse:
        data = await file.read()
        filename = file.filename or ""
        status, body = await asyncio.to_thread(session.accept_upload, data, filename)
        return JSONResponse(status_code=status, content=body)

    @app.post("/api/documents/{document_id}/duplicate")
    async def post_document_duplicate(document_id: str, request: Request) -> JSONResponse:
        body, error = await read_json_body(request, "重複の指定が不正です")
        if error is not None:
            return error
        status, payload = session.duplicate_against(document_id, body)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/api/documents/{document_id}/password")
    async def post_document_password(document_id: str, request: Request) -> JSONResponse:
        body, error = await read_json_body(request, "パスワードの指定が不正です")
        if error is not None:
            return error
        status, payload = await asyncio.to_thread(session.submit_password, document_id, body)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/api/documents/discard")
    async def post_discard(request: Request) -> JSONResponse:
        body, error = await read_json_body(request, "破棄する PDF の指定が不正です")
        if error is not None:
            return error
        status, payload = session.discard(body)
        return JSONResponse(status_code=status, content=payload)

    @app.get("/api/session/files/{document_id}")
    def get_document_file(document_id: str):
        path = session.workspace_copy_path(document_id)
        if path is None:
            return JSONResponse(status_code=404, content={"detail": "読み込まれた PDF はありません"})
        return FileResponse(path, media_type="application/pdf")

    @app.post("/api/exports/all")
    async def export_all() -> JSONResponse:
        status, body = await exporter.begin_export()
        return JSONResponse(status_code=status, content=body)

    @app.post("/api/exports/selection")
    async def export_selection() -> JSONResponse:
        status, body = await exporter.begin_selection()
        return JSONResponse(status_code=status, content=body)

    @app.post("/api/exports/split")
    async def export_split() -> JSONResponse:
        status, body = await exporter.begin_split()
        return JSONResponse(status_code=status, content=body)

    @app.post("/api/exports/split/confirm")
    async def export_split_confirm(request: Request) -> JSONResponse:
        body, error = await read_json_body(request, "ファイル名の指定が不正です")
        if error is not None:
            return error
        status, payload = await exporter.confirm_split(body)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/api/exports/split/cancel")
    def export_split_cancel() -> JSONResponse:
        status, payload = exporter.cancel_split()
        return JSONResponse(status_code=status, content=payload)

    @app.get("/api/exports/{job_id}")
    def export_status(job_id: str):
        body = exporter.job_view(job_id)
        if body is None:
            return JSONResponse(status_code=404, content={"detail": "書き出しが見つかりません"})
        return body

    @app.post("/api/exports/{job_id}/open-file")
    async def export_open_file(job_id: str, request: Request) -> JSONResponse:
        content_type = request.headers.get("content-type", "")
        body = None
        if content_type.startswith("application/json"):
            body, error = await read_json_body(request, "開き方の指定が不正です")
            if error is not None:
                return error
        status, payload = exporter.open_saved_file(job_id, body)
        return JSONResponse(status_code=status, content=payload)

    @app.post("/api/exports/{job_id}/open-folder")
    def export_open_folder(job_id: str) -> JSONResponse:
        status, body = exporter.open_saved_folder(job_id)
        return JSONResponse(status_code=status, content=body)

    assets = dist_dir() / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    return app
