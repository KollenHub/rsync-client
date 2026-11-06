from __future__ import annotations

import codecs
import math
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from .askpass import PasswordBroker
from .models import ACTIVE, ServerProfile, TaskSpec, local_path

PROGRESS = re.compile(r"^\s*([\d,]+)\s+(\d+)%\s+([\d.]+)([kKMGT]?B)/s\s+(\S+)")
TOTAL = re.compile(r"^Total transferred file size:\s*([\d,]+)")
FILE_MARKER = "@@RSYNC_FILE@@"


def _ssh_options(profile: ServerProfile | TaskSpec) -> list[str]:
    ssh = ["ssh", "-p", str(profile.port), "-l", profile.username, "-T", "-o", "ConnectTimeout=15",
           "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "NumberOfPasswordPrompts=1"]
    if profile.password or profile.credential_id:
        ssh.extend(["-o", "PreferredAuthentications=password,keyboard-interactive",
                    "-o", "PubkeyAuthentication=no", "-o", "BatchMode=no"])
    else:
        ssh.extend(["-o", "BatchMode=yes"])
    if profile.identity_file:
        ssh.extend(["-i", local_path(profile.identity_file), "-o", "IdentitiesOnly=yes"])
    return ssh


def build_command(spec: TaskSpec, *, dry_run: bool = False, rsync: str = "rsync") -> list[str]:
    # -rlt preserves links and timestamps without requiring ownership/device privileges.
    command = [rsync, "-rlt", "-s", "--8-bit-output", "--no-human-readable", "--stats", "--outbuf=L"]
    if dry_run:
        command.append("--dry-run")
    else:
        command.extend(["--partial-dir=.rsync-partial", "--info=progress2", f"--out-format={FILE_MARKER}%n"])
    if spec.bandwidth_kib:
        command.append(f"--bwlimit={spec.bandwidth_kib}")
    for pattern in spec.excludes:
        command.extend(["--exclude", pattern])
    if spec.direction != "local":
        proxy = ""
        for hop in spec.jump_hosts:
            hop_command = _ssh_options(hop)
            if proxy:
                hop_command.extend(["-o", "ProxyCommand=" + proxy.replace("%", "%%")])
            proxy = shlex.join(["env", f"RSYNC_DESK_AUTH_ID={hop.id}", *hop_command, "-W", "[%h]:%p", hop.host])
        ssh = _ssh_options(spec)
        if proxy:
            ssh.extend(["-o", "ProxyCommand=" + proxy])
        command.extend(["-e", shlex.join(ssh)])
        if spec.compression:
            command.append("-z")
        host = f"[{spec.host}]" if ":" in spec.host else spec.host
        remote = f"{host}:"
    source = spec.source.rstrip("/") or "/"
    destination = spec.destination.rstrip("/") + "/"
    if spec.direction == "upload":
        source, destination = local_path(source), remote + destination
    elif spec.direction == "download":
        source, destination = remote + source, local_path(destination) + "/"
    else:
        source, destination = local_path(source), local_path(destination) + "/"
    command.extend(["--", source, destination])
    return command


@dataclass(frozen=True)
class Snapshot:
    id: str
    spec: TaskSpec
    status: str
    total_bytes: int | None
    transferred_bytes: int
    speed: float
    eta: str
    current_file: str
    error: str
    return_code: int | None
    started_at: float | None
    ended_at: float | None

    @property
    def percent(self) -> float:
        if self.status == "success":
            return 100.0
        if not self.total_bytes:
            return 0.0
        return min(99.9, max(0, self.transferred_bytes / self.total_bytes * 100))

    def public_state(self) -> dict:
        state = {name: getattr(self, name) for name in ('status', 'total_bytes', 'transferred_bytes',
                 'current_file', 'error', 'return_code', 'started_at', 'ended_at')}
        for password in [self.spec.password, *(hop.password for hop in self.spec.jump_hosts)]:
            if password:
                state['error'] = state['error'].replace(password, '******')
        return state


@dataclass
class _Task:
    spec: TaskSpec
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "ready"
    total_bytes: int | None = None
    transferred_bytes: int = 0
    speed: float = 0
    eta: str = "—"
    current_file: str = ""
    error: str = ""
    return_code: int | None = None
    started_at: float | None = None
    ended_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    logs: deque = field(default_factory=lambda: deque(maxlen=10_000))
    sequence: int = 0

    def snapshot(self) -> Snapshot:
        fields = Snapshot.__dataclass_fields__
        return Snapshot(**{name: getattr(self, name) for name in fields})


@dataclass(frozen=True)
class Summary:
    total: int
    complete: int
    failed: int
    active: int
    unknown: int
    total_bytes: int
    transferred_bytes: int
    speed: float
    percent: float


def summarize(tasks: list[Snapshot]) -> Summary:
    total = sum(t.total_bytes or 0 for t in tasks)
    transferred = sum(min(t.transferred_bytes, t.total_bytes or 0) for t in tasks)
    completed = sum(t.status == "success" for t in tasks)
    unknown = sum(t.total_bytes is None for t in tasks)
    if tasks and completed == len(tasks):
        percent = 100.0
    elif total:
        percent = min(99.9, transferred / total * 100)
    else:
        percent = min(99.9, completed / len(tasks) * 100) if tasks else 0.0
    return Summary(len(tasks), completed, sum(t.status == "failed" for t in tasks),
                   sum(t.status in ACTIVE for t in tasks), unknown, total, transferred,
                   sum(t.speed for t in tasks if t.status == "running"), percent)


class TaskManager:
    def __init__(self, concurrency: int = 2, rsync: str = "rsync"):
        self._lock = threading.RLock()
        self._tasks: dict[str, _Task] = {}
        self._running: set[str] = set()
        self._threads: set[threading.Thread] = set()
        self._concurrency = concurrency
        self._closed = False
        self.rsync = rsync

    def add(self, spec: TaskSpec, *, check_paths: bool = True) -> str:
        spec.validate(check_paths=check_paths)
        with self._lock:
            task = _Task(spec)
            self._tasks[task.id] = task
            return task.id

    def update(self, task_id: str, spec: TaskSpec) -> None:
        spec.validate()
        with self._lock:
            task = self._tasks[task_id]
            if task.status in ACTIVE:
                raise ValueError("请先停止任务再编辑")
            task.spec = spec
            self._reset(task)
            task.status = "ready"

    def restore(self, task_id: str, state: dict) -> None:
        """Restore history without reviving a process or auto-starting a transfer."""
        with self._lock:
            task = self._tasks[task_id]
            if task.status in ACTIVE:
                raise ValueError('进行中的任务不能恢复历史状态')
            status = state.get('status', 'ready')
            if not isinstance(status, str):
                status = 'ready'
            interrupted = isinstance(status, str) and status in ACTIVE
            task.status = 'cancelled' if interrupted else status if status in {'ready', 'success', 'failed', 'cancelled'} else 'ready'
            total, done = state.get('total_bytes'), state.get('transferred_bytes', 0)
            task.total_bytes = total if type(total) is int and total >= 0 else None
            task.transferred_bytes = done if type(done) is int and done >= 0 else 0
            if task.total_bytes is not None:
                task.transferred_bytes = min(task.transferred_bytes, task.total_bytes)
            for name in ('started_at', 'ended_at'):
                value = state.get(name)
                setattr(task, name, value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None)
            task.return_code = state.get('return_code') if type(state.get('return_code')) is int else None
            task.current_file = state.get('current_file') if isinstance(state.get('current_file'), str) else ''
            task.error = state.get('error') if isinstance(state.get('error'), str) else ''
            task.speed, task.eta = 0, '0:00:00' if task.status == 'success' else '—'
            if interrupted:
                task.error = '上次退出时任务尚未完成，可重新开始。'
            if task.status != 'ready':
                self._log(task, f'已恢复任务记录：{task.status}。历史终端输出不跨会话保存。')

    def set_password(self, task_id: str, password: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            if task.status not in ACTIVE:
                task.spec = replace(task.spec, password=password)

    def remove(self, task_id: str) -> None:
        with self._lock:
            if self._tasks[task_id].status in ACTIVE:
                raise ValueError("请先停止任务再移除")
            del self._tasks[task_id]

    def snapshots(self) -> list[Snapshot]:
        with self._lock:
            return [task.snapshot() for task in self._tasks.values()]

    def get(self, task_id: str) -> Snapshot | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.snapshot() if task else None

    def logs_since(self, task_id: str, sequence: int) -> list[tuple[int, str]]:
        with self._lock:
            task = self._tasks.get(task_id)
            return [(n, text) for n, text in task.logs if n > sequence] if task else []

    def set_concurrency(self, count: int) -> None:
        if not 1 <= count <= 8:
            raise ValueError("并发数必须是 1–8")
        with self._lock:
            self._concurrency = count
            self._launch()

    def start(self, task_ids: list[str]) -> None:
        with self._lock:
            if self._closed:
                return
            pending = [self._tasks[i] for i in task_ids if self._tasks[i].status not in ACTIVE]
            # Validate the entire selection before enqueueing any of it.
            for task in pending:
                task.spec.validate()
            for task in pending:
                self._reset(task)
                task.status = "queued"
                self._log(task, "已加入传输队列")
            self._launch()

    @staticmethod
    def _reset(task: _Task) -> None:
        task.cancel.clear()
        task.total_bytes = None
        task.transferred_bytes = 0
        task.speed = 0
        task.eta = "—"
        task.current_file = task.error = ""
        task.return_code = None
        task.started_at = task.ended_at = None

    def stop(self, task_ids: list[str]) -> None:
        with self._lock:
            for task_id in task_ids:
                task = self._tasks.get(task_id)
                if task and task.status in ACTIVE:
                    task.cancel.set()
                    task.status = "cancelling" if task_id in self._running else "cancelled"
                    if task.status == "cancelled":
                        task.ended_at = time.time()
                        self._log(task, "排队任务已停止")

    @staticmethod
    def _target(spec: TaskSpec) -> tuple:
        remote = spec.direction == "upload"
        destination = spec.destination if remote else local_path(spec.destination)
        return (spec.host if remote else "", spec.port if remote else 0,
                spec.username if remote else "", str(PurePosixPath(destination) / PurePosixPath(spec.source).name))

    def _launch(self) -> None:
        if self._closed:
            return
        targets = {self._target(self._tasks[i].spec) for i in self._running}
        for task in self._tasks.values():
            if len(self._running) >= self._concurrency:
                break
            target = self._target(task.spec)
            if task.status != "queued" or target in targets:
                continue
            self._running.add(task.id)
            targets.add(target)
            task.status = "scanning"
            task.started_at = time.time()
            thread = threading.Thread(target=self._worker, args=(task,), daemon=True, name=f"rsync-{task.id[:8]}")
            self._threads.add(thread)
            thread.start()

    def _log(self, task: _Task, text: str) -> None:
        with self._lock:
            for password in [task.spec.password, *(hop.password for hop in task.spec.jump_hosts)]:
                if password:
                    text = text.replace(password, "******")
            task.sequence += 1
            task.logs.append((task.sequence, f"[{time.strftime('%H:%M:%S')}] {text}"))

    def _parse(self, task: _Task, line: str, scanning: bool) -> None:
        self._log(task, line)
        with self._lock:
            if match := TOTAL.match(line):
                task.total_bytes = int(match[1].replace(",", ""))
            elif not scanning and (match := PROGRESS.match(line)):
                task.transferred_bytes = max(task.transferred_bytes, int(match[1].replace(",", "")))
                unit = match[4][:-1].upper()
                factor = {"": 1, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}[unit]
                task.speed = float(match[3]) * factor
                task.eta = match[5]
            elif line.startswith(FILE_MARKER):
                task.current_file = line[len(FILE_MARKER):]

    def _worker(self, task: _Task) -> None:
        outcome = "failed"
        try:
            if not shutil.which(self.rsync):
                raise RuntimeError("未找到 rsync，请先安装 rsync 3.1 或更新版本")
            if task.spec.direction != "local" and not shutil.which("ssh"):
                raise RuntimeError("未找到 ssh，请先安装 OpenSSH 客户端")
            # Create the common local destination once, even for nested missing paths.
            if task.spec.direction != "upload":
                Path(local_path(task.spec.destination)).mkdir(parents=True, exist_ok=True)
            passwords = {hop.id: hop.password for hop in task.spec.jump_hosts if hop.password}
            if task.spec.password:
                passwords[task.spec.server_id or "target"] = task.spec.password
            with PasswordBroker(passwords if task.spec.direction != "local" else "") as auth_env:
                auth_env["RSYNC_DESK_AUTH_ID"] = task.spec.server_id or "target"
                self._log(task, "正在预扫描，计算本次需要更新的数据量……")
                result = self._execute(task, dry_run=True, auth_env=auth_env)
                if task.cancel.is_set():
                    return
                if result != 0:
                    raise RuntimeError(f"预扫描失败（rsync 退出码 {result}），请查看下方日志")
                with self._lock:
                    if task.total_bytes is None:
                        raise RuntimeError("无法读取 rsync 统计信息，请使用 rsync 3.1 或更新版本")
                    if not task.cancel.is_set():
                        task.status = "running"
                # Always run: zero-byte files, empty directories and metadata still need syncing.
                result = self._execute(task, dry_run=False, auth_env=auth_env)
                if task.cancel.is_set():
                    return
                if result != 0:
                    raise RuntimeError(f"同步失败（rsync 退出码 {result}），请查看终端日志后重试")
                with self._lock:
                    task.transferred_bytes = task.total_bytes or 0
                    task.eta = "0:00:00"
                    outcome = "success"
                self._log(task, "传输完成")
        except Exception as exc:  # noqa: BLE001 -- worker boundary must always publish a terminal status
            with self._lock:
                task.error = str(exc).replace(task.spec.password, "******") if task.spec.password else str(exc)
            self._log(task, task.error)
        finally:
            with self._lock:
                if task.cancel.is_set():
                    task.status = "cancelled"
                    self._log(task, "任务已停止；再次开始会重新扫描，部分文件已由 rsync 保留")
                else:
                    task.status = outcome
                task.speed = 0
                task.ended_at = time.time()
                self._running.discard(task.id)
                self._threads.discard(threading.current_thread())
                self._launch()

    def _execute(self, task: _Task, *, dry_run: bool, auth_env: dict[str, str]) -> int:
        if task.cancel.is_set():
            return -signal.SIGTERM
        command = build_command(task.spec, dry_run=dry_run, rsync=self.rsync)
        self._log(task, "$ " + shlex.join(command))
        env = os.environ.copy()
        env.update(auth_env)
        env["LC_ALL"] = "C"
        # Locale is fixed for parseable statistics; filenames are decoded as UTF-8.
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=env, start_new_session=True)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        terminated_at = None
        killed = False

        def consume(text: str, final: bool = False) -> None:
            nonlocal pending
            pieces = re.split(r"[\r\n]", pending + text)
            pending = pieces.pop()
            for line in pieces:
                if line.strip():
                    self._parse(task, line, dry_run)
            if final and pending.strip():
                self._parse(task, pending, dry_run)
                pending = ""
            # Bound memory even if a subprocess emits an unusually long line.
            if len(pending) > 65536:
                self._parse(task, pending, dry_run)
                pending = ""

        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map() or process.poll() is None:
                    if task.cancel.is_set():
                        now = time.monotonic()
                        if terminated_at is None:
                            self._signal_group(process, signal.SIGTERM)
                            terminated_at = now
                        elif now - terminated_at > 2 and not killed:
                            self._signal_group(process, signal.SIGKILL)
                            killed = True
                    for key, _ in selector.select(timeout=0.15):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if chunk:
                            consume(decoder.decode(chunk))
                        else:
                            selector.unregister(key.fileobj)
            consume(decoder.decode(b"", final=True), final=True)
            result = process.wait()
            with self._lock:
                task.return_code = result
            return result
        finally:
            if process.poll() is None:
                self._signal_group(process, signal.SIGKILL)
                process.wait()
            process.stdout.close()

    @staticmethod
    def _signal_group(process: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def shutdown(self, timeout: float = 5) -> None:
        with self._lock:
            self._closed = True
            self.stop(list(self._tasks))
            threads = list(self._threads)
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
