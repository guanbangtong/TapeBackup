"""Restore tab: pick a name, tick folders in the tree of what was backed up, restore them from tape.

Only the selection lives here. The restore itself runs in RestoreDialog (destination, progress,
tape prompts), the same dialog the search and records pages use.
"""

from __future__ import annotations

import os
import sqlite3

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import db
from .config import Config
from .copier import format_bytes
from .ui_search import ALL, filter_box
from .ui_style import label, page_margins

HEADERS = ["文件夹", "大小", "文件数", "磁带"]
PICK_NAME = "先选择一个名称"
NOTE = "文件会先从磁带读到中转盘，再送到“目标文件夹\\备份的文件夹名\\原路径”下，同名文件会被覆盖。"


class Folder:
    """One folder of the backed-up data, with totals of everything below it."""

    __slots__ = ("name", "children", "ids", "size", "count", "tapes")

    def __init__(self, name: str) -> None:
        self.name = name
        self.children: dict[str, Folder] = {}
        self.ids: list[int] = []  # files directly in this folder
        self.size = 0             # these three: this folder only while building, then everything below
        self.count = 0
        self.tapes: set[str] = set()

    def all_ids(self) -> list[int]:
        out = list(self.ids)
        for child in self.children.values():
            out.extend(child.all_ids())
        return out

    def _roll_up(self) -> None:
        for child in self.children.values():
            child._roll_up()
            self.size += child.size
            self.count += child.count
            self.tapes |= child.tapes


def build_tree(rows: list[tuple]) -> list[Folder]:
    """Folder tree from db.owner_files rows: one top folder per backed-up source folder.

    Backups of the same folder are merged. Each file is counted in its own folder first and the
    totals are added up the tree once at the end, so 200,000 files take well under a second.
    """
    roots: dict[str, Folder] = {}
    folders: dict[tuple[str, str], Folder] = {}  # (source key, folder path) -> Folder
    for file_id, source, rel_path, size, tape_id in rows:
        key = os.path.normcase(os.path.normpath(source)).rstrip("\\/")
        path = rel_path.rpartition(os.sep)[0]
        folder = folders.get((key, path))
        if folder is None:
            node = roots.get(key) or roots.setdefault(key, Folder(source))
            for part in path.split(os.sep) if path else ():
                node = node.children.get(part) or node.children.setdefault(part, Folder(part))
            folder = folders[key, path] = node
        folder.ids.append(file_id)
        folder.size += size or 0
        folder.count += 1
        if tape_id:
            folder.tapes.add(tape_id)
    for root in roots.values():
        root._roll_up()
    return sorted(roots.values(), key=lambda r: r.name.lower())


def restore_rows(con: sqlite3.Connection, folders: list[Folder]) -> list[dict]:
    """Full rows of every file in the folders, each with restore_as = <source folder name>\\<rel_path>."""
    rows = []
    for row in db.files_by_id(con, [i for folder in folders for i in folder.all_ids()]):
        f = dict(row)
        top = os.path.basename(f["source_path"].rstrip("\\/")) or "data"
        f["restore_as"] = os.path.join(top, f["rel_path"])
        rows.append(f)
    return rows


class RestorePage(QWidget):
    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.cfg = cfg
        self.con: sqlite3.Connection | None = None
        self.db_path = ""
        self.owner = ""  # name whose tree is shown
        self.mark: tuple | None = None  # db.owner_files_mark when the tree was loaded

        self.owner_box = filter_box(200)
        self.owner_box.lineEdit().setPlaceholderText("选择或输入名称")
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(label("从磁带取回文件夹", "title"))
        top.addSpacing(12)
        top.addWidget(label("名称", "hint"))
        top.addWidget(self.owner_box)
        top.addStretch(1)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(HEADERS)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.Interactive)  # sized once after filling, not per row
        self.tree.headerItem().setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        self.tree.headerItem().setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)

        self.summary = label("", "hint")
        self.restore_button = QPushButton("取回勾选的文件夹…")
        self.restore_button.setProperty("primary", True)
        self.restore_button.setEnabled(False)
        bottom = QHBoxLayout()
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(self.restore_button)

        layout = QVBoxLayout(self)
        page_margins(layout)
        layout.addLayout(top)
        layout.addWidget(label("勾选要取回的文件夹，会包括它下面的所有文件。只列出有文件清单的备份，导入的旧记录不在这里。", "hint"))
        layout.addWidget(self.tree, 1)
        layout.addLayout(bottom)

        # Ticking a parent ticks every child, and each change emits itemChanged; count once afterwards.
        self.summary_timer = QTimer(self)
        self.summary_timer.setSingleShot(True)
        self.summary_timer.setInterval(0)
        self.summary_timer.timeout.connect(self._update_summary)
        self.tree.itemChanged.connect(self.summary_timer.start)
        self.owner_box.currentTextChanged.connect(self._owner_changed)
        self.restore_button.clicked.connect(self.restore)

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
        """Reload the name list and the shown tree (a backup may have added files)."""
        try:
            owners = db.list_owners(self._connect())
        except Exception as e:
            self.summary.setText(f"无法读取记录数据库：{e}")
            return
        blocker = QSignalBlocker(self.owner_box)
        current = self.owner_box.currentText()
        self.owner_box.clear()
        self.owner_box.addItems(owners)
        self.owner_box.setEditText(current)
        blocker.unblock()
        self._owner_changed(current)

    def _owner_changed(self, text: str) -> None:
        """Load the tree once the box holds exactly one of the names; keep it (and the ticks) if nothing changed."""
        match = next((self.owner_box.itemText(i) for i in range(self.owner_box.count())
                      if self.owner_box.itemText(i).lower() == text.strip().lower()), "")
        try:
            mark = db.owner_files_mark(self._connect(), match) if match else None
        except Exception:
            mark = None
        if match == self.owner and mark == self.mark and self.tree.topLevelItemCount():
            return
        self.owner, self.mark = match, mark
        self._load(match)

    def _load(self, owner: str) -> None:
        self.tree.clear()
        if not owner or owner == ALL:
            self.summary.setText(PICK_NAME)
            self.restore_button.setEnabled(False)
            return
        try:
            roots = build_tree(db.owner_files(self._connect(), owner))
        except Exception as e:
            self.summary.setText(f"无法读取记录数据库：{e}")
            return
        blocker = QSignalBlocker(self.tree)
        self.tree.setUpdatesEnabled(False)
        try:
            for root in roots:
                item = self._add_item(None, root)
                item.setExpanded(True)
        finally:
            self.tree.setUpdatesEnabled(True)
            blocker.unblock()
        for col in (1, 2, 3):
            self.tree.resizeColumnToContents(col)
        if not roots:
            self.summary.setText(f"{owner} 没有可按文件夹取回的备份（只有旧记录，或还没有备份完成）。")
            self.restore_button.setEnabled(False)
        else:
            self._update_summary()

    def _add_item(self, parent: QTreeWidgetItem | None, folder: Folder) -> QTreeWidgetItem:
        item = QTreeWidgetItem([folder.name, format_bytes(folder.size), f"{folder.count:,}", ", ".join(sorted(folder.tapes))])
        item.setData(0, Qt.UserRole, folder)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
        item.setCheckState(0, Qt.Unchecked)
        item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        item.setTextAlignment(2, Qt.AlignRight | Qt.AlignVCenter)
        item.setToolTip(0, folder.name)
        if parent is None:
            self.tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        for name in sorted(folder.children, key=str.lower):
            self._add_item(item, folder.children[name])
        return item

    # ------------------------------------------------------------ selection

    def _checked(self) -> list[Folder]:
        """Folders ticked as a whole (a fully ticked folder stands for everything below it)."""
        out: list[Folder] = []
        pending = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while pending:
            item = pending.pop()
            state = item.checkState(0)
            if state == Qt.Checked:
                out.append(item.data(0, Qt.UserRole))
            elif state == Qt.PartiallyChecked:
                pending.extend(item.child(i) for i in range(item.childCount()))
        return out

    def selected_files(self) -> list[dict]:
        return restore_rows(self._connect(), self._checked())

    def _update_summary(self) -> None:
        folders = self._checked()
        if not self.owner:
            return
        if not folders:
            self.summary.setText("勾选要取回的文件夹")
            self.restore_button.setEnabled(False)
            return
        size = sum(f.size for f in folders)
        count = sum(f.count for f in folders)
        tapes = sorted(set().union(*(f.tapes for f in folders)))
        self.summary.setText(f"已选 {count:,} 个文件，共 {format_bytes(size)}，需要磁带：{', '.join(tapes) or '—'}")
        self.restore_button.setEnabled(count > 0)

    def restore(self) -> None:
        files = self.selected_files()
        if not files:
            return
        from .ui_restore import RestoreDialog

        RestoreDialog(self.cfg, files, self, note=NOTE).exec()
