"""Find likely fault codes, where they are stored and where they are raised or cleared.

No particular fault convention is assumed. Evidence is combined from:

* names (``FAULT_x``, ``ERR_x``, ``DTC``, ``ALARM``, ``g_errorFlags`` ...),
* types (enumerators of an enum named like ``fault_code_t``),
* usage (a constant stored into a fault variable or passed to a fault function,
  or a literal number written into a fault variable).

Everything found is a *candidate*: the user confirms or rejects it in the GUI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Node

from source_tracer.frontend.parser import ParsedFile, node_text
from source_tracer.frontend.symbols import (
    ENUM,
    ENUMERATOR,
    FIELD,
    FUNCTION,
    ISR,
    MACRO,
    METHOD,
    PROTOTYPE,
    TYPEDEF,
    VARIABLE,
    Symbol,
    SymbolTable,
)

_FAULT_WORDS = {
    "fault", "faults", "flt", "flts", "err", "errs", "error", "errors", "errno", "errcode",
    "dtc", "dtcs", "alarm", "alarms", "alm", "trip", "trips", "warn", "warning", "warnings",
    "diag", "diags", "fail", "failure", "failures", "fatal", "malfunction", "exception",
}
_FAULT_PREFIXES = ("fault", "alarm", "dtc", "diag", "error")
_CLEAR_WORDS = {"clear", "clr", "reset", "rst", "ack", "acknowledge", "remove", "unset", "cancel"}
_NONE_WORDS = {"none", "ok", "no", "clear", "cleared", "null", "nofault", "noerror"}

HIGH, MEDIUM, LOW = "high", "medium", "low"


def name_tokens(name: str) -> list[str]:
    """Split ``g_faultFlagsHI`` / ``FAULT_OVER_TEMP`` into lower-case words."""
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", parts)
    return [p.lower() for p in re.split(r"[_\W\d]+", parts) if p]


def looks_like_fault_name(name: str) -> bool:
    for tok in name_tokens(name):
        if tok in _FAULT_WORDS or any(tok.startswith(p) for p in _FAULT_PREFIXES):
            return True
    return False


def _is_clear_name(name: str) -> bool:
    return any(t in _CLEAR_WORDS for t in name_tokens(name))


def _is_none_name(name: str) -> bool:
    toks = name_tokens(name)
    return bool(toks) and toks[-1] in _NONE_WORDS


# --------------------------------------------------------------------------- results


@dataclass
class FaultUsage:
    file: Path
    line: int
    function: str  # enclosing function, "" at file scope
    action: str  # "set" | "clear" | "check" | "report"
    target: str  # variable or function the code goes into / is compared with
    code: str | None  # constant name, when a named constant is used
    value: int | None  # numeric value when known (literal or evaluated constant)
    text: str  # source line, stripped


@dataclass
class FaultCode:
    name: str
    value: int | None
    kind: str  # "code" (an identifier) or "bit" (a flag mask)
    symbol: Symbol | None
    confidence: str
    reasons: list[str] = field(default_factory=list)
    usages: list[FaultUsage] = field(default_factory=list)

    @property
    def set_sites(self) -> list[FaultUsage]:
        return [u for u in self.usages if u.action in ("set", "report")]


@dataclass
class FaultSink:
    """A variable that holds fault state, or a function that raises/clears faults."""

    symbol: Symbol
    role: str  # "store" | "reporter" | "clearer"
    confidence: str
    reasons: list[str] = field(default_factory=list)
    usages: list[FaultUsage] = field(default_factory=list)


@dataclass
class FaultReport:
    codes: list[FaultCode] = field(default_factory=list)
    sinks: list[FaultSink] = field(default_factory=list)
    usages: list[FaultUsage] = field(default_factory=list)

    def code(self, name: str) -> FaultCode | None:
        return next((c for c in self.codes if c.name == name), None)

    def sink(self, name: str) -> FaultSink | None:
        return next((s for s in self.sinks if s.symbol.name == name), None)

    @property
    def literal_codes(self) -> list[FaultUsage]:
        """Raw numbers written into fault stores (codes with no named constant)."""
        return [u for u in self.usages if u.code is None and u.value is not None and u.action in ("set", "report")]


# --------------------------------------------------------------------------- analysis


def _identifiers(node: Node) -> list[str]:
    """Identifiers under ``node`` in source order."""
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier":
            out.append(node_text(n))
        stack.extend(reversed(n.children))
    return out


def _literal_value(node: Node) -> int | None:
    n = node
    while n.type == "parenthesized_expression" and n.named_child_count == 1:
        n = n.named_children[0]
    if n.type == "cast_expression":
        v = n.child_by_field_name("value")
        return _literal_value(v) if v is not None else None
    if n.type == "number_literal":
        txt = node_text(n).rstrip("uUlL")
        try:
            return int(txt, 16) if txt.lower().startswith("0x") else int(txt, 8) if len(txt) > 1 and txt.startswith("0") else int(txt)
        except ValueError:
            return None
    return None


def _target_name(node: Node) -> str:
    """Name of an assignment target: ``x``, ``s.x``, ``p->x``, ``arr[i]`` -> ``x`` / ``arr``."""
    n = node
    while True:
        if n.type == "identifier":
            return node_text(n)
        if n.type == "field_expression":
            f = n.child_by_field_name("field")
            return node_text(f) if f is not None else node_text(n)
        if n.type in ("subscript_expression",):
            n = n.child_by_field_name("argument") or n.named_children[0]
            continue
        if n.type in ("parenthesized_expression", "pointer_expression") and n.named_child_count:
            n = n.named_children[-1]
            continue
        return node_text(n)


def _callee_name(node: Node) -> str:
    fn = node.child_by_field_name("function")
    if fn is None:
        return ""
    if fn.type == "field_expression":
        f = fn.child_by_field_name("field")
        return node_text(f) if f is not None else node_text(fn)
    if fn.type == "qualified_identifier":
        return node_text(fn).rpartition("::")[2]
    return node_text(fn)


def _function_bodies(table: SymbolTable) -> list[Symbol]:
    return [s for s in table.symbols if s.kind in (FUNCTION, ISR, METHOD) and s.node is not None]


def analyze_faults(files: list[ParsedFile], table: SymbolTable) -> FaultReport:
    by_path = {f.path: f for f in files}
    constants = table.constants()

    # ---- 1. candidate constants by name / enum type
    fault_enums = {
        s.name for s in table.symbols if s.kind == ENUM and looks_like_fault_name(s.name)
    }
    fault_types = set(fault_enums) | {
        s.name for s in table.symbols if s.kind == TYPEDEF and (looks_like_fault_name(s.name) or s.type.split()[-1:] and s.type.split()[-1] in fault_enums)
    }
    codes: dict[str, FaultCode] = {}
    for s in table.symbols:
        if s.kind not in (MACRO, ENUMERATOR) or s.int_value is None:
            continue
        reasons, confidence = [], None
        if s.kind == ENUMERATOR and s.parent in fault_enums:
            reasons.append(f"member of fault enum '{s.parent}'")
            confidence = HIGH
        if looks_like_fault_name(s.name):
            reasons.append("name suggests a fault/error code")
            confidence = confidence or MEDIUM
        if confidence:
            codes[s.name] = FaultCode(s.name, s.int_value, _code_kind(s), s, confidence, reasons)

    # ---- 2. candidate sinks by name / type
    sinks: dict[str, FaultSink] = {}

    def add_sink(sym: Symbol, role: str, confidence: str, reason: str) -> None:
        key = sym.qualified_name
        if key in sinks:
            if reason not in sinks[key].reasons:
                sinks[key].reasons.append(reason)
            if _rank(confidence) > _rank(sinks[key].confidence):
                sinks[key].confidence = confidence
            return
        sinks[key] = FaultSink(sym, role, confidence, [reason])

    for s in table.symbols:
        if s.kind in (VARIABLE, FIELD):
            base = s.type.replace("volatile", "").replace("const", "").split()
            if base and base[-1] in fault_types:
                add_sink(s, "store", HIGH, f"declared with fault type '{base[-1]}'")
            elif looks_like_fault_name(s.name):
                add_sink(s, "store", MEDIUM, "name suggests it holds fault state")
        elif s.kind in (FUNCTION, METHOD, PROTOTYPE) and s.parameters:
            if any(t.split()[-1:] and t.split()[-1] in fault_types for t, _ in s.parameters):
                add_sink(s, "clearer" if _is_clear_name(s.name) else "reporter", HIGH, "takes a fault-code parameter")
            elif looks_like_fault_name(s.name):
                add_sink(s, "clearer" if _is_clear_name(s.name) else "reporter", MEDIUM, "name suggests it raises/clears faults")

    # ---- 3. scan code for usages; infer more constants and sinks from them
    usages: list[FaultUsage] = []
    for _ in range(2):
        usages = []
        sink_names = {s.symbol.name: s for s in sinks.values()}
        for fn in _function_bodies(table):
            pf = by_path.get(fn.file)
            if pf is None:
                continue
            lines = pf.text.splitlines()
            _scan(fn.node, fn, lines, codes, sink_names, constants, usages, table, add_sink)
        # Constants used with a sink but not named like faults -> inferred codes.
        for u in usages:
            if u.code and u.code not in codes and u.code in constants:
                sym = next(iter(s for s in table.named(u.code) if s.kind in (MACRO, ENUMERATOR)), None)
                if sym is not None:
                    codes[u.code] = FaultCode(
                        u.code, constants[u.code], _code_kind(sym), sym, LOW, [f"used with fault {('function' if u.action == 'report' else 'variable')} '{u.target}'"]
                    )

    sinks_by_name = {s.symbol.name: s for s in sinks.values()}
    for u in usages:
        if u.code in codes:
            codes[u.code].usages.append(u)
        sink = sinks_by_name.get(u.target)
        if sink is not None:
            sink.usages.append(u)

    report = FaultReport(
        codes=sorted(codes.values(), key=lambda c: (-_rank(c.confidence), c.kind, c.value if c.value is not None else 1 << 40, c.name)),
        sinks=sorted(sinks.values(), key=lambda s: (-_rank(s.confidence), s.role, s.symbol.name)),
        usages=sorted(usages, key=lambda u: (str(u.file), u.line)),
    )
    return report


def _code_kind(sym: Symbol) -> str:
    toks = name_tokens(sym.name)
    v = sym.int_value
    if any(t in ("bit", "bits", "mask", "msk", "flag", "flags") for t in toks):
        return "bit"
    if sym.value and "<<" in sym.value and v and v & (v - 1) == 0:
        return "bit"
    return "code"


def _rank(conf: str) -> int:
    return {LOW: 0, MEDIUM: 1, HIGH: 2}[conf]


def _scan(root, fn, lines, codes, sinks, constants, out, table, add_sink) -> None:
    stack = [root]
    while stack:
        node = stack.pop()
        t = node.type
        if t == "assignment_expression":
            _assignment(node, fn, lines, codes, sinks, constants, out, table, add_sink)
        elif t == "call_expression":
            _call(node, fn, lines, codes, sinks, constants, out, table, add_sink)
        elif t == "binary_expression":
            op = node.child_by_field_name("operator")
            if op is not None and node_text(op) in ("==", "!=", "&"):
                ids = _identifiers(node)
                target = next((i for i in ids if i in sinks), None)
                if target:
                    for code in dict.fromkeys(i for i in ids if i in codes):
                        out.append(_usage(fn, node, lines, "check", target, code, constants.get(code)))
        elif t == "case_statement":
            value = node.child_by_field_name("value")
            if value is not None:
                code = next((i for i in _identifiers(value) if i in codes), None)
                if code:
                    out.append(_usage(fn, node, lines, "check", "switch", code, constants.get(code)))
        stack.extend(reversed(node.children))


def _assignment(node, fn, lines, codes, sinks, constants, out, table, add_sink) -> None:
    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
    op_node = node.child_by_field_name("operator")
    op = node_text(op_node) if op_node is not None else "="
    if left is None or right is None:
        return
    target = _target_name(left)
    rhs_ids = _identifiers(right)
    code = next((i for i in rhs_ids if i in codes), None)
    if code is None and len(rhs_ids) == 1 and rhs_ids[0] in constants and target in sinks:
        code = rhs_ids[0]  # a named constant written into a fault store
    rhs_text = node_text(right).strip()

    if target not in sinks and code is not None:
        # A fault constant written into a variable makes it a fault store.
        sym = next((s for s in table.named(target) if s.kind in (VARIABLE, FIELD)), None)
        if sym is not None:
            add_sink(sym, "store", MEDIUM, f"assigned fault constant '{code}'")
            sinks[target] = True  # visible to the rest of this pass
    if target not in sinks:
        return

    if op == "&=" or rhs_text.startswith("~") or (op == "=" and code and _is_none_name(code)):
        action = "clear"
    elif op == "=" and code is None and _literal_value(right) == 0:
        action = "clear"
    else:
        action = "set"
    value = constants.get(code) if code else _literal_value(right)
    if code is None and rhs_ids:
        # Copy from a parameter / other variable: record it without a value.
        out.append(_usage(fn, node, lines, action, target, None, None))
        return
    out.append(_usage(fn, node, lines, action, target, code, value))


def _call(node, fn, lines, codes, sinks, constants, out, table, add_sink) -> None:
    callee = _callee_name(node)
    args = node.child_by_field_name("arguments")
    if args is None:
        return
    arg_nodes = list(args.named_children)
    codes_in_args = [i for a in arg_nodes for i in _identifiers(a) if i in codes]
    if not codes_in_args and callee in sinks:
        # Named constants passed to a known fault function are codes too.
        codes_in_args = [i for a in arg_nodes for i in _identifiers(a) if i in constants]
    if callee not in sinks and codes_in_args:
        sym = next((s for s in table.named(callee) if s.is_callable), None)
        if sym is not None:
            role = "clearer" if _is_clear_name(callee) else "reporter"
            add_sink(sym, role, MEDIUM, f"called with fault constant '{codes_in_args[0]}'")
            sinks[callee] = True
    if callee not in sinks:
        return
    action = "clear" if _is_clear_name(callee) else "report"
    if codes_in_args:
        for code in codes_in_args:
            out.append(_usage(fn, node, lines, action, callee, code, constants.get(code)))
    else:
        lit = next((v for v in (_literal_value(a) for a in arg_nodes) if v is not None), None)
        out.append(_usage(fn, node, lines, action, callee, None, lit))


def _usage(fn: Symbol, node: Node, lines: list[str], action: str, target: str, code: str | None, value: int | None) -> FaultUsage:
    row = node.start_point[0]
    text = lines[row].strip() if row < len(lines) else node_text(node)
    return FaultUsage(fn.file, row + 1, fn.qualified_name, action, target, code, value, text)
