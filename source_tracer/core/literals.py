"""Decode C literals (numbers, characters, strings)."""

from __future__ import annotations

from tree_sitter import Node

from source_tracer.frontend.parser import node_text

_SIMPLE_ESCAPES = {"n": 10, "t": 9, "r": 13, "0": 0, "a": 7, "b": 8, "f": 12, "v": 11, "\\": 92, "'": 39, '"': 34, "?": 63, "e": 27}


def decode_escapes(body: str) -> list[int]:
    out: list[int] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c != "\\" or i + 1 >= len(body):
            out.append(ord(c) & 0xFF if ord(c) < 256 else ord("?"))
            i += 1
            continue
        nxt = body[i + 1]
        if nxt == "x":
            j = i + 2
            while j < len(body) and body[j] in "0123456789abcdefABCDEF":
                j += 1
            out.append(int(body[i + 2 : j] or "0", 16) & 0xFF)
            i = j
        elif nxt in "01234567":
            j = i + 1
            while j < len(body) and j < i + 4 and body[j] in "01234567":
                j += 1
            out.append(int(body[i + 1 : j], 8) & 0xFF)
            i = j
        else:
            out.append(_SIMPLE_ESCAPES.get(nxt, ord(nxt)))
            i += 2
    return out


def string_value(node: Node) -> list[int]:
    """Bytes of a string literal (without the terminating NUL)."""
    if node.type == "concatenated_string":
        out: list[int] = []
        for c in node.named_children:
            if c.type in ("string_literal", "raw_string_literal"):
                out.extend(string_value(c))
        return out
    text = node_text(node)
    # strip prefix (L, u8, ...) and quotes
    start = text.find('"')
    end = text.rfind('"')
    return decode_escapes(text[start + 1 : end] if 0 <= start < end else "")


def char_value(node: Node) -> int:
    text = node_text(node)
    start = text.find("'")
    end = text.rfind("'")
    vals = decode_escapes(text[start + 1 : end] if 0 <= start < end else "")
    v = 0
    for b in vals:  # multi-char constants: 'AB' -> 0x4142
        v = (v << 8) | b
    return v


def number_value(text: str) -> tuple[int | float, bool, int, bool]:
    """Return (value, is_float, long_count, is_unsigned) for a C numeric literal."""
    t = text.replace("'", "")  # C++14 digit separators
    low = t.lower()
    is_hex = low.startswith("0x")
    is_float = not is_hex and any(c in low for c in ".e") or (low.endswith("f") and not is_hex)
    if is_hex and "p" in low:
        is_float = True
    if is_float:
        body = low.rstrip("fl")
        try:
            return (float.fromhex(body) if is_hex else float(body)), True, 0, False
        except ValueError:
            return 0.0, True, 0, False
    suffix = ""
    while low and low[-1] in "ul":
        suffix = low[-1] + suffix
        low = low[:-1]
    if low.startswith("0x"):
        v = int(low, 16)
    elif low.startswith("0b"):
        v = int(low, 2)
    elif len(low) > 1 and low.startswith("0"):
        v = int(low, 8)
    else:
        v = int(low or "0")
    return v, False, suffix.count("l"), "u" in suffix
