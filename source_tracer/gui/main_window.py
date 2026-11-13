"""Main application window: project browser, source tabs, details, faults and warnings."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import QAction, QBrush, QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from source_tracer import __version__
from source_tracer.analysis.faults import FaultCode, FaultSink
from source_tracer.frontend import symbols as K
from source_tracer.frontend.symbols import Symbol
from source_tracer.gui import describe
from source_tracer.gui.code_view import CodeView
from source_tracer.gui.theme import ERROR, OK, TEXT_DIM, WARN
from source_tracer.profile import available_profiles, load_profile
from source_tracer.project import Project, load_project

ROLE_PAYLOAD = Qt.UserRole + 1


def _item(text: str, payload=None, tooltip: str = "") -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    if payload is not None:
        it.setData(ROLE_PAYLOAD, payload)
    if tooltip:
        it.setToolTip(tooltip)
    return it


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setAlternatingRowColors(True)
    t.setSortingEnabled(True)
    t.setWordWrap(False)
    t.horizontalHeader().setStretchLastSection(True)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    return t


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Source Tracer")
        self.resize(1500, 920)
        self.project: Project | None = None
        self.paths: list[Path] = []
        self.settings = QSettings("SourceTracer", "SourceTracer")
        self._build_ui()

    # ------------------------------------------------------------------ UI construction
    def _build_ui(self) -> None:
        # Toolbar
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        self.act_open_folder = QAction("Open Folder…", self, shortcut=QKeySequence("Ctrl+Shift+O"), triggered=self.open_folder)
        self.act_open_files = QAction("Open Files…", self, shortcut=QKeySequence.Open, triggered=self.open_files)
        self.act_reload = QAction("Reload", self, shortcut=QKeySequence("F5"), triggered=self.reload)
        for a in (self.act_open_folder, self.act_open_files, self.act_reload):
            tb.addAction(a)
        tb.addSeparator()
        tb.addWidget(QLabel(" Target: "))
        self.profile_combo = QComboBox()
        for name in available_profiles():
            self.profile_combo.addItem(load_profile(name).name, name)
        saved = self.settings.value("profile", "mc68332")
        idx = self.profile_combo.findData(saved)
        self.profile_combo.setCurrentIndex(max(idx, 0))
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        tb.addWidget(self.profile_combo)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.search = QLineEdit(placeholderText="Filter symbols…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(280)
        self.search.textChanged.connect(self._filter_tree)
        tb.addWidget(self.search)
        find = QAction(self)
        find.setShortcut(QKeySequence.Find)
        find.triggered.connect(lambda: self.search.setFocus())
        self.addAction(find)

        # Menu
        m = self.menuBar().addMenu("&File")
        m.addAction(self.act_open_folder)
        m.addAction(self.act_open_files)
        m.addAction(self.act_reload)
        m.addSeparator()
        m.addAction(QAction("Quit", self, shortcut=QKeySequence.Quit, triggered=self.close))
        self.view_menu = self.menuBar().addMenu("&View")
        h = self.menuBar().addMenu("&Help")
        h.addAction(QAction("About", self, triggered=self._about))

        # Centre: welcome page or source tabs
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.tabs.removeTab)
        self.center = QStackedWidget()
        self.center.addWidget(self._welcome_page())
        self.center.addWidget(self.tabs)
        self.setCentralWidget(self.center)

        # Left: project tree
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Value / Where"])
        self.tree.setColumnWidth(0, 230)
        self.tree.setUniformRowHeights(True)
        self.tree.itemSelectionChanged.connect(self._tree_selected)
        self._dock("Project", self.tree, Qt.LeftDockWidgetArea, 380)

        # Right: details
        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.details.anchorClicked.connect(self._anchor_clicked)
        self.details.setHtml(self._hint_html("Select anything in the Project list to see what it is and where it is used."))
        self._dock("Details", self.details, Qt.RightDockWidgetArea, 420)

        # Bottom: faults / fault sites / warnings
        self.fault_table = _table(["Fault code", "Value", "Kind", "Confidence", "Raised at", "Cleared at", "Why it was picked"])
        self.usage_table = _table(["Location", "Function", "Action", "Code", "Value", "Target", "Code line"])
        self.warning_table = _table(["Location", "Type", "Message"])
        for t in (self.fault_table, self.usage_table, self.warning_table):
            t.itemSelectionChanged.connect(lambda t=t: self._table_selected(t))
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.addTab(self.fault_table, "Fault codes")
        self.bottom_tabs.addTab(self.usage_table, "Where faults are set / cleared")
        self.bottom_tabs.addTab(self.warning_table, "Warnings")
        self._dock("Analysis", self.bottom_tabs, Qt.BottomDockWidgetArea, 260)

        self.statusBar().showMessage("Open a firmware folder or files to begin.")

    def _dock(self, title: str, widget: QWidget, area, size: int) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(title)
        dock.setWidget(widget)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetClosable)
        self.addDockWidget(area, dock)
        self.resizeDocks([dock], [size], Qt.Horizontal if area in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea) else Qt.Vertical)
        self.view_menu.addAction(dock.toggleViewAction())
        return dock

    def _welcome_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addStretch(2)
        title = QLabel("Source Tracer")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        hint = QLabel(
            "Load microcontroller firmware to see its functions, hardware registers\n"
            "and likely fault codes, and where each fault is raised."
        )
        hint.setObjectName("hint")
        hint.setAlignment(Qt.AlignCenter)
        btn = QPushButton("Open Firmware Folder…")
        btn.clicked.connect(self.open_folder)
        btn2 = QPushButton("Open Files…")
        btn2.setProperty("flat", True)
        btn2.clicked.connect(self.open_files)
        for wid in (title, hint):
            lay.addWidget(wid)
        lay.addSpacing(16)
        lay.addWidget(btn, alignment=Qt.AlignCenter)
        lay.addWidget(btn2, alignment=Qt.AlignCenter)
        lay.addStretch(3)
        return w

    @staticmethod
    def _hint_html(text: str) -> str:
        return f"<p style='color:{TEXT_DIM}'>{text}</p>"

    # ------------------------------------------------------------------ loading
    def open_folder(self) -> None:
        start = self.settings.value("last_dir", str(Path.home()))
        folder = QFileDialog.getExistingDirectory(self, "Open firmware folder", start)
        if folder:
            self.settings.setValue("last_dir", folder)
            self.load([Path(folder)])

    def open_files(self) -> None:
        start = self.settings.value("last_dir", str(Path.home()))
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Open source files",
            start,
            "C/C++ source (*.c *.h *.cpp *.hpp *.cc *.cxx *.ino *.s *.asm);;All files (*)",
        )
        if files:
            self.settings.setValue("last_dir", str(Path(files[0]).parent))
            self.load([Path(f) for f in files])

    def reload(self) -> None:
        if self.paths:
            self.load(self.paths)

    def _profile_changed(self) -> None:
        self.settings.setValue("profile", self.profile_combo.currentData())
        self.reload()

    def load(self, paths: list[Path]) -> None:
        self.paths = [Path(p) for p in paths]
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.project = load_project(self.paths, self.profile_combo.currentData())
        finally:
            QApplication.restoreOverrideCursor()
        self.tabs.clear()
        self._fill_tree()
        self._fill_tables()
        self.center.setCurrentWidget(self.tabs)
        names = ", ".join(p.name for p in self.paths[:3]) + ("…" if len(self.paths) > 3 else "")
        self.setWindowTitle(f"Source Tracer — {names}")
        self.statusBar().showMessage(f"{self.project.profile.name}: {self.project.summary}")
        self.details.setHtml(self._overview_html())
        main = next((s for s in self.project.symbols.of_kind(K.FUNCTION) if s.name == "main"), None)
        if main is not None:
            self.show_location(main.file, main.line)
        elif self.project.files:
            self.show_location(self.project.files[0].path, 1)

    # ------------------------------------------------------------------ tree
    def _fill_tree(self) -> None:
        p = self.project
        assert p is not None
        self.tree.clear()
        root_font = self.tree.font()
        root_font.setBold(True)

        def group(title: str, tooltip: str = "") -> QTreeWidgetItem:
            it = QTreeWidgetItem(self.tree, [title])
            it.setFont(0, root_font)
            it.setToolTip(0, tooltip)
            it.setFlags(it.flags() & ~Qt.ItemIsSelectable)
            return it

        def leaf(parent: QTreeWidgetItem, name: str, second: str, payload, tooltip: str = "", colour: str | None = None) -> QTreeWidgetItem:
            it = QTreeWidgetItem(parent, [name, second])
            it.setData(0, ROLE_PAYLOAD, payload)
            it.setToolTip(0, tooltip or name)
            it.setToolTip(1, second)
            if colour:
                it.setForeground(0, QBrush(QColor(colour)))
            return it

        def where(s: Symbol) -> str:
            return f"{s.file.name}:{s.line}"

        # Fault codes
        codes = p.faults.codes
        g = group(f"Likely fault codes ({len(codes)})", "Constants that look like fault/error codes. Click one to see where it is raised.")
        for c in codes:
            colour = None if c.set_sites else TEXT_DIM
            leaf(g, c.name, describe.fmt_value(c.value), c, f"{c.name}: {'; '.join(c.reasons)}", colour)
        g.setExpanded(True)
        sinks = p.faults.sinks
        if sinks:
            g = group(f"Fault state & functions ({len(sinks)})", "Variables that hold fault state and functions that raise/clear faults.")
            for s in sinks:
                leaf(g, s.symbol.name, s.role, s, "; ".join(s.reasons))
            g.setExpanded(True)

        syms = p.symbols
        sections = [
            ("Functions", [K.FUNCTION, K.METHOD], "Code the firmware runs."),
            ("Interrupt handlers", [K.ISR], "Run automatically when hardware events happen."),
            ("External functions (no code)", [K.PROTOTYPE], "Declared but implemented elsewhere (assembly / library)."),
            ("Global variables", [K.VARIABLE], "Values the firmware remembers between calls."),
        ]
        for title, kinds, tip in sections:
            items = sorted(syms.of_kind(*kinds), key=lambda s: s.qualified_name.lower())
            if not items:
                continue
            g = group(f"{title} ({len(items)})", tip)
            for s in items:
                second = s.type if s.kind == K.VARIABLE else where(s)
                leaf(g, s.qualified_name, second, s, s.signature or f"{s.type} {s.name}")

        regs = syms.of_kind(K.REGISTER, K.REGISTER_BLOCK)
        if regs:
            g = group(f"Hardware registers ({len(regs)})", "Memory-mapped hardware the code reads and writes.")
            by_module: dict[str, list[Symbol]] = defaultdict(list)
            for r in regs:
                by_module[r.register.module if r.register else "Unknown"].append(r)
            for module in sorted(by_module):
                mg = QTreeWidgetItem(g, [module])
                mg.setFlags(mg.flags() & ~Qt.ItemIsSelectable)
                for r in sorted(by_module[module], key=lambda s: s.address or 0):
                    tip = f"{r.register.name}: {r.register.description}" if r.register else "Not in the register map"
                    colour = ERROR if r.register and r.register.role == "tx" else None
                    leaf(mg, r.name, f"0x{r.address:06X}", r, tip, colour)

        types = sorted(syms.of_kind(K.CLASS, K.STRUCT, K.UNION, K.ENUM, K.TYPEDEF), key=lambda s: s.name.lower())
        if types:
            g = group(f"Types ({len(types)})", "Data structures, enumerations and type names.")
            for s in types:
                it = leaf(g, s.qualified_name, s.kind if s.kind != K.TYPEDEF else s.type, s)
                for c in s.children:
                    leaf(it, c.name, describe.fmt_value(c.int_value) if c.kind == K.ENUMERATOR else (c.type or c.kind), c)

        consts = sorted(syms.of_kind(K.MACRO, K.FUNCTION_MACRO), key=lambda s: s.name.lower())
        consts = [c for c in consts if c.value]  # skip include guards / empty flags
        if consts:
            g = group(f"Constants & macros ({len(consts)})", "#define values used by the code.")
            for s in consts:
                second = describe.fmt_value(s.int_value) if s.int_value is not None else (s.value or "")
                leaf(g, s.name if s.kind == K.MACRO else s.signature.replace("#define ", ""), second, s)

        g = group(f"Files ({len(p.files) + len(p.other_files)})")
        for f in p.files:
            leaf(g, f.path.name, self._rel(f.path), ("file", f.path), str(f.path))
        for f in p.other_files:
            leaf(g, f.name, "assembly (not interpreted)", ("file", f), str(f), TEXT_DIM)
        self.tree.resizeColumnToContents(0)
        self.tree.setColumnWidth(0, min(self.tree.columnWidth(0) + 10, 260))
        self._filter_tree(self.search.text())

    def _rel(self, path: Path) -> str:
        for root in self.project.roots if self.project else []:
            try:
                return str(path.relative_to(root if root.is_dir() else root.parent))
            except ValueError:
                pass
        return str(path)

    def _filter_tree(self, text: str) -> None:
        text = text.strip().lower()

        def visit(item: QTreeWidgetItem) -> bool:
            match = not text or text in item.text(0).lower() or text in item.text(1).lower()
            child_match = False
            for i in range(item.childCount()):
                child_match |= visit(item.child(i))
            visible = match or child_match
            item.setHidden(not visible)
            if text and child_match:
                item.setExpanded(True)
            return visible

        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            any_child = False
            for j in range(top.childCount()):
                any_child |= visit(top.child(j))
            top.setHidden(bool(text) and not any_child)
            if text and any_child:
                top.setExpanded(True)

    def _tree_selected(self) -> None:
        items = self.tree.selectedItems()
        if items:
            self.show_payload(items[0].data(0, ROLE_PAYLOAD))

    # ------------------------------------------------------------------ tables
    def _fill_tables(self) -> None:
        p = self.project
        assert p is not None
        for t in (self.fault_table, self.usage_table, self.warning_table):
            t.setSortingEnabled(False)
            t.setRowCount(0)

        def loc(u) -> str:
            return f"{u.file.name}:{u.line}"

        for c in p.faults.codes:
            r = self.fault_table.rowCount()
            self.fault_table.insertRow(r)
            raised = [u for u in c.usages if u.action in ("set", "report")]
            cleared = [u for u in c.usages if u.action == "clear"]
            cells = [
                _item(c.name, c),
                _item(describe.fmt_value(c.value), c),
                _item("bit/mask" if c.kind == "bit" else "code", c),
                _item(c.confidence, c),
                _item(", ".join(loc(u) for u in raised) or "never raised", c),
                _item(", ".join(loc(u) for u in cleared) or "—", c),
                _item("; ".join(c.reasons), c, "; ".join(c.reasons)),
            ]
            if not raised:
                cells[4].setForeground(QBrush(QColor(TEXT_DIM)))
            for col, cell in enumerate(cells):
                self.fault_table.setItem(r, col, cell)

        action_colour = {"set": WARN, "report": WARN, "clear": OK, "check": TEXT_DIM}
        for u in p.faults.usages:
            r = self.usage_table.rowCount()
            self.usage_table.insertRow(r)
            payload = ("goto", u.file, u.line)
            cells = [
                _item(loc(u), payload),
                _item(u.function, payload),
                _item(describe.ACTION_LABEL.get(u.action, u.action), payload),
                _item(u.code or "(variable)", payload),
                _item(describe.fmt_value(u.value) if u.value is not None else "—", payload),
                _item(u.target, payload),
                _item(u.text, payload, u.text),
            ]
            cells[2].setForeground(QBrush(QColor(action_colour.get(u.action, TEXT_DIM))))
            for col, cell in enumerate(cells):
                self.usage_table.setItem(r, col, cell)

        for w in p.warnings:
            r = self.warning_table.rowCount()
            self.warning_table.insertRow(r)
            payload = ("goto", w.file, w.line or 1) if w.file else None
            location = f"{w.file.name}:{w.line}" if w.file and w.line else (w.file.name if w.file else "")
            cells = [_item(location, payload), _item(w.severity, payload), _item(w.message, payload, w.message)]
            cells[1].setForeground(QBrush(QColor(WARN if w.severity == "warning" else TEXT_DIM)))
            for col, cell in enumerate(cells):
                self.warning_table.setItem(r, col, cell)

        for t in (self.fault_table, self.usage_table, self.warning_table):
            # Keep the analysis order (by confidence / file+line) until the user clicks a header.
            t.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
            t.setSortingEnabled(True)
            t.resizeColumnsToContents()
            for col in range(t.columnCount() - 1):
                t.setColumnWidth(col, min(t.columnWidth(col) + 12, 320))
        self.bottom_tabs.setTabText(0, f"Fault codes ({len(p.faults.codes)})")
        self.bottom_tabs.setTabText(1, f"Where faults are set / cleared ({len(p.faults.usages)})")
        self.bottom_tabs.setTabText(2, f"Warnings ({len(p.warnings)})")

    def _table_selected(self, table: QTableWidget) -> None:
        items = table.selectedItems()
        if items:
            self.show_payload(items[0].data(ROLE_PAYLOAD))

    # ------------------------------------------------------------------ navigation / details
    def show_payload(self, payload) -> None:
        if payload is None or self.project is None:
            return
        if isinstance(payload, FaultCode):
            self.details.setHtml(describe.describe_fault(payload, self.project))
            target = payload.set_sites[0] if payload.set_sites else None
            if target is not None:
                self.show_location(target.file, target.line)
            elif payload.symbol is not None:
                self.show_location(payload.symbol.file, payload.symbol.line)
        elif isinstance(payload, FaultSink):
            self.details.setHtml(describe.describe_sink(payload, self.project))
            self.show_location(payload.symbol.file, payload.symbol.line)
        elif isinstance(payload, Symbol):
            self.details.setHtml(describe.describe_symbol(payload, self.project))
            self.show_location(payload.file, payload.line)
        elif isinstance(payload, tuple) and payload and payload[0] == "goto":
            self.show_location(payload[1], payload[2])
        elif isinstance(payload, tuple) and payload and payload[0] == "file":
            self.show_location(payload[1], 1)

    def show_location(self, path: Path, line: int) -> None:
        view = self._open_tab(path)
        if view is not None:
            view.goto_line(line)

    def _open_tab(self, path: Path) -> CodeView | None:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, CodeView) and w.path == path:
                self.tabs.setCurrentIndex(i)
                return w
        pf = self.project.file(path) if self.project else None
        if pf is not None:
            text = pf.text
        else:
            try:
                text = path.read_text(errors="replace")
            except OSError:
                return None
        regs = {s.name for s in self.project.symbols.of_kind(K.REGISTER, K.REGISTER_BLOCK)} if self.project else set()
        faults = {c.name for c in self.project.faults.codes} if self.project else set()
        view = CodeView(path, text, regs, faults)
        idx = self.tabs.addTab(view, path.name)
        self.tabs.setTabToolTip(idx, str(path))
        self.tabs.setCurrentIndex(idx)
        return view

    def _anchor_clicked(self, url: QUrl) -> None:
        s = url.toString()
        if s.startswith("goto:"):
            file, _, line = s[5:].rpartition("|")
            self.show_location(Path(file), int(line))

    def _overview_html(self) -> str:
        p = self.project
        assert p is not None
        raised = [c for c in p.faults.codes if c.set_sites]
        never = [c for c in p.faults.codes if not c.set_sites]
        isrs = p.symbols.of_kind(K.ISR)
        tx = [s for s in p.symbols.of_kind(K.REGISTER, K.REGISTER_BLOCK) if s.register and s.register.role == "tx"]
        parts = [
            describe._h("Project overview", f"Target: {p.profile.name}"),
            f"<p>{describe.escape(p.summary)}.</p>",
            f"<p><b>{len(raised)}</b> fault codes are raised somewhere in the code"
            + (f"; <b>{len(never)}</b> are defined but never raised." if never else "."),
            "</p>",
        ]
        if tx:
            parts.append(
                "<p><b>Data is sent</b> through: "
                + ", ".join(f"<code>{describe.escape(s.name)}</code> ({describe.escape(s.register.channel)})" for s in tx)
                + ".</p>"
            )
        if isrs:
            parts.append("<p><b>Interrupt handlers:</b> " + ", ".join(describe.link(s.file, s.line, s.name) for s in isrs) + "</p>")
        parts.append(self._hint_html("Pick a fault code on the left or in the table below to see where it is raised."))
        return "".join(parts)

    def _about(self) -> None:
        self.details.setHtml(
            describe._h("Source Tracer", f"Version {__version__}")
            + "<p>Interprets microcontroller firmware so you can follow the code, see which fault codes are raised "
            "in a scenario and what data is sent.</p>"
        )
