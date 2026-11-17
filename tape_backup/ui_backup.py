"""Backup page: tape and disk space on top, the queue on the left, the live progress on the right.

The engine runs in a worker thread. Its callbacks only emit signals (which are queued
to the UI thread); the progress display is driven by a 500 ms timer that reads
engine.progress.snapshot(), so no widget is ever touched from the worker.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections import deque

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import db
from .config import Config
from .copier import format_bytes
from .engine import Engine, normalize_tape_id, read_tape_marker, valid_owner_name
from .source_cleanup import recycle_path
from .ui_style import STATUS_COLORS, card, label, page_margins, set_prop, setup_table, status_item

TITLE = "磁带备份"
BAR_RANGE = 1000
SPEED_WINDOW = 10.0  # seconds
STEPS = (
    ("scan", "扫描"), ("stage", "暂存"), ("tape", "写入磁带"), ("verify", "校验"), ("cleanup", "清理"),
    ("delete", "删除源数据"),
)
MAX_LOG_LINES = 5000
COL_NAME, COL_SOURCE, COL_SIZE, COL_STATUS, COL_DELETE = range(5)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h} 小时 {m:02d} 分" if h else f"{m} 分 {s:02d} 秒"


def _bar(width: int | None = None) -> QProgressBar:
    bar = QProgressBar()
    bar.setRange(0, BAR_RANGE)
    bar.setTextVisible(False)
    if width:
        bar.setFixedWidth(width)
    return bar


class TapePrompt(QDialog):
    """Asks the user to insert or confirm a tape. Closing the window counts as 'stop'."""

    def __init__(self, reason: str, suggested: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("需要处理磁带")
        self.setModal(True)
        self.setMinimumWidth(480)

        text = QLabel(reason)
        text.setWordWrap(True)
        self.edit = QLineEdit(suggested or "")
        self.edit.setPlaceholderText("磁带号，例如 MB2FEQ")
        self.edit.textChanged.connect(self._upper)
        self.warning = label("", "hint")

        ok = QPushButton("确定")
        ok.setProperty("primary", True)
        ok.setDefault(True)
        ok.clicked.connect(self._accept)
        stop = QPushButton("停止备份")
        stop.setProperty("danger", True)
        stop.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(stop)
        buttons.addWidget(ok)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(text)
        layout.addWidget(label("磁带号", "hint"))
        layout.addWidget(self.edit)
        layout.addWidget(self.warning)
        layout.addLayout(buttons)

    def _upper(self, text: str) -> None:
        if text != text.upper():
            pos = self.edit.cursorPosition()
            self.edit.setText(text.upper())
            self.edit.setCursorPosition(pos)

    def _accept(self) -> None:
        if normalize_tape_id(self.edit.text()):
            self.accept()
        else:
            self.warning.setText("磁带号只能包含字母、数字、横线和下划线。")

    def tape_id(self) -> str:
        return normalize_tape_id(self.edit.text())


class BackupPage(QWidget):
    jobs_changed = Signal()
    busy_changed = Signal(bool)
    title_progress = Signal(str)

    # worker thread -> UI thread
    _log_line = Signal(str)
    _job_update = Signal(int)
    _ask_tape = Signal(str, str)
    _finished = Signal(str, str)

    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setObjectName("page")
        self._engine: Engine | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._current_job: int | None = None
        self._jobs: dict[int, dict] = {}       # job id -> row, as last loaded into the queue table
        self._loading = False                  # the table is being filled: ignore its change signals
        self._samples: deque[tuple[float, int]] = deque()
        self._sample_job: int | None = None
        self._started_at = 0.0
        self._tape_answer: str | None = None
        self._tape_event = threading.Event()
        self._ticks = 0
        self._log_count = 0

        self._build_ui()

        self._log_line.connect(self._append_log)
        self._job_update.connect(lambda _id: self._on_job_update())
        self._ask_tape.connect(self._show_tape_prompt)
        self._finished.connect(self._on_finished)

        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

        self.refresh()

    # ------------------------------------------------------------ layout

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        page_margins(outer)
        outer.addWidget(self._build_media_card())
        middle = QHBoxLayout()
        middle.setSpacing(12)
        middle.addWidget(self._build_queue_card(), 5)
        middle.addWidget(self._build_progress_card(), 4)
        outer.addLayout(middle, 1)
        outer.addWidget(self._build_log_card())
        self._reset_progress()

    def _build_media_card(self) -> QWidget:
        """One strip: which tape is in the drive and how much room the tape and the staging disk have."""
        self.tape_edit = QLineEdit()
        self.tape_edit.setPlaceholderText("例如 MB2FEQ")
        self.tape_edit.setFixedWidth(150)
        self.tape_edit.textChanged.connect(self._upper_tape_id)
        self.read_button = QPushButton("从磁带读取")
        self.read_button.setProperty("flat", True)
        self.read_button.clicked.connect(self.read_tape)
        self.tape_bar, self.tape_label = _bar(110), label("—", "hint")
        self.staging_caption = label("", "title")
        self.staging_bar, self.staging_label = _bar(110), label("—", "hint")

        frame, layout = card(margins=14)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(label("磁带号", "title"))
        row.addWidget(self.tape_edit)
        row.addWidget(self.read_button)
        row.addSpacing(8)
        row.addWidget(self.tape_bar)
        row.addWidget(self.tape_label)
        row.addStretch(1)
        row.addWidget(self.staging_caption)
        row.addWidget(self.staging_bar)
        row.addWidget(self.staging_label)
        layout.addLayout(row)
        return frame

    def _build_queue_card(self) -> QWidget:
        self.add_button = QPushButton("添加文件夹…")
        self.add_button.clicked.connect(self.add_folder)
        self.remove_button = QPushButton("移除")
        self.remove_button.setProperty("flat", True)
        self.remove_button.clicked.connect(self.remove_selected)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["名称", "源文件夹", "大小", "状态", "备份后删除源数据"])
        setup_table(self.table)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_SOURCE, QHeaderView.Stretch)
        for col, width in ((COL_NAME, 120), (COL_SIZE, 86), (COL_STATUS, 150), (COL_DELETE, 132)):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)
        self.table.horizontalHeaderItem(COL_DELETE).setToolTip(
            "勾选后，这个文件夹备份完成并在磁带上核对无误，就把 NAS 上已备份的文件删除，并清空它的回收站。"
        )
        self.table.itemChanged.connect(self._item_changed)

        self.queue_hint = label("", "hint")
        self.queue_hint.setWordWrap(True)

        frame, layout = card()
        head = QHBoxLayout()
        head.addWidget(label("待备份", "title"))
        head.addStretch()
        head.addWidget(self.remove_button)
        head.addWidget(self.add_button)
        layout.addLayout(head)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.queue_hint)
        return frame

    def _build_progress_card(self) -> QWidget:
        # idle: says what to do next
        self.idle_title = label("", "title")
        self.idle_title.setAlignment(Qt.AlignCenter)
        self.idle_text = label("", "hint")
        self.idle_text.setAlignment(Qt.AlignCenter)
        self.idle_text.setWordWrap(True)
        idle = QWidget()
        idle_layout = QVBoxLayout(idle)
        idle_layout.addStretch(2)
        idle_layout.addWidget(self.idle_title)
        idle_layout.addWidget(self.idle_text)
        idle_layout.addStretch(3)

        # running: one big figure, the trail of steps, what is happening right now
        self.percent_label = label("0%", "hero")
        self.job_caption = label("", "title")
        self.overall_bar = _bar()
        self.step_labels: dict[str, QLabel] = {}
        self.step_arrows: dict[str, QLabel] = {}
        trail = QHBoxLayout()
        trail.setSpacing(6)
        for i, (key, text) in enumerate(STEPS):
            if i:
                arrow = label("›", "hint")
                self.step_arrows[key] = arrow
                trail.addWidget(arrow)
            step = QLabel(text)
            step.setProperty("step", "todo")
            self.step_labels[key] = step
            trail.addWidget(step)
        trail.addStretch()
        self.step_caption = QLabel("")
        for text in (self.job_caption, self.step_caption):  # long text wraps instead of widening the window
            text.setWordWrap(True)
        self.step_bar = _bar()
        self.file_label = label("", "hint")
        self.file_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.speed_value, self.elapsed_value, self.left_value = (label("—", "number") for _ in range(3))
        figures = QHBoxLayout()
        figures.setSpacing(8)
        for caption, value in (("速度", self.speed_value), ("已用", self.elapsed_value), ("预计还需", self.left_value)):
            figures.addWidget(label(caption, "hint"))
            figures.addWidget(value)
            figures.addSpacing(14)
        figures.addStretch()

        running = QWidget()
        run_layout = QVBoxLayout(running)
        run_layout.setContentsMargins(0, 0, 0, 0)
        run_layout.setSpacing(8)
        run_layout.addWidget(self.percent_label)
        run_layout.addWidget(self.job_caption)
        run_layout.addWidget(self.overall_bar)
        run_layout.addSpacing(10)
        run_layout.addLayout(trail)
        run_layout.addWidget(self.step_caption)
        run_layout.addWidget(self.step_bar)
        run_layout.addWidget(self.file_label)
        run_layout.addSpacing(6)
        run_layout.addLayout(figures)
        run_layout.addStretch()

        self.stack = QStackedWidget()
        self.stack.addWidget(idle)
        self.stack.addWidget(running)

        self.start_button = QPushButton("开始备份")
        self.start_button.setProperty("primary", True)
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("停止")
        self.stop_button.setProperty("danger", True)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addWidget(self.start_button, 1)
        buttons.addWidget(self.stop_button)

        frame, layout = card(margins=20)
        frame.setMinimumWidth(430)
        layout.addWidget(self.stack, 1)
        layout.addLayout(buttons)
        return frame

    def _build_log_card(self) -> QWidget:
        """Collapsed to one line (the latest message) until the user wants to read the whole log."""
        self.log_toggle = QPushButton("▸ 运行记录")
        self.log_toggle.setProperty("flat", True)
        self.log_toggle.setCheckable(True)
        self.log_toggle.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)  # stays at the left when the log is open
        self.log_toggle.toggled.connect(self._toggle_log)
        self.log_latest = label("", "hint")
        self.log_latest.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(MAX_LOG_LINES)
        self.log.setFixedHeight(190)
        self.log.setVisible(False)

        frame, layout = card(margins=10, spacing=6)
        head = QHBoxLayout()
        head.addWidget(self.log_toggle)
        head.addWidget(self.log_latest, 1)
        head.addStretch()
        layout.addLayout(head)
        layout.addWidget(self.log)
        return frame

    def _toggle_log(self, shown: bool) -> None:
        self.log.setVisible(shown)
        self.log_latest.setVisible(not shown)
        self._update_log_toggle()
        if shown:
            bar = self.log.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _update_log_toggle(self) -> None:
        arrow = "▾" if self.log_toggle.isChecked() else "▸"
        count = f"（{self._log_count} 条）" if self._log_count else ""
        self.log_toggle.setText(f"{arrow} 运行记录{count}")

    # ------------------------------------------------------------ public

    def is_running(self) -> bool:
        return self._running

    def refresh(self) -> None:
        self._load_queue()
        self._update_space()

    # ------------------------------------------------------------ queue

    def _load_queue(self) -> None:
        try:
            con = db.connect(self.cfg.db_path)
            try:
                jobs = [dict(row) for row in db.queue_jobs(con)]
            finally:
                con.close()
        except Exception as e:  # unreadable data folder: show an empty queue, the settings tab can fix it
            self._append_log(f"无法读取备份队列：{e}")
            jobs = []
        self._jobs = {job["id"]: job for job in jobs}
        selected = self._selected_job_id()

        self._loading = True
        self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            name = QTableWidgetItem(job["owner"])
            name.setData(Qt.UserRole, job["id"])
            source = QTableWidgetItem(job["source_path"])
            source.setToolTip(job["source_path"])
            size = QTableWidgetItem(format_bytes(job["total_bytes"]) if job["scanned"] else "—")
            size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            delete = QTableWidgetItem("删除" if job["delete_source"] else "")
            delete.setCheckState(Qt.Checked if job["delete_source"] else Qt.Unchecked)
            flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
            # Not changeable during a run, and a finished job only stays here because it is ticked.
            if not self._running and job["status"] != "DONE":
                flags |= Qt.ItemIsUserCheckable
            delete.setFlags(flags)
            target = recycle_path(job["source_path"], self.cfg.nas_recycle_dir)
            delete.setToolTip(
                f"备份完成并在磁带上核对后，删除 {job['source_path']} 里已备份的文件"
                + (f"，并清空 {target}" if target else "") + "。"
            )
            for col, item in ((COL_NAME, name), (COL_SOURCE, source), (COL_SIZE, size),
                              (COL_STATUS, status_item("wait", "")), (COL_DELETE, delete)):
                self.table.setItem(row, col, item)
            if job["id"] == selected:
                self.table.selectRow(row)
        self._loading = False
        self._update_status_column(None)
        self._update_idle_text()

    def _selected_job_id(self) -> int | None:
        row = self.table.currentRow()
        item = self.table.item(row, COL_NAME) if row >= 0 else None
        return item.data(Qt.UserRole) if item else None

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != COL_DELETE:
            return
        job_id = self.table.item(item.row(), COL_NAME).data(Qt.UserRole)
        checked = item.checkState() == Qt.Checked
        con = db.connect(self.cfg.db_path)
        try:
            db.set_delete_source(con, job_id, checked)
        finally:
            con.close()
        self._jobs[job_id]["delete_source"] = int(checked)
        self._loading = True
        item.setText("删除" if checked else "")
        self._loading = False
        self._update_idle_text()

    def _update_status_column(self, snap: dict | None) -> None:
        for row in range(self.table.rowCount()):
            job = self._jobs.get(self.table.item(row, COL_NAME).data(Qt.UserRole))
            if job is None:
                continue
            if self._running and snap is not None and job["id"] == snap["job_id"]:
                if snap["phase"] == "delete":
                    kind, text = "run", "等待删除源数据" if snap["phase_unit"] == "seconds" else "正在删除源数据"
                else:
                    kind, text = "run", f"进行中 {self._job_percent(snap):.0f}%"
            elif job["status"] == "DONE":
                kind, text = "warn", "已备份，待删除源数据"
            elif job["status"] == "RUNNING":
                kind, text = "warn", "未完成，可继续"
            else:
                kind, text = "wait", "等待"
            item = self.table.item(row, COL_STATUS)
            if item.text() != f"●  {text}":
                self._loading = True
                item.setText(f"●  {text}")
                item.setForeground(QColor(STATUS_COLORS[kind]))
                self._loading = False

    def _update_idle_text(self) -> None:
        jobs = list(self._jobs.values())
        deleting = sum(1 for job in jobs if job["delete_source"])
        if not jobs:
            title, text = "还没有要备份的文件夹", "点左边的“添加文件夹…”，选择 NAS 上要备份的文件夹。"
        elif any(job["status"] in ("RUNNING", "DONE") for job in jobs):
            title, text = "有未完成的任务", "点“开始备份”，会从上次中断的地方继续。"
        else:
            title = f"{len(jobs)} 个文件夹等待备份"
            text = "确认磁带号，然后点“开始备份”。过程中可以随时停止，下次会接着做。"
        self.idle_title.setText(title)
        self.idle_text.setText(text)
        self.queue_hint.setText(
            f"{deleting} 个文件夹备份后会删除 NAS 源数据：只删已在磁带上的文件，删除前等待 "
            f"{self.cfg.delete_delay_minutes:g} 分钟并在磁带上重新核对。" if deleting else ""
        )
        self.queue_hint.setVisible(bool(deleting))

    @staticmethod
    def _job_percent(snap: dict) -> float:
        return 100.0 * snap["job_done"] / snap["job_total"] if snap["job_total"] else 0.0

    def add_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择要备份的文件夹")
        if not folder:
            return
        folder = os.path.normpath(folder)
        name = os.path.basename(folder)
        if not name:  # a drive root such as E:\
            name, ok = QInputDialog.getText(self, TITLE, "这个位置没有文件夹名，请输入一个名称（例如姓名）：")
            name = name.strip()
            if not ok or not name:
                return
        con = db.connect(self.cfg.db_path)
        try:
            while True:
                if not valid_owner_name(name):
                    QMessageBox.warning(self, TITLE, f"“{name}”不能用作名称（不能含 \\ / : * ? \" < > | ，不能以点或空格结尾）。")
                    return
                history = db.owner_history(con, name)
                if history["active"]:
                    QMessageBox.warning(self, TITLE, f"“{name}”已在队列中，不能重复添加。")
                    return
                # Same name, different folder: mixing two data sets under one name would skip or overwrite files.
                known = {os.path.normcase(os.path.normpath(s)) for s in history["sources"]}
                if not known or os.path.normcase(folder) in known:
                    break
                box = QMessageBox(self)
                box.setWindowTitle(TITLE)
                box.setIcon(QMessageBox.Warning)
                box.setText(
                    f"名称“{name}”以前备份的是另一个位置：\n{history['sources'][0]}\n\n"
                    f"现在选的是：\n{folder}\n\n这两个是同一份数据吗？"
                )
                same = box.addButton("是同一份数据（只是位置变了）", QMessageBox.AcceptRole)
                rename = box.addButton("不是，换一个名称", QMessageBox.ActionRole)
                box.addButton("取消", QMessageBox.RejectRole)
                box.exec()
                if box.clickedButton() is same:
                    break
                if box.clickedButton() is not rename:
                    return
                name, ok = QInputDialog.getText(self, TITLE, "请输入一个新的名称（例如加上姓名或日期）：", text=name)
                name = name.strip()
                if not ok or not name:
                    return
            if history["archived_files"] > 0:
                QMessageBox.information(
                    self, TITLE,
                    f"“{name}”以前备份过（已有 {history['archived_files']} 个文件在磁带上）。\n"
                    "这次只会备份新增或有变化的文件。",
                )
            elif history["legacy_only"]:
                answer = QMessageBox.question(
                    self, TITLE,
                    f"“{name}”只有一条旧的备份记录，没有文件清单，无法判断哪些文件已经备份过。\n"
                    "继续的话，这个文件夹里的所有文件都会重新备份一遍。\n\n是否继续添加？",
                )
                if answer != QMessageBox.Yes:
                    return
            db.add_job(con, name, folder, delete_source=self.cfg.delete_source_default)
        finally:
            con.close()
        self.refresh()
        self.jobs_changed.emit()

    def remove_selected(self) -> None:
        job = self._jobs.get(self._selected_job_id())
        if job is None:
            QMessageBox.information(self, TITLE, "请先在列表里选中要移除的一行。")
            return
        if job["status"] == "DONE":  # backed up; only the deletion of its source is still pending
            answer = QMessageBox.question(
                self, TITLE,
                f"“{job['owner']}”已经备份完成，只是还没删除 NAS 上的源数据。\n\n移除后不再删除，数据留在 NAS 上。确定吗？",
            )
            if answer != QMessageBox.Yes:
                return
            con = db.connect(self.cfg.db_path)
            try:
                db.set_delete_source(con, job["id"], False)
            finally:
                con.close()
        elif job["status"] == "QUEUED" and not job["scanned"]:
            con = db.connect(self.cfg.db_path)
            try:
                db.remove_queued_job(con, job["id"])
            finally:
                con.close()
        else:
            answer = QMessageBox.question(
                self, TITLE,
                f"“{job['owner']}”已经开始备份。放弃它会删除中转盘上还没写入磁带的副本，不能恢复。\n"
                "已经写入磁带的文件仍会保留在记录里，以后再次备份时会自动跳过。\n\n确定放弃吗？",
            )
            if answer != QMessageBox.Yes:
                return
            try:
                Engine(self.cfg).abandon_job(job["id"])
            except Exception as e:
                QMessageBox.critical(self, TITLE, f"放弃任务失败：{e}")
        self.refresh()
        self.jobs_changed.emit()

    # ------------------------------------------------------------ tape and free space

    def _upper_tape_id(self, text: str) -> None:
        if text != text.upper():
            pos = self.tape_edit.cursorPosition()
            self.tape_edit.setText(text.upper())
            self.tape_edit.setCursorPosition(pos)

    def read_tape(self) -> None:
        if not os.path.isdir(self.cfg.tape_root):
            QMessageBox.warning(self, TITLE, f"未检测到磁带（{self.cfg.tape_root}）。请插入磁带后再试。")
            return
        try:
            marker = read_tape_marker(self.cfg.tape_root)
        except OSError as e:
            QMessageBox.warning(self, TITLE, f"无法读取磁带：{e}")
            return
        if marker:
            self.tape_edit.setText(marker)
        else:
            QMessageBox.information(self, TITLE, "这盘磁带还没有磁带号（没有用本软件写过）。请手动输入一个磁带号。")

    def _update_space(self) -> None:
        # A long staging path must not widen the window: show it shortened, whole in the tooltip.
        fm = QFontMetrics(self.staging_caption.font())
        self.staging_caption.setText("中转盘 " + fm.elidedText(self.cfg.staging_root, Qt.ElideMiddle, 130))
        self.staging_caption.setToolTip(self.cfg.staging_root)
        self._show_space(self.staging_bar, self.staging_label, self.cfg.staging_root, "无法读取")
        self._show_space(self.tape_bar, self.tape_label, self.cfg.tape_root, "未检测到磁带")

    @staticmethod
    def _show_space(bar: QProgressBar, text: QLabel, path: str, missing: str) -> None:
        """The bar fills as the disk fills, and turns amber when less than a tenth is left."""
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            bar.setValue(0)
            text.setText(missing)
            text.setToolTip("")
            return
        used = 1 - usage.free / usage.total if usage.total else 0
        bar.setValue(int(BAR_RANGE * used))
        set_prop(bar, "tone", "warn" if used > 0.9 else "")
        text.setText(f"剩余 {format_bytes(usage.free)}")
        text.setToolTip(f"剩余 {format_bytes(usage.free)}，总容量 {format_bytes(usage.total)}")

    # ------------------------------------------------------------ start / stop

    def start(self) -> None:
        if self._running:
            return
        if not self._jobs:
            QMessageBox.information(self, TITLE, "还没有要备份的文件夹，请先添加。")
            return
        tape_id = normalize_tape_id(self.tape_edit.text())
        if not tape_id:
            QMessageBox.warning(self, TITLE, "请输入磁带号（字母、数字、横线、下划线），或点“从磁带读取”。")
            return
        self.tape_edit.setText(tape_id)
        if not self._confirm_deletions():
            return

        self._engine = Engine(
            self.cfg,
            on_log=self._log_line.emit,
            ask_tape=self._ask_tape_blocking,
            on_job_update=self._job_update.emit,
        )
        self._running = True
        self._current_job = None
        self._samples.clear()
        self._started_at = time.monotonic()
        self._set_busy_ui(True)
        self.busy_changed.emit(True)
        self._thread = threading.Thread(target=self._worker, args=(self._engine, tape_id), daemon=True)
        self._thread.start()

    def _confirm_deletions(self) -> bool:
        """Deleting from the NAS cannot be undone: show exactly what will go and ask once."""
        targets = [job for job in self._jobs.values() if job["delete_source"]]
        if not targets:
            return True
        lines = []
        for job in targets:
            lines.append(f"• {job['source_path']}")
            recycle = recycle_path(job["source_path"], self.cfg.nas_recycle_dir)
            if recycle:
                lines.append(f"   回收站 {recycle}（整个清空）")
        box = QMessageBox(self)
        box.setWindowTitle(TITLE)
        box.setIcon(QMessageBox.Warning)
        box.setText(
            "以下文件夹备份完成后，会删除 NAS 上的源数据：\n\n" + "\n".join(lines) + "\n\n"
            "只删除已经在磁带上、且备份后没有改动过的文件；其余文件会留在 NAS 上。\n"
            f"删除前会等待 {self.cfg.delete_delay_minutes:g} 分钟，并在磁带上重新核对。\n\n"
            "删除后无法恢复。确定开始吗？"
        )
        go = box.addButton("开始备份", QMessageBox.AcceptRole)
        box.setDefaultButton(box.addButton("取消", QMessageBox.RejectRole))
        box.exec()
        return box.clickedButton() is go

    def stop(self) -> None:
        if not self._running or self._engine is None:
            return
        self._engine.stop()
        self.stop_button.setEnabled(False)
        self.stop_button.setText("正在停止…")

    def _worker(self, engine: Engine, tape_id: str) -> None:
        try:
            result = engine.run(tape_id)
        except Exception as e:  # engine.run already catches everything; this is a last resort
            result = ("error", f"意外错误：{e!r}")
        self._finished.emit(*result)

    def _set_busy_ui(self, busy: bool) -> None:
        self.add_button.setEnabled(not busy)
        self.remove_button.setEnabled(not busy)
        self.start_button.setEnabled(not busy)
        self.read_button.setEnabled(not busy)
        self.tape_edit.setReadOnly(busy)
        self.stop_button.setEnabled(busy)
        self.stop_button.setText("停止")
        self.stack.setCurrentIndex(1 if busy else 0)
        self._load_queue()  # the delete checkboxes are locked while running

    # ------------------------------------------------------------ worker callbacks

    def _ask_tape_blocking(self, reason: str, suggested: str | None) -> str | None:
        """Called on the worker thread: wait until the UI thread has shown the dialog and stored the answer."""
        self._tape_event.clear()
        self._tape_answer = None
        self._ask_tape.emit(reason, suggested or "")
        self._tape_event.wait()
        return self._tape_answer

    def _show_tape_prompt(self, reason: str, suggested: str) -> None:
        QApplication.beep()
        dialog = TapePrompt(reason, suggested, self)
        answer = dialog.tape_id() if dialog.exec() == QDialog.Accepted else None
        if answer:
            self.tape_edit.setText(answer)
        self._tape_answer = answer
        self._tape_event.set()

    def _on_job_update(self) -> None:
        self._load_queue()
        self.jobs_changed.emit()

    def _on_finished(self, result: str, message: str) -> None:
        self._running = False
        self._engine = None
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self._reset_progress()
        self._set_busy_ui(False)
        self._update_space()
        self.jobs_changed.emit()
        self.busy_changed.emit(False)
        self.title_progress.emit("")
        if result == "done":
            QApplication.beep()
            QMessageBox.information(self, TITLE, message)
        elif result == "error":
            QApplication.beep()
            self.log_toggle.setChecked(True)  # the details are in the log
            QMessageBox.critical(self, TITLE, message)

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(text)
        self._log_count += text.count("\n") + 1
        self._update_log_toggle()
        last = text.strip().splitlines()[-1] if text.strip() else ""
        fm = QFontMetrics(self.log_latest.font())
        self.log_latest.setText(fm.elidedText(last, Qt.ElideRight, max(self.log_latest.width() - 8, 80)))
        self.log_latest.setToolTip(last)
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ------------------------------------------------------------ progress display

    def _tick(self) -> None:
        self._ticks += 1
        if self._ticks % 6 == 0:  # every 3 seconds; no need to query the tape drive more often
            self._update_space()
        if not self._running or self._engine is None:
            return
        snap = self._engine.progress.snapshot()
        self._current_job = snap["job_id"]
        self._update_steps(snap)
        self._update_bars(snap)
        self._update_live(snap)
        self._update_status_column(snap)
        self.title_progress.emit(f"{self._job_percent(snap):.0f}%")

    def _update_steps(self, snap: dict | None) -> None:
        phase = snap["phase"] if snap else "idle"
        job = self._jobs.get(snap["job_id"]) if snap else None
        deletes = bool(job and job["delete_source"]) or phase == "delete"
        self.step_labels["delete"].setVisible(deletes)
        self.step_arrows["delete"].setVisible(deletes)
        keys = [key for key, _ in STEPS]
        active = keys.index(phase) if phase in keys else -1
        for i, (key, text) in enumerate(STEPS):
            state = "done" if 0 <= i < active else "active" if i == active else "todo"
            step = self.step_labels[key]
            step.setText(f"✓ {text}" if state == "done" else text)
            set_prop(step, "step", state)

    def _update_bars(self, snap: dict) -> None:
        phase, unit = snap["phase"], snap["phase_unit"]
        total, done = snap["phase_total"], snap["phase_done"]
        pct = 100.0 if phase == "delete" else self._job_percent(snap)
        self.percent_label.setText(f"{pct:.0f}%")
        caption = snap["owner"] or ""
        if snap["batch_seq"] and phase != "delete":
            caption += f"，第 {snap['batch_seq']} 批（预计共 {max(snap['batch_estimate'], snap['batch_seq'])} 批）"
        self.job_caption.setText(caption)
        self.overall_bar.setValue(int(BAR_RANGE * pct / 100))

        if phase == "scan":
            self.step_bar.setRange(0, 0)  # indeterminate: the total is unknown while scanning
            self.step_caption.setText(f"已扫描 {snap['scan_files']:,} 个文件，{format_bytes(snap['scan_bytes'])}")
            return
        if self.step_bar.maximum() == 0:
            self.step_bar.setRange(0, BAR_RANGE)
        self.step_bar.setValue(int(BAR_RANGE * done / total) if total else 0)
        name = dict(STEPS).get(phase, "")
        if phase == "delete" and unit == "seconds":
            self.step_caption.setText(
                f"等待磁带机把目录写入磁带，{format_duration(total - done)}后删除 NAS 源数据"
            )
        elif phase == "delete":
            self.step_caption.setText(f"删除 NAS 源数据  {done:,} / {total:,} 个文件" if total else "核对 NAS 上的文件 …")
        elif phase == "cleanup" or total == 0:
            self.step_caption.setText(name)
        elif unit == "bytes":
            self.step_caption.setText(f"{name}  {format_bytes(done)} / {format_bytes(total)}")
        else:
            self.step_caption.setText(f"{name}  {done:,} / {total:,} 个文件")

    def _update_live(self, snap: dict) -> None:
        now = time.monotonic()
        # Samples from a different job are not comparable (job_done restarts), so start over.
        if self._samples and self._sample_job != snap["job_id"]:
            self._samples.clear()
        self._sample_job = snap["job_id"]
        self._samples.append((now, snap["job_done"]))
        while len(self._samples) > 2 and now - self._samples[0][0] > SPEED_WINDOW:
            self._samples.popleft()

        speed = 0.0
        t0, d0 = self._samples[0]
        if now - t0 >= 1.0:
            speed = (snap["job_done"] - d0) / (now - t0)
        copying = snap["phase"] in ("stage", "tape") and speed > 0
        self.speed_value.setText(f"{speed / 1024 ** 2:.0f} MB/s" if copying else "—")
        self.left_value.setText(
            format_duration((snap["job_total"] - snap["job_done"]) / speed) if copying and snap["job_total"] else "—"
        )
        self.elapsed_value.setText(format_duration(now - self._started_at))

        current = snap["current_file"]
        fm = QFontMetrics(self.file_label.font())
        self.file_label.setText(fm.elidedText(current, Qt.ElideMiddle, max(self.file_label.width() - 8, 50)))
        self.file_label.setToolTip(current)

    def _reset_progress(self) -> None:
        self._samples.clear()
        self._update_steps(None)
        self.percent_label.setText("0%")
        self.job_caption.setText("")
        self.overall_bar.setValue(0)
        self.step_caption.setText("")
        self.step_bar.setRange(0, BAR_RANGE)
        self.step_bar.setValue(0)
        self.file_label.setText("")
        for value in (self.speed_value, self.elapsed_value, self.left_value):
            value.setText("—")
