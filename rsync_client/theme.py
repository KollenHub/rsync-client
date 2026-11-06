"""Qt Widgets light theme, without QML or WebEngine dependencies."""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .icons import app_icon

ACCENT = '#6554d9'
BG = '#f3f5fa'
INK = '#1e293b'
MUTED = '#64748b'
WHITE = '#ffffff'
STYLE = '''
QWidget { color: #1e293b; font-size: 13px; }
QMainWindow, QDialog { background: #f3f5fa; }
QLabel { background: transparent; }
QLabel[role="title"] { font-size: 26px; font-weight: 700; }
QLabel[role="subtitle"] { font-size: 16px; font-weight: 600; }
QLabel[role="muted"] { color: #64748b; }
QLabel[role="metric"] { font-size: 29px; font-weight: 700; }
QFrame[role="card"] { background: white; border-radius: 12px; }
QPushButton { background: white; border: 1px solid #dce2ee; border-radius: 7px;
 padding: 8px 14px; min-height: 20px; }
QPushButton:hover { background: #eeeaff; border-color: #b9adeb; }
QPushButton:pressed { background: #ddd7ff; }
QPushButton:disabled { color: #9aa4b5; background: #edf0f5; }
QPushButton[primary="true"] { color: white; background: #6554d9; border-color: #6554d9; }
QPushButton[primary="true"]:hover { background: #5544c6; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit { background: white; border: 1px solid #dce2ee;
 border-radius: 7px; padding: 7px; selection-background-color: #6554d9; selection-color: white; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus { border-color: #6554d9; }
QComboBox { padding-right: 28px; min-height: 20px; }
QComboBox::drop-down { border: none; width: 26px; }
QComboBox QAbstractItemView { background: white; color: #1e293b; border: 1px solid #dce2ee;
 outline: none; selection-background-color: #e9e5ff; selection-color: #1e293b; padding: 4px; }
QTableView, QTreeView, QListWidget { background: white; alternate-background-color: #fafbfe;
 border: 1px solid #e1e6f0; border-radius: 8px; outline: none;
 selection-background-color: #e9e5ff; selection-color: #1e293b; }
QTreeView::item, QListWidget::item { min-height: 30px; padding: 2px 5px; }
QHeaderView::section { background: #edf0f7; color: #64748b; border: none;
 padding: 11px 8px; font-weight: 600; }
QScrollBar:vertical { background: #eef1f7; width: 10px; margin: 1px; border-radius: 5px; }
QScrollBar:horizontal { background: #eef1f7; height: 10px; margin: 1px; border-radius: 5px; }
QScrollBar::handle { background: #c6cddd; border-radius: 4px; min-height: 26px; min-width: 26px; }
QScrollBar::handle:hover { background: #a99be9; }
QScrollBar::handle:pressed { background: #6554d9; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QProgressBar { background: #e8eaf2; border: none; border-radius: 5px; min-height: 10px;
 text-align: center; color: #1e293b; }
QProgressBar::chunk { background: #6554d9; border-radius: 5px; }
QGroupBox { border: 1px solid #dce2ee; border-radius: 9px; margin-top: 10px; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #64748b; }
QCheckBox { spacing: 7px; }
QTabBar::tab { background: white; color: #64748b; border: 1px solid #dce2ee;
 border-radius: 7px; padding: 9px 18px; margin-right: 8px; }
QTabBar::tab:selected { background: #6554d9; color: white; border-color: #6554d9; }
QTabBar::tab:hover:!selected { background: #eeeaff; color: #6554d9; }
QTabBar#statusTabs::tab { border: none; background: transparent; padding: 7px 12px; }
QTabBar#statusTabs::tab:selected { background: #e9e5ff; color: #6554d9; }
QToolTip { background: #1e293b; color: white; border: none; padding: 5px; }
'''


def setup_style(app: QApplication):
    app.setDesktopFileName('rsync-desk')
    app.setWindowIcon(app_icon())
    app.setStyle('Fusion')
    palette = QPalette()
    for role, color in ((QPalette.ColorRole.Window, BG), (QPalette.ColorRole.WindowText, INK),
                        (QPalette.ColorRole.Base, WHITE), (QPalette.ColorRole.Text, INK),
                        (QPalette.ColorRole.Button, WHITE), (QPalette.ColorRole.ButtonText, INK),
                        (QPalette.ColorRole.Highlight, ACCENT), (QPalette.ColorRole.HighlightedText, WHITE)):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet(STYLE)
