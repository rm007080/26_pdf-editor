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


def load_pdf(client: TestClient, app, path: Path) -> dict:
    received = post_file(client, app, path)
    assert received.status_code == 200, received.text
    document_id = received.json()["document_id"]
    appended = client.post(
        "/api/session/commands",
        headers=auth(app),
        json={"type": "append_documents", "document_ids": [document_id]},
    )
    assert appended.status_code == 200, appended.text
    return appended.json()


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
        "undo_enabled": False,
        "redo_enabled": False,
        "undo_hint": "元に戻せる操作がありません。実行しても状態は変わりません",
        "redo_hint": "やり直せる操作がありません。実行しても状態は変わりません",
        "unsaved": False,
    }


def test_load_records_pages_and_leaves_original_unchanged(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=3)
    original = source.read_bytes()

    before = client.get("/api/session", headers=auth(app)).json()
    received = post_file(client, app, source)

    assert received.status_code == 200
    assert set(received.json()) == {"document_id"}
    assert client.get("/api/session", headers=auth(app)).json() == before
    appended = client.post(
        "/api/session/commands",
        headers=auth(app),
        json={"type": "append_documents", "document_ids": [received.json()["document_id"]]},
    )
    assert appended.status_code == 200
    body = appended.json()
    assert body["documents"][0]["filename"] == "sample.pdf"
    assert [page["source_page"] for page in body["pages"]] == [1, 2, 3]
    assert [page["order"] for page in body["pages"]] == [1, 2, 3]
    assert all(page["deleted"] is False and page["rotation"] == 0 for page in body["pages"])
    assert all(page["document_id"] == body["documents"][0]["id"] for page in body["pages"])
    assert body["selected_page_ids"] == [body["pages"][0]["id"]]
    assert body["active_page_id"] == body["pages"][0]["id"]
    assert source.read_bytes() == original


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


def test_second_success_appends_and_keeps_previous_copy(env, tmp_path):
    client, app, session = env
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    write_pdf(first, pages=2)
    write_pdf(second, pages=1)
    loaded = load_pdf(client, app, first)
    previous = {path.name for path in session.temp_dir.iterdir()}
    received = post_file(client, app, second)
    assert received.status_code == 200

    response = client.post(
        "/api/session/commands",
        headers=auth(app),
        json={"type": "append_documents", "document_ids": [received.json()["document_id"]]},
    )

    assert response.status_code == 200
    body = response.json()
    assert [document["filename"] for document in body["documents"]] == ["first.pdf", "second.pdf"]
    assert [page["source_page"] for page in body["pages"]] == [1, 2, 1]
    assert body["selected_page_ids"] == loaded["selected_page_ids"]
    assert body["active_page_id"] == loaded["active_page_id"]
    assert body["unsaved"] is True
    current = {path.name for path in session.temp_dir.iterdir()}
    assert previous < current
    assert len(current) == 2


def test_second_failure_keeps_previous_state_and_copy(env, tmp_path):
    client, app, session = env
    first = tmp_path / "first.pdf"
    broken = tmp_path / "broken.pdf"
    write_pdf(first, pages=2)
    broken.write_bytes(b"%PDF-1.7\nnope")
    load_pdf(client, app, first)
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
    load_pdf(client, app, existing)
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
    assert set(response.json()) == {"document_id"}
    assert client.get("/api/session", headers=auth(app)).json() == kept


def test_session_file_supports_range(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    loaded = load_pdf(client, app, source)
    stored = next(session.temp_dir.iterdir()).read_bytes()

    response = client.get(
        f"/api/session/files/{loaded['documents'][0]['id']}",
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

    body = load_pdf(client, app, source)

    assert body["pages"] == []
    assert body["selected_page_ids"] == []
    assert body["active_page_id"] is None
    assert body["unsaved"] is False


def test_patch_selection_returns_visible_order(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=3)
    loaded = load_pdf(client, app, source)
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
    load_pdf(client, app, source)

    response = patch_selection(client, app, [], None)

    assert response.status_code == 200
    body = response.json()
    assert body["selected_page_ids"] == []
    assert body["active_page_id"] is None


def test_patch_rejects_invalid_selection_without_changing_session(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=3)
    loaded = load_pdf(client, app, source)
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
    loaded = load_pdf(client, app, source)
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
    load_pdf(client, app, source)
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
    loaded = load_pdf(client, app, source)
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
    loaded = load_pdf(client, app, source)
    page_id = loaded["pages"][0]["id"]

    response = patch_selection(client, app, [page_id], page_id, headers={})

    assert response.status_code == 401
    assert client.get("/api/session", headers=auth(app)).json()["selected_page_ids"] == [page_id]


def test_session_file_without_token_is_rejected(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    load_pdf(client, app, source)

    response = client.get("/api/session/files/missing")

    assert response.status_code == 401


def post_command(client: TestClient, app, body, headers=None):
    return client.post(
        "/api/session/commands",
        headers=auth(app) if headers is None else headers,
        json=body,
    )


def without_command_flags(body: dict) -> tuple[dict, bool, bool]:
    rest = dict(body)
    changed = rest.pop("changed")
    dropped = rest.pop("history_dropped")
    return rest, changed, dropped


def page_ids(body: dict) -> list[str]:
    return [page["id"] for page in body["pages"]]


def test_rotate_is_one_undo_and_redo_restores_angle_and_selection(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=3)
    original = source.read_bytes()
    loaded = load_pdf(client, app, source)
    ids = page_ids(loaded)
    assert patch_selection(client, app, [ids[0], ids[2]], ids[2]).status_code == 200

    response = post_command(
        client,
        app,
        {"type": "rotate", "page_ids": [ids[2], ids[0]], "direction": "right"},
    )

    assert response.status_code == 200
    rest, changed, dropped = without_command_flags(response.json())
    assert changed is True
    assert dropped is False
    assert [page["rotation"] for page in rest["pages"]] == [90, 0, 90]
    assert rest["selected_page_ids"] == [ids[0], ids[2]]
    assert rest["active_page_id"] == ids[2]
    assert rest["undo_enabled"] is True
    assert rest["undo_hint"] == "左に90度回転する"
    assert rest["redo_enabled"] is False
    fetched = client.get("/api/session", headers=auth(app)).json()
    assert "changed" not in fetched
    assert "history_dropped" not in fetched
    assert fetched == rest
    assert source.read_bytes() == original
    assert len(list(session.temp_dir.iterdir())) == 1

    undone = post_command(client, app, {"type": "undo"})
    assert undone.status_code == 200
    undone_body, changed, dropped = without_command_flags(undone.json())
    assert changed is True and dropped is False
    assert [page["rotation"] for page in undone_body["pages"]] == [0, 0, 0]
    assert undone_body["selected_page_ids"] == [ids[0], ids[2]]
    assert undone_body["active_page_id"] == ids[2]
    assert undone_body["undo_enabled"] is True
    assert undone_body["undo_hint"] == "追加したPDFを外す"
    assert undone_body["redo_enabled"] is True
    assert undone_body["redo_hint"] == "右に90度回転する"

    redone = post_command(client, app, {"type": "redo"})
    assert redone.status_code == 200
    redone_body, changed, _dropped = without_command_flags(redone.json())
    assert changed is True
    assert [page["rotation"] for page in redone_body["pages"]] == [90, 0, 90]
    assert redone_body["selected_page_ids"] == [ids[0], ids[2]]
    assert redone_body["active_page_id"] == ids[2]
    assert post_command(client, app, {"type": "redo"}).status_code == 400
    assert client.get("/api/session", headers=auth(app)).json() == redone_body


def test_four_rotations_return_to_zero(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    loaded = load_pdf(client, app, source)
    page_id = loaded["pages"][0]["id"]

    for _ in range(4):
        assert post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"}).status_code == 200
    assert client.get("/api/session", headers=auth(app)).json()["pages"][0]["rotation"] == 0

    for _ in range(4):
        assert post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "left"}).status_code == 200
    assert client.get("/api/session", headers=auth(app)).json()["pages"][0]["rotation"] == 0


def test_invalid_commands_leave_pages_selection_and_history(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=3)
    loaded = load_pdf(client, app, source)
    ids = page_ids(loaded)
    assert post_command(client, app, {"type": "rotate", "page_ids": [ids[0]], "direction": "right"}).status_code == 200
    before = client.get("/api/session", headers=auth(app)).json()

    cases = [
        {"type": "restore_deleted", "pages": []},
        {"type": "restore_order", "pages": []},
        {"type": "crop"},
        {"type": "rotate", "page_ids": [], "direction": "right"},
        {"type": "rotate", "page_ids": [ids[0], ids[0]], "direction": "right"},
        {"type": "rotate", "page_ids": ["missing"], "direction": "right"},
        {"type": "rotate", "page_ids": [ids[0]], "direction": "up"},
        {"type": "delete", "page_ids": []},
        {"type": "reorder", "page_ids": [ids[1]]},
        {"type": "reorder", "page_ids": [ids[1]], "before_page_id": ids[0], "after_page_id": ids[2]},
        {"type": "reorder", "page_ids": [ids[1]], "before_page_id": ids[1]},
        {"type": "reorder", "page_ids": [ids[0], ids[1]], "before_page_id": ids[2]},
        {"type": "reorder", "page_ids": [ids[1], ids[2]], "after_page_id": ids[0]},
        {"page_ids": [ids[0]]},
        {"type": "remove_documents", "document_ids": [loaded["documents"][0]["id"]]},
    ]
    for body in cases:
        response = post_command(client, app, body)
        assert response.status_code == 400, body
        assert client.get("/api/session", headers=auth(app)).json() == before

    broken = client.post(
        "/api/session/commands",
        headers={**auth(app), "Content-Type": "application/json"},
        content=b"{",
    )
    assert broken.status_code == 400
    assert client.get("/api/session", headers=auth(app)).json() == before

    # 上の rotate が履歴に残っている。戻すと、その前の追加がまだ残る。
    assert post_command(client, app, {"type": "undo"}).status_code == 200
    empty = client.get("/api/session", headers=auth(app)).json()
    assert empty["pages"][0]["rotation"] == 0
    assert empty["undo_enabled"] is True
    assert empty["undo_hint"] == "追加したPDFを外す"
    rejected_redo = post_command(client, app, {"type": "redo"})
    assert rejected_redo.status_code == 200
    exhausted = post_command(client, app, {"type": "redo"})
    assert exhausted.status_code == 400
    assert client.get("/api/session", headers=auth(app)).json()["pages"][0]["rotation"] == 90


@pytest.mark.parametrize("activity", [Activity.LOADING, Activity.DIALOG, Activity.EXPORTING])
def test_command_conflicts_while_busy(env, tmp_path, activity):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    loaded = load_pdf(client, app, source)
    before = client.get("/api/session", headers=auth(app)).json()
    assert session.try_begin(activity)

    response = post_command(
        client,
        app,
        {"type": "rotate", "page_ids": [loaded["pages"][0]["id"]], "direction": "right"},
    )
    during = client.get("/api/session", headers=auth(app)).json()
    session.finish(activity)

    assert response.status_code == 409
    assert during == before
    assert before["pages"][0]["rotation"] == 0
    assert before["undo_enabled"] is True


def test_noop_reorder_does_not_enter_history(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=3)
    loaded = load_pdf(client, app, source)
    ids = page_ids(loaded)
    assert post_command(client, app, {"type": "rotate", "page_ids": [ids[0]], "direction": "left"}).status_code == 200
    assert post_command(client, app, {"type": "undo"}).status_code == 200
    assert client.get("/api/session", headers=auth(app)).json()["redo_enabled"] is True
    assert patch_selection(client, app, [ids[1]], ids[1]).status_code == 200

    response = post_command(
        client,
        app,
        {"type": "reorder", "page_ids": [ids[1]], "before_page_id": ids[2]},
    )

    assert response.status_code == 200
    rest, changed, dropped = without_command_flags(response.json())
    assert changed is False
    assert dropped is False
    assert [page["order"] for page in rest["pages"]] == [1, 2, 3]
    assert rest["undo_enabled"] is True
    assert rest["undo_hint"] == "追加したPDFを外す"
    assert rest["redo_enabled"] is True
    assert client.get("/api/session", headers=auth(app)).json() == rest


def test_delete_keeps_other_selection_and_restores_on_undo(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=6)
    original = source.read_bytes()
    loaded = load_pdf(client, app, source)
    ids = page_ids(loaded)
    copy_before = next(session.temp_dir.iterdir()).read_bytes()
    assert patch_selection(client, app, [ids[1], ids[4]], ids[4]).status_code == 200

    response = post_command(client, app, {"type": "delete", "page_ids": [ids[1]]})

    assert response.status_code == 200
    rest, changed, _dropped = without_command_flags(response.json())
    assert changed is True
    assert rest["selected_page_ids"] == [ids[4]]
    assert rest["active_page_id"] == ids[4]
    visible = [page for page in rest["pages"] if not page["deleted"]]
    assert [page["order"] for page in visible] == [1, 2, 3, 4, 5]
    deleted = next(page for page in rest["pages"] if page["id"] == ids[1])
    assert deleted["deleted"] is True
    assert deleted["order"] == 0
    assert source.read_bytes() == original
    assert next(session.temp_dir.iterdir()).read_bytes() == copy_before

    undone = post_command(client, app, {"type": "undo"})
    assert undone.status_code == 200
    restored, _changed, _dropped = without_command_flags(undone.json())
    assert [page["deleted"] for page in restored["pages"]] == [False, False, False, False, False, False]
    assert [page["order"] for page in restored["pages"]] == [1, 2, 3, 4, 5, 6]
    assert restored["selected_page_ids"] == [ids[1], ids[4]]
    assert restored["active_page_id"] == ids[4]


def test_delete_of_active_page_selects_nearest_remaining_selection(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=6)
    loaded = load_pdf(client, app, source)
    ids = page_ids(loaded)
    assert patch_selection(client, app, [ids[1], ids[3], ids[5]], ids[3]).status_code == 200

    removed = post_command(client, app, {"type": "delete", "page_ids": [ids[3]]})

    assert removed.status_code == 200
    body, _changed, _dropped = without_command_flags(removed.json())
    assert body["selected_page_ids"] == [ids[1], ids[5]]
    assert body["active_page_id"] == ids[5]

    assert post_command(client, app, {"type": "undo"}).status_code == 200
    assert patch_selection(client, app, [ids[1], ids[3], ids[5]], ids[5]).status_code == 200
    tail = post_command(client, app, {"type": "delete", "page_ids": [ids[5]]})
    tail_body, _changed, _dropped = without_command_flags(tail.json())
    assert tail_body["selected_page_ids"] == [ids[1], ids[3]]
    assert tail_body["active_page_id"] == ids[3]


def test_delete_of_whole_selection_selects_page_that_arrives(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=5)
    loaded = load_pdf(client, app, source)
    ids = page_ids(loaded)
    assert patch_selection(client, app, [ids[1], ids[2]], ids[2]).status_code == 200

    response = post_command(client, app, {"type": "delete", "page_ids": [ids[1], ids[2]]})

    assert response.status_code == 200
    body, _changed, _dropped = without_command_flags(response.json())
    assert body["selected_page_ids"] == [ids[3]]
    assert body["active_page_id"] == ids[3]
    assert post_command(client, app, {"type": "undo"}).status_code == 200

    assert patch_selection(client, app, [ids[3], ids[4]], ids[4]).status_code == 200
    tail = post_command(client, app, {"type": "delete", "page_ids": [ids[3], ids[4]]})
    tail_body, _changed, _dropped = without_command_flags(tail.json())
    assert tail_body["selected_page_ids"] == [ids[2]]
    assert tail_body["active_page_id"] == ids[2]

    assert post_command(client, app, {"type": "undo"}).status_code == 200
    everything = post_command(client, app, {"type": "delete", "page_ids": ids})
    empty, _changed, _dropped = without_command_flags(everything.json())
    assert empty["selected_page_ids"] == []
    assert empty["active_page_id"] is None
    assert all(page["deleted"] is True and page["order"] == 0 for page in empty["pages"])


def test_deleted_page_cannot_be_rotated_or_reordered(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=3)
    loaded = load_pdf(client, app, source)
    ids = page_ids(loaded)
    assert post_command(client, app, {"type": "delete", "page_ids": [ids[1]]}).status_code == 200
    before = client.get("/api/session", headers=auth(app)).json()

    rotate = post_command(client, app, {"type": "rotate", "page_ids": [ids[1]], "direction": "left"})
    reorder = post_command(client, app, {"type": "reorder", "page_ids": [ids[0]], "before_page_id": ids[1]})

    assert rotate.status_code == 400
    assert reorder.status_code == 400
    assert client.get("/api/session", headers=auth(app)).json() == before


def test_unselected_reorder_selects_that_page_and_undo_restores_selection(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=5)
    loaded = load_pdf(client, app, source)
    ids = page_ids(loaded)

    response = post_command(client, app, {"type": "reorder", "page_ids": [ids[2]], "after_page_id": ids[4]})

    assert response.status_code == 200
    body, changed, dropped = without_command_flags(response.json())
    assert changed is True and dropped is False
    visible = sorted((page for page in body["pages"] if not page["deleted"]), key=lambda page: page["order"])
    assert [page["source_page"] for page in visible] == [1, 2, 4, 5, 3]
    assert [page["order"] for page in visible] == [1, 2, 3, 4, 5]
    assert body["selected_page_ids"] == [ids[2]]
    assert body["active_page_id"] == ids[2]

    undone = post_command(client, app, {"type": "undo"})
    restored, _changed, _dropped = without_command_flags(undone.json())
    restored_visible = sorted(restored["pages"], key=lambda page: page["order"])
    assert [page["source_page"] for page in restored_visible] == [1, 2, 3, 4, 5]
    assert [page["order"] for page in restored_visible] == [1, 2, 3, 4, 5]
    assert restored["selected_page_ids"] == [ids[0]]
    assert restored["active_page_id"] == ids[0]
    assert restored["undo_hint"] == "追加したPDFを外す"


def test_selection_reorder_keeps_order_and_one_undo(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=5)
    loaded = load_pdf(client, app, source)
    ids = page_ids(loaded)
    assert patch_selection(client, app, [ids[1], ids[3]], ids[3]).status_code == 200

    response = post_command(
        client,
        app,
        {"type": "reorder", "page_ids": [ids[3], ids[1]], "after_page_id": ids[4]},
    )

    assert response.status_code == 200
    body, _changed, _dropped = without_command_flags(response.json())
    visible = sorted(body["pages"], key=lambda page: page["order"])
    assert [page["source_page"] for page in visible] == [1, 3, 5, 2, 4]
    assert body["selected_page_ids"] == [ids[1], ids[3]]
    assert body["active_page_id"] == ids[3]
    undone = post_command(client, app, {"type": "undo"})
    restored, _changed, _dropped = without_command_flags(undone.json())
    restored_visible = sorted(restored["pages"], key=lambda page: page["order"])
    assert [page["source_page"] for page in restored_visible] == [1, 2, 3, 4, 5]
    assert restored["selected_page_ids"] == [ids[1], ids[3]]
    assert restored["active_page_id"] == ids[3]
    removed = post_command(client, app, {"type": "undo"})
    assert removed.status_code == 200
    assert removed.json()["documents"] == []
    assert post_command(client, app, {"type": "undo"}).status_code == 400


def test_new_edit_after_undo_discards_redo(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    loaded = load_pdf(client, app, source)
    page_id = loaded["pages"][0]["id"]
    assert post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"}).status_code == 200
    assert post_command(client, app, {"type": "undo"}).status_code == 200
    assert client.get("/api/session", headers=auth(app)).json()["redo_enabled"] is True

    assert post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "left"}).status_code == 200

    current = client.get("/api/session", headers=auth(app)).json()
    assert current["pages"][0]["rotation"] == 270
    assert current["redo_enabled"] is False
    assert post_command(client, app, {"type": "redo"}).status_code == 400
    assert client.get("/api/session", headers=auth(app)).json() == current


def test_history_drop_is_notified_once_until_replace(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    loaded = load_pdf(client, app, source)
    page_id = loaded["pages"][0]["id"]

    dropped_flags = []
    for _ in range(102):
        response = post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"})
        assert response.status_code == 200
        dropped_flags.append(response.json()["history_dropped"])

    assert dropped_flags[:99] == [False] * 99
    assert dropped_flags[99] is True
    assert dropped_flags[100:] == [False, False]
    assert client.get("/api/session", headers=auth(app)).json()["pages"][0]["rotation"] == 180
    assert len(list(session.temp_dir.iterdir())) == 1
    for _ in range(100):
        assert post_command(client, app, {"type": "undo"}).status_code == 200
    kept = client.get("/api/session", headers=auth(app)).json()
    assert kept["pages"][0]["rotation"] == 180
    assert kept["undo_enabled"] is False
    assert post_command(client, app, {"type": "undo"}).status_code == 400

    replacement = tmp_path / "other.pdf"
    write_pdf(replacement, pages=1)
    added = load_pdf(client, app, replacement)
    assert added["history_dropped"] is False
    assert added["undo_enabled"] is True
    assert [document["filename"] for document in added["documents"]] == ["sample.pdf", "other.pdf"]
    assert len(list(session.temp_dir.iterdir())) == 2
    next_id = added["pages"][-1]["id"]
    flags = []
    for _ in range(101):
        response = post_command(client, app, {"type": "rotate", "page_ids": [next_id], "direction": "right"})
        flags.append(response.json()["history_dropped"])
    assert flags == [False] * 101


def test_failed_replace_keeps_history_and_selection(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    loaded = load_pdf(client, app, source)
    ids = page_ids(loaded)
    assert patch_selection(client, app, [ids[1]], ids[1]).status_code == 200
    assert post_command(client, app, {"type": "rotate", "page_ids": [ids[1]], "direction": "right"}).status_code == 200
    before = client.get("/api/session", headers=auth(app)).json()
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\nthis is not a pdf")

    response = post_file(client, app, broken)

    assert response.status_code == 400
    assert client.get("/api/session", headers=auth(app)).json() == before
    refused = post_command(client, app, {"type": "append_documents", "document_ids": ["missing"]})
    assert refused.status_code == 400
    assert client.get("/api/session", headers=auth(app)).json() == before


def test_selection_patch_does_not_add_history(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=3)
    loaded = load_pdf(client, app, source)
    ids = page_ids(loaded)

    assert patch_selection(client, app, [ids[1]], ids[1]).status_code == 200
    assert client.get("/api/session", headers=auth(app)).json()["undo_enabled"] is True
    assert client.get("/api/session", headers=auth(app)).json()["undo_hint"] == "追加したPDFを外す"
    assert post_command(client, app, {"type": "rotate", "page_ids": [ids[1]], "direction": "right"}).status_code == 200
    assert patch_selection(client, app, [ids[2]], ids[2]).status_code == 200
    current = client.get("/api/session", headers=auth(app)).json()
    assert current["undo_enabled"] is True
    assert current["selected_page_ids"] == [ids[2]]

    undone = post_command(client, app, {"type": "undo"})
    body, _changed, _dropped = without_command_flags(undone.json())
    assert body["pages"][1]["rotation"] == 0
    assert body["selected_page_ids"] == [ids[1]]
    assert body["active_page_id"] == ids[1]
    removed = post_command(client, app, {"type": "undo"})
    assert removed.status_code == 200
    assert removed.json()["documents"] == []
    assert post_command(client, app, {"type": "undo"}).status_code == 400


def test_commands_require_token(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    load_pdf(client, app, source)

    response = post_command(client, app, {"type": "undo"}, headers={})

    assert response.status_code == 401


def test_append_redo_restores_same_pages_then_later_redo_uses_them(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    loaded = load_pdf(client, app, source)
    ids = page_ids(loaded)
    filename = loaded["documents"][0]["filename"]
    assert post_command(client, app, {"type": "rotate", "page_ids": [ids[0]], "direction": "right"}).status_code == 200
    assert post_command(client, app, {"type": "delete", "page_ids": [ids[1]]}).status_code == 200
    assert post_command(client, app, {"type": "undo"}).status_code == 200
    assert post_command(client, app, {"type": "undo"}).status_code == 200
    assert post_command(client, app, {"type": "undo"}).status_code == 200
    cleared = client.get("/api/session", headers=auth(app)).json()
    assert cleared["documents"] == []
    assert cleared["pages"] == []
    assert cleared["unsaved"] is True

    redone = post_command(client, app, {"type": "redo"})
    assert redone.status_code == 200
    body = redone.json()
    assert [page["id"] for page in body["pages"]] == ids
    assert [page["rotation"] for page in body["pages"]] == [0, 0]
    assert [page["deleted"] for page in body["pages"]] == [False, False]
    assert body["documents"][0]["filename"] == filename
    assert body["selected_page_ids"] == [ids[0]]
    assert body["active_page_id"] == ids[0]

    rotated = post_command(client, app, {"type": "redo"})
    assert rotated.status_code == 200
    assert rotated.json()["pages"][0]["id"] == ids[0]
    assert rotated.json()["pages"][0]["rotation"] == 90
    deleted = post_command(client, app, {"type": "redo"})
    assert deleted.status_code == 200
    page = next(item for item in deleted.json()["pages"] if item["id"] == ids[1])
    assert page["deleted"] is True


def test_new_append_reuses_removed_document_records(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    loaded = load_pdf(client, app, source)
    ids = page_ids(loaded)
    document_id = loaded["documents"][0]["id"]
    filename = loaded["documents"][0]["filename"]
    assert post_command(client, app, {"type": "undo"}).status_code == 200

    again = post_command(client, app, {"type": "append_documents", "document_ids": [document_id]})

    assert again.status_code == 200
    body = again.json()
    assert body["documents"][0]["id"] == document_id
    assert body["documents"][0]["filename"] == filename
    assert [page["id"] for page in body["pages"]] == ids


def test_unsaved_single_append_undo_is_unsaved(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    single = load_pdf(client, app, source)
    assert single["unsaved"] is False
    undone = post_command(client, app, {"type": "undo"})
    assert undone.json()["unsaved"] is True
    assert undone.json()["documents"] == []


def test_unsaved_tracks_multiple_and_later_appends(env, tmp_path):
    client, app, _session = env
    second = tmp_path / "second.pdf"
    third = tmp_path / "third.pdf"
    write_pdf(second, pages=1)
    write_pdf(third, pages=1)

    received = [post_file(client, app, path) for path in (second, third)]
    assert all(response.status_code == 200 for response in received)
    added = post_command(
        client,
        app,
        {"type": "append_documents", "document_ids": [response.json()["document_id"] for response in received]},
    )
    assert added.status_code == 200
    assert added.json()["unsaved"] is True
    cleared = post_command(client, app, {"type": "undo"})
    assert cleared.status_code == 200
    assert cleared.json()["documents"] == []
    assert cleared.json()["unsaved"] is False

    again = load_pdf(client, app, second)
    assert again["unsaved"] is False
    extra = post_file(client, app, third)
    assert extra.status_code == 200
    mid = post_command(client, app, {"type": "append_documents", "document_ids": [extra.json()["document_id"]]})
    assert mid.status_code == 200
    assert mid.json()["unsaved"] is True
    assert mid.json()["selected_page_ids"] == again["selected_page_ids"]
    back = post_command(client, app, {"type": "undo"})
    assert back.json()["unsaved"] is False
    rotated = post_command(
        client,
        app,
        {"type": "rotate", "page_ids": [again["pages"][0]["id"]], "direction": "right"},
    )
    assert rotated.json()["unsaved"] is True
    patched = patch_selection(client, app, [again["pages"][0]["id"]], again["pages"][0]["id"])
    assert patched.status_code == 200
    assert patched.json()["unsaved"] is True
    assert post_command(client, app, {"type": "undo"}).json()["unsaved"] is False


def test_failed_append_keeps_received_copy(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    received = post_file(client, app, source)
    assert received.status_code == 200
    document_id = received.json()["document_id"]
    before = client.get("/api/session", headers=auth(app)).json()
    copies = {path.name: path.read_bytes() for path in session.temp_dir.iterdir()}

    cases = [
        {"type": "append_documents", "document_ids": []},
        {"type": "append_documents", "document_ids": [document_id, document_id]},
        {"type": "append_documents", "document_ids": ["missing"]},
    ]
    for body in cases:
        response = post_command(client, app, body)
        assert response.status_code == 400, body
        assert client.get("/api/session", headers=auth(app)).json() == before
        assert {path.name: path.read_bytes() for path in session.temp_dir.iterdir()} == copies


def test_discard_removes_only_fresh_intakes(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    other = tmp_path / "other.pdf"
    write_pdf(source, pages=1)
    write_pdf(other, pages=1)
    fresh = post_file(client, app, source)
    assert fresh.status_code == 200
    fresh_id = fresh.json()["document_id"]
    before = client.get("/api/session", headers=auth(app)).json()

    empty = client.post("/api/documents/discard", headers=auth(app), json={"document_ids": []})
    assert empty.status_code == 200
    assert empty.json()["discarded"] == []
    assert list(session.temp_dir.iterdir())

    removed = client.post("/api/documents/discard", headers=auth(app), json={"document_ids": [fresh_id]})
    assert removed.status_code == 200
    assert removed.json()["discarded"] == [fresh_id]
    assert list(session.temp_dir.iterdir()) == []
    assert client.get("/api/session", headers=auth(app)).json() == before

    loaded = load_pdf(client, app, source)
    document_id = loaded["documents"][0]["id"]
    kept = {path.name: path.read_bytes() for path in session.temp_dir.iterdir()}
    session_before = client.get("/api/session", headers=auth(app)).json()
    refused = client.post("/api/documents/discard", headers=auth(app), json={"document_ids": [document_id]})
    assert refused.status_code == 400
    assert {path.name: path.read_bytes() for path in session.temp_dir.iterdir()} == kept
    assert client.get("/api/session", headers=auth(app)).json() == session_before

    assert post_command(client, app, {"type": "undo"}).status_code == 200
    held = {path.name: path.read_bytes() for path in session.temp_dir.iterdir()}
    still = client.post("/api/documents/discard", headers=auth(app), json={"document_ids": [document_id]})
    assert still.status_code == 400
    assert {path.name: path.read_bytes() for path in session.temp_dir.iterdir()} == held

    extra = post_file(client, app, other)
    assert extra.status_code == 200
    with_extra = {path.name: path.read_bytes() for path in session.temp_dir.iterdir()}
    assert len(with_extra) == len(held) + 1
    mixed = client.post(
        "/api/documents/discard",
        headers=auth(app),
        json={"document_ids": [extra.json()["document_id"], document_id]},
    )
    assert mixed.status_code == 400
    assert {path.name: path.read_bytes() for path in session.temp_dir.iterdir()} == with_extra


def test_document_file_is_workspace_only(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    received = post_file(client, app, source)
    document_id = received.json()["document_id"]
    hidden = client.get(f"/api/session/files/{document_id}", headers=auth(app))
    missing = client.get("/api/session/files/missing", headers=auth(app))
    assert hidden.status_code == 404
    assert missing.status_code == 404
    added = post_command(client, app, {"type": "append_documents", "document_ids": [document_id]})
    assert added.status_code == 200
    opened = client.get(f"/api/session/files/{document_id}", headers=auth(app))
    assert opened.status_code == 200
    assert post_command(client, app, {"type": "undo"}).status_code == 200
    gone = client.get(f"/api/session/files/{document_id}", headers=auth(app))
    assert gone.status_code == 404
    old = client.get("/api/session/file", headers=auth(app))
    assert old.status_code == 404
