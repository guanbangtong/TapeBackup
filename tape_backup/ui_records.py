"""Records page: what has been backed up, when, to which tape."""

from __future__ import annotations

import csv
import sqlite3

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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

HEADERS = ["日期", "名称", "磁带号", "大小", "文件数", "批次", "状态", "源文件夹"]
LEGACY_COLUMNS = {"date", "tape_id", "owner", "size_tb"}


class NumItem(QTableWidgetItem):
    """Shows text but sorts by a number."""

    def __init__(self, text: str, number: float) -> None:
        super().__init__(text)
        self.setData(Qt.UserRole, number)
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other: QTableWidgetItem) -> bool:  # type: ignore[override]
        return self.data(Qt.UserRole) < other.data(Qt.UserRole)


def status_text(job: sqlite3.Row) -> str:
    if job["legacy"]:
        return "旧记录"
    s = job["status"]
    if s == "DONE":
        return f"完成（{job['failed_files']} 个文件失败）" if job["failed_files"] > 0 else "完成"
    return {"RUNNING": "未完成", "QUEUED": "等待中", "CANCELLED": "已放弃"}.get(s, s)


def make_table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(28)
    t.setAlternatingRowColors(True)
    t.setWordWrap(False)
    t.horizontalHeader().setStretchLastSection(True)
    return t


class RecordsPage(QWidget):
    records_changed = Signal()

    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.con: sqlite3.Connection | None = None
        self.db_path = ""
        self.jobs: list[sqlite3.Row] = []

        self.table = make_table(HEADERS)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSortingEnabled(True)
        for col, width in enumerate([150, 120, 100, 90, 80, 60, 150]):
            self.table.setColumnWidth(col, width)

        self.tape_table = make_table(["磁带号", "已写入", "最近使用"])
        self.tape_table.setSortingEnabled(True)
        self.tape_table.setMaximumHeight(170)
        self.tape_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        self.refresh_button = QPushButton("刷新")
        self.import_button = QPushButton("导入旧记录…")
        self.export_button = QPushButton("导出 CSV…")
        self.failed_button = QPushButton("查看失败的文件")
        self.restore_button = QPushButton("取回整个备份…")
        self.failed_button.setEnabled(False)
        self.restore_button.setEnabled(False)

        buttons = QHBoxLayout()
        for b in (self.refresh_button, self.import_button, self.export_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(self.failed_button)
        buttons.addWidget(self.restore_button)

        self.status = QLabel("")
        self.status.setProperty("hint", True)
        tape_title = QLabel("磁带一览")

        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.status)
        layout.addWidget(tape_title)
        layout.addWidget(self.tape_table)

        self.refresh_button.clicked.connect(self.refresh)
        self.import_button.clicked.connect(lambda: self.import_legacy())
        self.export_button.clicked.connect(lambda: self.export_csv())
        self.failed_button.clicked.connect(self.show_failed)
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
        selected = self.selected_job()
        selected_id = selected["id"] if selected else None
        try:
            con = self._connect()
            self.jobs = db.list_jobs(con)
            tapes = db.list_tapes(con)
        except Exception as e:
            self.status.setText(f"无法读取记录数据库：{e}")
            return

        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(self.jobs))
        for i, j in enumerate(self.jobs):
            stamp = j["finished_at"] or j["created_at"]
            files = "—" if j["legacy"] else str(j["done_files"])
            cells = [
                QTableWidgetItem(stamp),
                QTableWidgetItem(j["owner"]),
                QTableWidgetItem(j["tapes"] or ""),
                NumItem(format_bytes(j["done_bytes"]), j["done_bytes"]),
                NumItem(files, -1 if j["legacy"] else j["done_files"]),
                NumItem(str(j["batch_count"]), j["batch_count"]),
                QTableWidgetItem(status_text(j)),
                QTableWidgetItem(j["source_path"]),
            ]
            cells[0].setData(Qt.UserRole, i)  # index into self.jobs, survives sorting
            cells[7].setToolTip(j["source_path"])
            for col, item in enumerate(cells):
                t.setItem(i, col, item)
        t.setSortingEnabled(True)
        t.sortByColumn(0, Qt.DescendingOrder)
        if selected_id is not None:
            for r in range(t.rowCount()):
                if self.jobs[t.item(r, 0).data(Qt.UserRole)]["id"] == selected_id:
                    t.selectRow(r)
                    break

        tt = self.tape_table
        tt.setSortingEnabled(False)
        tt.setRowCount(len(tapes))
        for i, r in enumerate(tapes):
            tt.setItem(i, 0, QTableWidgetItem(r["tape_id"]))
            tt.setItem(i, 1, NumItem(format_bytes(r["bytes"] or 0), r["bytes"] or 0))
            tt.setItem(i, 2, QTableWidgetItem(r["last_used"] or ""))
        tt.setSortingEnabled(True)

        self.status.setText(f"共 {len(self.jobs)} 条备份记录，{len(tapes)} 盘磁带")
        self._selection_changed()

    def selected_job(self) -> sqlite3.Row | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.jobs[self.table.item(rows[0].row(), 0).data(Qt.UserRole)]

    def _selection_changed(self) -> None:
        job = self.selected_job()
        self.failed_button.setEnabled(bool(job and job["failed_files"] > 0))
        self.restore_button.setEnabled(bool(job and not job["legacy"] and job["done_files"] > 0))

    # ------------------------------------------------------------ actions

    def import_legacy(self, path: str | None = None) -> bool:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "选择旧的 archive_index.csv", "", "CSV 文件 (*.csv)")
            if not path:
                return False
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                header = set(next(csv.reader(f), []))
            if not LEGACY_COLUMNS <= {h.strip() for h in header}:
                raise ValueError("第一行应包含 date, tape_id, owner, size_tb 这几列")
            imported, skipped = db.import_legacy_csv(self._connect(), path)
        except (OSError, UnicodeDecodeError, csv.Error, ValueError) as e:
            QMessageBox.warning(
                self, "导入旧记录", f"这个文件的格式不对，无法导入。\n请选择旧版程序生成的 archive_index.csv。\n\n{e}"
            )
            return False
        QMessageBox.information(self, "导入旧记录", f"导入 {imported} 条，跳过 {skipped} 条重复")
        self.refresh()
        self.records_changed.emit()
        return True

    def export_csv(self, path: str | None = None) -> bool:
        if not self.jobs:
            QMessageBox.information(self, "导出", "当前没有记录可导出。")
            return False
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", "备份记录.csv", "CSV 文件 (*.csv)")
            if not path:
                return False
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:  # BOM so Excel reads Chinese correctly
                w = csv.writer(f)
                w.writerow(HEADERS)
                for j in self.jobs:
                    w.writerow([
                        j["finished_at"] or j["created_at"], j["owner"], j["tapes"] or "", j["done_bytes"],
                        "" if j["legacy"] else j["done_files"], j["batch_count"], status_text(j), j["source_path"],
                    ])
        except OSError as e:
            QMessageBox.warning(self, "导出", f"无法写入文件：\n{e}")
            return False
        self.status.setText(f"已导出 {len(self.jobs)} 条到 {path}")
        return True

    def failed_dialog(self, job: sqlite3.Row) -> QDialog:
        rows = db.failed_files(self._connect(), job["id"])
        dlg = QDialog(self)
        dlg.setWindowTitle(f"失败的文件 - {job['owner']}")
        dlg.resize(820, 420)
        table = make_table(["文件", "大小", "原因"])
        table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            table.setItem(i, 0, QTableWidgetItem(r["rel_path"]))
            table.setItem(i, 1, NumItem(format_bytes(r["size"]), r["size"]))
            table.setItem(i, 2, QTableWidgetItem(r["error"] or ""))
        table.setColumnWidth(0, 400)
        table.setColumnWidth(1, 90)
        close = QPushButton("关闭")
        close.clicked.connect(dlg.accept)
        note = QLabel(f"共 {len(rows)} 个文件没有备份成功（读取失败）。这些文件不在磁带上。")
        note.setProperty("hint", True)
        layout = QVBoxLayout(dlg)
        layout.addWidget(note)
        layout.addWidget(table, 1)
        layout.addWidget(close, 0, Qt.AlignRight)
        return dlg

    def show_failed(self) -> None:
        job = self.selected_job()
        if job:
            self.failed_dialog(job).exec()

    def restore(self) -> None:
        job = self.selected_job()
        if not job:
            return
        from .ui_restore import RestoreDialog

        rows = db.files_of_job(self._connect(), job["id"])
        RestoreDialog(self.cfg, rows, self).exec()
