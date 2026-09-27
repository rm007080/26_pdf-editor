import hashlib
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
CLIENT_COMMANDS = {
    "rotate",
    "delete",
    "reorder",
    "append_documents",
    "set_split",
    "clear_split",
    "clear_all_splits",
    "undo",
    "redo",
}


class LoadError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class Activity(Enum):
    IDLE = "idle"
    LOADING = "loading"
    DIALOG = "dialog"
    EXPORTING = "exporting"
    RESETTING = "resetting"


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
        self._intakes: dict[str, dict[str, object]] = {}
        self._copies: dict[str, Path] = {}
        self._hashes: dict[str, str] = {}
        self._catalog: dict[str, str] = {}
        self._baseline: list[tuple[str, int, int]] = []
        self._split_positions: list[int] = []
        self._split_exported: list[frozenset[str]] | None = None
        self.pause_during_load = None
        self.pause_during_reset = None
        self.before_commit = None
        self.fail_decrypt_save = None
        self.activity_listener = None
        self.cleared_leftover = False
        self._assign_selection([], None)

    def startup(self) -> None:
        self.cleared_leftover = False
        if self.temp_dir.exists() and any(self.temp_dir.iterdir()):
            shutil.rmtree(self.temp_dir)
            self.cleared_leftover = True
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._clear_workspace_unlocked()
            self._activity = Activity.IDLE

    def reset(self) -> tuple[int, dict]:
        with self._lock:
            if self._activity is not Activity.IDLE:
                return 409, {"detail": "読み込み中、ダイアログ中、または書き出し中のため新しく始められません"}
            self._activity = Activity.RESETTING
        try:
            if self.pause_during_reset is not None:
                self.pause_during_reset()
            if self.temp_dir.exists():
                shutil.rmtree(self.temp_dir)
            self.temp_dir.mkdir(parents=True)
        except Exception:
            with self._lock:
                if self._activity is Activity.RESETTING:
                    self._activity = Activity.IDLE
            raise
        with self._lock:
            self._clear_workspace_unlocked()
            self._activity = Activity.IDLE
            return 200, self._snapshot_unlocked()

    def _clear_workspace_unlocked(self) -> None:
        self.documents = []
        self.pages = []
        self._assign_selection([], None)
        self._undo = []
        self._redo = []
        self._history_drop_notified = False
        self._intakes = {}
        self._copies = {}
        self._hashes = {}
        self._catalog = {}
        self._baseline = []
        self._split_positions = []
        self._split_exported = None

    def shutdown(self) -> None:
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def current_activity(self) -> Activity:
        with self._lock:
            return self._activity

    def try_begin(self, activity: Activity) -> bool:
        with self._lock:
            if self._activity is not Activity.IDLE:
                return False
            previous = self._activity
            self._activity = activity
        self._notify_activity(previous, activity)
        return True

    def finish(self, activity: Activity) -> None:
        with self._lock:
            if self._activity is not activity:
                return
            previous = self._activity
            self._activity = Activity.IDLE
        self._notify_activity(previous, Activity.IDLE)

    def transition(self, current: Activity, new: Activity) -> bool:
        with self._lock:
            if self._activity is not current:
                return False
            previous = self._activity
            self._activity = new
        self._notify_activity(previous, new)
        return True

    def mark_dialog_before_commit_for_test(self) -> None:
        # 読み込み確定の直前にダイアログへ入った並びを、テストが再現する。
        with self._lock:
            previous = self._activity
            self._activity = Activity.DIALOG
        self._notify_activity(previous, Activity.DIALOG)

    def _notify_activity(self, previous: Activity, new: Activity) -> None:
        listener = self.activity_listener
        if listener is not None and previous is not new:
            listener(previous, new)

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
            if kind == "append_documents":
                detail = self._append_error(body)
                if detail is not None:
                    return 400, {"detail": detail}
                return self._apply_append_unlocked(body)
            if kind in {"set_split", "clear_split", "clear_all_splits"}:
                detail = self._split_command_error(body)
                if detail is not None:
                    return 400, {"detail": detail}
                return self._apply_split_unlocked(body)
            detail = self._user_command_error(body)
            if detail is not None:
                return 400, {"detail": detail}
            return self._apply_user_unlocked(body)

    def _assign_selection(self, selected_page_ids: list[str], active_page_id: str | None) -> None:
        self.selected_page_ids = list(selected_page_ids)
        self.active_page_id = active_page_id

    def _snapshot_unlocked(self) -> dict[str, object]:
        workspace = self._workspace_ids()
        return {
            "documents": [dict(item) for item in self.documents],
            "pages": [dict(item) for item in self.pages if item["document_id"] in workspace],
            "selected_page_ids": self._sorted_ids(self.selected_page_ids),
            "active_page_id": self.active_page_id,
            "unsaved": self._output_state() != self._baseline,
            "split_positions": list(self._split_positions),
            "split_unexported": self._split_unexported_unlocked(),
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

    def _workspace_ids(self) -> set[str]:
        return {document["id"] for document in self.documents}

    def _output_state(self) -> list[tuple[str, int, int]]:
        return [
            (str(page["document_id"]), int(page["source_page"]), int(page["rotation"]))
            for page in self._visible_pages_unlocked()
        ]

    def _visible_pages_unlocked(self) -> list[dict[str, object]]:
        workspace = self._workspace_ids()
        pages = [
            page
            for page in self.pages
            if page["document_id"] in workspace and not page["deleted"]
        ]
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

    def _append_error(self, body: dict) -> str | None:
        document_ids = body.get("document_ids")
        if (
            not isinstance(document_ids, list)
            or not document_ids
            or any(not isinstance(document_id, str) for document_id in document_ids)
        ):
            return "追加する PDF の指定が不正です"
        if len(document_ids) != len(set(document_ids)):
            return "追加単位の識別子が重複しています"
        workspace = self._workspace_ids()
        for document_id in document_ids:
            if document_id in workspace:
                return "すでに作業領域にある PDF は追加できません"
            intake = self._intakes.get(document_id)
            if intake is not None and intake["needs_password"]:
                return "パスワードの確認が終わっていない PDF は追加できません"
            if document_id not in self._intakes and document_id not in self._catalog:
                return "受け取っていない PDF は追加できません"
        return None

    def _split_command_error(self, body: dict) -> str | None:
        kind = body["type"]
        if kind == "clear_all_splits":
            if set(body) != {"type"}:
                return "分割線の指定が不正です"
            return None
        if kind == "set_split" and ("page_ids" in body or "after_page_ids" in body):
            return "分割線の指定が不正です"
        if set(body) != {"type", "after_page_id"}:
            return "分割線の指定が不正です"
        after_page_id = body.get("after_page_id")
        if not isinstance(after_page_id, str):
            return "分割線の指定が不正です"
        return self._split_anchor_error(after_page_id)

    def _split_anchor_error(self, page_id: str) -> str | None:
        known = {str(page["id"]) for page in self.pages}
        if page_id not in known:
            return "不明なページは操作できません"
        visible = self._visible_ids()
        if page_id not in visible:
            return "見えていないページは操作できません"
        if page_id == visible[-1]:
            return "末尾のページの後ろには分割線を入れられません"
        return None

    def _apply_split_unlocked(self, body: dict) -> tuple[int, dict]:
        normalized = self._normalize(body, [])
        before_selected = list(self.selected_page_ids)
        before_active = self.active_page_id
        inverse = self._inverse(normalized, [])
        if not self._mutate(normalized):
            return 200, self._command_response(False, False)
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

    def _apply_append_unlocked(self, body: dict) -> tuple[int, dict]:
        document_ids = list(body["document_ids"])
        was_empty = len(self.documents) == 0
        before_selected = list(self.selected_page_ids)
        before_active = self.active_page_id
        normalized = {"type": "append_documents", "document_ids": document_ids}
        if not self._mutate(normalized):
            return 200, self._command_response(False, False)
        if was_empty:
            visible = self._visible_ids()
            if visible:
                after_selected: list[str] = [visible[0]]
                after_active: str | None = visible[0]
            else:
                after_selected = []
                after_active = None
        else:
            after_selected = list(before_selected)
            after_active = before_active
        self._assign_selection(self._sorted_ids(after_selected), after_active)
        self._redo = []
        record = {
            "inverse": {"type": "remove_documents", "document_ids": list(document_ids)},
            "original": normalized,
            "before_selected": before_selected,
            "before_active": before_active,
            "after_selected": list(self.selected_page_ids),
            "after_active": self.active_page_id,
        }
        dropped = self._push_undo(record)
        return 200, self._command_response(True, dropped)

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
        inverse = record["inverse"]
        if isinstance(inverse, dict) and inverse.get("type") == "remove_documents":
            record["split_positions"] = list(self._split_positions)
        self._mutate(inverse)
        self._assign_selection(list(record["before_selected"]), record["before_active"])
        self._redo.append(record)
        return 200, self._command_response(True, False)

    def _redo_unlocked(self) -> tuple[int, dict]:
        if not self._redo:
            return 400, {"detail": "やり直せる操作がありません"}
        record = self._redo.pop()
        self._mutate(record["original"])
        original = record["original"]
        if (
            isinstance(original, dict)
            and original.get("type") == "append_documents"
            and "split_positions" in record
        ):
            stored = record["split_positions"]
            self._split_positions = self._normalize_splits([int(item) for item in stored])
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
        if body["type"] == "set_split":
            return {"type": "set_split", "after_page_id": body["after_page_id"]}
        if body["type"] == "clear_split":
            return {"type": "clear_split", "after_page_id": body["after_page_id"]}
        if body["type"] == "clear_all_splits":
            return {"type": "clear_all_splits"}
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
        if command["type"] == "set_split" and "after_page_ids" not in command:
            return {"type": "clear_split", "after_page_id": command["after_page_id"]}
        if command["type"] == "clear_split":
            return {"type": "set_split", "after_page_id": command["after_page_id"]}
        if command["type"] == "clear_all_splits":
            visible = self._visible_ids()
            return {
                "type": "set_split",
                "after_page_ids": [visible[position - 1] for position in self._split_positions],
            }
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
        if command["type"] == "delete":
            return {
                "type": "restore_deleted",
                "pages": places,
                "split_positions": list(self._split_positions),
            }
        return {"type": "restore_order", "pages": places}

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
        if kind == "set_split":
            return self._mutate_set_split(command)
        if kind == "clear_split":
            return self._mutate_clear_split(command)
        if kind == "clear_all_splits":
            if not self._split_positions:
                return False
            self._split_positions = []
            return True
        if kind == "rotate":
            delta = 90 if command["direction"] == "right" else -90
            targets = set(command["page_ids"])
            for page in self.pages:
                if page["id"] in targets:
                    page["rotation"] = (int(page["rotation"]) + delta) % 360
            return True
        if kind == "delete":
            visible = self._visible_ids()
            removed_positions = [visible.index(str(page_id)) + 1 for page_id in command["page_ids"]]
            targets = set(command["page_ids"])
            for page in self.pages:
                if page["id"] in targets:
                    page["deleted"] = True
            self._compact_split_positions(removed_positions)
            self._apply_sequence(self._visible_ids())
            return True
        if kind == "reorder":
            return self._reorder(command)
        if kind == "append_documents":
            return self._place_documents(list(command["document_ids"]))
        if kind == "remove_documents":
            visible = self._visible_pages_unlocked()
            removing = {str(document_id) for document_id in command["document_ids"]}
            removed_positions = [
                index
                for index, page in enumerate(visible, start=1)
                if str(page["document_id"]) in removing
            ]
            self._detach_documents(list(command["document_ids"]))
            self._compact_split_positions(removed_positions)
            return True
        if kind == "restore_deleted":
            self._reinsert(command["pages"])
            stored = command["split_positions"]
            self._split_positions = self._normalize_splits([int(item) for item in stored])
            return True
        self._reinsert(command["pages"])
        return True

    def _mutate_set_split(self, command: dict[str, object]) -> bool:
        visible = self._visible_ids()
        if "after_page_ids" in command:
            index = {page_id: position for position, page_id in enumerate(visible, start=1)}
            positions = [index[str(page_id)] for page_id in command["after_page_ids"] if str(page_id) in index]
            normalized = self._normalize_splits(positions)
            if normalized == self._split_positions:
                return False
            self._split_positions = normalized
            return True
        position = visible.index(str(command["after_page_id"])) + 1
        if position in self._split_positions:
            return False
        self._split_positions = self._normalize_splits([*self._split_positions, position])
        return True

    def _mutate_clear_split(self, command: dict[str, object]) -> bool:
        visible = self._visible_ids()
        position = visible.index(str(command["after_page_id"])) + 1
        if position not in self._split_positions:
            return False
        self._split_positions = [item for item in self._split_positions if item != position]
        return True

    def _compact_split_positions(self, removed_positions: list[int]) -> None:
        removed = sorted(removed_positions)
        shifted: list[int] = []
        for position in self._split_positions:
            shift = sum(1 for item in removed if item <= position)
            shifted.append(position - shift)
        self._split_positions = self._normalize_splits(shifted)

    def _normalize_splits(self, positions: list[int]) -> list[int]:
        count = len(self._visible_ids())
        kept = {
            position
            for position in positions
            if isinstance(position, int) and not isinstance(position, bool) and 1 <= position <= count - 1
        }
        return sorted(kept)

    def _groups_unlocked(self) -> list[list[dict[str, object]]]:
        visible = self._visible_pages_unlocked()
        if not visible:
            return []
        bounds = [0, *self._split_positions, len(visible)]
        return [visible[start:end] for start, end in zip(bounds, bounds[1:])]

    def _split_unexported_unlocked(self) -> bool:
        if not self._split_positions:
            return False
        if self._split_exported is None:
            return True
        current = [frozenset(str(page["id"]) for page in group) for group in self._groups_unlocked()]
        return current != self._split_exported

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
        workspace = self._workspace_ids()
        by_id = {page["id"]: page for page in self.pages}
        for page in self.pages:
            if page["document_id"] not in workspace or page["id"] in wanted:
                continue
            page["deleted"] = True
            page["order"] = 0
        for index, page_id in enumerate(visible_ids, start=1):
            page = by_id[page_id]
            page["deleted"] = False
            page["order"] = index

    def _renumber(self, visible_ids: list[str]) -> None:
        by_id = {page["id"]: page for page in self.pages}
        for index, page_id in enumerate(visible_ids, start=1):
            by_id[page_id]["order"] = index

    def _place_documents(self, document_ids: list[str]) -> bool:
        was_empty = len(self.documents) == 0
        existing = self._visible_ids()
        appended: list[str] = []
        for document_id in document_ids:
            if document_id in self._intakes:
                intake = self._intakes.pop(document_id)
                filename = str(intake["filename"])
                self._catalog[document_id] = filename
                self._append_new_pages(document_id, int(intake["page_count"]))
                self.documents.append({"id": document_id, "filename": filename})
            else:
                filename = self._catalog[document_id]
                self.documents.append({"id": document_id, "filename": filename})
            own = [page for page in self.pages if page["document_id"] == document_id and not page["deleted"]]
            own.sort(key=lambda page: (int(page["order"]), int(page["source_page"])))
            appended.extend(str(page["id"]) for page in own)
        self._renumber(existing + appended)
        if was_empty and len(document_ids) == 1:
            self._baseline = self._output_state()
        return True

    def _detach_documents(self, document_ids: list[str]) -> None:
        removing = set(document_ids)
        self.documents = [document for document in self.documents if document["id"] not in removing]
        self._renumber(self._visible_ids())

    def _append_new_pages(self, document_id: str, page_count: int) -> None:
        for number in range(1, page_count + 1):
            self.pages.append(
                {
                    "id": uuid4().hex,
                    "document_id": document_id,
                    "source_page": number,
                    "order": number,
                    "deleted": False,
                    "rotation": 0,
                }
            )

    def workspace_copy_path(self, document_id: str) -> Path | None:
        with self._lock:
            if document_id not in self._workspace_ids():
                return None
            path = self._copies.get(document_id)
        if path is not None and path.is_file():
            return path
        return None

    def export_plan(self) -> dict[str, object] | None:
        with self._lock:
            return self._export_plan_unlocked(None)

    def selection_export_plan(self) -> dict[str, object] | None:
        with self._lock:
            if not self.selected_page_ids:
                return None
            return self._export_plan_unlocked(set(self.selected_page_ids))

    def split_proposal(self) -> dict[str, object] | None:
        with self._lock:
            visible = self._visible_pages_unlocked()
            if not visible:
                return None
            groups = self._groups_unlocked()
            paths, required = self._source_budget(visible)
            return {
                "stem": Path(self._filename_for(visible)).stem,
                "groups": [
                    {
                        "page_ids": [str(page["id"]) for page in group],
                        "pages": [
                            (str(page["document_id"]), int(page["source_page"]), int(page["rotation"]))
                            for page in group
                        ],
                    }
                    for group in groups
                ],
                "bytes": required,
                "paths": paths,
            }

    def mark_split_exported(self, groups: list[list[str]]) -> None:
        with self._lock:
            self._split_exported = [frozenset(group) for group in groups]

    def _export_plan_unlocked(self, only: set[str] | None) -> dict[str, object] | None:
        pages = self._visible_pages_unlocked()
        if only is not None:
            pages = [page for page in pages if str(page["id"]) in only]
        if not pages:
            return None
        page_plan = [
            (str(page["document_id"]), int(page["source_page"]), int(page["rotation"]))
            for page in pages
        ]
        paths, required = self._source_budget(pages)
        return {"filename": self._filename_for(pages), "pages": page_plan, "bytes": required, "paths": paths}

    def _filename_for(self, pages: list[dict[str, object]]) -> str:
        visible_docs = {str(page["document_id"]) for page in pages}
        for document in self.documents:
            if document["id"] in visible_docs:
                return str(document["filename"])
        raise RuntimeError("見えているページの追加単位がありません")

    def _source_budget(self, pages: list[dict[str, object]]) -> tuple[dict[str, Path], int]:
        paths: dict[str, Path] = {}
        required = 0
        for page in pages:
            document_id = str(page["document_id"])
            if document_id in paths:
                continue
            path = self._copies[document_id]
            paths[document_id] = path
            required += path.stat().st_size
        return paths, required

    def mark_saved(self, pages: list[tuple[str, int, int]]) -> None:
        with self._lock:
            self._baseline = list(pages)

    def discard(self, body: object) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"detail": "破棄する PDF の指定が不正です"}
        document_ids = body.get("document_ids")
        if not isinstance(document_ids, list) or any(not isinstance(document_id, str) for document_id in document_ids):
            return 400, {"detail": "破棄する PDF の指定が不正です"}
        with self._lock:
            if len(document_ids) != len(set(document_ids)):
                return 400, {"detail": "追加単位の識別子が重複しています"}
            if not document_ids:
                return 200, {"discarded": []}
            for document_id in document_ids:
                if document_id not in self._intakes:
                    return 400, {"detail": "作業領域へ入れていない受け取りだけを破棄できます"}
            removed: list[Path] = []
            for document_id in document_ids:
                removed.append(self._copies.pop(document_id))
                del self._intakes[document_id]
                del self._hashes[document_id]
            doomed = self._unreferenced(removed)
        for path in doomed:
            path.unlink(missing_ok=True)
        return 200, {"discarded": list(document_ids)}

    def accept_upload(self, data: bytes, filename: str) -> tuple[int, dict]:
        if not self.try_begin(Activity.LOADING):
            return 409, {"detail": "読み込み中、ダイアログ中、または書き出し中のため受け付けられません"}
        try:
            if self.pause_during_load is not None:
                self.pause_during_load()
            return self.receive_bytes(data, filename, hold=Activity.LOADING)
        finally:
            self.finish(Activity.LOADING)

    def receive_bytes(
        self,
        data: bytes,
        filename: str,
        *,
        hold: Activity,
        exclude_ids: set[str] | None = None,
    ) -> tuple[int, dict]:
        try:
            prepared = self._prepare_upload(data, filename, exclude_ids=exclude_ids or set())
            stored = self._store_intake(prepared, hold=hold)
            if stored is None:
                if prepared["owns_file"]:
                    prepared["path"].unlink(missing_ok=True)
                return 409, {"detail": "書き出し中のため、読み込みを確定できませんでした"}
            return 200, stored
        except LoadError as exc:
            return 400, {"detail": str(exc)}

    def duplicate_against(self, document_id: str, body: object) -> tuple[int, dict]:
        if not isinstance(body, dict) or set(body) != {"approved_document_ids"}:
            return 400, {"detail": "重複の指定が不正です"}
        approved = body["approved_document_ids"]
        if not isinstance(approved, list) or any(not isinstance(item, str) for item in approved):
            return 400, {"detail": "重複の指定が不正です"}
        approved_ids = set(approved)
        with self._lock:
            intake = self._intakes.get(document_id)
            if intake is None:
                return 400, {"detail": "受け取っていない PDF です"}
            if intake["needs_password"]:
                return 400, {"detail": "パスワードの確認が終わっていない受け取りです"}
            digest = self._hashes[document_id]
            workspace = self._workspace_ids()
            duplicate = any(
                other_id != document_id
                and existing == digest
                and (other_id in workspace or other_id in approved_ids)
                for other_id, existing in self._hashes.items()
            )
            return 200, {"duplicate": duplicate}

    def submit_password(self, document_id: str, body: object) -> tuple[int, dict]:
        if not isinstance(body, dict) or set(body) != {"password"} or not isinstance(body.get("password"), str):
            return 400, {"detail": "パスワードの指定が不正です"}
        password = body["password"]
        try:
            with self._lock:
                refused = self._password_refusal(document_id)
                if refused is not None:
                    return refused
                encrypted = self._copies[document_id]
            try:
                decrypted, page_count, signed = self._decrypt(encrypted, password)
            except LoadError as exc:
                return 400, {"detail": str(exc)}
            with self._lock:
                if self._activity in {Activity.DIALOG, Activity.EXPORTING}:
                    decrypted.unlink(missing_ok=True)
                    return 409, {"detail": "ダイアログ中、または書き出し中のためパスワードを受け付けられません"}
                intake = self._intakes.get(document_id)
                if intake is None or not intake["needs_password"] or self._copies.get(document_id) != encrypted:
                    decrypted.unlink(missing_ok=True)
                    return 400, {"detail": "パスワードの確認が終わっていない受け取りではありません"}
                self._adopt_decrypted(document_id, decrypted, page_count, signed)
                return 200, self._intake_payload(document_id)
        finally:
            del password

    def _password_refusal(self, document_id: str) -> tuple[int, dict] | None:
        if self._activity in {Activity.DIALOG, Activity.EXPORTING}:
            return 409, {"detail": "ダイアログ中、または書き出し中のためパスワードを受け付けられません"}
        if document_id not in self._intakes:
            if document_id in self._workspace_ids() or document_id in self._catalog:
                return 400, {"detail": "作業領域にある PDF のパスワードは受け付けられません"}
            return 400, {"detail": "受け取っていない PDF です"}
        if not self._intakes[document_id]["needs_password"]:
            return 400, {"detail": "この PDF に開くパスワードは不要です"}
        return None

    def _decrypt(self, encrypted: Path, password: str) -> tuple[Path, int, bool]:
        try:
            with pikepdf.open(encrypted, password=password) as pdf:
                signed = _is_signed(pdf)
                page_count = len(pdf.pages)
                decrypted = self.temp_dir / f"incoming-{uuid4().hex}.pdf"
                try:
                    if self.fail_decrypt_save is not None:
                        decrypted.write_bytes(b"partial")
                        self.fail_decrypt_save()
                    pdf.save(decrypted, compress_streams=False)
                except Exception:
                    decrypted.unlink(missing_ok=True)
                    raise LoadError("PDF を保存できませんでした") from None
        except pikepdf.PasswordError:
            raise LoadError("パスワードが違います") from None
        except pikepdf.PdfError:
            raise LoadError("PDF を開けませんでした") from None
        return decrypted, page_count, signed

    def _adopt_decrypted(self, document_id: str, decrypted: Path, page_count: int, signed: bool) -> None:
        digest = self._hashes[document_id]
        shared = self._plain_path(digest, skip=document_id)
        chosen = decrypted
        if shared is not None and shared != decrypted:
            decrypted.unlink(missing_ok=True)
            chosen = shared
        previous = self._copies[document_id]
        self._copies[document_id] = chosen
        intake = self._intakes[document_id]
        intake["needs_password"] = False
        intake["page_count"] = page_count
        intake["signed"] = signed
        if previous != chosen and not self._referenced(previous):
            previous.unlink(missing_ok=True)

    def _store_intake(self, prepared: dict[str, object], hold: Activity = Activity.LOADING) -> dict[str, object] | None:
        if self.before_commit is not None:
            self.before_commit()
        with self._lock:
            if self._activity is not hold:
                return None
            document_id = uuid4().hex
            self._intakes[document_id] = {
                "filename": prepared["filename"],
                "page_count": prepared["page_count"],
                "needs_password": prepared["needs_password"],
                "signed": prepared["signed"],
                "byte_size": prepared["byte_size"],
                "duplicate": prepared["duplicate"],
            }
            self._copies[document_id] = prepared["path"]
            self._hashes[document_id] = prepared["digest"]
            return self._intake_payload(document_id)

    def _prepare_upload(
        self, data: bytes, filename: str, exclude_ids: set[str] | None = None
    ) -> dict[str, object]:
        safe_name = Path(filename).name
        if not safe_name.lower().endswith(".pdf"):
            raise LoadError("PDF 以外のファイルは読み込めません")
        digest = hashlib.sha256(data).hexdigest()
        byte_size = len(data)
        staging = self.temp_dir / f"incoming-{uuid4().hex}.pdf"
        staging.write_bytes(data)
        try:
            with pikepdf.open(staging) as pdf:
                page_count: int | None = len(pdf.pages)
                signed = _is_signed(pdf)
        except pikepdf.PasswordError:
            with self._lock:
                duplicate = self._is_duplicate(digest, exclude_ids)
            return {
                "filename": safe_name,
                "path": staging,
                "owns_file": True,
                "page_count": None,
                "needs_password": True,
                "signed": False,
                "byte_size": byte_size,
                "duplicate": duplicate,
                "digest": digest,
            }
        except pikepdf.PdfError:
            staging.unlink(missing_ok=True)
            raise LoadError("PDF を開けませんでした") from None
        except Exception:
            staging.unlink(missing_ok=True)
            raise
        with self._lock:
            duplicate = self._is_duplicate(digest, exclude_ids)
            shared = self._plain_path(digest)
        path = staging
        owns_file = True
        if shared is not None:
            staging.unlink(missing_ok=True)
            path = shared
            owns_file = False
        return {
            "filename": safe_name,
            "path": path,
            "owns_file": owns_file,
            "page_count": page_count,
            "needs_password": False,
            "signed": signed,
            "byte_size": byte_size,
            "duplicate": duplicate,
            "digest": digest,
        }

    def _intake_payload(self, document_id: str) -> dict[str, object]:
        intake = self._intakes[document_id]
        return {
            "document_id": document_id,
            "needs_password": bool(intake["needs_password"]),
            "byte_size": int(intake["byte_size"]),
            "page_count": intake["page_count"],
            "signed": bool(intake["signed"]),
            "duplicate": bool(intake["duplicate"]),
        }

    def _is_duplicate(self, digest: str, exclude_ids: set[str] | None = None) -> bool:
        excluded = exclude_ids or set()
        workspace = self._workspace_ids()
        for document_id, existing in self._hashes.items():
            if document_id in excluded or existing != digest:
                continue
            if document_id in workspace or document_id in self._intakes:
                return True
        return False

    def _plain_path(self, digest: str, skip: str | None = None) -> Path | None:
        for document_id, existing in self._hashes.items():
            if document_id == skip or existing != digest:
                continue
            intake = self._intakes.get(document_id)
            if intake is not None and intake["needs_password"]:
                continue
            path = self._copies.get(document_id)
            if path is not None and path.is_file():
                return path
        return None

    def _referenced(self, path: Path) -> bool:
        return any(existing == path for existing in self._copies.values())

    def _unreferenced(self, paths: list[Path]) -> list[Path]:
        doomed: list[Path] = []
        seen: set[Path] = set()
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            if not self._referenced(path):
                doomed.append(path)
        return doomed

def _is_signed(pdf: pikepdf.Pdf) -> bool:
    root = pdf.Root
    perms = root.get("/Perms")
    if isinstance(perms, pikepdf.Dictionary) and perms.get("/DocMDP") is not None:
        return True
    acro = root.get("/AcroForm")
    if not isinstance(acro, pikepdf.Dictionary):
        return False
    flags = acro.get("/SigFlags")
    if flags is not None:
        try:
            if int(flags) != 0:
                return True
        except (TypeError, ValueError):
            pass
    return _has_sig_field(acro.get("/Fields"), set())


def _has_sig_field(node: object, seen: set[int]) -> bool:
    if isinstance(node, pikepdf.Array):
        return any(_has_sig_field(item, seen) for item in node)
    if not isinstance(node, pikepdf.Dictionary):
        return False
    marker = id(node)
    if marker in seen:
        return False
    seen.add(marker)
    field_type = node.get("/FT")
    if field_type is not None and str(field_type) == "/Sig":
        return True
    return _has_sig_field(node.get("/Kids"), seen)


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
    if kind == "append_documents":
        return "PDFを追加する"
    if kind == "remove_documents":
        return "追加したPDFを外す"
    if kind == "set_split":
        if "after_page_ids" in command:
            return "解除した分割線を戻す"
        return "分割線を入れる"
    if kind == "clear_split":
        return "分割線を外す"
    if kind == "clear_all_splits":
        return "すべての分割線を解除する"
    return "操作する"
