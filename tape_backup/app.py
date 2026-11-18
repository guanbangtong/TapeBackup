"""Main window: five tabs (backup / search / records / statistics / settings)."""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QLockFile
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QTabWidget

from . import db
from .config import Config
from .icon import app_icon
from .ui_backup import BackupPage
from .ui_records import RecordsPage
from .ui_search import SearchPage
from .ui_settings import SettingsDialog, SettingsPage
from .ui_stats import StatsPage
from .ui_style import apply_theme

APP_TITLE = "磁带备份"


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle(APP_TITLE)
        self.resize(1180, 780)

        self.backup_page = BackupPage(cfg)
        self.search_page = SearchPage(cfg)
        self.records_page = RecordsPage(cfg)
        self.stats_page = StatsPage(cfg)
        self.settings_page = SettingsPage(cfg)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.backup_page, "备份")
        self.tabs.addTab(self.search_page, "查询")
        self.tabs.addTab(self.records_page, "记录")
        self.tabs.addTab(self.stats_page, "统计")
        self.tabs.addTab(self.settings_page, "设置")
        self.setCentralWidget(self.tabs)

        self.tabs.currentChanged.connect(self._tab_changed)
        self.backup_page.jobs_changed.connect(self.records_page.refresh)
        self.backup_page.busy_changed.connect(self._busy_changed)
        self.backup_page.title_progress.connect(self._set_title_progress)
        self.records_page.records_changed.connect(self.search_page.refresh)
        self.settings_page.saved.connect(self._settings_saved)

    def _tab_changed(self, index: int) -> None:
        page = self.tabs.widget(index)
        if page is not self.backup_page:
            page.refresh()  # type: ignore[attr-defined] 

    def _busy_changed(self, busy: bool) -> None:
        # Paths must not change under a running backup.
        self.tabs.setTabEnabled(self.tabs.indexOf(self.settings_page), not busy)
        self.tabs.setTabToolTip(self.tabs.indexOf(self.settings_page), "备份进行中，不能修改设置" if busy else "")

    def _settings_saved(self) -> None:
        for page in (self.backup_page, self.search_page, self.records_page, self.stats_page):
            page.refresh()

    def _set_title_progress(self, text: str) -> None:
        self.setWindowTitle(f"{text} - {APP_TITLE}" if text else APP_TITLE)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.backup_page.is_running():
            QMessageBox.information(
                self, APP_TITLE, "备份正在进行。请先点击“停止”，等待停止完成后再关闭窗口。\n已完成的部分会保留，下次可以继续。"
            )
            event.ignore()
            return
        event.accept()


def main() -> int:
    if sys.platform == "win32":
        # Own taskbar identity, so Windows shows this app's icon instead of python's when run from source.
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TapeBackup")
    app = QApplication(sys.argv)
    app.setWindowIcon(app_icon())
    apply_theme(app)

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
