"""Search page: which tape is this file on?"""

from __future__ import annotations

import csv
import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QCompleter,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
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
from .ui_style import bulk_update, label, page_margins, setup_table

LIMIT = 2000
ALL = "全部"
HEADERS = ["文件", "大小", "名称", "磁带号", "备份时间", "存放方式"]
EMPTY_BEFORE = "输入关键字查找文件在哪盘磁带上"
EMPTY_NO_HITS = "没有找到匹配的文件，换个关键字试试"


class SizeItem(QTableWidgetItem):
    """Shows '1.23 MB' but sorts by the byte count."""

    def __init__(self, size: int) -> None:
        super().__init__(format_bytes(size))
        self.setData(Qt.UserRole, size)
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other: QTableWidgetItem) -> bool:  # type: ignore[override]
        return self.data(Qt.UserRole) < other.data(Qt.UserRole)


def placement(row: sqlite3.Row) -> str:
    return "直接存放" if row["kind"] == "raw" else "tar 包"


def filter_box(min_width: int) -> QComboBox:
    """A list to pick from that also takes typing: typed text pops up every entry that contains it."""
    box = QComboBox()
    box.setEditable(True)
    box.setInsertPolicy(QComboBox.NoInsert)  # typed text is a filter, not a new entry
    box.setMinimumWidth(min_width)
    box.lineEdit().setPlaceholderText(ALL)
    completer = box.completer()
    completer.setFilterMode(Qt.MatchContains)
    completer.setCaseSensitivity(Qt.CaseInsensitive)
    completer.setCompletionMode(QCompleter.PopupCompletion)
    return box


def box_filter(box: QComboBox) -> tuple[str | None, str | None]:
    """(whole name, part of a name) for what a filter box holds; (None, None) means no filter.

    Text that is exactly one of the listed names selects that name only; any other text matches
    every name that contains it ("li" finds LiHua and Lily).
    """
    text = box.currentText().strip()
    if text in ("", ALL):
        return None, None
    for i in range(1, box.count()):
        if box.itemText(i).lower() == text.lower():
            return box.itemText(i), None
    return None, text


class SearchPage(QWidget):
    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.cfg = cfg
        self.con: sqlite3.Connection | None = None
        self.db_path = ""
        self.rows: list[sqlite3.Row] = []  # current results, in query order

        self.keyword = QLineEdit()
        self.keyword.setPlaceholderText("输入文件名或路径中的关键字，多个词用空格隔开")
        self.owner_box = filter_box(160)
        self.owner_box.setToolTip("可以直接输入名称的一部分，例如 li")
        self.tape_box = filter_box(130)
        self.tape_box.setToolTip("可以直接输入磁带号的一部分")
        self.search_button = QPushButton("搜索")
        self.search_button.setProperty("primary", True)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self.keyword, 1)
        top.addSpacing(8)
        top.addWidget(label("名称", "hint"))
        top.addWidget(self.owner_box)
        top.addSpacing(4)
        top.addWidget(label("磁带", "hint"))
        top.addWidget(self.tape_box)
        top.addSpacing(8)
        top.addWidget(self.search_button)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        setup_table(self.table, multi_select=True)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, len(HEADERS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)

        self.table.horizontalHeaderItem(1).setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.empty_label = label(EMPTY_BEFORE, "hint")
        self.empty_label.setAlignment(Qt.AlignCenter)

        self.status = label("", "hint")
        self.export_button = QPushButton("导出 CSV…")
        self.restore_button = QPushButton("取回选中的文件…")
        self.restore_button.setEnabled(False)

        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.export_button)
        bottom.addWidget(self.restore_button)

        layout = QVBoxLayout(self)
        page_margins(layout)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.empty_label, 1)
        layout.addLayout(bottom)
        self._show_empty(EMPTY_BEFORE)

        self.search_button.clicked.connect(self.search)
        self.keyword.returnPressed.connect(self.search)
        self.owner_box.lineEdit().returnPressed.connect(self.search)
        self.tape_box.lineEdit().returnPressed.connect(self.search)
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
            index = box.findText(current)
            if index >= 0:
                box.setCurrentIndex(index)
            else:
                box.setEditText(current)  # keep what was typed, even if no name matches it exactly

    def search(self) -> None:
        try:
            con = self._connect()
            owner, owner_part = box_filter(self.owner_box)
            tape, tape_part = box_filter(self.tape_box)
            self.rows = db.search_files(
                con, self.keyword.text(), owner, tape, LIMIT, owner_part=owner_part, tape_part=tape_part
            )
        except Exception as e:
            QMessageBox.warning(self, "查询", f"查询失败：\n{e}")
            return
        self._fill()

    def _show_empty(self, text: str | None) -> None:
        """Show the grey message instead of the table while there is nothing to list."""
        self.empty_label.setVisible(text is not None)
        self.table.setVisible(text is None)
        if text is not None:
            self.empty_label.setText(text)

    def _fill(self) -> None:
        with bulk_update(self.table):
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
                if r["kind"] != "raw":
                    cells[5].setToolTip(f"打包在 {r['tar_name']}")
                for col, item in enumerate(cells):
                    self.table.setItem(i, col, item)
        self._show_empty(None if self.rows else EMPTY_NO_HITS)
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
                w.writerow(HEADERS + ["tar 文件", "sha256"])
                for r in self.rows:
                    w.writerow([r["rel_path"], r["size"], r["owner"], r["tape_id"], r["archived_at"],
                                placement(r), r["tar_name"] if r["kind"] != "raw" else "", r["sha256"] or ""])
        except OSError as e:
            QMessageBox.warning(self, "导出", f"无法写入文件：\n{e}")
            return False
        self.status.setText(f"已导出 {len(self.rows)} 条到 {path}")
        return True
