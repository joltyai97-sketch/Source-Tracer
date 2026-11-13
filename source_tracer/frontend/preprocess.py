"""Run the C preprocessor (pcpp) on a translation unit, keeping a map back to original lines."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from pcpp import Action, OutputDirective, Preprocessor

from source_tracer.frontend.dialect import blank_keywords
from source_tracer.frontend.parser import read_source
from source_tracer.profile import Profile

HEADERS_DIR = Path(__file__).resolve().parent.parent / "headers"
_LINE_DIRECTIVE = re.compile(r'^\s*#\s*line\s+(\d+)(?:\s+"(.*)")?')


@dataclass
class PreprocessedUnit:
    path: Path
    text: str  # expanded text with #line directives replaced by blank lines
    line_map: list[tuple[Path, int]]  # output row (0-based) -> (original file, 1-based line)
    missing_includes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    macros: dict[str, int] = field(default_factory=dict)  # object-like macros with integer values

    def origin(self, row: int) -> tuple[Path, int]:
        if 0 <= row < len(self.line_map):
            return self.line_map[row]
        return self.path, row + 1


class _PP(Preprocessor):
    def __init__(self, keywords: list[str]):
        super().__init__()
        self.keywords = keywords
        self.missing: list[str] = []
        self.errors: list[str] = []
        self.line_directive = "#line"
        self.compress = 0

    def on_include_not_found(self, is_malformed, is_system_include, curdir, includepath):
        self.missing.append(includepath)
        raise OutputDirective(Action.IgnoreAndRemove)

    def on_file_open(self, is_system_include, includepath):
        text = read_source(Path(includepath))
        blanked, _ = blank_keywords(text, self.keywords)
        return io.StringIO(blanked)

    def on_error(self, file, line, msg):
        self.errors.append(f"{Path(file).name}:{line}: {msg}")
        self.return_code += 1

    def on_unknown_macro_in_expr(self, ident):
        return 0  # standard C: unknown identifiers in #if are 0

    def on_unknown_macro_function_in_expr(self, ident):
        return lambda *args: 0

    def on_directive_unknown(self, directive, toks, ifpassthru, precedingtoks):
        # #pragma, #error-less vendor directives, #asm ... drop quietly.
        raise OutputDirective(Action.IgnoreAndRemove)


def preprocess(path: Path, profile: Profile, include_dirs: list[Path], defines: dict[str, str] | None = None) -> PreprocessedUnit:
    pp = _PP(profile.compiler_keywords)
    int32 = "int" if profile.type_sizes.get("int", 4) >= 4 else "long"
    pp.define(f"__ST_INT32 {int32}")
    pp.define("__SOURCE_TRACER__ 1")
    if path.suffix.lower() not in (".c", ".h"):
        pp.define("__cplusplus 199711L")
    for k, v in (defines or {}).items():
        pp.define(f"{k} {v}".strip())
    for d in include_dirs:
        pp.add_path(str(d))
    pp.add_path(str(HEADERS_DIR))

    text = read_source(path)
    blanked, _ = blank_keywords(text, profile.compiler_keywords)
    pp.parse(blanked, str(path))
    out = io.StringIO()
    pp.write(out)

    lines_out: list[str] = []
    line_map: list[tuple[Path, int]] = []
    cur_file, cur_line = path, 1
    for raw in out.getvalue().split("\n"):
        m = _LINE_DIRECTIVE.match(raw)
        if m:
            cur_line = int(m.group(1))
            if m.group(2):
                cur_file = Path(m.group(2)).resolve()
            lines_out.append("")
            line_map.append((cur_file, cur_line))
            continue
        lines_out.append(raw)
        line_map.append((cur_file, cur_line))
        cur_line += 1
    return PreprocessedUnit(path, "\n".join(lines_out), line_map, pp.missing, pp.errors, _macro_values(pp))


def _macro_values(pp: Preprocessor) -> dict[str, int]:
    from source_tracer.frontend.consteval import evaluate

    bodies = {}
    for name, macro in pp.macros.items():
        if getattr(macro, "arglist", None) is None and not name.startswith("__"):
            bodies[name] = "".join(tok.value for tok in macro.value)
    values: dict[str, int] = {}
    for _ in range(3):  # macros referring to other macros
        for name, body in bodies.items():
            if name not in values:
                v = evaluate(body, values)
                if v is not None:
                    values[name] = v
    return values
