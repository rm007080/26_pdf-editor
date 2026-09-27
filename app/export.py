import asyncio
import os
import shutil
import threading
from pathlib import Path
from uuid import uuid4

import pikepdf

from app.folders import initial_directory, with_notice
from app.notify import show_notification
from app.open_path import open_file, open_folder
from app.save_dialog import ask_folder, ask_save_path
from app.session import Activity, Session
from app.settings import SettingsStore


class ExportFailure(Exception):
    pass


class Job:
    def __init__(
        self,
        job_id: str,
        requested: Path,
        page_plan: list[tuple[str, int, int]],
        sources: dict[str, Path],
        required_bytes: int,
        *,
        kind: str = "all",
        parts: list[tuple[Path, list[tuple[str, int, int]]]] | None = None,
        group_ids: list[list[str]] | None = None,
    ) -> None:
        self.id = job_id
        self.requested = requested
        self.page_plan = page_plan
        self.sources = sources
        self.required_bytes = required_bytes
        self.kind = kind
        self.parts = parts
        self.group_ids = group_ids
        self.stage = "copying"
        self.processed_pages = 0
        self.total_pages = len(page_plan)
        self.path: Path | None = None
        self.files: list[Path] | None = None
        self.detail: str | None = None


class ExportService:
    def __init__(self, session: Session, settings: SettingsStore) -> None:
        self.session = session
        self.settings = settings
        self.show_notification = show_notification
        self.ask_save_path = ask_save_path
        self.ask_folder = ask_folder
        self.enough_space = enough_space
        self.page_count = page_count
        self.open_file = open_file
        self.open_folder = open_folder
        self.pause_during_copy = None
        self.before_replace = None
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._split: dict[str, object] | None = None

    async def begin_export(self) -> tuple[int, dict]:
        return await self._begin_single("all")

    async def begin_selection(self) -> tuple[int, dict]:
        return await self._begin_single("selection")

    async def _begin_single(self, kind: str) -> tuple[int, dict]:
        if not self.session.try_begin(Activity.DIALOG):
            return 409, {"detail": "読み込み中、または書き出し中のため受け付けられません"}
        plan = self.session.selection_export_plan() if kind == "selection" else self.session.export_plan()
        if plan is None:
            self.session.finish(Activity.DIALOG)
            return 400, {"detail": "書き出すページがありません"}
        initial_dir, notice = initial_directory(self.settings.read()["last_save_dir"])
        initial_name = initial_filename(str(plan["filename"]))
        pages = list(plan["pages"])
        required_bytes = int(plan["bytes"])
        sources = dict(plan["paths"])
        try:
            chosen = await asyncio.to_thread(self.ask_save_path, initial_dir, initial_name)
        except Exception:
            self.session.finish(Activity.DIALOG)
            return 500, {"detail": "保存ダイアログを開けませんでした"}
        if chosen is None:
            self.session.finish(Activity.DIALOG)
            return 200, with_notice({"status": "cancelled"}, notice)
        chosen_path = Path(chosen)
        job_id = self._create_job(chosen_path, pages, sources, required_bytes, kind=kind)
        if not self.session.transition(Activity.DIALOG, Activity.EXPORTING):
            self._discard_job(job_id)
            self.session.finish(Activity.DIALOG)
            return 409, {"detail": "書き出しを開始できませんでした"}
        self.settings.remember_directory("last_save_dir", chosen_path.parent)
        asyncio.get_running_loop().run_in_executor(None, self.run_job, job_id)
        return 200, with_notice({"job_id": job_id}, notice)

    async def begin_split(self) -> tuple[int, dict]:
        if not self.session.try_begin(Activity.DIALOG):
            return 409, {"detail": "読み込み中、または書き出し中のため受け付けられません"}
        proposal = self.session.split_proposal()
        if proposal is None:
            self.session.finish(Activity.DIALOG)
            return 400, {"detail": "書き出すページがありません"}
        ticket = object()
        with self._lock:
            self._split = {"kind": "folder", "ticket": ticket}
        initial_dir, notice = initial_directory(self.settings.read()["last_save_dir"])
        try:
            chosen = await asyncio.to_thread(self.ask_folder, initial_dir)
        except Exception:
            if self._drop_split(ticket):
                self.session.finish(Activity.DIALOG)
            return 500, {"detail": "フォルダを選べませんでした"}
        if chosen is None:
            if self._drop_split(ticket):
                self.session.finish(Activity.DIALOG)
            return 200, with_notice({"status": "cancelled"}, notice)
        groups = list(proposal["groups"])
        names = [f"{proposal['stem']}_{index}.pdf" for index in range(1, len(groups) + 1)]
        with self._lock:
            if self._split is None or self._split.get("ticket") is not ticket:
                return 200, with_notice({"status": "cancelled"}, notice)
            self._split = {
                "kind": "naming",
                "ticket": ticket,
                "directory": Path(chosen),
                "names": names,
                "groups": groups,
                "paths": dict(proposal["paths"]),
                "bytes": int(proposal["bytes"]),
            }
        return 200, with_notice(
            {
                "status": "naming",
                "files": [{"name": name, "pages": len(group["page_ids"])} for name, group in zip(names, groups)],
            },
            notice,
        )

    async def confirm_split(self, body: object) -> tuple[int, dict]:
        with self._lock:
            pending = self._split
            if pending is None or pending.get("kind") != "naming":
                return 409, {"detail": "ファイル名の確定を受け付けられません"}
            detail = _names_error(body, list(pending["names"]))
            if detail is not None:
                return 400, {"detail": detail}
            names = list(body["names"])
            directory = Path(pending["directory"])
            groups = list(pending["groups"])
            paths = dict(pending["paths"])
            required = int(pending["bytes"])
        parts: list[tuple[Path, list[tuple[str, int, int]]]] = []
        flat: list[tuple[str, int, int]] = []
        group_ids: list[list[str]] = []
        for name, group in zip(names, groups):
            pages = list(group["pages"])
            parts.append((directory / name, pages))
            flat.extend(pages)
            group_ids.append(list(group["page_ids"]))
        job_id = self._create_job(
            parts[0][0],
            flat,
            paths,
            required,
            kind="split",
            parts=parts,
            group_ids=group_ids,
        )
        if not self.session.transition(Activity.DIALOG, Activity.EXPORTING):
            self._discard_job(job_id)
            with self._lock:
                self._split = None
            self.session.finish(Activity.DIALOG)
            return 409, {"detail": "書き出しを開始できませんでした"}
        with self._lock:
            self._split = None
        self.settings.remember_directory("last_save_dir", directory)
        asyncio.get_running_loop().run_in_executor(None, self.run_job, job_id)
        return 200, {"job_id": job_id}

    def cancel_split(self) -> tuple[int, dict]:
        with self._lock:
            pending = self._split
            if pending is None:
                return 200, {"status": "cancelled"}
            self._split = None
        self.session.finish(Activity.DIALOG)
        return 200, {"status": "cancelled"}

    def _drop_split(self, ticket: object) -> bool:
        with self._lock:
            pending = self._split
            if pending is None or pending.get("ticket") is not ticket:
                return False
            self._split = None
            return True

    def job_view(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            total = job.total_pages
            processed = job.processed_pages
            stage = job.stage
            files = None
            if stage == "completed" and job.kind == "split":
                path = None
                files = [str(item) for item in (job.files or [])]
            else:
                path = str(job.path) if stage == "completed" and job.path is not None else None
            detail = job.detail
        ratio = (processed / total) if total else 0.0
        body = {
            "job_id": job_id,
            "stage": stage,
            "processed_pages": processed,
            "total_pages": total,
            "ratio": ratio,
            "path": path,
            "detail": detail,
        }
        if files is not None:
            body["files"] = files
        return body

    def open_saved_file(self, job_id: str, body: object = None) -> tuple[int, dict]:
        path, error = self._open_target(job_id, body)
        if error is not None:
            return error
        self.open_file(path)
        return 200, {"status": "opened"}

    def open_saved_folder(self, job_id: str) -> tuple[int, dict]:
        path, error = self._open_target(job_id, None, folder=True)
        if error is not None:
            return error
        self.open_folder(path)
        return 200, {"status": "opened"}

    def _open_target(
        self, job_id: str, body: object, folder: bool = False
    ) -> tuple[Path, tuple[int, dict]] | tuple[None, tuple[int, dict]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None, (404, {"detail": "書き出しが見つかりません"})
            if job.stage != "completed":
                return None, (409, {"detail": "書き出しが完了していません"})
            if job.kind == "split":
                files = list(job.files or [])
                if folder:
                    if not files:
                        return None, (409, {"detail": "書き出しが完了していません"})
                    return files[0], None
                if (
                    not isinstance(body, dict)
                    or set(body) != {"index"}
                    or isinstance(body.get("index"), bool)
                    or not isinstance(body.get("index"), int)
                ):
                    return None, (400, {"detail": "開くファイルの指定が不正です"})
                index = int(body["index"])
                if index < 0 or index >= len(files):
                    return None, (400, {"detail": "開くファイルの指定が不正です"})
                return files[index], None
            if job.path is None:
                return None, (409, {"detail": "書き出しが完了していません"})
            return job.path, None

    def run_job(self, job_id: str) -> None:
        if self._job_kind(job_id) == "split":
            self._run_split(job_id)
            return
        temp_path: Path | None = None
        outcome: bool | None = None
        try:
            requested, total, pages, sources, required_bytes = self._job_plan(job_id)
            self._update(job_id, stage="copying")
            final = avoid_collision(requested)
            if not self.enough_space(final.parent, required_bytes):
                raise ExportFailure("空き容量が足りません")
            temp_path = final.with_name(f".{final.name}.{job_id}.part")
            self._copy_pages(sources, temp_path, pages, job_id)
            self._update(job_id, stage="verifying")
            if self.page_count(temp_path) != total:
                raise ExportFailure("書き出した PDF のページ数が一致しません")
            self._update(job_id, stage="committing")
            final = avoid_collision(requested)
            os.replace(temp_path, final)
            temp_path = None
            if self._job_kind(job_id) == "all":
                self.session.mark_saved(pages)
            self._update(job_id, stage="completed", path=final, processed_pages=total)
            outcome = True
        except Exception as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            message = str(exc) if str(exc) else "書き出しに失敗しました"
            self._update(job_id, stage="failed", detail=message)
            outcome = False
        finally:
            self.session.finish(Activity.EXPORTING)
        if outcome is not None:
            self._notify(outcome)

    def _run_split(self, job_id: str) -> None:
        temps: list[Path] = []
        committed: list[Path] = []
        outcome: bool | None = None
        try:
            parts, sources, required_bytes, group_ids = self._split_plan(job_id)
            total = sum(len(pages) for _requested, pages in parts)
            self._update(job_id, stage="copying")
            if self.pause_during_copy is not None:
                self.pause_during_copy()
            directory = parts[0][0].parent
            if not self.enough_space(directory, required_bytes):
                raise ExportFailure("空き容量が足りません")
            reserved: set[Path] = set()
            prepared: list[tuple[Path, Path, list[tuple[str, int, int]]]] = []
            processed = 0
            for index, (requested, pages) in enumerate(parts):
                final = _allocate_unique(requested, reserved)
                temp_path = final.with_name(f".{final.name}.{job_id}.{index}.part")
                self._copy_pages(sources, temp_path, pages, job_id, pause=False, processed_base=processed)
                processed += len(pages)
                temps.append(temp_path)
                prepared.append((temp_path, final, pages))
            self._update(job_id, stage="verifying")
            for temp_path, _final, pages in prepared:
                if self.page_count(temp_path) != len(pages):
                    raise ExportFailure("書き出した PDF のページ数が一致しません")
            self._update(job_id, stage="committing")
            for index, (temp_path, final, _pages) in enumerate(prepared):
                chosen = _allocate_unique(final, reserved) if final.exists() else final
                if self.before_replace is not None:
                    self.before_replace(index, chosen)
                os.replace(temp_path, chosen)
                temps.remove(temp_path)
                committed.append(chosen)
            self.session.mark_split_exported(group_ids)
            self._update(job_id, stage="completed", files=list(committed), processed_pages=total, path=None)
            outcome = True
        except Exception as exc:
            for path in temps:
                path.unlink(missing_ok=True)
            for path in committed:
                path.unlink(missing_ok=True)
            message = str(exc) if str(exc) else "書き出しに失敗しました"
            self._update(job_id, stage="failed", detail=message)
            outcome = False
        finally:
            self.session.finish(Activity.EXPORTING)
        if outcome is not None:
            self._notify(outcome)

    def _notify(self, success: bool) -> None:
        try:
            if self.settings.read()["windows_notifications"] is not True:
                return
            body = "書き出しが完了しました" if success else "書き出しに失敗しました"
            self.show_notification("PDF編集", body)
        except Exception:
            return

    def _create_job(
        self,
        requested: Path,
        pages: list[tuple[str, int, int]],
        sources: dict[str, Path],
        required_bytes: int,
        *,
        kind: str = "all",
        parts: list[tuple[Path, list[tuple[str, int, int]]]] | None = None,
        group_ids: list[list[str]] | None = None,
    ) -> str:
        job_id = uuid4().hex
        with self._lock:
            self._jobs[job_id] = Job(
                job_id,
                requested,
                pages,
                sources,
                required_bytes,
                kind=kind,
                parts=parts,
                group_ids=group_ids,
            )
        return job_id

    def _discard_job(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)

    def _job_kind(self, job_id: str) -> str:
        with self._lock:
            return self._jobs[job_id].kind

    def _job_plan(self, job_id: str) -> tuple[Path, int, list[tuple[str, int, int]], dict[str, Path], int]:
        with self._lock:
            job = self._jobs[job_id]
            return job.requested, job.total_pages, list(job.page_plan), dict(job.sources), job.required_bytes

    def _split_plan(
        self, job_id: str
    ) -> tuple[list[tuple[Path, list[tuple[str, int, int]]]], dict[str, Path], int, list[list[str]]]:
        with self._lock:
            job = self._jobs[job_id]
            parts = [(path, list(pages)) for path, pages in (job.parts or [])]
            return parts, dict(job.sources), job.required_bytes, [list(group) for group in (job.group_ids or [])]

    def _update(self, job_id: str, **changes) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)

    def _copy_pages(
        self,
        sources: dict[str, Path],
        temp_path: Path,
        pages: list[tuple[str, int, int]],
        job_id: str,
        *,
        pause: bool = True,
        processed_base: int = 0,
    ) -> None:
        opened: dict[str, pikepdf.Pdf] = {}
        try:
            if pause and self.pause_during_copy is not None:
                self.pause_during_copy()
            output = pikepdf.Pdf.new()
            for index, (document_id, source_page, rotation) in enumerate(pages, start=1):
                if document_id not in opened:
                    opened[document_id] = pikepdf.open(sources[document_id])
                output.pages.append(opened[document_id].pages[source_page - 1])
                if rotation % 360:
                    output.pages[-1].rotate(rotation % 360, relative=True)
                self._update(job_id, processed_pages=processed_base + index)
            output.save(temp_path)
        finally:
            for src in opened.values():
                src.close()

def initial_filename(original: str) -> str:
    return f"{Path(original).stem}_編集済み.pdf"


_FORBIDDEN_NAME_CHARS = set('<>:"/\\|?*')


def _names_error(body: object, expected: list[str]) -> str | None:
    if not isinstance(body, dict) or set(body) != {"names"}:
        return "ファイル名の指定が不正です"
    names = body.get("names")
    if not isinstance(names, list) or len(names) != len(expected) or any(not isinstance(name, str) for name in names):
        return "ファイル名の指定が不正です"
    if len(names) != len(set(names)):
        return "ファイル名が重複しています"
    for name in names:
        if (
            name == ""
            or name.endswith(" ")
            or name.endswith(".")
            or not name.endswith(".pdf")
            or any(char in name for char in _FORBIDDEN_NAME_CHARS)
        ):
            return "ファイル名が不正です"
    return None


def _allocate_unique(path: Path, reserved: set[Path]) -> Path:
    candidate = path
    number = 1
    while candidate in reserved or candidate.exists():
        candidate = path.with_name(f"{path.stem}({number}){path.suffix}")
        number += 1
    reserved.add(candidate)
    return candidate


def avoid_collision(path: Path) -> Path:
    candidate = path
    number = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}({number}){path.suffix}")
        number += 1
    return candidate


def enough_space(directory: Path, required: int) -> bool:
    return directory.exists() and shutil.disk_usage(directory).free >= required


def page_count(path: Path) -> int:
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)
