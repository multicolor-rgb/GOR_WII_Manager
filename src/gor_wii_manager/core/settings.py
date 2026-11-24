"""Persistent application settings (JSON in XDG config)."""

from __future__ import annotations

import json
import os
import shutil

APP_ID = "io.github.gor.GorWiiManager"
APP_NAME = "GOR Wii Manager"
APP_VERSION = "0.6.0"

APP_DIR_NAME = "gor-wii-manager"
LEGACY_DIR_NAME = "wbfs-manager"

_MIGRATION_DONE = False
_MIGRATION_NOTES: list[str] = []


def _xdg_base(kind: str) -> str:
    if kind == "config":
        return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    if kind == "cache":
        return os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def _copy_missing_files(source: str, target: str) -> int:
    copied = 0
    try:
        entries = list(os.scandir(source))
    except OSError:
        return 0
    os.makedirs(target, exist_ok=True)
    for entry in entries:
        destination = os.path.join(target, entry.name)
        if entry.is_file() and not os.path.exists(destination):
            try:
                shutil.copy2(entry.path, destination)
                copied += 1
            except OSError:
                pass
    return copied


def _ensure_migration() -> None:
    """Copy settings/titles/covers from the pre-rename directories (once)."""
    global _MIGRATION_DONE
    if _MIGRATION_DONE:
        return
    _MIGRATION_DONE = True
    try:
        new_config = os.path.join(_xdg_base("config"), APP_DIR_NAME)
        old_config = os.path.join(_xdg_base("config"), LEGACY_DIR_NAME)
        old_settings = os.path.join(old_config, "settings.json")
        new_settings = os.path.join(new_config, "settings.json")
        os.makedirs(new_config, exist_ok=True)
        if os.path.isfile(old_settings) and not os.path.exists(new_settings):
            try:
                shutil.copy2(old_settings, new_settings)
                _MIGRATION_NOTES.append("ustawienia")
            except OSError:
                pass

        new_data = os.path.join(_xdg_base("data"), APP_DIR_NAME)
        old_data = os.path.join(_xdg_base("data"), LEGACY_DIR_NAME)
        os.makedirs(new_data, exist_ok=True)
        titles = 0
        try:
            for entry in os.scandir(old_data):
                if (
                    entry.is_file()
                    and entry.name.startswith("titles")
                    and entry.name.endswith(".txt")
                    and not os.path.exists(os.path.join(new_data, entry.name))
                ):
                    try:
                        shutil.copy2(entry.path, os.path.join(new_data, entry.name))
                        titles += 1
                    except OSError:
                        pass
        except OSError:
            pass
        if titles:
            _MIGRATION_NOTES.append("baza tytułów")

        new_cache = os.path.join(_xdg_base("cache"), APP_DIR_NAME)
        old_covers = os.path.join(_xdg_base("cache"), LEGACY_DIR_NAME, "covers")
        if os.path.isdir(old_covers):
            count = _copy_missing_files(old_covers, os.path.join(new_cache, "covers"))
            if count:
                _MIGRATION_NOTES.append(f"okładki ({count})")
    except OSError:
        pass


def data_dir() -> str:
    _ensure_migration()
    path = os.path.join(_xdg_base("data"), APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def cache_dir() -> str:
    _ensure_migration()
    path = os.path.join(_xdg_base("cache"), APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_dir() -> str:
    _ensure_migration()
    path = os.path.join(_xdg_base("config"), APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


DEFAULTS = {
    "scrub": "remove_update",
    "verify_after_import": False,
    "show_covers": True,
    "auto_fetch_titles": True,
    "ui_language": "en",
    "language": "en",
    "titles_url": "https://www.gametdb.com/wiitdb.zip",
    "recent": [],
    "last_dir": "",
    "window_width": 1180,
    "window_height": 760,
    "cover_kind": "cover3D",
}


class Settings:
    def __init__(self, path: str | None = None):
        _ensure_migration()
        self.migration_notes = list(_MIGRATION_NOTES)
        self.path = path or os.path.join(config_dir(), "settings.json")
        self.values = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            for key, value in data.items():
                if key in DEFAULTS:
                    self.values[key] = value

    def save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.values, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def set(self, key: str, value) -> None:
        if self.values.get(key) != value:
            self.values[key] = value
            self.save()

    def add_recent(self, path: str, limit: int = 10) -> None:
        recent = [item for item in self.values.get("recent", []) if item != path]
        recent.insert(0, path)
        self.values["recent"] = recent[:limit]
        self.save()
