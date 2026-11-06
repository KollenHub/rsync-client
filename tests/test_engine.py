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

    def test_reject_overlapping_local_paths(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "source"
            source.mkdir()
            for destination in (source, source / "nested", source.parent):
                with self.subTest(destination=destination), self.assertRaises(ValueError):
                    TaskSpec("bad", str(source), str(destination)).validate()


if __name__ == "__main__":
    unittest.main()
