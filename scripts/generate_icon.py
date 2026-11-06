"""Render the code-native sync mark using QtGui only (no QtSvg dependency)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen

ROOT = Path(__file__).resolve().parents[1] / 'rsync_client' / 'assets'
SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
  <title>Rsync Desk</title>
  <defs>
    <linearGradient id="background" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#8976f5"/>
      <stop offset="1" stop-color="#5140b8"/>
    </linearGradient>
  </defs>
  <rect x="24" y="24" width="464" height="464" rx="108" fill="url(#background)"/>
  <g fill="none" stroke="#fff" stroke-width="34" stroke-linecap="round">
    <path d="M 144 228 C 158 173 201 140 253 140 C 291 140 323 157 346 189"/>
    <path d="M 144 228 C 158 173 201 140 253 140 C 291 140 323 157 346 189" transform="rotate(180 256 256)"/>
  </g>
  <g fill="#fff">
    <path d="M 366 132 L 366 222 L 276 222 Z"/>
    <path d="M 366 132 L 366 222 L 276 222 Z" transform="rotate(180 256 256)"/>
  </g>
</svg>
'''


def draw(size):
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 512, size / 512)
    gradient = QLinearGradient(24, 24, 488, 488)
    gradient.setColorAt(0, QColor('#8976f5'))
    gradient.setColorAt(1, QColor('#5140b8'))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(24, 24, 464, 464, 108, 108)
    arc = QPainterPath()
    arc.moveTo(144, 228)
    arc.cubicTo(158, 173, 201, 140, 253, 140)
    arc.cubicTo(291, 140, 323, 157, 346, 189)
    head = QPainterPath()
    head.moveTo(366, 132)
    head.lineTo(366, 222)
    head.lineTo(276, 222)
    head.closeSubpath()
    for angle in (0, 180):
        painter.save()
        painter.translate(256, 256)
        painter.rotate(angle)
        painter.translate(-256, -256)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor('white'), 34, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawPath(arc)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('white'))
        painter.drawPath(head)
        painter.restore()
    painter.end()
    return image


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / 'rsync-desk.svg').write_text(SVG, encoding='utf-8')
    for size in SIZES:
        filename = 'rsync-desk.png' if size == 512 else f'rsync-desk-{size}.png'
        if not draw(size).save(str(ROOT / filename)):
            raise RuntimeError(f'Unable to write {filename}')
    print(f'Wrote SVG and {len(SIZES)} PNG sizes to {ROOT}')


if __name__ == '__main__':
    main()
