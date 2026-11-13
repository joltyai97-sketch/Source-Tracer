"""C/C++ syntax highlighting, plus project-aware colouring of registers and fault codes."""

from __future__ import annotations

import re

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument

from source_tracer.gui.theme import SYNTAX

KEYWORDS = (
    "auto break case catch class const constexpr continue default delete do else enum explicit extern "
    "for friend goto if inline mutable namespace new noexcept nullptr operator private protected public "
    "register return sizeof static static_cast reinterpret_cast const_cast dynamic_cast struct switch "
    "template this throw try typedef typename union using virtual volatile while true false NULL"
).split()
TYPES = (
    "void char short int long float double signed unsigned bool _Bool size_t "
    "int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t"
).split()


def _fmt(colour: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(colour))
    if bold:
        f.setFontWeight(QFont.Bold)
    if italic:
        f.setFontItalic(True)
    return f


class CHighlighter(QSyntaxHighlighter):
    def __init__(self, document: QTextDocument, registers: set[str] | None = None, faults: set[str] | None = None):
        super().__init__(document)
        self.rules: list[tuple[re.Pattern, QTextCharFormat]] = [
            (re.compile(r"\b(?:%s)\b" % "|".join(TYPES) + r"|\b\w+_t\b"), _fmt(SYNTAX["type"])),
            (re.compile(r"\b(?:%s)\b" % "|".join(KEYWORDS)), _fmt(SYNTAX["keyword"])),
            (re.compile(r"\b(?:0[xX][0-9a-fA-F]+|0[bB][01]+|\d+\.?\d*(?:[eE][-+]?\d+)?)[uUlLfF]*\b"), _fmt(SYNTAX["number"])),
        ]
        if registers:
            self.rules.append((re.compile(r"\b(?:%s)\b" % "|".join(map(re.escape, sorted(registers)))), _fmt(SYNTAX["register"], bold=True)))
        if faults:
            self.rules.append((re.compile(r"\b(?:%s)\b" % "|".join(map(re.escape, sorted(faults)))), _fmt(SYNTAX["fault"], bold=True)))
        self.preproc = (re.compile(r"^\s*#\s*\w+"), _fmt(SYNTAX["preprocessor"]))
        self.string = (re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'"), _fmt(SYNTAX["string"]))
        self.comment_fmt = _fmt(SYNTAX["comment"], italic=True)

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt naming)
        for pattern, fmt in self.rules:
            for m in pattern.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)
        m = self.preproc[0].match(text)
        if m:
            self.setFormat(m.start(), m.end() - m.start(), self.preproc[1])
        for m in self.string[0].finditer(text):
            self.setFormat(m.start(), m.end() - m.start(), self.string[1])
        self._comments(text)

    def _comments(self, text: str) -> None:
        # State 1 = inside a /* block comment */ carried over from the previous line.
        start = 0 if self.previousBlockState() == 1 else -1
        self.setCurrentBlockState(0)
        i = 0
        while i < len(text):
            if start >= 0:
                end = text.find("*/", i)
                if end < 0:
                    self.setFormat(start, len(text) - start, self.comment_fmt)
                    self.setCurrentBlockState(1)
                    return
                self.setFormat(start, end + 2 - start, self.comment_fmt)
                i, start = end + 2, -1
                continue
            line_c = text.find("//", i)
            block_c = text.find("/*", i)
            if line_c < 0 and block_c < 0:
                return
            if block_c < 0 or (0 <= line_c < block_c):
                if not self._in_string(text, line_c):
                    self.setFormat(line_c, len(text) - line_c, self.comment_fmt)
                    return
                i = line_c + 2
            else:
                if self._in_string(text, block_c):
                    i = block_c + 2
                    continue
                start, i = block_c, block_c + 2

    @staticmethod
    def _in_string(text: str, pos: int) -> bool:
        return text[:pos].count('"') % 2 == 1
