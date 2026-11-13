"""Run panel: choose a function, set inputs, run it in a background thread."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from source_tracer.analysis.runtime import FaultRunSummary, summarize_faults
from source_tracer.core.events import RunResult
from source_tracer.core.hardware import HardwareInputs
from source_tracer.core.interpreter import Interpreter, RunInputs
from source_tracer.core.program import Program, build_program
from source_tracer.core.types import ArrayType, PointerType, StructType
from source_tracer.gui.theme import ACCENT, TEXT_DIM, WARN
from source_tracer.project import Project


@dataclass
class RunOutcome:
    result: RunResult
    faults: FaultRunSummary
    program: Program


class _Worker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, project: Project, program: Program | None, function: str, inputs: RunInputs):
        super().__init__()
        self.project, self.program, self.function, self.inputs = project, program, function, inputs

    def run(self) -> None:
        try:
            program = self.program or build_program([f.path for f in self.project.files], self.project.profile)
            interp = Interpreter(program, self.inputs)
            result = interp.run_function(self.function)
            self.finished.emit(RunOutcome(result, summarize_faults(result, self.project.faults), program))
        except Exception as exc:  # noqa: BLE001 - report anything to the user rather than crash the GUI
            import traceback

            self.failed.emit(f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc(limit=6)}")


def _ro(text: str, tooltip: str = "") -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    if tooltip:
        it.setToolTip(tooltip)
    return it


def _editable(text: str = "", placeholder_tip: str = "") -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    if placeholder_tip:
        it.setToolTip(placeholder_tip)
    return it


def _input_table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setAlternatingRowColors(True)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.AllEditTriggers)
    t.horizontalHeader().setStretchLastSection(True)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    return t


def _hint_for(ctype) -> str:
    if isinstance(ctype, PointerType):
        if isinstance(ctype.target, StructType):
            return "fields, e.g. id=1, value=0x20"
        return 'list, e.g. 1, 2, 0x10  or  "text"'
    if isinstance(ctype, StructType):
        return "fields, e.g. id=1, value=0x20"
    if isinstance(ctype, ArrayType):
        return "list, e.g. 1, 2, 3"
    return "e.g. 42, 0x1F, -3, 1.5, or a name like FAULT_X"


class RunPanel(QWidget):
    """Emits ``run_finished(RunOutcome)``."""

    run_finished = Signal(object)
    run_started = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project: Project | None = None
        self.program: Program | None = None
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._arg_edits: list[QLineEdit] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)

        top = QFormLayout()
        self.function = QComboBox()
        self.function.setEditable(True)
        self.function.setInsertPolicy(QComboBox.NoInsert)
        self.function.completer().setCompletionMode(QCompleter.PopupCompletion)
        self.function.completer().setFilterMode(Qt.MatchContains)
        self.function.currentIndexChanged.connect(self._function_changed)
        top.addRow("Function to run", self.function)
        self.signature = QLabel("")
        self.signature.setObjectName("hint")
        self.signature.setWordWrap(True)
        top.addRow("", self.signature)
        self.max_steps = QSpinBox()
        self.max_steps.setRange(1_000, 50_000_000)
        self.max_steps.setSingleStep(100_000)
        self.max_steps.setValue(200_000)
        self.max_steps.setToolTip("The run stops after this many statements (protects against endless loops).")
        top.addRow("Step limit", self.max_steps)
        lay.addLayout(top)

        self.tabs = QTabWidget()
        # Arguments
        self.args_page = QWidget()
        self.args_form = QFormLayout(self.args_page)
        self.tabs.addTab(self.args_page, "Arguments")
        # Variables
        var_page = QWidget()
        vl = QVBoxLayout(var_page)
        vl.setContentsMargins(0, 4, 0, 0)
        self.var_filter = QLineEdit(placeholderText="Filter variables…")
        self.var_filter.setClearButtonEnabled(True)
        self.var_filter.textChanged.connect(self._filter_vars)
        vl.addWidget(self.var_filter)
        self.var_table = _input_table(["Variable", "Type", "Starts as (code)", "Your start value"])
        vl.addWidget(self.var_table)
        self.tabs.addTab(var_page, "Variables")
        # Stubs
        self.stub_table = _input_table(["Function (no code)", "Returns", "Values returned (in order, last repeats)"])
        self.tabs.addTab(self.stub_table, "Missing functions")
        # Hardware
        self.hw_table = _input_table(["Register", "Address", "Behaviour", "Value read (fixed, or list in order)"])
        self.tabs.addTab(self.hw_table, "Hardware")
        lay.addWidget(self.tabs, 1)

        buttons = QHBoxLayout()
        self.clear_btn = QPushButton("Clear inputs")
        self.clear_btn.setProperty("flat", True)
        self.clear_btn.clicked.connect(self.clear_inputs)
        self.run_btn = QPushButton("▶  Run")
        self.run_btn.clicked.connect(self.run)
        self.status = QLabel("")
        self.status.setObjectName("hint")
        buttons.addWidget(self.status, 1)
        buttons.addWidget(self.clear_btn)
        buttons.addWidget(self.run_btn)
        lay.addLayout(buttons)
        self.setEnabled(False)

    # ------------------------------------------------------------------ population
    def set_project(self, project: Project) -> None:
        self.project = project
        self.program = None
        self.setEnabled(True)
        self.status.setText("Building program model…")
        try:
            self.program = build_program([f.path for f in project.files], project.profile)
            self.status.setText("")
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Could not prepare the code for running: {exc}")
            return
        prog = self.program
        current = self.function.currentText()
        self.function.blockSignals(True)
        self.function.clear()
        fns = sorted(prog.all_functions(), key=lambda f: (f.qualname != "main", f.qualname.lower()))
        for f in fns:
            self.function.addItem(f.qualname, f)
        self.function.blockSignals(False)
        idx = self.function.findText(current) if current else -1
        self.function.setCurrentIndex(idx if idx >= 0 else 0)
        self._function_changed()
        self._fill_variables()
        self._fill_stubs()
        self._fill_hardware()

    def select_function(self, name: str) -> bool:
        idx = self.function.findText(name)
        if idx < 0:
            idx = next((i for i in range(self.function.count()) if self.function.itemText(i).endswith("::" + name)), -1)
        if idx >= 0:
            self.function.setCurrentIndex(idx)
            return True
        return False

    def _function_changed(self) -> None:
        fn = self.function.currentData()
        while self.args_form.rowCount():
            self.args_form.removeRow(0)
        self._arg_edits = []
        if fn is None:
            self.signature.setText("")
            return
        params = ", ".join(f"{t} {n}" for n, t in fn.params)
        self.signature.setText(f"{fn.ret} {fn.qualname}({params})  —  {fn.location[0].name}:{fn.location[1]}")
        if not fn.params:
            note = QLabel("This function takes no arguments. Set its inputs on the Variables / Hardware tabs.")
            note.setObjectName("hint")
            note.setWordWrap(True)
            self.args_form.addRow(note)
        for name, ctype in fn.params:
            edit = QLineEdit(placeholderText=_hint_for(ctype))
            edit.returnPressed.connect(self.run)
            self.args_form.addRow(f"{name}  ({ctype})", edit)
            self._arg_edits.append(edit)

    def _fill_variables(self) -> None:
        prog = self.program
        t = self.var_table
        t.setRowCount(0)
        fault_stores = {s.symbol.name for s in self.project.faults.sinks if s.role == "store"} if self.project else set()
        for g in sorted(prog.globals, key=lambda g: (g.name not in fault_stores, g.name.lower())):
            r = t.rowCount()
            t.insertRow(r)
            init = g.init.text.decode(errors="replace") if g.init is not None else "0"
            name_item = _ro(g.name, f"{g.location[0].name}:{g.location[1]}" + ("  (holds fault state)" if g.name in fault_stores else ""))
            if g.name in fault_stores:
                name_item.setForeground(QBrush(QColor(WARN)))
            t.setItem(r, 0, name_item)
            t.setItem(r, 1, _ro(str(g.ctype)))
            t.setItem(r, 2, _ro(" ".join(init.split())[:60]))
            t.setItem(r, 3, _editable("", _hint_for(g.ctype)))
        t.resizeColumnsToContents()
        t.setColumnWidth(3, 160)

    def _fill_stubs(self) -> None:
        prog = self.program
        t = self.stub_table
        t.setRowCount(0)
        defined = {f.name for f in prog.all_functions()}
        names = sorted(n for n in prog.prototypes if n not in defined)
        for n in names:
            p = prog.prototypes[n]
            r = t.rowCount()
            t.insertRow(r)
            t.setItem(r, 0, _ro(n, f"{p.ret} {n}({', '.join(str(tp) for _, tp in p.params)})"))
            t.setItem(r, 1, _ro(str(p.ret)))
            t.setItem(r, 2, _editable("", "e.g. 0   or   100, 200, 300 (one per call)"))
        t.resizeColumnsToContents()
        if not names:
            t.setRowCount(0)

    def _fill_hardware(self) -> None:
        t = self.hw_table
        t.setRowCount(0)
        prof = self.project.profile
        used = {s.register.name for s in self.project.symbols.symbols if getattr(s, "register", None) is not None}
        regs = [r for r in prof.registers if r.role in ("status", "rx", "tx") or r.name in used]
        regs.sort(key=lambda r: (r.name not in used, r.role not in ("status", "rx", "tx"), r.address))
        for reg in regs:
            row = t.rowCount()
            t.insertRow(row)
            behaviour = {
                "status": f"status: reads 0x{reg.default:X} unless set",
                "rx": f"receive ({reg.channel})",
                "tx": f"transmit ({reg.channel})" + (" / receive" if reg.rx_channel else ""),
                "plain": "memory",
            }[reg.role]
            item = _ro(reg.name, reg.description)
            if reg.name in used:
                item.setForeground(QBrush(QColor(ACCENT)))
            t.setItem(row, 0, item)
            t.setItem(row, 1, _ro(f"0x{reg.address:06X}"))
            t.setItem(row, 2, _ro(behaviour, reg.description))
            t.setItem(row, 3, _editable("", "fixed value (e.g. 0x0180) or a list read in order (e.g. 0x41, 0x42)"))
        t.resizeColumnsToContents()

    def _filter_vars(self, text: str) -> None:
        text = text.lower().strip()
        for r in range(self.var_table.rowCount()):
            name = self.var_table.item(r, 0).text().lower()
            self.var_table.setRowHidden(r, bool(text) and text not in name)

    def clear_inputs(self) -> None:
        for e in self._arg_edits:
            e.clear()
        for table, col in ((self.var_table, 3), (self.stub_table, 2), (self.hw_table, 3)):
            for r in range(table.rowCount()):
                it = table.item(r, col)
                if it is not None:
                    it.setText("")

    # ------------------------------------------------------------------ running
    def collect_inputs(self) -> RunInputs:
        inputs = RunInputs(max_steps=self.max_steps.value())
        inputs.args = [e.text() for e in self._arg_edits]
        for r in range(self.var_table.rowCount()):
            v = self.var_table.item(r, 3).text().strip()
            if v:
                inputs.globals[self.var_table.item(r, 0).text()] = v
        for r in range(self.stub_table.rowCount()):
            v = self.stub_table.item(r, 2).text().strip()
            if v:
                inputs.stubs[self.stub_table.item(r, 0).text()] = [p.strip() for p in v.split(",") if p.strip()]
        hw = HardwareInputs()
        for r in range(self.hw_table.rowCount()):
            v = self.hw_table.item(r, 3).text().strip()
            if not v:
                continue
            name = self.hw_table.item(r, 0).text()
            parts = [p.strip() for p in v.replace(";", ",").split(",") if p.strip()]
            values = [int(p, 0) for p in parts]
            if len(values) == 1 and "rx" not in self.hw_table.item(r, 2).text() and "receive" not in self.hw_table.item(r, 2).text():
                hw.values[name] = values[0]
            else:
                hw.queues[name] = values
        inputs.hardware = hw
        return inputs

    def run(self) -> None:
        if self.project is None or self._thread is not None:
            return
        fn = self.function.currentData()
        if fn is None:
            self.status.setText("Pick a function to run.")
            return
        try:
            inputs = self.collect_inputs()
        except ValueError as exc:
            self.status.setText(f"Input not understood: {exc}")
            return
        self.run_btn.setEnabled(False)
        self.status.setText(f"Running {fn.qualname}…")
        self.run_started.emit(fn.qualname)
        self._thread = QThread(self)
        self._thread.setStackSize(256 * 1024 * 1024)  # deep interpreted recursion
        self._worker = _Worker(self.project, self.program, fn.qualname, inputs)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._done)
        self._worker.failed.connect(self._failed)
        self._thread.start()

    def _cleanup(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None
        self.run_btn.setEnabled(True)

    def _done(self, outcome: RunOutcome) -> None:
        self._cleanup()
        r = outcome.result
        colour = TEXT_DIM if r.ok else WARN
        self.status.setText(f"<span style='color:{colour}'>{r.message}</span>")
        self.run_finished.emit(outcome)

    def _failed(self, message: str) -> None:
        self._cleanup()
        self.status.setText("Internal error while running — see the Run summary tab.")
        self.run_finished.emit(message)

    def wait(self) -> None:
        """Block until a run in progress finishes (used by tests)."""
        if self._thread is not None:
            self._thread.wait()
