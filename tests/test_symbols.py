from source_tracer.frontend import symbols as K
from tests.helpers import one, symbols_of


def test_functions_parameters_and_return_types():
    _, t = symbols_of(
        """
        static unsigned short adc_read(unsigned char channel, int *out) { return 0; }
        void noargs(void) {}
        """
    )
    f = one(t, "adc_read")
    assert f.kind == K.FUNCTION
    assert f.type == "static unsigned short"
    assert f.parameters == [("unsigned char", "channel"), ("int *", "out")]
    assert (f.line, f.end_line) == (2, 2)
    assert one(t, "noargs").parameters == []


def test_knr_definition_is_a_function():
    _, t = symbols_of(
        """
        unsigned char checksum(buf, len)
        unsigned char *buf;
        int len;
        {
            return 0;
        }
        """
    )
    f = one(t, "checksum", K.FUNCTION)
    assert f.parameters == [("unsigned char *", "buf"), ("int", "len")]
    assert "K&R" in f.storage
    assert (f.line, f.end_line) == (2, 7)


def test_isr_detected_by_compiler_keyword_and_name():
    _, t = symbols_of(
        """
        interrupt void pit(void) { }
        void uart_isr(void) { }
        void plain(void) { }
        """
    )
    assert one(t, "pit").kind == K.ISR
    assert one(t, "pit").signature == "interrupt void pit(void)"
    assert one(t, "uart_isr").kind == K.ISR
    assert one(t, "plain").kind == K.FUNCTION


def test_register_macros_in_several_styles():
    _, t = symbols_of(
        """
        typedef volatile unsigned short vu16;
        #define REG16(a) (*(vu16 *)(a))
        #define BASE 0xFFFC00
        #define SCSR (*(volatile unsigned short *)0xFFFC0C)
        #define SCDR REG16(BASE + 0x0E)
        #define QSPI_TR ((vu16 *)0xFFFD20)
        #define CUSTOM (*(volatile unsigned char *)0x200000)
        #define NOT_A_REG (0x10 + 1)
        """
    )
    scsr = one(t, "SCSR")
    assert (scsr.kind, scsr.address, scsr.register.name) == (K.REGISTER, 0xFFFC0C, "SCSR")
    scdr = one(t, "SCDR")
    assert (scdr.kind, scdr.address, scdr.register.role) == (K.REGISTER, 0xFFFC0E, "tx")
    tr = one(t, "QSPI_TR")
    assert (tr.kind, tr.register.name) == (K.REGISTER_BLOCK, "QSPI_TR")
    custom = one(t, "CUSTOM")
    assert custom.kind == K.REGISTER and custom.register is None  # off-chip, not in profile
    assert one(t, "NOT_A_REG").kind == K.MACRO and one(t, "NOT_A_REG").int_value == 17
    assert one(t, "vu16").type == "volatile unsigned short"


def test_enums_count_up_and_use_constants():
    _, t = symbols_of(
        """
        #define BASE_CODE 0x40
        typedef enum { A, B = BASE_CODE, C, D = B + 0x10 } code_t;
        """
    )
    assert [one(t, n).int_value for n in "ABCD"] == [0, 0x40, 0x41, 0x50]
    assert one(t, "code_t", K.ENUM).children[1].name == "B"
    assert not [s for s in t.symbols if s.kind == K.TYPEDEF]  # alias of anonymous enum is folded in


def test_variables_types_and_storage():
    _, t = symbols_of(
        """
        static volatile unsigned long ticks = 0, other;
        unsigned char buf[16];
        const char *name = "x";
        extern int defined_later;
        int defined_later = 3;
        struct point { int x; int y; } origin;
        """
    )
    assert one(t, "ticks").type == "volatile unsigned long"
    assert "static" in one(t, "ticks").storage
    assert one(t, "other").type == "volatile unsigned long"
    assert one(t, "buf").type == "unsigned char [16]"
    assert one(t, "name").type == "const char *"
    assert one(t, "defined_later").int_value == 3  # extern declaration merged away
    assert one(t, "origin").type == "struct point"
    assert [c.name for c in one(t, "point", K.STRUCT).children] == ["x", "y"]


def test_prototypes_without_bodies_are_kept_as_external():
    _, t = symbols_of(
        """
        void has_body(void);
        void in_assembly(void);
        void has_body(void) {}
        """
    )
    assert one(t, "has_body").kind == K.FUNCTION
    assert one(t, "in_assembly").kind == K.PROTOTYPE


def test_include_guard_is_not_conditional_but_ifdef_is():
    _, t = symbols_of(
        """
        #ifndef MY_H
        #define MY_H
        int always;
        #ifdef DEBUG
        void debug_dump(void) {}
        #endif
        #endif
        """
    )
    assert not one(t, "always").conditional
    assert one(t, "debug_dump").conditional


def test_cpp_classes_methods_and_namespaces():
    _, t = symbols_of(
        """
        namespace motor {
        class Pump {
          public:
            Pump(int pin);
            void start();
            int speed() const { return speed_; }
          private:
            int speed_;
        };
        void Pump::start() { speed_ = 100; }
        }
        """,
        filename="pump.cpp",
    )
    cls = one(t, "Pump", K.CLASS)
    assert cls.qualified_name == "motor::Pump"
    start = one(t, "start", K.METHOD)
    assert start.qualified_name == "motor::Pump::start"
    assert one(t, "speed", K.METHOD).parent == "motor::Pump"
    assert one(t, "speed_", K.FIELD).type == "int"
    # Constructor declared but never defined -> external
    assert one(t, "Pump", K.PROTOTYPE).parent == "motor::Pump"
