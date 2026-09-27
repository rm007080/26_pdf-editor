import threading
import time
from pathlib import Path

import pikepdf
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.save_dialog import documents_folder
from app.session import Activity, Session
from tests.test_session import load_pdf, patch_selection, post_command, visible_ids

STAGES = {"copying", "verifying", "committing", "completed", "failed"}


def write_pdf(path: Path, pages: int) -> None:
    pdf = pikepdf.Pdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(72, 72))
    pdf.save(path)


@pytest.fixture
def env(tmp_path):
    session = Session(temp_dir=tmp_path / "pdf-editor")
    app = create_app(session)
    with TestClient(app) as client:
        yield client, app, session


def auth(app) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.token}"}


def post_file(client, app, path: Path):
    return client.post(
        "/api/documents",
        headers=auth(app),
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
    )


def post_export(client, app):
    return client.post("/api/exports/all", headers=auth(app))


def wait_job(client, app, job_id: str) -> dict:
    last = None
    for _ in range(50):
        response = client.get(f"/api/exports/{job_id}", headers=auth(app))
        assert response.status_code == 200
        last = response.json()
        if last["stage"] in {"completed", "failed"}:
            return last
        time.sleep(0.05)
    raise AssertionError(last)


def test_documents_folder_exists():
    assert documents_folder().is_dir()


def test_export_matches_page_count_and_keeps_original(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    original = source.read_bytes()
    load_pdf(client, app, source)
    before_copy = next(session.temp_dir.iterdir()).read_bytes()
    destination = tmp_path / "out"
    destination.mkdir()
    seen: dict[str, object] = {}

    def dialog(initial_dir: Path, initial_name: str) -> Path:
        seen["dir"] = initial_dir
        seen["name"] = initial_name
        return destination / "renamed.pdf"

    app.state.exporter.ask_save_path = dialog
    response = post_export(client, app)
    assert response.status_code == 200
    job = wait_job(client, app, response.json()["job_id"])

    assert seen["name"] == "sample_編集済み.pdf"
    assert Path(seen["dir"]).is_dir()
    assert job["stage"] == "completed"
    assert job["processed_pages"] == job["total_pages"] == 2
    saved = Path(job["path"])
    assert saved.name == "renamed.pdf"
    with pikepdf.open(saved) as pdf:
        assert len(pdf.pages) == 2
    assert source.read_bytes() == original
    assert next(session.temp_dir.iterdir()).read_bytes() == before_copy
    assert client.get("/api/session", headers=auth(app)).json()["documents"][0]["filename"] == "sample.pdf"


def test_existing_name_is_not_overwritten(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    load_pdf(client, app, source)
    destination = tmp_path / "out"
    destination.mkdir()
    existing = destination / "sample_編集済み.pdf"
    existing.write_bytes(b"keep-me")

    app.state.exporter.ask_save_path = lambda _directory, _name: existing
    response = post_export(client, app)
    job = wait_job(client, app, response.json()["job_id"])

    assert existing.read_bytes() == b"keep-me"
    assert Path(job["path"]) == destination / "sample_編集済み(1).pdf"
    with pikepdf.open(job["path"]) as pdf:
        assert len(pdf.pages) == 1


def test_cancel_leaves_no_file_and_allows_another_export(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    load_pdf(client, app, source)
    copies = {path.name for path in session.temp_dir.iterdir()}
    app.state.exporter.ask_save_path = lambda _directory, _name: None

    response = post_export(client, app)

    assert response.status_code == 200
    assert response.json() == {"status": "cancelled"}
    assert "job_id" not in response.json()
    assert {path.name for path in session.temp_dir.iterdir()} == copies

    again = post_export(client, app)
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"


def test_failed_verification_removes_temp_file(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    load_pdf(client, app, source)
    destination = tmp_path / "out"
    destination.mkdir()
    app.state.exporter.ask_save_path = lambda _directory, _name: destination / "out.pdf"
    app.state.exporter.page_count = lambda _path: 0

    response = post_export(client, app)
    job = wait_job(client, app, response.json()["job_id"])

    assert job["stage"] == "failed"
    assert list(destination.iterdir()) == []


def test_insufficient_space_creates_no_file(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    load_pdf(client, app, source)
    destination = tmp_path / "out"
    destination.mkdir()
    app.state.exporter.ask_save_path = lambda _directory, _name: destination / "out.pdf"
    app.state.exporter.enough_space = lambda _directory, _required: False

    response = post_export(client, app)
    job = wait_job(client, app, response.json()["job_id"])

    assert job["stage"] == "failed"
    assert list(destination.iterdir()) == []


def test_requests_stay_responsive_while_dialog_is_open(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    loaded = load_pdf(client, app, source)
    stored = source.read_bytes()
    started = threading.Event()
    release = threading.Event()

    def dialog(_directory: Path, _name: str):
        started.set()
        release.wait(5)
        return None

    app.state.exporter.ask_save_path = dialog
    result: dict[str, object] = {}

    def export() -> None:
        result["response"] = post_export(client, app)

    worker = threading.Thread(target=export)
    worker.start()
    assert started.wait(5)
    session_response = client.get("/api/session", headers=auth(app))
    file_response = client.get(
        f"/api/session/files/{loaded['documents'][0]['id']}",
        headers={**auth(app), "Range": "bytes=0-3"},
    )
    document_conflict = post_file(client, app, source)
    export_conflict = post_export(client, app)
    release.set()
    worker.join(5)

    assert session_response.status_code == 200
    assert file_response.status_code == 206
    assert file_response.content == stored[:4]
    assert document_conflict.status_code == 409
    assert export_conflict.status_code == 409
    assert worker.is_alive() is False
    body = result["response"].json()
    assert result["response"].status_code == 200
    assert body == {"status": "cancelled"}


def test_export_in_progress_rejects_load_and_another_export(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    other = tmp_path / "other.pdf"
    write_pdf(source, pages=1)
    write_pdf(other, pages=1)
    loaded = load_pdf(client, app, source)
    kept = client.get("/api/session", headers=auth(app)).json()
    stored = next(session.temp_dir.iterdir()).read_bytes()
    destination = tmp_path / "out"
    destination.mkdir()
    started = threading.Event()
    release = threading.Event()
    app.state.exporter.ask_save_path = lambda _directory, _name: destination / "out.pdf"

    def pause() -> None:
        started.set()
        release.wait(5)

    app.state.exporter.pause_during_copy = pause
    response = post_export(client, app)
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert started.wait(5)
    during = client.get(f"/api/exports/{job_id}", headers=auth(app))
    file_response = client.get(
        f"/api/session/files/{loaded['documents'][0]['id']}",
        headers={**auth(app), "Range": "bytes=0-3"},
    )
    assert post_file(client, app, other).status_code == 409
    assert post_export(client, app).status_code == 409
    assert client.get("/api/session", headers=auth(app)).status_code == 200
    release.set()
    job = wait_job(client, app, job_id)

    assert during.json()["stage"] in STAGES
    assert file_response.status_code == 206
    assert file_response.content == stored[:4]
    assert job["stage"] == "completed"
    assert job["processed_pages"] == job["total_pages"]
    assert client.get("/api/session", headers=auth(app)).json() == kept


def test_export_is_rejected_while_loading(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    load_pdf(client, app, source)
    incoming = tmp_path / "incoming.pdf"
    write_pdf(incoming, pages=1)
    started = threading.Event()
    release = threading.Event()

    def pause() -> None:
        started.set()
        release.wait(5)

    session.pause_during_load = pause
    result: dict[str, object] = {}

    def load() -> None:
        result["response"] = post_file(client, app, incoming)

    worker = threading.Thread(target=load)
    worker.start()
    assert started.wait(5)
    conflict = post_export(client, app)
    release.set()
    worker.join(5)

    assert conflict.status_code == 409
    assert result["response"].status_code == 200


def test_inflight_load_does_not_replace_session_during_export(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    incoming = tmp_path / "incoming.pdf"
    write_pdf(source, pages=2)
    write_pdf(incoming, pages=1)
    load_pdf(client, app, source)
    kept = client.get("/api/session", headers=auth(app)).json()
    copies = {path.name: path.read_bytes() for path in session.temp_dir.iterdir()}

    def collide() -> None:
        session.mark_dialog_before_commit_for_test()

    session.before_commit = collide
    response = post_file(client, app, incoming)

    assert response.status_code == 409
    assert client.get("/api/session", headers=auth(app)).json() == kept
    assert {path.name: path.read_bytes() for path in session.temp_dir.iterdir()} == copies


def test_export_adds_session_rotation_without_changing_source(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(72, 144))
    pdf.pages[0].rotate(90, relative=False)
    pdf.add_blank_page(page_size=(72, 72))
    pdf.save(source)
    original = source.read_bytes()
    loaded = load_pdf(client, app, source)
    ids = [page["id"] for page in loaded["pages"]]
    command = client.post(
        "/api/session/commands",
        headers=auth(app),
        json={"type": "rotate", "page_ids": ids, "direction": "right"},
    )
    assert command.status_code == 200
    destination = tmp_path / "out"
    destination.mkdir()
    app.state.exporter.ask_save_path = lambda _directory, _name: destination / "out.pdf"

    response = post_export(client, app)
    job = wait_job(client, app, response.json()["job_id"])

    assert job["stage"] == "completed"
    with pikepdf.open(job["path"]) as exported:
        assert [page.rotation for page in exported.pages] == [180, 90]
    with pikepdf.open(next(session.temp_dir.iterdir())) as stored:
        assert [page.rotation for page in stored.pages] == [90, 0]
    assert source.read_bytes() == original


def test_export_rejected_when_no_visible_pages(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    loaded = load_pdf(client, app, source)
    deleted = client.post(
        "/api/session/commands",
        headers=auth(app),
        json={"type": "delete", "page_ids": [loaded["pages"][0]["id"]]},
    )
    assert deleted.status_code == 200

    response = post_export(client, app)

    assert response.status_code == 400


def test_open_actions_work_only_after_success(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    load_pdf(client, app, source)
    destination = tmp_path / "out"
    destination.mkdir()
    started = threading.Event()
    release = threading.Event()
    app.state.exporter.ask_save_path = lambda _directory, _name: destination / "out.pdf"

    def pause() -> None:
        started.set()
        release.wait(5)

    app.state.exporter.pause_during_copy = pause
    response = post_export(client, app)
    job_id = response.json()["job_id"]
    assert started.wait(5)
    assert client.post(f"/api/exports/{job_id}/open-file", headers=auth(app)).status_code == 409
    assert client.post(f"/api/exports/{job_id}/open-folder", headers=auth(app)).status_code == 409
    release.set()
    job = wait_job(client, app, job_id)
    opened: list[Path] = []
    folders: list[Path] = []
    app.state.exporter.open_file = opened.append
    app.state.exporter.open_folder = folders.append

    assert client.post(f"/api/exports/{job_id}/open-file", headers=auth(app)).status_code == 200
    assert client.post(f"/api/exports/{job_id}/open-folder", headers=auth(app)).status_code == 200
    assert opened == [Path(job["path"])]
    assert folders == [Path(job["path"])]


def write_sized(path: Path, sizes: list[tuple[int, int]]) -> None:
    pdf = pikepdf.Pdf.new()
    for width, height in sizes:
        pdf.add_blank_page(page_size=(width, height))
    pdf.save(path)


def test_export_copies_pages_from_each_document_in_order(env, tmp_path):
    client, app, _session = env
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    write_sized(first, [(72, 100), (72, 120)])
    write_sized(second, [(72, 200)])
    load_pdf(client, app, first)
    load_pdf(client, app, second)
    destination = tmp_path / "out"
    destination.mkdir()
    seen: dict[str, str] = {}

    def dialog(_directory: Path, name: str) -> Path:
        seen["name"] = name
        return destination / "out.pdf"

    app.state.exporter.ask_save_path = dialog
    response = post_export(client, app)
    job = wait_job(client, app, response.json()["job_id"])

    assert seen["name"] == "first_編集済み.pdf"
    assert job["stage"] == "completed"
    assert "unsaved" not in job
    with pikepdf.open(job["path"]) as pdf:
        assert [float(page.mediabox[3]) for page in pdf.pages] == [100, 120, 200]
    session = client.get("/api/session", headers=auth(app)).json()
    assert session["unsaved"] is False
    assert session["undo_enabled"] is True


def test_export_filename_skips_document_without_visible_pages(env, tmp_path):
    client, app, _session = env
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    write_sized(first, [(72, 80)])
    write_sized(second, [(72, 90)])
    loaded = load_pdf(client, app, first)
    deleted = client.post(
        "/api/session/commands",
        headers=auth(app),
        json={"type": "delete", "page_ids": [loaded["pages"][0]["id"]]},
    )
    assert deleted.status_code == 200
    load_pdf(client, app, second)
    destination = tmp_path / "out"
    destination.mkdir()
    seen: dict[str, str] = {}
    app.state.exporter.ask_save_path = lambda _directory, name: seen.setdefault("name", name) and destination / "out.pdf"
    response = post_export(client, app)
    job = wait_job(client, app, response.json()["job_id"])

    assert seen["name"] == "second_編集済み.pdf"
    with pikepdf.open(job["path"]) as pdf:
        assert len(pdf.pages) == 1
        assert float(pdf.pages[0].mediabox[3]) == 90


def test_failed_and_cancelled_export_keep_unsaved(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    loaded = load_pdf(client, app, source)
    rotated = client.post(
        "/api/session/commands",
        headers=auth(app),
        json={"type": "rotate", "page_ids": [loaded["pages"][0]["id"]], "direction": "right"},
    )
    assert rotated.json()["unsaved"] is True
    app.state.exporter.ask_save_path = lambda _directory, _name: None
    cancelled = post_export(client, app)
    assert cancelled.json() == {"status": "cancelled"}
    assert client.get("/api/session", headers=auth(app)).json()["unsaved"] is True

    destination = tmp_path / "out"
    destination.mkdir()
    app.state.exporter.ask_save_path = lambda _directory, _name: destination / "out.pdf"
    app.state.exporter.page_count = lambda _path: 0
    failed = post_export(client, app)
    job = wait_job(client, app, failed.json()["job_id"])
    assert job["stage"] == "failed"
    assert "unsaved" not in job
    assert "files" not in job
    assert client.get("/api/session", headers=auth(app)).json()["unsaved"] is True
    assert client.get("/api/session", headers=auth(app)).json()["undo_enabled"] is True


def post_selection(client, app):
    return client.post("/api/exports/selection", headers=auth(app))


def post_split(client, app):
    return client.post("/api/exports/split", headers=auth(app))


def post_confirm(client, app, names):
    return client.post("/api/exports/split/confirm", headers=auth(app), json={"names": names})


def post_cancel(client, app):
    return client.post("/api/exports/split/cancel", headers=auth(app))


def test_selection_exports_visible_order_and_keeps_unsaved(env, tmp_path):
    client, app, session = env
    source = tmp_path / "picked.pdf"
    write_sized(source, [(72, 100), (72, 120), (72, 140), (72, 160)])
    original = source.read_bytes()
    loaded = load_pdf(client, app, source)
    ids = visible_ids(loaded)
    rotated = post_command(client, app, {"type": "rotate", "page_ids": [ids[1]], "direction": "right"})
    assert rotated.json()["unsaved"] is True
    assert post_command(client, app, {"type": "set_split", "after_page_id": ids[0]}).json()["split_unexported"] is True
    assert patch_selection(client, app, [ids[3], ids[1]], ids[1]).status_code == 200
    copy_size = session.workspace_copy_path(loaded["documents"][0]["id"]).stat().st_size
    assert patch_selection(client, app, [], None).status_code == 200
    refused = post_selection(client, app)
    assert patch_selection(client, app, [ids[3], ids[1]], ids[1]).status_code == 200
    destination = tmp_path / "out"
    destination.mkdir()
    seen: dict[str, object] = {}

    def dialog(_directory: Path, name: str) -> Path:
        seen["name"] = name
        return destination / "picked.pdf"

    def space(_directory: Path, required: int) -> bool:
        seen["bytes"] = required
        return True

    app.state.exporter.ask_save_path = dialog
    app.state.exporter.enough_space = space
    response = post_selection(client, app)
    job = wait_job(client, app, response.json()["job_id"])

    assert refused.status_code == 400
    assert seen["name"] == "picked_編集済み.pdf"
    assert seen["bytes"] == copy_size
    assert job["stage"] == "completed"
    assert isinstance(job["path"], str)
    assert "files" not in job
    assert "split_unexported" not in job
    with pikepdf.open(job["path"]) as pdf:
        assert [float(page.mediabox[3]) for page in pdf.pages] == [120, 160]
        assert pdf.pages[0].rotation == 90
    session_body = client.get("/api/session", headers=auth(app)).json()
    assert session_body["unsaved"] is True
    assert session_body["split_unexported"] is True
    assert source.read_bytes() == original


def test_split_without_lines_writes_one_numbered_file(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    load_pdf(client, app, source)
    destination = tmp_path / "out"
    destination.mkdir()
    seen: dict[str, object] = {}

    def folder(initial: Path) -> Path:
        seen["initial"] = initial
        return destination

    app.state.exporter.ask_folder = folder
    started = post_split(client, app)
    assert started.status_code == 200
    assert list(started.json()) == ["status", "files"]
    assert started.json()["files"] == [{"name": "sample_1.pdf", "pages": 2}]
    assert Path(seen["initial"]) == documents_folder()
    confirmed = post_confirm(client, app, ["sample_1.pdf"])
    job = wait_job(client, app, confirmed.json()["job_id"])

    assert job["stage"] == "completed"
    assert job["path"] is None
    assert "split_unexported" not in job
    assert [Path(path).name for path in job["files"]] == ["sample_1.pdf"]
    with pikepdf.open(job["files"][0]) as pdf:
        assert len(pdf.pages) == 2
    session_body = client.get("/api/session", headers=auth(app)).json()
    assert session_body["split_unexported"] is False


def test_split_commits_only_after_every_file_is_verified(env, tmp_path):
    client, app, session = env
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    write_sized(first, [(72, 80), (72, 90)])
    write_sized(second, [(72, 100)])
    loaded = load_pdf(client, app, first)
    extra = load_pdf(client, app, second)
    ids = visible_ids(loaded)
    assert post_command(client, app, {"type": "set_split", "after_page_id": ids[1]}).status_code == 200
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "first_1.pdf").write_bytes(b"keep")
    calls: list[int] = []
    replaces: list[int] = []
    sizes: list[int] = []

    def counting(path: Path) -> int:
        calls.append(1)
        if len(calls) == 2:
            assert replaces == []
            assert list(destination.glob("*.pdf")) == [destination / "first_1.pdf"]
            return 0
        with pikepdf.open(path) as pdf:
            return len(pdf.pages)

    def remember(_directory: Path, required: int) -> bool:
        sizes.append(required)
        return True

    app.state.exporter.ask_folder = lambda _initial: destination
    app.state.exporter.page_count = counting
    app.state.exporter.enough_space = remember
    started = post_split(client, app)
    assert started.json()["files"] == [
        {"name": "first_1.pdf", "pages": 2},
        {"name": "first_2.pdf", "pages": 1},
    ]
    confirmed = post_confirm(client, app, ["first_1.pdf", "first_2.pdf"])
    job = wait_job(client, app, confirmed.json()["job_id"])

    copy_sizes = [
        session.workspace_copy_path(loaded["documents"][0]["id"]).stat().st_size,
        session.workspace_copy_path(extra["documents"][-1]["id"]).stat().st_size,
    ]
    assert job["stage"] == "failed"
    assert len(calls) == 2
    assert replaces == []
    assert sizes == [sum(copy_sizes)]
    assert (destination / "first_1.pdf").read_bytes() == b"keep"
    assert list(destination.iterdir()) == [destination / "first_1.pdf"]
    assert client.get("/api/session", headers=auth(app)).json()["split_unexported"] is True


def test_split_removes_files_committed_before_a_later_failure(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    loaded = load_pdf(client, app, source)
    ids = visible_ids(loaded)
    assert post_command(client, app, {"type": "set_split", "after_page_id": ids[0]}).status_code == 200
    destination = tmp_path / "out"
    destination.mkdir()
    seen: list[Path] = []

    def block(index: int, final: Path) -> None:
        seen.append(final)
        if index == 0:
            assert final.exists() is False
        if index == 1:
            assert seen[0].exists()
            raise OSError("disk full")

    app.state.exporter.ask_folder = lambda _initial: destination
    app.state.exporter.before_replace = block
    started = post_split(client, app)
    confirmed = post_confirm(client, app, ["renamed.pdf", "sample_2.pdf"])
    job = wait_job(client, app, confirmed.json()["job_id"])

    assert started.json()["files"][0]["name"] == "sample_1.pdf"
    assert job["stage"] == "failed"
    assert seen[0].exists() is False
    assert list(destination.iterdir()) == []
    assert client.get("/api/session", headers=auth(app)).json()["split_unexported"] is True


def test_split_avoids_existing_names_and_rejects_duplicates(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    loaded = load_pdf(client, app, source)
    ids = visible_ids(loaded)
    assert post_command(client, app, {"type": "set_split", "after_page_id": ids[0]}).status_code == 200
    destination = tmp_path / "out"
    destination.mkdir()
    existing = destination / "sample_1.pdf"
    existing.write_bytes(b"keep-me")
    app.state.exporter.ask_folder = lambda _initial: destination
    started = post_split(client, app)
    duplicate = post_confirm(client, app, ["sample_1.pdf", "sample_1.pdf"])
    assert duplicate.status_code == 400
    assert post_command(client, app, {"type": "rotate", "page_ids": [ids[0]], "direction": "right"}).status_code == 409
    bad_names = [
        [""],
        ["sample_1.txt", "sample_2.pdf"],
        ["sam<>ple_1.pdf", "sample_2.pdf"],
        ["sample_1.pdf ", "sample_2.pdf"],
        ["sample_1.pdf.", "sample_2.pdf"],
        ["only-one.pdf"],
    ]
    for names in bad_names:
        assert post_confirm(client, app, names).status_code == 400, names
    confirmed = post_confirm(client, app, ["sample_1.pdf", "sample_2.pdf"])
    job = wait_job(client, app, confirmed.json()["job_id"])

    assert started.json()["files"] == [
        {"name": "sample_1.pdf", "pages": 1},
        {"name": "sample_2.pdf", "pages": 1},
    ]
    assert existing.read_bytes() == b"keep-me"
    assert Path(job["files"][0]).name == "sample_1(1).pdf"
    assert Path(job["files"][1]).name == "sample_2.pdf"
    assert client.get("/api/session", headers=auth(app)).json()["split_unexported"] is False


def test_successful_split_clears_unexported_until_membership_changes(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_sized(source, [(72, 100), (72, 110), (72, 120), (72, 130)])
    loaded = load_pdf(client, app, source)
    ids = visible_ids(loaded)
    placed = post_command(client, app, {"type": "set_split", "after_page_id": ids[1]})
    assert placed.json()["split_unexported"] is True
    rotated = post_command(client, app, {"type": "rotate", "page_ids": [ids[0]], "direction": "right"})
    assert rotated.json()["split_unexported"] is True
    destination = tmp_path / "out"
    destination.mkdir()
    app.state.exporter.ask_folder = lambda _initial: destination
    started = post_split(client, app)
    confirmed = post_confirm(client, app, ["sample_1.pdf", "sample_2.pdf"])
    job = wait_job(client, app, confirmed.json()["job_id"])
    session_body = client.get("/api/session", headers=auth(app)).json()

    assert job["path"] is None
    assert len(job["files"]) == 2
    assert "split_unexported" not in job
    assert session_body["split_unexported"] is False
    again = post_command(client, app, {"type": "rotate", "page_ids": [ids[2]], "direction": "left"})
    assert again.json()["split_unexported"] is False
    inside = post_command(client, app, {"type": "reorder", "page_ids": [ids[0]], "after_page_id": ids[1]})
    assert inside.json()["split_positions"] == [2]
    assert inside.json()["split_unexported"] is False
    across = post_command(client, app, {"type": "reorder", "page_ids": [ids[2]], "before_page_id": ids[0]})
    assert across.json()["split_positions"] == [2]
    assert across.json()["split_unexported"] is True
    cleared = post_command(client, app, {"type": "clear_all_splits"})
    assert cleared.json()["split_unexported"] is False
    assert started.status_code == 200


def test_split_open_uses_index_and_opens_folder_once(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    loaded = load_pdf(client, app, source)
    ids = visible_ids(loaded)
    assert post_command(client, app, {"type": "set_split", "after_page_id": ids[0]}).status_code == 200
    destination = tmp_path / "out"
    destination.mkdir()
    app.state.exporter.ask_folder = lambda _initial: destination
    started = post_split(client, app)
    confirmed = post_confirm(client, app, ["a.pdf", "b.pdf"])
    job = wait_job(client, app, confirmed.json()["job_id"])
    opened: list[Path] = []
    folders: list[Path] = []
    app.state.exporter.open_file = opened.append
    app.state.exporter.open_folder = folders.append
    job_id = job["job_id"]

    assert client.post(f"/api/exports/{job_id}/open-file", headers=auth(app)).status_code == 400
    assert client.post(f"/api/exports/{job_id}/open-file", headers=auth(app), json={"index": 2}).status_code == 400
    assert client.post(f"/api/exports/{job_id}/open-file", headers=auth(app), json={"index": 0}).status_code == 200
    assert client.post(f"/api/exports/{job_id}/open-file", headers=auth(app), json={"index": 1}).status_code == 200
    assert client.post(f"/api/exports/{job_id}/open-folder", headers=auth(app)).status_code == 200
    assert opened == [Path(job["files"][0]), Path(job["files"][1])]
    assert folders == [Path(job["files"][0])]
    assert started.status_code == 200


def test_split_cancel_and_confirm_without_naming_leave_activity(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    loaded = load_pdf(client, app, source)
    ids = visible_ids(loaded)
    before = client.get("/api/session", headers=auth(app)).json()
    idle_confirm = post_confirm(client, app, ["a.pdf"])
    idle_cancel = post_cancel(client, app)
    assert idle_confirm.status_code == 409
    assert idle_cancel.status_code == 200
    assert client.get("/api/session", headers=auth(app)).json() == before
    assert post_command(client, app, {"type": "set_split", "after_page_id": ids[0]}).status_code == 200

    deleted = post_command(client, app, {"type": "delete", "page_ids": ids})
    assert deleted.status_code == 200
    refused = post_split(client, app)
    assert refused.status_code == 400
    assert post_command(client, app, {"type": "undo"}).status_code == 200

    destination = tmp_path / "out"
    destination.mkdir()
    app.state.exporter.ask_folder = lambda _initial: destination
    naming = post_split(client, app)
    assert naming.json()["status"] == "naming"
    assert post_confirm(client, app, ["same.pdf", "same.pdf"]).status_code == 400
    assert post_command(client, app, {"type": "rotate", "page_ids": [ids[0]], "direction": "right"}).status_code == 409
    cancelled = post_cancel(client, app)
    assert cancelled.status_code == 200
    edited = post_command(client, app, {"type": "rotate", "page_ids": [ids[0]], "direction": "left"})
    assert edited.status_code == 200
    assert edited.json()["split_positions"] == [1]
    assert client.get("/api/session", headers=auth(app)).json()["split_unexported"] is True
    assert session._activity is Activity.IDLE


def test_cancel_during_folder_wait_lets_all_pages_keep_dialog(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    loaded = load_pdf(client, app, source)
    page_id = visible_ids(loaded)[0]
    folder_started = threading.Event()
    folder_release = threading.Event()
    save_started = threading.Event()
    save_release = threading.Event()

    def folder(_initial: Path) -> Path:
        folder_started.set()
        folder_release.wait(5)
        return tmp_path / "ignored"

    def save(_directory: Path, _name: str):
        save_started.set()
        save_release.wait(5)
        return None

    app.state.exporter.ask_folder = folder
    app.state.exporter.ask_save_path = save
    split_result: dict[str, object] = {}
    save_result: dict[str, object] = {}

    def run_split() -> None:
        split_result["response"] = post_split(client, app)

    def run_save() -> None:
        save_result["response"] = post_export(client, app)

    split_worker = threading.Thread(target=run_split)
    split_worker.start()
    assert folder_started.wait(5)
    assert post_cancel(client, app).status_code == 200
    save_worker = threading.Thread(target=run_save)
    save_worker.start()
    assert save_started.wait(5)
    folder_release.set()
    split_worker.join(5)
    during = post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"})
    save_release.set()
    save_worker.join(5)

    assert split_worker.is_alive() is False
    assert split_result["response"].json() == {"status": "cancelled"}
    assert during.status_code == 409
    assert save_result["response"].status_code == 200
    assert save_result["response"].json() == {"status": "cancelled"}
    assert post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"}).status_code == 200


def test_split_while_save_dialog_is_open_does_not_finish_it(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    load_pdf(client, app, source)
    destination = tmp_path / "out"
    destination.mkdir()
    started = threading.Event()
    release = threading.Event()

    def save(_directory: Path, _name: str) -> Path:
        started.set()
        release.wait(5)
        return destination / "out.pdf"

    app.state.exporter.ask_save_path = save
    result: dict[str, object] = {}

    def run_save() -> None:
        result["response"] = post_export(client, app)

    worker = threading.Thread(target=run_save)
    worker.start()
    assert started.wait(5)
    assert post_split(client, app).status_code == 409
    release.set()
    worker.join(5)
    job = wait_job(client, app, result["response"].json()["job_id"])
    assert job["stage"] == "completed"
    assert isinstance(job["path"], str)


def test_cancel_during_load_and_export_does_not_change_activity(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    other = tmp_path / "other.pdf"
    write_pdf(source, pages=1)
    write_pdf(other, pages=1)
    loaded = load_pdf(client, app, source)
    before = client.get("/api/session", headers=auth(app)).json()
    started = threading.Event()
    release = threading.Event()

    def pause() -> None:
        started.set()
        release.wait(5)

    session.pause_during_load = pause
    result: dict[str, object] = {}

    def load() -> None:
        result["response"] = client.post(
            "/api/documents",
            headers=auth(app),
            files={"file": (other.name, other.read_bytes(), "application/pdf")},
        )

    worker = threading.Thread(target=load)
    worker.start()
    assert started.wait(5)
    assert post_cancel(client, app).status_code == 200
    assert client.get("/api/session", headers=auth(app)).json() == before
    release.set()
    worker.join(5)
    assert result["response"].status_code == 200

    destination = tmp_path / "out"
    destination.mkdir()
    export_started = threading.Event()
    export_release = threading.Event()
    app.state.exporter.ask_save_path = lambda _directory, _name: destination / "out.pdf"

    def copy_pause() -> None:
        export_started.set()
        export_release.wait(5)

    app.state.exporter.pause_during_copy = copy_pause
    response = post_export(client, app)
    assert export_started.wait(5)
    assert post_cancel(client, app).status_code == 200
    assert post_command(client, app, {"type": "rotate", "page_ids": [visible_ids(loaded)[0]], "direction": "right"}).status_code == 409
    export_release.set()
    job = wait_job(client, app, response.json()["job_id"])
    assert job["stage"] == "completed"


def test_failed_split_transition_drops_naming(env, tmp_path):
    client, app, session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=2)
    loaded = load_pdf(client, app, source)
    ids = visible_ids(loaded)
    assert post_command(client, app, {"type": "set_split", "after_page_id": ids[0]}).status_code == 200
    destination = tmp_path / "out"
    destination.mkdir()
    app.state.exporter.ask_folder = lambda _initial: destination
    assert post_split(client, app).json()["status"] == "naming"
    session._activity = Activity.IDLE
    failed = post_confirm(client, app, ["a.pdf", "b.pdf"])
    assert failed.status_code == 409
    assert post_confirm(client, app, ["a.pdf", "b.pdf"]).status_code == 409
    edited = post_command(client, app, {"type": "clear_split", "after_page_id": ids[0]})
    assert edited.status_code == 200
    assert edited.json()["split_positions"] == []
