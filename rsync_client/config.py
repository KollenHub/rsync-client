from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from .models import ServerProfile, TaskSpec, connection_route


def default_config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "rsync-desk" / "tasks.json"


def migrate_connections(specs: list[TaskSpec]) -> tuple[list[TaskSpec], list[ServerProfile]]:
    profiles = {}
    migrated = []
    for spec in specs:
        if spec.direction == "local":
            migrated.append(spec)
            continue
        key = (spec.host, spec.username, spec.port, spec.identity_file, spec.credential_id)
        if key not in profiles:
            suffix = f" ({len(profiles) + 1})" if any(p.host == spec.host for p in profiles.values()) else ""
            profiles[key] = ServerProfile(f"{spec.username}@{spec.host}{suffix}", spec.host, spec.username,
                                          spec.port, spec.password, spec.identity_file, spec.credential_id)
        migrated.append(profiles[key].bind(spec))
    return migrated, list(profiles.values())


DIRECTIONS = ("upload", "download", "local")


def sanitize_last_directories(raw) -> dict[str, dict[str, dict[str, str]]]:
    """Keep only well-formed {server_id: {direction: {source/destination: path}}} entries."""
    result: dict[str, dict[str, dict[str, str]]] = {}
    if not isinstance(raw, dict):
        return result
    for server_id, directions in raw.items():
        if not isinstance(server_id, str) or not isinstance(directions, dict):
            continue
        cleaned = {}
        for direction, entry in directions.items():
            if direction not in DIRECTIONS:
                continue
            if isinstance(entry, str):  # tolerate a plain destination string
                entry = {"destination": entry}
            if not isinstance(entry, dict):
                continue
            paths = {key: entry.get(key) for key in ("source", "destination")}
            paths = {key: value for key, value in paths.items()
                     if isinstance(value, str) and value.strip()}
            if paths:
                cleaned[direction] = paths
        if cleaned:
            result[server_id] = cleaned
    return result


def save_config(path: Path, specs: list[TaskSpec], concurrency: int, servers: list[ServerProfile], *,
                states: list[dict] | None = None,
                last_directories: dict[str, dict[str, dict[str, str]]] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tasks = [s.public_dict() for s in specs]
    if states is not None:
        if len(states) != len(tasks):
            raise ValueError("任务状态与任务列表数量不一致")
        for task, state in zip(tasks, states):
            task['state'] = state
    data = {"version": 2, "concurrency": concurrency, "servers": [s.public_dict() for s in servers],
            "tasks": tasks, "last_directories": sanitize_last_directories(last_directories)}
    fd, temporary = tempfile.mkstemp(prefix=".tasks-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_config(path: Path) -> tuple[list[TaskSpec], int, list[ServerProfile], dict[str, dict[str, dict[str, str]]]]:
    if not path.exists():
        return [], 2, [], {}
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or data.get("version") not in (1, 2) or not isinstance(data.get("tasks"), list):
        raise ValueError("无法识别的任务配置格式")
    concurrency = data.get("concurrency", 2)
    if type(concurrency) is not int or not 1 <= concurrency <= 8:
        raise ValueError("并发任务数必须是 1–8")
    if data["version"] == 1:
        specs, servers = migrate_connections([TaskSpec.from_dict(item) for item in data["tasks"]])
        return specs, concurrency, servers, {}
    if not isinstance(data.get("servers"), list):
        raise TypeError("服务器列表格式无效")
    servers = [ServerProfile.from_dict(item) for item in data["servers"]]
    lookup = {s.id: s for s in servers}
    if len(lookup) != len(servers):
        raise ValueError("服务器标识重复")
    if len({s.name for s in servers}) != len(servers):
        raise ValueError("服务器名称重复")
    for profile in servers:
        connection_route(profile, lookup)
    specs = []
    for item in data["tasks"]:
        if not isinstance(item, dict):
            raise TypeError("任务配置必须是对象")
        if server_id := item.get("server_id"):
            if server_id not in lookup:
                raise ValueError("任务引用的服务器不存在")
            profile = lookup[server_id]
            fields = {key: value for key, value in profile.public_dict().items()
                      if key in {"host", "username", "port", "identity_file", "credential_id"}}
            spec = replace(profile.bind(TaskSpec.from_dict({**item, **fields})), jump_hosts=connection_route(profile, lookup)[:-1])
        else:
            spec = TaskSpec.from_dict(item)
            if spec.direction != "local":
                raise ValueError("远端任务缺少服务器引用")
        specs.append(spec)
    return specs, concurrency, servers, sanitize_last_directories(data.get("last_directories"))


def save_tasks(path: Path, specs: list[TaskSpec], concurrency: int) -> None:
    """Compatibility API for standalone users of the original task configuration."""
    migrated, profiles = migrate_connections(specs)
    save_config(path, migrated, concurrency, profiles)


def load_tasks(path: Path) -> tuple[list[TaskSpec], int]:
    specs, concurrency, _, _ = load_config(path)
    return specs, concurrency


def load_task_states(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    return [item.get('state', {}) if isinstance(item.get('state'), dict) else {} for item in data['tasks']]
