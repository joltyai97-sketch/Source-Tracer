from pathlib import Path
from textwrap import dedent

from source_tracer.core.interpreter import Interpreter, RunInputs
from source_tracer.core.program import build_program
from source_tracer.profile import load_profile


def program_of(tmp_path: Path, code: str, name: str = "t.c", profile="mc68332", extra: dict | None = None):
    (tmp_path / name).write_text(dedent(code))
    for fname, text in (extra or {}).items():
        (tmp_path / fname).write_text(dedent(text))
    files = sorted(p for p in tmp_path.iterdir() if p.is_file())
    prof = load_profile(profile) if isinstance(profile, str) else profile
    return build_program(files, prof)


def run(tmp_path: Path, code: str, func: str = "f", name: str = "t.c", profile="mc68332", **inputs):
    prog = program_of(tmp_path, code, name, profile)
    it = Interpreter(prog, RunInputs(**inputs))
    result = it.run_function(func)
    return result, it
