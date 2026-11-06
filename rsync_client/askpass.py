"""Private SSH_ASKPASS helper. Passwords travel over a protected local socket."""
from __future__ import annotations

import json
import os
import shlex
import socket
import sys
import tempfile
import threading
from pathlib import Path


class PasswordBroker:
    def __init__(self, password: str | dict[str, str]):
        self._passwords = password if isinstance(password, dict) else {"target": password} if password else {}
        self._temporary = None
        self._server = None
        self._thread = None
        self._stop = threading.Event()

    def __enter__(self) -> dict[str, str]:
        if not any(self._passwords.values()):
            return {"SSH_ASKPASS_REQUIRE": "never"}
        # /tmp keeps Unix socket names below the macOS/Linux length limits.
        self._temporary = tempfile.TemporaryDirectory(prefix="rsync-auth-", dir="/tmp")
        root = Path(self._temporary.name)
        address = str(root / "auth.sock")
        helper = root / "askpass"
        if "__compiled__" in globals() or getattr(sys, "frozen", False):
            # Nuitka can preserve the original interpreter basename in
            # sys.executable; the Linux kernel identifies the actual binary.
            executable = os.path.realpath("/proc/self/exe") if sys.platform.startswith("linux") else os.path.abspath(sys.argv[0])
            command = [executable, "--ssh-askpass"]
        else:
            command = [sys.executable, str(Path(__file__).resolve())]
        helper.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n', encoding="utf-8")
        helper.chmod(0o700)
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(address)
        os.chmod(address, 0o600)
        self._server.listen(4)
        self._server.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        # OpenSSH 8.2 (Ubuntu 20.04) also requires DISPLAY even though our
        # helper is non-graphical; newer versions honor ASKPASS_REQUIRE=force.
        return {"SSH_ASKPASS": str(helper), "SSH_ASKPASS_REQUIRE": "force",
                "DISPLAY": os.environ.get("DISPLAY") or ":0", "RSYNC_DESK_AUTH_SOCKET": address}

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                try:
                    connection.settimeout(1)
                    request = json.loads(connection.recv(4096).decode("utf-8"))
                    prompt = request["prompt"].lower()
                    # Never answer host-key confirmations or private-key prompts with a password.
                    if "password" in prompt and "passphrase" not in prompt:
                        password = self._passwords.get(request["id"], "")
                        connection.sendall(password.encode("utf-8"))
                except (OSError, ValueError, KeyError):
                    pass

    def __exit__(self, *args) -> None:
        self._stop.set()
        if self._server:
            self._server.close()
        if self._thread:
            self._thread.join(timeout=2)
        self._passwords.clear()
        if self._temporary:
            self._temporary.cleanup()


def main(prompt_args: list[str] | None = None) -> int:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(os.environ["RSYNC_DESK_AUTH_SOCKET"])
            connection.sendall(json.dumps({"id": os.environ.get("RSYNC_DESK_AUTH_ID", "target"),
                                           "prompt": " ".join(sys.argv[1:] if prompt_args is None else prompt_args)}).encode("utf-8"))
            parts = []
            while chunk := connection.recv(4096):
                parts.append(chunk)
        password = b"".join(parts)
        if not password:
            return 1
        sys.stdout.buffer.write(password + b"\n")
        return 0
    except (OSError, KeyError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
