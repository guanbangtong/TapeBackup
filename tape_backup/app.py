"""Main window: three tabs (backup / search / records) and the settings button."""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QLockFile
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QPushButton, QTabWidget

from . import db
from .config import Config
from .ui_backup import BackupPage
from .ui_records import RecordsPage
from .ui_search import SearchPage
from .ui_settings import SettingsDialog

APP_TITLE = "磁带备份 TapeBackup"

# One stylesheet for the whole app; pages only set the dynamic properties used below.
STYLE = """
QWidget { font-size: 10pt; }
QTabWidget::pane { border: 1px solid #d0d7de; top: -1px; background: #ffffff; }
QTabBar::tab { padding: 8px 22px; border: 1px solid #d0d7de; border-bottom: none;
               background: #f3f5f7; margin-right: 2px; }
QTabBar::tab:selected { background: #ffffff; font-weight: bold; }
QPushButton { padding: 6px 16px; border: 1px solid #c4ccd4; border-radius: 4px; background: #f6f8fa; }
QPushButton:hover { background: #eaeef2; }
QPushButton:disabled { color: #9aa4ae; background: #f3f5f7; }
QPushButton[primary="true"] { background: #1f6feb; color: white; border-color: #1f6feb; font-weight: bold; }
QPushButton[primary="true"]:hover { background: #1a5fd0; }
QPushButton[primary="true"]:disabled { background: #9dbcf0; border-color: #9dbcf0; color: white; }
QPushButton[danger="true"] { background: #d1242f; color: white; border-color: #d1242f; font-weight: bold; }
QPushButton[danger="true"]:disabled { background: #e9a3a8; border-color: #e9a3a8; color: white; }
QProgressBar { border: 1px solid #c4ccd4; border-radius: 4px; text-align: center; height: 20px; background: #f3f5f7; }
QProgressBar::chunk { background: #2da44e; border-radius: 3px; }
QLabel[step="todo"]   { color: #8c959f; padding: 5px 12px; border: 1px solid #d0d7de; border-radius: 12px; }
QLabel[step="active"] { color: white; background: #1f6feb; padding: 5px 12px; border: 1px solid #1f6feb;
                        border-radius: 12px; font-weight: bold; }
QLabel[step="done"]   { color: #1a7f37; padding: 5px 12px; border: 1px solid #2da44e; border-radius: 12px; }
QLabel[hint="true"] { color: #57606a; }
QTableView, QTableWidget, QTreeWidget, QPlainTextEdit, QLineEdit, QComboBox { border: 1px solid #d0d7de; }
QHeaderView::section { padding: 5px; border: none; border-bottom: 1px solid #d0d7de; background: #f6f8fa; }
"""


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle(APP_TITLE)
        self.resize(1100, 760)

        self.backup_page = BackupPage(cfg)
        self.search_page = SearchPage(cfg)
        self.records_page = RecordsPage(cfg)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.backup_page, "备份")
        self.tabs.addTab(self.search_page, "查询")
        self.tabs.addTab(self.records_page, "记录")
        self.setCentralWidget(self.tabs)

        self.settings_button = QPushButton("设置")
        self.settings_button.clicked.connect(self.open_settings)
        self.tabs.setCornerWidget(self.settings_button)

        self.tabs.currentChanged.connect(self._tab_changed)
        self.backup_page.jobs_changed.connect(self.records_page.refresh)
        self.backup_page.busy_changed.connect(lambda busy: self.settings_button.setEnabled(not busy))
        self.backup_page.title_progress.connect(self._set_title_progress)
        self.records_page.records_changed.connect(self.search_page.refresh)

    def _tab_changed(self, index: int) -> None:
        page = self.tabs.widget(index)
        if page is not self.backup_page:
            page.refresh()  # type: ignore[attr-defined]

    def _set_title_progress(self, text: str) -> None:
        self.setWindowTitle(f"{text} - {APP_TITLE}" if text else APP_TITLE)

    def open_settings(self) -> None:
        if SettingsDialog(self.cfg, self).exec():
            for page in (self.backup_page, self.search_page, self.records_page):
                page.refresh()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.backup_page.is_running():
            QMessageBox.information(
                self, APP_TITLE, "备份正在进行。请先点击“停止”，等待停止完成后再关闭窗口。\n已完成的部分会保留，下次可以继续。"
            )
            event.ignore()
            return
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLE)

    cfg = Config.load()
    while True:  # the database lives in cfg.data_dir; if that is unreachable, let the user fix the settings
        try:
            db.connect(cfg.db_path).close()
            break
        except Exception as e:
            QMessageBox.warning(
                None, APP_TITLE, f"无法打开记录数据库：\n{cfg.db_path}\n\n{e}\n\n请在设置中检查“记录存放目录”。"
            )
            if not SettingsDialog(cfg).exec():
                return 1

    # Two copies working on the same records and staging folder would corrupt each other's work.
    lock = QLockFile(os.path.join(cfg.data_dir, "tapebackup.lock"))
    lock.setStaleLockTime(0)  # the lock is only released when this process ends (or has died)
    if not lock.tryLock(100):
        QMessageBox.warning(None, APP_TITLE, "磁带备份软件已经在运行（可能在另一个窗口或另一个用户下）。\n同一时间只能打开一个。")
        return 1

    window = MainWindow(cfg)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
