"""Widgets that present a run's results: summary, faults, sent data, variables, event log."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHeaderView,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from source_tracer.analysis.runtime import transmitted_by_channel
from source_tracer.core.events import (
    CallEvent,
    OutputText,
    RegisterAccess,
    ReturnEvent,
    RunWarning,
    Transmit,
    VarWrite,
)
from source_tracer.gui import describe
from source_tracer.gui.theme import ERROR, OK, TEXT_DIM, WARN

ROLE_LOC = Qt.UserRole + 7
MAX_LOG_ROWS = 20_000


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.setWordWrap(False)
    t.horizontalHeader().setStretchLastSection(True)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    return t


def _cell(text: str, loc=None, colour: str | None = None, mono: bool = False) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setToolTip(text)
    if loc is not None:
        it.setData(ROLE_LOC, loc)
    if colour:
        it.setForeground(QBrush(QColor(colour)))
    if mono:
        it.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
    return it


def _loc(e) -> str:
    return f"{e.file.name}:{e.line}" if e.file is not None and e.line else ""


def _fill(table: QTableWidget, rows: list[list[QTableWidgetItem]]) -> None:
    table.setUpdatesEnabled(False)
    table.setRowCount(len(rows))
    for r, cells in enumerate(rows):
        for c, cell in enumerate(cells):
            table.setItem(r, c, cell)
    table.resizeColumnsToContents()
    for c in range(table.columnCount() - 1):
        table.setColumnWidth(c, min(table.columnWidth(c) + 12, 360))
    table.setUpdatesEnabled(True)


class ResultsView:
    """Owns the result widgets; the main window places them in tabs."""

    def __init__(self, goto) -> None:
        self.goto = goto  # callable(path, line)
        self.summary = QTextBrowser()
        self.summary.setOpenLinks(False)
        self.summary.anchorClicked.connect(self._anchor)
        self.summary.setHtml(f"<p style='color:{TEXT_DIM}'>Run a function from the <b>Run</b> panel to see what it does: "
                             "which faults it raises, what data it sends and which variables change.</p>")

        self.faults = _table(["Step", "Location", "Function", "Event", "Fault", "Value", "Via"])

        self.sent = QWidget()
        sl = QVBoxLayout(self.sent)
        sl.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Vertical)
        self.sent_dump = QTextBrowser()
        self.sent_table = _table(["Step", "Location", "Function", "Channel", "Register", "Value", "Char"])
        split.addWidget(self.sent_dump)
        split.addWidget(self.sent_table)
        split.setSizes([90, 200])
        sl.addWidget(split)

        self.vars = QWidget()
        vl = QVBoxLayout(self.vars)
        vl.setContentsMargins(0, 0, 0, 0)
        self.changed_only = QCheckBox("Show only variables that changed")
        self.changed_only.setChecked(True)
        self.changed_only.toggled.connect(self._fill_vars)
        self.var_table = _table(["Variable", "Before", "After"])
        vl.addWidget(self.changed_only)
        vl.addWidget(self.var_table)

        self.log = _table(["Step", "Location", "Function", "What happened"])

        for t in (self.faults, self.sent_table, self.log):
            t.itemSelectionChanged.connect(lambda t=t: self._selected(t))
        self.outcome = None

    # ------------------------------------------------------------------ navigation
    def _selected(self, table: QTableWidget) -> None:
        items = table.selectedItems()
        if items:
            loc = items[0].data(ROLE_LOC)
            if loc:
                self.goto(*loc)

    def _anchor(self, url) -> None:
        s = url.toString()
        if s.startswith("goto:"):
            from pathlib import Path

            file, _, line = s[5:].rpartition("|")
            self.goto(Path(file), int(line))

    # ------------------------------------------------------------------ display
    def show_error(self, message: str) -> None:
        self.summary.setHtml(f"<h2 style='color:{ERROR}'>Internal error</h2><pre>{escape(message)}</pre>"
                             "<p>This is a bug in Source Tracer, not in your code. The other panels are unchanged.</p>")

    def show(self, outcome) -> None:
        self.outcome = outcome
        r, fs = outcome.result, outcome.faults
        self.summary.setHtml(self._summary_html(outcome))

        rows = []
        for e in fs.events:
            loc = (e.file, e.line) if e.file else None
            colour = WARN if e.action == "raised" else OK
            rows.append([
                _cell(str(e.step), loc), _cell(_loc(e), loc), _cell(e.function, loc),
                _cell(e.action, loc, colour), _cell(e.code, loc, colour),
                _cell(describe.fmt_value(e.value) if e.value is not None else "", loc), _cell(e.detail or e.via, loc),
            ])
        _fill(self.faults, rows)

        chans = transmitted_by_channel(r)
        if chans:
            parts = []
            for ch in chans:
                parts.append(f"<p><b>{escape(ch.channel)}</b> — {len(ch.values)} values<br>"
                             f"<code style='font-size:11pt'>{escape(ch.hex)}</code><br>"
                             f"<span style='color:{TEXT_DIM}'>as text: {escape(ch.ascii)}</span></p>")
            self.sent_dump.setHtml("".join(parts))
        else:
            self.sent_dump.setHtml(f"<p style='color:{TEXT_DIM}'>Nothing was sent during this run.</p>")
        rows = []
        for t in r.transmitted:
            loc = (t.file, t.line) if t.file else None
            width = max(t.size, 1) * 2 if t.value > 0xFF else 2
            rows.append([
                _cell(str(t.step), loc), _cell(_loc(t), loc), _cell(t.function, loc), _cell(t.channel, loc),
                _cell(t.register, loc), _cell(f"0x{t.value:0{width}X}", loc, mono=True),
                _cell(chr(t.value) if 32 <= t.value < 127 else "", loc),
            ])
        _fill(self.sent_table, rows)
        self._fill_vars()
        self._fill_log(r)

    def _fill_vars(self) -> None:
        if self.outcome is None:
            return
        r = self.outcome.result
        rows = []
        stores = set(self.outcome.faults.final_state)
        for name in sorted(r.globals_after, key=str.lower):
            before, after = r.globals_before.get(name, ""), r.globals_after[name]
            changed = before != after
            if self.changed_only.isChecked() and not changed:
                continue
            colour = WARN if name in stores and changed else (None if changed else TEXT_DIM)
            rows.append([_cell(name, colour=colour), _cell(before, mono=True), _cell(after, colour=WARN if changed else None, mono=True)])
        _fill(self.var_table, rows)

    def _fill_log(self, r) -> None:
        rows = []
        for e in r.events[:MAX_LOG_ROWS]:
            loc = (e.file, e.line) if e.file else None
            colour = None
            if isinstance(e, CallEvent):
                what = f"{'calls stub' if e.kind == 'stub' else 'calls'} {e.callee}({', '.join(e.args)})"
                colour = TEXT_DIM if e.kind == "stub" else None
            elif isinstance(e, ReturnEvent):
                what = f"{e.callee}() returns {e.value}"
                colour = TEXT_DIM
            elif isinstance(e, VarWrite):
                what = f"{e.name} = {e.text}"
            elif isinstance(e, Transmit):
                what = f"sends 0x{e.value:02X} on {e.channel} ({e.register})"
                colour = ERROR
            elif isinstance(e, RegisterAccess):
                what = f"{e.kind}s register {e.register} = 0x{e.value:X}"
                colour = TEXT_DIM
            elif isinstance(e, OutputText):
                what = f"prints {e.text!r}"
            elif isinstance(e, RunWarning):
                what = f"⚠ {e.message}"
                colour = WARN
            else:
                what = type(e).__name__
            rows.append([_cell(str(e.step), loc), _cell(_loc(e), loc), _cell(e.function, loc), _cell(what, loc, colour)])
        _fill(self.log, rows)

    def _summary_html(self, outcome) -> str:
        r, fs = outcome.result, outcome.faults
        colour = OK if r.ok else WARN
        parts = [f"<h2 style='margin-bottom:2px'>{escape(r.function)}()</h2>",
                 f"<div style='color:{colour}'>{escape(r.message)}</div>"]
        if r.stop_reason == "step_limit" and r.last_location:
            f, line = r.last_location
            parts.append(f"<p>It was at {describe.link(f, line)} when it stopped.</p>")
        if r.return_text:
            parts.append(f"<p><b>Returned:</b> <code>{escape(r.return_text)}</code></p>")

        parts.append("<h3>Faults</h3>")
        if fs.events:
            raised = fs.raised
            if raised:
                parts.append("<p><b>Raised:</b> " + ", ".join(f"<span style='color:{WARN}'>{escape(c)}</span>" for c in raised) + "</p>")
            cleared = list(dict.fromkeys(e.code for e in fs.events if e.action == "cleared"))
            if cleared:
                parts.append("<p><b>Cleared:</b> " + ", ".join(f"<span style='color:{OK}'>{escape(c)}</span>" for c in cleared) + "</p>")
        else:
            parts.append(f"<p style='color:{TEXT_DIM}'>No fault codes were raised or cleared.</p>")
        if fs.final_state:
            parts.append("<p><b>Fault state at the end:</b><br>" + "<br>".join(
                f"<code>{escape(k)}</code> = {escape(v)}" for k, v in fs.final_state.items()) + "</p>")
            parts.append("<p><b>Active faults at the end:</b> " + (", ".join(escape(a) for a in fs.active) if fs.active else "none") + "</p>")

        parts.append("<h3>Data sent</h3>")
        chans = transmitted_by_channel(r)
        if chans:
            for ch in chans:
                shown = ch.hex if len(ch.values) <= 64 else " ".join(ch.hex.split()[:64]) + " …"
                parts.append(f"<p><b>{escape(ch.channel)}</b> ({len(ch.values)} values): <code>{escape(shown)}</code></p>")
        else:
            parts.append(f"<p style='color:{TEXT_DIM}'>Nothing was sent.</p>")
        printed = "".join(e.text for e in r.of(OutputText))
        if printed:
            parts.append(f"<p><b>Printed text:</b></p><pre>{escape(printed[:2000])}</pre>")

        changed = r.changed_globals
        parts.append(f"<h3>Variables</h3><p>{len(changed)} global variable(s) changed.</p>")
        if changed:
            items = list(changed.items())[:12]
            parts.append("<table cellspacing='0' cellpadding='2'>" + "".join(
                f"<tr><td><code>{escape(k)}</code></td><td style='color:{TEXT_DIM}'>{escape(a)}</td><td>&rarr;</td><td>{escape(b)}</td></tr>"
                for k, (a, b) in items) + "</table>")
            if len(changed) > 12:
                parts.append(f"<p style='color:{TEXT_DIM}'>… see the Variables tab for all.</p>")

        stubs = [e for e in r.of(CallEvent) if e.kind == "stub"]
        if stubs:
            names = sorted({e.callee for e in stubs})
            parts.append("<h3>Missing functions called</h3><p>These have no code in the project; each returned the value set on "
                         "the Run panel's <i>Missing functions</i> tab (default 0): " + ", ".join(f"<code>{escape(n)}</code>" for n in names) + "</p>")
        warns = r.of(RunWarning)
        if warns:
            parts.append(f"<h3 style='color:{WARN}'>Warnings</h3><ul style='margin-left:-20px'>" + "".join(
                f"<li>{describe.link(w.file, w.line) + ': ' if w.file and w.line else ''}{escape(w.message)}</li>" for w in warns[:20]) + "</ul>")
        parts.append(f"<p style='color:{TEXT_DIM}'>{r.steps} steps. Lines that ran are tinted in the source view.</p>")
        return "".join(parts)
