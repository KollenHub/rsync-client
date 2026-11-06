from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from rsync_client.askpass import PasswordBroker
from rsync_client.config import load_tasks, save_tasks
from rsync_client.models import TaskSpec
from rsync_client.vault import Vault


class CredentialTests(unittest.TestCase):
    def test_encrypted_password_survives_restart_and_wrong_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "credentials.enc"
            first = Vault(path)
            first.unlock("my master password")
            reference = first.put("远端密码-p@ssword")
            raw = path.read_text()
            self.assertNotIn("p@ssword", raw)
            self.assertNotIn("my master password", raw)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            first.lock()
            with self.assertRaises(ValueError):
                first.get(reference)
            second = Vault(path)
            with self.assertRaisesRegex(ValueError, "主密码"):
                second.unlock("wrong password")
            self.assertFalse(second.unlocked)
            second.unlock("my master password")
            self.assertEqual(second.get(reference), "远端密码-p@ssword")
            before = json.loads(raw)["nonce"]
            second.put("updated", reference)
            self.assertNotEqual(before, json.loads(path.read_text())["nonce"])
            third = Vault(path)
            third.unlock("my master password")
            self.assertEqual(third.get(reference), "updated")

    def test_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "credentials.enc"
            vault = Vault(path)
            vault.unlock("master password")
            vault.put("ssh password")
            data = json.loads(path.read_text())
            encrypted = bytearray(base64.b64decode(data["ciphertext"]))
            encrypted[0] ^= 1
            data["ciphertext"] = base64.b64encode(encrypted).decode()
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                Vault(path).unlock("master password")

    def test_config_saves_account_and_reference_but_no_plaintext_secret(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tasks.json"
            spec = TaskSpec("saved", "/src", "/dst", direction="upload", host="server.example",
                            username="alice", port=2222, password="private-password", credential_id="ref")
            save_tasks(path, [spec], 3)
            self.assertNotIn("private-password", path.read_text())
            specs, count = load_tasks(path)
            self.assertEqual(count, 3)
            self.assertEqual(specs[0].credential_id, "ref")
            self.assertEqual(specs[0].username, "alice")
            self.assertEqual(specs[0].port, 2222)
            self.assertEqual(specs[0].password, "")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_askpass_only_answers_password_prompts_and_cleans_up(self):
        secret = "ssh password 密码"
        with PasswordBroker(secret) as env:
            self.assertNotIn(secret, repr(env))
            helper = env["SSH_ASKPASS"]
            directory = Path(helper).parent
            for path in directory.iterdir():
                if path.is_file():
                    self.assertNotIn(secret, path.read_text())
            result = subprocess.run([helper, "alice@server's password:"], env={**os.environ, **env}, capture_output=True, timeout=5, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.decode().strip(), secret)
            refused = subprocess.run([helper, "Enter passphrase for key"], env={**os.environ, **env}, capture_output=True, timeout=5, check=False)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(refused.stdout, b"")
        self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
