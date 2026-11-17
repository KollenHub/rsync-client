"""Expand wildcard source paths into concrete per-match transfer tasks.

`*` matches one path segment, `?` a single character, `[seq]` a character
class, and a standalone `**` any number of directory levels (including zero).
Matching runs in Python — locally through scandir, remotely through SFTP
listings — so every match becomes an ordinary concrete task instead of rsync
filter rules: each task gets its own fast scan, progress, retry and grouping.
"""
from __future__ import annotations

import fnmatch
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from .models import ServerProfile, TaskSpec, local_path, split_wildcard

MAX_MATCHES = 10_000
Entry = tuple[str, bool]  # (name, is_directory)
Progress = Callable[[tuple[int, str]], None]  # publish takes exactly one payload


@dataclass(frozen=True)
class Expansion:
    root: str
    pattern: str
    matches: tuple[str, ...]

    def relative(self, match: str) -> str:
        return str(PurePosixPath(match).relative_to(PurePosixPath(self.root)))


def _join(folder: str, name: str) -> str:
    return folder.rstrip("/") + "/" + name


def _segments(pattern: str) -> tuple[list[str], bool]:
    """Split a relative pattern into segments; a trailing / means directories only."""
    directories_only = pattern.endswith("/")
    parts = [part for part in pattern.rstrip("/").split("/") if part]
    if not parts or not all(parts):
        raise ValueError("通配符模式无效，请检查路径分段")
    if sum(part == "**" for part in parts) > 4:
        raise ValueError("递归通配符 ** 最多使用 4 段")
    return parts, directories_only


def expand_segments(base: str, pattern: str, list_children: Callable[[str], list[Entry]],
                    on_progress: Progress | None = None,
                    cancel: threading.Event | None = None) -> list[str]:
    """Match `pattern` (relative to `base`) using `list_children` for directory reads."""
    segments, directories_only = _segments(pattern)
    matches: list[str] = []

    def walk(folder: str, pending: list[str]) -> None:
        if cancel and cancel.is_set():
            raise ValueError("解析已取消")
        head, rest = pending[0], pending[1:]
        if head == "**":
            if rest:
                walk(folder, rest)  # ** may match zero levels
            for name, is_dir in list_children(folder):
                if is_dir:
                    walk(_join(folder, name), pending)
            return
        for name, is_dir in list_children(folder):
            if cancel and cancel.is_set():
                raise ValueError("解析已取消")
            if not fnmatch.fnmatchcase(name, head):
                continue
            child = _join(folder, name)
            if rest:
                if is_dir:
                    walk(child, rest)
            elif is_dir or not directories_only:
                if len(matches) >= MAX_MATCHES:
                    raise ValueError(f"匹配数量超过上限 {MAX_MATCHES}，请缩小模式范围")
                matches.append(child)
                if on_progress:
                    on_progress((len(matches), child))

    walk(base, segments)
    return matches


def expand_local(path: str, on_progress: Progress | None = None,
                 cancel: threading.Event | None = None) -> Expansion:
    root, pattern = split_wildcard(path)
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        raise ValueError(f"路径前缀不存在：{root}")

    def list_children(folder: str) -> list[Entry]:
        try:
            with os.scandir(folder) as iterator:
                return [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in iterator]
        except OSError as exc:
            raise ValueError(f"无法读取目录 {folder}：{exc}") from exc

    matches = expand_segments(root, pattern, list_children, on_progress, cancel)
    if not matches:
        raise ValueError(f"没有匹配项：{path}")
    return Expansion(root, pattern, tuple(matches))


def expand_remote(profile: ServerProfile, path: str, service, jumps: tuple[ServerProfile, ...] = (),
                  on_progress: Progress | None = None,
                  cancel: threading.Event | None = None) -> Expansion:
    root, pattern = split_wildcard(path)
    if not root.startswith("/"):
        raise ValueError("远端路径请使用绝对路径，例如 /map_data/…")

    def list_children(folder: str) -> list[Entry]:
        _, entries = service.list_directory(profile, folder, jumps, cancel=cancel)
        return [(entry.name, entry.is_directory) for entry in entries]

    directory, _ = service.list_directory(profile, root, jumps, cancel=cancel)  # validates + normalizes
    matches = expand_segments(directory, pattern, list_children, on_progress, cancel)
    if not matches:
        raise ValueError(f"没有匹配项：{path}")
    return Expansion(directory, pattern, tuple(matches))


def build_group_specs(expansion: Expansion, destination_root: str, *, direction: str, local: bool,
                      compression: bool = True, bandwidth_kib: int = 0, excludes: tuple[str, ...] = (),
                      group_name: str = "") -> list[TaskSpec]:
    """Turn an expansion into one task per match, preserving the relative tree.

    A match `root/a/b/Pose` maps to `destination_root/a/b/Pose` on disk, so
    every task is a plain directory copy with the usual basename semantics.
    """
    group_id = uuid.uuid4().hex
    dest_root = destination_root.rstrip("/") or "/"
    dest_is_local = direction in ("local", "download")
    specs = []
    for match in expansion.matches:
        rel = PurePosixPath(expansion.relative(match))
        parent = str(rel.parent)
        destination = dest_root if parent == "." else f"{dest_root}/{parent}"
        if dest_is_local:
            destination = local_path(destination)
        source = local_path(match) if local else match
        specs.append(TaskSpec(
            name=f"{group_name} / {rel}" if group_name else str(rel),
            source=source, destination=destination, direction=direction,
            compression=compression, bandwidth_kib=bandwidth_kib,
            excludes=tuple(excludes),
            group_id=group_id, group_name=group_name))
    return specs
