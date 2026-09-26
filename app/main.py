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


def render_index(token: str) -> str:
    if DIST_INDEX.is_file():
        html = DIST_INDEX.read_text(encoding="utf-8")
    else:
        html = FALLBACK_INDEX
    return html.replace(TOKEN_PLACEHOLDER, token)


def create_app(session: Session | None = None) -> FastAPI:
    session = session or Session()
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

    @app.get("/api/session")
    def get_session() -> dict:
        return session.snapshot()

    @app.post("/api/documents")
    async def post_document(file: UploadFile = File(...)) -> JSONResponse:
        data = await file.read()
        filename = file.filename or ""
        status, body = await asyncio.to_thread(session.accept_upload, data, filename)
        return JSONResponse(status_code=status, content=body)

    @app.get("/api/session/file")
    def get_session_file():
        path = session.session_file_path()
        if path is None:
            return JSONResponse(status_code=404, content={"detail": "読み込まれた PDF はありません"})
        return FileResponse(path, media_type="application/pdf")

    @app.post("/api/exports/all")
    async def export_all() -> JSONResponse:
        status, body = await exporter.begin_export()
        return JSONResponse(status_code=status, content=body)

    @app.get("/api/exports/{job_id}")
    def export_status(job_id: str):
        body = exporter.job_view(job_id)
        if body is None:
            return JSONResponse(status_code=404, content={"detail": "書き出しが見つかりません"})
        return body

    @app.post("/api/exports/{job_id}/open-file")
    def export_open_file(job_id: str) -> JSONResponse:
        status, body = exporter.open_saved_file(job_id)
        return JSONResponse(status_code=status, content=body)

    @app.post("/api/exports/{job_id}/open-folder")
    def export_open_folder(job_id: str) -> JSONResponse:
        status, body = exporter.open_saved_folder(job_id)
        return JSONResponse(status_code=status, content=body)

    assets = DIST_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    return app
