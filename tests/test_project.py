from source_tracer.frontend import symbols as K
from source_tracer.profile import available_profiles, load_profile


def test_profiles_load():
    assert {"generic", "mc68332"} <= set(available_profiles())
    p = load_profile("mc68332")
    assert p.endianness == "big"
    assert p.register_at(0xFFFC0E).name == "SCDR"
    assert p.register_at(0xFFFD24).name == "QSPI_TR"  # inside a RAM block
    assert p.register_at(0x7FFC0E) is None
    assert p.register_named("SCSR").default == 0x0180


def test_example_project_loads(example_project):
    p = example_project
    assert sorted(f.path.name for f in p.files) == ["comms.c", "faults.h", "main.c", "regs.h", "sensors.c"]
    assert [f.name for f in p.other_files] == ["startup.s"]
    assert [s.name for s in p.symbols.of_kind(K.ISR)] == ["pit_isr"]
    assert [s.name for s in p.symbols.of_kind(K.PROTOTYPE)] == ["cpu_idle"]
    regs = {s.name: s for s in p.symbols.of_kind(K.REGISTER, K.REGISTER_BLOCK)}
    assert regs["PORTF"].address == 0xFFFA19 and regs["PORTF"].register.name == "PORTF0"
    assert all(r.register is not None for r in regs.values())
    # Only informational warnings: no parse problems in the example.
    assert {w.severity for w in p.warnings} == {"info"}
    assert len(p.symbols.of_kind(K.FUNCTION, K.ISR)) == 13


def test_function_at(example_project):
    p = example_project
    sensors = next(f.path for f in p.files if f.path.name == "sensors.c")
    assert p.symbols.function_at(sensors, 55).name == "sensors_check_faults"
    assert p.symbols.function_at(sensors, 5) is None
