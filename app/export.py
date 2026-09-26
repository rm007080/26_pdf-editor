import asyncio
import os
import shutil
import threading
from pathlib import Path
from uuid import uuid4

import pikepdf

from app.open_path import open_file, open_folder
from app.save_dialog import ask_save_path, documents_folder
from app.session import Activity, Session


class ExportFailure(Exception):
    pass


class Job:
    def __init__(self, job_id: str, requested: Path, source_pages: list[int]) -> None:
        self.id = job_id
        self.requested = requested
        self.source_pages = source_pages
        self.stage = "copying"
        self.processed_pages = 0
        self.total_pages = len(source_pages)
        self.path: Path | None = None
        self.detail: str | None = None


class ExportService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.ask_save_path = ask_save_path
        self.enough_space = enough_space
        self.page_count = page_count
        self.open_file = open_file
        self.open_folder = open_folder
        self.pause_during_copy = None
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}

    async def begin_export(self) -> tuple[int, dict]:
        if not self.session.try_begin(Activity.DIALOG):
            return 409, {"detail": "読み込み中、または書き出し中のため受け付けられません"}
        plan = self._plan()
        if plan is None:
            self.session.finish(Activity.DIALOG)
            return 400, {"detail": "書き出すページがありません"}
        initial_dir, initial_name, pages = plan
        try:
            chosen = await asyncio.to_thread(self.ask_save_path, initial_dir, initial_name)
        except Exception:
            self.session.finish(Activity.DIALOG)
            return 500, {"detail": "保存ダイアログを開けませんでした"}
        if chosen is None:
            self.session.finish(Activity.DIALOG)
            return 200, {"status": "cancelled"}
        job_id = self._create_job(Path(chosen), pages)
        if not self.session.transition(Activity.DIALOG, Activity.EXPORTING):
            self.session.finish(Activity.DIALOG)
            return 409, {"detail": "書き出しを開始できませんでした"}
        asyncio.get_running_loop().run_in_executor(None, self.run_job, job_id)
        return 200, {"job_id": job_id}

    def job_view(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            total = job.total_pages
            processed = job.processed_pages
            stage = job.stage
            path = str(job.path) if stage == "completed" and job.path is not None else None
            detail = job.detail
        ratio = (processed / total) if total else 0.0
        return {
            "job_id": job_id,
            "stage": stage,
            "processed_pages": processed,
            "total_pages": total,
            "ratio": ratio,
            "path": path,
            "detail": detail,
        }

    def open_saved_file(self, job_id: str) -> tuple[int, dict]:
        return self._open_saved(job_id, self.open_file)

    def open_saved_folder(self, job_id: str) -> tuple[int, dict]:
        return self._open_saved(job_id, self.open_folder)

    def _open_saved(self, job_id: str, opener) -> tuple[int, dict]:
        path = self._completed_path(job_id)
        if path is None:
            return self._open_rejection(job_id)
        opener(path)
        return 200, {"status": "opened"}

    def run_job(self, job_id: str) -> None:
        temp_path: Path | None = None
        try:
            requested, total, pages = self._job_plan(job_id)
            self._update(job_id, stage="copying")
            source = self.session.session_file_path()
            if source is None:
                raise ExportFailure("読み込まれた PDF がありません")
            final = avoid_collision(requested)
            if not self.enough_space(final.parent, source.stat().st_size):
                raise ExportFailure("空き容量が足りません")
            temp_path = final.with_name(f".{final.name}.{job_id}.part")
            self._copy_pages(source, temp_path, pages, job_id)
            self._update(job_id, stage="verifying")
            if self.page_count(temp_path) != total:
                raise ExportFailure("書き出した PDF のページ数が一致しません")
            self._update(job_id, stage="committing")
            final = avoid_collision(requested)
            os.replace(temp_path, final)
            temp_path = None
            self._update(job_id, stage="completed", path=final, processed_pages=total)
        except Exception as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            message = str(exc) if str(exc) else "書き出しに失敗しました"
            self._update(job_id, stage="failed", detail=message)
        finally:
            self.session.finish(Activity.EXPORTING)

    def _plan(self) -> tuple[Path, str, list[int]] | None:
        snapshot = self.session.snapshot()
        pages = [page for page in snapshot["pages"] if not page["deleted"]]
        pages.sort(key=lambda page: page["order"])
        if not snapshot["documents"] or not pages:
            return None
        filename = initial_filename(snapshot["documents"][0]["filename"])
        return documents_folder(), filename, [int(page["source_page"]) for page in pages]

    def _create_job(self, requested: Path, pages: list[int]) -> str:
        job_id = uuid4().hex
        with self._lock:
            self._jobs[job_id] = Job(job_id, requested, pages)
        return job_id

    def _job_plan(self, job_id: str) -> tuple[Path, int, list[int]]:
        with self._lock:
            job = self._jobs[job_id]
            return job.requested, job.total_pages, list(job.source_pages)

    def _update(self, job_id: str, **changes) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)

    def _copy_pages(self, source: Path, temp_path: Path, pages: list[int], job_id: str) -> None:
        with pikepdf.open(source) as src:
            if self.pause_during_copy is not None:
                self.pause_during_copy()
            output = pikepdf.Pdf.new()
            for index, source_page in enumerate(pages, start=1):
                output.pages.append(src.pages[source_page - 1])
                self._update(job_id, processed_pages=index)
            output.save(temp_path)

    def _completed_path(self, job_id: str) -> Path | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.stage != "completed" or job.path is None:
                return None
            return job.path

    def _open_rejection(self, job_id: str) -> tuple[int, dict]:
        with self._lock:
            exists = job_id in self._jobs
        if not exists:
            return 404, {"detail": "書き出しが見つかりません"}
        return 409, {"detail": "書き出しが完了していません"}


def initial_filename(original: str) -> str:
    return f"{Path(original).stem}_編集済み.pdf"


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
