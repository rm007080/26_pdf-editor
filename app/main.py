import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.auth import bearer_matches, new_token
from app.export import ExportService
from app.session import Session
from app.settings import SettingsStore

DIST_DIR = Path(__file__).resolve().parent.parent / "web" / "dist"
DIST_INDEX = DIST_DIR / "index.html"
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


def _development_enabled() -> bool:
    return os.environ.get("PDF_EDITOR_ENV", "development") == "development"


async def read_json_body(request: Request, invalid_detail: str) -> tuple[object, JSONResponse | None]:
    try:
        return await request.json(), None
    except Exception:
        return None, JSONResponse(status_code=400, content={"detail": invalid_detail})


def render_index(token: str) -> str:
    if DIST_INDEX.is_file():
        html = DIST_INDEX.read_text(encoding="utf-8")
    else:
        html = FALLBACK_INDEX
    return html.replace(TOKEN_PLACEHOLDER, token)


def create_app(session: Session | None = None, settings_path: Path | None = None) -> FastAPI:
    session = session or Session()
    settings = SettingsStore(settings_path)
    development = _development_enabled()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        session.startup()
        yield

    app = FastAPI(
        lifespan=lifespan,
        docs_url="/docs" if development else None,
        redoc_url=None,
        openapi_url="/openapi.json" if development else None,
    )
    exporter = ExportService(session)
    app.state.session = session
    app.state.exporter = exporter
    app.state.token = new_token()

    @app.middleware("http")
    async def require_bearer(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            header = request.headers.get("authorization", "")
            if not bearer_matches(header, app.state.token):
                return JSONResponse(status_code=401, content={"detail": "認証が必要です"})
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return render_index(app.state.token)

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

    @app.get("/api/session")
    def get_session() -> dict:
        return session.snapshot()

    @app.patch("/api/session/selection")
    async def patch_selection(request: Request) -> JSONResponse:
        body, error = await read_json_body(request, "選択の指定が不正です")
        if error is not None:
            return error
        status, payload = session.update_selection(body)
        return JSONResponse(status_code=status, content=payload)

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

    assets = DIST_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    return app
