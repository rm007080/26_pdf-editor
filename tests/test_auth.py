from fastapi.testclient import TestClient

from app.__main__ import HOST
from app.main import create_app
from app.session import Session


def test_api_without_token_is_rejected(tmp_path):
    app = create_app(Session(temp_dir=tmp_path / "pdf-editor"))
    with TestClient(app) as client:
        response = client.get("/api/session")
    assert response.status_code == 401


def test_index_puts_token_in_meta(tmp_path):
    app = create_app(Session(temp_dir=tmp_path / "pdf-editor"))
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert f'<meta name="pdf-editor-token" content="{app.state.token}">' in response.text


def test_production_has_no_docs(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_EDITOR_ENV", "production")
    app = create_app(Session(temp_dir=tmp_path / "pdf-editor"))
    with TestClient(app) as client:
        response = client.get("/docs")
    assert response.status_code == 404


def test_development_enables_docs(tmp_path, monkeypatch):
    monkeypatch.delenv("PDF_EDITOR_ENV", raising=False)
    app = create_app(Session(temp_dir=tmp_path / "pdf-editor"))
    with TestClient(app) as client:
        response = client.get("/docs")
    assert response.status_code == 200


def test_bind_host_is_loopback():
    assert HOST == "127.0.0.1"
