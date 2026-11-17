"""The look of the whole app in one place: colours, stylesheet, and a few small widget helpers.

Pages do not call setStyleSheet. They build plain widgets, use the helpers below, and set the
dynamic properties the stylesheet knows:

    QPushButton   primary="true"   the one main action of a page (solid blue)
                  danger="true"    a destructive or stopping action (red text, quiet until hovered)
                  flat="true"      a low-key text button
    QFrame        card="true"      white panel with a hairline border (use card())
    QLabel        role="title"     heading of a card
                  role="hint"      grey helper text
                  role="number"    a figure (percent, speed, size): DIN-style digits
                  role="hero"      the one big figure of a page
                  role="banner"    amber notice that needs the user's attention
                  status="wait|run|ok|warn|fail"   coloured status text
                  step="todo|active|done"          one step of the progress trail
    QProgressBar  tone="ok|warn"   green / amber instead of blue

After changing a property at run time call set_prop(widget, name, value) so it is re-styled.
"""

from __future__ import annotations

import os
import tempfile

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# ---- palette: one blue for action and progress, three status colours, greys for everything else
PRIMARY = "#1D4ED8"
PRIMARY_HOVER = "#1E40AF"
PRIMARY_SOFT = "#EFF6FF"
TEXT = "#1F2937"
MUTED = "#6B7280"
BORDER = "#E5E7EB"
SURFACE = "#FFFFFF"   # cards, tables, inputs
CANVAS = "#F3F4F6"    # window background
OK = "#15803D"
WARN = "#B45309"
WARN_SOFT = "#FFFBEB"
FAIL = "#B91C1C"

UI_FONT = "Microsoft YaHei UI"
NUMBER_FONT = "Bahnschrift"  # ships with Windows 10/11; DIN-style digits that read like an instrument

STATUS_COLORS = {"wait": MUTED, "run": PRIMARY, "ok": OK, "warn": WARN, "fail": FAIL}

STYLE = f"""
QWidget {{ color: {TEXT}; font-family: "{UI_FONT}"; font-size: 10pt; }}
QMainWindow, QDialog, QWidget#page {{ background: {CANVAS}; }}

/* tabs: plain text with a blue underline on the current one */
QTabWidget::pane {{ border: none; border-top: 1px solid {BORDER}; background: {CANVAS}; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ padding: 11px 22px; margin: 0; border: none; border-bottom: 3px solid transparent;
               background: transparent; color: {MUTED}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ color: {PRIMARY}; border-bottom-color: {PRIMARY}; font-weight: bold; }}
QTabBar::tab:disabled {{ color: #C4C9D1; }}

/* cards */
QFrame[card="true"] {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; }}
QLabel {{ background: transparent; border: none; }}
QLabel[role="title"] {{ font-size: 11pt; font-weight: bold; }}
QLabel[role="hint"] {{ color: {MUTED}; }}
QLabel[role="number"] {{ font-family: "{NUMBER_FONT}", "{UI_FONT}"; font-size: 12pt; }}
QLabel[role="hero"] {{ font-family: "{NUMBER_FONT}", "{UI_FONT}"; font-size: 46pt; color: {PRIMARY}; }}
QLabel[role="banner"] {{ background: {WARN_SOFT}; color: {WARN}; border: 1px solid #FDE68A;
                        border-radius: 6px; padding: 8px 12px; }}
QLabel[status="wait"] {{ color: {MUTED}; }}
QLabel[status="run"]  {{ color: {PRIMARY}; font-weight: bold; }}
QLabel[status="ok"]   {{ color: {OK}; }}
QLabel[status="warn"] {{ color: {WARN}; }}
QLabel[status="fail"] {{ color: {FAIL}; }}
QLabel[step="todo"]   {{ color: #9CA3AF; }}
QLabel[step="active"] {{ color: {PRIMARY}; font-weight: bold; }}
QLabel[step="done"]   {{ color: {OK}; }}

/* buttons */
QPushButton {{ padding: 7px 16px; border: 1px solid #D1D5DB; border-radius: 6px; background: {SURFACE}; }}
QPushButton:hover {{ background: #F9FAFB; border-color: #9CA3AF; }}
QPushButton:pressed {{ background: {CANVAS}; }}
QPushButton:disabled {{ color: #9CA3AF; background: #F9FAFB; border-color: {BORDER}; }}
QPushButton[primary="true"] {{ background: {PRIMARY}; border-color: {PRIMARY}; color: white; font-weight: bold;
                              padding: 10px 26px; font-size: 11pt; }}
QPushButton[primary="true"]:hover {{ background: {PRIMARY_HOVER}; border-color: {PRIMARY_HOVER}; }}
QPushButton[primary="true"]:disabled {{ background: #BFCDF3; border-color: #BFCDF3; color: white; }}
QPushButton[danger="true"] {{ color: {FAIL}; border-color: #F1C4C4; }}
QPushButton[danger="true"]:hover {{ background: #FEF2F2; border-color: {FAIL}; }}
QPushButton[danger="true"]:disabled {{ color: #D9A5A5; border-color: {BORDER}; }}
QPushButton[flat="true"] {{ border: none; background: transparent; color: {PRIMARY}; padding: 4px 6px; }}
QPushButton[flat="true"]:hover {{ color: {PRIMARY_HOVER}; background: {PRIMARY_SOFT}; }}

/* inputs */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: {SURFACE}; border: 1px solid #D1D5DB;
    border-radius: 6px; padding: 6px 8px; selection-background-color: {PRIMARY}; selection-color: white; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {PRIMARY}; }}
QLineEdit:disabled, QComboBox:disabled {{ background: #F9FAFB; color: #9CA3AF; }}
/* the text field inside a number box or combo must not get a second border and padding */
QAbstractSpinBox QLineEdit, QComboBox QLineEdit {{ border: none; padding: 0; background: transparent; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER};
    selection-background-color: {PRIMARY_SOFT}; selection-color: {TEXT}; outline: none; }}
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid #9CA3AF; border-radius: 3px;
    background: {SURFACE}; }}
QCheckBox::indicator:hover {{ border-color: {PRIMARY}; }}
QCheckBox::indicator:checked {{ border-color: {PRIMARY}; background: {PRIMARY}; image: url(%CHECK%); }}

/* tables: no grid, a hairline between rows, a quiet header */
QTableWidget, QTableView {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px;
    gridline-color: transparent; selection-background-color: {PRIMARY_SOFT}; selection-color: {TEXT}; outline: none; }}
QTableWidget::item, QTableView::item {{ padding: 2px 8px; border: none; border-bottom: 1px solid #F0F1F3; }}
QTableWidget::item:selected, QTableView::item:selected {{ background: {PRIMARY_SOFT}; color: {TEXT}; }}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{ background: {SURFACE}; color: {MUTED}; padding: 8px; border: none;
    border-bottom: 1px solid {BORDER}; font-size: 9pt; }}
QTableCornerButton::section {{ background: {SURFACE}; border: none; }}

/* progress bars: a thin track */
QProgressBar {{ border: none; border-radius: 4px; background: {BORDER}; max-height: 8px; min-height: 8px; }}
QProgressBar::chunk {{ background: {PRIMARY}; border-radius: 4px; }}
QProgressBar[tone="ok"]::chunk {{ background: {OK}; }}
QProgressBar[tone="warn"]::chunk {{ background: {WARN}; }}

/* log */
QPlainTextEdit {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px;
    font-family: "Consolas", "{UI_FONT}"; font-size: 9pt; color: #374151; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #D1D5DB; border-radius: 4px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: #9CA3AF; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #D1D5DB; border-radius: 4px; min-width: 28px; }}
QToolTip {{ background: {TEXT}; color: white; border: none; padding: 5px 8px; }}
QMessageBox, QInputDialog {{ background: {SURFACE}; }}
"""


def apply_theme(app: QApplication) -> None:
    """Fixed light theme, so the app looks the same whether Windows is in light or dark mode."""
    app.setStyle("Fusion")
    hints = app.styleHints()
    if hasattr(hints, "setColorScheme"):  # Qt 6.8+
        hints.setColorScheme(Qt.ColorScheme.Light)
    palette = QPalette()
    for role, colour in (
        (QPalette.Window, CANVAS), (QPalette.WindowText, TEXT), (QPalette.Base, SURFACE),
        (QPalette.AlternateBase, "#F9FAFB"), (QPalette.Text, TEXT), (QPalette.Button, SURFACE),
        (QPalette.ButtonText, TEXT), (QPalette.ToolTipBase, TEXT), (QPalette.ToolTipText, "#FFFFFF"),
        (QPalette.Highlight, PRIMARY), (QPalette.HighlightedText, "#FFFFFF"), (QPalette.PlaceholderText, "#9CA3AF"),
    ):
        palette.setColor(role, QColor(colour))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor("#9CA3AF"))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#9CA3AF"))
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#9CA3AF"))
    app.setPalette(palette)
    app.setFont(QFont(UI_FONT, 10))
    app.setStyleSheet(STYLE.replace("%CHECK%", _check_mark_file()))


def _check_mark_file() -> str:
    """A white tick for ticked checkboxes. Stylesheets can only take images from files, so draw one."""
    path = os.path.join(tempfile.gettempdir(), "tapebackup_check.png")
    image = QImage(32, 32, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor("#FFFFFF"), 4.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.drawPolyline([QPointF(7, 17), QPointF(13.5, 23), QPointF(25, 9.5)])
    painter.end()
    image.save(path, "PNG")
    return path.replace("\\", "/")


def set_prop(widget: QWidget, name: str, value) -> None:
    """Set a dynamic property and re-apply the stylesheet to the widget."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def card(margins: int = 16, spacing: int = 10) -> tuple[QFrame, QVBoxLayout]:
    """A white panel and the vertical layout inside it."""
    frame = QFrame()
    frame.setProperty("card", True)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    return frame, layout


def label(text: str = "", role: str | None = None) -> QLabel:
    widget = QLabel(text)
    if role:
        widget.setProperty("role", role)
    return widget


def page_margins(layout) -> None:
    """The outer spacing every tab page uses."""
    layout.setContentsMargins(20, 16, 20, 16)
    layout.setSpacing(12)


def setup_table(table: QTableWidget, *, multi_select: bool = False, row_height: int = 36) -> None:
    """Common table behaviour: whole-row selection, no grid, no row numbers, one-line cells."""
    table.setShowGrid(False)
    table.setAlternatingRowColors(False)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.ElideMiddle)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.ExtendedSelection if multi_select else QAbstractItemView.SingleSelection)
    table.setFocusPolicy(Qt.StrongFocus)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(row_height)
    header = table.horizontalHeader()
    header.setHighlightSections(False)
    header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    header.setSectionResizeMode(QHeaderView.Interactive)


def status_item(kind: str, text: str) -> QTableWidgetItem:
    """A table cell showing a status as a coloured dot and text. kind: wait | run | ok | warn | fail."""
    item = QTableWidgetItem(f"●  {text}")
    item.setForeground(QColor(STATUS_COLORS.get(kind, MUTED)))
    return item
