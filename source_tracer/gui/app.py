"""Create the Qt application and main window."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from source_tracer.gui.main_window import MainWindow
from source_tracer.gui.theme import apply_dark_theme


def run(args: list[str]) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Source Tracer")
    apply_dark_theme(app)
    window = MainWindow()
    window.show()
    paths = [Path(a) for a in args if not a.startswith("-")]
    if paths:
        window.load(paths)
    return app.exec()
