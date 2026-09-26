import atexit
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from app.main import create_app
from app.session import Session

HOST = "127.0.0.1"
PORT = 8765


def ensure_port_free() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, PORT))
    except OSError:
        print(f"ポート {PORT} を使用できないため終了します。", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        sock.close()


def open_browser_when_ready() -> None:
    url = f"http://{HOST}:{PORT}/"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.2):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.1)


def register_exit_cleanup(session: Session) -> None:
    atexit.register(session.shutdown)


def main() -> None:
    ensure_port_free()
    session = Session()
    app = create_app(session)
    register_exit_cleanup(session)
    threading.Thread(target=open_browser_when_ready, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
