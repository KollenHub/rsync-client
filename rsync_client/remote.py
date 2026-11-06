from __future__ import annotations

import stat
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import paramiko

from .models import ServerProfile


@dataclass(frozen=True)
class RemoteEntry:
    path: str
    name: str
    is_directory: bool
    size: int
    is_symlink: bool = False


class DirectoryReadCancelled(Exception):
    pass


class RemoteService:
    """Reusable SSH sessions for server login and SFTP browsing."""

    CACHE_TTL = 15.0
    CACHE_DIRECTORIES = 32
    CACHE_ENTRIES = 20_000
    READ_AHEADS = 16

    def __init__(self, known_hosts: Path | None = None):
        self.known_hosts = known_hosts or Path.home() / ".ssh" / "known_hosts"
        self._lock = threading.RLock()
        self._host_lock = threading.Lock()
        self._sessions = {}
        self._server_locks = {}
        self._sftp_sessions = {}
        self._directory_cache = OrderedDict()
        self._closed = threading.Event()

    def _server_lock(self, server_id: str):
        with self._lock:
            return self._server_locks.setdefault(server_id, threading.RLock())

    def connected(self, server_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(server_id)
            transport = session[1].get_transport() if session else None
            return bool(transport and transport.is_active() and transport.is_authenticated())

    def _client(self, profile: ServerProfile, jumps: tuple[ServerProfile, ...] = ()) -> paramiko.SSHClient:
        if self._closed.is_set():
            raise ValueError("应用正在关闭")
        profile.validate()
        with self._lock:
            session = self._sessions.get(profile.id)
            if session and session[0] == (*jumps, profile) and self.connected(profile.id):
                return session[1]
        clients = []
        try:
            for endpoint in (*jumps, profile):
                endpoint.validate()
                via = clients[-1].get_transport().open_channel(
                    "direct-tcpip", (endpoint.host, endpoint.port), ("127.0.0.1", 0), timeout=15,
                ) if clients else None
                client = paramiko.SSHClient()
                clients.append(client)
                with self._host_lock:
                    client.load_system_host_keys()
                    self.known_hosts.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    self.known_hosts.touch(mode=0o600, exist_ok=True)
                    client.load_host_keys(str(self.known_hosts))
                    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    client.connect(endpoint.host, port=endpoint.port, username=endpoint.username,
                                   password=endpoint.password or None,
                                   key_filename=str(Path(endpoint.identity_file).expanduser()) if endpoint.identity_file else None,
                                   look_for_keys=not bool(endpoint.password), allow_agent=not bool(endpoint.password),
                                   timeout=15, banner_timeout=15, auth_timeout=15, channel_timeout=15, sock=via)
                client.get_transport().set_keepalive(15)
            with self._lock:
                if self._closed.is_set():
                    raise ValueError("应用正在关闭")
                previous = self._sessions.get(profile.id)
                self._sessions[profile.id] = ((*jumps, profile), client, clients)
            if previous:
                self._drop_sftp(profile.id)
                self._clear_cache(profile.id)
                for previous_client in reversed(previous[2]):
                    previous_client.close()
            return client
        except BaseException:
            for client in reversed(clients):
                client.close()
            raise

    def login(self, profile: ServerProfile, jumps: tuple[ServerProfile, ...] = ()) -> str:
        with self._server_lock(profile.id):
            self.disconnect(profile.id)
            client = self._client(profile, jumps)
            try:
                stdin, stdout, stderr = client.exec_command("rsync --version", timeout=15)
                stdin.close()
                output = stdout.read(65536).decode("utf-8", "replace")
                error = stderr.read(65536).decode("utf-8", "replace")
                code = stdout.channel.recv_exit_status()
                stdout.close()
                stderr.close()
                if code:
                    raise ValueError(f"SSH 已登录，但远端无法运行 rsync：{error.strip() or code}")
                return output.splitlines()[0] if output else "登录成功"
            except BaseException:
                self.disconnect(profile.id)
                raise

    def _sftp(self, profile: ServerProfile, jumps: tuple[ServerProfile, ...]):
        client = self._client(profile, jumps)
        with self._lock:
            existing = self._sftp_sessions.get(profile.id)
        if existing and existing[0] is client and not existing[1].get_channel().closed:
            return existing[1]
        self._drop_sftp(profile.id)
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(15)
        with self._lock:
            session = self._sessions.get(profile.id)
            if self._closed.is_set() or not session or session[1] is not client:
                sftp.close()
                raise DirectoryReadCancelled("目录读取已取消")
            self._sftp_sessions[profile.id] = (client, sftp)
        return sftp

    def _drop_sftp(self, server_id, expected=None):
        with self._lock:
            existing = self._sftp_sessions.get(server_id)
            if existing and (expected is None or existing[1] is expected):
                self._sftp_sessions.pop(server_id)
            else:
                existing = None
        if existing:
            with suppress(OSError, EOFError):
                existing[1].close()

    def _clear_cache(self, server_id):
        with self._lock:
            for key in list(self._directory_cache):
                if key[0] == server_id:
                    del self._directory_cache[key]

    def resolve_symlink(self, profile: ServerProfile, entry: RemoteEntry,
                        jumps: tuple[ServerProfile, ...] = ()) -> RemoteEntry:
        with self._server_lock(profile.id):
            attributes = self._sftp(profile, jumps).stat(entry.path)
            return RemoteEntry(entry.path, entry.name, stat.S_ISDIR(attributes.st_mode or 0),
                               entry.size, is_symlink=True)

    def list_directory(
        self, profile: ServerProfile, path: str, jumps: tuple[ServerProfile, ...] = (), *,
        force_refresh: bool = False, on_batch: Callable | None = None,
        cancel: threading.Event | None = None,
    ) -> tuple[str, list[RemoteEntry]]:
        def check_cancel():
            if cancel and cancel.is_set():
                raise DirectoryReadCancelled("目录读取已取消")

        with self._server_lock(profile.id):
            check_cancel()
            requested = path or profile.initial_directory
            route = (*jumps, profile)
            key = (profile.id, requested)
            with self._lock:
                cached = self._directory_cache.get(key)
                session = self._sessions.get(profile.id)
                if (not force_refresh and cached and time.monotonic() - cached[0] < self.CACHE_TTL
                        and session and session[0] == route and self.connected(profile.id)):
                    self._directory_cache.move_to_end(key)
                    directory, entries = cached[1], list(cached[2])
                else:
                    cached = None
            if cached:
                if on_batch:
                    for start in range(0, max(1, len(entries)), 128):
                        check_cancel()
                        on_batch((directory, tuple(entries[start:start + 128])))
                return directory, entries

            sftp = self._sftp(profile, jumps)
            try:
                directory = sftp.normalize(requested)
                entries = []
                pending = []
                emitted = False
                last_emit = time.monotonic()
                # Pipeline READDIR requests; attributes arrive with each batch, without
                # one round trip per item or per symlink. Resolve links only when opened.
                for item in sftp.listdir_iter(directory, read_aheads=self.READ_AHEADS):
                    check_cancel()
                    full_path = str(PurePosixPath(directory) / item.filename)
                    is_directory = stat.S_ISDIR(item.st_mode or 0)
                    entry = RemoteEntry(full_path, item.filename, is_directory, item.st_size or 0,
                                        is_symlink=stat.S_ISLNK(item.st_mode or 0))
                    entries.append(entry)
                    if on_batch:
                        pending.append(entry)
                        now = time.monotonic()
                        if len(pending) >= 128 or now - last_emit >= 0.05:
                            on_batch((directory, tuple(pending)))
                            pending.clear()
                            emitted, last_emit = True, now
                check_cancel()
                if on_batch and (pending or not emitted):
                    on_batch((directory, tuple(pending)))
                entries.sort(key=lambda item: (not (item.is_directory or item.is_symlink), item.name.casefold()))
                with self._lock:
                    if self.connected(profile.id):
                        for cache_key in {key, (profile.id, directory)}:
                            self._directory_cache[cache_key] = (time.monotonic(), directory, tuple(entries))
                            self._directory_cache.move_to_end(cache_key)
                        while (len(self._directory_cache) > self.CACHE_DIRECTORIES
                               or sum(len(value[2]) for value in self._directory_cache.values()) > self.CACHE_ENTRIES):
                            self._directory_cache.popitem(last=False)
                return directory, entries
            except BaseException:
                # A cancelled generator can leave outstanding READDIR replies; discard
                # only its SFTP channel so subsequent navigation starts cleanly.
                self._drop_sftp(profile.id, sftp)
                raise

    def disconnect(self, server_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(server_id, None)
        self._drop_sftp(server_id)
        self._clear_cache(server_id)
        if session:
            for client in reversed(session[2]):
                client.close()

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            sftp_sessions = list(self._sftp_sessions.values())
            self._sftp_sessions.clear()
            self._directory_cache.clear()
        for _, sftp in sftp_sessions:
            with suppress(OSError, EOFError):
                sftp.close()
        for _, _, clients in sessions:
            for client in reversed(clients):
                client.close()
