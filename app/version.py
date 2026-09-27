import tomllib
from pathlib import Path

FALLBACK_VERSION = "0.1.0"


def read_app_version(path: Path | None = None) -> str:
    target = path or Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with target.open("rb") as handle:
            loaded = tomllib.load(handle)
        version = loaded["project"]["version"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return FALLBACK_VERSION
    if isinstance(version, str) and version:
        return version
    return FALLBACK_VERSION
