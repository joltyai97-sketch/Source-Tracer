from source_tracer.frontend.dialect import blank_keywords


def test_blanks_keyword_preserving_positions():
    src = "interrupt void isr(void) { x++; }\n@interrupt void b(void);"
    out, found = blank_keywords(src, ["interrupt", "@interrupt"])
    assert len(out) == len(src)
    assert out.startswith("          void isr")
    assert "@interrupt" not in out
    assert [(f.keyword, f.line, f.column) for f in found] == [("interrupt", 1, 0), ("@interrupt", 2, 0)]


def test_ignores_comments_strings_and_longer_identifiers():
    src = '/* interrupt */ char *s = "interrupt"; int interrupt_count; // interrupt\n'
    out, found = blank_keywords(src, ["interrupt"])
    assert out == src
    assert found == []


def test_preprocessor_lines_untouched():
    src = "#pragma interrupt\nvoid f(void) {}\n"
    out, found = blank_keywords(src, ["interrupt"])
    assert out == src and found == []
