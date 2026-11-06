"""Direction and lifecycle groups shared by the task list and its counters."""
from __future__ import annotations

from PySide6.QtCore import QSortFilterProxyModel

RUNNING = frozenset({'scanning', 'running', 'cancelling'})
DIRECTION_TABS = (('upload', '↑ 上传'), ('download', '↓ 下载'), ('local', '本地导出'))
STATUS_TABS = (('all', '全部'), ('pending', '待开始 / 排队'), ('running', '运行中'),
               ('success', '已完成'), ('failed', '已失败'), ('cancelled', '已停止'))


def task_group(status: str) -> str:
    if status in RUNNING:
        return 'running'
    if status in {'ready', 'queued'}:
        return 'pending'
    return status


class TaskFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.direction, self.status = 'upload', 'all'
        self.setDynamicSortFilter(True)

    def set_filters(self, direction: str, status: str):
        self.direction, self.status = direction, status
        self.invalidate()

    def filterAcceptsRow(self, source_row, source_parent):
        task = self.sourceModel().tasks[source_row]
        return task.spec.direction == self.direction and (self.status == 'all' or task_group(task.status) == self.status)
