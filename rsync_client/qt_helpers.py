from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeView,
)


def label(text='', role='', *, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    if role:
        widget.setProperty('role', role)
    return widget


def button(text, command, *, primary=False):
    widget = QPushButton(text)
    widget.setProperty('primary', primary)
    widget.setAutoDefault(False)
    widget.clicked.connect(command)
    return widget


def row(*widgets):
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    for widget in widgets:
        layout.addWidget(widget)
    return layout


def configure_tree(tree: QTreeView):
    tree.setRootIsDecorated(False)
    tree.setUniformRowHeights(True)
    tree.setAlternatingRowColors(True)
    tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)


class Modal(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._alive = True
        self.finished.connect(self._finished)

    def _finished(self, result):
        self._alive = False
        self.cleanup()

    def cleanup(self):
        pass
