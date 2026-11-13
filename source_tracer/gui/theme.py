"""Dark theme: palette + stylesheet + shared colours."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

# Shared colours (also used by the highlighter and code view).
BG = "#1e1f22"
BG_ALT = "#26282c"
PANEL = "#2b2d31"
BORDER = "#3a3d42"
TEXT = "#d4d7dc"
TEXT_DIM = "#8b9099"
ACCENT = "#4c9df3"
SELECTION = "#2f4f75"
CURRENT_LINE = "#2a3a4f"
WARN = "#e5b567"
ERROR = "#ef6b73"
OK = "#7cc87c"

SYNTAX = {
    "keyword": "#cc8bf0",
    "type": "#56b6c2",
    "preprocessor": "#e59f5c",
    "number": "#d19a66",
    "string": "#98c379",
    "comment": "#6a737d",
    "register": "#ef6b73",
    "fault": "#e5c07b",
}

STYLESHEET = f"""
QWidget {{ background: {BG}; color: {TEXT}; font-size: 10pt; }}
QMainWindow::separator {{ background: {BORDER}; width: 1px; height: 1px; }}
QDockWidget {{ titlebar-close-icon: none; }}
QDockWidget::title {{ background: {PANEL}; padding: 6px 8px; border-bottom: 1px solid {BORDER}; }}
QTreeView, QTableView, QListView, QPlainTextEdit, QTextBrowser, QLineEdit {{
    background: {BG_ALT}; border: 1px solid {BORDER}; selection-background-color: {SELECTION};
    selection-color: {TEXT}; alternate-background-color: #2a2c30;
}}
QHeaderView::section {{ background: {PANEL}; color: {TEXT_DIM}; border: none;
    border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER}; padding: 4px 6px; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; top: -1px; }}
QTabBar::tab {{ background: {PANEL}; color: {TEXT_DIM}; padding: 6px 14px; border: 1px solid {BORDER};
    border-bottom: none; margin-right: 1px; }}
QTabBar::tab:selected {{ background: {BG_ALT}; color: {TEXT}; border-top: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QToolBar {{ background: {PANEL}; border-bottom: 1px solid {BORDER}; spacing: 6px; padding: 3px; }}
QToolButton {{ padding: 4px 10px; border-radius: 4px; }}
QToolButton:hover {{ background: #3a3d42; }}
QPushButton {{ background: {ACCENT}; color: white; border: none; border-radius: 4px; padding: 7px 16px; }}
QPushButton:hover {{ background: #63abf5; }}
QPushButton[flat="true"] {{ background: transparent; color: {ACCENT}; }}
QComboBox {{ background: {BG_ALT}; border: 1px solid {BORDER}; border-radius: 3px; padding: 3px 8px; }}
QComboBox QAbstractItemView {{ background: {BG_ALT}; selection-background-color: {SELECTION}; }}
QLineEdit {{ padding: 4px 6px; border-radius: 3px; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QMenuBar {{ background: {PANEL}; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {SELECTION}; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; }}
QStatusBar {{ background: {PANEL}; color: {TEXT_DIM}; border-top: 1px solid {BORDER}; }}
QScrollBar:vertical, QScrollBar:horizontal {{ background: {BG}; border: none; width: 12px; height: 12px; }}
QScrollBar::handle {{ background: #44484e; border-radius: 5px; min-height: 24px; min-width: 24px; margin: 2px; }}
QScrollBar::handle:hover {{ background: #555a61; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QToolTip {{ background: {PANEL}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
QLabel#hint {{ color: {TEXT_DIM}; }}
QLabel#title {{ font-size: 18pt; font-weight: 600; }}
"""


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    c = QColor
    for role, colour in {
        QPalette.Window: BG,
        QPalette.WindowText: TEXT,
        QPalette.Base: BG_ALT,
        QPalette.AlternateBase: "#2a2c30",
        QPalette.ToolTipBase: PANEL,
        QPalette.ToolTipText: TEXT,
        QPalette.Text: TEXT,
        QPalette.Button: PANEL,
        QPalette.ButtonText: TEXT,
        QPalette.BrightText: ERROR,
        QPalette.Link: ACCENT,
        QPalette.Highlight: SELECTION,
        QPalette.HighlightedText: TEXT,
        QPalette.PlaceholderText: TEXT_DIM,
    }.items():
        pal.setColor(role, c(colour))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, c(TEXT_DIM))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)
