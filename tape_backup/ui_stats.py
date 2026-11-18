"""Statistics page: how much tape space each name's data takes, all backups and all tapes together."""

from __future__ import annotations

import sqlite3

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPainterPath, QPaintEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QScrollArea,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import db
from .config import Config
from .copier import format_bytes
from .ui_records import NumItem, make_table
from .ui_style import BORDER, MUTED, NUMBER_FONT, PRIMARY, PRIMARY_SOFT, SURFACE, TEXT, UI_FONT, card, label, page_margins

EMPTY = "还没有备份记录"


def share_text(part: int, total: int) -> str:
    if total <= 0:
        return "—"
    share = part / total * 100
    return "<0.1%" if 0 < share < 0.1 else f"{share:.1f}%"


def tapes_text(row: sqlite3.Row) -> str:
    return ", ".join(sorted((row["tapes"] or "").split(","))) if row["tapes"] else ""


class UsageChart(QWidget):
    """Horizontal bars, one per name, largest first; the value is written at the end of each bar.

    Hovering a row highlights it and shows the details (share, tapes, number of backups).
    """

    ROW = 34       # height of one row; the whole row is the hover target
    BAR = 16       # bar thickness
    PAD = 10       # space above the first and below the last row
    NAME_MAX = 180  # longest name column before names are shortened with "…"
    VALUE_SPACE = 150  # room right of the longest bar for its label

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.rows: list[sqlite3.Row] = []
        self.total = 0
        self.hover = -1
        self.value_font = QFont(NUMBER_FONT)
        self.value_font.setPointSize(10)
        self.name_font = QFont(UI_FONT, 10)

    def set_rows(self, rows: list[sqlite3.Row]) -> None:
        self.rows = rows
        self.total = sum(r["bytes"] or 0 for r in rows)
        self.hover = -1
        self.setFixedHeight(self.sizeHint().height())
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return QSize(480, 2 * self.PAD + len(self.rows) * self.ROW)

    def _name_width(self) -> int:
        fm = QFontMetrics(self.name_font)
        widest = max((fm.horizontalAdvance(r["owner"]) for r in self.rows), default=0)
        return min(widest, self.NAME_MAX)

    def _row_at(self, y: float) -> int:
        i = int((y - self.PAD) // self.ROW)
        return i if 0 <= y - self.PAD and i < len(self.rows) else -1

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt name)
        if not self.rows:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        name_w = self._name_width()
        x0 = 12 + name_w + 12  # baseline of the bars
        span = max(self.width() - x0 - self.VALUE_SPACE, 40)
        largest = max(r["bytes"] or 0 for r in self.rows) or 1
        name_fm = QFontMetrics(self.name_font)

        for i, r in enumerate(self.rows):
            y = self.PAD + i * self.ROW
            if i == self.hover:
                p.fillRect(QRectF(0, y, self.width(), self.ROW), QColor(PRIMARY_SOFT))

            p.setFont(self.name_font)
            p.setPen(QColor(TEXT))
            name = name_fm.elidedText(r["owner"], Qt.ElideRight, name_w)
            p.drawText(QRectF(12, y, name_w, self.ROW), Qt.AlignRight | Qt.AlignVCenter, name)

            size = r["bytes"] or 0
            w = span * size / largest
            if size > 0:
                w = max(w, 3.0)  # a tiny share still shows as a sliver
                top = y + (self.ROW - self.BAR) / 2
                # Square at the baseline, 4 px rounded at the data end: a rounded bar that starts left of
                # the baseline, cut off at the baseline.
                p.save()
                p.setClipRect(QRectF(x0, top, w, self.BAR))
                path = QPainterPath()
                path.addRoundedRect(QRectF(x0 - 4, top, w + 4, self.BAR), 4, 4)
                p.fillPath(path, QColor(PRIMARY))
                p.restore()

            p.setFont(self.value_font)
            p.setPen(QColor(TEXT))
            value = format_bytes(size)
            p.drawText(QRectF(x0 + w + 8, y, self.VALUE_SPACE, self.ROW), Qt.AlignLeft | Qt.AlignVCenter, value)
            vw = QFontMetrics(self.value_font).horizontalAdvance(value)
            p.setFont(self.name_font)
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(x0 + w + 8 + vw + 8, y, self.VALUE_SPACE, self.ROW), Qt.AlignLeft | Qt.AlignVCenter,
                       share_text(size, self.total))

        p.setPen(QColor(BORDER))
        p.drawLine(x0, self.PAD, x0, self.PAD + len(self.rows) * self.ROW)
        p.end()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        i = self._row_at(event.position().y())
        if i != self.hover:
            self.hover = i
            self.update()
            if i < 0:
                QToolTip.hideText()
        if i >= 0:
            r = self.rows[i]
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"{r['owner']}\n{format_bytes(r['bytes'] or 0)}，占全部的 {share_text(r['bytes'] or 0, self.total)}\n"
                f"磁带：{tapes_text(r)}\n共 {r['backups']} 次备份",
                self,
            )

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt name)
        self.hover = -1
        self.update()


class StatsPage(QWidget):
    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.cfg = cfg
        self.con: sqlite3.Connection | None = None
        self.db_path = ""
        self.rows: list[sqlite3.Row] = []

        self.status = label("", "hint")
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(label("每人占用的磁带空间", "title"))
        top.addSpacing(4)
        top.addWidget(self.status)
        top.addStretch(1)

        self.chart = UsageChart()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"QScrollArea, QScrollArea > QWidget > QWidget {{ background: {SURFACE}; }}")
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addWidget(self.chart)
        holder_layout.addStretch(1)
        scroll.setWidget(holder)
        self.empty_label = label(EMPTY, "hint")
        self.empty_label.setAlignment(Qt.AlignCenter)

        chart_card, chart_layout = card(margins=12)
        chart_layout.addWidget(label("同一个名称在不同磁带、不同次备份的数据已合并；包括导入的旧记录。", "hint"))
        chart_layout.addWidget(scroll, 1)
        chart_layout.addWidget(self.empty_label, 1)

        self.table = make_table(["名称", "占用空间", "占比", "磁带"])
        self.table.setSortingEnabled(True)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setTextElideMode(Qt.ElideRight)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)  # the tape list takes what is left
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.setColumnWidth(0, 130)  # long names are shortened; the full name is in the tooltip
        for col in (1, 2):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
            self.table.horizontalHeaderItem(col).setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        side = QWidget()
        side.setFixedWidth(440)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.addWidget(self.table, 1)

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(chart_card, 1)
        body.addWidget(side)

        layout = QVBoxLayout(self)
        page_margins(layout)
        layout.addLayout(top)
        layout.addLayout(body, 1)
        self.scroll = scroll

        self.refresh()

    def _connect(self) -> sqlite3.Connection:
        if self.con is None or self.db_path != self.cfg.db_path:
            if self.con is not None:
                self.con.close()
            self.con = db.connect(self.cfg.db_path)
            self.db_path = self.cfg.db_path
        return self.con

    def refresh(self) -> None:
        try:
            con = self._connect()
            self.rows = db.owner_usage(con)
            tape_count = len(db.list_tapes(con))
        except Exception as e:
            self.status.setText(f"无法读取记录数据库：{e}")
            return
        total = sum(r["bytes"] or 0 for r in self.rows)

        self.chart.set_rows(self.rows)
        self.scroll.setVisible(bool(self.rows))
        self.empty_label.setVisible(not self.rows)

        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            size = r["bytes"] or 0
            tapes = tapes_text(r)
            share = size / total if total else 0
            cells = [
                QTableWidgetItem(r["owner"]),
                NumItem(format_bytes(size), size),
                NumItem(share_text(size, total), share),
                QTableWidgetItem(tapes),
            ]
            cells[0].setToolTip(r["owner"])
            cells[3].setToolTip(tapes)
            for col, item in enumerate(cells):
                t.setItem(i, col, item)
        t.setSortingEnabled(True)
        t.sortByColumn(1, Qt.DescendingOrder)

        self.status.setText(f"共 {format_bytes(total)}，{len(self.rows)} 个名称，{tape_count} 盘磁带" if self.rows else "")
