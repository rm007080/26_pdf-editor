import atexit
import os
import socket
import sys
import threading
import time

import uvicorn

from app.main import create_app, open_editor_page
from app.session import Session
from app.single_instance import (
    PORT_BUSY_MESSAGE,
    acquire_mutex,
    delete_session_token,
    finish_as_second_instance,
    write_session_token,
)
from app.tray import Tray, decide_quit, loss_confirm_message

HOST = "127.0.0.1"
PORT = 8765


def apply_frozen_environment() -> None:
    if getattr(sys, "frozen", False) and "PDF_EDITOR_ENV" not in os.environ:
        os.environ["PDF_EDITOR_ENV"] = "production"


def ensure_port_free() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, PORT))
    except OSError:
        print(PORT_BUSY_MESSAGE, file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        sock.close()


def open_browser_when_ready() -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.2):
                open_editor_page()
                return
        except OSError:
            time.sleep(0.1)


def register_exit_cleanup(session: Session) -> None:
    atexit.register(session.shutdown)


def main() -> None:
    apply_frozen_environment()
    if not acquire_mutex():
        raise SystemExit(finish_as_second_instance())
    ensure_port_free()
    session = Session()
    tray_holder: dict[str, Tray] = {}
    server_holder: dict[str, uvicorn.Server] = {}
    app_holder: dict[str, object] = {}

    def stop() -> None:
        delete_session_token()
        tray = tray_holder.get("tray")
        if tray is not None:
            tray.remove()
        server = server_holder.get("server")
        if server is not None:
            server.should_exit = True

    def present(text: str) -> None:
        tray = tray_holder.get("tray")
        app = app_holder.get("app")
        if tray is None or app is None:
            return
        tray.ask(text, on_yes=app.state.idle.shutdown_now, on_no=lambda: None)

    app = create_app(session, stop=stop, present_exit=present)
    app_holder["app"] = app
    write_session_token(app.state.token)

    def on_open() -> None:
        open_editor_page()

    def on_quit() -> None:
        snapshot = session.snapshot()
        unsaved = bool(snapshot["unsaved"])
        split_unexported = bool(snapshot["split_unexported"])
        decide_quit(
            dirty=unsaved or split_unexported,
            holder=app.state.lease.holder(),
            stop=app.state.idle.shutdown_now,
            begin_exit=lambda: app.state.exit_request.begin(app.state.lease.last_seen()),
            ask=lambda message: tray_holder["tray"].ask(
                message,
                on_yes=app.state.idle.shutdown_now,
                on_no=lambda: None,
            ),
            message=loss_confirm_message(unsaved, split_unexported),
        )

    tray = Tray(on_open, on_quit)
    tray_holder["tray"] = tray
    register_exit_cleanup(session)
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT))
    server_holder["server"] = server
    tray.start()
    threading.Thread(target=open_browser_when_ready, daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
