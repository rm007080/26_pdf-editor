import os
from datetime import datetime, timedelta
from pathlib import Path

from app.open_path import open_folder

LIMIT_BYTES = 10 * 1024 * 1024
MAX_AGE = timedelta(days=30)


def default_log_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        local = str(Path.home() / "AppData" / "Local")
    return Path(local) / "pdf-editor" / "logs"


def prune_logs(directory: Path, now: datetime | None = None) -> None:
    if not directory.is_dir():
        return
    moment = now or datetime.now()
    cutoff = moment - MAX_AGE
    for path in [item for item in directory.iterdir() if item.is_file()]:
        if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
            path.unlink()
    remaining = [item for item in directory.iterdir() if item.is_file()]
    remaining.sort(key=lambda item: (item.stat().st_mtime, item.name))
    total = sum(item.stat().st_size for item in remaining)
    for path in remaining:
        if total <= LIMIT_BYTES:
            return
        size = path.stat().st_size
        path.unlink()
        total -= size


class LogStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or default_log_dir()
        self.open_folder = open_folder

    def open(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        prune_logs(self.directory)
        self.open_folder(self.directory)
