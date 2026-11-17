"""Search page: which tape is this file on?"""

from __future__ import annotations

import csv
import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import db
from .config import Config
from .copier import format_bytes

LIMIT = 2000
ALL = "全部"
HEADERS = ["文件", "大小", "名称", "磁带号", "备份时间", "存放方式"]


class SizeItem(QTableWidgetItem):
    """Shows '1.23 MB' but sorts by the byte count."""

    def __init__(self, size: int) -> None:
        super().__init__(format_bytes(size))
        self.setData(Qt.UserRole, size)
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other: QTableWidgetItem) -> bool:  # type: ignore[override]
        return self.data(Qt.UserRole) < other.data(Qt.UserRole)


def placement(row: sqlite3.Row) -> str:
    return "直接存放" if row["kind"] == "raw" else f"打包在 {row['tar_name']}"


class SearchPage(QWidget):
    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.con: sqlite3.Connection | None = None
        self.db_path = ""
        self.rows: list[sqlite3.Row] = []  # current results, in query order

        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("输入文件名或路径中的关键字，多个词用空格隔开")
        self.owner_box = QComboBox()
        self.owner_box.setMinimumWidth(140)
        self.tape_box = QComboBox()
        self.tape_box.setMinimumWidth(120)
        self.search_button = QPushButton("搜索")
        self.search_button.setProperty("primary", True)

        top = QHBoxLayout()
        top.addWidget(self.keyword, 1)
        top.addWidget(QLabel("名称"))
        top.addWidget(self.owner_box)
        top.addWidget(QLabel("磁带"))
        top.addWidget(self.tape_box)
        top.addWidget(self.search_button)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        for col, width in enumerate([420, 90, 120, 90, 150]):
            self.table.setColumnWidth(col, width)

        self.status = QLabel("")
        self.status.setProperty("hint", True)
        self.export_button = QPushButton("导出 CSV…")
        self.restore_button = QPushButton("取回选中的文件…")
        self.restore_button.setEnabled(False)

        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.export_button)
        bottom.addWidget(self.restore_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addLayout(bottom)

        self.search_button.clicked.connect(self.search)
        self.keyword.returnPressed.connect(self.search)
        self.export_button.clicked.connect(lambda: self.export_csv())
        self.restore_button.clicked.connect(self.restore)
        self.table.itemSelectionChanged.connect(self._selection_changed)

        self.refresh()

    # ------------------------------------------------------------ data

    def _connect(self) -> sqlite3.Connection:
        if self.con is None or self.db_path != self.cfg.db_path:
            if self.con is not None:
                self.con.close()
            self.con = db.connect(self.cfg.db_path)
            self.db_path = self.cfg.db_path
        return self.con

    def refresh(self) -> None:
        """Reload the filter lists, keeping the current choice when it still exists."""
        try:
            con = self._connect()
            owners = db.list_owners(con)
            tapes = [r["tape_id"] for r in db.list_tapes(con)]
        except Exception as e:
            self.status.setText(f"无法读取记录数据库：{e}")
            return
        for box, items in ((self.owner_box, owners), (self.tape_box, sorted(tapes))):
            current = box.currentText()
            box.clear()
            box.addItems([ALL] + items)
            box.setCurrentIndex(max(box.findText(current), 0))

    def search(self) -> None:
        try:
            con = self._connect()
            owner = self.owner_box.currentText()
            tape = self.tape_box.currentText()
            self.rows = db.search_files(
                con,
                self.keyword.text(),
                None if owner in (ALL, "") else owner,
                None if tape in (ALL, "") else tape,
                LIMIT,
            )
        except Exception as e:
            QMessageBox.warning(self, "查询", f"查询失败：\n{e}")
            return
        self._fill()

    def _fill(self) -> None:
        self.table.setSortingEnabled(False)  # keep row indexes stable while filling
        self.table.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            cells = [
                QTableWidgetItem(r["rel_path"]),
                SizeItem(r["size"]),
                QTableWidgetItem(r["owner"]),
                QTableWidgetItem(r["tape_id"] or ""),
                QTableWidgetItem(r["archived_at"] or ""),
                QTableWidgetItem(placement(r)),
            ]
            cells[0].setData(Qt.UserRole, i)  # index into self.rows, survives sorting
            cells[0].setToolTip(r["rel_path"])
            for col, item in enumerate(cells):
                self.table.setItem(i, col, item)
        self.table.setSortingEnabled(True)
        text = f"找到 {len(self.rows)} 个文件"
        if len(self.rows) >= LIMIT:
            text += f"。只显示前 {LIMIT} 个，请输入更具体的关键字缩小范围。"
        self.status.setText(text)
        self._selection_changed()

    # ------------------------------------------------------------ actions

    def _selection_changed(self) -> None:
        self.restore_button.setEnabled(bool(self.table.selectionModel().selectedRows()))

    def selected_rows(self) -> list[sqlite3.Row]:
        indexes = [self.table.item(i.row(), 0).data(Qt.UserRole) for i in self.table.selectionModel().selectedRows()]
        return [self.rows[i] for i in sorted(indexes)]

    def restore(self) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        from .ui_restore import RestoreDialog

        RestoreDialog(self.cfg, rows, self).exec()

    def export_csv(self, path: str | None = None) -> bool:
        if not self.rows:
            QMessageBox.information(self, "导出", "当前没有搜索结果可导出。")
            return False
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", "搜索结果.csv", "CSV 文件 (*.csv)")
            if not path:
                return False
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:  # BOM so Excel reads Chinese correctly
                w = csv.writer(f)
                w.writerow(HEADERS + ["sha256"])
                for r in self.rows:
                    w.writerow([r["rel_path"], r["size"], r["owner"], r["tape_id"], r["archived_at"],
                                placement(r), r["sha256"] or ""])
        except OSError as e:
            QMessageBox.warning(self, "导出", f"无法写入文件：\n{e}")
            return False
        self.status.setText(f"已导出 {len(self.rows)} 条到 {path}")
        return True
