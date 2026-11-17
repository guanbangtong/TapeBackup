"""Settings: a tab page (SettingsPage) and a thin dialog wrapper (SettingsDialog) used only at start-up."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDoubleValidator, QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .config import Config
from .ui_style import card, label, page_margins, set_prop

COLUMN_WIDTH = 760   # the form never gets wider than this
LABEL_WIDTH = 230    # fixed label column so all fields line up
NUMBER_WIDTH = 120


class NumberEdit(QLineEdit):
    """A number typed into an ordinary field, kept inside [minimum, maximum]."""

    valueChanged = Signal()

    def __init__(self, minimum: float, maximum: float, decimals: int = 0) -> None:
        super().__init__()
        self._minimum, self._maximum, self._decimals = minimum, maximum, decimals
        if decimals:
            validator = QDoubleValidator(minimum, maximum, decimals, self)
            validator.setNotation(QDoubleValidator.StandardNotation)
            self.setValidator(validator)
        else:
            self.setValidator(QIntValidator(int(minimum), int(maximum), self))
        self.textChanged.connect(lambda _text: self.valueChanged.emit())

    def value(self) -> float:
        try:
            number = float(self.text().replace(",", "."))
        except ValueError:
            number = self._minimum
        number = min(max(number, self._minimum), self._maximum)
        return round(number, self._decimals) if self._decimals else int(number)

    def setValue(self, number: float) -> None:
        self.setText(f"{number:.{self._decimals}f}" if self._decimals else str(int(number)))


def parse_extensions(text: str) -> list[str]:
    """'RAW, .tif' -> ['.raw', '.tif'] (lower case, leading dot, no duplicates)."""
    result: list[str] = []
    for part in text.replace("，", ",").replace(";", ",").replace("；", ",").split(","):
        part = part.strip().lower()
        if not part:
            continue
        if not part.startswith("."):
            part = "." + part
        if part not in result:
            result.append(part)
    return result


class _Form:
    """Rows of 'label | field | optional button' with a hint line under the field, inside one card."""

    def __init__(self, title: str) -> None:
        self.frame, layout = card(margins=20, spacing=14)
        layout.addWidget(label(title, "title"))
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(2)
        self.grid.setColumnMinimumWidth(0, LABEL_WIDTH)
        self.grid.setColumnMinimumWidth(2, 90)  # same width as a browse button, so rows without one line up
        self.grid.setColumnStretch(1, 1)
        layout.addLayout(self.grid)
        self._layout = layout
        self._row = 0

    def add(self, text: str, field: QWidget, hint: str, button: QPushButton | None = None) -> None:
        name = label(text)
        name.setFixedWidth(LABEL_WIDTH)
        self.grid.addWidget(name, self._row, 0, Qt.AlignLeft | Qt.AlignVCenter)
        if isinstance(field, NumberEdit):
            field.setFixedWidth(NUMBER_WIDTH)
            self.grid.addWidget(field, self._row, 1, Qt.AlignLeft)
        else:
            self.grid.addWidget(field, self._row, 1)
        if button is not None:
            self.grid.addWidget(button, self._row, 2)
        self._hint(hint)

    def add_wide(self, widget: QWidget, hint: str) -> None:
        """A full-width row (checkbox) with its hint, placed above the grid rows."""
        box = QVBoxLayout()
        box.setSpacing(2)
        box.addWidget(widget)
        text = label(hint, "hint")
        text.setWordWrap(True)
        box.addWidget(text)
        self._layout.insertLayout(1, box)

    def _hint(self, hint: str) -> None:
        if hint:
            text = label(hint, "hint")
            text.setWordWrap(True)
            # Ignored: the hint wraps to the card width and never pushes the columns around
            text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self.grid.addWidget(text, self._row + 1, 1, 1, 2)
        self.grid.setRowMinimumHeight(self._row + 2, 10)  # breathing space before the next row
        self._row += 3


class SettingsPage(QWidget):
    saved = Signal()

    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.setObjectName("page")

        self.staging = self._path_edit()
        self.tape = self._path_edit()
        self.data_dir = self._path_edit()
        self.db_backup = self._path_edit()
        self.reserve_staging = self._gb_spin(100000)
        self.reserve_tape = self._gb_spin(100000)
        self.threads = NumberEdit(1, 32)
        self.extensions = QLineEdit()
        self.delete_default = QCheckBox("新加入队列的文件夹默认勾选“备份后删除源数据”")
        self.delete_delay = self._gb_spin(120)
        self.recycle = QLineEdit()

        where = _Form("位置")
        where.add("中转目录", self.staging, "NAS 的文件先拷到这里，再写入磁带。请选本机空间大的硬盘。",
                  self._browse_button(self.staging, "中转目录"))
        where.add("磁带盘符", self.tape, "磁带插入后在电脑里显示的盘符，例如 F:\\。",
                  self._browse_button(self.tape, "磁带盘符"))
        where.add("记录存放目录", self.data_dir, "备份记录和运行日志保存在这里，请选本机固定的文件夹。",
                  self._browse_button(self.data_dir, "记录存放目录"))
        where.add("记录备份到 NAS 的目录", self.db_backup, "备份完成后，把记录复制一份到这里防丢失。留空表示不备份。",
                  self._browse_button(self.db_backup, "记录备份目录"))

        backup = _Form("备份")
        backup.add("中转盘保留空间 (GB)", self.reserve_staging, "中转盘至少留出这么多空间不用，避免写满。")
        backup.add("磁带保留空间 (GB)", self.reserve_tape, "每盘磁带至少留出这么多空间不写，不够时会提示换磁带。")
        backup.add("同时拷贝的文件数 (1–32)", self.threads, "数字越大，从 NAS 拷到中转盘越快，但对 NAS 压力也越大。")
        backup.add("直接拷贝的扩展名", self.extensions,
                   "用逗号分隔，例如 .raw, .tif。这些文件原样拷贝，其余文件会打包成 tar。")

        delete = _Form("备份后删除 NAS 源数据")
        delete.add_wide(self.delete_default,
                        "只有磁带上已有路径、大小和修改时间都相同的文件，才会从 NAS 删除；没写进磁带的文件不会动。")
        delete.add("删除前等待 (分钟，0–120)", self.delete_delay,
                   "写完磁带后先等一会儿，让磁带机把目录写进磁带（LTFS 通常每 5 分钟写一次），"
                   "这样备份刚结束就断电也不会丢数据。")
        delete.add("NAS 回收站文件夹名", self.recycle,
                   "删除后会把被删文件夹在回收站里的对应文件夹整个清空，空间才会真正释放。"
                   "例如删除 Z:\\LiHua 时，清空 Z:\\#recycle\\LiHua。留空表示不处理回收站。")

        self.save_button = QPushButton("保存设置")
        self.save_button.setProperty("primary", True)
        self.save_button.clicked.connect(self._save)
        self.status = label("")
        self.status.setProperty("status", "ok")

        footer = QHBoxLayout()
        footer.addWidget(self.save_button)
        footer.addSpacing(12)
        footer.addWidget(self.status)
        footer.addStretch()

        column = QWidget()
        column.setMaximumWidth(COLUMN_WIDTH)
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(12)
        for form in (where, backup, delete):
            column_layout.addWidget(form.frame)
        column_layout.addStretch()

        inner = QWidget()
        inner.setObjectName("page")
        inner_layout = QHBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 8, 0)
        inner_layout.addWidget(column, 0, Qt.AlignTop)
        inner_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        page_margins(outer)
        outer.addWidget(scroll, 1)
        outer.addLayout(footer)  # below the scroll area, so the save button is always in view

        self.refresh()
        for edit in (self.staging, self.tape, self.data_dir, self.db_backup, self.extensions, self.recycle):
            edit.textChanged.connect(self._clear_status)
        for spin in (self.reserve_staging, self.reserve_tape, self.threads, self.delete_delay):
            spin.valueChanged.connect(self._clear_status)
        self.delete_default.toggled.connect(self._clear_status)

    # ---- widget factories
    def _path_edit(self) -> QLineEdit:
        edit = QLineEdit()
        edit.setMinimumWidth(320)
        edit.textChanged.connect(lambda text, e=edit: e.setToolTip(text))
        return edit

    @staticmethod
    def _gb_spin(maximum: float) -> NumberEdit:
        spin = NumberEdit(0, maximum, decimals=1)
        return spin

    def _browse_button(self, edit: QLineEdit, title: str) -> QPushButton:
        button = QPushButton("浏览…")
        button.clicked.connect(lambda _checked=False: self._browse(edit, title))
        return button

    def _browse(self, edit: QLineEdit, title: str) -> None:
        folder = QFileDialog.getExistingDirectory(self, f"选择{title}", edit.text())
        if folder:
            edit.setText(folder.replace("/", "\\"))
            edit.setCursorPosition(0)

    # ---- loading and saving
    def refresh(self) -> None:
        """Reload every field from cfg, discarding unsaved edits."""
        cfg = self.cfg
        for edit, value in ((self.staging, cfg.staging_root), (self.tape, cfg.tape_root),
                            (self.data_dir, cfg.data_dir), (self.db_backup, cfg.db_backup_dir),
                            (self.extensions, ", ".join(cfg.raw_extensions)), (self.recycle, cfg.nas_recycle_dir)):
            edit.setText(value)
            edit.setCursorPosition(0)  # long paths show their beginning
        self.reserve_staging.setValue(cfg.reserve_staging_gb)
        self.reserve_tape.setValue(cfg.reserve_tape_gb)
        self.threads.setValue(int(cfg.threads))
        self.delete_default.setChecked(bool(cfg.delete_source_default))
        self.delete_delay.setValue(cfg.delete_delay_minutes)
        self._clear_status()

    def _clear_status(self, *_args) -> None:
        self.status.setText("")

    def _warn(self, message: str, field: QWidget) -> None:
        QMessageBox.warning(self, "设置", message)
        field.setFocus()

    def _save(self) -> None:
        for edit, name in ((self.staging, "中转目录"), (self.tape, "磁带盘符"), (self.data_dir, "记录存放目录")):
            if not edit.text().strip():
                self._warn(f"“{name}”不能为空。", edit)
                return
        extensions = parse_extensions(self.extensions.text())
        if not extensions:
            self._warn("“直接拷贝的扩展名”至少要填一个，例如 .raw。", self.extensions)
            return

        cfg = self.cfg
        cfg.staging_root = self.staging.text().strip()
        cfg.tape_root = self.tape.text().strip()
        cfg.data_dir = self.data_dir.text().strip()
        cfg.db_backup_dir = self.db_backup.text().strip()
        cfg.reserve_staging_gb = self.reserve_staging.value()
        cfg.reserve_tape_gb = self.reserve_tape.value()
        cfg.threads = self.threads.value()
        cfg.raw_extensions = extensions
        cfg.delete_source_default = self.delete_default.isChecked()
        cfg.delete_delay_minutes = self.delete_delay.value()
        cfg.nas_recycle_dir = self.recycle.text().strip()
        cfg.__post_init__()  # normalise paths such as "D:" -> "D:\"
        try:
            cfg.save()
        except OSError as e:
            QMessageBox.critical(self, "设置", f"设置保存失败：{e}")
            return
        self.refresh()  # show the normalised values
        self.status.setText("已保存")
        set_prop(self.status, "status", "ok")
        self.saved.emit()


class SettingsDialog(QDialog):
    """Start-up only: the same page in a window, closed with accept once it has been saved."""

    def __init__(self, cfg: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(860, 900)
        self.page = SettingsPage(cfg)
        self.page.saved.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(20, 0, 20, 14)
        buttons.addStretch()
        buttons.addWidget(cancel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.page, 1)
        layout.addLayout(buttons)
