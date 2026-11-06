"""Optional real OpenSSH/rsync integration against a loopback-only SSH fixture."""
from __future__ import annotations

import os
import shlex
import shutil
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from rsync_client.engine import TaskManager
from rsync_client.models import ServerProfile, TaskSpec
from rsync_client.remote import DirectoryReadCancelled, RemoteService
from tests.ssh_fixture import SSHFixture

try:
    import paramiko
except ImportError:
    paramiko = None


@unittest.skipUnless(paramiko and shutil.which("ssh") and shutil.which("rsync"), "optional test needs paramiko, ssh and rsync")
class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture = SSHFixture(self.root)
        self.port = self.fixture.port
        self.password = self.fixture.password
        self.service = RemoteService(self.root / "service_known_hosts")
        # Isolate known_hosts, without modifying the application's host-key policy.
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        wrapper = bin_dir / "ssh"
        wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(shutil.which('ssh'))} "
                           f"-o UserKnownHostsFile={shlex.quote(str(self.root / 'known_hosts'))} "
                           "-o GlobalKnownHostsFile=/dev/null \"$@\"\n")
        wrapper.chmod(0o700)
        self.environment = patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]})
        self.environment.start()
        self.manager = TaskManager()

    def tearDown(self):
        self.manager.shutdown()
        self.service.close()
        self.fixture.close()
        self.environment.stop()
        self.temp.cleanup()

    def transfer(self, spec):
        key = self.manager.add(spec)
        self.manager.start([key])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.manager.get(key)
            if result.status in {"success", "failed", "cancelled"}:
                return result
            time.sleep(0.03)
        self.fail("remote transfer timed out")

    def test_password_upload_download_with_custom_port_and_unicode_paths(self):
        source = self.root / "文件 ; special.txt"
        content = os.urandom(180_000)
        source.write_bytes(content)
        remote = self.root / "远端 目录"
        remote.mkdir()
        args = {"host": "127.0.0.1", "username": "alice", "port": self.port, "password": self.password}
        upload = self.transfer(TaskSpec("upload", str(source), str(remote), direction="upload", **args))
        logs = "\n".join(t for _, t in self.manager.logs_since(upload.id, 0))
        self.assertEqual(upload.status, "success", logs)
        self.assertNotIn(self.password, logs)
        self.assertEqual((remote / source.name).read_bytes(), content)
        download = self.transfer(TaskSpec("download", str(remote / source.name), str(self.root / "download"), direction="download", **args))
        self.assertEqual(download.status, "success", download.error)
        self.assertEqual((self.root / "download" / source.name).read_bytes(), content)
        self.assertEqual(self.fixture.authenticated, 4)  # scan and transfer, for both directions

    def test_wrong_password_is_reported_as_failure(self):
        source = self.root / "source"
        source.touch()
        result = self.transfer(TaskSpec("bad password", str(source), str(self.root / "remote"), direction="upload",
                                        host="127.0.0.1", username="alice", port=self.port, password="wrong-password"))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.return_code, 255)
        self.assertLess(result.percent, 100)
        self.assertEqual(self.fixture.authenticated, 0)

    def test_login_and_sftp_browsing_reuse_authenticated_session(self):
        folder = self.root / "目录"
        folder.mkdir()
        file = self.root / "文件.txt"
        file.write_text("内容")
        profile = ServerProfile("测试服务器", "127.0.0.1", "alice", port=self.port, password=self.password)
        self.assertIn("rsync", self.service.login(profile))
        self.assertTrue(self.service.connected(profile.id))
        directory, entries = self.service.list_directory(profile, str(self.root))
        self.assertEqual(directory, str(self.root))
        self.assertTrue(next(item for item in entries if item.name == folder.name).is_directory)
        self.assertEqual(next(item for item in entries if item.name == file.name).size, len(file.read_bytes()))
        self.assertEqual(self.fixture.authenticated, 1)
        self.service.disconnect(profile.id)
        self.assertFalse(self.service.connected(profile.id))

    def test_one_and_two_hop_transfers_with_distinct_passwords_and_composite_username(self):
        relay = SSHFixture(self.root, username="relay", password="relay-password")
        target = SSHFixture(self.root, username="user@root@10.0.0.8", password="target-password")
        try:
            jump = ServerProfile("跳板机", "127.0.0.1", "alice", port=self.port, password=self.password)
            second = ServerProfile("第二跳", "127.0.0.1", "relay", port=relay.port, password=relay.password)
            destination = ServerProfile("目标机", "127.0.0.1", target.username, port=target.port, password=target.password)
            source = self.root / "经跳板机 ; 文件.bin"
            content = os.urandom(120_000)
            source.write_bytes(content)
            for n, hops in enumerate(((jump,), (jump, second)), 1):
                remote = self.root / f"remote-{n}"
                remote.mkdir()
                upload_spec = destination.bind(TaskSpec("跳板机上传", str(source), str(remote), direction="upload"))
                upload = self.transfer(replace(upload_spec, jump_hosts=hops))
                logs = "\n".join(text for _, text in self.manager.logs_since(upload.id, 0))
                self.assertEqual(upload.status, "success", logs)
                for password in (jump.password, second.password, destination.password):
                    self.assertNotIn(password, logs)
                self.assertEqual((remote / source.name).read_bytes(), content)
                download_spec = destination.bind(TaskSpec("跳板机下载", str(remote / source.name), str(self.root / f"download-{n}"), direction="download"))
                download = self.transfer(replace(download_spec, jump_hosts=hops))
                self.assertEqual(download.status, "success", download.error)
                self.assertEqual((self.root / f"download-{n}" / source.name).read_bytes(), content)
            self.assertIn("rsync", self.service.login(destination, (jump, second)))
            _, entries = self.service.list_directory(destination, str(self.root), (jump, second))
            self.assertIn(source.name, [entry.name for entry in entries])
            self.assertGreater(self.fixture.forwarded, 0)
            self.assertGreater(relay.forwarded, 0)
        finally:
            self.service.close()
            target.close()
            relay.close()

    def test_directory_cache_streaming_refresh_and_lazy_symlink_resolution(self):
        folder = self.root / "大目录"
        folder.mkdir()
        nested = folder / "nested"
        nested.mkdir()
        for n in range(150):
            (folder / f"link-{n}").symlink_to(nested.name)
        (folder / "broken-link").symlink_to("missing")
        file = folder / "file.txt"
        file.write_text("payload")
        profile = ServerProfile("大目录服务器", "127.0.0.1", "alice", port=self.port, password=self.password)
        batches = []
        directory, entries = self.service.list_directory(profile, str(folder), on_batch=batches.append)
        self.assertEqual(len(entries), 153)
        self.assertGreaterEqual(len(batches), 2)
        self.assertEqual(sum(len(batch) for _, batch in batches), len(entries))
        self.assertEqual(self.fixture.stat_requests, 0)
        self.assertEqual(self.fixture.sftp_sessions, 1)
        self.assertEqual(self.fixture.directory_reads, 1)
        broken = next(entry for entry in entries if entry.name == "broken-link")
        self.assertTrue(broken.is_symlink)
        self.assertFalse(broken.is_directory)
        # Cached navigation sends no network requests. Explicit refresh sees changes
        # while reusing the authenticated SSH and SFTP sessions.
        (folder / "added.txt").touch()
        self.assertEqual(self.service.list_directory(profile, str(folder))[1], entries)
        self.assertEqual(self.fixture.directory_reads, 1)
        refreshed = self.service.list_directory(profile, str(folder), force_refresh=True)[1]
        self.assertIn("added.txt", [entry.name for entry in refreshed])
        self.assertEqual(self.fixture.directory_reads, 2)
        self.assertEqual(self.fixture.sftp_sessions, 1)
        link = next(entry for entry in entries if entry.name == "link-0")
        resolved = self.service.resolve_symlink(profile, link)
        self.assertTrue(resolved.is_directory)
        self.assertEqual(self.fixture.stat_requests, 1)
        resolved_directory, _ = self.service.list_directory(profile, link.path)
        self.assertEqual(resolved_directory, str(nested))
        self.assertEqual(self.fixture.sftp_sessions, 1)
        self.service.disconnect(profile.id)
        self.service.list_directory(profile, directory)
        self.assertEqual(self.fixture.sftp_sessions, 2)
        self.assertEqual(self.fixture.authenticated, 2)

    def test_cancelling_partial_listing_discards_channel_and_next_listing_succeeds(self):
        folder = self.root / "many"
        folder.mkdir()
        for n in range(300):
            (folder / str(n)).touch()
        profile = ServerProfile("服务器", "127.0.0.1", "alice", port=self.port, password=self.password)
        cancel = threading.Event()

        def first_batch(payload):
            cancel.set()

        with self.assertRaises(DirectoryReadCancelled):
            self.service.list_directory(profile, str(folder), cancel=cancel, on_batch=first_batch)
        self.assertEqual(self.fixture.directory_reads, 1)
        _, entries = self.service.list_directory(profile, str(folder))
        self.assertEqual(len(entries), 300)
        self.assertEqual(self.fixture.directory_reads, 2)
        self.assertEqual(self.fixture.sftp_sessions, 2)
        self.assertEqual(self.fixture.authenticated, 1)
