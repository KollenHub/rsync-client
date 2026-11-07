from __future__ import annotations

import ipaddress
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath

STATUS_LABELS = {
    "ready": "待开始", "queued": "排队中", "scanning": "计算大小", "running": "传输中",
    "cancelling": "正在停止", "success": "已完成", "failed": "失败", "cancelled": "已停止",
}
ACTIVE = frozenset({"queued", "scanning", "running", "cancelling"})
DIRECTIONS = {"local": "本地同步", "upload": "上传到远端", "download": "从远端下载", "remote": "远端内部同步"}


@dataclass(frozen=True)
class TaskSpec:
    name: str
    source: str
    destination: str
    direction: str = "local"
    host: str = ""
    username: str = ""
    port: int = 22
    password: str = field(default="", repr=False, compare=False)
    identity_file: str = ""
    credential_id: str = ""
    server_id: str = ""
    jump_hosts: tuple[ServerProfile, ...] = field(default=(), repr=False)
    compression: bool = True
    bandwidth_kib: int = 0
    excludes: tuple[str, ...] = ()
    sync_contents: bool = False

    def validate(self, *, check_paths: bool = True) -> None:
        for key in ("name", "source", "destination", "direction", "host", "username", "password", "identity_file", "credential_id", "server_id"):
            if not isinstance(getattr(self, key), str):
                raise TypeError(f"配置字段 {key} 必须为文本")
        if not isinstance(self.excludes, (tuple, list)):
            raise TypeError("排除规则必须是列表")
        if type(self.sync_contents) is not bool:
            raise TypeError("内容直传选项必须为布尔值")
        if self.direction not in DIRECTIONS:
            raise ValueError("不支持的同步方向")
        if not self.name.strip() or not self.source.strip() or not self.destination.strip():
            raise ValueError("任务名称、源路径和目标目录不能为空")
        for value in (self.source, self.destination, self.host, self.username, self.identity_file):
            if any(c in value for c in ("\0", "\r", "\n")):
                raise ValueError("路径和连接信息不能包含换行或空字符")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("SSH 端口必须是 1–65535 之间的整数")
        if type(self.bandwidth_kib) is not int or self.bandwidth_kib < 0:
            raise ValueError("限速必须是大于等于 0 的整数（KiB/s）")
        if any(c in self.password for c in ("\0", "\r", "\n")):
            raise ValueError("SSH 密码不能包含换行或空字符")
        if self.direction != "local":
            try:
                ipaddress.ip_address(self.host)
            except ValueError:
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", self.host):
                    raise ValueError("请输入有效的主机名、IPv4 或 IPv6 地址，不含协议及端口")
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", self.username):
                raise ValueError("请输入有效的远端用户名")
            remote_paths = {"download": (self.source,), "upload": (self.destination,),
                            "remote": (self.source, self.destination)}.get(self.direction, ())
            for remote_path in remote_paths:
                if not remote_path.startswith("/"):
                    raise ValueError("远端路径请使用绝对路径，例如 /home/user/data")
            if self.identity_file and check_paths and not Path(self.identity_file).expanduser().is_file():
                raise ValueError("SSH 私钥文件不存在")
        if check_paths and self.direction in ("local", "upload"):
            source = Path(self.source).expanduser()
            if not source.exists() and not source.is_symlink():
                raise ValueError(f"源文件或目录不存在：{source}")
            if self.direction == "local":
                destination = Path(self.destination).expanduser().resolve()
                resolved = source.resolve()
                if self.sync_contents:
                    # Contents land directly in the destination directory itself.
                    if resolved == destination:
                        raise ValueError("内容直传需要与源不同的目标目录")
                    if source.is_dir() and resolved in destination.parents:
                        raise ValueError("目标目录不能位于源目录内部")
                else:
                    # Each source keeps its basename in the common destination directory.
                    target = destination / source.name
                    if resolved == destination or resolved == target:
                        raise ValueError("目标目录与源路径重叠，请选择其他目录")
                    if source.is_dir() and resolved in destination.parents:
                        raise ValueError("目标目录不能位于源目录内部")
        if check_paths and self.direction in ("local", "download"):
            destination = Path(self.destination).expanduser()
            if destination.exists() and not destination.is_dir():
                raise ValueError("目标路径必须是目录")
        if self.direction == "remote":
            # Paths live on the server; compare them lexically to forbid self-overlap.
            source_path = PurePosixPath(self.source.rstrip("/") or "/")
            destination_path = PurePosixPath(self.destination.rstrip("/") or "/")
            if self.sync_contents:
                if source_path == destination_path or source_path in destination_path.parents:
                    raise ValueError("内容直传时目标目录不能与源目录相同或位于其内部")
            elif source_path == destination_path or (source_path.name and source_path == destination_path / source_path.name):
                raise ValueError("目标目录与源路径重叠，请选择其他目录")
            elif source_path in destination_path.parents:
                raise ValueError("目标目录不能位于源目录内部")
        if any(not isinstance(pattern, str) or "\0" in pattern for pattern in self.excludes):
            raise ValueError("排除规则无效")
        for hop in self.jump_hosts:
            hop.validate(check_paths=check_paths)

    def public_dict(self) -> dict:
        data = asdict(self)
        data.pop("password")
        data.pop("jump_hosts")
        if self.server_id:
            for key in ("host", "username", "port", "identity_file", "credential_id"):
                data.pop(key)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> TaskSpec:
        # Credentials are never restored from disk, even from manually edited JSON.
        if not isinstance(data, dict):
            raise TypeError("任务配置必须是对象")
        allowed = set(cls.__dataclass_fields__) - {"password", "jump_hosts"}
        values = {key: value for key, value in data.items() if key in allowed}
        if not isinstance(values.get("excludes", []), (list, tuple)):
            raise TypeError("排除规则必须是列表")
        values["excludes"] = tuple(values.get("excludes", ()))
        spec = cls(**values)
        spec.validate(check_paths=False)
        return spec


@dataclass(frozen=True)
class ServerProfile:
    name: str
    host: str
    username: str
    port: int = 22
    password: str = field(default="", repr=False, compare=False)
    identity_file: str = ""
    credential_id: str = ""
    initial_directory: str = "/"
    jump_server_id: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def validate(self, *, check_paths: bool = True) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("服务器标识不能为空")
        if self.name in {"本机", "直接连接"}:
            raise ValueError("请使用其他服务器名称")
        TaskSpec(self.name, "/placeholder", "/", direction="upload", host=self.host,
                 username=self.username, port=self.port, password=self.password,
                 identity_file=self.identity_file, credential_id=self.credential_id).validate(check_paths=False)
        if not isinstance(self.initial_directory, str) or not self.initial_directory.startswith("/"):
            raise ValueError("服务器初始目录必须是绝对路径")
        if any(c in self.initial_directory for c in ("\0", "\r", "\n")):
            raise ValueError("初始目录不能包含换行或空字符")
        if check_paths and self.identity_file and not Path(self.identity_file).expanduser().is_file():
            raise ValueError("SSH 私钥文件不存在")

    def bind(self, spec: TaskSpec) -> TaskSpec:
        return replace(spec, server_id=self.id, host=self.host, username=self.username, port=self.port,
                       password=self.password, identity_file=self.identity_file, credential_id=self.credential_id)

    def public_dict(self) -> dict:
        data = asdict(self)
        data.pop("password")
        return data

    @classmethod
    def from_dict(cls, data: dict) -> ServerProfile:
        if not isinstance(data, dict):
            raise TypeError("服务器配置必须是对象")
        profile = cls(**{key: value for key, value in data.items()
                         if key in cls.__dataclass_fields__ and key != "password"})
        profile.validate(check_paths=False)
        return profile


def connection_route(profile: ServerProfile, servers: dict[str, ServerProfile]) -> tuple[ServerProfile, ...]:
    route = [profile]
    seen = {profile.id}
    while route[-1].jump_server_id:
        server_id = route[-1].jump_server_id
        if server_id in seen:
            raise ValueError("跳板机连接存在循环，请重新选择")
        if server_id not in servers:
            raise ValueError("所引用的跳板机不存在")
        seen.add(server_id)
        route.append(servers[server_id])
    return tuple(reversed(route))


def format_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024
    return "0 B"


def local_path(value: str) -> str:
    return os.path.abspath(os.path.expanduser(value))
