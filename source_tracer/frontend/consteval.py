"""Evaluate simple C integer constant expressions (macro bodies, enum values).

Only literals, known names and arithmetic/bitwise operators are supported;
anything else returns ``None`` instead of raising.
"""

from __future__ import annotations

import ast
import operator
import re
from collections.abc import Mapping

_INT_LITERAL = re.compile(
    r"\b(0[xX][0-9a-fA-F]+|0[bB][01]+|\d+)(?:[uUlL]{1,3})?\b"
)
_CHAR_LITERAL = re.compile(r"'(\\.|[^\\'])'")
# Casts like (unsigned short), (uint8_t), (int32_t) -- a parenthesised type name.
_CAST = re.compile(
    r"\(\s*(?:const\s+|volatile\s+|signed\s+|unsigned\s+|struct\s+|enum\s+)*"
    r"(?:char|short|int|long|long\s+long|float|double|bool|_Bool|"
    r"u?int\d+_t|[us]\d+|U?INT\d+|BYTE|WORD|DWORD|UBYTE|UWORD|ULONG|[A-Za-z_]\w*_t)"
    r"(?:\s+(?:int|long))?\s*\)"
)
_ESCAPES = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, "'": 39, '"': 34, "a": 7, "b": 8, "f": 12, "v": 11}

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: lambda a, b: int(a / b) if b else None,  # C truncates toward zero
    ast.Mod: lambda a, b: a - b * int(a / b) if b else None,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
    ast.BitOr: operator.or_,
    ast.BitAnd: operator.and_,
    ast.BitXor: operator.xor,
}
_UNOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Invert: operator.invert}


def _char_value(m: re.Match) -> str:
    body = m.group(1)
    if body.startswith("\\"):
        esc = body[1:]
        if esc.startswith("x"):
            return str(int(esc[1:], 16))
        if esc.isdigit():
            return str(int(esc, 8))
        return str(_ESCAPES.get(esc, ord(esc[0])))
    return str(ord(body))


def _literal(m: re.Match) -> str:
    tok = m.group(1)
    if tok[:2] in ("0x", "0X", "0b", "0B"):
        return str(int(tok, 0))
    if len(tok) > 1 and tok.startswith("0"):
        return str(int(tok, 8))
    return tok


def evaluate(expr: str, names: Mapping[str, int] | None = None, depth: int = 0) -> int | None:
    """Return the integer value of ``expr`` or ``None`` if it can't be computed."""
    if depth > 16 or not expr or not expr.strip():
        return None
    names = names or {}
    text = expr.strip()
    text = re.sub(r"/\*.*?\*/|//.*$", " ", text, flags=re.S | re.M)
    text = _CHAR_LITERAL.sub(_char_value, text)
    text = _CAST.sub(" ", text)
    text = _INT_LITERAL.sub(_literal, text)
    text = text.replace("/", "//")
    text = text.replace("&&", " and ").replace("||", " or ")
    text = re.sub(r"!(?!=)", " not ", text)
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError:
        return None
    try:
        value = _eval(tree.body, names)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
    return int(value) if isinstance(value, int) else None


def _eval(node: ast.AST, names: Mapping[str, int]):
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        raise ValueError(node.id)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        result = _BINOPS[type(node.op)](_eval(node.left, names), _eval(node.right, names))
        if result is None:
            raise ValueError("division by zero")
        return result
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return int(not _eval(node.operand, names))
        if type(node.op) in _UNOPS:
            return _UNOPS[type(node.op)](_eval(node.operand, names))
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, names) for v in node.values]
        return int(all(vals)) if isinstance(node.op, ast.And) else int(any(vals))
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        a, b = _eval(node.left, names), _eval(node.comparators[0], names)
        ops = {ast.Eq: a == b, ast.NotEq: a != b, ast.Lt: a < b, ast.LtE: a <= b, ast.Gt: a > b, ast.GtE: a >= b}
        return int(ops[type(node.ops[0])])
    if isinstance(node, ast.IfExp):
        return _eval(node.body, names) if _eval(node.test, names) else _eval(node.orelse, names)
    raise ValueError(ast.dump(node))
