import pytest

from source_tracer.frontend.consteval import evaluate


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("42", 42),
        ("0x7E", 126),
        ("0x0100u", 256),
        ("010", 8),
        ("0b101", 5),
        ("(1u << 3)", 8),
        ("(0x8000 | (2 << 12))", 0xA000),
        ("((unsigned short)0xFFFC0E)", 0xFFFC0E),
        ("(uint8_t)(300 & 0xFF)", 44),
        ("'A'", 65),
        ("'\\n'", 10),
        ("~0 & 0xFF", 0xFF),
        ("-7 / 2", -3),  # C truncates toward zero
        ("-7 % 2", -1),
        ("BASE + 0x0E", 0xFFFC0E),
        ("(A == 1) && !B", 1),
        ("100L * 3UL", 300),
    ],
)
def test_evaluates(expr, expected):
    assert evaluate(expr, {"BASE": 0xFFFC00, "A": 1, "B": 0}) == expected


@pytest.mark.parametrize("expr", ["", "UNKNOWN + 1", "foo(3)", "\"text\"", "1 / 0", "a ? b : c"])
def test_unknown_returns_none(expr):
    assert evaluate(expr) is None
