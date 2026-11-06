"""Offline smoke checks for a source installation or standalone release."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


def run() -> int:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import qVersion
    from PySide6.QtWidgets import QApplication

    from .askpass import PasswordBroker
    from .dialogs import CommandPreview, ServerDialog, TaskDialog
    from .engine import TaskManager
    from .models import TaskSpec
    from .pickers import MixedPicker
    from .theme import setup_style
    from .ui import App
    from .vault import Vault

    with tempfile.TemporaryDirectory(prefix='rsync-desk-check-') as directory:
        root = Path(directory)
        source = root / '源文件.txt'
        source.write_text('Rsync Desk standalone check\n', encoding='utf-8')
        spec = TaskSpec('check', str(source), str(root / 'out'))
        manager = TaskManager()
        try:
            task_id = manager.add(spec)
            manager.start([task_id])
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                task = manager.get(task_id)
                if task.status in {'success', 'failed'}:
                    break
                time.sleep(0.025)
            if task.status != 'success' or (root / 'out' / source.name).read_bytes() != source.read_bytes():
                raise RuntimeError(f'同步检查失败：{task.error}')
        finally:
            manager.shutdown()
        vault = Vault(root / 'credentials.enc')
        vault.unlock('temporary-master-password')
        reference = vault.put('temporary-ssh-password')
        vault.lock()
        vault.unlock('temporary-master-password')
        if vault.get(reference) != 'temporary-ssh-password':
            raise RuntimeError('密码库检查失败')
        with PasswordBroker('temporary-ssh-password') as auth_env:
            result = subprocess.run([auth_env['SSH_ASKPASS'], "user@server's password:"],
                                    env={**os.environ, **auth_env, 'RSYNC_DESK_AUTH_ID': 'target'},
                                    capture_output=True, timeout=10, check=False)
            if result.returncode or result.stdout.strip() != b'temporary-ssh-password':
                raise RuntimeError(f'密码助手检查失败：{result.stderr.decode(errors="replace")}')
        application = QApplication.instance() or QApplication([])
        application.setQuitOnLastWindowClosed(False)
        setup_style(application)
        window = App(root / 'tasks.json')
        window.show()
        history_id = window.manager.add(spec)
        window.manager.restore(history_id, {'status': 'success', 'total_bytes': source.stat().st_size,
                                           'transferred_bytes': source.stat().st_size})
        window.show_tasks('local', 'success')
        if window.proxy.rowCount() != 1:
            raise RuntimeError('任务分组检查失败')
        window.show_tasks('upload')
        if window.proxy.rowCount() != 0:
            raise RuntimeError('传输方向隔离检查失败')
        if window.windowIcon().pixmap(64, 64).isNull():
            raise RuntimeError('应用图标检查失败')
        dialogs = [TaskDialog(window), ServerDialog(window, window), CommandPreview(window, [spec]), MixedPicker(window, lambda paths: None)]
        for dialog in dialogs:
            dialog.show()
        application.processEvents()
        for dialog in dialogs:
            dialog.close()
        window.close()
        application.processEvents()
        print(json.dumps({'ok': True, 'qt': qVersion(), 'checks': ['rsync', 'encrypted-vault', 'ssh-askpass', 'qt-windows', 'file-picker', 'application-icon', 'task-groups']}, ensure_ascii=False))
    return 0
