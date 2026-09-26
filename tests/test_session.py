import atexit
import threading
import time
from pathlib import Path

import pikepdf
import pytest
from fastapi.testclient import TestClient

from app.__main__ import register_exit_cleanup
from app.main import create_app
from app.session import Activity, Session


def write_pdf(path: Path, pages: int) -> None:
    pdf = pikepdf.Pdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(72, 72))
    pdf.save(path)


def write_encrypted_pdf(path: Path) -> None:
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(72, 72))
    pdf.save(path, encryption=pikepdf.Encryption(user="secret", owner="owner-secret"))


@pytest.fixture
def env(tmp_path):
    session = Session(temp_dir=tmp_path / "pdf-editor")
    app = create_app(session)
    with TestClient(app) as client:
        yield client, app, session


def auth(app) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.token}"}


def post_file(client: TestClient, app, path: Path, content_type: str = "application/pdf"):
    return client.post(
        "/api/documents",
        headers=auth(app),
        files={"file": (path.name, path.read_bytes(), content_type)},
    )


def patch_selection(client: TestClient, app, selected_page_ids, active_page_id, headers=None):
    return client.patch(
        "/api/session/selection",
        headers=auth(app) if headers is None else headers,
        json={"selected_page_ids": selected_page_ids, "active_page_id": active_page_id},
    )


def test_empty_session(env):
    client, app, _session = env
    response = client.get("/api/session", headers=auth(app))
    assert response.status_code == 200
    assert response.json() == {
        "documents": [],
        "pages": [],
        "selected_page_ids": [],
        "active_page_id": None,
    }


def test_load_records_pages_and_leaves_original_unchanged(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=3)
    before = source.read_bytes()

    response = post_file(client, app, source)

    assert response.status_code == 200
    body = response.json()
    assert body["documents"][0]["filename"] == "sample.pdf"
    assert [page["source_page"] for page in body["pages"]] == [1, 2, 3]
    assert [page["order"] for page in body["pages"]] == [1, 2, 3]
    assert all(page["deleted"] is False and page["rotation"] == 0 for page in body["pages"])
    assert all(page["document_id"] == body["documents"][0]["id"] for page in body["pages"])
    assert body["selected_page_ids"] == [body["pages"][0]["id"]]
    assert body["active_page_id"] == body["pages"][0]["id"]
    assert source.read_bytes() == before


def test_non_pdf_is_not_kept(env, tmp_path):
    client, app, session = env
    source = tmp_path / "notes.txt"
    source.write_text("not a pdf", encoding="utf-8")

    response = post_file(client, app, source, content_type="text/plain")

    assert response.status_code == 400
    assert response.json()["detail"]
    assert list(session.temp_dir.iterdir()) == []


def test_unreadable_pdf_is_not_kept(env, tmp_path):
    client, app, session = env
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"%PDF-1.7\nthis is not a pdf")

    response = post_file(client, app, source)

    assert response.status_code == 400
    assert response.json()["detail"]
    assert list(session.temp_dir.iterdir()) == []


def test_password_pdf_is_not_kept(env, tmp_path):
    client, app, session = env
    source = tmp_path / "secret.pdf"
    write_encrypted_pdf(source)

    response = post_file(client, app, source)

    assert response.status_code == 400
    assert "パスワード" in response.json()["detail"]
    assert list(session.temp_dir.iterdir()) == []


def test_second_success_replaces_previous_copy(env, tmp_path):
    client, app, session = env
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    write_pdf(first, pages=2)
    write_pdf(second, pages=1)
    assert post_file(client, app, first).status_code == 200
    previous = {path.name for path in session.temp_dir.iterdir()}

    response = post_file(client, app, second)

    assert response.status_code == 200
    body = response.json()
    assert body["documents"][0]["filename"] == "second.pdf"
    assert [page["source_page"] for page in body["pages"]] == [1]
    assert body["selected_page_ids"] == [body["pages"][0]["id"]]
    assert body["active_page_id"] == body["pages"][0]["id"]
    current = {path.name for path in session.temp_dir.iterdir()}
    assert previous.isdisjoint(current)
    assert len(current) == 1


def test_second_failure_keeps_previous_state_and_copy(env, tmp_path):
    client, app, session = env
    first = tmp_path / "first.pdf"
    broken = tmp_path / "broken.pdf"
    write_pdf(first, pages=2)
    broken.write_bytes(b"%PDF-1.7\nnope")
    assert post_file(client, app, first).status_code == 200
    before = client.get("/api/session", headers=auth(app)).json()
    copies = {path.name: path.read_bytes() for path in session.temp_dir.iterdir()}

    response = post_file(client, app, broken)

    assert response.status_code == 400
    after = client.get("/api/session", headers=auth(app)).json()
    assert after == before
    assert after["selected_page_ids"] == before["selected_page_ids"]
    assert after["active_page_id"] == before["active_page_id"]
    assert {path.name: path.read_bytes() for path in session.temp_dir.iterdir()} == copies


def test_overlapping_load_is_rejected_without_damage(env, tmp_path):
    client, app, session = env
    existing = tmp_path / "existing.pdf"
    incoming = tmp_path / "incoming.pdf"
    extra = tmp_path / "extra.pdf"
    write_pdf(existing, pages=2)
    write_pdf(incoming, pages=1)
    write_pdf(extra, pages=1)
    assert post_file(client, app, existing).status_code == 200
    kept = client.get("/api/session", headers=auth(app)).json()
    started = threading.Event()
    release = threading.Event()

    def pause() -> None:
        started.set()
        assert release.wait(5)

    session.pause_during_load = pause
    result: dict[str, object] = {}

    def load_incoming() -> None:
        result["response"] = post_file(client, app, incoming)

    worker = threading.Thread(target=load_incoming)
    worker.start()
    assert started.wait(5)
    conflict = post_file(client, app, extra)
    during = client.get("/api/session", headers=auth(app)).json()
    release.set()
    worker.join(5)

    assert conflict.status_code == 409
    assert during == kept
    assert during["selected_page_ids"] == kept["selected_page_ids"]
    assert during["active_page_id"] == kept["active_page_id"]
    assert worker.is_alive() is False
    response = result["response"]
    assert response.status_code == 200
    assert response.json()["documents"][0]["filename"] == "incoming.pdf"


def test_session_file_supports_range(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    assert post_file(client, app, source).status_code == 200
    stored = next(session.temp_dir.iterdir()).read_bytes()

    response = client.get(
        "/api/session/file",
        headers={**auth(app), "Range": "bytes=0-3"},
    )

    assert response.status_code == 206
    assert response.headers["accept-ranges"] == "bytes"
    assert response.content == stored[:4]
    assert response.headers.get("content-encoding") in (None, "identity")


def test_startup_removes_leftover_files(tmp_path):
    temp = tmp_path / "pdf-editor"
    temp.mkdir()
    (temp / "left.pdf").write_bytes(b"old")
    session = Session(temp_dir=temp)
    app = create_app(session)
    with TestClient(app):
        assert not (temp / "left.pdf").exists()
        assert list(temp.iterdir()) == []


def test_shutdown_hook_removes_files(tmp_path, monkeypatch):
    temp = tmp_path / "pdf-editor"
    session = Session(temp_dir=temp)
    session.startup()
    (temp / "left.pdf").write_bytes(b"old")
    registered: list[object] = []
    monkeypatch.setattr(atexit, "register", lambda fn: registered.append(fn))
    register_exit_cleanup(session)
    assert registered == [session.shutdown]
    session.shutdown()
    assert not (temp / "left.pdf").exists()


def test_empty_pdf_has_no_selection(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "empty.pdf"
    write_pdf(source, pages=0)

    response = post_file(client, app, source)

    assert response.status_code == 200
    body = response.json()
    assert body["pages"] == []
    assert body["selected_page_ids"] == []
    assert body["active_page_id"] is None


def test_patch_selection_returns_visible_order(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=3)
    loaded = post_file(client, app, source).json()
    ids = [page["id"] for page in loaded["pages"]]

    response = patch_selection(client, app, [ids[2], ids[0]], ids[2])

    assert response.status_code == 200
    body = response.json()
    assert body["selected_page_ids"] == [ids[0], ids[2]]
    assert body["active_page_id"] == ids[2]
    assert client.get("/api/session", headers=auth(app)).json()["selected_page_ids"] == [ids[0], ids[2]]


def test_patch_empty_selection_clears_active_page(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    assert post_file(client, app, source).status_code == 200

    response = patch_selection(client, app, [], None)

    assert response.status_code == 200
    body = response.json()
    assert body["selected_page_ids"] == []
    assert body["active_page_id"] is None


def test_patch_rejects_invalid_selection_without_changing_session(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=3)
    loaded = post_file(client, app, source).json()
    ids = [page["id"] for page in loaded["pages"]]
    assert patch_selection(client, app, [ids[1]], ids[1]).status_code == 200
    session.pages[0]["deleted"] = True
    hidden = ids[0]
    before = client.get("/api/session", headers=auth(app)).json()

    cases = [
        ([ids[1]], ids[2]),
        ([ids[1], ids[1]], ids[1]),
        ([hidden], hidden),
        (["missing"], "missing"),
        ([], ids[1]),
        ([ids[1]], None),
    ]
    for selected, active in cases:
        response = patch_selection(client, app, selected, active)
        assert response.status_code == 400
        assert client.get("/api/session", headers=auth(app)).json() == before


def test_patch_selection_conflicts_while_busy(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    loaded = post_file(client, app, source).json()
    before = client.get("/api/session", headers=auth(app)).json()
    ids = [page["id"] for page in loaded["pages"]]

    session.mark_dialog_before_commit_for_test()
    dialog = patch_selection(client, app, [ids[1]], ids[1])
    assert dialog.status_code == 409
    assert client.get("/api/session", headers=auth(app)).json() == before

    session.finish(Activity.DIALOG)
    assert patch_selection(client, app, [ids[1]], ids[1]).status_code == 200


def test_patch_selection_conflicts_while_loading(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    incoming = tmp_path / "incoming.pdf"
    write_pdf(source, pages=2)
    write_pdf(incoming, pages=1)
    assert post_file(client, app, source).status_code == 200
    before = client.get("/api/session", headers=auth(app)).json()
    started = threading.Event()
    release = threading.Event()

    def pause() -> None:
        started.set()
        assert release.wait(5)

    session.pause_during_load = pause
    result: dict[str, object] = {}

    def load_incoming() -> None:
        result["response"] = post_file(client, app, incoming)

    worker = threading.Thread(target=load_incoming)
    worker.start()
    assert started.wait(5)
    conflict = patch_selection(client, app, [], None)
    during = client.get("/api/session", headers=auth(app)).json()
    release.set()
    worker.join(5)

    assert conflict.status_code == 409
    assert during["selected_page_ids"] == before["selected_page_ids"]
    assert during["active_page_id"] == before["active_page_id"]
    assert result["response"].status_code == 200


def test_patch_selection_conflicts_while_exporting(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    loaded = post_file(client, app, source).json()
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
    started_export = client.post("/api/exports/all", headers=auth(app))
    assert started_export.status_code == 200
    assert started.wait(5)
    conflict = patch_selection(client, app, [loaded["pages"][1]["id"]], loaded["pages"][1]["id"])
    during = client.get("/api/session", headers=auth(app)).json()
    release.set()

    assert conflict.status_code == 409
    assert during == before
    job_id = started_export.json()["job_id"]
    for _ in range(50):
        job = client.get(f"/api/exports/{job_id}", headers=auth(app)).json()
        if job["stage"] in {"completed", "failed"}:
            break
        time.sleep(0.05)


def test_patch_selection_without_token_is_rejected(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    loaded = post_file(client, app, source).json()
    page_id = loaded["pages"][0]["id"]

    response = patch_selection(client, app, [page_id], page_id, headers={})

    assert response.status_code == 401
    assert client.get("/api/session", headers=auth(app)).json()["selected_page_ids"] == [page_id]


def test_session_file_without_token_is_rejected(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    assert post_file(client, app, source).status_code == 200

    response = client.get("/api/session/file")

    assert response.status_code == 401
