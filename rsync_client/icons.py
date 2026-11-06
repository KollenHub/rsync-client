"""Application icon; pre-rendered PNGs avoid a QtSvg runtime dependency."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
ASSETS = Path(__file__).resolve().with_name('assets')


def app_icon() -> QIcon:
    icon = QIcon()
    for size in ICON_SIZES:
        filename = 'rsync-desk.png' if size == 512 else f'rsync-desk-{size}.png'
        icon.addFile(str(ASSETS / filename), QSize(size, size))
    return icon
