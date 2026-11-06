from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path


class Vault:
    """AES-256-GCM vault with a PBKDF2-derived key, never saved alongside it."""

    ITERATIONS = 600_000
    AAD = b"rsync-desk-vault-v1"

    def __init__(self, path: Path):
        self.path = path
        self._key: bytes | None = None
        self._salt = b""
        self._entries: dict[str, str] = {}

    @property
    def unlocked(self) -> bool:
        return self._key is not None

    def unlock(self, master_password: str) -> None:
        try:
            from cryptography.exceptions import InvalidTag
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise ValueError("保存密码需要 cryptography，请先 pip install -r requirements.txt") from exc
        if not master_password:
            raise ValueError("主密码不能为空")
        existing = self.path.exists()
        data = json.loads(self.path.read_text(encoding="utf-8")) if existing else None
        if existing and not isinstance(data, dict):
            raise ValueError("无法识别的密码库格式")
        if data and (data.get("version") != 1 or data.get("iterations") != self.ITERATIONS):
            raise ValueError("无法识别的密码库格式")
        salt = base64.b64decode(data["salt"], validate=True) if existing else os.urandom(16)
        key = hashlib.pbkdf2_hmac("sha256", master_password.encode(), salt, self.ITERATIONS, 32)
        try:
            entries = json.loads(AESGCM(key).decrypt(
                base64.b64decode(data["nonce"], validate=True),
                base64.b64decode(data["ciphertext"], validate=True), self.AAD,
            )) if existing else {}
        except InvalidTag as exc:
            raise ValueError("主密码不正确，或密码库已损坏") from exc
        if not isinstance(entries, dict) or any(not isinstance(v, str) for v in entries.values()):
            raise ValueError("密码库内容无效")
        self._salt, self._key, self._entries = salt, key, entries

    def put(self, password: str, credential_id: str = "") -> str:
        if not self.unlocked:
            raise ValueError("请先解锁密码库")
        credential_id = credential_id or uuid.uuid4().hex
        previous = self._entries.copy()
        self._entries[credential_id] = password
        try:
            self._save()
        except Exception:
            self._entries = previous
            raise
        return credential_id

    def get(self, credential_id: str) -> str:
        if not self.unlocked:
            raise ValueError("请先解锁密码库")
        if credential_id not in self._entries:
            raise ValueError("已保存的密码不存在，请在服务器管理中重新保存密码")
        return self._entries[credential_id]

    def _save(self) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, json.dumps(self._entries).encode(), self.AAD)
        data = {
            "version": 1, "iterations": self.ITERATIONS,
            "salt": base64.b64encode(self._salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".vault-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
                stream.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def lock(self) -> None:
        self._key = None
        self._entries.clear()
