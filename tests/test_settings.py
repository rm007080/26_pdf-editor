import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.session import Session
from app.settings import default_settings_path
from tests.test_session import auth, post_file, write_pdf


def test_default_path_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_settings_path() == tmp_path / "pdf-editor" / "settings.json"


@pytest.fixture
def env(tmp_path):
    session = Session(temp_dir=tmp_path / "pdf-editor")
    path = tmp_path / "settings" / "settings.json"
    app = create_app(session, settings_path=path)
    with TestClient(app) as client:
        yield client, app, session, path


def test_missing_file_returns_defaults(env):
    client, app, session, path = env
    before = client.get("/api/session", headers=auth(app)).json()

    response = client.get("/api/settings", headers=auth(app))

    assert response.status_code == 200
    assert response.json() == {
        "thumbnail_layout": "row",
        "thumbnail_size": "medium",
        "pane_ratio": None,
    }
    assert not path.exists()
    assert client.get("/api/session", headers=auth(app)).json() == before
    assert session.snapshot()["selected_page_ids"] == []


def test_broken_file_returns_defaults_and_app_starts(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{", encoding="utf-8")
    session = Session(temp_dir=tmp_path / "pdf-editor")
    app = create_app(session, settings_path=path)
    with TestClient(app) as client:
        response = client.get("/api/settings", headers=auth(app))
    assert response.status_code == 200
    assert response.json()["thumbnail_layout"] == "row"
    assert response.json()["thumbnail_size"] == "medium"
    assert response.json()["pane_ratio"] is None


def test_missing_keys_default_individually(env):
    _client, app, _session, path = env
    client = _client
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"thumbnail_layout": "grid"}), encoding="utf-8")

    response = client.get("/api/settings", headers=auth(app))

    assert response.status_code == 200
    assert response.json() == {
        "thumbnail_layout": "grid",
        "thumbnail_size": "medium",
        "pane_ratio": None,
    }


def test_put_settings_writes_file_and_leaves_session(env, tmp_path):
    client, app, _session, path = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    assert post_file(client, app, source).status_code == 200
    before = client.get("/api/session", headers=auth(app)).json()
    body = {"thumbnail_layout": "grid", "thumbnail_size": "large", "pane_ratio": 0.25}

    response = client.put("/api/settings", headers=auth(app), json=body)

    assert response.status_code == 200
    assert response.json() == body
    assert json.loads(path.read_text(encoding="utf-8")) == body
    assert client.get("/api/settings", headers=auth(app)).json() == body
    assert client.get("/api/session", headers=auth(app)).json() == before


def test_put_rejects_invalid_settings_without_changing_file(env):
    client, app, _session, path = env
    valid = {"thumbnail_layout": "row", "thumbnail_size": "small", "pane_ratio": 0.4}
    assert client.put("/api/settings", headers=auth(app), json=valid).status_code == 200
    written = path.read_bytes()
    invalid = [
        {"thumbnail_size": "small", "pane_ratio": 0.4},
        {"thumbnail_layout": "column", "thumbnail_size": "small", "pane_ratio": 0.4},
        {"thumbnail_layout": "row", "thumbnail_size": "huge", "pane_ratio": 0.4},
        {"thumbnail_layout": "row", "thumbnail_size": "small", "pane_ratio": 0},
        {"thumbnail_layout": "row", "thumbnail_size": "small", "pane_ratio": 1},
        {"thumbnail_layout": "row", "thumbnail_size": "small", "pane_ratio": -0.2},
        {"thumbnail_layout": "row", "thumbnail_size": "small", "pane_ratio": True},
    ]
    for body in invalid:
        response = client.put("/api/settings", headers=auth(app), json=body)
        assert response.status_code == 400
        assert path.read_bytes() == written


def test_settings_without_token_are_rejected(env):
    client, _app, _session, path = env
    assert client.get("/api/settings").status_code == 401
    assert client.put(
        "/api/settings",
        json={"thumbnail_layout": "grid", "thumbnail_size": "small", "pane_ratio": None},
    ).status_code == 401
    assert not path.exists()


def test_settings_work_while_exporting(env, tmp_path):
    client, app, _session, path = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    assert post_file(client, app, source).status_code == 200
    before = client.get("/api/session", headers=auth(app)).json()
    destination = tmp_path / "out"
    destination.mkdir()
    started = threading.Event()
    release = threading.Event()
    app.state.exporter.ask_save_path = lambda _directory, _name: destination / "out.pdf"

    def pause() -> None:
        started.set()
        release.wait(5)

    app.state.exporter.pause_during_copy = pause
    export = client.post("/api/exports/all", headers=auth(app))
    assert export.status_code == 200
    assert started.wait(5)
    body = {"thumbnail_layout": "grid", "thumbnail_size": "small", "pane_ratio": None}
    put = client.put("/api/settings", headers=auth(app), json=body)
    fetched = client.get("/api/settings", headers=auth(app))
    during = client.get("/api/session", headers=auth(app)).json()
    release.set()

    assert put.status_code == 200
    assert put.json() == body
    assert fetched.json() == body
    assert json.loads(path.read_text(encoding="utf-8")) == body
    assert during == before
