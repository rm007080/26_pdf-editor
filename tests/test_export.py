import threading
import time
from pathlib import Path

import pikepdf
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.save_dialog import documents_folder
from app.session import Session

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
    assert post_file(client, app, source).status_code == 200
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
    assert post_file(client, app, source).status_code == 200
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
    assert post_file(client, app, source).status_code == 200
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
    assert post_file(client, app, source).status_code == 200
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
    assert post_file(client, app, source).status_code == 200
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
    assert post_file(client, app, source).status_code == 200
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
        "/api/session/file",
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
    assert post_file(client, app, source).status_code == 200
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
        "/api/session/file",
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
    assert post_file(client, app, source).status_code == 200
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
    assert post_file(client, app, source).status_code == 200
    kept = client.get("/api/session", headers=auth(app)).json()
    copies = {path.name: path.read_bytes() for path in session.temp_dir.iterdir()}

    def collide() -> None:
        session.mark_dialog_before_commit_for_test()

    session.before_commit = collide
    response = post_file(client, app, incoming)

    assert response.status_code == 409
    assert client.get("/api/session", headers=auth(app)).json() == kept
    assert {path.name: path.read_bytes() for path in session.temp_dir.iterdir()} == copies


def test_open_actions_work_only_after_success(env, tmp_path):
    client, app, _session = env
    source = tmp_path / "sample.pdf"
    write_pdf(source, pages=1)
    assert post_file(client, app, source).status_code == 200
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
