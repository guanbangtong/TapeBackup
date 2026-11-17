"""The application icon, drawn in code so the window icon and the exe icon come from one place.

    python -m tape_backup.icon <out.ico>     writes a multi-size .ico (build.ps1 uses this for the exe)

The design is a tape cartridge on a 256 x 256 grid. Small sizes (taskbar, title bar) get a
simplified drawing, because thin details turn into blur at 16 pixels.
"""

from __future__ import annotations

import struct
import sys

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)  # what Windows picks from for the taskbar, explorer, etc.

BODY_TOP = QColor("#3b82f6")
BODY_BOTTOM = QColor("#1d4ed8")
DARK = QColor("#1e3a8a")
WINDOW = QColor("#dbeafe")
WHITE = QColor("#ffffff")


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 256, size / 256)  # draw on a 256 grid at every size
    p.setPen(Qt.NoPen)

    body = QRectF(12, 36, 232, 184)
    gradient = QLinearGradient(body.topLeft(), body.bottomLeft())
    gradient.setColorAt(0, BODY_TOP)
    gradient.setColorAt(1, BODY_BOTTOM)
    p.setBrush(gradient)
    p.drawRoundedRect(body, 30, 30)

    # The two reels hold different amounts of tape, like a tape that is partly wound;
    # (centre x, radius of the wound tape)
    if size < 32:
        # Simplified: a light window with the two reels, nothing else.
        p.setBrush(WINDOW)
        p.drawRoundedRect(QRectF(32, 72, 192, 104), 22, 22)
        for x, r in ((92, 42), (172, 30)):
            p.setBrush(DARK)
            p.drawEllipse(QPointF(x, 124), r, r)
            p.setBrush(WHITE)
            p.drawEllipse(QPointF(x, 124), 13, 13)
    else:
        # Window with the two reels.
        p.setBrush(WINDOW)
        p.drawRoundedRect(QRectF(40, 62, 176, 96), 18, 18)
        for x, r in ((96, 34), (166, 24)):
            p.setBrush(DARK)
            p.drawEllipse(QPointF(x, 110), r, r)
            p.setBrush(WHITE)
            p.drawEllipse(QPointF(x, 110), 12, 12)
            p.setBrush(DARK)
            p.drawEllipse(QPointF(x, 110), 4, 4)
        # Bottom guide, like the tape opening of a cassette.
        guide = QPainterPath()
        guide.moveTo(62, 220)
        guide.lineTo(80, 180)
        guide.lineTo(176, 180)
        guide.lineTo(194, 220)
        guide.closeSubpath()
        p.setBrush(DARK)
        p.drawPath(guide)
        p.setBrush(WHITE)
        for x in (104, 152):
            p.drawEllipse(QPointF(x, 200), 7, 7)

    p.end()
    return image


def app_icon() -> QIcon:
    """The window/taskbar icon. Needs a QGuiApplication."""
    icon = QIcon()
    for size in SIZES:
        icon.addPixmap(QPixmap.fromImage(render(size)))
    return icon


def _png(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def write_ico(path: str) -> None:
    """Multi-size .ico with PNG-compressed entries (supported since Windows Vista)."""
    images = [(size, _png(render(size))) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO directory
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    with open(path, "wb") as f:
        f.write(header + entries + blobs)


if __name__ == "__main__":
    write_ico(sys.argv[1])
