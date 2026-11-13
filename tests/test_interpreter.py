import dataclasses

import pytest

from source_tracer.core.events import CallEvent, OutputText, RunWarning, Transmit, VarWrite
from source_tracer.core.hardware import HardwareInputs
from source_tracer.profile import load_profile
from tests.run_helpers import run


def ret(tmp_path, code, func="f", **kw):
    r, _ = run(tmp_path, code, func, **kw)
    assert r.stop_reason == "returned", r.message
    return r.return_value


# ---------------------------------------------------------------- integers


def test_fixed_width_wraparound_and_promotion(tmp_path):
    code = """
    #include <stdint.h>
    int f(void) {
        uint8_t a = 250; a += 10;            /* 4 */
        int8_t  b = -128; b--;               /* 127 */
        uint8_t x = 200, y = 2;
        int big = x * y;                     /* promoted: 400 */
        uint16_t w = 0xFFFF; w++;            /* 0 */
        return a + b + big + w;
    }
    """
    assert ret(tmp_path, code) == 4 + 127 + 400 + 0


def test_signed_unsigned_comparison_and_division(tmp_path):
    code = """
    int f(void) {
        int r = 0;
        if (-1 < 1u) r |= 1;                 /* false in C: -1 becomes UINT_MAX */
        if (-7 / 2 == -3) r |= 2;            /* truncation toward zero */
        if (-7 % 2 == -1) r |= 4;
        if ((0x80u >> 7) == 1 && (1 << 4) == 16) r |= 8;
        if ((unsigned char)0x1FF == 0xFF) r |= 16;
        return r;
    }
    """
    assert ret(tmp_path, code) == 2 | 4 | 8 | 16


def test_sixteen_bit_int_profile(tmp_path):
    prof = load_profile("generic")
    prof = dataclasses.replace(prof, type_sizes={**prof.type_sizes, "int": 2})
    code = "int f(void) { int x = 32767; x = x + 1; return x; }"
    r, _ = run(tmp_path, code, profile=prof)
    assert r.return_value == -32768


def test_floats(tmp_path):
    code = """
    int f(void) { float t = 21.5f; double k = t * 2 + 0.25; return (int)(k * 10); }
    """
    assert ret(tmp_path, code) == 432


# ---------------------------------------------------------------- control flow


def test_loops_switch_and_recursion(tmp_path):
    code = """
    static int fact(int n) { return n <= 1 ? 1 : n * fact(n - 1); }
    int classify(int c) {
        int r = 0;
        switch (c) {
        case 1:
        case 2: r = 10; break;
        case 3: r = 30;            /* falls through */
        default: r += 1;
        }
        return r;
    }
    int f(void) {
        int i, sum = 0, n = 0;
        for (i = 0; i < 10; i++) { if (i == 2) continue; if (i == 6) break; sum += i; }
        do { n++; } while (n < 3);
        while (1) { if (++n > 5) break; }
        return sum * 1000 + n * 100 + classify(2) + classify(3) + classify(9) + fact(5);
    }
    """
    # sum = 0+1+3+4+5 = 13 ; n = 6 ; classify: 10 + 31 + 1 ; fact(5) = 120
    assert ret(tmp_path, code) == 13000 + 600 + 42 + 120


def test_step_limit_stops_infinite_loop(tmp_path):
    r, _ = run(tmp_path, "void f(void) { volatile int x = 0; for (;;) { x++; } }", max_steps=500)
    assert r.stop_reason == "step_limit"
    assert r.steps == 501


# ---------------------------------------------------------------- pointers / arrays / structs


def test_pointers_and_arrays(tmp_path):
    code = """
    static void fill(unsigned char *p, int n) { while (n--) *p++ = (unsigned char)(n + 1); }
    int buf_sum(const unsigned char *b, int n) { int s = 0; int i; for (i = 0; i < n; i++) s += b[i]; return s; }
    int f(void) {
        unsigned char buf[4];
        unsigned char *end = &buf[4];
        int *q; int arr[3] = {5, 6, 7};
        fill(buf, 4);                      /* 4 3 2 1 */
        q = arr + 1;
        return buf_sum(buf, 4) * 100 + (int)(end - buf) * 10 + *q + q[1] - arr[0];
    }
    """
    assert ret(tmp_path, code) == 10 * 100 + 4 * 10 + 6 + 7 - 5


def test_structs_unions_and_byte_views(tmp_path):
    code = """
    #include <stdint.h>
    #include <string.h>
    typedef struct { uint8_t id; uint16_t value; } item_t;
    typedef union { uint32_t word; uint8_t bytes[4]; } word_u;
    struct holder { item_t items[2]; item_t *cur; };
    uint8_t out[4];
    int f(void) {
        struct holder h = { .items = { {1, 0x1234}, {2, 0x5678} } };
        item_t copy;
        word_u u;
        const uint8_t *raw;
        h.cur = &h.items[1];
        h.cur->value += 1;
        copy = h.items[1];                 /* struct copy */
        copy.id = 9;
        u.word = 0x12345678;               /* big-endian target */
        raw = (const uint8_t *)&h.items[0];
        memcpy(out, &h.items[1], sizeof(item_t));
        return (h.items[1].id == 2) + (copy.value == 0x5679) * 2 + (u.bytes[0] == 0x12) * 4
             + (raw[2] == 0x12 && raw[3] == 0x34) * 8 + (sizeof(item_t) == 4) * 16;  /* 68k pads to 2 */
    }
    """
    r, it = run(tmp_path, code)
    assert r.return_value == 31, r.message
    assert r.globals_after["out"] == "[02 00 56 79]"


def test_register_bitfield_struct_overlay(tmp_path):
    code = """
    typedef struct { unsigned short te : 1; unsigned short re : 1; unsigned short : 12; unsigned short mode : 2; } sccr1_t;
    #define SCCR1 (*(volatile sccr1_t *)0xFFFC0A)
    #define SCCR1_RAW (*(volatile unsigned short *)0xFFFC0A)
    int f(void) { SCCR1_RAW = 0; SCCR1.te = 1; SCCR1.mode = 2; return SCCR1_RAW; }
    """
    # big-endian bit allocation: te = bit 15, re = bit 14, mode = bits 1..0
    assert ret(tmp_path, code) == 0x8002


def test_bitfields_and_enums(tmp_path):
    code = """
    typedef enum { MODE_OFF, MODE_RUN = 5, MODE_FAULT } mode_t;
    struct flags { unsigned a : 1; unsigned b : 3; };
    mode_t g_mode = MODE_OFF;
    int f(void) { struct flags fl; fl.a = 3; fl.b = 9; g_mode = MODE_FAULT; return fl.a * 10 + fl.b; }
    """
    r, _ = run(tmp_path, code)
    assert r.return_value == 11
    assert r.globals_after["g_mode"] == "MODE_FAULT (6)"


def test_globals_initialisers_and_static_locals(tmp_path):
    code = """
    int table[] = {10, 20, 30};
    const char *name = "pump";
    int *ptr = &table[2];
    int counter(void) { static int n = 100; return ++n; }
    int f(void) { counter(); return counter() + *ptr + (int)sizeof(table) + name[1]; }
    """
    assert ret(tmp_path, code) == 102 + 30 + 12 + ord("u")


def test_function_pointers(tmp_path):
    code = """
    static int dbl(int x) { return 2 * x; }
    static int sq(int x) { return x * x; }
    typedef int (*op_t)(int);
    op_t ops[2] = { dbl, sq };
    int f(void) { op_t o = &sq; return ops[0](3) + ops[1](4) + o(5) + (*o)(1); }
    """
    assert ret(tmp_path, code) == 6 + 16 + 25 + 1


def test_knr_and_printf(tmp_path):
    code = """
    #include <stdio.h>
    int add(a, b) int a; int b; { return a + b; }
    int f(void) { printf("v=%d hex=%02X s=%s\\n", add(2, 3), 10, "ok"); return add(1, 1); }
    """
    r, _ = run(tmp_path, code)
    assert r.return_value == 2
    assert r.of(OutputText)[0].text == "v=5 hex=0A s=ok\n"


# ---------------------------------------------------------------- hardware, stubs, inputs


HW = """
#define SCSR (*(volatile unsigned short *)0xFFFC0C)
#define SCDR (*(volatile unsigned short *)0xFFFC0E)
#define TDRE 0x0100
void putc_(unsigned char c) { while (!(SCSR & TDRE)) ; SCDR = c; }
void f(void) { const char *s = "OK"; while (*s) putc_(*s++); }
unsigned short rx(void) { return SCDR; }
"""


def test_register_writes_are_transmitted(tmp_path):
    r, _ = run(tmp_path, HW)
    assert r.ok
    tx = r.of(Transmit)
    assert [(t.channel, t.value) for t in tx] == [("SCI", ord("O")), ("SCI", ord("K"))]
    assert tx[0].file.name == "t.c" and tx[0].line == 5


def test_status_override_makes_polling_loop_hit_limit(tmp_path):
    r, _ = run(tmp_path, HW, hardware=HardwareInputs(values={"SCSR": 0}), max_steps=1000)
    assert r.stop_reason == "step_limit"
    assert r.last_location[1] == 5


def test_receive_queue(tmp_path):
    r, _ = run(tmp_path, HW, func="rx", hardware=HardwareInputs(queues={"SCI": [0x41]}))
    assert r.return_value == 0x41


def test_stubs_and_inputs(tmp_path):
    code = """
    unsigned short read_adc(int ch);          /* no body: stubbed */
    extern int g_limit;
    int g_limit = 50;
    int f(int offset) { return read_adc(0) + read_adc(1) + read_adc(2) + offset + g_limit; }
    """
    r, _ = run(tmp_path, code, args=["7"], stubs={"read_adc": [100, 200]}, globals={"g_limit": "0x10"})
    assert r.return_value == 100 + 200 + 200 + 7 + 16
    stub_calls = [e for e in r.of(CallEvent) if e.kind == "stub"]
    assert [e.args for e in stub_calls] == [["0"], ["1"], ["2"]]


def test_pointer_argument_buffers_and_names_in_inputs(tmp_path):
    code = """
    #define MODE_FAST 3
    int sum(const unsigned char *data, int len, int mode) { int s = 0; while (len--) s += *data++; return s * mode; }
    """
    r, _ = run(tmp_path, code, func="sum", args=["1, 2, 0x10", "3", "MODE_FAST"])
    assert r.return_value == 19 * 3


def test_unknown_identifier_warns_and_continues(tmp_path):
    r, _ = run(tmp_path, "int f(void) { undeclared_flag = 4; return undeclared_flag + 1; }")
    assert r.return_value == 5
    assert any("not declared" in w.message for w in r.of(RunWarning))


def test_global_writes_recorded(tmp_path):
    r, _ = run(tmp_path, "int g; int arr[2]; void f(void) { int local = 1; g = 5; arr[1] = local + g; }")
    writes = [(w.name, w.new) for w in r.of(VarWrite)]
    assert writes == [("g", 5), ("arr[1]", 6)]


# ---------------------------------------------------------------- C++


def test_cpp_classes(tmp_path):
    code = """
    class Sensor {
    public:
        Sensor(int offset) : offset_(offset), reads_(0) {}
        virtual int read() { reads_++; return 10 + offset_; }
        int reads() const { return reads_; }
    protected:
        int offset_;
        int reads_;
    };
    class FastSensor : public Sensor {
    public:
        FastSensor() : Sensor(5) {}
        int read() { reads_ += 2; return 100; }
    };
    static void bump(int &x) { x += 3; }
    int f() {
        Sensor s(1);
        FastSensor fs;
        Sensor *p = &fs;
        int v = s.read() + p->read();
        int n = 4;
        bump(n);
        Sensor *h = new Sensor(2);
        return v + n + h->read() + s.reads() * 1000 + fs.reads() * 10000;
    }
    """
    assert ret(tmp_path, code, name="t.cpp") == 11 + 100 + 7 + 12 + 1000 + 20000


def test_cpp_unknown_object_methods_become_stubs(tmp_path):
    r, _ = run(tmp_path, "void f() { Serial.print(42); }", name="t.cpp", stubs={})
    assert r.ok
    assert [e.callee for e in r.of(CallEvent) if e.kind == "stub"] == ["Serial.print"]


def test_event_locations_are_absolute_original_files(tmp_path):
    from tests.run_helpers import program_of
    from source_tracer.core.interpreter import Interpreter, RunInputs

    prog = program_of(tmp_path, '#include "hw.h"\nvoid f(void) { send(1); }\n', extra={"hw.h": "int g;\nstatic void send(int v) {\n  g = v;\n}\n"})
    r = Interpreter(prog, RunInputs()).run_function("f")
    w = r.of(VarWrite)[0]
    assert w.file == (tmp_path / "hw.h").resolve() and w.line == 3
