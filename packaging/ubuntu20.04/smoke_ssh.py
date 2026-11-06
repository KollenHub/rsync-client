"""Exercise real rsync/OpenSSH using the compiled ASKPASS entry point."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from rsync_client.askpass import PasswordBroker
from rsync_client.engine import build_command
from rsync_client.models import TaskSpec
from tests.ssh_fixture import SSHFixture


def main():
    binary = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix='release-ssh-') as directory:
        root = Path(directory)
        server = SSHFixture(root)
        try:
            source = root / '文件 ; with spaces.txt'
            source.write_text('compiled SSH password authentication\n', encoding='utf-8')
            remote = root / 'remote'
            remote.mkdir()
            ssh_wrapper = root / 'bin' / 'ssh'
            ssh_wrapper.parent.mkdir()
            ssh_wrapper.write_text('#!/bin/sh\nexec /usr/bin/ssh -o GlobalKnownHostsFile=/dev/null -o '
                                   + shlex.quote(f'UserKnownHostsFile={root / "known_hosts"}') + ' "$@"\n')
            ssh_wrapper.chmod(0o700)
            connection = {'host': '127.0.0.1', 'username': server.username, 'port': server.port, 'password': server.password}
            with PasswordBroker(server.password) as environment:
                helper = Path(environment['SSH_ASKPASS'])
                helper.write_text('#!/bin/sh\nexec ' + shlex.quote(str(binary)) + ' --ssh-askpass "$@"\n')
                env = {**os.environ, **environment, 'PATH': str(ssh_wrapper.parent) + os.pathsep + os.environ['PATH']}
                specs = [TaskSpec('upload', str(source), str(remote), direction='upload', **connection),
                         TaskSpec('download', str(remote / source.name), str(root / 'download'), direction='download', **connection)]
                (root / 'download').mkdir()
                for spec in specs:
                    subprocess.run(build_command(spec), env=env, stdin=subprocess.DEVNULL,
                                   capture_output=True, start_new_session=True,
                                   timeout=20, check=True)
            assert (remote / source.name).read_bytes() == source.read_bytes()
            assert (root / 'download' / source.name).read_bytes() == source.read_bytes()
            assert server.authenticated == 2
            print('PASS: real upload/download with compiled SSH_ASKPASS; no GUI initialized for authentication')
        finally:
            server.close()


if __name__ == '__main__':
    main()
