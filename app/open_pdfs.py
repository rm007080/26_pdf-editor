import asyncio
import time
from pathlib import Path

from app.folders import initial_directory, with_notice
from app.save_dialog import cancel_open_dialog
from app.session import Activity, Session
from app.settings import SettingsStore

OPEN_FAILURE = "ファイル選択を完了できませんでした"
BUSY_DETAIL = "読み込み中、ダイアログ中、または書き出し中のため受け付けられません"


async def _wait_until_idle(session: Session) -> bool:
    deadline = time.monotonic() + 5
    while session.current_activity() is not Activity.IDLE:
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def open_selected_pdfs(session: Session, settings: SettingsStore, ask) -> tuple[int, dict]:
    # 再読み込み後の「PDF を選ぶ」は、裏に残った選択を閉じ、この要求で開き直す。
    if cancel_open_dialog():
        if not await _wait_until_idle(session):
            return 409, {"detail": BUSY_DETAIL}
    if not session.try_begin(Activity.DIALOG):
        return 409, {"detail": BUSY_DETAIL}
    received: list[str] = []
    try:
        initial, notice = initial_directory(settings.read()["last_open_dir"])
        try:
            chosen = await asyncio.to_thread(ask, initial)
        except Exception:
            return 500, {"detail": OPEN_FAILURE}
        if not chosen:
            return 200, with_notice({"status": "cancelled"}, notice)
        items: list[dict] = []
        folder = Path(chosen[0]).parent
        try:
            for path in chosen:
                file_path = Path(path)
                data = file_path.read_bytes()
                status, payload = session.receive_bytes(
                    data,
                    file_path.name,
                    hold=Activity.DIALOG,
                    exclude_ids=set(received),
                )
                if status == 200 and isinstance(payload, dict) and "document_id" in payload:
                    received.append(str(payload["document_id"]))
                    items.append({**payload, "filename": file_path.name})
                else:
                    detail = payload.get("detail") if isinstance(payload, dict) else OPEN_FAILURE
                    items.append({"filename": file_path.name, "detail": str(detail)})
            settings.remember_directory("last_open_dir", folder)
        except Exception:
            if received:
                session.discard({"document_ids": list(received)})
            return 500, {"detail": OPEN_FAILURE}
        return 200, with_notice({"status": "ok", "items": items}, notice)
    finally:
        session.finish(Activity.DIALOG)
