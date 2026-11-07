from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from rsync_client.config import load_config, load_task_states, save_config
from rsync_client.engine import TaskManager
from rsync_client.models import ServerProfile, TaskSpec, connection_route


class ServerConfigTests(unittest.TestCase):
    def test_direction_directories_round_trip_and_invalid_entries_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.json'
            server = ServerProfile('backup', 'backup.example', 'alice')
            directories = {
                server.id: {'upload': {'source': '/home/alice/data', 'destination': '/srv/backup'},
                            'download': {'source': '/srv/remote', 'destination': '/home/alice/restore'}},
                '': {'local': {'destination': '/home/alice/mirror'}},
            }
            save_config(path, [], 2, [server], last_directories=directories)
            _, _, _, restored = load_config(path)
            self.assertEqual(restored[server.id]['upload']['destination'], '/srv/backup')
            self.assertEqual(restored[server.id]['download']['source'], '/srv/remote')
            self.assertEqual(restored['']['local']['destination'], '/home/alice/mirror')
            path.write_text(json.dumps({
                'version': 2, 'concurrency': 2, 'servers': [server.public_dict()], 'tasks': [],
                'last_directories': {server.id: {'upload': 'plain-destination', 'bogus': {'x': 1}, 'download': 5}}
            }))
            _, _, _, restored = load_config(path)
            self.assertEqual(restored[server.id]['upload']['destination'], 'plain-destination')
            self.assertNotIn('download', restored[server.id])
            self.assertNotIn('bogus', restored[server.id])

    def test_saved_history_is_restored_and_credentials_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tasks.json'
            manager = TaskManager()
            spec = TaskSpec('failed', '/source', '/target', password='private-password')
            key = manager.add(spec, check_paths=False)
            manager.restore(key, {'status': 'failed', 'error': 'private-password denied', 'total_bytes': 10, 'transferred_bytes': 4, 'return_code': 23})
            snapshot = manager.get(key)
            save_config(path, [spec], 2, [], states=[snapshot.public_state()])
            self.assertNotIn('private-password', path.read_text())
            restored = load_task_states(path)[0]
            self.assertEqual(restored['status'], 'failed')
            self.assertEqual(restored['return_code'], 23)
            self.assertEqual(restored['error'], '****** denied')
            manager.restore(key, restored)
            self.assertEqual(manager.get(key).transferred_bytes, 4)

    def test_restored_live_tasks_become_stopped_without_starting(self):
        manager = TaskManager()
        key = manager.add(TaskSpec('interrupted', '/source', '/target'), check_paths=False)
        for status in ('running', 'scanning', 'queued', 'cancelling'):
            manager.restore(key, {'status': status, 'total_bytes': 100, 'transferred_bytes': 40})
            snapshot = manager.get(key)
            self.assertEqual(snapshot.status, 'cancelled')
            self.assertEqual(snapshot.speed, 0)
            self.assertEqual(snapshot.percent, 40)
        manager.restore(key, {'status': [], 'total_bytes': -1, 'started_at': float('nan')})
        self.assertEqual(manager.get(key).status, 'ready')
        self.assertIsNone(manager.get(key).started_at)

    def test_legacy_connections_migrate_without_losing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            first = TaskSpec("download", "/remote/one", "/local", direction="download", host="server.example",
                             username="user@root@10.0.0.2", port=2222, credential_id="old-vault-reference")
            second = replace(first, name="second", source="/remote/two")
            path.write_text(json.dumps({"version": 1, "concurrency": 3, "tasks": [first.public_dict(), second.public_dict()]}))
            specs, concurrency, servers, _ = load_config(path)
            self.assertEqual(len(servers), 1)
            self.assertEqual(concurrency, 3)
            self.assertEqual(servers[0].credential_id, "old-vault-reference")
            self.assertTrue(all(s.server_id == servers[0].id for s in specs))
            save_config(path, specs, concurrency, servers)
            data = json.loads(path.read_text())
            self.assertEqual(data["version"], 2)
            self.assertNotIn("host", data["tasks"][0])
            self.assertNotIn("credential_id", data["tasks"][0])
            restored, _, profiles, _ = load_config(path)
            self.assertEqual(restored[0].username, first.username)
            self.assertEqual(profiles[0].port, 2222)

    def test_tasks_reference_latest_server_settings_and_jump_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.json"
            jump = ServerProfile("jump", "jump.example", "relay", password="jump-secret", credential_id="jump-ref")
            target = ServerProfile("target", "target.example", "user", password="target-secret", credential_id="target-ref", jump_server_id=jump.id)
            task = target.bind(TaskSpec("download", "/data", "/local", direction="download"))
            target = replace(target, host="new-target.example", port=2222)
            save_config(path, [task], 2, [jump, target])
            raw = path.read_text()
            for secret in (jump.password, target.password):
                self.assertNotIn(secret, raw)
            specs, _, profiles, _ = load_config(path)
            self.assertEqual(specs[0].host, "new-target.example")
            self.assertEqual(specs[0].port, 2222)
            self.assertEqual(specs[0].jump_hosts[0].id, jump.id)
            self.assertEqual(specs[0].jump_hosts[0].credential_id, "jump-ref")
            self.assertTrue(all(not p.password for p in profiles))

    def test_reject_missing_hop_and_cycles(self):
        first = ServerProfile("first", "one.example", "alice")
        second = ServerProfile("second", "two.example", "alice", jump_server_id=first.id)
        first = replace(first, jump_server_id=second.id)
        with self.assertRaisesRegex(ValueError, "循环"):
            connection_route(first, {first.id: first, second.id: second})
        with self.assertRaisesRegex(ValueError, "不存在"):
            connection_route(second, {second.id: second})
