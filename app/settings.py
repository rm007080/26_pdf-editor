import json
import math
import os
from pathlib import Path

LAYOUTS = {"row", "grid"}
SIZES = {"small", "medium", "large"}
DEFAULTS = {
    "thumbnail_layout": "row",
    "thumbnail_size": "medium",
    "pane_ratio": None,
}


def default_settings_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        local = str(Path.home() / "AppData" / "Local")
    return Path(local) / "pdf-editor" / "settings.json"


def _valid_ratio(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and 0 < value < 1


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_settings_path()

    def read(self) -> dict[str, object]:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return dict(DEFAULTS)
        if not isinstance(loaded, dict):
            return dict(DEFAULTS)
        layout = loaded.get("thumbnail_layout")
        size = loaded.get("thumbnail_size")
        ratio = loaded.get("pane_ratio", None)
        return {
            "thumbnail_layout": layout if layout in LAYOUTS else DEFAULTS["thumbnail_layout"],
            "thumbnail_size": size if size in SIZES else DEFAULTS["thumbnail_size"],
            "pane_ratio": ratio if ratio is None or _valid_ratio(ratio) else DEFAULTS["pane_ratio"],
        }

    def write(self, body: object) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"detail": "設定の指定が不正です"}
        if any(key not in body for key in DEFAULTS):
            return 400, {"detail": "設定の指定が不正です"}
        layout = body["thumbnail_layout"]
        size = body["thumbnail_size"]
        ratio = body["pane_ratio"]
        if layout not in LAYOUTS or size not in SIZES or not (ratio is None or _valid_ratio(ratio)):
            return 400, {"detail": "設定の指定が不正です"}
        saved = {
            "thumbnail_layout": layout,
            "thumbnail_size": size,
            "pane_ratio": ratio,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)
        return 200, saved
