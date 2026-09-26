import shutil
import tempfile
import threading
from enum import Enum
from pathlib import Path
from uuid import uuid4

import pikepdf


def default_app_temp_dir() -> Path:
    return Path(tempfile.gettempdir()) / "pdf-editor"


UNDO_LIMIT = 100
EMPTY_UNDO_HINT = "元に戻せる操作がありません。実行しても状態は変わりません"
EMPTY_REDO_HINT = "やり直せる操作がありません。実行しても状態は変わりません"
CLIENT_COMMANDS = {"rotate", "delete", "reorder", "undo", "redo"}


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
        self._undo: list[dict[str, object]] = []
        self._redo: list[dict[str, object]] = []
        self._history_drop_notified = False
        self.session_file: Path | None = None
        self.pause_during_load = None
        self.before_commit = None
        self._assign_selection([], None)

    def startup(self) -> None:
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
        self.temp_dir.mkdir(parents=True)
        with self._lock:
            self.documents = []
            self.pages = []
            self._assign_selection([], None)
            self._undo = []
            self._redo = []
            self._history_drop_notified = False
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
            self._assign_selection(self._sorted_ids(body["selected_page_ids"]), body["active_page_id"])
            return 200, self._snapshot_unlocked()

    def apply_command(self, body: object) -> tuple[int, dict]:
        with self._lock:
            if self._activity is not Activity.IDLE:
                return 409, {"detail": "読み込み中、ダイアログ中、または書き出し中のため編集できません"}
            if not isinstance(body, dict) or not isinstance(body.get("type"), str):
                return 400, {"detail": "コマンドの指定が不正です"}
            kind = body["type"]
            if kind not in CLIENT_COMMANDS:
                return 400, {"detail": "不明な操作です"}
            if kind == "undo":
                return self._undo_unlocked()
            if kind == "redo":
                return self._redo_unlocked()
            detail = self._user_command_error(body)
            if detail is not None:
                return 400, {"detail": detail}
            return self._apply_user_unlocked(body)

    def _assign_selection(self, selected_page_ids: list[str], active_page_id: str | None) -> None:
        self.selected_page_ids = list(selected_page_ids)
        self.active_page_id = active_page_id

    def _snapshot_unlocked(self) -> dict[str, object]:
        return {
            "documents": [dict(item) for item in self.documents],
            "pages": [dict(item) for item in self.pages],
            "selected_page_ids": self._sorted_ids(self.selected_page_ids),
            "active_page_id": self.active_page_id,
            **self._history_fields(),
        }

    def _history_fields(self) -> dict[str, object]:
        if self._undo:
            undo_enabled = True
            undo_hint = _operation_phrase(self._undo[-1]["inverse"])
        else:
            undo_enabled = False
            undo_hint = EMPTY_UNDO_HINT
        if self._redo:
            redo_enabled = True
            redo_hint = _operation_phrase(self._redo[-1]["original"])
        else:
            redo_enabled = False
            redo_hint = EMPTY_REDO_HINT
        return {
            "undo_enabled": undo_enabled,
            "redo_enabled": redo_enabled,
            "undo_hint": undo_hint,
            "redo_hint": redo_hint,
        }

    def _command_response(self, changed: bool, history_dropped: bool) -> dict[str, object]:
        body = self._snapshot_unlocked()
        body["changed"] = changed
        body["history_dropped"] = history_dropped
        return body

    def _visible_pages_unlocked(self) -> list[dict[str, object]]:
        pages = [page for page in self.pages if not page["deleted"]]
        pages.sort(key=lambda page: page["order"])
        return pages

    def _visible_ids(self) -> list[str]:
        return [str(page["id"]) for page in self._visible_pages_unlocked()]

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

    def _user_command_error(self, body: dict) -> str | None:
        page_ids = body.get("page_ids")
        detail = self._page_ids_error(page_ids)
        if detail is not None:
            return detail
        assert isinstance(page_ids, list)
        if body["type"] == "rotate":
            if body.get("direction") not in {"left", "right"}:
                return "回転方向の指定が不正です"
            return None
        if body["type"] == "delete":
            return None
        return self._reorder_error(body, page_ids)

    def _page_ids_error(self, page_ids: object) -> str | None:
        if not isinstance(page_ids, list) or not page_ids or any(not isinstance(page_id, str) for page_id in page_ids):
            return "ページの指定が不正です"
        if len(page_ids) != len(set(page_ids)):
            return "ページの識別子が重複しています"
        known = {str(page["id"]): page for page in self.pages}
        for page_id in page_ids:
            page = known.get(page_id)
            if page is None:
                return "不明なページは操作できません"
            if page["deleted"]:
                return "見えていないページは操作できません"
        return None

    def _reorder_error(self, body: dict, page_ids: list[str]) -> str | None:
        has_before = body.get("before_page_id") is not None
        has_after = body.get("after_page_id") is not None
        if has_before == has_after:
            return "移動先の指定が不正です"
        anchor = body["before_page_id"] if has_before else body["after_page_id"]
        if not isinstance(anchor, str):
            return "移動先の指定が不正です"
        known = {str(page["id"]): page for page in self.pages}
        page = known.get(anchor)
        if page is None:
            return "不明なページは操作できません"
        if page["deleted"]:
            return "見えていないページは操作できません"
        if anchor in page_ids:
            return "移動先が対象ページに含まれています"
        selected = set(self.selected_page_ids)
        moving = set(page_ids)
        if moving == selected:
            return None
        if len(page_ids) == 1 and page_ids[0] not in selected:
            return None
        return "そのページの組み合わせは並べ替えられません"

    def _apply_user_unlocked(self, body: dict) -> tuple[int, dict]:
        ordered = self._visible_order(body["page_ids"])
        normalized = self._normalize(body, ordered)
        before_selected = list(self.selected_page_ids)
        before_active = self.active_page_id
        inverse = self._inverse(normalized, ordered)
        if body["type"] == "delete":
            after_selected, after_active = self._selection_after_delete(ordered, self._visible_ids())
        elif body["type"] == "reorder":
            after_selected, after_active = self._selection_after_reorder(ordered)
        else:
            after_selected, after_active = list(before_selected), before_active
        if not self._mutate(normalized):
            return 200, self._command_response(False, False)
        self._assign_selection(self._sorted_ids(after_selected), after_active)
        self._redo = []
        record = {
            "inverse": inverse,
            "original": normalized,
            "before_selected": before_selected,
            "before_active": before_active,
            "after_selected": list(self.selected_page_ids),
            "after_active": self.active_page_id,
        }
        dropped = self._push_undo(record)
        return 200, self._command_response(True, dropped)

    def _undo_unlocked(self) -> tuple[int, dict]:
        if not self._undo:
            return 400, {"detail": "元に戻せる操作がありません"}
        record = self._undo.pop()
        self._mutate(record["inverse"])
        self._assign_selection(list(record["before_selected"]), record["before_active"])
        self._redo.append(record)
        return 200, self._command_response(True, False)

    def _redo_unlocked(self) -> tuple[int, dict]:
        if not self._redo:
            return 400, {"detail": "やり直せる操作がありません"}
        record = self._redo.pop()
        self._mutate(record["original"])
        self._assign_selection(list(record["after_selected"]), record["after_active"])
        dropped = self._push_undo(record)
        return 200, self._command_response(True, dropped)

    def _push_undo(self, record: dict[str, object]) -> bool:
        self._undo.append(record)
        if len(self._undo) <= UNDO_LIMIT:
            return False
        del self._undo[0]
        if self._history_drop_notified:
            return False
        self._history_drop_notified = True
        return True

    def _visible_order(self, page_ids: list[str]) -> list[str]:
        wanted = set(page_ids)
        return [page_id for page_id in self._visible_ids() if page_id in wanted]

    def _normalize(self, body: dict, ordered: list[str]) -> dict[str, object]:
        if body["type"] == "rotate":
            return {"type": "rotate", "page_ids": ordered, "direction": body["direction"]}
        if body["type"] == "delete":
            return {"type": "delete", "page_ids": ordered}
        command: dict[str, object] = {"type": "reorder", "page_ids": ordered}
        if body.get("before_page_id") is not None:
            command["before_page_id"] = body["before_page_id"]
        else:
            command["after_page_id"] = body["after_page_id"]
        return command

    def _inverse(self, command: dict[str, object], ordered: list[str]) -> dict[str, object]:
        if command["type"] == "rotate":
            direction = "left" if command["direction"] == "right" else "right"
            return {"type": "rotate", "page_ids": list(ordered), "direction": direction}
        # 各ページは、操作前に一つ前だったページの直後へ戻る。先頭だったページの一つ前は無い。
        visible = self._visible_ids()
        places = []
        for page_id in ordered:
            index = visible.index(page_id)
            places.append(
                {
                    "page_id": page_id,
                    "after_page_id": None if index == 0 else visible[index - 1],
                }
            )
        kind = "restore_deleted" if command["type"] == "delete" else "restore_order"
        return {"type": kind, "pages": places}

    def _selection_after_delete(
        self, deleted_ids: list[str], before_visible: list[str]
    ) -> tuple[list[str], str | None]:
        deleted = set(deleted_ids)
        remaining = [page_id for page_id in self.selected_page_ids if page_id not in deleted]
        if remaining:
            active = self.active_page_id
            if active in remaining:
                return remaining, active
            if isinstance(active, str) and active in deleted:
                active_index = before_visible.index(active)
                later = [page_id for page_id in remaining if before_visible.index(page_id) > active_index]
                if later:
                    return remaining, later[0]
                earlier = [page_id for page_id in remaining if before_visible.index(page_id) < active_index]
                if earlier:
                    return remaining, earlier[-1]
            return remaining, remaining[0]
        survivors = [page_id for page_id in before_visible if page_id not in deleted]
        if not survivors:
            return [], None
        first_deleted = min(before_visible.index(page_id) for page_id in deleted_ids)
        for page_id in survivors:
            if before_visible.index(page_id) > first_deleted:
                return [page_id], page_id
        return [survivors[-1]], survivors[-1]

    def _selection_after_reorder(self, page_ids: list[str]) -> tuple[list[str], str | None]:
        if set(page_ids) == set(self.selected_page_ids):
            return list(self.selected_page_ids), self.active_page_id
        return [page_ids[0]], page_ids[0]

    def _mutate(self, command: dict[str, object]) -> bool:
        kind = command["type"]
        if kind == "rotate":
            delta = 90 if command["direction"] == "right" else -90
            targets = set(command["page_ids"])
            for page in self.pages:
                if page["id"] in targets:
                    page["rotation"] = (int(page["rotation"]) + delta) % 360
            return True
        if kind == "delete":
            targets = set(command["page_ids"])
            for page in self.pages:
                if page["id"] in targets:
                    page["deleted"] = True
            self._apply_sequence(self._visible_ids())
            return True
        if kind == "reorder":
            return self._reorder(command)
        self._reinsert(command["pages"])
        return True

    def _reorder(self, command: dict[str, object]) -> bool:
        moving = list(command["page_ids"])
        moving_set = set(moving)
        stationary = [page_id for page_id in self._visible_ids() if page_id not in moving_set]
        if "before_page_id" in command:
            index = stationary.index(command["before_page_id"])
            inserted = stationary[:index] + moving + stationary[index:]
        else:
            index = stationary.index(command["after_page_id"])
            inserted = stationary[: index + 1] + moving + stationary[index + 1 :]
        if inserted == self._visible_ids():
            return False
        self._apply_sequence(inserted)
        return True

    def _reinsert(self, places: list[dict[str, object]]) -> None:
        moving = [str(place["page_id"]) for place in places]
        moving_set = set(moving)
        for page in self.pages:
            if page["id"] in moving_set:
                page["deleted"] = False
        current = [page_id for page_id in self._visible_ids() if page_id not in moving_set]
        for place in places:
            page_id = str(place["page_id"])
            after_id = place["after_page_id"]
            if after_id is None:
                current.insert(0, page_id)
            else:
                current.insert(current.index(after_id) + 1, page_id)
        self._apply_sequence(current)

    def _apply_sequence(self, visible_ids: list[str]) -> None:
        wanted = set(visible_ids)
        by_id = {page["id"]: page for page in self.pages}
        for page in self.pages:
            if page["id"] not in wanted:
                page["deleted"] = True
                page["order"] = 0
        for index, page_id in enumerate(visible_ids, start=1):
            page = by_id[page_id]
            page["deleted"] = False
            page["order"] = index

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
                self._assign_selection([str(visible[0]["id"])], str(visible[0]["id"]))
            else:
                self._assign_selection([], None)
            self._undo = []
            self._redo = []
            self._history_drop_notified = False
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


def _operation_phrase(command: object) -> str:
    if not isinstance(command, dict):
        return "操作する"
    kind = command.get("type")
    if kind == "rotate":
        side = "左" if command.get("direction") == "left" else "右"
        return f"{side}に90度回転する"
    if kind == "delete":
        return "ページを削除する"
    if kind == "restore_deleted":
        return "削除したページを戻す"
    if kind == "reorder":
        return "ページを並べ替える"
    if kind == "restore_order":
        return "並べ替えを元に戻す"
    return "操作する"
