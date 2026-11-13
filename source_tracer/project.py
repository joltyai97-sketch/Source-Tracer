"""A loaded firmware project: files, symbols, fault candidates and warnings."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from source_tracer.analysis.faults import FaultReport, analyze_faults
from source_tracer.frontend.parser import (
    ASM_EXTENSIONS,
    SOURCE_EXTENSIONS,
    ParsedFile,
    parse_file,
)
from source_tracer.frontend.symbols import (
    FUNCTION,
    ISR,
    PROTOTYPE,
    REGISTER,
    REGISTER_BLOCK,
    SymbolTable,
    extract_symbols,
)
from source_tracer.profile import Profile, load_profile

_SKIP_DIRS = {".git", ".svn", ".hg", "build", "Debug", "Release", "__pycache__", ".venv", "node_modules"}
MAX_FILES = 5000


@dataclass
class Warning:
    file: Path | None
    line: int | None
    message: str
    severity: str = "warning"  # "info" | "warning"


@dataclass
class Project:
    profile: Profile
    files: list[ParsedFile] = field(default_factory=list)
    other_files: list[Path] = field(default_factory=list)  # assembly etc. (listed, not interpreted)
    symbols: SymbolTable = field(default_factory=SymbolTable)
    faults: FaultReport = field(default_factory=FaultReport)
    warnings: list[Warning] = field(default_factory=list)
    roots: list[Path] = field(default_factory=list)

    def file(self, path: Path) -> ParsedFile | None:
        return next((f for f in self.files if f.path == path), None)

    @property
    def summary(self) -> str:
        n_fn = len(self.symbols.of_kind(FUNCTION, ISR))
        n_reg = len(self.symbols.of_kind(REGISTER, REGISTER_BLOCK))
        return (
            f"{len(self.files)} source files, {n_fn} functions, {n_reg} hardware registers, "
            f"{len(self.faults.codes)} likely fault codes, {len(self.warnings)} warnings"
        )


def collect_files(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """Expand files/folders into (C/C++ sources, assembly/other recognised files)."""
    sources: list[Path] = []
    others: list[Path] = []
    seen: set[Path] = set()

    def add(p: Path) -> None:
        rp = p.resolve()
        if rp in seen:
            return
        seen.add(rp)
        ext = p.suffix.lower()
        if ext in SOURCE_EXTENSIONS:
            sources.append(rp)
        elif ext in ASM_EXTENSIONS:
            others.append(rp)

    for path in paths:
        path = Path(path)
        if path.is_dir():
            for p in sorted(path.rglob("*")):
                if any(part in _SKIP_DIRS for part in p.relative_to(path).parts[:-1]):
                    continue
                if p.is_file():
                    add(p)
                if len(sources) >= MAX_FILES:
                    break
        elif path.is_file():
            add(path)
    return sources, others


def load_project(paths: list[Path | str], profile: Profile | str = "generic") -> Project:
    if isinstance(profile, str):
        profile = load_profile(profile)
    roots = [Path(p).resolve() for p in paths]
    sources, others = collect_files(roots)
    project = Project(profile=profile, roots=roots, other_files=others)

    for path in sources:
        try:
            project.files.append(parse_file(path, profile.compiler_keywords))
        except OSError as exc:
            project.warnings.append(Warning(path, None, f"Could not read file: {exc}"))

    project.symbols = extract_symbols(project.files, profile)
    project.faults = analyze_faults(project.files, project.symbols)
    _collect_warnings(project)
    return project


def _collect_warnings(project: Project) -> None:
    w = project.warnings
    if not project.files:
        w.append(Warning(None, None, "No C/C++ source files found (.c .h .cpp .hpp .ino ...)."))

    # Parse problems, minus noise from constructs we handle ourselves (K&R definitions).
    knr_headers = {
        (s.file, line)
        for s in project.symbols.of_kind(FUNCTION, ISR)
        if "K&R" in s.storage
        for line in range(s.line, (s.node.start_point[0] + 1) if s.node is not None else s.line + 1)
    }
    for pf in project.files:
        for issue in pf.issues:
            if (pf.path, issue.line) in knr_headers:
                continue
            w.append(Warning(pf.path, issue.line, issue.message))

    for path in project.other_files:
        w.append(
            Warning(path, None, "Assembly file: listed but not interpreted. Functions defined here will be stubbed.", "info")
        )
    for s in project.symbols.of_kind(PROTOTYPE):
        if s.parent is None:
            w.append(
                Warning(
                    s.file,
                    s.line,
                    f"'{s.name}' is declared but its code isn't in the project (library or assembly?). It will be stubbed.",
                    "info",
                )
            )
    for s in project.symbols.of_kind(REGISTER, REGISTER_BLOCK):
        if s.register is None and project.profile.registers:
            w.append(
                Warning(s.file, s.line, f"'{s.name}' accesses address {s.address:#x}, which is not in the {project.profile.name} register map.", "info")
            )
    w.sort(key=lambda x: (str(x.file or ""), x.line or 0))
