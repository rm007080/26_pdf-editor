import os
import subprocess
from pathlib import Path


def open_file(path: Path) -> None:
    os.startfile(path)  # type: ignore[attr-defined]


def open_folder(path: Path) -> None:
    subprocess.Popen(["explorer", f"/select,{path}"])
