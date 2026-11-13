"""Compiler-dialect normalisation.

Embedded compilers add non-standard keywords (``interrupt``, ``@far``, ...).
We blank them with spaces *of the same length* so every line/column reported
by the parser still points at the original text shown to the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class BlankedKeyword:
    keyword: str
    line: int  # 1-based
    column: int  # 0-based


def _pattern(keywords: list[str]) -> re.Pattern | None:
    if not keywords:
        return None
    # Longest first so "__interrupt" wins over "interrupt".
    alts = "|".join(re.escape(k) for k in sorted(set(keywords), key=len, reverse=True))
    return re.compile(rf"(?<![\w@])(?:{alts})(?!\w)")


def _code_mask(text: str) -> list[bool]:
    """True for characters that are code (not inside comments, strings or char literals)."""
    mask = [True] * len(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        two = text[i : i + 2]
        if two == "//":
            j = text.find("\n", i)
            j = n if j < 0 else j
        elif two == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
        else:
            i += 1
            continue
        for k in range(i, j):
            mask[k] = False
        i = j
    return mask


def blank_keywords(text: str, keywords: list[str]) -> tuple[str, list[BlankedKeyword]]:
    """Replace compiler-specific keywords in code (not comments/strings) with spaces."""
    pattern = _pattern(keywords)
    if pattern is None:
        return text, []
    mask = _code_mask(text)
    chars = list(text)
    found: list[BlankedKeyword] = []
    line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
    for m in pattern.finditer(text):
        if not mask[m.start()]:
            continue
        line = _line_of(line_starts, m.start())
        if text[line_starts[line] : m.start()].lstrip().startswith("#"):
            continue  # leave preprocessor lines (e.g. `#pragma interrupt`) intact
        for k in range(m.start(), m.end()):
            chars[k] = " "
        found.append(BlankedKeyword(m.group(0), line + 1, m.start() - line_starts[line]))
    return "".join(chars), found


def _line_of(line_starts: list[int], offset: int) -> int:
    lo, hi = 0, len(line_starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo
