import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.folders import FOLDER_NOTICE
from app.log_store import LIMIT_BYTES, prune_logs
from app.main import create_app
from app.save_dialog import documents_folder
from app.session import Session
from app.settings import default_settings_path
from app.version import read_app_version
from tests.test_session import (
    auth,
    claim_tab,
    load_pdf,
    post_command,
    post_file,
    post_password,
    write_encrypted_pdf,
    write_pdf,
)

DEFAULT_SETTINGS = {
    "thumbnail_layout": "row",
    "thumbnail_size": "medium",
    "pane_ratio": None,
    "theme": None,
    "windows_notifications": True,
    "idle_minutes": 120,
    "last_open_dir": None,
    "last_save_dir": None,
}


def view_body(**overrides: object) -> dict[str, object]:
    body = {
        "thumbnail_layout": "row",
        "thumbnail_size": "medium",
        "pane_ratio": None,
        "theme": None,
        "windows_notifications": True,
        "idle_minutes": 120,
    }
    body.update(overrides)
    return body


def test_default_path_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_settings_path() == tmp_path / "pdf-editor" / "settings.json"


@pytest.fixture
def env(tmp_path):
    session = Session(temp_dir=tmp_path / "pdf-editor")
    path = tmp_path / "settings" / "settings.json"
    app = create_app(session, settings_path=path)
    with TestClient(app) as client:
        claim_tab(client, app)
        yield client, app, session, path


def test_missing_file_returns_defaults(env):
    client, app, session, path = env
    before = client.get("/api/session", headers=auth(app)).json()

    response = client.get("/api/settings", headers=auth(app))

    assert response.status_code == 200
    assert response.json() == DEFAULT_SETTINGS
    assert not path.exists()
    assert client.get("/api/session", headers=auth(app)).json() == before
    assert session.snapshot()["selected_page_ids"] == []


def test_broken_file_returns_defaults_and_app_starts(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{", encoding="utf-8")
    session = Session(temp_dir=tmp_path / "pdf-editor")
    app = create_app(session, settings_path=path)
    with TestClient(app) as client:
        claim_tab(client, app)
        response = client.get("/api/settings", headers=auth(app))
    assert response.status_code == 200
    assert response.json() == DEFAULT_SETTINGS


def test_missing_keys_default_individually(env):
    _client, app, _session, path = env
    client = _client
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"thumbnail_layout": "grid"}), encoding="utf-8")

    response = client.get("/api/settings", headers=auth(app))

    assert response.status_code == 200
    assert response.json() == {**DEFAULT_SETTINGS, "thumbnail_layout": "grid"}


def test_put_settings_writes_file_and_leaves_session(env, tmp_path):
    client, app, _session, path = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    load_pdf(client, app, source)
    before = client.get("/api/session", headers=auth(app)).json()
    body = view_body(thumbnail_layout="grid", thumbnail_size="large", pane_ratio=0.25)
    saved = {**body, "last_open_dir": None, "last_save_dir": None}

    response = client.put("/api/settings", headers=auth(app), json=body)

    assert response.status_code == 200
    assert response.json() == saved
    assert json.loads(path.read_text(encoding="utf-8")) == saved
    assert client.get("/api/settings", headers=auth(app)).json() == saved
    assert client.get("/api/session", headers=auth(app)).json() == before


def test_put_rejects_invalid_settings_without_changing_file(env):
    client, app, _session, path = env
    valid = view_body(thumbnail_size="small", pane_ratio=0.4)
    assert client.put("/api/settings", headers=auth(app), json=valid).status_code == 200
    written = path.read_bytes()
    invalid = [
        view_body(thumbnail_size="small", pane_ratio=0.4),
        view_body(thumbnail_layout="column", thumbnail_size="small", pane_ratio=0.4),
        view_body(thumbnail_size="huge", pane_ratio=0.4),
        view_body(thumbnail_size="small", pane_ratio=0),
        view_body(thumbnail_size="small", pane_ratio=1),
        view_body(thumbnail_size="small", pane_ratio=-0.2),
        view_body(thumbnail_size="small", pane_ratio=True),
        view_body(theme="sepia"),
        view_body(idle_minutes=15),
        view_body(idle_minutes=True),
        {**view_body(), "extra": 1},
    ]
    invalid[0].pop("thumbnail_layout")
    for body in invalid:
        response = client.put("/api/settings", headers=auth(app), json=body)
        assert response.status_code == 400
        assert path.read_bytes() == written


def test_settings_without_token_are_rejected(env):
    client, _app, _session, path = env
    assert client.get("/api/settings").status_code == 401
    assert client.put("/api/settings", json=view_body(thumbnail_layout="grid", thumbnail_size="small")).status_code == 401
    assert client.post("/api/settings/reset").status_code == 401
    assert not path.exists()


def test_settings_work_while_exporting(env, tmp_path):
    client, app, _session, path = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    load_pdf(client, app, source)
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
    body = view_body(thumbnail_layout="grid", thumbnail_size="small")
    saved = {**body, "last_open_dir": None, "last_save_dir": str(destination)}
    put = client.put("/api/settings", headers=auth(app), json=body)
    fetched = client.get("/api/settings", headers=auth(app))
    during = client.get("/api/session", headers=auth(app)).json()
    release.set()

    assert put.status_code == 200
    assert put.json() == saved
    assert fetched.json() == saved
    assert json.loads(path.read_text(encoding="utf-8")) == saved
    assert during == before


def test_legacy_three_keys_keep_their_values_and_new_keys_default(env):
    client, app, _session, path = env
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"thumbnail_layout": "grid", "thumbnail_size": "small", "pane_ratio": 0.3}),
        encoding="utf-8",
    )

    response = client.get("/api/settings", headers=auth(app))

    assert response.status_code == 200
    assert response.json() == {
        **DEFAULT_SETTINGS,
        "thumbnail_layout": "grid",
        "thumbnail_size": "small",
        "pane_ratio": 0.3,
    }


def test_invalid_theme_and_idle_minutes_are_rejected_and_file_remains(env):
    client, app, _session, path = env
    stored = {
        **DEFAULT_SETTINGS,
        "theme": "dark",
        "idle_minutes": 60,
        "last_open_dir": "C:/kept",
        "last_save_dir": "C:/saved",
    }
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(stored), encoding="utf-8")
    before = path.read_bytes()

    theme = client.put("/api/settings", headers=auth(app), json=view_body(theme="blue"))
    idle = client.put("/api/settings", headers=auth(app), json=view_body(idle_minutes=90))

    assert theme.status_code == 400
    assert idle.status_code == 400
    assert path.read_bytes() == before


def test_put_keeps_directories_when_folder_keys_are_omitted_or_sent(env):
    client, app, _session, path = env
    stored = {**DEFAULT_SETTINGS, "last_open_dir": "C:/open", "last_save_dir": "C:/save"}
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(stored), encoding="utf-8")

    omitted = client.put("/api/settings", headers=auth(app), json=view_body(theme="light"))
    included = client.put(
        "/api/settings",
        headers=auth(app),
        json={**view_body(theme="dark"), "last_open_dir": "C:/other", "last_save_dir": None},
    )

    assert omitted.status_code == 200
    assert omitted.json()["last_open_dir"] == "C:/open"
    assert omitted.json()["last_save_dir"] == "C:/save"
    assert included.status_code == 200
    assert included.json()["theme"] == "dark"
    assert included.json()["last_open_dir"] == "C:/open"
    assert included.json()["last_save_dir"] == "C:/save"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["last_open_dir"] == "C:/open"
    assert on_disk["last_save_dir"] == "C:/save"


def test_reset_restores_defaults_without_changing_session(env, tmp_path):
    client, app, _session, path = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    load_pdf(client, app, source)
    before = client.get("/api/session", headers=auth(app)).json()
    client.put(
        "/api/settings",
        headers=auth(app),
        json=view_body(theme="dark", windows_notifications=False, idle_minutes=30),
    )
    path.write_text(
        json.dumps(
            {
                **json.loads(path.read_text(encoding="utf-8")),
                "last_open_dir": "C:/open",
                "last_save_dir": "C:/save",
            }
        ),
        encoding="utf-8",
    )

    response = client.post("/api/settings/reset", headers=auth(app), json={})

    assert response.status_code == 200
    assert response.json() == DEFAULT_SETTINGS
    assert json.loads(path.read_text(encoding="utf-8")) == DEFAULT_SETTINGS
    assert response.json()["theme"] is None
    assert response.json()["windows_notifications"] is True
    assert response.json()["idle_minutes"] == 120
    assert response.json()["last_open_dir"] is None
    assert response.json()["last_save_dir"] is None
    assert client.get("/api/session", headers=auth(app)).json() == before


def test_open_pdfs_returns_basename_in_selection_order(env, tmp_path):
    client, app, session, path = env
    before = client.get("/api/session", headers=auth(app)).json()
    first = tmp_path / "beta.pdf"
    second = tmp_path / "alpha.pdf"
    write_pdf(first, pages=1)
    write_pdf(second, pages=2)
    uploads = {"count": 0}
    original = session.accept_upload

    def wrapped(data, filename):
        uploads["count"] += 1
        return original(data, filename)

    session.accept_upload = wrapped
    app.state.ask_open_pdfs = lambda _initial: [first, second]

    response = client.post("/api/dialogs/open-pdfs", headers=auth(app))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert [item["filename"] for item in body["items"]] == ["beta.pdf", "alpha.pdf"]
    assert body["items"][0]["page_count"] == 1
    assert body["items"][1]["page_count"] == 2
    assert "document_id" in body["items"][0]
    assert str(first) not in response.text
    assert uploads["count"] == 0
    assert client.get("/api/session", headers=auth(app)).json() == before
    assert json.loads(path.read_text(encoding="utf-8"))["last_open_dir"] == str(first.parent)
    extra = tmp_path / "extra.pdf"
    write_pdf(extra, pages=1)
    assert post_file(client, app, extra).status_code == 200


def test_open_pdfs_cancel_leaves_edit_state_and_folder(env, tmp_path):
    client, app, _session, path = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    load_pdf(client, app, source)
    before = client.get("/api/session", headers=auth(app)).json()
    app.state.ask_open_pdfs = lambda _initial: None

    response = client.post("/api/dialogs/open-pdfs", headers=auth(app))

    assert response.status_code == 200
    assert response.json() == {"status": "cancelled"}
    assert client.get("/api/session", headers=auth(app)).json() == before
    assert not path.exists() or json.loads(path.read_text(encoding="utf-8")).get("last_open_dir") is None


def test_open_pdfs_holds_dialog_and_rejects_upload(env, tmp_path):
    client, app, _session, _path = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    started = threading.Event()
    release = threading.Event()

    def ask(_initial):
        started.set()
        assert release.wait(5)
        return None

    app.state.ask_open_pdfs = ask
    holder: dict[str, object] = {}

    def choose() -> None:
        holder["response"] = client.post("/api/dialogs/open-pdfs", headers=auth(app))

    worker = threading.Thread(target=choose)
    worker.start()
    assert started.wait(5)
    conflict = post_file(client, app, source)
    release.set()
    worker.join(5)

    assert conflict.status_code == 409
    assert holder["response"].status_code == 200
    assert holder["response"].json()["status"] == "cancelled"
    assert post_file(client, app, source).status_code == 200


def test_open_pdfs_exception_discards_received_files_and_keeps_folder(env, tmp_path):
    client, app, session, path = env
    before = client.get("/api/session", headers=auth(app)).json()
    first = tmp_path / "kept.pdf"
    write_pdf(first, pages=1)
    missing = tmp_path / "gone.pdf"
    app.state.ask_open_pdfs = lambda _initial: [first, missing]

    response = client.post("/api/dialogs/open-pdfs", headers=auth(app))

    assert response.status_code == 500
    assert response.json()["detail"] == "ファイル選択を完了できませんでした"
    assert client.get("/api/session", headers=auth(app)).json() == before
    assert list(session.temp_dir.iterdir()) == []
    assert not path.exists() or json.loads(path.read_text(encoding="utf-8")).get("last_open_dir") is None
    assert post_file(client, app, first).status_code == 200


def test_open_pdfs_keeps_going_after_one_unreadable_pdf(env, tmp_path):
    client, app, _session, path = env
    good = tmp_path / "good.pdf"
    bad = tmp_path / "bad.pdf"
    later = tmp_path / "later.pdf"
    write_pdf(good, pages=1)
    bad.write_bytes(b"not a pdf")
    write_pdf(later, pages=1)
    app.state.ask_open_pdfs = lambda _initial: [good, bad, later]

    response = client.post("/api/dialogs/open-pdfs", headers=auth(app))

    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["filename"] == "good.pdf"
    assert "document_id" in items[0]
    assert items[1] == {"filename": "bad.pdf", "detail": "PDF を開けませんでした"}
    assert "document_id" not in items[1]
    assert items[2]["filename"] == "later.pdf"
    assert json.loads(path.read_text(encoding="utf-8"))["last_open_dir"] == str(good.parent)


def test_unusable_open_folder_uses_documents_and_notifies(env, tmp_path):
    client, app, _session, path = env
    missing = tmp_path / "missing-open"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"last_open_dir": str(missing)}), encoding="utf-8")
    seen: dict[str, Path] = {}

    def ask(initial: Path):
        seen["initial"] = initial
        return None

    app.state.ask_open_pdfs = ask
    response = client.post("/api/dialogs/open-pdfs", headers=auth(app))

    assert response.status_code == 200
    assert seen["initial"] == documents_folder()
    assert response.json()["initial_folder_notice"] == FOLDER_NOTICE
    assert response.json()["status"] == "cancelled"


def test_duplicate_recheck_uses_approved_ids_only(env, tmp_path):
    client, app, _session, _path = env
    source = tmp_path / "same.pdf"
    write_pdf(source, pages=1)
    other = tmp_path / "copy.pdf"
    other.write_bytes(source.read_bytes())
    app.state.ask_open_pdfs = lambda _initial: [source, other]
    opened = client.post("/api/dialogs/open-pdfs", headers=auth(app)).json()["items"]
    first_id = opened[0]["document_id"]
    second_id = opened[1]["document_id"]
    before = client.get("/api/session", headers=auth(app)).json()

    approved = client.post(
        f"/api/documents/{second_id}/duplicate",
        headers=auth(app),
        json={"approved_document_ids": [first_id]},
    )
    staged_only = client.post(
        f"/api/documents/{second_id}/duplicate",
        headers=auth(app),
        json={"approved_document_ids": []},
    )
    assert approved.status_code == 200
    assert approved.json() == {"duplicate": True}
    assert staged_only.json() == {"duplicate": False}
    assert client.get("/api/session", headers=auth(app)).json() == before

    assert client.post(
        "/api/documents/discard",
        headers=auth(app),
        json={"document_ids": [first_id]},
    ).status_code == 200
    after_cancel = client.post(
        f"/api/documents/{second_id}/duplicate",
        headers=auth(app),
        json={"approved_document_ids": []},
    )
    assert after_cancel.json() == {"duplicate": False}

    assert client.post(
        "/api/documents/discard",
        headers=auth(app),
        json={"document_ids": [second_id]},
    ).status_code == 200
    loaded = load_pdf(client, app, source)
    assert loaded["documents"][0]["filename"] == "same.pdf"
    app.state.ask_open_pdfs = lambda _initial: [other]
    again = client.post("/api/dialogs/open-pdfs", headers=auth(app)).json()["items"][0]
    in_workspace = client.post(
        f"/api/documents/{again['document_id']}/duplicate",
        headers=auth(app),
        json={"approved_document_ids": []},
    )
    assert in_workspace.json() == {"duplicate": True}

    assert post_command(client, app, {"type": "undo"}).status_code == 200
    app.state.ask_open_pdfs = lambda _initial: [source]
    undone = client.post("/api/dialogs/open-pdfs", headers=auth(app)).json()["items"][0]
    after_undo = client.post(
        f"/api/documents/{undone['document_id']}/duplicate",
        headers=auth(app),
        json={"approved_document_ids": []},
    )
    assert after_undo.json() == {"duplicate": False}


def test_duplicate_recheck_rejects_unknown_and_locked_pdf(env, tmp_path):
    client, app, _session, _path = env
    secret = tmp_path / "secret.pdf"
    write_encrypted_pdf(secret)
    app.state.ask_open_pdfs = lambda _initial: [secret]
    opened = client.post("/api/dialogs/open-pdfs", headers=auth(app)).json()["items"][0]
    locked = client.post(
        f"/api/documents/{opened['document_id']}/duplicate",
        headers=auth(app),
        json={"approved_document_ids": []},
    )
    assert locked.status_code == 400
    assert post_password(client, app, opened["document_id"], "secret").status_code == 200
    unlocked = client.post(
        f"/api/documents/{opened['document_id']}/duplicate",
        headers=auth(app),
        json={"approved_document_ids": []},
    )
    assert unlocked.status_code == 200
    assert unlocked.json() == {"duplicate": False}
    unknown = client.post(
        "/api/documents/missing/duplicate",
        headers=auth(app),
        json={"approved_document_ids": []},
    )
    assert unknown.status_code == 400


def test_settings_and_help_match_index_without_bearer(env):
    client, app, _session, _path = env
    index = client.get("/")
    settings = client.get("/settings")
    help_page = client.get("/help")
    assert index.status_code == 200
    assert settings.status_code == 200
    assert help_page.status_code == 200
    assert settings.text == index.text
    assert help_page.text == index.text
    assert app.state.token in index.text


def test_about_returns_name_and_version(env):
    client, app, _session, _path = env
    before = client.get("/api/session", headers=auth(app)).json()
    response = client.get("/api/about", headers=auth(app))
    assert response.status_code == 200
    assert response.json() == {"name": "PDF編集", "version": "0.1.0", "cleared_leftover": False}
    assert client.get("/api/about").status_code == 401
    assert client.get("/api/session", headers=auth(app)).json() == before


def test_missing_version_file_falls_back(tmp_path):
    assert read_app_version(tmp_path / "missing.toml") == "0.1.0"


def test_prune_logs_drops_old_files_then_oldest_over_ten_megabytes(tmp_path):
    directory = tmp_path / "logs"
    directory.mkdir()
    now = datetime(2026, 9, 27, 12, 0, 0)
    stamp = now.timestamp()
    ancient = directory / "ancient.log"
    edge = directory / "edge.log"
    ancient.write_bytes(b"old")
    edge.write_bytes(b"edge")
    os.utime(ancient, ((now - timedelta(days=30, seconds=1)).timestamp(),) * 2)
    os.utime(edge, ((now - timedelta(days=30)).timestamp(),) * 2)
    prune_logs(directory, now=now)
    assert not ancient.exists()
    assert edge.exists()

    sized = tmp_path / "sized"
    sized.mkdir()
    oldest = sized / "oldest.bin"
    middle = sized / "middle.bin"
    newest = sized / "newest.bin"
    oldest.write_bytes(b"x" * (6 * 1024 * 1024))
    middle.write_bytes(b"y" * (6 * 1024 * 1024))
    newest.write_bytes(b"z")
    os.utime(oldest, (stamp - 30, stamp - 30))
    os.utime(middle, (stamp - 20, stamp - 20))
    os.utime(newest, (stamp - 10, stamp - 10))
    assert oldest.stat().st_size + middle.stat().st_size + newest.stat().st_size > LIMIT_BYTES
    prune_logs(sized, now=now)
    assert not oldest.exists()
    assert middle.exists()
    assert newest.exists()

    exact = tmp_path / "exact"
    exact.mkdir()
    exact_file = exact / "exact.bin"
    exact_file.write_bytes(b"e" * LIMIT_BYTES)
    os.utime(exact_file, (stamp, stamp))
    prune_logs(exact, now=now)
    assert exact_file.exists()


def test_open_logs_creates_directory_without_changing_session(env, tmp_path):
    client, app, _session, _path = env
    directory = tmp_path / "logs"
    directory.mkdir()
    old = directory / "old.log"
    fresh = directory / "fresh.log"
    old.write_bytes(b"old")
    fresh.write_bytes(b"fresh")
    os.utime(old, ((datetime.now() - timedelta(days=31)).timestamp(),) * 2)
    app.state.logs.directory = directory
    opened: list[Path] = []
    app.state.logs.open_folder = lambda target: opened.append(target)
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    load_pdf(client, app, source)
    before = client.get("/api/session", headers=auth(app)).json()

    response = client.post("/api/logs/open", headers=auth(app))

    assert response.status_code == 200
    assert response.json() == {"status": "opened"}
    assert directory.is_dir()
    assert opened == [directory]
    assert not old.exists()
    assert fresh.exists()
    assert client.get("/api/session", headers=auth(app)).json() == before
    assert client.post("/api/logs/open").status_code == 401
