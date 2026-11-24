"""SQLite persistence for servers, tasks, settings and the encrypted vault blob.

Everything the application persists lives in a single database file. Legacy
installations kept ``tasks.json`` plus ``credentials.enc`` next to it; the first
time a fresh database is created those files are imported automatically and
renamed to ``*.migrated`` so they stay available as a manual backup. A legacy
file that cannot be parsed is sidelined as ``*.invalid`` instead of blocking
the database, so saving keeps working.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .models import ServerProfile, TaskSpec, connection_route

SCHEMA_VERSION = 1
SQLITE_MAGIC = b"SQLite format 3\x00"
DIRECTIONS = ("upload", "download", "local", "remote")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS servers (
    id                TEXT PRIMARY KEY,
    position          INTEGER NOT NULL UNIQUE,
    name              TEXT NOT NULL UNIQUE,
    host              TEXT NOT NULL,
    username          TEXT NOT NULL,
    port              INTEGER NOT NULL,
    identity_file     TEXT NOT NULL DEFAULT '',
    credential_id     TEXT NOT NULL DEFAULT '',
    initial_directory TEXT NOT NULL DEFAULT '/',
    jump_server_id    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tasks (
    position       INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    source         TEXT NOT NULL,
    destination    TEXT NOT NULL,
    direction      TEXT NOT NULL,
    host           TEXT NOT NULL DEFAULT '',
    username       TEXT NOT NULL DEFAULT '',
    port           INTEGER NOT NULL DEFAULT 22,
    identity_file  TEXT NOT NULL DEFAULT '',
    credential_id  TEXT NOT NULL DEFAULT '',
    server_id      TEXT NOT NULL DEFAULT '',
    compression    INTEGER NOT NULL DEFAULT 1,
    bandwidth_kib  INTEGER NOT NULL DEFAULT 0,
    excludes       TEXT NOT NULL DEFAULT '[]',
    sync_contents  INTEGER NOT NULL DEFAULT 0,
    group_id       TEXT NOT NULL DEFAULT '',
    group_name     TEXT NOT NULL DEFAULT '',
    state          TEXT
);
CREATE TABLE IF NOT EXISTS last_directories (
    server_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    kind      TEXT NOT NULL,
    path      TEXT NOT NULL,
    PRIMARY KEY (server_id, direction, kind)
);
CREATE TABLE IF NOT EXISTS vault (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    blob TEXT NOT NULL
);
"""

_SERVER_COLUMNS = ("id", "name", "host", "username", "port", "identity_file",
                   "credential_id", "initial_directory", "jump_server_id")
_TASK_COLUMNS = ("name", "source", "destination", "direction", "host", "username", "port",
                 "identity_file", "credential_id", "server_id", "compression", "bandwidth_kib",
                 "excludes", "sync_contents", "group_id", "group_name")


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


def _validate_concurrency(value) -> int:
    if type(value) is not int or not 1 <= value <= 16:
        raise ValueError("并发任务数必须是 1–16")
    return value


def _assemble(items: list[dict], servers: list[ServerProfile]) -> list[TaskSpec]:
    """Bind task dictionaries to the server list, mirroring the legacy JSON loader."""
    lookup = {s.id: s for s in servers}
    if len(lookup) != len(servers):
        raise ValueError("服务器标识重复")
    if len({s.name for s in servers}) != len(servers):
        raise ValueError("服务器名称重复")
    for profile in servers:
        connection_route(profile, lookup)
    specs = []
    for item in items:
        if not isinstance(item, dict):
            raise TypeError("任务配置必须是对象")
        if server_id := item.get("server_id"):
            if server_id not in lookup:
                raise ValueError("任务引用的服务器不存在")
            profile = lookup[server_id]
            fields = {key: value for key, value in profile.public_dict().items()
                      if key in {"host", "username", "port", "identity_file", "credential_id"}}
            spec = replace(profile.bind(TaskSpec.from_dict({**item, **fields})),
                           jump_hosts=connection_route(profile, lookup)[:-1])
        else:
            spec = TaskSpec.from_dict(item)
            if spec.direction != "local":
                raise ValueError("远端任务缺少服务器引用")
        specs.append(spec)
    return specs


def _parse_legacy(data) -> tuple[list[TaskSpec], int, list[ServerProfile], list[dict], dict]:
    """Validate a legacy tasks.json document and return its normalized contents."""
    if not isinstance(data, dict) or data.get("version") not in (1, 2) or not isinstance(data.get("tasks"), list):
        raise ValueError("无法识别的任务配置格式")
    concurrency = data.get("concurrency", 2)
    if type(concurrency) is not int:
        concurrency = 2
    concurrency = min(max(concurrency, 1), 16)  # clamp; old UIs allowed values the legacy loader rejected
    if data["version"] == 1:
        specs, servers = migrate_connections([TaskSpec.from_dict(item) for item in data["tasks"]])
        return specs, concurrency, servers, [{} for _ in specs], {}
    if not isinstance(data.get("servers"), list):
        raise TypeError("服务器列表格式无效")
    servers = [ServerProfile.from_dict(item) for item in data["servers"]]
    items, states = [], []
    for item in data["tasks"]:
        if not isinstance(item, dict):
            raise TypeError("任务配置必须是对象")
        state = item.get("state")
        states.append(state if isinstance(state, dict) else {})
        items.append({key: value for key, value in item.items() if key != "state"})
    return _assemble(items, servers), concurrency, servers, states, sanitize_last_directories(data.get("last_directories"))


def _read_legacy_tasks(source: Path) -> tuple[list[TaskSpec], int, list[ServerProfile], dict, list[dict]]:
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError("无法识别的任务配置格式") from exc
    return _parse_legacy(data)


class Store:
    """Single-file SQLite persistence; one instance wraps one database path."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._pending: tuple | None = None  # legacy payload absorbed from a foreign file

    # ------------------------------------------------------------------ io --
    @contextmanager
    def _connect(self):
        self._absorb_foreign_file()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            self._prepare(connection)
            yield connection
        finally:
            connection.close()

    def _absorb_foreign_file(self) -> None:
        """If the target path holds a legacy JSON config, parse it and move it aside."""
        try:
            with self.path.open("rb") as stream:
                header = stream.read(16)
        except OSError:
            return  # missing or unreadable; sqlite3 reports the real problem later
        if header[:15] == SQLITE_MAGIC[:15]:
            return
        if not header:
            self.path.unlink(missing_ok=True)  # empty placeholder from an aborted run
            return
        try:
            payload = _read_legacy_tasks(self.path)
            suffix = ".migrated"
        except (ValueError, TypeError, KeyError):
            payload, suffix = None, ".invalid"  # keep the foreign file aside; create a fresh database
        os.replace(self.path, _backup_name(self.path, suffix))
        self._pending = payload

    def _prepare(self, connection: sqlite3.Connection) -> None:
        try:
            fresh = not connection.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise ValueError("配置数据库无法读取，可能已损坏") from exc
        if not fresh:
            return
        legacy = None
        legacy_file = None
        if self._pending:
            legacy = self._pending
            self._pending = None
        else:
            candidate = self.path.parent / "tasks.json"
            if candidate.exists() and candidate.stat().st_size:
                try:
                    legacy, legacy_file = _read_legacy_tasks(candidate), candidate
                except (OSError, ValueError, TypeError, KeyError):
                    os.replace(candidate, _backup_name(candidate, ".invalid"))
        credentials = self.path.parent / "credentials.enc"
        try:
            blob = credentials.read_text(encoding="utf-8") if credentials.exists() and credentials.stat().st_size else None
        except (OSError, UnicodeDecodeError):
            blob = None  # keep the unreadable file in place for manual recovery
        connection.executescript(_SCHEMA)
        if legacy is not None:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._write(connection, *legacy)
                connection.commit()
            except (sqlite3.Error, ValueError, TypeError):
                connection.rollback()  # database stays usable; the original file is kept
                legacy_file = None
        if blob is not None:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("INSERT OR REPLACE INTO vault (id, blob) VALUES (1, ?)", (blob,))
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        if legacy_file is not None:
            os.replace(legacy_file, _backup_name(legacy_file))
        if blob is not None:
            os.replace(credentials, _backup_name(credentials))

    # --------------------------------------------------------------- writes --
    def save(self, specs: list[TaskSpec], concurrency: int, servers: list[ServerProfile], *,
             states: list[dict] | None = None,
             last_directories: dict[str, dict[str, dict[str, str]]] | None = None) -> None:
        if states is not None and len(states) != len(specs):
            raise ValueError("任务状态与任务列表数量不一致")
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._write(connection, specs, concurrency, servers, states, last_directories or {})
                except BaseException:
                    connection.rollback()
                    raise
                connection.commit()
        except sqlite3.Error as exc:
            raise ValueError(f"配置数据库写入失败：{exc}") from exc

    def _write(self, connection, specs: list[TaskSpec], concurrency: int, servers: list[ServerProfile],
               states: list[dict] | None, last_directories) -> None:
        connection.execute("DELETE FROM servers")
        connection.execute("DELETE FROM tasks")
        connection.execute("DELETE FROM last_directories")
        connection.executemany(
            f"INSERT INTO servers ({', '.join(_SERVER_COLUMNS)}, position) VALUES ({', '.join('?' * len(_SERVER_COLUMNS))}, ?)",
            [(profile.id, profile.name, profile.host, profile.username, profile.port, profile.identity_file,
              profile.credential_id, profile.initial_directory, profile.jump_server_id, index)
             for index, profile in enumerate(servers)])
        for position, spec in enumerate(specs):
            item = spec.public_dict()
            connection.execute(
                f"INSERT INTO tasks ({', '.join(_TASK_COLUMNS)}, position, state) VALUES ({', '.join('?' * len(_TASK_COLUMNS))}, ?, ?)",
                (item["name"], item["source"], item["destination"], item["direction"],
                 item.get("host", ""), item.get("username", ""), item.get("port", 22),
                 item.get("identity_file", ""), item.get("credential_id", ""), item.get("server_id", ""),
                 int(item.get("compression", True)), item.get("bandwidth_kib", 0),
                 json.dumps(list(item.get("excludes", ())), ensure_ascii=False),
                 int(item.get("sync_contents", False)), item.get("group_id", ""), item.get("group_name", ""),
                 position,
                 json.dumps(states[position], ensure_ascii=False) if states is not None else None))
        rows = [(server_id, direction, kind, path)
                for server_id, directions in sanitize_last_directories(last_directories).items()
                for direction, paths in directions.items()
                for kind, path in paths.items()]
        connection.executemany("INSERT INTO last_directories (server_id, direction, kind, path) VALUES (?, ?, ?, ?)", rows)
        connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('concurrency', ?)", (int(concurrency),))

    # ---------------------------------------------------------------- vault --
    def vault_blob(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT blob FROM vault WHERE id = 1").fetchone()
        except sqlite3.Error as exc:
            raise ValueError(f"配置数据库读取失败：{exc}") from exc
        return row[0] if row else None

    def write_vault(self, blob: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("INSERT OR REPLACE INTO vault (id, blob) VALUES (1, ?)", (blob,))
                except BaseException:
                    connection.rollback()
                    raise
                connection.commit()
        except sqlite3.Error as exc:
            raise ValueError(f"配置数据库写入失败：{exc}") from exc

    # ---------------------------------------------------------------- reads --
    def load(self) -> tuple[list[TaskSpec], int, list[ServerProfile], dict[str, dict[str, dict[str, str]]]]:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT value FROM meta WHERE key = 'concurrency'").fetchone()
                server_rows = connection.execute(
                    f"SELECT {', '.join(_SERVER_COLUMNS)} FROM servers ORDER BY position").fetchall()
                task_rows = connection.execute(
                    f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks ORDER BY position").fetchall()
                directory_rows = connection.execute(
                    "SELECT server_id, direction, kind, path FROM last_directories").fetchall()
        except sqlite3.Error as exc:
            raise ValueError(f"配置数据库读取失败：{exc}") from exc
        try:
            concurrency = _validate_concurrency(int(row[0])) if row else 2
        except (TypeError, ValueError) as exc:
            raise ValueError("并发任务数必须是 1–16") from exc
        servers = [ServerProfile.from_dict(dict(zip(_SERVER_COLUMNS, row))) for row in server_rows]
        items = [_task_item(row) for row in task_rows]
        directories: dict[str, dict[str, dict[str, str]]] = {}
        for server_id, direction, kind, path in directory_rows:
            directories.setdefault(server_id, {}).setdefault(direction, {})[kind] = path
        return _assemble(items, servers), concurrency, servers, sanitize_last_directories(directories)

    def load_states(self) -> list[dict]:
        try:
            with self._connect() as connection:
                rows = connection.execute("SELECT state FROM tasks ORDER BY position").fetchall()
        except sqlite3.Error as exc:
            raise ValueError(f"配置数据库读取失败：{exc}") from exc
        states = []
        for (raw,) in rows:
            try:
                state = json.loads(raw) if raw else {}
            except ValueError:
                state = {}
            states.append(state if isinstance(state, dict) else {})
        return states


def _task_item(row) -> dict:
    item = dict(zip(_TASK_COLUMNS[:10], row[:10]))
    item["compression"] = bool(row[10])
    item["bandwidth_kib"] = row[11]
    item["excludes"] = tuple(json.loads(row[12]))
    item["sync_contents"] = bool(row[13])
    item["group_id"] = row[14]
    item["group_name"] = row[15]
    return item


def _backup_name(path: Path, suffix: str = ".migrated") -> Path:
    return path.with_name(path.name + suffix)
