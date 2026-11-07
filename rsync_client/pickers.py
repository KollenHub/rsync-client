from __future__ import annotations

import threading
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QDir, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QFileSystemModel,
    QHBoxLayout,
    QLineEdit,
    QTreeView,
    QVBoxLayout,
)

from .models import format_bytes
from .qt_helpers import Modal, button, configure_tree, label


class LocalFileModel(QFileSystemModel):
    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ('名称', '大小', '类型', '修改时间')[section]
        return super().headerData(section, orientation, role)


class MixedPicker(Modal):
    def __init__(self, parent, callback, initial=''):
        super().__init__(parent)
        self.callback, self.basket = callback, {}
        self.setWindowTitle('选择多个文件和目录')
        self.resize(880, 600)
        self.setMinimumSize(650, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        self.location = QLineEdit(initial or str(Path.home()))
        nav = QHBoxLayout()
        nav.addWidget(button('↑ 上一级', self.up))
        nav.addWidget(self.location, 1)
        nav.addWidget(button('打开', self.load))
        layout.addLayout(nav)
        self.location.returnPressed.connect(self.load)
        layout.addWidget(label('Ctrl / Shift 多选；双击目录进入，双击文件加入待选，最后点击“确认选择”。', 'muted', wrap=True))
        self.model = LocalFileModel(self)
        self.model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden)
        self.model.setOption(QFileSystemModel.Option.DontUseCustomDirectoryIcons)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        configure_tree(self.tree)
        self.tree.setColumnWidth(0, 430)
        self.tree.setColumnHidden(2, True)
        self.tree.activated.connect(self.activate)
        layout.addWidget(self.tree, 1)
        self.count = label('待选 0 项', 'muted')
        footer = QHBoxLayout()
        footer.addWidget(self.count)
        footer.addWidget(button('加入待选', self.collect))
        footer.addWidget(button('清空', self.clear))
        footer.addStretch()
        self.confirm = button('确认选择', self.confirm_selection, primary=True)
        footer.addWidget(self.confirm)
        layout.addLayout(footer)
        self.load()

    def load(self):
        path = str(Path(self.location.text()).expanduser().absolute())
        if not Path(path).is_dir():
            self.count.setText('目录不存在或无法打开')
            return
        self.location.setText(path)
        self.model.setRootPath(path)
        self.tree.setRootIndex(self.model.index(path))

    def up(self):
        self.location.setText(str(Path(self.location.text()).parent))
        self.load()

    def activate(self, index):
        index = index.siblingAtColumn(0)
        if self.model.isDir(index):
            self.location.setText(self.model.filePath(index))
            self.load()
        else:
            self.basket[self.model.filePath(index)] = self.model.filePath(index)
            self.tree.clearSelection()
            self.count.setText(f'待选 {len(self.basket)} 项')

    def collect(self):
        for index in self.tree.selectionModel().selectedRows():
            path = self.model.filePath(index)
            self.basket[path] = path
        self.tree.clearSelection()
        self.count.setText(f'待选 {len(self.basket)} 项')

    def clear(self):
        self.basket.clear()
        self.count.setText('待选 0 项')

    def confirm_selection(self):
        self.collect()
        if self.basket:
            self.callback(list(self.basket))
            self.accept()
        else:
            self.count.setText('请先选择文件或目录')


class RemotePicker(Modal):
    def __init__(self, parent, app, server_id, callback, *, directory_only=False, initial=''):
        super().__init__(parent)
        self.app, self.server_id, self.callback = app, server_id, callback
        self.directory_only = directory_only
        self.entries, self.basket = {}, {}
        self.generation, self.current_directory = 0, ''
        self.cancel = None
        self.setWindowTitle(f'{app.servers[server_id].name} · 选择目录' if directory_only else f'{app.servers[server_id].name} · 选择文件和目录')
        self.resize(900, 620)
        self.setMinimumSize(700, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        self.location = QLineEdit(initial or app.servers[server_id].initial_directory)
        nav = QHBoxLayout()
        nav.addWidget(button('↑ 上一级', self.up))
        nav.addWidget(self.location, 1)
        nav.addWidget(button('打开', self.load))
        nav.addWidget(button('刷新', lambda: self.load(force_refresh=True)))
        layout.addLayout(nav)
        self.location.returnPressed.connect(self.load)
        layout.addWidget(label('双击目录进入，双击文件加入待选；Ctrl / Shift 多选后点击“确认选择”。' if not directory_only else '双击进入目录，再点击“选择当前目录”。', 'muted', wrap=True))
        self.model = QStandardItemModel(0, 3, self)
        self.model.setHorizontalHeaderLabels(['名称', '类型', '大小'])
        self.model.setSortRole(Qt.ItemDataRole.UserRole + 1)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        configure_tree(self.tree)
        self.tree.setColumnWidth(0, 530)
        self.tree.activated.connect(self.activate)
        layout.addWidget(self.tree, 1)
        self.status = label('正在连接…', 'muted', wrap=True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        if not directory_only:
            actions.addWidget(button('加入待选', self.collect))
            actions.addWidget(button('清空待选', self.clear))
        actions.addStretch()
        self.confirm = button('选择当前目录' if directory_only else '确认选择', self.confirm_selection, primary=True)
        actions.addWidget(self.confirm)
        layout.addLayout(actions)
        self.load()

    def load(self, *, force_refresh=False):
        try:
            route = self.app.resolve_server(self.server_id, self)
        except (ValueError, KeyError) as exc:
            self.status.setText(str(exc))
            return
        if not route:
            self.status.setText('解锁已取消，可重新打开目录。')
            return
        self.generation += 1
        generation = self.generation
        if self.cancel:
            self.cancel.set()
        self.cancel = cancel = threading.Event()
        self.model.removeRows(0, self.model.rowCount())
        self.entries.clear()
        self.current_directory = ''
        self.confirm.setEnabled(False)
        self.status.setText('正在读取远端目录…')
        requested = self.location.text()

        def batch(payload):
            if not self._alive or generation != self.generation:
                return
            directory, entries = payload
            self.current_directory = directory
            self.location.setText(directory)
            for entry in entries:
                if self.directory_only and not (entry.is_directory or entry.is_symlink):
                    continue
                self.entries[entry.path] = entry
                kind = '链接' if entry.is_symlink else '目录' if entry.is_directory else '文件'
                name = QStandardItem(entry.name)
                name.setData(entry.path, Qt.ItemDataRole.UserRole)
                name.setData(('0' if entry.is_directory or entry.is_symlink else '1') + entry.name.casefold(), Qt.ItemDataRole.UserRole + 1)
                self.model.appendRow([name, QStandardItem(kind), QStandardItem('—' if entry.is_directory else format_bytes(entry.size))])
            self.confirm.setEnabled(True)
            self.status.setText(f'已读取 {len(self.entries)} 项 · 待选 {len(self.basket)} 项 · 正在读取…')

        def done(result, error):
            if not self._alive or generation != self.generation:
                return
            if error:
                self.status.setText(f'无法读取目录：{error}')
                self.confirm.setEnabled(bool(self.basket) and not self.directory_only)
                return
            directory, _ = result
            self.current_directory = directory
            self.location.setText(directory)
            self.model.sort(0)
            self.confirm.setEnabled(True)
            self.status.setText(f'{len(self.entries)} 项 · 待选 {len(self.basket)} 项')

        service = self.app.remote
        self.app.background(lambda publish: service.list_directory(route[-1], requested, route[:-1],
                            force_refresh=force_refresh, on_batch=publish, cancel=cancel), done, on_progress=batch)

    def up(self):
        self.location.setText(str(PurePosixPath(self.current_directory or '/').parent))
        self.load()

    def activate(self, index):
        path = index.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole)
        entry = self.entries[path]
        if entry.is_symlink:
            route = self.app.resolve_server(self.server_id, self)
            if not route:
                return
            generation = self.generation
            service = self.app.remote

            def resolved(result, error):
                if not self._alive or generation != self.generation:
                    return
                if error:
                    self.status.setText(f'无法打开链接：{error}')
                else:
                    self.activate_entry(result)
            self.app.background(lambda: service.resolve_symlink(route[-1], entry, route[:-1]), resolved)
        else:
            self.activate_entry(entry)

    def activate_entry(self, entry):
        if entry.is_directory:
            self.location.setText(entry.path)
            self.load()
        elif not self.directory_only:
            self.basket[entry.path] = entry.path
            self.tree.clearSelection()
            self.status.setText(f'待选 {len(self.basket)} 项')
        else:
            self.status.setText('此链接指向文件，请选择目录。')

    def collect(self):
        for index in self.tree.selectionModel().selectedRows():
            path = index.data(Qt.ItemDataRole.UserRole)
            self.basket[path] = path
        self.tree.clearSelection()
        self.status.setText(f'待选 {len(self.basket)} 项')

    def clear(self):
        self.basket.clear()
        self.status.setText('待选 0 项')

    def confirm_selection(self):
        if self.directory_only:
            if self.current_directory:
                self.callback(self.current_directory)
                self.accept()
        else:
            self.collect()
            if self.basket:
                self.callback(list(self.basket))
                self.accept()

    def cleanup(self):
        if self.cancel:
            self.cancel.set()
