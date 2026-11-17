from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from rsync_client import engine
from rsync_client.engine import TaskManager, build_command, summarize
from rsync_client.models import TaskSpec


@unittest.skipUnless(shutil.which("rsync"), "rsync is required for integration tests")
class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = TaskManager()

    def tearDown(self):
        self.manager.shutdown()
        self.temp.cleanup()

    def wait_for(self, key, statuses, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = self.manager.get(key)
            if snapshot.status in statuses:
                return snapshot
            time.sleep(0.025)
        self.fail(f"Timed out waiting for {statuses}; got {snapshot}")

    def test_mixed_batch_unicode_excludes_links_and_incremental_rerun(self):
        folder = self.root / "相册 和文档"
        folder.mkdir()
        content = os.urandom(220_000)
        (folder / "数据.bin").write_bytes(content)
        (folder / "空文件").touch()
        (folder / "空目录").mkdir()
        (folder / "ignored.tmp").write_text("excluded")
        (folder / "link").symlink_to("数据.bin")
        other = self.root / "separate ; $(echo).txt"
        other.write_text("standalone")
        destination = self.root / "new" / "destination"
        first = self.manager.add(TaskSpec("相册", str(folder), str(destination), excludes=("*.tmp",)))
        second = self.manager.add(TaskSpec("文件", str(other), str(destination)))
        self.manager.start([first, second])
        for key in (first, second):
            result = self.wait_for(key, {"success", "failed"})
            self.assertEqual(result.status, "success", result.error)
            self.assertEqual(result.percent, 100)
            self.assertEqual(result.transferred_bytes, result.total_bytes)
        copied = destination / folder.name
        self.assertEqual((copied / "数据.bin").read_bytes(), content)
        self.assertTrue((copied / "空目录").is_dir())
        self.assertTrue((copied / "空文件").exists())
        self.assertTrue((copied / "link").is_symlink())
        self.assertFalse((copied / "ignored.tmp").exists())
        self.assertTrue((destination / other.name).exists())
        summary = summarize(self.manager.snapshots())
        self.assertEqual(summary.percent, 100)
        self.assertEqual(summary.total_bytes, len(content) + len(b"standalone"))
        logs = "\n".join(text for _, text in self.manager.logs_since(first, 0))
        self.assertIn("@@RSYNC_FILE@@", logs)
        self.assertIn("传输完成", logs)
        self.manager.start([first, second])
        for key in (first, second):
            result = self.wait_for(key, {"success", "failed"})
            self.assertEqual(result.status, "success", result.error)
            self.assertEqual(result.total_bytes, 0)

    def test_empty_directory_and_zero_byte_file_still_transferred(self):
        folder = self.root / "empty"
        folder.mkdir()
        (folder / "zero").touch()
        key = self.manager.add(TaskSpec("空目录", str(folder), str(self.root / "out")))
        self.manager.start([key])
        snapshot = self.wait_for(key, {"success", "failed"})
        self.assertEqual(snapshot.status, "success", snapshot.error)
        self.assertEqual(snapshot.total_bytes, 0)
        self.assertTrue((self.root / "out" / "empty" / "zero").exists())

    def test_stop_queued_and_running_then_retry(self):
        self.manager.set_concurrency(1)
        source = self.root / "large.bin"
        source.write_bytes(os.urandom(2 * 1024 * 1024))
        spec = TaskSpec("large", str(source), str(self.root / "out"), bandwidth_kib=128)
        running = self.manager.add(spec)
        queued = self.manager.add(replace(spec, name="queued", destination=str(self.root / "other")))
        self.manager.start([running, queued])
        self.assertEqual(self.manager.get(queued).status, "queued")
        self.manager.stop([queued])
        self.assertEqual(self.manager.get(queued).status, "cancelled")
        self.wait_for(running, {"running"})
        deadline = time.monotonic() + 4
        while self.manager.get(running).transferred_bytes == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertGreater(self.manager.get(running).transferred_bytes, 0)
        self.assertGreater(self.manager.get(running).percent, 0)
        self.assertLess(self.manager.get(running).percent, 100)
        self.manager.stop([running])
        result = self.wait_for(running, {"cancelled"}, timeout=5)
        self.assertLess(result.percent, 100)
        self.assertFalse((self.root / "other").exists())
        self.manager.update(running, replace(spec, bandwidth_kib=0))
        self.manager.start([running])
        result = self.wait_for(running, {"success", "failed"})
        self.assertEqual(result.status, "success", result.error)
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(),
                         hashlib.sha256((self.root / "out" / source.name).read_bytes()).digest())

    def test_concurrency_limit_and_same_destination_serialization(self):
        source = self.root / "large.bin"
        source.write_bytes(os.urandom(1024 * 1024))
        base = TaskSpec("one", str(source), str(self.root / "out"), bandwidth_kib=64)
        keys = [self.manager.add(base), self.manager.add(replace(base, name="same-target")),
                self.manager.add(replace(base, name="other-target", destination=str(self.root / "other")))]
        self.manager.start(keys)
        self.assertEqual(self.manager.get(keys[1]).status, "queued")
        self.assertIn(self.manager.get(keys[0]).status, {"scanning", "running"})
        self.assertIn(self.manager.get(keys[2]).status, {"scanning", "running"})
        self.manager.stop(keys)
        for key in keys:
            self.wait_for(key, {"cancelled"})

    def test_wildcard_source_transfers_matching_tree(self):
        root = self.root / "lvio"
        for folder in (root / "Pose", root / "t1" / "Pose", root / "t1" / "deep" / "t2"):
            folder.mkdir(parents=True)
        (root / "Pose" / "f0").write_text("0")
        (root / "t1" / "Pose" / "f1").write_text("1")
        (root / "t1" / "deep" / "t2" / "Pose").write_text("2")  # a file named Pose
        (root / "t1" / "skip.txt").write_text("skip")
        out = self.root / "out"
        spec = TaskSpec("通配", str(root) + "/**/Pose", str(out))
        command = build_command(spec)
        self.assertEqual(command[-2], str(root) + "/")
        key = self.manager.add(spec)
        self.manager.start([key])
        result = self.wait_for(key, {"success", "failed"})
        self.assertEqual(result.status, "success", result.error)
        self.assertTrue((out / "Pose" / "f0").exists())
        self.assertTrue((out / "t1" / "Pose" / "f1").exists())
        self.assertTrue((out / "t1" / "deep" / "t2" / "Pose").exists())
        self.assertFalse((out / "t1" / "skip.txt").exists())

    def test_wildcard_source_matches_files_by_name(self):
        root = self.root / "logs"
        (root / "a").mkdir(parents=True)
        (root / "a" / "x.info").write_text("x")
        (root / "a" / "y.txt").write_text("y")
        out = self.root / "out2"
        key = self.manager.add(TaskSpec("文件名", str(root) + "/**/*.info", str(out)))
        self.manager.start([key])
        result = self.wait_for(key, {"success", "failed"})
        self.assertEqual(result.status, "success", result.error)
        self.assertTrue((out / "a" / "x.info").exists())
        self.assertFalse((out / "a" / "y.txt").exists())

    def test_scan_heartbeat_logs_while_silent(self):
        source = self.root / "source"
        source.touch()
        fake = self.root / "slow-rsync"
        fake.write_text("#!/usr/bin/env python3\nimport sys, time\ntime.sleep(0.7)\n"
                        "if '--dry-run' in sys.argv:\n print('Total transferred file size: 1024 bytes')\n")
        fake.chmod(0o700)
        self.manager.rsync = str(fake)
        previous = engine.SCAN_HEARTBEAT_SECONDS
        engine.SCAN_HEARTBEAT_SECONDS = 0.2
        try:
            key = self.manager.add(TaskSpec("slow", str(source), str(self.root / "out")))
            self.manager.start([key])
            result = self.wait_for(key, {"success", "failed"})
            self.assertEqual(result.status, "success", result.error)
            logs = "\n".join(text for _, text in self.manager.logs_since(key, 0))
            self.assertIn("仍在扫描", logs)
            beat = next(line for line in logs.splitlines() if "仍在扫描" in line)
            elapsed = int(beat.split("已持续 ")[1].split(" 秒")[0])
            self.assertGreaterEqual(elapsed, 0)
        finally:
            engine.SCAN_HEARTBEAT_SECONDS = previous

    def test_nonzero_exit_is_failure_and_password_is_redacted(self):
        source = self.root / "source"
        source.touch()
        fake = self.root / "fake-rsync"
        fake.write_text("#!/usr/bin/env python3\nimport sys\n"
                        "if '--dry-run' in sys.argv:\n print('Total transferred file size: 4096 bytes')\n"
                        "else:\n print('password=secret-123 denied'); sys.exit(23)\n")
        fake.chmod(0o700)
        self.manager.rsync = str(fake)
        key = self.manager.add(TaskSpec("failure", str(source), str(self.root / "out"), password="secret-123"))
        self.manager.start([key])
        result = self.wait_for(key, {"failed", "success"})
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.return_code, 23)
        self.assertLess(result.percent, 100)
        logs = "\n".join(text for _, text in self.manager.logs_since(key, 0))
        self.assertNotIn("secret-123", logs)
        self.assertIn("******", logs)

    def test_sync_contents_mode_copies_directory_contents(self):
        folder = self.root / "相册"
        (folder / "nested").mkdir(parents=True)
        (folder / "数据.bin").write_bytes(os.urandom(50_000))
        (folder / "nested" / "deep.txt").write_text("内容")
        spec = TaskSpec("内容直传", str(folder), str(self.root / "out"), sync_contents=True)
        command = build_command(spec)
        self.assertEqual(command[-2], str(folder) + "/")
        self.assertEqual(command[-1], str(self.root / "out") + "/")
        key = self.manager.add(spec)
        self.manager.start([key])
        result = self.wait_for(key, {"success", "failed"})
        self.assertEqual(result.status, "success", result.error)
        self.assertTrue((self.root / "out" / "数据.bin").exists())
        self.assertTrue((self.root / "out" / "nested" / "deep.txt").exists())
        self.assertFalse((self.root / "out" / folder.name).exists())
        # Without the option the directory itself is created inside the destination.
        plain = build_command(TaskSpec("普通", str(folder), str(self.root / "out")))
        self.assertEqual(plain[-2], str(folder))

    def test_missing_rsync_and_atomic_input_validation(self):
        source = self.root / "source"
        source.touch()
        first = self.manager.add(TaskSpec("valid", str(source), str(self.root / "out")))
        bad = self.manager.add(TaskSpec("bad", str(self.root / "missing"), str(self.root / "out")), check_paths=False)
        with self.assertRaises(ValueError):
            self.manager.start([first, bad])
        self.assertEqual(self.manager.get(first).status, "ready")
        self.manager.rsync = str(self.root / "not-installed")
        self.manager.start([first])
        self.assertEqual(self.wait_for(first, {"failed"}).status, "failed")


class CommandTests(unittest.TestCase):
    def test_remote_command_uses_port_protected_args_and_no_password(self):
        spec = TaskSpec("upload", "/tmp/name ; $(command)", "/backup/空 格", direction="upload",
                        host="2001:db8::1", username="test", port=2222, password="secret", excludes=("*.tmp",))
        command = build_command(spec)
        self.assertNotIn("secret", shlex.join(command))
        ssh = shlex.split(command[command.index("-e") + 1])
        self.assertEqual(ssh[ssh.index("-p") + 1], "2222")
        self.assertEqual(ssh[ssh.index("-l") + 1], "test")
        self.assertIn("StrictHostKeyChecking=accept-new", ssh)
        self.assertIn("-s", command)
        self.assertEqual(command[-1], "[2001:db8::1]:/backup/空 格/")
        self.assertEqual(command[-2], "/tmp/name ; $(command)")

    def test_remote_internal_command_runs_rsync_on_the_server(self):
        spec = TaskSpec("remote", "/srv/数据 ; 目录", "/srv/backup", direction="remote",
                        host="203.0.113.7", username="test", port=2222, password="secret", excludes=("*.tmp",))
        spec.validate(check_paths=False)
        command = build_command(spec)
        self.assertNotIn("secret", shlex.join(command))
        self.assertEqual(command[0], "ssh")
        self.assertEqual(command[command.index("-p") + 1], "2222")
        self.assertEqual(command[command.index("-l") + 1], "test")
        self.assertEqual(command[command.index("--") + 1], "203.0.113.7")
        wrapper = command[-1]
        self.assertTrue(wrapper.startswith("echo RSYNC_PID=$$; exec rsync"))
        remote_rsync = shlex.split(wrapper.split("; exec ", 1)[1])
        self.assertEqual(remote_rsync[0], "rsync")
        self.assertIn("-z", remote_rsync)
        self.assertEqual(remote_rsync[-2:], ["/srv/数据 ; 目录", "/srv/backup/"])
        contents = build_command(replace(spec, sync_contents=True))
        self.assertIn(repr("/srv/数据 ; 目录/"), contents[-1])  # trailing slash: copy contents

    def test_stage_error_exit_code_hints(self):
        remote = TaskSpec("r", "/a", "/b", direction="remote", host="s", username="u")
        self.assertIn("未安装 rsync", TaskManager._stage_error(remote, 127, scanning=True))
        self.assertNotIn("未安装", TaskManager._stage_error(remote, 23, scanning=True))
        upload = TaskSpec("u", "/tmp/x", "/b", direction="upload", host="s", username="u")
        self.assertIn("退出码 12", TaskManager._stage_error(upload, 12, scanning=False))
        self.assertIn("可能是远端服务器未安装", TaskManager._stage_error(upload, 12, scanning=False))

    def test_wildcard_source_expands_into_filter_rules(self):
        spec = TaskSpec("wild", "/data/lvio_task/**/Pose", "/backup/pose", direction="download",
                        host="server.example", username="alice", excludes=(".git",))
        spec.validate(check_paths=False)
        command = build_command(spec)
        self.assertIn("-m", command)
        for rule in ("--include=*/", "--include=/Pose", "--include=/Pose/***",
                     "--include=/**/Pose", "--include=/**/Pose/***", "--exclude=*"):
            self.assertIn(rule, command)
        # user excludes keep priority over the generated traversal rule
        self.assertLess(command.index(".git"), command.index("--include=*/"))
        tail = command[command.index("--") + 1:]
        self.assertEqual(tail, ["server.example:/data/lvio_task/", "/backup/pose/"])

    def test_wildcard_name_and_single_level_patterns(self):
        spec = TaskSpec("name", "/data/*/*.info", "/backup/files", direction="remote",
                        host="server.example", username="alice")
        spec.validate(check_paths=False)
        remote_rsync = shlex.split(build_command(spec)[-1].split("; exec ", 1)[1])
        self.assertIn("--include=/*/*.info", remote_rsync)
        self.assertIn("--include=/*/*.info/***", remote_rsync)
        self.assertEqual(remote_rsync[-2:], ["/data/", "/backup/files/"])

    def test_wildcard_validation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "lvio"
            (root / "t1").mkdir(parents=True)
            TaskSpec("ok", str(root) + "/**/Pose", str(Path(td) / "out")).validate()
            TaskSpec("ok-file", str(root) + "/**/*.info", str(Path(td) / "out")).validate()
            with self.assertRaises(ValueError):  # literal prefix must exist
                TaskSpec("bad", str(Path(td) / "missing") + "/**/Pose", str(Path(td) / "out")).validate()
            with self.assertRaises(ValueError):  # destination cannot contain wildcards
                TaskSpec("bad", str(root) + "/**/Pose", str(Path(td) / "out/*")).validate()
            for destination in (root, root / "nested"):  # wildcard source behaves like contents mode
                with self.subTest(destination=destination), self.assertRaises(ValueError):
                    TaskSpec("bad", str(root) + "/**/Pose", str(destination)).validate()
        with self.assertRaises(ValueError):  # too many recursive segments
            TaskSpec("bad", "/a/**/**/**/**/**/Pose", "/backup").validate(check_paths=False)
        with self.assertRaises(ValueError):  # empty path segment
            TaskSpec("bad", "/a/**//Pose", "/backup").validate(check_paths=False)
        with self.assertRaises(ValueError):  # remote-internal wildcard overlap
            TaskSpec("bad", "/srv/data/**/Pose", "/srv/data", direction="remote",
                     host="s", username="u").validate(check_paths=False)
        TaskSpec("ok", "/srv/data/**/Pose", "/srv/backup", direction="remote",
                 host="s", username="u").validate(check_paths=False)

    def test_scan_streams_found_items_as_progress(self):
        spec = TaskSpec("s", "/tmp/x", "/tmp/out")
        spec.validate(check_paths=False)
        manager = TaskManager()
        task = engine._Task(spec)
        for name in ("a/Pose/x.info", "b.info"):
            TaskManager._parse(manager, task, engine.FILE_MARKER + name, True)
        self.assertEqual(task.scanned_files, 2)
        self.assertEqual(task.snapshot().scanned_files, 2)
        self.assertIn("已发现 2 项", task.current_file)
        TaskManager._parse(manager, task, engine.FILE_MARKER + "c.info", False)
        self.assertEqual(task.current_file, "c.info")
        dry = build_command(spec, dry_run=True)
        self.assertIn("-v", dry)
        self.assertIn("--out-format=@@RSYNC_FILE@@%n", dry)
        manager.shutdown()

    def test_sync_contents_rejects_same_or_nested_destination(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "data"
            source.mkdir()
            for destination in (source, source / "nested"):
                with self.subTest(destination=destination), self.assertRaises(ValueError):
                    TaskSpec("bad", str(source), str(destination), sync_contents=True).validate()
            TaskSpec("ok", str(source), str(Path(td) / "out"), sync_contents=True).validate()
        with self.assertRaises(ValueError):
            TaskSpec("remote", "/srv/data", "/srv/data", direction="remote",
                     host="s", username="u", sync_contents=True).validate(check_paths=False)
        with self.assertRaises(ValueError):
            TaskSpec("remote", "/srv/data", "/srv/data/sub", direction="remote",
                     host="s", username="u", sync_contents=True).validate(check_paths=False)

    def test_reject_overlapping_local_paths(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "source"
            source.mkdir()
            for destination in (source, source / "nested", source.parent):
                with self.subTest(destination=destination), self.assertRaises(ValueError):
                    TaskSpec("bad", str(source), str(destination)).validate()

    def test_remote_internal_paths_must_be_absolute_and_non_overlapping(self):
        args = {"direction": "remote", "host": "server.example", "username": "alice"}
        with self.assertRaises(ValueError):
            TaskSpec("relative", "data", "/srv/backup", **args).validate(check_paths=False)
        with self.assertRaises(ValueError):
            TaskSpec("relative", "/srv/data", "backup", **args).validate(check_paths=False)
        for source, destination in (("/srv/data", "/srv/data"), ("/srv/data", "/srv/data/backup"),
                                    ("/srv/data", "/srv"), ("/", "/srv")):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                TaskSpec("overlap", source, destination, **args).validate(check_paths=False)
        TaskSpec("valid", "/srv/data", "/srv/backup", **args).validate(check_paths=False)


if __name__ == "__main__":
    unittest.main()
