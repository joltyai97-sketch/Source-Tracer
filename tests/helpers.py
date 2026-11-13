from textwrap import dedent

from source_tracer.analysis.faults import analyze_faults
from source_tracer.frontend.parser import parse_text
from source_tracer.frontend.symbols import extract_symbols
from source_tracer.profile import load_profile


def symbols_of(code: str, profile: str = "mc68332", filename: str = "test.c"):
    prof = load_profile(profile)
    pf = parse_text(dedent(code), filename, prof.compiler_keywords)
    return pf, extract_symbols([pf], prof)


def faults_of(code: str, profile: str = "generic"):
    pf, table = symbols_of(code, profile)
    return table, analyze_faults([pf], table)


def one(table, name, kind=None):
    found = [s for s in table.symbols if s.name == name and (kind is None or s.kind == kind)]
    assert len(found) == 1, f"expected one {name!r} ({kind}), got {found}"
    return found[0]
