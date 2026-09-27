import json
import math
import os
from pathlib import Path

LAYOUTS = {"row", "grid"}
SIZES = {"small", "medium", "large"}
THEMES = {"light", "dark"}
IDLE_MINUTES = {30, 60, 120, 240}
REQUIRED_KEYS = (
    "thumbnail_layout",
    "thumbnail_size",
    "pane_ratio",
    "theme",
    "windows_notifications",
    "idle_minutes",
)
FOLDER_KEYS = ("last_open_dir", "last_save_dir")
PUT_KEYS = set(REQUIRED_KEYS) | set(FOLDER_KEYS)
DEFAULTS = {
    "thumbnail_layout": "row",
    "thumbnail_size": "medium",
    "pane_ratio": None,
    "theme": None,
    "windows_notifications": True,
    "idle_minutes": 120,
    "last_open_dir": None,
    "last_save_dir": None,
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


def _folder_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() != "":
        return value
    return None


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
        layout = loaded.get("thumbnail_layout", DEFAULTS["thumbnail_layout"])
        size = loaded.get("thumbnail_size", DEFAULTS["thumbnail_size"])
        ratio = loaded.get("pane_ratio", DEFAULTS["pane_ratio"])
        theme = loaded.get("theme", DEFAULTS["theme"])
        notifications = loaded.get("windows_notifications", DEFAULTS["windows_notifications"])
        idle = loaded.get("idle_minutes", DEFAULTS["idle_minutes"])
        return {
            "thumbnail_layout": layout if layout in LAYOUTS else DEFAULTS["thumbnail_layout"],
            "thumbnail_size": size if size in SIZES else DEFAULTS["thumbnail_size"],
            "pane_ratio": ratio if ratio is None or _valid_ratio(ratio) else DEFAULTS["pane_ratio"],
            "theme": theme if theme is None or theme in THEMES else DEFAULTS["theme"],
            "windows_notifications": notifications
            if type(notifications) is bool
            else DEFAULTS["windows_notifications"],
            "idle_minutes": idle if type(idle) is int and idle in IDLE_MINUTES else DEFAULTS["idle_minutes"],
            "last_open_dir": _folder_value(loaded.get("last_open_dir", DEFAULTS["last_open_dir"])),
            "last_save_dir": _folder_value(loaded.get("last_save_dir", DEFAULTS["last_save_dir"])),
        }

    def write(self, body: object) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"detail": "設定の指定が不正です"}
        if any(key not in PUT_KEYS for key in body) or any(key not in body for key in REQUIRED_KEYS):
            return 400, {"detail": "設定の指定が不正です"}
        layout = body["thumbnail_layout"]
        size = body["thumbnail_size"]
        ratio = body["pane_ratio"]
        theme = body["theme"]
        notifications = body["windows_notifications"]
        idle = body["idle_minutes"]
        if (
            layout not in LAYOUTS
            or size not in SIZES
            or not (ratio is None or _valid_ratio(ratio))
            or not (theme is None or theme in THEMES)
            or type(notifications) is not bool
            or type(idle) is not int
            or idle not in IDLE_MINUTES
        ):
            return 400, {"detail": "設定の指定が不正です"}
        current = self.read()
        saved = {
            "thumbnail_layout": layout,
            "thumbnail_size": size,
            "pane_ratio": ratio,
            "theme": theme,
            "windows_notifications": notifications,
            "idle_minutes": idle,
            "last_open_dir": current["last_open_dir"],
            "last_save_dir": current["last_save_dir"],
        }
        self._write_file(saved)
        return 200, saved

    def reset(self) -> dict[str, object]:
        saved = dict(DEFAULTS)
        self._write_file(saved)
        return saved

    def remember_directory(self, key: str, directory: Path) -> None:
        if key not in FOLDER_KEYS:
            raise ValueError(key)
        current = self.read()
        current[key] = str(directory)
        self._write_file(current)

    def _write_file(self, saved: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)
