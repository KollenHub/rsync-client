"""Configuration facade: every persisted value lives in the SQLite store."""
from __future__ import annotations

import os
from pathlib import Path

from .models import ServerProfile, TaskSpec
from .storage import DIRECTIONS, Store, migrate_connections, sanitize_last_directories

__all__ = ["DIRECTIONS", "default_config_path", "load_config", "load_task_states", "load_tasks",
           "migrate_connections", "sanitize_last_directories", "save_config", "save_tasks"]


def default_config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "rsync-desk" / "rsync-desk.db"


def save_config(path: Path, specs: list[TaskSpec], concurrency: int, servers: list[ServerProfile], *,
                states: list[dict] | None = None,
                last_directories: dict[str, dict[str, dict[str, str]]] | None = None) -> None:
    Store(path).save(specs, concurrency, servers, states=states, last_directories=last_directories)


def load_config(path: Path) -> tuple[list[TaskSpec], int, list[ServerProfile], dict[str, dict[str, dict[str, str]]]]:
    return Store(path).load()


def save_tasks(path: Path, specs: list[TaskSpec], concurrency: int) -> None:
    """Compatibility API for standalone users of the original task configuration."""
    migrated, profiles = migrate_connections(specs)
    save_config(path, migrated, concurrency, profiles)


def load_tasks(path: Path) -> tuple[list[TaskSpec], int]:
    specs, concurrency, _, _ = load_config(path)
    return specs, concurrency


def load_task_states(path: Path) -> list[dict]:
    return Store(path).load_states()
