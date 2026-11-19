"""NAS usage: how much space each person's folder takes on the NAS, as a bar chart that can be saved as PNG.

The statistics tab shows this next to the tape usage (StatsTabs). Folders under 1 TB stay out of the
chart (the table lists them all). Measuring runs in a worker thread (NasScan); a timer polls it.
"""

from __future__ import annotations

import threading
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .config import Config
from .copier import format_bytes
from .nas_usage import FolderUsage, NasScan, normalize_root
from .ui_records import NumItem, make_table
from .ui_stats import StatsPage, UsageChart, chart_image, share_text
from .ui_style import SURFACE, bulk_update, card, label, page_margins, set_prop

TB = 1024 ** 4
CHART_MIN_BYTES = 1 * TB  # smaller folders are left out of the chart
TITLE = "NAS 各文件夹占用空间"


def files_detail(row) -> str:
    return f"{row['files']:,} 个文件"


def duration_text(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分" if h else f"{m} 分 {s} 秒" if m else f"{s} 秒"


class NasPage(QWidget):
    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.cfg = cfg
        self.scan: NasScan | None = None
        self.thread: threading.Thread | None = None
        self.shown = -1  # number of finished folders currently drawn
        self.root = ""   # location of the shown result
        self.measured_at = ""
        self.rows: list[dict] = []  # all finished folders, largest first

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("NAS 盘符或路径，例如 Z:")
        self.path_edit.setMaximumWidth(260)
        self.browse_button = QPushButton("浏览…")
        self.start_button = QPushButton("开始统计")
        set_prop(self.start_button, "primary", True)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.export_button = QPushButton("导出图片…")
        self.export_button.setEnabled(False)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(label(TITLE, "title"))
        top.addSpacing(12)
        top.addWidget(label("位置", "hint"))
        top.addWidget(self.path_edit)
        top.addWidget(self.browse_button)
        top.addWidget(self.start_button)
        top.addWidget(self.stop_button)
        top.addStretch(1)
        top.addWidget(self.export_button)

        self.status = label("输入 NAS 的盘符后点“开始统计”。只读取文件大小，不会改动 NAS 上的任何东西。", "hint")
        self.status.setWordWrap(True)

        self.chart = UsageChart(detail=files_detail)
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
        self.chart_hint = label("", "hint")
        chart_card, chart_layout = card(margins=12)
        chart_layout.addWidget(self.chart_hint)
        chart_layout.addWidget(scroll, 1)

        self.table = make_table(["名称", "大小", "文件数"])
        self.table.setSortingEnabled(True)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setTextElideMode(Qt.ElideRight)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
            self.table.horizontalHeaderItem(col).setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        side = QWidget()
        side.setFixedWidth(360)
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
        layout.addWidget(self.status)
        layout.addLayout(body, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll)
        self.browse_button.clicked.connect(self._browse)
        self.start_button.clicked.connect(self.start)
        self.path_edit.returnPressed.connect(self.start)
        self.stop_button.clicked.connect(self._stop)
        self.export_button.clicked.connect(lambda: self.export_png())
        self._show([])

    # ------------------------------------------------------------ running

    def is_running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择 NAS 位置", self.path_edit.text())
        if folder:
            self.path_edit.setText(folder.replace("/", "\\"))

    def start(self) -> None:
        if self.is_running():
            return
        root = normalize_root(self.path_edit.text())
        if not root:
            QMessageBox.information(self, TITLE, "请先输入 NAS 的盘符或路径，例如 Z:")
            return
        self.path_edit.setText(root)
        self.scan = NasScan(root, workers=max(2, min(self.cfg.threads, 16)))
        self.root, self.shown = root, -1
        self.measured_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._set_running(True)
        self.thread = threading.Thread(target=self._work, args=(self.scan,), daemon=True)
        self.thread.start()
        self.timer.start()

    def _work(self, scan: NasScan) -> None:
        scan.result = scan.run()  # read by _poll once the thread has ended

    def _stop(self) -> None:
        if self.scan:
            self.scan.stop()
        self.stop_button.setEnabled(False)
        self.status.setText("正在停止……")

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.path_edit.setReadOnly(running)
        self.browse_button.setEnabled(not running)
        self.export_button.setEnabled(not running and bool(self.rows))

    def _poll(self) -> None:
        scan = self.scan
        if scan is None:
            return
        snap = scan.snapshot()
        if len(snap["done"]) != self.shown:
            self.shown = len(snap["done"])
            self._show(snap["done"])
        if self.is_running():
            now = "，正在统计：" + "、".join(snap["running"][:4]) if snap["running"] else ""
            self.status.setText(
                f"已完成 {len(snap['done'])} / {snap['folders'] or '?'} 个文件夹，已扫描 {snap['files']:,} 个文件"
                f"（{format_bytes(snap['bytes'])}），用时 {duration_text(snap['seconds'])}{now}"
            )
            return
        self.timer.stop()
        self.thread = None
        result, message = scan.result or ("error", "统计意外中断")
        self._set_running(False)
        errors = sum(u.errors for u in snap["done"])
        if result == "error":
            self.status.setText(message)
            set_prop(self.status, "status", "fail")
            return
        set_prop(self.status, "status", "ok" if result == "done" else "warn")
        total = sum(r["bytes"] for r in self.rows)
        text = message or (
            f"{self.root}：{len(self.rows)} 个文件夹，共 {format_bytes(total)}，统计于 {self.measured_at}，"
            f"用时 {duration_text(snap['seconds'])}。"
        )
        if result == "stopped":
            text = f"已停止：只统计完 {len(self.rows)} / {snap['folders']} 个文件夹，下面只是这一部分。"
        if errors:
            text += f" 有 {errors:,} 个文件或文件夹无法读取，已跳过。"
        self.status.setText(text)

    # ------------------------------------------------------------ showing

    def _show(self, done: list[FolderUsage]) -> None:
        self.rows = sorted(({"owner": u.name, "bytes": u.bytes, "files": u.files} for u in done),
                           key=lambda r: (-r["bytes"], r["owner"].lower()))
        total = sum(r["bytes"] for r in self.rows)
        big = [r for r in self.rows if r["bytes"] >= CHART_MIN_BYTES]
        self.chart.set_rows(big, total)
        small = len(self.rows) - len(big)
        if not self.rows:
            self.chart_hint.setText("还没有统计结果")
        elif not big:
            self.chart_hint.setText(f"没有 1 TB 及以上的文件夹；{small} 个较小的文件夹见右侧表格。")
        else:
            self.chart_hint.setText(
                f"只画 1 TB 及以上的 {len(big)} 个文件夹" + (f"；另有 {small} 个小于 1 TB，见右侧表格。" if small else "。")
                + "占比按全部文件夹合计。"
            )

        t = self.table
        with bulk_update(t):
            t.setRowCount(len(self.rows))
            for i, r in enumerate(self.rows):
                name = QTableWidgetItem(r["owner"])
                name.setToolTip(f"{r['owner']}：占全部的 {share_text(r['bytes'], total)}")
                t.setItem(i, 0, name)
                t.setItem(i, 1, NumItem(format_bytes(r["bytes"]), r["bytes"]))
                t.setItem(i, 2, NumItem(f"{r['files']:,}", r["files"]))
        t.sortByColumn(1, Qt.DescendingOrder)

    def chart_rows(self) -> list[dict]:
        return [r for r in self.rows if r["bytes"] >= CHART_MIN_BYTES]

    def export_png(self, path: str | None = None) -> bool:
        big = self.chart_rows()
        if not big:
            QMessageBox.information(self, TITLE, "没有 1 TB 及以上的文件夹，没有可导出的图。")
            return False
        if path is None:
            name = f"NAS空间占用_{self.measured_at[:10]}.png"
            path, _ = QFileDialog.getSaveFileName(self, "导出图片", name, "PNG 图片 (*.png)")
            if not path:
                return False
        total = sum(r["bytes"] for r in self.rows)
        small = len(self.rows) - len(big)
        image = chart_image(
            big, total, TITLE,
            f"{self.root}    统计于 {self.measured_at}    全部 {len(self.rows)} 个文件夹，共 {format_bytes(total)}",
            f"只列出 1 TB 及以上的 {len(big)} 个文件夹" + (f"，另有 {small} 个小于 1 TB 未列出" if small else "")
            + "；百分比为占全部文件夹合计的比例。",
        )
        if not image.save(path, "PNG"):
            QMessageBox.warning(self, TITLE, f"无法保存图片：{path}")
            return False
        self.status.setText(f"已导出图片：{path}")
        return True


class StatsTabs(QWidget):
    """The statistics tab: tape usage (from the records) and NAS usage (measured on demand)."""

    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tape_page = StatsPage(cfg)
        self.nas_page = NasPage(cfg)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.tape_page, "磁带占用")
        self.tabs.addTab(self.nas_page, "NAS 占用")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)

    def refresh(self) -> None:
        self.tape_page.refresh()  # NAS usage is only measured when asked: it can take long
