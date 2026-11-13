"""Read-only source view with line numbers and line highlighting."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QTextCursor, QTextFormat
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget

from source_tracer.gui.highlighter import CHighlighter
from source_tracer.gui.theme import BG, CURRENT_LINE, TEXT, TEXT_DIM


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
            block = block.next()
            top += round(self.blockBoundingRect(block).height()) if block.isValid() else height

    # -- navigation
    def goto_line(self, line: int) -> None:
        """Scroll to a 1-based line and highlight it."""
        block = self.document().findBlockByNumber(max(0, line - 1))
        cursor = QTextCursor(block)
        self.setTextCursor(cursor)
        self.centerCursor()
        self._marked_line = line
        sel = QTextEdit.ExtraSelection()
        sel.format.setBackground(QColor(CURRENT_LINE))
        sel.format.setProperty(QTextFormat.FullWidthSelection, True)
        sel.cursor = cursor
        sel.cursor.clearSelection()
        self.setExtraSelections([sel])
        self._numbers.update()
