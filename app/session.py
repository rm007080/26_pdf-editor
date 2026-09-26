import shutil
import tempfile
import threading
from enum import Enum
from pathlib import Path
from uuid import uuid4

import pikepdf


def default_app_temp_dir() -> Path:
    return Path(tempfile.gettempdir()) / "pdf-editor"


class LoadError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class Activity(Enum):
    IDLE = "idle"
    LOADING = "loading"
    DIALOG = "dialog"
    EXPORTING = "exporting"


class Session:
    def __init__(self, temp_dir: Path | None = None) -> None:
        self.temp_dir = temp_dir or default_app_temp_dir()
        self._lock = threading.Lock()
        self._activity = Activity.IDLE
        self.documents: list[dict[str, str]] = []
        self.pages: list[dict[str, object]] = []
        self.selected_page_ids: list[str] = []
        self.active_page_id: str | None = None
        self.session_file: Path | None = None
        self.pause_during_load = None
        self.before_commit = None

    def startup(self) -> None:
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
        self.temp_dir.mkdir(parents=True)
        with self._lock:
            self.documents = []
            self.pages = []
            self.selected_page_ids = []
            self.active_page_id = None
            self.session_file = None
            self._activity = Activity.IDLE

    def shutdown(self) -> None:
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def try_begin(self, activity: Activity) -> bool:
        with self._lock:
            if self._activity is not Activity.IDLE:
                return False
            self._activity = activity
            return True

    def finish(self, activity: Activity) -> None:
        with self._lock:
            if self._activity is activity:
                self._activity = Activity.IDLE

    def transition(self, current: Activity, new: Activity) -> bool:
        with self._lock:
            if self._activity is not current:
                return False
            self._activity = new
            return True

    def mark_dialog_before_commit_for_test(self) -> None:
        # 読み込み確定の直前にダイアログへ入った並びを、テストが再現する。
        with self._lock:
            self._activity = Activity.DIALOG

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return self._snapshot_unlocked()

    def update_selection(self, body: object) -> tuple[int, dict]:
        with self._lock:
            if self._activity is not Activity.IDLE:
                return 409, {"detail": "読み込み中、ダイアログ中、または書き出し中のため選択を変更できません"}
            if not isinstance(body, dict) or "selected_page_ids" not in body or "active_page_id" not in body:
                return 400, {"detail": "選択の指定が不正です"}
            detail = self._selection_error(body["selected_page_ids"], body["active_page_id"])
            if detail is not None:
                return 400, {"detail": detail}
            self.selected_page_ids = self._sorted_ids(body["selected_page_ids"])
            self.active_page_id = body["active_page_id"]
            return 200, self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict[str, object]:
        return {
            "documents": [dict(item) for item in self.documents],
            "pages": [dict(item) for item in self.pages],
            "selected_page_ids": self._sorted_ids(self.selected_page_ids),
            "active_page_id": self.active_page_id,
        }

    def _visible_pages_unlocked(self) -> list[dict[str, object]]:
        pages = [page for page in self.pages if not page["deleted"]]
        pages.sort(key=lambda page: page["order"])
        return pages

    def _sorted_ids(self, page_ids: list[str]) -> list[str]:
        order = {page["id"]: index for index, page in enumerate(self._visible_pages_unlocked())}
        return sorted(page_ids, key=lambda page_id: order.get(page_id, len(order)))

    def _selection_error(self, selected: object, active: object) -> str | None:
        if not isinstance(selected, list) or any(not isinstance(page_id, str) for page_id in selected):
            return "選択の指定が不正です"
        if active is not None and not isinstance(active, str):
            return "選択の指定が不正です"
        if len(selected) != len(set(selected)):
            return "ページの識別子が重複しています"
        known = {page["id"] for page in self.pages}
        visible = {page["id"] for page in self._visible_pages_unlocked()}
        for page_id in selected:
            if page_id not in known:
                return "不明なページは選択できません"
            if page_id not in visible:
                return "見えていないページは選択できません"
        if not isinstance(active, str) or active not in selected:
            if selected or active is not None:
                return "アクティブページが選択に含まれていません"
        return None

    def session_file_path(self) -> Path | None:
        with self._lock:
            path = self.session_file
        if path is not None and path.is_file():
            return path
        return None

    def accept_upload(self, data: bytes, filename: str) -> tuple[int, dict]:
        if not self.try_begin(Activity.LOADING):
            return 409, {"detail": "読み込み中、または書き出し中のため受け付けられません"}
        staging: Path | None = None
        try:
            if self.pause_during_load is not None:
                self.pause_during_load()
            staging, page_count = self._stage(data, filename)
            document, pages = self._records(Path(filename).name, page_count)
            if not self._commit(document, pages, staging):
                staging.unlink(missing_ok=True)
                return 409, {"detail": "書き出し中のため、読み込みを確定できませんでした"}
            staging = None
            return 200, self.snapshot()
        except LoadError as exc:
            if staging is not None:
                staging.unlink(missing_ok=True)
            return 400, {"detail": str(exc)}
        finally:
            self.finish(Activity.LOADING)

    def _commit(self, document: dict[str, str], pages: list[dict[str, object]], staging: Path) -> bool:
        if self.before_commit is not None:
            self.before_commit()
        with self._lock:
            if self._activity is not Activity.LOADING:
                return False
            previous = self.session_file
            self.documents = [document]
            self.pages = pages
            visible = [page for page in pages if not page["deleted"]]
            visible.sort(key=lambda page: page["order"])
            if visible:
                self.selected_page_ids = [str(visible[0]["id"])]
                self.active_page_id = str(visible[0]["id"])
            else:
                self.selected_page_ids = []
                self.active_page_id = None
            self.session_file = staging
            self._activity = Activity.IDLE
        if previous is not None and previous != staging and previous.exists():
            previous.unlink()
        return True

    def _stage(self, data: bytes, filename: str) -> tuple[Path, int]:
        safe_name = Path(filename).name
        if not safe_name.lower().endswith(".pdf"):
            raise LoadError("PDF 以外のファイルは読み込めません")
        staging = self.temp_dir / f"incoming-{uuid4().hex}.pdf"
        staging.write_bytes(data)
        try:
            with pikepdf.open(staging) as pdf:
                return staging, len(pdf.pages)
        except pikepdf.PasswordError as exc:
            staging.unlink(missing_ok=True)
            raise LoadError("パスワードが必要な PDF は、この操作では開けません") from exc
        except pikepdf.PdfError as exc:
            staging.unlink(missing_ok=True)
            raise LoadError("PDF を開けませんでした") from exc
        except Exception:
            staging.unlink(missing_ok=True)
            raise

    def _records(self, filename: str, page_count: int) -> tuple[dict[str, str], list[dict[str, object]]]:
        document_id = uuid4().hex
        document = {"id": document_id, "filename": filename}
        pages = [
            {
                "id": uuid4().hex,
                "document_id": document_id,
                "source_page": number,
                "order": number,
                "deleted": False,
                "rotation": 0,
            }
            for number in range(1, page_count + 1)
        ]
        return document, pages
