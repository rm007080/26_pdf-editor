from pathlib import Path

from app.save_dialog import documents_folder

FOLDER_NOTICE = "前回のフォルダが使えないため、ドキュメントを開きました。"


def initial_directory(stored: object) -> tuple[Path, str | None]:
    if isinstance(stored, str) and stored.strip():
        candidate = Path(stored)
        try:
            if candidate.is_dir():
                return candidate, None
        except OSError:
            pass
        return documents_folder(), FOLDER_NOTICE
    return documents_folder(), None


def with_notice(body: dict, notice: str | None) -> dict:
    if not notice:
        return body
    noted = dict(body)
    noted["initial_folder_notice"] = notice
    return noted
