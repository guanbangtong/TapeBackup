"""Backup page: queue, tape and disk space, start/stop, live progress and log.

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
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import db
from .config import Config
from .copier import format_bytes
from .engine import Engine, normalize_tape_id, read_tape_marker, valid_owner_name

TITLE = "磁带备份"
BAR_RANGE = 1000
SPEED_WINDOW = 10.0  # seconds
STEPS = (("scan", "扫描"), ("stage", "暂存到中转盘"), ("tape", "写入磁带"), ("verify", "校验"), ("cleanup", "清理"))
MAX_LOG_LINES = 5000


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}小时{m:02d}分" if h else f"{m}分{s:02d}秒"


class TapePrompt(QDialog):
    """Asks the user to insert or confirm a tape. Closing the window counts as 'stop'."""

    def __init__(self, reason: str, suggested: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("需要处理磁带")
        self.setModal(True)
        self.setMinimumWidth(460)

        text = QLabel(reason)
        text.setWordWrap(True)
        self.edit = QLineEdit(suggested or "")
        self.edit.setPlaceholderText("磁带号，例如 MB2FEQ")
        self.edit.textChanged.connect(self._upper)
        self.warning = QLabel("")
        self.warning.setProperty("hint", True)

        ok = QPushButton("确定")
        ok.setProperty("primary", True)
        ok.setDefault(True)
        ok.clicked.connect(self._accept)
        stop = QPushButton("停止备份")
        stop.setProperty("danger", True)
        stop.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(ok)
        buttons.addWidget(stop)
        layout = QVBoxLayout(self)
        layout.addWidget(text)
        layout.addWidget(QLabel("磁带号："))
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
        self._engine: Engine | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._stopping = False
        self._current_job: int | None = None
        self._samples: deque[tuple[float, int]] = deque()
        self._started_at = 0.0
        self._tape_answer: str | None = None
        self._tape_event = threading.Event()
        self._ticks = 0
        self._sample_job: int | None = None

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
        # queue
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["名称", "源文件夹", "大小", "状态"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False)  # long paths are elided on one line instead of collapsing to "C:..."
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(30)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col, width in ((0, 130), (2, 110), (3, 140)):  # fixed widths so the table does not jump while running
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)
        self.table.setMinimumHeight(130)
        self.add_button = QPushButton("添加文件夹…")
        self.add_button.clicked.connect(self.add_folder)
        self.remove_button = QPushButton("移除")
        self.remove_button.clicked.connect(self.remove_selected)
        queue_buttons = QVBoxLayout()
        queue_buttons.addWidget(self.add_button)
        queue_buttons.addWidget(self.remove_button)
        queue_buttons.addStretch()
        queue_row = QHBoxLayout()
        queue_row.addWidget(self.table, 1)
        queue_row.addLayout(queue_buttons)
        queue_box = QGroupBox("备份队列")
        queue_box.setLayout(queue_row)

        # tape row and free space
        self.tape_edit = QLineEdit()
        self.tape_edit.setPlaceholderText("磁带号")
        self.tape_edit.setMaximumWidth(180)
        self.tape_edit.textChanged.connect(self._upper_tape_id)
        self.read_button = QPushButton("读取磁带")
        self.read_button.clicked.connect(self.read_tape)
        self.staging_bar, self.staging_label = self._space_widgets()
        self.tape_bar, self.tape_label = self._space_widgets()
        tape_row = QGridLayout()
        tape_row.addWidget(QLabel("磁带号："), 0, 0)
        tape_row.addWidget(self.tape_edit, 0, 1)
        tape_row.addWidget(self.read_button, 0, 2)
        tape_row.addWidget(QLabel("中转盘："), 0, 4)
        tape_row.addWidget(self.staging_bar, 0, 5)
        tape_row.addWidget(self.staging_label, 0, 6)
        tape_row.addWidget(QLabel("磁带："), 1, 4)
        tape_row.addWidget(self.tape_bar, 1, 5)
        tape_row.addWidget(self.tape_label, 1, 6)
        tape_row.setColumnStretch(3, 1)

        # start / stop
        self.start_button = QPushButton("开始备份")
        self.start_button.setProperty("primary", True)
        self.start_button.setMinimumWidth(130)
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("停止")
        self.stop_button.setProperty("danger", True)
        self.stop_button.setMinimumWidth(130)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        run_row = QHBoxLayout()
        run_row.addWidget(self.start_button)
        run_row.addWidget(self.stop_button)
        run_row.addStretch()

        # progress
        steps_row = QHBoxLayout()
        self.step_labels: dict[str, QLabel] = {}
        for i, (key, text) in enumerate(STEPS):
            label = QLabel(text)
            label.setProperty("step", "todo")
            self.step_labels[key] = label
            if i:
                steps_row.addWidget(QLabel("→"))
            steps_row.addWidget(label)
        steps_row.addStretch()

        self.overall_caption = QLabel("总进度：—")
        self.overall_bar = self._bar()
        self.step_caption = QLabel("当前步骤：—")
        self.step_bar = self._bar()
        self.file_label = QLabel("当前文件：—")
        self.file_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.stats_label = QLabel()
        progress_layout = QVBoxLayout()
        progress_layout.addLayout(steps_row)
        progress_layout.addWidget(self.overall_caption)
        progress_layout.addWidget(self.overall_bar)
        progress_layout.addWidget(self.step_caption)
        progress_layout.addWidget(self.step_bar)
        progress_layout.addWidget(self.file_label)
        progress_layout.addWidget(self.stats_label)
        progress_box = QGroupBox("进度")
        progress_box.setLayout(progress_layout)

        # log
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(MAX_LOG_LINES)
        log_layout = QVBoxLayout()
        log_layout.addWidget(self.log)
        log_box = QGroupBox("运行记录")
        log_box.setLayout(log_layout)

        layout = QVBoxLayout(self)
        layout.addWidget(queue_box)
        layout.addLayout(tape_row)
        layout.addLayout(run_row)
        layout.addWidget(progress_box)
        layout.addWidget(log_box, 1)
        self._reset_progress()

    @staticmethod
    def _bar() -> QProgressBar:
        bar = QProgressBar()
        bar.setRange(0, BAR_RANGE)
        bar.setTextVisible(False)
        return bar

    @staticmethod
    def _space_widgets() -> tuple[QProgressBar, QLabel]:
        bar = QProgressBar()
        bar.setRange(0, BAR_RANGE)
        bar.setTextVisible(False)
        bar.setFixedWidth(140)
        bar.setFixedHeight(14)
        return bar, QLabel("—")

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
                jobs = db.active_jobs(con)
            finally:
                con.close()
        except Exception as e:  # unreadable data folder: show an empty queue, settings can fix it
            self._append_log(f"无法读取备份队列：{e}")
            jobs = []
        self.table.setRowCount(len(jobs))
        for row, job in enumerate(jobs):
            size = format_bytes(job["total_bytes"]) if job["scanned"] else "—"
            cells = [job["owner"], job["source_path"], size, ""]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, item)
            first = self.table.item(row, 0)
            first.setData(Qt.UserRole, job["id"])
            first.setData(Qt.UserRole + 1, (job["status"], job["scanned"]))
            self.table.item(row, 1).setToolTip(job["source_path"])
        self._update_status_column(None)

    def _update_status_column(self, snap: dict | None) -> None:
        for row in range(self.table.rowCount()):
            first = self.table.item(row, 0)
            job_id = first.data(Qt.UserRole)
            status, _scanned = first.data(Qt.UserRole + 1)
            if self._running and snap is not None and job_id == snap["job_id"]:
                text = f"进行中 {self._job_percent(snap):.0f}%"
            elif self._running and job_id == self._current_job:
                text = "进行中"
            elif status == "RUNNING":
                text = "未完成，可继续"
            else:
                text = "等待"
            item = self.table.item(row, 3)
            if item.text() != text:
                item.setText(text)

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
            db.add_job(con, name, folder)
        finally:
            con.close()
        self.refresh()
        self.jobs_changed.emit()

    def remove_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, TITLE, "请先在队列里选中要移除的一行。")
            return
        first = self.table.item(row, 0)
        job_id = first.data(Qt.UserRole)
        status, scanned = first.data(Qt.UserRole + 1)
        if status == "QUEUED" and not scanned:
            con = db.connect(self.cfg.db_path)
            try:
                db.remove_queued_job(con, job_id)
            finally:
                con.close()
        else:
            answer = QMessageBox.question(
                self, TITLE,
                f"“{first.text()}”已经开始备份。放弃它会删除中转盘上还没写入磁带的副本，不能恢复。\n"
                "已经写入磁带的文件仍会保留在记录里，以后再次备份时会自动跳过。\n\n确定放弃吗？",
            )
            if answer != QMessageBox.Yes:
                return
            try:
                Engine(self.cfg).abandon_job(job_id)
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
            QMessageBox.warning(self, TITLE, f"未检测到磁带（找不到 {self.cfg.tape_root}）。请先插入磁带。")
            return
        try:
            marker = read_tape_marker(self.cfg.tape_root)
        except OSError as e:
            QMessageBox.warning(self, TITLE, f"无法读取磁带：{e}")
            return
        if marker:
            self.tape_edit.setText(marker)
        else:
            QMessageBox.information(self, TITLE, "这盘磁带还没有磁带号（没有用本软件写过）。请手动输入一个新的磁带号。")

    def _update_space(self) -> None:
        self._show_space(self.staging_bar, self.staging_label, self.cfg.staging_root, "无法读取中转盘")
        self._show_space(self.tape_bar, self.tape_label, self.cfg.tape_root, "未检测到磁带")

    @staticmethod
    def _show_space(bar: QProgressBar, label: QLabel, path: str, missing: str) -> None:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            bar.setValue(0)
            label.setText(missing)
            return
        bar.setValue(int(BAR_RANGE * usage.free / usage.total) if usage.total else 0)
        label.setText(f"可用 {format_bytes(usage.free)} / 共 {format_bytes(usage.total)}")

    # ------------------------------------------------------------ start / stop

    def start(self) -> None:
        if self._running:
            return
        if self.table.rowCount() == 0:
            QMessageBox.information(self, TITLE, "备份队列是空的，请先添加要备份的文件夹。")
            return
        tape_id = normalize_tape_id(self.tape_edit.text())
        if not tape_id:
            QMessageBox.warning(self, TITLE, "请输入磁带号（字母、数字、横线、下划线），或点“读取磁带”。")
            return
        self.tape_edit.setText(tape_id)

        self._engine = Engine(
            self.cfg,
            on_log=self._log_line.emit,
            ask_tape=self._ask_tape_blocking,
            on_job_update=self._job_update.emit,
        )
        self._running = True
        self._stopping = False
        self._current_job = None
        self._samples.clear()
        self._started_at = time.monotonic()
        self._set_busy_ui(True)
        self.busy_changed.emit(True)
        self._thread = threading.Thread(target=self._worker, args=(self._engine, tape_id), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running or self._engine is None:
            return
        self._stopping = True
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
        self.stop_button.setEnabled(busy)
        self.stop_button.setText("停止")

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
        self._stopping = False
        self._engine = None
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self._reset_progress()
        self._set_busy_ui(False)
        self.refresh()
        self.jobs_changed.emit()
        self.busy_changed.emit(False)
        self.title_progress.emit("")
        if result == "done":
            QApplication.beep()
            QMessageBox.information(self, TITLE, message)
        elif result == "error":
            QApplication.beep()
            QMessageBox.critical(self, TITLE, message)

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(text)
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
        self._update_steps(snap["phase"])
        self._update_bars(snap)
        self._update_live(snap)
        self._update_status_column(snap)
        self.title_progress.emit(f"{self._job_percent(snap):.0f}%")

    def _update_steps(self, phase: str) -> None:
        keys = [key for key, _ in STEPS]
        active = keys.index(phase) if phase in keys else -1
        for i, key in enumerate(keys):
            state = "todo" if active < 0 else "done" if i < active else "active" if i == active else "todo"
            label = self.step_labels[key]
            if label.property("step") != state:
                label.setProperty("step", state)
                label.style().unpolish(label)
                label.style().polish(label)

    def _update_bars(self, snap: dict) -> None:
        owner = snap["owner"] or "—"
        pct = self._job_percent(snap)
        batch = ""
        if snap["batch_seq"]:
            batch = f"：第 {snap['batch_seq']} 批（预计共 {max(snap['batch_estimate'], snap['batch_seq'])} 批）"
        self.overall_caption.setText(f"总进度  {owner}{batch}  {pct:.1f}%")
        self.overall_bar.setValue(int(BAR_RANGE * pct / 100))

        phase, unit = snap["phase"], snap["phase_unit"]
        total, done = snap["phase_total"], snap["phase_done"]
        if phase == "scan":
            self.step_bar.setRange(0, 0)  # indeterminate: the total is unknown while scanning
            self.step_caption.setText(
                f"当前步骤  已扫描 {snap['scan_files']:,} 个文件，{format_bytes(snap['scan_bytes'])}"
            )
            return
        if self.step_bar.maximum() == 0:
            self.step_bar.setRange(0, BAR_RANGE)
        self.step_bar.setValue(int(BAR_RANGE * done / total) if total else 0)
        names = dict(STEPS)
        name = names.get(phase, "")
        if phase == "cleanup" or total == 0:
            self.step_caption.setText(f"当前步骤  {name}" if name else "当前步骤：—")
        elif unit == "bytes":
            self.step_caption.setText(f"当前步骤  {name}  {format_bytes(done)} / {format_bytes(total)}")
        else:
            self.step_caption.setText(f"当前步骤  {name}  {done:,} / {total:,} 个文件")

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
        speed_text = left_text = "—"
        if speed > 0:
            speed_text = f"{speed / 1024 ** 2:.1f} MB/s"
            if snap["job_total"] > 0:
                left_text = format_duration((snap["job_total"] - snap["job_done"]) / speed)
        self.stats_label.setText(
            f"速度：{speed_text}（最近 10 秒平均）    已用时间：{format_duration(now - self._started_at)}"
            f"    预计剩余：{left_text}"
        )

        current = snap["current_file"] or "—"
        fm = QFontMetrics(self.file_label.font())
        width = max(self.file_label.width() - fm.horizontalAdvance("当前文件：") - 8, 50)
        self.file_label.setText("当前文件：" + fm.elidedText(current, Qt.ElideMiddle, width))

    def _reset_progress(self) -> None:
        self._samples.clear()
        self._update_steps("idle")
        self.overall_caption.setText("总进度：—")
        self.overall_bar.setValue(0)
        self.step_caption.setText("当前步骤：—")
        self.step_bar.setRange(0, BAR_RANGE)
        self.step_bar.setValue(0)
        self.file_label.setText("当前文件：—")
        self.stats_label.setText("速度：—    已用时间：—    预计剩余：—")
