"""Loopback-only SSH server for rsync, SFTP and jump-host integration tests."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
from contextlib import suppress
from pathlib import Path

import paramiko


class SSHFixture:
    def __init__(self, root: Path, username="alice", password="s3cret-测试"):
        self.root, self.username, self.password = root, username, password
        self.hide_rsync = False  # simulate a server without rsync installed
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(16)
        self.listener.settimeout(0.2)
        self.stop = threading.Event()
        self.key = paramiko.RSAKey.generate(2048)
        self.connections = []
        self.channels = []
        self.processes = []
        self.authenticated = 0
        self.forwarded = 0
        self.sftp_sessions = 0
        self.directory_reads = 0
        self.stat_requests = 0
        self.normalizations = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._connection, args=(connection,), daemon=True).start()

    def _connection(self, connection):
        fixture = self

        class Server(paramiko.ServerInterface):
            def __init__(self):
                self.destinations = {}

            def get_allowed_auths(self, username):
                return "password"

            def check_auth_password(self, username, password):
                if username == fixture.username and password == fixture.password:
                    fixture.authenticated += 1
                    return paramiko.AUTH_SUCCESSFUL
                return paramiko.AUTH_FAILED

            def check_channel_request(self, kind, channel_id):
                return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_direct_tcpip_request(self, channel_id, origin, destination):
                if destination[0] != "127.0.0.1":
                    return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
                self.destinations[channel_id] = destination
                return paramiko.OPEN_SUCCEEDED

            def check_channel_exec_request(self, channel, command):
                threading.Thread(target=fixture._exec, args=(channel, command.decode()), daemon=True).start()
                return True

        class SFTP(paramiko.SFTPServerInterface):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                fixture.sftp_sessions += 1

            def canonicalize(self, path):
                fixture.normalizations += 1
                return str((fixture.root / path).resolve() if not path.startswith("/") else Path(path).resolve())

            def _path(self, path):
                result = Path(self.canonicalize(path))
                if result != fixture.root and fixture.root not in result.parents:
                    raise PermissionError("outside test root")
                return result

            def list_folder(self, path):
                fixture.directory_reads += 1
                try:
                    return [paramiko.SFTPAttributes.from_stat(p.lstat(), filename=p.name) for p in self._path(path).iterdir()]
                except OSError as exc:
                    return paramiko.SFTPServer.convert_errno(exc.errno or 13)

            def stat(self, path):
                fixture.stat_requests += 1
                try:
                    return paramiko.SFTPAttributes.from_stat(self._path(path).stat())
                except OSError as exc:
                    return paramiko.SFTPServer.convert_errno(exc.errno or 13)

        transport = paramiko.Transport(connection)
        self.connections.append(transport)
        transport.add_server_key(self.key)
        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, SFTP)
        server = Server()
        try:
            transport.start_server(server=server)
            while transport.is_active() and not self.stop.is_set():
                channel = transport.accept(timeout=0.2)
                if channel is None:
                    continue
                self.channels.append(channel)
                if destination := server.destinations.get(channel.get_id()):
                    threading.Thread(target=self._forward, args=(channel, destination), daemon=True).start()
        except (OSError, EOFError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def _forward(self, channel, destination):
        self.forwarded += 1
        with socket.create_connection(destination, timeout=5) as remote:
            remote.settimeout(None)

            def copy(source, target):
                try:
                    while chunk := source.recv(65536):
                        target.sendall(chunk)
                except (OSError, EOFError):
                    pass
                finally:
                    try:
                        target.shutdown_write() if isinstance(target, paramiko.Channel) else target.shutdown(socket.SHUT_WR)
                    except (OSError, EOFError):
                        pass

            thread = threading.Thread(target=copy, args=(channel, remote), daemon=True)
            thread.start()
            copy(remote, channel)
            with suppress(OSError, EOFError):
                channel.close()
            thread.join(timeout=2)

    def _exec(self, channel, command):
        if self.hide_rsync:  # simulate a server where rsync is not installed
            command = re.sub(r"\brsync\b", "rsync-missing", command)
        # A real sshd hands the command to the user's login shell, which makes
        # `echo RSYNC_PID=$$; exec rsync ...` wrappers and shell builtins (kill) work.
        process = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)

        def incoming():
            try:
                while chunk := channel.recv(65536):
                    process.stdin.write(chunk)
                    process.stdin.flush()
            except (OSError, EOFError):
                pass
            finally:
                process.stdin.close()

        def outgoing(stream, send):
            try:
                while chunk := os.read(stream.fileno(), 65536):
                    send(chunk)
            except (OSError, EOFError):
                pass
            finally:
                stream.close()

        reader = threading.Thread(target=incoming, daemon=True)
        stdout = threading.Thread(target=outgoing, args=(process.stdout, channel.sendall), daemon=True)
        stderr = threading.Thread(target=outgoing, args=(process.stderr, channel.sendall_stderr), daemon=True)
        for thread in (reader, stdout, stderr):
            thread.start()
        try:
            result = process.wait(timeout=20)
            stdout.join(timeout=2)
            stderr.join(timeout=2)
            channel.send_exit_status(result)
        except (OSError, EOFError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()
        finally:
            channel.close()
            reader.join(timeout=2)

    def close(self):
        self.stop.set()
        self.listener.close()
        for channel in self.channels:
            with suppress(OSError, EOFError):
                channel.close()
        for transport in self.connections:
            transport.close()
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        self.thread.join(timeout=2)
