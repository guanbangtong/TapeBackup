"""Restore dialog: choose a destination folder, restore the given files from tape, show progress.

    RestoreDialog(cfg, files, parent).exec()      # files: rows from db.search_files / db.files_of_job

The Restorer runs in a worker thread. Its callbacks are forwarded to the UI thread with signals;
ask_tape blocks the worker until the user answers a modal box.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .config import Config
from .copier import format_bytes
from .restore import Restorer, plan_tapes

PROGRESS_STEPS = 1000  # byte counts exceed 32 bits, so the bar works on a fixed integer range


class RestoreDialog(QDialog):
    log_line = Signal(str)
    tape_requested = Signal(str, str)      # tape id, reason
    run_finished = Signal(str, str)        # result, message

    def __init__(self, cfg: Config, files: list, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.files = list(files)
        self._restorer: Restorer | None = None
        self._thread: threading.Thread | None = None
        self._answer_ready = threading.Event()
        self._answer = False

        self.setWindowTitle("从磁带取回文件")
        self.setMinimumWidth(680)
        self.resize(720, 640)

        total = sum(int(f["size"] or 0) for f in self.files)
        tapes = plan_tapes(self.files)
        summary = QLabel(f"将取回 {len(self.files)} 个文件，共 {format_bytes(total)}。")
        font = summary.font()
        font.setBold(True)
        summary.setFont(font)
        tape_lines = [f"{i}. {tape_id or '（记录里没有磁带号）'}：{count} 个文件" for i, (tape_id, count) in enumerate(tapes, 1)]
        tape_label = QLabel("需要的磁带（按取回顺序）：\n" + "\n".join(tape_lines))
        tape_label.setProperty("hint", "true")

        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText("选择要保存到的文件夹")
        self.browse_button = QPushButton("浏览…")
        self.browse_button.clicked.connect(self._browse)
        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("取回到："))
        dest_row.addWidget(self.dest_edit, 1)
        dest_row.addWidget(self.browse_button)
        note = QLabel("文件会保存在“目标文件夹\\所有者\\原路径”下，同名文件会被覆盖。")
        note.setProperty("hint", "true")

        self.start_button = QPushButton("开始取回")
        self.start_button.setProperty("primary", "true")
        self.start_button.clicked.connect(self._start)
        self.stop_button = QPushButton("停止")
        self.stop_button.setProperty("danger", "true")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)

        self.bar = QProgressBar()
        self.bar.setRange(0, PROGRESS_STEPS)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.amount_label = QLabel("")
        self.tape_now_label = QLabel("")
        self.file_label = QLabel("")
        self.file_label.setProperty("hint", "true")
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result_label = QLabel("")
        font = self.result_label.font()
        font.setBold(True)
        self.result_label.setFont(font)
        self.result_label.setWordWrap(True)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)

        layout = QVBoxLayout(self)
        layout.addWidget(summary)
        layout.addWidget(tape_label)
        layout.addSpacing(6)
        layout.addLayout(dest_row)
        layout.addWidget(note)
        layout.addLayout(buttons)
        layout.addSpacing(6)
        layout.addWidget(self.bar)
        layout.addWidget(self.amount_label)
        layout.addWidget(self.tape_now_label)
        layout.addWidget(self.file_label)
        layout.addWidget(self.result_label)
        layout.addWidget(self.log_view, 1)

        self.start_button.setEnabled(bool(self.files))
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll)
        self.log_line.connect(self.log_view.appendPlainText)
        self.tape_requested.connect(self._show_tape_box)
        self.run_finished.connect(self._on_finished)

    # ------------------------------------------------------------ state

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running and bool(self.files))
        self.stop_button.setEnabled(running)
        self.browse_button.setEnabled(not running)
        self.dest_edit.setReadOnly(running)
        self.close_button.setEnabled(not running)

    # ------------------------------------------------------------ actions

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择保存位置", self.dest_edit.text())
        if folder:
            self.dest_edit.setText(folder.replace("/", "\\"))

    def _start(self) -> None:
        dest = self.dest_edit.text().strip()
        if not dest:
            QMessageBox.warning(self, self.windowTitle(), "请先选择要保存到的文件夹。")
            return
        self.log_view.clear()
        self.result_label.setText("")
        self.bar.setValue(0)
        self._restorer = Restorer(
            self.cfg, self.files, dest, on_log=self.log_line.emit, ask_tape=self._ask_tape
        )
        self._set_running(True)
        self._thread = threading.Thread(target=self._work, args=(self._restorer,), daemon=True)
        self._thread.start()
        self.timer.start()

    def _stop(self) -> None:
        if self._restorer:
            self._restorer.stop()
        self.stop_button.setEnabled(False)
        self.log_line.emit("正在停止，请稍候……")

    # ------------------------------------------------------------ worker thread side

    def _work(self, restorer: Restorer) -> None:
        result, message = restorer.run()
        self.run_finished.emit(result, message)

    def _ask_tape(self, tape_id: str, reason: str) -> bool:
        """Called on the worker thread: block until the user answered the box shown in the UI thread."""
        self._answer_ready.clear()
        self.tape_requested.emit(tape_id, reason)
        self._answer_ready.wait()
        return self._answer

    # ------------------------------------------------------------ UI thread side

    def _show_tape_box(self, tape_id: str, reason: str) -> None:
        QApplication.beep()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("请插入磁带")
        box.setText(f"请插入磁带 {tape_id or '（未知）'}，然后点击继续。")
        box.setInformativeText(reason)
        go = box.addButton("继续", QMessageBox.ButtonRole.AcceptRole)
        quit_ = box.addButton("停止取回", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(go)
        box.setEscapeButton(quit_)  # closing the box counts as stop
        box.exec()
        self._answer = box.clickedButton() is go
        self._answer_ready.set()

    def _poll(self) -> None:
        if self._restorer is None:
            return
        snap = self._restorer.progress.snapshot()
        done, total = snap["done_bytes"], snap["total_bytes"]
        self.bar.setValue(PROGRESS_STEPS if total == 0 else int(done * PROGRESS_STEPS / total))
        self.amount_label.setText(
            f"{format_bytes(done)} / {format_bytes(total)}，{snap['done_files']} / {snap['total_files']} 个文件"
        )
        self.tape_now_label.setText(f"当前磁带：{snap['tape_id']}" if snap["tape_id"] else "")
        self.file_label.setText(f"正在处理：{snap['current_file']}" if snap["current_file"] else "")

    def _on_finished(self, result: str, message: str) -> None:
        self.timer.stop()
        if self._thread:
            self._thread.join()
        restorer = self._restorer
        assert restorer is not None
        self._poll()
        self.file_label.setText("")
        self._set_running(False)

        failures = restorer.failures
        if failures:
            self.log_view.appendPlainText(f"\n以下 {len(failures)} 个文件没有取回成功：")
            for owner, rel_path, reason in failures:
                self.log_view.appendPlainText(f"  {owner}\\{rel_path}：{reason}")
        if result == "done":
            self.bar.setValue(PROGRESS_STEPS)
            text = f"完成：成功取回 {restorer.restored} 个文件，失败 {len(failures)} 个。"
        elif result == "stopped":
            text = f"已停止：已取回 {restorer.restored} 个文件，失败 {len(failures)} 个。"
        else:
            text = f"出错了：{message}"
        self.result_label.setText(text)
        if result == "error":
            QMessageBox.warning(self, self.windowTitle(), text)

    # ------------------------------------------------------------ closing

    def _refuse_while_running(self) -> bool:
        if self.is_running():
            QMessageBox.information(self, self.windowTitle(), "正在取回文件。请先点击“停止”，等待停止完成后再关闭。")
            return True
        return False

    def reject(self) -> None:
        if not self._refuse_while_running():
            super().reject()

    def closeEvent(self, event) -> None:
        if self._refuse_while_running():
            event.ignore()
        else:
            super().closeEvent(event)
