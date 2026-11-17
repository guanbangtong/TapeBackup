"""Settings dialog: one labelled field per Config value, with a short hint under each."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QGridLayout,
)

from .config import Config


def parse_extensions(text: str) -> list[str]:
    """'RAW, .tif' -> ['.raw', '.tif'] (lower case, leading dot, no duplicates)."""
    result: list[str] = []
    for part in text.replace("，", ",").replace(";", ",").split(","):
        part = part.strip().lower()
        if not part:
            continue
        if not part.startswith("."):
            part = "." + part
        if part not in result:
            result.append(part)
    return result


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("设置")
        self.setMinimumWidth(640)

        self.staging = QLineEdit(cfg.staging_root)
        self.tape = QLineEdit(cfg.tape_root)
        self.data_dir = QLineEdit(cfg.data_dir)
        self.reserve_staging = self._gb_spin(cfg.reserve_staging_gb)
        self.reserve_tape = self._gb_spin(cfg.reserve_tape_gb)
        self.threads = QSpinBox()
        self.threads.setRange(1, 32)
        self.threads.setValue(int(cfg.threads))
        self.extensions = QLineEdit(", ".join(cfg.raw_extensions))
        self.db_backup = QLineEdit(cfg.db_backup_dir)

        grid = QGridLayout()
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        rows = [
            ("中转目录", self.staging, True, "NAS 的文件先拷到这里，再写入磁带。请选本机空间大的硬盘，例如 D:\\。"),
            ("磁带盘符", self.tape, True, "磁带插入后在电脑里显示的盘符，例如 F:\\。"),
            ("记录存放目录", self.data_dir, True, "备份记录和运行日志保存在这里，请选本机固定的文件夹。"),
            ("中转盘保留空间 (GB)", self.reserve_staging, False, "中转盘至少留出这么多空间不用，避免写满。"),
            ("磁带保留空间 (GB)", self.reserve_tape, False, "每盘磁带至少留出这么多空间不写，不够时会提示换磁带。"),
            ("同时拷贝的文件数 (1–32)", self.threads, False, "数字越大，从 NAS 拷到中转盘越快，但对 NAS 压力也越大。"),
            ("直接拷贝的扩展名", self.extensions, False, "用逗号分隔，例如 .raw, .tif。这些文件原样拷贝，其余文件会打包成 tar。"),
            ("记录备份到 NAS 的目录", self.db_backup, True, "每次备份完成后，把记录复制一份到这里防丢失。留空表示不备份。"),
        ]
        for i, (title, widget, browse, hint) in enumerate(rows):
            grid.addWidget(QLabel(title), i * 2, 0)
            grid.addWidget(widget, i * 2, 1)
            if browse:
                button = QPushButton("浏览…")
                button.clicked.connect(lambda _checked=False, w=widget, t=title: self._browse(w, t))
                grid.addWidget(button, i * 2, 2)
            hint_label = QLabel(hint)
            hint_label.setProperty("hint", True)
            hint_label.setWordWrap(True)
            grid.addWidget(hint_label, i * 2 + 1, 1, 1, 2)

        ok = QPushButton("保存")
        ok.setProperty("primary", True)
        ok.setDefault(True)
        ok.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(ok)
        buttons.addWidget(cancel)

        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addSpacing(8)
        layout.addLayout(buttons)

    @staticmethod
    def _gb_spin(value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0, 100000)
        spin.setDecimals(1)
        spin.setValue(value)
        return spin

    def _browse(self, edit: QLineEdit, title: str) -> None:
        folder = QFileDialog.getExistingDirectory(self, f"选择{title}", edit.text())
        if folder:
            edit.setText(folder.replace("/", "\\"))

    def _save(self) -> None:
        required = [(self.staging, "中转目录"), (self.tape, "磁带盘符"), (self.data_dir, "记录存放目录")]
        for edit, name in required:
            if not edit.text().strip():
                QMessageBox.warning(self, "设置", f"“{name}”不能为空。")
                edit.setFocus()
                return
        extensions = parse_extensions(self.extensions.text())
        if not extensions:
            QMessageBox.warning(self, "设置", "“直接拷贝的扩展名”至少要填一个，例如 .raw。")
            self.extensions.setFocus()
            return

        cfg = self.cfg
        cfg.staging_root = self.staging.text().strip()
        cfg.tape_root = self.tape.text().strip()
        cfg.data_dir = self.data_dir.text().strip()
        cfg.reserve_staging_gb = self.reserve_staging.value()
        cfg.reserve_tape_gb = self.reserve_tape.value()
        cfg.threads = self.threads.value()
        cfg.raw_extensions = extensions
        cfg.db_backup_dir = self.db_backup.text().strip()
        try:
            cfg.save()
        except OSError as e:
            QMessageBox.critical(self, "设置", f"设置保存失败：{e}")
            return
        self.accept()
