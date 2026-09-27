import os
import socket
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.__main__ import apply_frozen_environment, ensure_port_free, main
from app.auth import new_token
from app.main import EDITOR_URL, create_app, dist_dir
from app.session import Session
from app.single_instance import (
    MUTEX_NAME,
    OPEN_FAILED_MESSAGE,
    PORT_BUSY_MESSAGE,
    acquire_mutex,
    delete_session_token,
    finish_as_second_instance,
    read_session_token,
    signal_running_instance,
    token_path,
    write_session_token,
)
from app.tray import (
    BOTH_CONFIRM,
    ID_OPEN,
    ID_QUIT,
    MENU_OPEN,
    MENU_QUIT,
    SPLIT_CONFIRM,
    UNSAVED_CONFIRM,
    Tray,
    decide_quit,
    loss_confirm_message,
    menu_entries,
)
from tests.test_session import auth, claim_tab, load_pdf, post_command, release_tab, write_pdf


class _Server:
    def __init__(self) -> None:
        self.should_exit = False


class _Icon:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.removed = False

    def remove(self) -> None:
        self.removed = True
        self.order.append("tray")


def _app(tmp_path, now, present, order, server, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    session = Session(temp_dir=tmp_path / "work")
    icon = _Icon(order)

    def tail() -> None:
        order.append("token")
        delete_session_token()
        icon.remove()
        order.append("server")
        server.should_exit = True

    original = session.shutdown

    def wrapped() -> None:
        order.append("temp")
        original()

    session.shutdown = wrapped
    app = create_app(
        session,
        settings_path=tmp_path / "settings.json",
        log_dir=tmp_path / "logs",
        clock=lambda: now[0],
        stop=tail,
        present_exit=present,
    )
    write_session_token(app.state.token)
    return session, app, icon


def test_mutex_name_and_second_acquire_fails():
    assert MUTEX_NAME == "Local\\pdf-editor"
    name = f"Local\\pdf-editor-test-{os.getpid()}-{threading.get_ident()}"
    assert acquire_mutex(name) is True
    assert acquire_mutex(name) is False


def test_second_instance_opens_the_existing_screen_and_does_not_start_another(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    write_session_token("same-token")
    seen = []
    monkeypatch.setattr("app.__main__.acquire_mutex", lambda: False)
    monkeypatch.setattr("app.single_instance.signal_running_instance", lambda token: seen.append(token) or True)
    created = []
    monkeypatch.setattr("app.__main__.Session", lambda *args, **kwargs: created.append("session"))
    monkeypatch.setattr("app.__main__.create_app", lambda *args, **kwargs: created.append("app"))

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 0
    assert seen == ["same-token"]
    assert created == []
    assert "same-token" not in capsys.readouterr().err


def test_second_instance_exits_1_when_open_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    write_session_token("same-token")
    monkeypatch.setattr("app.__main__.acquire_mutex", lambda: False)
    monkeypatch.setattr("app.single_instance.signal_running_instance", lambda token: False)
    created = []
    monkeypatch.setattr("app.__main__.create_app", lambda *args, **kwargs: created.append("app"))

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 1
    assert created == []
    assert capsys.readouterr().err.strip() == OPEN_FAILED_MESSAGE


def test_missing_token_file_uses_the_port_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("app.__main__.acquire_mutex", lambda: False)
    created = []
    monkeypatch.setattr("app.__main__.create_app", lambda *args, **kwargs: created.append("app"))

    with pytest.raises(SystemExit) as caught:
        main()

    assert caught.value.code == 1
    assert created == []
    assert capsys.readouterr().err.strip() == PORT_BUSY_MESSAGE


def test_signal_running_instance_succeeds_only_on_http_success():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.headers.get("Authorization") == "Bearer good":
                self.send_response(200)
                self.end_headers()
                return
            self.send_response(401)
            self.end_headers()

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/editor/open"
    try:
        assert signal_running_instance("good", url) is True
        assert signal_running_instance("bad", url) is False
        assert signal_running_instance("good", "http://127.0.0.1:9/api/editor/open") is False
    finally:
        server.shutdown()


def test_port_in_use_without_a_second_instance_does_not_start_a_backend(monkeypatch, capsys):
    monkeypatch.setattr("app.__main__.acquire_mutex", lambda: True)
    created = []
    monkeypatch.setattr("app.__main__.Session", lambda *args, **kwargs: created.append("session"))

    class Busy:
        def bind(self, _address):
            raise OSError("busy")

        def close(self):
            return None

    monkeypatch.setattr("app.__main__.socket.socket", lambda *args, **kwargs: Busy())
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    assert created == []
    assert capsys.readouterr().err.strip() == PORT_BUSY_MESSAGE


def test_token_file_matches_app_state_and_is_private(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    calls = []
    real = new_token

    def counting():
        value = real()
        calls.append(value)
        return value

    monkeypatch.setattr("app.main.new_token", counting)
    session = Session(temp_dir=tmp_path / "work")
    app = create_app(session, settings_path=tmp_path / "settings.json", log_dir=tmp_path / "logs")
    write_session_token(app.state.token)
    assert calls == [app.state.token]
    assert read_session_token() == app.state.token
    assert app.state.token not in capsys.readouterr().err
    output = subprocess.check_output(["icacls", str(token_path())], text=True, encoding="oem")
    username = os.environ["USERNAME"]
    assert username in output
    body = [line.strip() for line in output.splitlines() if line.strip() and "処理" not in line and "processed" not in line.lower()]
    aces = [line for line in body if ":" in line]
    assert len(aces) == 1
    assert "(I)" not in output
    session.shutdown()
    assert token_path().exists()


def test_first_instance_writes_the_token_and_clean_quit_uses_common_stop(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("app.__main__.acquire_mutex", lambda: True)
    monkeypatch.setattr("app.__main__.ensure_port_free", lambda: None)
    monkeypatch.setattr("app.__main__.open_browser_when_ready", lambda: None)
    calls = []
    monkeypatch.setattr("app.main.new_token", lambda: calls.append("token-not-for-logs") or "token-not-for-logs")
    monkeypatch.setattr("app.__main__.Session", lambda: Session(temp_dir=tmp_path / "work"))
    posted = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: posted.append("open"))

    class FakeTray:
        def __init__(self, on_open, on_quit):
            self.on_open = on_open
            self.on_quit = on_quit
            self.removed = False

        def start(self):
            return None

        def remove(self):
            self.removed = True

        def ask(self, text, on_yes, on_no):
            raise AssertionError("確認は出さない")

    trays = []

    def build_tray(on_open, on_quit):
        tray = FakeTray(on_open, on_quit)
        trays.append(tray)
        return tray

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.should_exit = False
            servers.append(self)

        def run(self):
            return None

    servers = []
    monkeypatch.setattr("app.__main__.Tray", build_tray)
    monkeypatch.setattr("app.__main__.uvicorn.Server", FakeServer)
    monkeypatch.setattr("app.__main__.uvicorn.run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("run")))

    main()

    assert calls == ["token-not-for-logs"]
    assert read_session_token() == "token-not-for-logs"
    assert len(servers) == 1
    assert len(trays) == 1
    opened = []
    monkeypatch.setattr("app.main.webbrowser.open", lambda url: opened.append(url))
    trays[0].on_open()
    assert opened == [EDITOR_URL]
    trays[0].on_quit()
    assert trays[0].removed is True
    assert servers[0].should_exit is True
    assert not token_path().exists()
    assert posted == []
    assert "token-not-for-logs" not in capsys.readouterr().err
    log_dir = tmp_path / "pdf-editor" / "logs"
    if log_dir.exists():
        for path in log_dir.iterdir():
            assert "token-not-for-logs" not in path.read_text(encoding="utf-8", errors="ignore")


def test_open_does_not_need_the_editor_and_does_not_change_state(monkeypatch, tmp_path):
    now = [0.0]
    presented = []
    order = []
    server = _Server()
    session, app, _icon = _app(tmp_path, now, presented.append, order, server, monkeypatch)
    opened = []
    monkeypatch.setattr("app.main.webbrowser.open", lambda url: opened.append(url))
    with TestClient(app) as client:
        claim_tab(client, app)
        source = tmp_path / "sample.pdf"
        write_pdf(source, pages=1)
        loaded = load_pdf(client, app, source)
        before = session.snapshot()
        missing_lease = client.post("/api/editor/open", headers={"Authorization": f"Bearer {app.state.token}"})
        assert missing_lease.status_code == 200
        assert missing_lease.json() == {"opened": True}
        assert app.state.token not in missing_lease.text
        assert opened == [EDITOR_URL]
        assert session.snapshot() == before
        other = client.post("/api/editor/open", headers={"Authorization": f"Bearer {app.state.token}", "X-Tab-Id": "tab-b"})
        assert other.status_code == 200
        page_id = loaded["pages"][0]["id"]
        rotated = post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"})
        assert rotated.json()["unsaved"] is True
        after = session.snapshot()
        client.post("/api/editor/open", headers={"Authorization": f"Bearer {app.state.token}"})
        assert session.snapshot() == after


def test_clean_quit_runs_common_stop_without_http_shutdown(monkeypatch, tmp_path):
    now = [0.0]
    presented = []
    order = []
    server = _Server()
    session, app, icon = _app(tmp_path, now, presented.append, order, server, monkeypatch)
    posted = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: posted.append(args))
    with TestClient(app) as client:
        claim_tab(client, app)
        source = tmp_path / "sample.pdf"
        write_pdf(source, pages=1)
        load_pdf(client, app, source)
        (session.temp_dir / "keep.pdf").write_bytes(b"keep")
        decide_quit(
            dirty=False,
            holder=app.state.lease.holder(),
            stop=app.state.idle.shutdown_now,
            begin_exit=lambda: app.state.exit_request.begin(app.state.lease.last_seen()),
            ask=presented.append,
            message=loss_confirm_message(False, False),
        )
    assert presented == []
    assert order == ["temp", "token", "tray", "server"]
    assert icon.removed is True
    assert server.should_exit is True
    assert not token_path().exists()
    assert not session.temp_dir.exists()
    assert posted == []


def test_shutdown_route_uses_the_same_common_stop(monkeypatch, tmp_path):
    now = [0.0]
    order = []
    server = _Server()
    session, app, _icon = _app(tmp_path, now, lambda text: None, order, server, monkeypatch)
    with TestClient(app) as client:
        claim_tab(client, app)
        source = tmp_path / "sample.pdf"
        write_pdf(source, pages=1)
        loaded = load_pdf(client, app, source)
        page_id = loaded["pages"][0]["id"]
        post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"})
        confirmed = client.post("/api/shutdown", headers=auth(app), json={"confirmed": True})
        assert confirmed.json()["stopped"] is True
    assert order == ["temp", "token", "tray", "server"]
    assert server.should_exit is True
    assert not token_path().exists()


def test_dirty_quit_with_a_holder_asks_on_heartbeat_and_cancel_does_not_stop(monkeypatch, tmp_path):
    now = [0.0]
    presented = []
    order = []
    server = _Server()
    session, app, _icon = _app(tmp_path, now, presented.append, order, server, monkeypatch)
    with TestClient(app) as client:
        claim_tab(client, app)
        source = tmp_path / "sample.pdf"
        write_pdf(source, pages=2)
        loaded = load_pdf(client, app, source)
        page_id = loaded["pages"][0]["id"]
        rotated = post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"})
        assert rotated.json()["unsaved"] is True
        assert rotated.json()["split_unexported"] is False
        decide_quit(
            dirty=True,
            holder=app.state.lease.holder(),
            stop=app.state.idle.shutdown_now,
            begin_exit=lambda: app.state.exit_request.begin(app.state.lease.last_seen()),
            ask=presented.append,
            message=UNSAVED_CONFIRM,
        )
        assert presented == []
        assert order == []
        assert server.should_exit is False
        beat = client.post(
            "/api/editor/heartbeat",
            headers=auth(app),
            json={"tab_id": "tab-a"},
        )
        assert beat.json()["exit_requested"] is True
        assert beat.json()["holding"] is True
        session_body = client.get("/api/session", headers=auth(app)).json()
        assert session_body["exit_requested"] is True
        anonymous = client.post("/api/session/exit-cancel", headers={"Authorization": f"Bearer {app.state.token}"})
        assert anonymous.status_code == 409
        cancelled = client.post("/api/session/exit-cancel", headers=auth(app))
        assert cancelled.json() == {"exit_requested": False}
        assert app.state.exit_request.requested is False
        assert server.should_exit is False
        assert token_path().exists()
        again = client.post(
            "/api/editor/heartbeat",
            headers=auth(app),
            json={"tab_id": "tab-a"},
        )
        assert again.json()["exit_requested"] is False


def test_split_only_and_both_use_the_fourth_plan_wording(monkeypatch, tmp_path):
    now = [0.0]
    asked = []
    session, app, _icon = _app(tmp_path, now, lambda text: None, [], _Server(), monkeypatch)
    with TestClient(app) as client:
        claim_tab(client, app)
        source = tmp_path / "sample.pdf"
        write_pdf(source, pages=2)
        loaded = load_pdf(client, app, source)
        ids = [page["id"] for page in loaded["pages"]]
        placed = post_command(client, app, {"type": "set_split", "after_page_id": ids[0]})
        assert placed.json()["split_unexported"] is True
        assert placed.json()["unsaved"] is False
        release_tab(client, app, "tab-a")
        decide_quit(
            dirty=True,
            holder=app.state.lease.holder(),
            stop=app.state.idle.shutdown_now,
            begin_exit=lambda: None,
            ask=asked.append,
            message=loss_confirm_message(False, True),
        )
        assert asked == [SPLIT_CONFIRM]
        asked.clear()
        claim_tab(client, app)
        post_command(client, app, {"type": "rotate", "page_ids": [ids[0]], "direction": "right"})
        release_tab(client, app, "tab-a")
        decide_quit(
            dirty=True,
            holder=None,
            stop=app.state.idle.shutdown_now,
            begin_exit=lambda: None,
            ask=asked.append,
            message=loss_confirm_message(True, True),
        )
        assert asked == [BOTH_CONFIRM]
        assert loss_confirm_message(True, False) == UNSAVED_CONFIRM
        assert session.temp_dir.exists()


def test_message_box_cancel_does_not_stop_and_yes_uses_common_stop(monkeypatch, tmp_path):
    now = [0.0]
    order = []
    server = _Server()
    session, app, _icon = _app(tmp_path, now, lambda text: None, order, server, monkeypatch)
    answers = {"yes": False}

    def ask(message: str) -> None:
        assert message == UNSAVED_CONFIRM
        if answers["yes"]:
            app.state.idle.shutdown_now()

    with TestClient(app) as client:
        claim_tab(client, app)
        source = tmp_path / "sample.pdf"
        write_pdf(source, pages=1)
        loaded = load_pdf(client, app, source)
        page_id = loaded["pages"][0]["id"]
        post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"})
        release_tab(client, app, "tab-a")
        decide_quit(
            dirty=True,
            holder=app.state.lease.holder(),
            stop=app.state.idle.shutdown_now,
            begin_exit=lambda: app.state.exit_request.begin(app.state.lease.last_seen()),
            ask=ask,
            message=UNSAVED_CONFIRM,
        )
        assert order == []
        assert server.should_exit is False
        answers["yes"] = True
        decide_quit(
            dirty=True,
            holder=None,
            stop=app.state.idle.shutdown_now,
            begin_exit=lambda: None,
            ask=ask,
            message=UNSAVED_CONFIRM,
        )
    assert order == ["temp", "token", "tray", "server"]
    assert not session.temp_dir.exists()


def test_exit_falls_back_after_five_seconds_or_release_without_waiting_for_another_tab(monkeypatch, tmp_path):
    now = [0.0]
    presented = []
    order = []
    server = _Server()
    _session, app, _icon = _app(tmp_path, now, presented.append, order, server, monkeypatch)
    with TestClient(app) as client:
        claim_tab(client, app)
        source = tmp_path / "sample.pdf"
        write_pdf(source, pages=1)
        loaded = load_pdf(client, app, source)
        page_id = loaded["pages"][0]["id"]
        post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"})
        app.state.exit_request.begin(now[0])
        now[0] = 4
        beat = client.post("/api/editor/heartbeat", headers=auth(app), json={"tab_id": "tab-a"})
        assert beat.json()["exit_requested"] is True
        now[0] = 5.1
        app.state.exit_request.poll()
        assert app.state.exit_request.requested is True
        assert presented == []
        now[0] = 9.2
        app.state.exit_request.poll()
        assert app.state.exit_request.requested is False
        assert presented == [UNSAVED_CONFIRM]
        assert server.should_exit is False
        closed = client.post("/api/editor/heartbeat", headers=auth(app), json={"tab_id": "tab-a"})
        assert closed.json()["exit_requested"] is False
        assert closed.json()["holding"] is False

    presented.clear()
    now[0] = 20
    _session, app, _icon = _app(tmp_path, now, presented.append, order, server, monkeypatch)
    with TestClient(app) as client:
        claim_tab(client, app)
        source = tmp_path / "again.pdf"
        write_pdf(source, pages=1)
        loaded = load_pdf(client, app, source)
        page_id = loaded["pages"][0]["id"]
        post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"})
        app.state.exit_request.begin(now[0])
        released = release_tab(client, app, "tab-a")
        assert released.status_code == 200
        assert app.state.exit_request.requested is False
        assert presented == [UNSAVED_CONFIRM]
        assert server.should_exit is False


def test_message_box_runs_on_the_tray_thread_and_does_not_block_release(monkeypatch, tmp_path):
    now = [0.0]
    order = []
    server = _Server()
    seen = []

    def box(_hwnd, text):
        seen.append((text, threading.get_ident()))
        return False

    monkeypatch.setattr("app.tray.message_box", box)
    tray = Tray(lambda: None, lambda: None)
    tray.start(window=False)

    def present(text: str) -> None:
        tray.ask(text, on_yes=lambda: None, on_no=lambda: None)

    _session, app, _icon = _app(tmp_path, now, present, order, server, monkeypatch)
    with TestClient(app) as client:
        claim_tab(client, app)
        source = tmp_path / "sample.pdf"
        write_pdf(source, pages=1)
        loaded = load_pdf(client, app, source)
        page_id = loaded["pages"][0]["id"]
        post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"})
        app.state.exit_request.begin(now[0])
        released = release_tab(client, app, "tab-a")
        assert released.status_code == 200
        for _ in range(50):
            if seen:
                break
            threading.Event().wait(0.02)
        assert seen[0][0] == UNSAVED_CONFIRM
        assert seen[0][1] == tray.thread_ident
        assert seen[0][1] != threading.get_ident()
        assert server.should_exit is False
    tray.remove()


def test_remove_deletes_the_icon_before_it_returns(monkeypatch):
    deleted = []
    finished = threading.Event()
    tray = Tray(lambda: None, lambda: None)

    def delete(_self) -> None:
        assert not finished.is_set()
        threading.Event().wait(0.05)
        deleted.append(threading.get_ident())

    monkeypatch.setattr(Tray, "_delete_icon", delete)
    tray.start(window=False)
    tray.remove()
    finished.set()
    assert deleted == [tray.thread_ident]


def test_remove_on_the_tray_thread_returns_without_waiting_for_itself(monkeypatch):
    monkeypatch.setattr("app.tray.message_box", lambda _hwnd, _text: True)
    tray = Tray(lambda: None, lambda: None)
    calls = []

    def delete(_self) -> None:
        calls.append(threading.get_ident())

    monkeypatch.setattr(Tray, "_delete_icon", delete)

    def on_yes() -> None:
        tray.remove()
        calls.append("returned")

    tray.start(window=False)
    tray.ask("確認", on_yes, lambda: None)
    for _ in range(50):
        if "returned" in calls:
            break
        threading.Event().wait(0.02)
    assert calls == [tray.thread_ident, "returned"]


def test_clean_loss_during_exit_stops_without_confirm(monkeypatch, tmp_path):
    now = [0.0]
    presented = []
    order = []
    server = _Server()
    _session, app, _icon = _app(tmp_path, now, presented.append, order, server, monkeypatch)
    with TestClient(app) as client:
        claim_tab(client, app)
        source = tmp_path / "sample.pdf"
        write_pdf(source, pages=1)
        loaded = load_pdf(client, app, source)
        page_id = loaded["pages"][0]["id"]
        post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"})
        app.state.exit_request.begin(now[0])
        undone = post_command(client, app, {"type": "undo"})
        assert undone.json()["unsaved"] is False
        now[0] = 9
        app.state.exit_request.poll()
        assert presented == []
        assert app.state.exit_request.requested is False
        assert order == ["temp", "token", "tray", "server"]
        assert server.should_exit is True

    presented.clear()
    order.clear()
    now[0] = 20
    server = _Server()
    _session, app, _icon = _app(tmp_path, now, presented.append, order, server, monkeypatch)
    with TestClient(app) as client:
        claim_tab(client, app)
        source = tmp_path / "again.pdf"
        write_pdf(source, pages=1)
        loaded = load_pdf(client, app, source)
        page_id = loaded["pages"][0]["id"]
        post_command(client, app, {"type": "rotate", "page_ids": [page_id], "direction": "right"})
        app.state.exit_request.begin(now[0])
        undone = post_command(client, app, {"type": "undo"})
        assert undone.json()["unsaved"] is False
        beat = client.post("/api/editor/heartbeat", headers=auth(app), json={"tab_id": "tab-a"})
        assert beat.status_code == 200
        assert beat.json()["exit_requested"] is False
        assert presented == []
        assert order == ["temp", "token", "tray", "server"]
        assert server.should_exit is True


def test_menu_has_only_open_and_quit():
    calls = []
    tray = Tray(lambda: calls.append("open"), lambda: calls.append("quit"))
    assert [label for label, _command in menu_entries()] == [MENU_OPEN, MENU_QUIT]
    assert menu_entries() == (("開く", ID_OPEN), ("終了", ID_QUIT))
    tray.handle_command(ID_OPEN)
    tray.handle_command(ID_QUIT)
    tray.handle_command(99)
    assert calls == ["open", "quit"]


def test_frozen_dist_and_production_env(monkeypatch, tmp_path):
    monkeypatch.delenv("PDF_EDITOR_ENV", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    apply_frozen_environment()
    assert "PDF_EDITOR_ENV" not in os.environ
    dev = create_app(Session(temp_dir=tmp_path / "dev"), settings_path=tmp_path / "dev.json")
    assert dev.docs_url == "/docs"
    assert dist_dir() == Path(__file__).resolve().parent.parent / "web" / "dist"

    monkeypatch.delenv("PDF_EDITOR_ENV", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    meipass = tmp_path / "bundle"
    dist = meipass / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>__PDF_EDITOR_TOKEN__</html>", encoding="utf-8")
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    apply_frozen_environment()
    assert os.environ["PDF_EDITOR_ENV"] == "production"
    frozen = create_app(Session(temp_dir=tmp_path / "frozen"), settings_path=tmp_path / "frozen.json")
    assert frozen.docs_url is None
    assert dist_dir() == dist
    with TestClient(frozen) as client:
        body = client.get("/").text
        assert frozen.state.token in body
        assert "__PDF_EDITOR_TOKEN__" not in body


def test_spec_is_one_folder_without_autostart_or_onefile():
    text = Path("pdf-editor.spec").read_text(encoding="utf-8")
    assert "app/__main__.py" in text
    assert "('web/dist', 'web/dist')" in text
    assert "exclude_binaries=True" in text
    assert "COLLECT(" in text
    assert "onefile" not in text.lower()
    assert "Run" not in text.split("Run キー")[0]
    assert "npm run build" in text
    assert "自動起動は登録しない" in text


def test_ensure_port_free_message(monkeypatch, capsys):
    class Busy:
        def bind(self, _address):
            raise OSError("busy")

        def close(self):
            return None

    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: Busy())
    with pytest.raises(SystemExit) as caught:
        ensure_port_free()
    assert caught.value.code == 1
    assert capsys.readouterr().err.strip() == PORT_BUSY_MESSAGE


def test_finish_as_second_instance_hides_the_token(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    write_session_token("token-not-for-logs")
    monkeypatch.setattr("app.single_instance.signal_running_instance", lambda token: False)
    assert finish_as_second_instance() == 1
    assert capsys.readouterr().err.strip() == OPEN_FAILED_MESSAGE
    assert "token-not-for-logs" not in capsys.readouterr().err
