"""Read-only source view with line numbers and line highlighting."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QTextCursor, QTextFormat
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget

from source_tracer.gui.highlighter import CHighlighter
from source_tracer.gui.theme import BG, COVERED_LINE, CURRENT_LINE, OK, TEXT, TEXT_DIM


class _LineNumberArea(QWidget):
    def __init__(self, view: CodeView):
        super().__init__(view)
        self.view = view

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.view.line_number_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802
        self.view.paint_line_numbers(event)


class CodeView(QPlainTextEdit):
    def __init__(self, path: Path, text: str, registers: set[str] | None = None, faults: set[str] | None = None, parent=None):
        super().__init__(parent)
        self.path = path
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setPointSize(10)
        self.setFont(font)
        self.setPlainText(text)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.highlighter = CHighlighter(self.document(), registers, faults)
        self._numbers = _LineNumberArea(self)
        self._marked_line: int | None = None
        self._coverage: dict[int, int] = {}
        self._coverage_sels: list = []
        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._update_numbers)
        self._update_margin()

    # -- line numbers
    def line_number_width(self) -> int:
        digits = max(3, len(str(self.blockCount())))
        return 16 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margin(self, *_):
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _update_numbers(self, rect, dy):
        if dy:
            self._numbers.scroll(0, dy)
        else:
            self._numbers.update(0, rect.y(), self._numbers.width(), rect.height())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._numbers.setGeometry(QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self._numbers)
        painter.fillRect(event.rect(), QColor(BG))
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        height = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible():
                n = block.blockNumber() + 1
                painter.setPen(QColor(TEXT if n == self._marked_line else TEXT_DIM))
                painter.drawText(0, top, self._numbers.width() - 8, height, Qt.AlignRight, str(n))
                if n in self._coverage:
                    painter.fillRect(self._numbers.width() - 4, top, 3, height, QColor(OK))
            block = block.next()
            top += round(self.blockBoundingRect(block).height()) if block.isValid() else height

    # -- coverage (lines executed in the last run)
    def set_coverage(self, lines: dict[int, int]) -> None:
        self._coverage = dict(lines)
        sels = []
        for line in sorted(lines):
            block = self.document().findBlockByNumber(line - 1)
            if not block.isValid():
                continue
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor(COVERED_LINE))
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            sel.cursor = QTextCursor(block)
            sels.append(sel)
        self._coverage_sels = sels
        self._apply_selections()
        self._numbers.update()

    def _apply_selections(self, current: QTextEdit.ExtraSelection | None = None) -> None:
        sels = list(self._coverage_sels)
        if current is not None:
            sels.append(current)
        elif self._marked_line is not None:
            sels.append(self._current_sel(self._marked_line))
        self.setExtraSelections(sels)

    def _current_sel(self, line: int) -> QTextEdit.ExtraSelection:
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(QColor(CURRENT_LINE))
        sel.format.setProperty(QTextFormat.FullWidthSelection, True)
        sel.cursor = QTextCursor(self.document().findBlockByNumber(max(0, line - 1)))
        return sel

    # -- navigation
    def goto_line(self, line: int) -> None:
        """Scroll to a 1-based line and highlight it."""
        block = self.document().findBlockByNumber(max(0, line - 1))
        cursor = QTextCursor(block)
        self.setTextCursor(cursor)
        self.centerCursor()
        self._marked_line = line
        self._apply_selections(self._current_sel(line))
        self._numbers.update()
