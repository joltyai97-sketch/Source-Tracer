"""Plain-English HTML descriptions of symbols, for non-software engineers.

Links use the form ``goto:<file>|<line>`` so the GUI can jump to code.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from source_tracer.analysis.faults import FaultCode, FaultSink, FaultUsage
from source_tracer.frontend import symbols as K
from source_tracer.frontend.symbols import Symbol
from source_tracer.project import Project

KIND_LABEL = {
    K.FUNCTION: "Function",
    K.ISR: "Interrupt handler",
    K.METHOD: "Method",
    K.PROTOTYPE: "External function (no code in project)",
    K.CLASS: "Class",
    K.STRUCT: "Structure",
    K.UNION: "Union",
    K.ENUM: "Enumeration (list of named values)",
    K.ENUMERATOR: "Named value",
    K.TYPEDEF: "Type name",
    K.VARIABLE: "Variable",
    K.FIELD: "Field",
    K.MACRO: "Constant (#define)",
    K.FUNCTION_MACRO: "Macro",
    K.REGISTER: "Hardware register",
    K.REGISTER_BLOCK: "Hardware register block",
}

ACTION_LABEL = {"set": "sets", "report": "raises", "clear": "clears", "check": "checks"}


def fmt_value(v: int | None) -> str:
    if v is None:
        return "?"
    return f"{v} (0x{v:X})" if v > 9 else str(v)


def link(file: Path, line: int, label: str | None = None) -> str:
    return f'<a href="goto:{escape(str(file))}|{line}">{escape(label or f"{file.name}:{line}")}</a>'


def _h(title: str, subtitle: str) -> str:
    return f"<h2 style='margin-bottom:2px'>{escape(title)}</h2><div style='color:#8b9099'>{escape(subtitle)}</div>"


def _row(label: str, value_html: str) -> str:
    return f"<tr><td style='color:#8b9099;padding-right:12px;vertical-align:top'>{escape(label)}</td><td>{value_html}</td></tr>"


def _table(rows: list[str]) -> str:
    return "<table cellspacing='0' cellpadding='3'>" + "".join(rows) + "</table>" if rows else ""


def _usages(usages: list[FaultUsage], limit: int = 40) -> str:
    if not usages:
        return "<p><i>Not used anywhere in the code.</i></p>"
    items = []
    for u in usages[:limit]:
        where = f" in <b>{escape(u.function)}()</b>" if u.function else ""
        items.append(f"<li>{link(u.file, u.line)}{where} {ACTION_LABEL.get(u.action, u.action)}: <code>{escape(u.text)}</code></li>")
    more = f"<li>... and {len(usages) - limit} more</li>" if len(usages) > limit else ""
    return "<ul style='margin-left:-20px'>" + "".join(items) + more + "</ul>"


def describe_symbol(sym: Symbol, project: Project) -> str:
    rows = [_row("Defined in", link(sym.file, sym.line))]
    body = ""
    k = sym.kind
    if k in (K.FUNCTION, K.ISR, K.METHOD, K.PROTOTYPE):
        rows.append(_row("Declaration", f"<code>{escape(sym.signature)}</code>"))
        if sym.parameters:
            rows.append(_row("Inputs", "<br>".join(f"<code>{escape(n or '(unnamed)')}</code> &mdash; {escape(t)}" for t, n in sym.parameters)))
        else:
            rows.append(_row("Inputs", "none"))
        ret = sym.type.replace("static", "").replace("inline", "").strip()
        rows.append(_row("Returns", "nothing" if ret in ("void", "") else escape(ret)))
        if k == K.ISR:
            body += "<p>Runs automatically when its hardware interrupt fires, not when called by other code.</p>"
        if k == K.PROTOTYPE:
            body += "<p>Its code is not in the project (it may be in assembly or a pre-built library). When running scenarios it will be replaced by a stub whose result you choose.</p>"
        if "K&R" in sym.storage:
            body += "<p style='color:#8b9099'>Written in old-style (K&amp;R) C.</p>"
        sink = _sink_for(sym, project)
        if sink:
            body += f"<h3>Fault {escape(sink.role)}</h3><p>{escape('; '.join(sink.reasons))}.</p>" + _usages(sink.usages)
        callers = _call_sites(sym, project)
        if callers:
            body += "<h3>Called from</h3><ul style='margin-left:-20px'>" + "".join(callers) + "</ul>"
    elif k in (K.VARIABLE, K.FIELD):
        rows.append(_row("Type", escape(sym.type)))
        if sym.value is not None:
            rows.append(_row("Starts as", f"<code>{escape(sym.value)}</code>" + (f" = {fmt_value(sym.int_value)}" if sym.int_value is not None and sym.value.strip() != str(sym.int_value) else "")))
        scope = "only inside its file" if "static" in sym.storage and k == K.VARIABLE else ("everywhere" if k == K.VARIABLE else f"a part of {sym.parent}")
        rows.append(_row("Visible", scope))
        if "volatile" in sym.type:
            body += "<p>Marked <i>volatile</i>: it may be changed by an interrupt or hardware at any time.</p>"
        sink = _sink_for(sym, project)
        if sink:
            body += f"<h3>Holds fault state</h3><p>{escape('; '.join(sink.reasons))}.</p>" + _usages(sink.usages)
    elif k in (K.REGISTER, K.REGISTER_BLOCK):
        rows.append(_row("Address", f"0x{sym.address:06X}"))
        rows.append(_row("Defined as", f"<code>{escape(sym.value or '')}</code>"))
        reg = sym.register
        if reg is not None:
            rows.append(_row("Chip register", f"{escape(reg.name)} ({escape(reg.module)})"))
            rows.append(_row("Purpose", escape(reg.description)))
            if reg.role == "tx":
                body += f"<p><b>Writing here sends data</b> on the <b>{escape(reg.channel)}</b> channel. These writes will be captured as transmitted data.</p>"
            elif reg.role == "rx":
                body += f"<p>Reading here receives data from the <b>{escape(reg.channel)}</b> channel; values come from the scenario inputs.</p>"
            elif reg.role == "status":
                body += f"<p>Status register: reads return 0x{reg.default:X} by default (\"ready\") unless the scenario overrides it.</p>"
        else:
            body += f"<p>This address is not in the {escape(project.profile.name)} register map.</p>"
    elif k in (K.MACRO, K.ENUMERATOR):
        rows.append(_row("Value", fmt_value(sym.int_value) if sym.int_value is not None else f"<code>{escape(sym.value or '')}</code>"))
        if sym.value and sym.int_value is not None and sym.value.strip() not in (str(sym.int_value), f"0x{sym.int_value:X}", f"0x{sym.int_value:x}"):
            rows.append(_row("Written as", f"<code>{escape(sym.value)}</code>"))
        if sym.parent:
            rows.append(_row("Part of", escape(sym.parent)))
        code = project.faults.code(sym.name)
        if code:
            return describe_fault(code, project)
    elif k in (K.ENUM, K.STRUCT, K.CLASS, K.UNION):
        if sym.children:
            items = []
            for c in sym.children:
                if c.kind == K.ENUMERATOR:
                    items.append(f"<li><code>{escape(c.name)}</code> = {fmt_value(c.int_value)}</li>")
                else:
                    items.append(f"<li><code>{escape(c.name)}</code> &mdash; {escape(c.signature or c.type)}</li>")
            body += "<h3>Contains</h3><ul style='margin-left:-20px'>" + "".join(items) + "</ul>"
    elif k == K.TYPEDEF:
        rows.append(_row("Same as", escape(sym.type)))
    elif k == K.FUNCTION_MACRO:
        rows.append(_row("Form", f"<code>{escape(sym.signature)}</code>"))
        rows.append(_row("Expands to", f"<code>{escape(sym.value or '')}</code>"))
    if sym.conditional:
        body += "<p style='color:#e5b567'>Inside an #if / #ifdef block: it only exists in some build configurations.</p>"
    return _h(sym.qualified_name, KIND_LABEL.get(k, k)) + _table(rows) + body


def describe_fault(code: FaultCode, project: Project) -> str:
    rows = [
        _row("Value", fmt_value(code.value)),
        _row("Kind", "flag bit / mask" if code.kind == "bit" else "fault code"),
        _row("Confidence", escape(code.confidence)),
        _row("Why", escape("; ".join(code.reasons))),
    ]
    if code.symbol is not None:
        rows.insert(0, _row("Defined in", link(code.symbol.file, code.symbol.line)))
    raised = [u for u in code.usages if u.action in ("set", "report")]
    cleared = [u for u in code.usages if u.action == "clear"]
    checked = [u for u in code.usages if u.action == "check"]
    body = "<h3>Raised at</h3>" + (_usages(raised) if raised else "<p><i>Never raised in this code.</i></p>")
    if cleared:
        body += "<h3>Cleared at</h3>" + _usages(cleared)
    if checked:
        body += "<h3>Checked at</h3>" + _usages(checked)
    body += "<p style='color:#8b9099'>Coming next: the exact conditions needed to raise this fault, and scenario runs that show when it is raised and what data is sent.</p>"
    return _h(code.name, "Likely fault code") + _table(rows) + body


def describe_sink(sink: FaultSink, project: Project) -> str:
    return describe_symbol(sink.symbol, project)


def _sink_for(sym: Symbol, project: Project) -> FaultSink | None:
    return next((s for s in project.faults.sinks if s.symbol is sym), None)


def _call_sites(sym: Symbol, project: Project, limit: int = 30) -> list[str]:
    """Find calls to ``sym`` by scanning function bodies (cheap text-free tree walk)."""
    from source_tracer.frontend.parser import node_text

    out: list[str] = []
    for fn in project.symbols.of_kind(K.FUNCTION, K.ISR, K.METHOD):
        if fn.node is None:
            continue
        stack = [fn.node]
        while stack and len(out) < limit:
            n = stack.pop()
            if n.type == "call_expression":
                f = n.child_by_field_name("function")
                name = node_text(f) if f is not None else ""
                if name.rpartition("::")[2].rpartition(".")[2].rpartition("->")[2] == sym.name:
                    out.append(f"<li>{link(fn.file, n.start_point[0] + 1)} in <b>{escape(fn.qualified_name)}()</b></li>")
            stack.extend(n.children)
    return out
