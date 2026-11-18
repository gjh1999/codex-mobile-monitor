"""Render the translucent 澜 icon for Windows Explorer, the window and tray."""
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen


def icon_png(size):
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size / 256, size / 256)
    surface = QPainterPath()
    surface.addRoundedRect(QRectF(11, 11, 234, 234), 57, 57)
    glass = QLinearGradient(34, 14, 209, 244)
    glass.setColorAt(0, QColor(228, 251, 255, 234))
    glass.setColorAt(.48, QColor(146, 223, 246, 220))
    glass.setColorAt(1, QColor(67, 165, 217, 225))
    painter.fillPath(surface, QBrush(glass))
    painter.setPen(QPen(QColor(233, 252, 255, 196), 6))
    painter.drawPath(surface)
    font = QFont('Microsoft YaHei', 122, QFont.Weight.Bold)
    glyph = QPainterPath()
    glyph.addText(0, 0, font, '澜')
    center = glyph.boundingRect().center()
    painter.translate(128 - center.x(), 126 - center.y())
    painter.fillPath(glyph, QColor('#133e58'))
    painter.end()
    result = QByteArray()
    buffer = QBuffer(result)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, 'PNG'):
        raise RuntimeError('Could not encode icon frame')
    return bytes(result)


def main():
    app = QGuiApplication(sys.argv[:1])
    sizes = (16, 24, 32, 48, 64, 128, 256)
    blobs = [icon_png(size) for size in sizes]
    offset = 6 + 16 * len(blobs)
    entries = []
    for size, data in zip(sizes, blobs):
        dimension = size % 256
        entries.append(struct.pack('<BBBBHHII', dimension, dimension, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    out = Path(__file__).resolve().parents[2] / 'app.ico'
    out.write_bytes(struct.pack('<HHH', 0, 1, len(blobs)) + b''.join(entries) + b''.join(blobs))
    print(out)
    app.quit()


if __name__ == '__main__':
    main()
