from __future__ import annotations

import queue
import threading
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QItemSelectionModel, Qt, QTimer
from PySide6.QtGui import QColor, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionProgressBar,
    QTabBar,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .config import default_config_path, load_config, load_task_states, save_config
from .engine import TaskManager, summarize
from .models import ACTIVE, STATUS_LABELS, ServerProfile, connection_route, format_bytes
from .qt_helpers import Modal, button, label, row
from .remote import RemoteService
from .task_view import DIRECTION_TABS, RUNNING, STATUS_TABS, TaskFilter, task_group
from .vault import Vault


class TaskModel(QAbstractTableModel):
    HEADERS = ('文件 / 目录', '传输方向', '目标目录', '状态', '进度', '已处理 / 待更新', '速度', '剩余时间')

    def __init__(self, app):
        super().__init__(app)
        self.app, self.tasks = app, []

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.tasks)

    def columnCount(self, parent=None):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        task = self.tasks[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return task.id
        if role == Qt.ItemDataRole.UserRole + 1:
            return task.percent
        if role == Qt.ItemDataRole.ToolTipRole:
            return f'{task.spec.source}\n→ {task.spec.destination}' + (f'\n{task.error}' if task.error else '')
        if role == Qt.ItemDataRole.ForegroundRole:
            color = {'success': '#17866e', 'failed': '#c24454', 'running': '#6554d9', 'cancelled': '#64748b'}.get(task.status)
            return QColor(color) if color else None
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        profile = self.app.servers.get(task.spec.server_id)
        server = profile.name if profile else task.spec.host
        direction = (f'{server} → {server}' if task.spec.direction == 'remote' else
                     f'{server} → 本机' if task.spec.direction == 'download' else
                     f'本机 → {server}' if task.spec.direction == 'upload' else '本机 → 本机')
        total = format_bytes(task.total_bytes) if task.total_bytes is not None else '—'
        progress = '正在扫描…' if task.status == 'scanning' else '等待扫描' if task.status in {'ready', 'queued'} else f'{task.percent:.1f}%'
        values = (task.spec.name, direction, task.spec.destination, STATUS_LABELS[task.status], progress,
                  f'{format_bytes(task.transferred_bytes)} / {total}',
                  f'{format_bytes(task.speed)}/s' if task.status == 'running' else '—',
                  task.eta if task.status == 'running' else '—')
        return values[index.column()]

    def update_tasks(self, snapshots):
        if [task.id for task in snapshots] != [task.id for task in self.tasks]:
            self.beginResetModel()
            self.tasks = snapshots
            self.endResetModel()
        elif snapshots != self.tasks:
            self.tasks = snapshots
            if snapshots:
                self.dataChanged.emit(self.index(0, 0), self.index(len(snapshots) - 1, self.columnCount() - 1))


class ProgressDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        text = str(index.data())
        if '%' not in text:
            return super().paint(painter, option, index)
        progress = QStyleOptionProgressBar()
        progress.rect = option.rect.adjusted(8, 10, -8, -10)
        progress.minimum, progress.maximum = 0, 1000
        progress.progress = int(float(index.data(Qt.ItemDataRole.UserRole + 1)) * 10)
        progress.text, progress.textVisible = text, True
        QApplication.style().drawControl(QStyle.ControlElement.CE_ProgressBar, progress, painter)


class DetailWindow(Modal):
    def __init__(self, app, task_id):
        super().__init__(app)
        self.app, self.task_id, self.sequence = app, task_id, 0
        self.setWindowTitle('传输详情 · 终端日志')
        self.resize(1040, 720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        self.title_label = label('', 'title')
        self.info = label('', 'muted', wrap=True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.stats = label('', wrap=True)
        for widget in (self.title_label, self.info, self.progress, self.stats):
            layout.addWidget(widget)
        self.follow = QCheckBox('自动滚动')
        self.follow.setChecked(True)
        layout.addLayout(row(label('终端输出', 'subtitle'), self.follow,
                             button('停止', lambda: app.manager.stop([task_id])), button('导出日志…', self.export_logs)))
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(10000)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.text.setStyleSheet('QPlainTextEdit { background: #101827; color: #d5dfef; border: none; padding: 12px; }')
        layout.addWidget(self.text, 1)
        layout.addWidget(label('日志保留最近 10,000 行；速度和字节数为 rsync 逻辑处理量。', 'muted'))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(200)
        self.refresh()

    def refresh(self):
        task = self.app.manager.get(self.task_id)
        if not task:
            self.close()
            return
        self.title_label.setText(task.spec.name)
        endpoint = '本机' if task.spec.direction == 'local' else f'{task.spec.username}@{task.spec.host}:{task.spec.port}'
        self.info.setText(f'{endpoint}\n源：{task.spec.source}\n目标：{task.spec.destination}')
        self.progress.setValue(int(task.percent * 10))
        total = format_bytes(task.total_bytes) if task.total_bytes is not None else '计算中'
        elapsed = int((task.ended_at or time.time()) - task.started_at) if task.started_at else 0
        self.stats.setText(f'{STATUS_LABELS[task.status]}   {task.percent:.1f}%   ·   {format_bytes(task.transferred_bytes)} / {total}'
                           f'   ·   {format_bytes(task.speed)}/s   ·   ETA {task.eta}   ·   {elapsed} 秒\n当前条目：{task.current_file or "—"}'
                           + (f'\n{task.error}' if task.error else ''))
        lines = self.app.manager.logs_since(self.task_id, self.sequence)
        if lines:
            scroll = self.text.verticalScrollBar()
            old = scroll.value()
            self.text.appendPlainText('\n'.join(text for _, text in lines))
            self.sequence = lines[-1][0]
            scroll.setValue(scroll.maximum() if self.follow.isChecked() else old)

    def export_logs(self):
        filename, _ = QFileDialog.getSaveFileName(self, '导出日志', '', '日志 (*.log);;所有文件 (*)')
        if filename:
            try:
                Path(filename).write_text('\n'.join(text for _, text in self.app.manager.logs_since(self.task_id, 0)) + '\n', encoding='utf-8')
            except OSError as exc:
                QMessageBox.critical(self, '导出失败', str(exc))

    def cleanup(self):
        self.timer.stop()
        self.app.details.pop(self.task_id, None)


class App(QMainWindow):
    def __init__(self, config_path: Path | None = None):
        super().__init__()
        self.setWindowTitle(f'Rsync Desk {__version__} · 文件同步客户端')
        self.resize(1320, 860)
        self.setMinimumSize(1080, 720)
        self.config_path = config_path or default_config_path()
        self.vault = Vault(self.config_path.parent / 'credentials.enc')
        self.remote = RemoteService()
        self.details = {}
        self.server_window = None
        self.server_messages = {}
        self.connecting = set()
        self.dialogs = []
        self.last_directories: dict[str, dict[str, dict[str, str]]] = {}
        self._results, self._callbacks = queue.Queue(), {}
        self._next_job, self._closing = 0, False
        error = None
        try:
            specs, concurrency, servers, last_directories = load_config(self.config_path)
            self.last_directories = last_directories
            states = load_task_states(self.config_path)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            specs, concurrency, servers, states, error = [], 2, [], [], str(exc)
        self.servers = {profile.id: profile for profile in servers}
        self.manager = TaskManager(concurrency)
        for index, spec in enumerate(specs):
            task_id = self.manager.add(spec, check_paths=False)
            self.manager.restore(task_id, states[index] if index < len(states) else {})
        self._save_disabled = bool(error)
        self._runtime_signature = self.runtime_signature()
        self._build(concurrency)
        initial = next((direction for direction, _ in DIRECTION_TABS if any(s.direction == direction for s in specs)), 'upload')
        self.show_tasks(initial)
        QShortcut(QKeySequence('Ctrl+N'), self, activated=self.new_transfer)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(100)
        self.refresh()
        if error:
            QTimer.singleShot(0, lambda: QMessageBox.critical(self, '配置读取失败', f'原配置已保留，自动保存已暂停。\n{error}'))

    def _build(self, concurrency):
        content = QWidget()
        self.setCentralWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        brand = QVBoxLayout()
        brand.addWidget(label('Rsync Desk', 'title'))
        brand.addWidget(label('文件同步工作台   /   批量传输 · 实时进度 · 终端日志', 'muted'))
        header = QHBoxLayout()
        logo = label()
        logo.setPixmap(QApplication.windowIcon().pixmap(44, 44))
        header.addWidget(logo)
        header.addLayout(brand, 1)
        header.addWidget(button('服务器管理', self.open_servers))
        header.addWidget(button('＋ 新建传输', self.new_transfer, primary=True))
        layout.addLayout(header)
        metrics = QHBoxLayout()
        self.metric_labels = []
        for title in ('全部任务', '上传运行中', '下载运行中', '失败任务'):
            card = QFrame()
            card.setProperty('role', 'card')
            box = QVBoxLayout(card)
            box.setContentsMargins(18, 12, 18, 12)
            box.addWidget(label(title, 'muted'))
            value = label('0', 'metric')
            self.metric_labels.append(value)
            box.addWidget(value)
            metrics.addWidget(card, 1)
        layout.addLayout(metrics)
        overall = QFrame()
        overall.setProperty('role', 'card')
        box = QVBoxLayout(overall)
        box.setContentsMargins(18, 15, 18, 15)
        self.overall_label = label('', 'muted')
        self.progress_title = label('上传总体进度', 'subtitle')
        box.addLayout(row(self.progress_title, self.overall_label))
        self.overall_bar = QProgressBar()
        self.overall_bar.setRange(0, 1000)
        self.overall_bar.setTextVisible(False)
        self.overall_bar.setFixedHeight(12)
        box.addWidget(self.overall_bar)
        self.overall_info = label('', 'muted')
        box.addWidget(self.overall_info)
        layout.addWidget(overall)
        self.direction_tabs = QTabBar()
        self.direction_tabs.setExpanding(False)
        self.direction_tabs.setDrawBase(False)
        for _, title in DIRECTION_TABS:
            self.direction_tabs.addTab(title)
        layout.addWidget(self.direction_tabs)
        self.status_tabs = QTabBar()
        self.status_tabs.setObjectName('statusTabs')
        self.status_tabs.setExpanding(False)
        self.status_tabs.setDrawBase(False)
        for _, title in STATUS_TABS:
            self.status_tabs.addTab(title)
        layout.addWidget(self.status_tabs)
        toolbar = QHBoxLayout()
        for widget in (label('传输任务', 'subtitle'), button('▶ 开始当前列表', self.start_all),
                       button('开始 / 重试选中', self.start_selected), button('停止选中', self.stop_selected)):
            toolbar.addWidget(widget)
        self.retry_button = button('重试失败任务', self.retry_failed)
        toolbar.addWidget(self.retry_button)
        toolbar.addStretch()
        toolbar.addWidget(label('并发任务数', 'muted'))
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 8)
        self.concurrency.setValue(concurrency)
        self.concurrency.valueChanged.connect(self._concurrency_changed)
        toolbar.addWidget(self.concurrency)
        layout.addLayout(toolbar)
        self.model = TaskModel(self)
        self.proxy = TaskFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setMinimumHeight(70)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for n, width in enumerate((195, 170, 230, 100, 145, 180, 90, 90)):
            self.table.setColumnWidth(n, width)
        self.table.setItemDelegateForColumn(4, ProgressDelegate(self.table))
        self.table.doubleClicked.connect(self.open_details)
        self.table.selectionModel().selectionChanged.connect(self.selected_info)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        actions.addWidget(label('Ctrl / Shift 多选 · 双击查看终端日志', 'muted'), 1)
        for text, command in (('命令预览', self.preview_selected), ('查看详情 / 日志', self.open_details),
                              ('编辑传输', self.edit_selected), ('移除选中', self.remove_selected)):
            actions.addWidget(button(text, command))
        self.clear_button = button('清空列表', self.clear_current)
        self.clear_button.setToolTip('仅清空当前方向、当前分类中的非活动任务记录；运行中和排队中的任务会保留，不删除传输文件。')
        actions.addWidget(self.clear_button)
        layout.addLayout(actions)
        card = QFrame()
        card.setProperty('role', 'card')
        box = QVBoxLayout(card)
        self.selection_info = label('选择任务查看进度，双击打开实时终端。', wrap=True)
        self.selection_bar = QProgressBar()
        self.selection_bar.setRange(0, 1000)
        self.selection_bar.setTextVisible(False)
        self.selection_bar.setFixedHeight(10)
        box.addWidget(self.selection_info)
        box.addWidget(self.selection_bar)
        layout.addWidget(card)
        self.footer = label(f'配置自动保存 · 密码加密保存 · {self.config_path}', 'muted')
        layout.addWidget(self.footer)
        self.direction_tabs.currentChanged.connect(self.filters_changed)
        self.status_tabs.currentChanged.connect(self.filters_changed)

    def show_dialog(self, dialog):
        self.dialogs.append(dialog)
        parent = dialog.parentWidget()

        def closed(_result):
            if dialog in self.dialogs:
                self.dialogs.remove(dialog)
            # Some window managers drop the parent behind the main window when a
            # child dialog closes; bring it back to the front explicitly.
            if parent is not None and parent.isVisible():
                parent.raise_()
                parent.activateWindow()

        dialog.finished.connect(closed)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def new_transfer(self):
        from .dialogs import TaskDialog
        return self.show_dialog(TaskDialog(self))

    def selected_ids(self):
        return [index.data(Qt.ItemDataRole.UserRole) for index in self.table.selectionModel().selectedRows()]

    def current_direction(self):
        return DIRECTION_TABS[self.direction_tabs.currentIndex()][0]

    def current_status(self):
        return STATUS_TABS[self.status_tabs.currentIndex()][0]

    def show_tasks(self, direction, status='all'):
        self.direction_tabs.blockSignals(True)
        self.status_tabs.blockSignals(True)
        self.direction_tabs.setCurrentIndex(next(index for index, item in enumerate(DIRECTION_TABS) if item[0] == direction))
        self.status_tabs.setCurrentIndex(next(index for index, item in enumerate(STATUS_TABS) if item[0] == status))
        self.direction_tabs.blockSignals(False)
        self.status_tabs.blockSignals(False)
        self.filters_changed()

    def filters_changed(self, *args):
        self.table.clearSelection()
        self.proxy.set_filters(self.current_direction(), self.current_status())
        self.refresh()

    def visible_tasks(self):
        return [self.model.tasks[self.proxy.mapToSource(self.proxy.index(index, 0)).row()]
                for index in range(self.proxy.rowCount())]

    def runtime_signature(self):
        return tuple((task.id, task.status) for task in self.manager.snapshots())

    def selected_info(self, *args):
        ids = self.selected_ids()
        task = self.manager.get(ids[0]) if ids else None
        if not task:
            self.selection_info.setText('选择任务查看进度，双击打开实时终端。')
            self.selection_bar.setValue(0)
            return
        self.selection_info.setText(f'{task.spec.name}   ·   {STATUS_LABELS[task.status]}   ·   {task.percent:.1f}%   ·   ETA {task.eta}\n'
                                    f'{task.spec.source} → {task.spec.destination}\n当前条目：{task.current_file or "—"}'
                                    + (f'\n{task.error}' if task.error else ''))
        self.selection_bar.setValue(int(task.percent * 10))

    def refresh(self):
        snapshots = self.manager.snapshots()
        selected = set(self.selected_ids())
        previous_ids = [task.id for task in self.model.tasks]
        self.model.update_tasks(snapshots)
        if previous_ids != [task.id for task in snapshots] and selected:
            for row_index in range(self.proxy.rowCount()):
                index = self.proxy.index(row_index, 0)
                if index.data(Qt.ItemDataRole.UserRole) in selected:
                    self.table.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        counts = {}
        for index, (direction, title) in enumerate(DIRECTION_TABS):
            group = [task for task in snapshots if task.spec.direction == direction]
            counts[direction] = sum(task.status in RUNNING for task in group)
            self.direction_tabs.setTabText(index, f'{title}  {counts[direction]} 运行 · {len(group)} 项')
        tasks = [task for task in snapshots if task.spec.direction == self.current_direction()]
        for index, (status, title) in enumerate(STATUS_TABS):
            count = len(tasks) if status == 'all' else sum(task_group(task.status) == status for task in tasks)
            self.status_tabs.setTabText(index, f'{title} ({count})')
        # The overall bar tracks only this batch (queued/scanning/running/cancelling),
        # not the whole history list, so it reflects "this run" progress.
        active = [task for task in tasks if task.status in ACTIVE]
        summary = summarize(active)
        queued = sum(task.status == 'queued' for task in tasks)
        for widget, value in zip(self.metric_labels, (len(snapshots), counts['upload'], counts['download'], sum(task.status == 'failed' for task in snapshots))):
            widget.setText(str(value))
        title = dict(DIRECTION_TABS)[self.current_direction()].replace('↑ ', '').replace('↓ ', '').replace('⇄ ', '')
        self.progress_title.setText(f'{title}总体进度')
        if active:
            self.overall_bar.setValue(int(summary.percent * 10))
            self.overall_label.setText(f'{summary.percent:.1f}%   ·   运行中 {counts[self.current_direction()]} · 排队 {queued}')
            pending = f'   ·   {summary.unknown} 项尚未扫描，当前为已知数据估计' if summary.unknown else ''
            if summary.total:
                self.overall_info.setText(f'{counts[self.current_direction()]} 个运行中 · {queued} 个排队   ·   {format_bytes(summary.transferred_bytes)} / {format_bytes(summary.total_bytes)}'
                                          f'   ·   合计速度 {format_bytes(summary.speed)}/s{pending}')
            else:
                self.overall_info.setText(f'{counts[self.current_direction()]} 个运行中 · {queued} 个排队   ·   正在预扫描，稍后显示数据量')
        else:
            self.overall_bar.setValue(0)
            self.overall_label.setText('空闲   ·   运行中 0 · 排队 0')
            self.overall_info.setText('当前没有运行中的任务；总体进度只统计本次运行的任务。')
        visible = self.visible_tasks()
        self.retry_button.setEnabled(any(task.status == 'failed' for task in visible))
        self.clear_button.setEnabled(any(task.status not in ACTIVE for task in visible))
        self.selected_info()
        if self.runtime_signature() != self._runtime_signature:
            self.persist(quiet=True)

    def persist(self, *, quiet=False):
        if self._save_disabled:
            return
        try:
            snapshots = self.manager.snapshots()
            save_config(self.config_path, [s.spec for s in snapshots], self.concurrency.value(), list(self.servers.values()),
                        states=[s.public_state() for s in snapshots], last_directories=self.last_directories)
            self._runtime_signature = tuple((task.id, task.status) for task in snapshots)
        except (OSError, ValueError) as exc:
            self._runtime_signature = self.runtime_signature()
            self.footer.setText(f'保存失败：{exc}')
            if not quiet:
                QMessageBox.critical(self, '配置保存失败', str(exc))

    def last_directory_for(self, server_id: str, direction: str, kind: str = 'destination') -> str:
        """Last used source/destination directory for one server and transfer direction."""
        return self.last_directories.get(server_id or '', {}).get(direction, {}).get(kind, '')

    def remember_directories(self, server_id: str, direction: str, source: str = '', destination: str = '') -> None:
        entry = self.last_directories.setdefault(server_id or '', {}).setdefault(direction, {})
        changed = False
        for kind, path in (('source', source.strip()), ('destination', destination.strip())):
            if path and entry.get(kind) != path:
                entry[kind] = path
                changed = True
        if changed:
            self.persist(quiet=True)

    def disconnect_server(self, server_id: str, message: str | None = '已断开浏览连接') -> None:
        """Close the SSH browsing session off the GUI thread; paramiko teardown blocks."""
        service = self.remote

        def done(_result, error):
            if error:
                self.server_messages[server_id] = f'断开失败：{error}'
            elif message:
                self.server_messages[server_id] = message

        self.background(lambda: service.disconnect(server_id), done)

    def unlock_vault(self, parent=None):
        if self.vault.unlocked:
            return True
        existing = self.vault.path.exists()
        prompt = '输入主密码以解锁已保存的 SSH 密码。' if existing else '设置密码库主密码（至少 8 位），请妥善记住。'
        password, ok = QInputDialog.getText(parent or self, '解锁密码库' if existing else '创建密码库', prompt, QLineEdit.EchoMode.Password)
        if not ok:
            return False
        try:
            if not existing:
                if len(password) < 8:
                    raise ValueError('主密码至少需要 8 位')
                confirmation, ok = QInputDialog.getText(parent or self, '确认主密码', '再次输入主密码', QLineEdit.EchoMode.Password)
                if not ok:
                    return False
                if confirmation != password:
                    raise ValueError('两次输入的主密码不一致')
            self.vault.unlock(password)
            return True
        except (ValueError, OSError, KeyError, TypeError) as exc:
            QMessageBox.critical(parent or self, '密码库无法解锁', str(exc))
            return False

    def _concurrency_changed(self, count):
        self.manager.set_concurrency(count)
        self.persist()

    def start(self, ids):
        try:
            resolved = {}
            candidates = [task_id for task_id in ids
                          if (task := self.manager.get(task_id)) and task.status not in ACTIVE]
            for task_id in candidates:
                task = self.manager.get(task_id)
                if task.spec.server_id:
                    server_id = task.spec.server_id
                    if server_id not in resolved:
                        route = self.resolve_server(server_id)
                        if not route:
                            return
                        resolved[server_id] = route
                    route = resolved[server_id]
                    self.manager.update(task_id, replace(route[-1].bind(task.spec), jump_hosts=route[:-1]))
            self.manager.start(candidates)
            if candidates:
                self.show_tasks(self.current_direction(), 'running')
        except (ValueError, OSError) as exc:
            QMessageBox.critical(self, '无法启动任务', str(exc))

    def start_all(self):
        self.start([s.id for s in self.visible_tasks() if s.status not in ACTIVE and s.status != 'success'])

    def start_selected(self):
        self.start(self.selected_ids())

    def stop_selected(self):
        self.manager.stop(self.selected_ids())

    def retry_failed(self):
        ids = [task.id for task in self.visible_tasks() if task.status == 'failed']
        if ids:
            self.start(ids)  # start() switches to the running tab

    def clear_current(self):
        visible = self.visible_tasks()
        removed = [task for task in visible if task.status not in ACTIVE]
        for task in removed:
            self.manager.remove(task.id)
            if task.id in self.details:
                self.details[task.id].close()
        self.persist()
        self.refresh()
        self.footer.setText(f'已清空 {len(removed)} 条记录，保留 {len(visible) - len(removed)} 条进行中或排队任务。')

    def edit_selected(self):
        from .dialogs import TaskDialog
        if ids := self.selected_ids():
            task = self.manager.get(ids[0])
            if task.status in ACTIVE:
                QMessageBox.information(self, '任务进行中', '请先停止任务再编辑。')
            else:
                self.show_dialog(TaskDialog(self, task))

    def remove_selected(self):
        ids = self.selected_ids()
        if any(self.manager.get(i).status in ACTIVE for i in ids):
            QMessageBox.information(self, '任务进行中', '请先停止选中的任务再移除。')
            return
        for task_id in ids:
            self.manager.remove(task_id)
        self.persist()
        self.refresh()

    def open_details(self, *args):
        if ids := self.selected_ids():
            task_id = ids[0]
            if task_id in self.details:
                self.details[task_id].raise_()
            else:
                dialog = DetailWindow(self, task_id)
                dialog.setWindowModality(Qt.WindowModality.NonModal)
                self.details[task_id] = dialog
                self.show_dialog(dialog)

    def preview_selected(self):
        from .dialogs import CommandPreview
        specs = []
        for task_id in self.selected_ids():
            spec = self.manager.get(task_id).spec
            if spec.server_id:
                profile = self.servers[spec.server_id]
                spec = replace(profile.bind(spec), jump_hosts=connection_route(profile, self.servers)[:-1])
            specs.append(spec)
        if specs:
            self.show_dialog(CommandPreview(self, specs))

    def _poll(self):
        if self._closing:
            return
        deadline = time.monotonic() + 0.012
        while time.monotonic() < deadline:
            try:
                job_id, finished, result, error = self._results.get_nowait()
            except queue.Empty:
                break
            callbacks = self._callbacks.pop(job_id, None) if finished else self._callbacks.get(job_id)
            if callbacks:
                if finished:
                    callbacks[0](result, error)
                elif callbacks[1]:
                    callbacks[1](result)
        self.refresh()
        self.timer.setInterval(50 if not self._results.empty() else 100)

    def background(self, work, callback, *, on_progress=None):
        secrets = tuple(p.password for p in self.servers.values() if p.password)
        results = self._results
        self._next_job += 1
        job_id = self._next_job
        self._callbacks[job_id] = (callback, on_progress)
        streaming = on_progress is not None

        def publish(payload):
            results.put((job_id, False, payload, None))

        def run():
            try:
                result, error = work(publish) if streaming else work(), None
            except Exception as exc:  # noqa: BLE001 -- marshal network errors to the GUI thread
                result, error = None, str(exc)
                for password in secrets:
                    error = error.replace(password, '******')
            results.put((job_id, True, result, error))
        threading.Thread(target=run, daemon=True).start()

    def open_servers(self, parent=None):
        from .dialogs import ServerManager
        if self.server_window:
            self.server_window.raise_()
            self.server_window.activateWindow()
        else:
            self.server_window = self.show_dialog(ServerManager(parent or self, self))
        return self.server_window

    def server_busy(self, server_id):
        return any(any(p.id == server_id for p in connection_route(self.servers[key], self.servers))
                   for key in self.connecting if key in self.servers) or any(
            s.status in ACTIVE and (s.spec.server_id == server_id or any(p.id == server_id for p in s.spec.jump_hosts))
            for s in self.manager.snapshots())

    def validate_server(self, profile):
        if any(p.name == profile.name and p.id != profile.id for p in self.servers.values()):
            raise ValueError('服务器名称已存在')
        if self.server_busy(profile.id):
            raise ValueError('请先停止相关任务，或等待登录结束')
        connection_route(profile, {**self.servers, profile.id: profile})

    def save_server(self, profile: ServerProfile):
        profile.validate()
        self.validate_server(profile)
        self.servers[profile.id] = profile
        for candidate in self.servers.values():
            if any(p.id == profile.id for p in connection_route(candidate, self.servers)):
                self.disconnect_server(candidate.id, message=None)
                self.server_messages.pop(candidate.id, None)
        self.persist()

    def remove_server(self, server_id):
        if self.server_busy(server_id):
            raise ValueError('请先停止相关任务，或等待连接结束')
        if any(s.spec.server_id == server_id for s in self.manager.snapshots()):
            raise ValueError('服务器仍被任务引用，请先移除或修改相关任务')
        if any(s.jump_server_id == server_id for s in self.servers.values()):
            raise ValueError('服务器仍被用作跳板机，请先修改相关服务器')
        self.disconnect_server(server_id, message=None)
        self.server_messages.pop(server_id, None)
        del self.servers[server_id]
        self.last_directories.pop(server_id, None)
        self.persist()

    def resolve_server(self, server_id, parent=None):
        resolved = []
        for profile in connection_route(self.servers[server_id], self.servers):
            if profile.credential_id and not profile.password:
                if not self.unlock_vault(parent):
                    return None
                profile = replace(profile, password=self.vault.get(profile.credential_id))
                self.servers[profile.id] = profile
            resolved.append(profile)
        return tuple(resolved)

    def login_server(self, server_id, parent=None):
        if server_id in self.connecting:
            return
        try:
            route = self.resolve_server(server_id, parent)
            if not route:
                return
        except (ValueError, KeyError) as exc:
            QMessageBox.critical(parent or self, '无法登录', str(exc))
            return
        self.connecting.add(server_id)
        self.server_messages[server_id] = '正在连接…'

        def done(result, error):
            self.connecting.discard(server_id)
            if self.servers.get(server_id) is route[-1]:
                self.server_messages[server_id] = f'登录失败：{error}' if error else f'登录成功 · {result}'
        service = self.remote
        self.background(lambda: service.login(route[-1], route[:-1]), done)

    def closeEvent(self, event):
        if not self._closing:
            self.persist()
            self._closing = True
            self._callbacks.clear()
            # SSH teardown can block on network IO; keep the GUI responsive while closing.
            threading.Thread(target=self.remote.close, daemon=True).start()
            self.manager.stop([task.id for task in self.manager.snapshots()])
            self.centralWidget().setEnabled(False)
        if any(task.status in ACTIVE for task in self.manager.snapshots()):
            self.footer.setText('正在停止传输并关闭…')
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        self.timer.stop()
        self.manager.shutdown(timeout=0)
        self.vault.lock()
        for dialog in list(self.dialogs):
            dialog.close()
        event.accept()
