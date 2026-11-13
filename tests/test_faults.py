from source_tracer.analysis.faults import looks_like_fault_name, name_tokens
from tests.helpers import faults_of


def test_name_heuristics():
    assert name_tokens("g_faultFlagsHI") == ["g", "fault", "flags", "hi"]
    for name in ["FAULT_OVERTEMP", "ErrCode", "g_err", "DTC_P0101", "alarmState", "FLT_BIT_X", "diagStatus"]:
        assert looks_like_fault_name(name), name
    for name in ["TERRAIN", "buffer", "counter", "FAULTS"[:0] + "MOTOR_SPEED"]:
        assert not looks_like_fault_name(name), name


def test_example_project_fault_codes(example_project):
    faults = example_project.faults
    names = [c.name for c in faults.codes]
    for expected in ["FAULT_OVERTEMP", "FAULT_UNDERVOLT", "FAULT_DRY_RUN", "FLT_BIT_VOLTAGE"]:
        assert expected in names
    ot = faults.code("FAULT_OVERTEMP")
    assert ot.value == 0x12 and ot.confidence == "high" and ot.kind == "code"
    assert [(u.file.name, u.line, u.function) for u in ot.set_sites] == [("sensors.c", 55, "sensors_check_faults")]
    assert faults.code("FLT_BIT_VOLTAGE").kind == "bit"
    assert faults.code("FAULT_COMMS").set_sites == []  # defined but never raised
    assert {s.symbol.name: s.role for s in faults.sinks} == {
        "set_fault": "reporter",
        "clear_fault": "clearer",
        "g_active_fault": "store",
        "g_fault_flags": "store",
    }
    clears = [(u.line, u.target) for u in faults.usages if u.action == "clear" and u.file.name == "main.c"]
    assert clears == [(29, "g_fault_flags"), (31, "g_active_fault")]
    check = [u.code for u in faults.usages if u.action == "check" and u.function == "pump_set"]
    assert check == ["FLT_BIT_OVERTEMP", "FLT_BIT_DRY_RUN"]


def test_literal_codes_and_inferred_constants():
    _, report = faults_of(
        """
        #define MOTOR_STALL 7
        unsigned char g_error;
        unsigned char status;
        void check(int rpm) {
            if (rpm == 0) g_error = MOTOR_STALL;     /* constant not named like a fault */
            else if (rpm > 9000) g_error = 12;        /* literal code */
            else g_error = 0;                         /* cleared */
            status = 3;                               /* not a fault store */
        }
        """
    )
    stall = report.code("MOTOR_STALL")
    assert stall is not None and stall.confidence == "low" and stall.value == 7
    assert [(u.value, u.action) for u in report.literal_codes] == [(12, "set")]
    actions = [(u.line, u.action, u.value) for u in report.usages]
    assert actions == [(6, "set", 7), (7, "set", 12), (8, "clear", 0)]
    assert report.sink("status") is None


def test_reporter_inferred_from_calls_with_fault_constants():
    _, report = faults_of(
        """
        #define ERR_CRC 0x31
        void log_event(int code);
        void rx(void) { log_event(ERR_CRC); log_event(99); }
        """
    )
    sink = report.sink("log_event")
    assert sink is not None and sink.role == "reporter"
    assert [(u.code, u.value) for u in sink.usages] == [("ERR_CRC", 0x31), (None, 99)]
