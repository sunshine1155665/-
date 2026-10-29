"""Configuration loading with profile merge and path resolution."""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = "low"


def _load_dotenv(path: Path | None = None) -> None:
    """Small stdlib .env loader; existing environment variables win."""
    target = path or PROJECT_ROOT / ".env"
    if not target.exists():
        return
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


class Config:
    """Small dotted-path wrapper around a nested dict."""

    def __init__(self, data: dict[str, Any], *, profile: str, source: Path | None = None):
        self._data = data
        self.profile = profile
        self.source = source

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def section(self, name: str) -> dict[str, Any]:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    @property
    def data_dir(self) -> Path:
        raw = os.environ.get("FATFISH_DATA_DIR") or self.get("app.data_dir", "data")
        path = Path(str(raw))
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @property
    def db_path(self) -> Path:
        raw = os.environ.get("FATFISH_DB") or self.get("storage.db_path", "data/fatfish.db")
        path = Path(str(raw))
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path.resolve()

    @property
    def sticker_dir(self) -> Path:
        raw = self.get("sticker.library_dir", "stickers")
        path = Path(str(raw))
        if not path.is_absolute():
            path = self.data_dir / path
        return path.resolve()

    @property
    def archive_dir(self) -> Path:
        raw = self.get("storage.archive_dir", "archive")
        path = Path(str(raw))
        if not path.is_absolute():
            path = self.data_dir / path
        return path.resolve()

    @property
    def backup_dir(self) -> Path:
        raw = self.get("storage.backup_dir", "backups")
        path = Path(str(raw))
        if not path.is_absolute():
            path = self.data_dir / path
        return path.resolve()

    @property
    def timezone_name(self) -> str:
        return str(self.get("app.timezone", "Asia/Shanghai"))

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.sticker_dir, self.archive_dir, self.backup_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_config(
    profile: str | None = None,
    config_path: str | Path | None = None,
) -> Config:
    """Load a profile file, merge it over low.toml defaults.

    Precedence: low.toml < selected profile < explicit config file.
    Environment variables are read lazily by the components that need them.
    """
    profile = profile or os.environ.get("FATFISH_PROFILE") or DEFAULT_PROFILE
    base_path = PROJECT_ROOT / "config" / "low.toml"
    base = _read_toml(base_path) if base_path.exists() else {}
    selected_path = PROJECT_ROOT / "config" / f"{profile}.toml"
    selected = _read_toml(selected_path) if selected_path.exists() else {}
    merged = _deep_merge(base, selected)
    source = selected_path if selected_path.exists() else base_path
    if config_path:
        explicit_path = Path(config_path)
        if not explicit_path.is_absolute():
            explicit_path = PROJECT_ROOT / explicit_path
        merged = _deep_merge(merged, _read_toml(explicit_path))
        source = explicit_path
    return Config(merged, profile=profile, source=source)


def env_secret(name: str | None) -> str | None:
    if not name:
        return None
    value = os.environ.get(name)
    return value.strip() if value else None
