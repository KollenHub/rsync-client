from __future__ import annotations

import os
import shlex
import shutil
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QItemSelectionModel, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rsync_client.dialogs import CommandPreview, ServerDialog, TaskDialog
from rsync_client.models import ServerProfile, TaskSpec
from rsync_client.pickers import MixedPicker, RemotePicker
from rsync_client.remote import RemoteService
from rsync_client.theme import setup_style
from rsync_client.ui import App
from tests.ssh_fixture import SSHFixture


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)
        setup_style(cls.qt)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = App(self.root / 'config' / 'tasks.json')
        self.app.show()
        self.qt.processEvents()

    def tearDown(self):
        self.app.manager.shutdown()
        self.app.close()
        self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def wait_for(self, condition, timeout=8):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.qt.processEvents()
            QTest.qWait(10)
        self.assertTrue(condition())

    def assert_footer_visible(self, picker):
        self.qt.processEvents()
        self.assertTrue(picker.confirm.isVisible())
        bottom = picker.confirm.mapTo(picker, picker.confirm.rect().bottomRight()).y()
        self.assertLess(bottom, picker.height())
        self.assertGreater(picker.confirm.height(), 25)

    def add_transfer(self, direction, name, status='ready'):
        source = self.root / f'{name}.bin'
        source.write_bytes(b'data')
        spec = TaskSpec(name, '/remote/' + name if direction in ('download', 'remote') else str(source),
                        '/remote/output' if direction in ('upload', 'remote') else str(self.root / 'out'),
                        direction=direction, host='example.org' if direction != 'local' else '', username='alice')
        if direction != 'local':
            profile = next(iter(self.app.servers.values()), None)
            if profile is None:
                profile = ServerProfile('测试服务器', 'example.org', 'alice')
                self.app.save_server(profile)
            spec = profile.bind(spec)
        task_id = self.app.manager.add(spec)
        self.app.manager.restore(task_id, {'status': status, 'total_bytes': 4, 'transferred_bytes': 4 if status == 'success' else 0})
        return task_id

    def test_direction_status_counters_and_actions_use_visible_task_ids(self):
        identifiers = [self.add_transfer('upload', 'upload-running'), self.add_transfer('download', 'download-failed'),
                       self.add_transfer('upload', 'upload-queued'), self.add_transfer('upload', 'upload-failed'),
                       self.add_transfer('download', 'download-running'), self.add_transfer('upload', 'upload-complete'),
                       self.add_transfer('download', 'download-complete'), self.add_transfer('local', 'local-stopped'),
                       self.add_transfer('remote', 'remote-running')]
        statuses = ('running', 'failed', 'queued', 'failed', 'scanning', 'success', 'success', 'cancelled', 'running')
        snapshots = [replace(task, status=status) for task, status in zip(self.app.manager.snapshots(), statuses)]
        self.app._save_disabled = True
        with patch.object(self.app.manager, 'snapshots', return_value=snapshots):
            self.app.show_tasks('upload', 'running')
            self.assertEqual([task.id for task in self.app.visible_tasks()], [identifiers[0]])
            self.assertEqual(self.app.metric_labels[1].text(), '1')
            self.assertEqual(self.app.metric_labels[2].text(), '1')
            self.assertIn('1 个排队', self.app.overall_info.text())
            self.app.show_tasks('upload', 'pending')
            self.assertEqual([task.id for task in self.app.visible_tasks()], [identifiers[2]])
            self.app.show_tasks('download', 'failed')
            self.app.table.selectRow(0)
            self.assertEqual(self.app.selected_ids(), [identifiers[1]])
            with patch.object(self.app.manager, 'start') as start:
                self.app.start_selected()
                start.assert_called_once_with([identifiers[1]])
            self.app.show_tasks('upload', 'failed')
            with patch.object(self.app.manager, 'start') as start:
                self.app.retry_failed()
                start.assert_called_once_with([identifiers[3]])
            self.assertEqual(self.app.current_direction(), 'upload')
            # Starting tasks jumps to the running tab so progress is visible.
            self.assertEqual(self.app.current_status(), 'running')
            self.app.show_tasks('download', 'success')
            self.assertEqual([task.id for task in self.app.visible_tasks()], [identifiers[6]])
            self.app.show_tasks('local', 'cancelled')
            self.assertEqual([task.id for task in self.app.visible_tasks()], [identifiers[7]])
            self.app.show_tasks('remote', 'running')
            self.assertEqual([task.id for task in self.app.visible_tasks()], [identifiers[8]])

    def test_clear_only_current_category_and_keeps_live_or_queued_transfers(self):
        upload = self.add_transfer('upload', 'upload-complete', 'success')
        download = self.add_transfer('download', 'download-complete', 'success')
        failed = self.add_transfer('upload', 'upload-failed', 'failed')
        local_complete = self.add_transfer('local', 'local-complete', 'success')
        self.app.show_tasks('upload', 'success')
        self.app.clear_button.click()
        self.assertIsNone(self.app.manager.get(upload))
        self.assertIsNotNone(self.app.manager.get(download))
        self.assertIsNotNone(self.app.manager.get(failed))
        large = self.root / 'large.bin'
        large.write_bytes(os.urandom(2 * 1024 * 1024))
        active = self.app.manager.add(TaskSpec('active', str(large), str(self.root / 'active'), bandwidth_kib=64))
        queued = self.app.manager.add(TaskSpec('queued', str(large), str(self.root / 'queued'), bandwidth_kib=64))
        self.app.manager.set_concurrency(1)
        self.app.manager.start([active, queued])
        self.wait_for(lambda: self.app.manager.get(active).status == 'running')
        self.app.show_tasks('local')
        # With a throttled transfer running, the bar reflects this batch only.
        self.wait_for(lambda: self.app.overall_bar.value() > 0)
        self.assertIn('1 个运行中', self.app.overall_info.text())
        self.app.clear_button.click()
        self.assertIsNone(self.app.manager.get(local_complete))
        self.assertEqual(self.app.manager.get(active).status, 'running')
        self.assertEqual(self.app.manager.get(queued).status, 'queued')
        self.assertIsNotNone(self.app.manager.get(download))
        self.assertIsNotNone(self.app.manager.get(failed))
        self.app.show_tasks('local', 'running')
        self.assertFalse(self.app.clear_button.isEnabled())

    def test_retry_failed_jobs_and_history_survive_restart(self):
        first = self.add_transfer('local', 'first')
        second = self.add_transfer('local', 'second')
        remote = self.add_transfer('download', 'remote-failed', 'failed')
        self.app.manager.rsync = '/missing/rsync'
        self.app.show_tasks('local')
        self.app.start_all()
        self.wait_for(lambda: all(self.app.manager.get(key).status == 'failed' for key in (first, second)))
        self.app.show_tasks('local', 'failed')
        self.assertEqual(self.app.proxy.rowCount(), 2)
        self.assertTrue(self.app.retry_button.isEnabled())
        self.app.manager.rsync = shutil.which('rsync')
        self.app.retry_button.click()
        self.wait_for(lambda: all(self.app.manager.get(key).status == 'success' for key in (first, second)))
        self.assertEqual(self.app.manager.get(remote).status, 'failed')
        self.app.show_tasks('local', 'success')
        self.assertEqual(self.app.proxy.rowCount(), 2)
        self.app.persist()
        config = self.app.config_path
        self.app.close()
        self.app.deleteLater()
        self.qt.processEvents()
        self.app = App(config)
        self.app.show()
        self.app.show_tasks('local', 'success')
        self.assertEqual(self.app.proxy.rowCount(), 2)
        self.app.show_tasks('download', 'failed')
        self.assertEqual(self.app.proxy.rowCount(), 1)
        self.assertEqual(self.app.visible_tasks()[0].spec.name, 'remote-failed')

    def test_mixed_selection_double_click_confirm_sync_and_logs(self):
        folder = self.root / 'photos'
        folder.mkdir()
        (folder / 'photo.bin').write_bytes(os.urandom(200000))
        file = self.root / 'report.pdf'
        file.write_text('report')
        dialog = self.app.show_dialog(TaskDialog(self.app))
        picker = self.app.show_dialog(MixedPicker(dialog, dialog.add_paths))
        picker.resize(650, 420)
        picker.location.setText(str(self.root))
        picker.load()
        self.wait_for(lambda: picker.model.rowCount(picker.tree.rootIndex()) >= 2)
        self.assert_footer_visible(picker)
        index = picker.model.index(str(file))
        picker.tree.scrollTo(index)
        self.qt.processEvents()
        QTest.mouseClick(picker.tree.viewport(), Qt.MouseButton.LeftButton, pos=picker.tree.visualRect(index).center())
        QTest.mouseDClick(picker.tree.viewport(), Qt.MouseButton.LeftButton, pos=picker.tree.visualRect(index).center())
        self.assertIn(str(file), picker.basket)
        directory_index = picker.model.index(str(folder))
        picker.tree.selectionModel().select(directory_index, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        QTest.mouseClick(picker.confirm, Qt.MouseButton.LeftButton)
        self.assertEqual(set(dialog.sources), {str(file), str(folder)})
        dialog.destination.setText(str(self.root / 'out'))
        dialog.submit()
        self.assertEqual(self.app.model.rowCount(), 2)
        # Submitting a new task stays on the current view; starting jumps to running.
        self.assertEqual(self.app.current_status(), 'all')
        self.app.start_all()
        self.assertEqual(self.app.current_status(), 'running')
        self.wait_for(lambda: all(task.status == 'success' for task in self.app.manager.snapshots()))
        self.app.refresh()
        # Finished tasks are history: the overall bar only tracks the active batch.
        self.assertEqual(self.app.overall_bar.value(), 0)
        self.assertIn('没有运行中的任务', self.app.overall_info.text())
        self.app.show_tasks('local', 'success')
        self.app.table.selectRow(0)
        self.app.open_details()
        key = self.app.model.tasks[0].id
        self.assertIn('rsync', self.app.details[key].text.toPlainText())
        self.assertIn('传输完成', self.app.details[key].text.toPlainText())
        self.assertTrue((self.root / 'out' / 'report.pdf').exists())
        self.assertTrue((self.root / 'out' / 'photos' / 'photo.bin').exists())

    def test_destination_memory_kept_per_server_and_direction(self):
        # A closed local port fails fast so the remote picker never blocks on DNS or TCP timeouts.
        server = ServerProfile('备份机', '127.0.0.1', 'alice', port=1)
        self.app.save_server(server)
        project = self.root / 'project'
        project.mkdir()
        dialog = self.app.show_dialog(TaskDialog(self.app))
        dialog.target_endpoint.setCurrentIndex(dialog.target_endpoint.findData(server.id))
        self.assertEqual(dialog.destination.text(), '/')
        dialog.add_paths([str(project)])
        dialog.destination.setText('/srv/backup')
        dialog.submit()

        dialog = self.app.show_dialog(TaskDialog(self.app))
        dialog.source_endpoint.setCurrentIndex(dialog.source_endpoint.findData(server.id))
        dialog.add_paths(['/srv/remote/source'])
        dialog.destination.setText(str(self.root / 'restore'))
        dialog.submit()

        self.assertEqual(self.app.last_directory_for(server.id, 'upload'), '/srv/backup')
        self.assertEqual(self.app.last_directory_for(server.id, 'upload', 'source'), str(self.root))
        self.assertEqual(self.app.last_directory_for(server.id, 'download'), str(self.root / 'restore'))
        self.assertEqual(self.app.last_directory_for(server.id, 'download', 'source'), '/srv/remote')

        # A fresh upload dialog pre-fills the remote destination and picker start.
        dialog = self.app.show_dialog(TaskDialog(self.app))
        dialog.target_endpoint.setCurrentIndex(dialog.target_endpoint.findData(server.id))
        self.assertEqual(dialog.destination.text(), '/srv/backup')
        picker = dialog.pick_sources()
        self.assertEqual(picker.location.text(), str(self.root))
        # The download direction remembers an independent local destination.
        dialog = self.app.show_dialog(TaskDialog(self.app))
        dialog.source_endpoint.setCurrentIndex(dialog.source_endpoint.findData(server.id))
        self.assertEqual(dialog.destination.text(), str(self.root / 'restore'))
        picker = dialog.pick_sources()
        self.assertEqual(picker.location.text(), '/srv/remote')

    def test_saved_server_is_reused_after_restart(self):
        source = self.root / 'report.txt'
        source.write_text('data')
        server = self.app.show_dialog(ServerDialog(self.app, self.app))
        for key, value in {'name': '开发服务器', 'host': 'example.org', 'username': 'alice', 'port': '2222', 'password': 'saved-password'}.items():
            server.fields[key].setText(value)
        with patch('rsync_client.ui.QInputDialog.getText', side_effect=[('master-password', True), ('master-password', True)]):
            server.submit()
        profile = next(iter(self.app.servers.values()))
        dialog = self.app.show_dialog(TaskDialog(self.app))
        dialog.target_endpoint.setCurrentIndex(dialog.target_endpoint.findData(profile.id))
        dialog.add_paths([source])
        dialog.destination.setText('/backup')
        self.assertFalse(hasattr(dialog, 'password'))
        dialog.submit()
        config = self.app.config_path
        self.assertNotIn('saved-password', config.read_text())
        self.app.close()
        self.app.deleteLater()
        self.qt.processEvents()
        self.app = App(config)
        self.app.show()
        restored = self.app.manager.snapshots()[0]
        self.assertEqual(restored.spec.server_id, profile.id)
        self.assertEqual(restored.spec.password, '')
        with patch('rsync_client.ui.QInputDialog.getText', return_value=('master-password', True)), patch.object(self.app.manager, 'start') as start:
            self.app.start([restored.id])
        start.assert_called_once_with([restored.id])
        self.assertEqual(self.app.manager.get(restored.id).spec.password, 'saved-password')

    def test_endpoints_only_allow_local_remote_and_preview_matches_execution(self):
        from rsync_client.engine import build_command
        first = ServerProfile('服务器一', 'one.example', 'user@root@10.0.0.8', password='hidden-password')
        second = ServerProfile('服务器二', 'two.example', 'bob')
        self.app.save_server(first)
        self.app.save_server(second)
        dialog = self.app.show_dialog(TaskDialog(self.app))
        dialog.source_endpoint.setCurrentIndex(dialog.source_endpoint.findData(first.id))
        # With a remote source the target offers 本机 plus the same server only.
        self.assertEqual(dialog.target_endpoint.count(), 2)
        self.assertEqual(dialog.target_endpoint.currentData(), '')
        dialog.target_endpoint.setCurrentIndex(dialog.target_endpoint.findData(first.id))
        self.assertIn('远端内部同步', dialog.route_hint.text())
        dialog.add_paths(['/data/目录'])
        dialog.destination.setText('/srv/backup')
        self.assertEqual(dialog.build_specs()[0].direction, 'remote')
        dialog.target_endpoint.setCurrentIndex(dialog.target_endpoint.findData(''))
        dialog.destination.setText(str(self.root / 'out'))
        specs = dialog.build_specs()
        self.assertEqual(specs[0].direction, 'download')
        preview = self.app.show_dialog(CommandPreview(dialog, specs))
        actual = shlex.split(preview.commands.split('\n', 1)[1].replace('\\\n', ''))
        self.assertEqual(actual, build_command(specs[0]))
        self.assertNotIn(first.password, preview.commands)
        preview.mode.setCurrentIndex(1)
        self.assertIn('--dry-run', preview.commands)
        preview.copy_commands()
        self.assertEqual(QApplication.clipboard().text(), preview.commands)
        preview.close()
        dialog.swap()
        self.assertEqual(dialog.source_endpoint.currentData(), '')
        self.assertEqual(dialog.target_endpoint.currentData(), first.id)
        self.assertEqual(dialog.sources, [])

    def test_contents_mode_option_in_dialog(self):
        from rsync_client.engine import build_command
        folder = self.root / 'data'
        (folder / 'nested').mkdir(parents=True)
        (folder / 'a.txt').write_text('a')
        dialog = self.app.show_dialog(TaskDialog(self.app))
        dialog.add_paths([str(folder)])
        dialog.destination.setText(str(self.root / 'out'))
        self.assertNotIn('内容直传', dialog.source_count.text())
        dialog.contents.setChecked(True)
        self.assertIn('内容直传', dialog.source_count.text())
        specs = dialog.build_specs()
        self.assertTrue(specs[0].sync_contents)
        self.assertEqual(build_command(specs[0])[-2], str(folder) + '/')
        dialog.submit()
        self.assertTrue(self.app.manager.snapshots()[0].spec.sync_contents)
        # Same-name directories are rejected normally but may merge in contents mode.
        dialog = self.app.show_dialog(TaskDialog(self.app))
        merge = self.root / 'other' / 'data'
        merge.mkdir(parents=True)
        dialog.add_paths([str(folder), str(merge)])
        dialog.destination.setText(str(self.root / 'out2'))
        with self.assertRaises(ValueError):
            dialog.build_specs()
        dialog.contents.setChecked(True)
        self.assertEqual(len(dialog.build_specs()), 2)

    def test_remote_internal_dialog_direction_and_directory_memory(self):
        server = ServerProfile('备份机', '127.0.0.1', 'alice', port=1)
        self.app.save_server(server)
        dialog = self.app.show_dialog(TaskDialog(self.app))
        dialog.source_endpoint.setCurrentIndex(dialog.source_endpoint.findData(server.id))
        dialog.target_endpoint.setCurrentIndex(dialog.target_endpoint.findData(server.id))
        self.assertIn('数据不经本机中转', dialog.route_hint.text())
        self.assertEqual(dialog.destination.text(), '/')  # server initial directory
        dialog.add_paths(['/srv/remote/源 目录'])
        dialog.destination.setText('/srv/backup')
        specs = dialog.build_specs()
        self.assertEqual(specs[0].direction, 'remote')
        self.assertEqual(specs[0].server_id, server.id)
        self.assertEqual(specs[0].source, '/srv/remote/源 目录')
        self.assertEqual(specs[0].destination, '/srv/backup')
        dialog.submit()
        self.assertEqual(self.app.last_directory_for(server.id, 'remote'), '/srv/backup')
        self.assertEqual(self.app.last_directory_for(server.id, 'remote', 'source'), '/srv/remote')
        self.assertEqual(self.app.model.tasks[0].spec.direction, 'remote')
        # A fresh dialog pre-fills the remembered remote destination and picker start.
        dialog = self.app.show_dialog(TaskDialog(self.app))
        dialog.source_endpoint.setCurrentIndex(dialog.source_endpoint.findData(server.id))
        dialog.target_endpoint.setCurrentIndex(dialog.target_endpoint.findData(server.id))
        self.assertEqual(dialog.destination.text(), '/srv/backup')
        self.assertEqual(dialog.pick_sources().location.text(), '/srv/remote')
        # The download direction keeps an independent local destination.
        dialog = self.app.show_dialog(TaskDialog(self.app))
        dialog.source_endpoint.setCurrentIndex(dialog.source_endpoint.findData(server.id))
        self.assertEqual(dialog.destination.text(), '')

    def test_server_login_remote_selection_and_download(self):
        folder = self.root / '远端目录'
        folder.mkdir()
        (folder / 'nested.txt').write_text('nested content')
        file = self.root / '单文件.txt'
        file.write_text('remote content')
        fixture = SSHFixture(self.root)
        self.app.remote.close()
        self.app.remote = RemoteService(self.root / 'known_hosts')
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        wrapper = bin_dir / 'ssh'
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(shutil.which("ssh"))} -o UserKnownHostsFile={shlex.quote(str(self.root / "known_hosts"))} -o GlobalKnownHostsFile=/dev/null "$@"\n')
        wrapper.chmod(0o700)
        profile = ServerProfile('可浏览服务器', '127.0.0.1', fixture.username, port=fixture.port, password=fixture.password, initial_directory=str(self.root))
        self.app.save_server(profile)
        try:
            self.app.login_server(profile.id)
            self.wait_for(lambda: profile.id not in self.app.connecting)
            self.assertIn('登录成功', self.app.server_messages[profile.id])
            dialog = self.app.show_dialog(TaskDialog(self.app))
            dialog.source_endpoint.setCurrentIndex(dialog.source_endpoint.findData(profile.id))
            picker = self.app.show_dialog(RemotePicker(dialog, self.app, profile.id, dialog.add_paths))
            picker.resize(700, 440)
            self.wait_for(lambda: len(picker.entries) > 0)
            self.assert_footer_visible(picker)
            file_index = next(picker.model.index(index, 0) for index in range(picker.model.rowCount()) if picker.model.index(index, 0).data(Qt.ItemDataRole.UserRole) == str(file))
            picker.tree.scrollTo(file_index)
            self.qt.processEvents()
            QTest.mouseClick(picker.tree.viewport(), Qt.MouseButton.LeftButton, pos=picker.tree.visualRect(file_index).center())
            QTest.mouseDClick(picker.tree.viewport(), Qt.MouseButton.LeftButton, pos=picker.tree.visualRect(file_index).center())
            self.assertIn(str(file), picker.basket)
            folder_index = next(picker.model.index(index, 0) for index in range(picker.model.rowCount()) if picker.model.index(index, 0).data(Qt.ItemDataRole.UserRole) == str(folder))
            picker.tree.selectionModel().select(folder_index, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
            QTest.mouseClick(picker.confirm, Qt.MouseButton.LeftButton)
            self.assertEqual(set(dialog.sources), {str(file), str(folder)})
            dialog.destination.setText(str(self.root / 'downloads'))
            dialog.submit()
            with patch.dict(os.environ, {'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}):
                self.app.start_all()
                self.wait_for(lambda: all(task.status == 'success' for task in self.app.manager.snapshots()))
            self.assertEqual((self.root / 'downloads' / file.name).read_text(), file.read_text())
            self.assertEqual((self.root / 'downloads' / folder.name / 'nested.txt').read_text(), 'nested content')
        finally:
            self.app.remote.close()
            fixture.close()
