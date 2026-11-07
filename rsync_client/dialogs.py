from __future__ import annotations

import shlex
from dataclasses import replace
from pathlib import PurePosixPath

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .engine import Snapshot, build_command
from .models import ServerProfile, TaskSpec, connection_route, local_path
from .pickers import MixedPicker, RemotePicker
from .qt_helpers import Modal, button, label, row


class ServerDialog(Modal):
    def __init__(self, parent, app, profile=None):
        super().__init__(parent)
        self.app, self.profile = app, profile
        self.setWindowTitle('编辑服务器' if profile else '新增服务器')
        self.resize(740, 700)
        self.setMinimumSize(640, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.addWidget(label(self.windowTitle(), 'title'))
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        form = QFormLayout(content)
        form.setVerticalSpacing(14)
        profile = profile or ServerProfile('', '', '')
        self.fields = {}
        for key, title in (('name', '服务器名称'), ('host', '主机地址'), ('port', 'SSH 端口'), ('username', '登录账号'),
                           ('password', '登录密码'), ('identity_file', '私钥（可选）'), ('initial_directory', '初始目录')):
            entry = QLineEdit(str(getattr(profile, key)))
            if key == 'password':
                entry.setEchoMode(QLineEdit.EchoMode.Password)
            self.fields[key] = entry
            form.addRow(title, entry)
        self.remember = QCheckBox('加密保存密码')
        self.remember.setChecked(bool(profile.credential_id) if self.profile else True)
        visible = QCheckBox('显示密码')
        visible.toggled.connect(lambda value: self.fields['password'].setEchoMode(QLineEdit.EchoMode.Normal if value else QLineEdit.EchoMode.Password))
        form.addRow(row(self.remember, visible, button('选择私钥…', self.choose_identity)))
        self.jump = QComboBox()
        self.jump.addItem('直接连接', '')
        for server in app.servers.values():
            if server.id != profile.id:
                self.jump.addItem(server.name, server.id)
        self.jump.setCurrentIndex(max(0, self.jump.findData(profile.jump_server_id)))
        form.addRow('通过跳板机', self.jump)
        form.addRow(label('支持 user@root@10.0.0.8 等堡垒机复合账号。\n使用跳板机时先保存跳板服务器，再在上方选择。密码留空可使用私钥 / SSH agent。', 'muted', wrap=True))
        if profile.credential_id:
            form.addRow(label('已保存密码；密码框留空会继续使用。取消勾选可解绑保存的密码。', 'muted', wrap=True))
        area.setWidget(content)
        layout.addWidget(area, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button('保存', self.submit))
        actions.addWidget(button('保存并登录', lambda: self.submit(login=True), primary=True))
        actions.addWidget(button('取消', self.reject))
        layout.addLayout(actions)

    def choose_identity(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择 SSH 私钥')
        if path:
            self.fields['identity_file'].setText(path)

    def submit(self, *, login=False):
        try:
            values = {key: entry.text().strip() if key != 'password' else entry.text() for key, entry in self.fields.items()}
            values['port'] = int(values['port'])
            values['jump_server_id'] = self.jump.currentData()
            if self.profile:
                values['id'] = self.profile.id
                same = all(getattr(self.profile, key) == values[key] for key in ('host', 'username', 'port', 'identity_file'))
                if same and not values['password']:
                    values['password'] = self.profile.password
                    if self.remember.isChecked():
                        values['credential_id'] = self.profile.credential_id
            profile = ServerProfile(**values)
            profile.validate()
            self.app.validate_server(profile)
            if self.remember.isChecked() and profile.password:
                if not self.app.unlock_vault(self):
                    return
                profile = replace(profile, credential_id=self.app.vault.put(profile.password))
            self.app.save_server(profile)
            if login:
                self.app.login_server(profile.id, self.parentWidget())
            self.accept()
        except (ValueError, OSError, KeyError) as exc:
            QMessageBox.critical(self, '无法保存服务器', str(exc))


class ServerManager(Modal):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app, self.items = app, {}
        self.setWindowTitle('服务器管理')
        self.resize(1040, 610)
        self.setMinimumSize(850, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addWidget(label('服务器管理', 'title'))
        layout.addWidget(label('统一保存账号、密码和跳板机，传输时直接选择服务器。', 'muted'))
        layout.addLayout(row(button('＋ 新增服务器', lambda: app.show_dialog(ServerDialog(self, app)), primary=True),
                             button('编辑', self.edit), button('登录 / 测试连接', self.login),
                             button('断开', self.disconnect), button('删除', self.delete)))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['名称', '主机 / 端口', '账号', '连接方式', '状态'])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setColumnWidth(0, 175)
        self.tree.setColumnWidth(1, 220)
        self.tree.setColumnWidth(2, 200)
        self.tree.itemDoubleClicked.connect(lambda item, column: self.edit())
        self.tree.itemSelectionChanged.connect(self.show_info)
        layout.addWidget(self.tree, 1)
        self.info = label('选择服务器查看详情。', 'muted', wrap=True)
        layout.addWidget(self.info)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button('完成', self.accept, primary=True))
        layout.addLayout(actions)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(300)
        self.refresh()

    def selected(self):
        item = self.tree.currentItem()
        return self.app.servers.get(item.data(0, Qt.ItemDataRole.UserRole)) if item else None

    def refresh(self):
        for server_id in list(self.items):
            if server_id not in self.app.servers:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(self.items.pop(server_id)))
        for profile in self.app.servers.values():
            if profile.id not in self.items:
                item = QTreeWidgetItem(self.tree)
                item.setData(0, Qt.ItemDataRole.UserRole, profile.id)
                self.items[profile.id] = item
            item = self.items[profile.id]
            via = self.app.servers.get(profile.jump_server_id)
            status = '连接中…' if profile.id in self.app.connecting else '已登录' if self.app.remote.connected(profile.id) else '未登录'
            for column, value in enumerate((profile.name, f'{profile.host}:{profile.port}', profile.username, via.name if via else '直接连接', status)):
                item.setText(column, value)
        self.show_info()

    def show_info(self):
        if profile := self.selected():
            route = connection_route(profile, self.app.servers)
            self.info.setText(f'连接路径：本机 → {" → ".join(p.name for p in route)}\n{self.app.server_messages.get(profile.id, "尚未测试连接")}')

    def edit(self):
        if profile := self.selected():
            if self.app.server_busy(profile.id):
                QMessageBox.information(self, '服务器使用中', '请先停止相关任务，或等待连接完成。')
            else:
                self.app.show_dialog(ServerDialog(self, self.app, profile))

    def login(self):
        if profile := self.selected():
            self.app.login_server(profile.id, self)

    def disconnect(self):
        if profile := self.selected():
            self.app.disconnect_server(profile.id)

    def delete(self):
        if profile := self.selected():
            try:
                self.app.remove_server(profile.id)
            except ValueError as exc:
                QMessageBox.warning(self, '无法删除服务器', str(exc))

    def cleanup(self):
        self.timer.stop()
        self.app.server_window = None
        if hasattr(self.parentWidget(), 'refresh_endpoints'):
            self.parentWidget().refresh_endpoints()


class CommandPreview(Modal):
    def __init__(self, parent, specs):
        super().__init__(parent)
        self.specs = specs
        self.setWindowTitle('传输命令预览')
        self.resize(1080, 720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.addWidget(label('传输命令预览', 'title'))
        layout.addWidget(label('每条任务先预扫描，再执行实际传输；密码不会写入命令。', 'muted', wrap=True))
        self.mode = QComboBox()
        self.mode.addItems(['实际传输命令', '预扫描命令'])
        self.mode.currentIndexChanged.connect(self.render)
        layout.addLayout(row(self.mode, button('复制全部命令', self.copy_commands)))
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(self.text, 1)
        layout.addWidget(label('复制到外部终端时，使用终端认证或 SSH agent。', 'muted'))
        self.render()

    def render(self, *args):
        dry_run = self.mode.currentIndex() == 1
        separator = ' ' + chr(92) + '\n  '
        self.commands = '\n\n'.join(f'# {n + 1}. {spec.name.replace(chr(10), " ")}\n' + separator.join(shlex.quote(arg) for arg in build_command(spec, dry_run=dry_run)) for n, spec in enumerate(self.specs))
        self.text.setPlainText(self.commands)

    def copy_commands(self):
        QApplication.clipboard().setText(self.commands)


class TaskDialog(Modal):
    def __init__(self, app, editing: Snapshot | None = None):
        super().__init__(app)
        self.app, self.editing, self.sources = app, editing, [editing.spec.source] if editing else []
        self.setWindowTitle('编辑传输' if editing else '新建传输')
        self.resize(980, 780)
        self.setMinimumSize(820, 660)
        spec = editing.spec if editing else None
        self._source = spec.server_id if spec and spec.direction == 'download' else ''
        self._target = spec.server_id if spec and spec.direction == 'upload' else ''
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        heading = QHBoxLayout()
        heading.addWidget(label(self.windowTitle(), 'title'), 1)
        heading.addWidget(button('服务器管理…', lambda: app.open_servers(self)))
        layout.addLayout(heading)
        route = QHBoxLayout()
        self.source_endpoint, self.target_endpoint = QComboBox(), QComboBox()
        for title, combo in (('从哪里传输', self.source_endpoint), ('传输到哪里', self.target_endpoint)):
            card = QGroupBox(title)
            box = QVBoxLayout(card)
            box.addWidget(combo)
            route.addWidget(card, 1)
            if combo is self.source_endpoint:
                route.addWidget(button('⇄', self.swap))
        layout.addLayout(route)
        self.route_hint = label('', 'muted', wrap=True)
        layout.addWidget(self.route_hint)
        layout.addLayout(row(button('＋ 选择文件 / 目录', self.pick_sources), button('输入路径…', self.manual_paths), button('移除选中', self.remove_paths)))
        self.source_list = QListWidget()
        self.source_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        layout.addWidget(self.source_list, 1)
        self.source_count = label('', 'muted')
        layout.addWidget(self.source_count)
        target = QGroupBox('目标目录')
        box = QHBoxLayout(target)
        self.destination = QLineEdit(spec.destination if spec else '')
        box.addWidget(self.destination, 1)
        box.addWidget(button('浏览目录…', self.choose_destination))
        layout.addWidget(target)
        self.name = QLineEdit(spec.name if spec else '')
        self.name.setPlaceholderText('名称前缀（可选）' if not editing else '任务名称')
        self.bandwidth = QSpinBox()
        self.bandwidth.setRange(0, 2_000_000_000)
        self.bandwidth.setValue(spec.bandwidth_kib if spec else 0)
        layout.addLayout(row(self.name, label('限速 KiB/s（0 不限）'), self.bandwidth))
        self.compression = QCheckBox('远端压缩')
        self.compression.setChecked(spec.compression if spec else True)
        self.excludes = QLineEdit(';'.join(spec.excludes) if spec else '')
        self.excludes.setPlaceholderText('排除规则，使用 ; 分隔，例如 .git;*.tmp')
        layout.addLayout(row(self.compression, self.excludes))
        actions = QHBoxLayout()
        actions.addWidget(button('预览传输命令', self.preview))
        actions.addStretch()
        actions.addWidget(button('取消', self.reject))
        actions.addWidget(button('保存任务' if editing else '添加到任务列表', self.submit, primary=True))
        layout.addLayout(actions)
        self.refresh_endpoints()
        self.source_endpoint.currentIndexChanged.connect(lambda index: self.endpoints_changed(True))
        self.target_endpoint.currentIndexChanged.connect(lambda index: self.endpoints_changed(False))
        self.refresh_sources()
        if not spec:
            self.apply_destination_default()

    def apply_destination_default(self, source=None, target=None):
        """Pre-fill the destination with the remembered directory for this server and direction."""
        source = self._source if source is None else source
        target = self._target if target is None else target
        if source:  # download: destination is a local directory
            path = self.app.last_directory_for(source, 'download')
        elif target:  # upload: destination is a remote directory
            path = self.app.last_directory_for(target, 'upload') or self.app.servers[target].initial_directory
        else:  # local to local
            path = self.app.last_directory_for('', 'local')
        self.destination.setText(path)

    def refresh_endpoints(self):
        if self._source and self._source not in self.app.servers:
            self.sources.clear()
            self.refresh_sources()
        if self._target and self._target not in self.app.servers:
            self.destination.clear()
        self._source = self._source if self._source in self.app.servers else ''
        self._target = self._target if self._target in self.app.servers else ''
        self.source_endpoint.blockSignals(True)
        self.source_endpoint.clear()
        self.source_endpoint.addItem('本机', '')
        for profile in self.app.servers.values():
            self.source_endpoint.addItem(profile.name, profile.id)
        self.source_endpoint.setCurrentIndex(max(0, self.source_endpoint.findData(self._source)))
        self.source_endpoint.blockSignals(False)
        self.update_route()

    def update_route(self):
        if self._source:
            self._target = ''
        self.target_endpoint.blockSignals(True)
        self.target_endpoint.clear()
        self.target_endpoint.addItem('本机', '')
        if not self._source:
            for profile in self.app.servers.values():
                self.target_endpoint.addItem(profile.name, profile.id)
        self.target_endpoint.setCurrentIndex(max(0, self.target_endpoint.findData(self._target)))
        self.target_endpoint.blockSignals(False)
        server_id = self._source or self._target
        if server_id:
            route = connection_route(self.app.servers[server_id], self.app.servers)
            self.route_hint.setText(f'连接路径：本机 → {" → ".join(profile.name for profile in route)}')
        else:
            self.route_hint.setText('本机目录之间导出；选择已保存的服务器即可上传或下载。')

    def endpoints_changed(self, source_changed):
        source, target = self.source_endpoint.currentData(), self.target_endpoint.currentData()
        if source_changed and source != self._source:
            self.sources.clear()
            self.refresh_sources()
        if source != self._source or target != self._target:
            self.apply_destination_default(source, target)
        self._source, self._target = source, target
        self.update_route()

    def swap(self):
        self._source, self._target = self._target, self._source
        self.sources.clear()
        self.refresh_endpoints()
        self.apply_destination_default()
        self.refresh_sources()

    def refresh_sources(self):
        self.source_list.clear()
        self.source_list.addItems(self.sources)
        self.source_count.setText(f'{len(self.sources)} 项 · 每个文件或目录独立生成一条任务，保留目录名称')

    def add_paths(self, paths):
        for path in paths:
            value = str(path).rstrip('/') or '/'
            if value not in self.sources:
                self.sources.append(value)
        self.refresh_sources()

    def remove_paths(self):
        remove = {self.source_list.row(item) for item in self.source_list.selectedItems()}
        self.sources = [path for index, path in enumerate(self.sources) if index not in remove]
        self.refresh_sources()

    def pick_sources(self):
        if self._source:  # download: sources live on the remote server
            initial = self.app.last_directory_for(self._source, 'download', 'source')
            picker = RemotePicker(self, self.app, self._source, self.add_paths, initial=initial)
        else:  # upload / local: sources live on this machine
            direction = 'upload' if self._target else 'local'
            initial = self.app.last_directory_for(self._target, direction, 'source')
            picker = MixedPicker(self, self.add_paths, initial=initial)
        return self.app.show_dialog(picker)

    def choose_destination(self):
        if self._target:
            initial = self.app.last_directory_for(self._target, 'upload')
            return self.app.show_dialog(RemotePicker(self, self.app, self._target, self.destination.setText,
                                                     directory_only=True, initial=initial))
        direction = 'download' if self._source else 'local'
        server_id = self._source or ''
        path = QFileDialog.getExistingDirectory(self, '选择目标目录', self.app.last_directory_for(server_id, direction))
        if path:
            self.destination.setText(path)
        return None

    def manual_paths(self):
        window = Modal(self)
        window.setWindowTitle('输入源路径 · 每行一项')
        window.resize(650, 360)
        box = QVBoxLayout(window)
        box.addWidget(label('每行填写一个文件或目录的绝对路径。', 'muted'))
        text = QPlainTextEdit()
        box.addWidget(text, 1)

        def add():
            self.add_paths([path.strip() for path in text.toPlainText().splitlines() if path.strip()])
            window.accept()
        box.addWidget(button('加入列表', add, primary=True))
        self.app.show_dialog(window)

    def build_specs(self):
        if not self.sources or not self.destination.text().strip():
            raise ValueError('请选择源文件 / 目录，并指定目标目录')
        if self._source and self._target:
            raise ValueError('请选择本机与远端之间的传输')
        if self.editing and len(self.sources) != 1:
            raise ValueError('编辑单条任务时请保留一个源路径；批量传输请新建')
        names = [PurePosixPath(path).name for path in self.sources]
        if len(names) != len(set(names)):
            raise ValueError('同名文件或目录会写入同一目标，请拆分到不同目标目录')
        direction = 'download' if self._source else 'upload' if self._target else 'local'
        profile = self.app.servers.get(self._source or self._target)
        specs = []
        for path in self.sources:
            prefix = self.name.text().strip()
            basename = PurePosixPath(path).name or 'root'
            spec = TaskSpec(name=prefix if self.editing else f'{prefix} / {basename}' if prefix else basename,
                            source=path if self._source else local_path(path),
                            destination=self.destination.text().strip() if self._target else local_path(self.destination.text().strip()),
                            direction=direction, compression=self.compression.isChecked(), bandwidth_kib=self.bandwidth.value(),
                            excludes=tuple(rule.strip() for rule in self.excludes.text().split(';') if rule.strip()))
            if profile:
                spec = replace(profile.bind(spec), jump_hosts=connection_route(profile, self.app.servers)[:-1])
            spec.validate()
            specs.append(spec)
        return specs

    def preview(self):
        try:
            return self.app.show_dialog(CommandPreview(self, self.build_specs()))
        except (ValueError, OSError) as exc:
            QMessageBox.critical(self, '无法预览命令', str(exc))
        return None

    def submit(self):
        try:
            specs = self.build_specs()
            if self.editing:
                self.app.manager.update(self.editing.id, specs[0])
            else:
                for spec in specs:
                    self.app.manager.add(spec)
            direction = specs[0].direction
            server_id = self._source if direction == 'download' else self._target if direction == 'upload' else ''
            source_hint = str(PurePosixPath(self.sources[-1]).parent) if self.sources else ''
            self.app.remember_directories(server_id, direction, source=source_hint, destination=self.destination.text())
            self.app.persist()
            self.app.show_tasks(specs[0].direction)
            self.accept()
        except (ValueError, OSError) as exc:
            QMessageBox.critical(self, '无法保存任务', str(exc))
