import pytest

pytest.importorskip("PySide6.QtWidgets")

from source_tracer.gui.main_window import MainWindow  # noqa: E402
from tests.conftest import EXAMPLE  # noqa: E402


@pytest.fixture
def window(qtbot):
    from source_tracer.gui.theme import apply_dark_theme
    from PySide6.QtWidgets import QApplication

    apply_dark_theme(QApplication.instance())
    w = MainWindow()
    qtbot.addWidget(w)
    w.load([EXAMPLE])
    return w


def _find(tree, text):
    stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        it = stack.pop(0)  # breadth-first: prefer the top "Likely fault codes" entry
        if it.text(0) == text:
            return it
        stack.extend(it.child(i) for i in range(it.childCount()))
    return None


def test_load_fills_panels(window):
    assert window.fault_table.rowCount() == len(window.project.faults.codes)
    assert window.usage_table.rowCount() == len(window.project.faults.usages)
    assert window.tabs.count() >= 1
    assert "MC68332" in window.statusBar().currentMessage()


def test_selecting_fault_shows_details_and_source(window):
    window.tree.setCurrentItem(_find(window.tree, "FAULT_OVERTEMP"))
    assert "FAULT_OVERTEMP" in window.details.toPlainText()
    view = window.tabs.currentWidget()
    assert view.path.name == "sensors.c"
    assert view.textCursor().blockNumber() + 1 == 55


def test_filter_hides_non_matching(window):
    window.search.setText("SCDR")
    assert not _find(window.tree, "SCDR").isHidden()
    assert _find(window.tree, "FAULT_OVERTEMP").isHidden()


def _set_var(panel, name, value):
    t = panel.var_table
    for r in range(t.rowCount()):
        if t.item(r, 0).text() == name:
            t.item(r, 3).setText(value)
            return
    raise AssertionError(f"{name} not in variables table")


def test_run_function_from_gui(window, qtbot):
    panel = window.run_panel
    assert panel.select_function("sensors_check_faults")
    _set_var(panel, "g_temp_c", "90")
    _set_var(panel, "g_vbus_mv", "24000")
    _set_var(panel, "s_overtemp_ticks", "50")
    with qtbot.waitSignal(panel.run_finished, timeout=10000):
        panel.run()
    out = window.results.outcome
    assert out.result.ok
    assert out.faults.raised[:1] == ["FAULT_OVERTEMP"]
    assert "FAULT_OVERTEMP" in window.results.summary.toPlainText()
    assert window.results.faults.rowCount() >= 2
    # executed lines are recorded for the source view
    sensors = next(p for p in window.coverage if p.name == "sensors.c")
    assert 55 in window.coverage[sensors]


def test_run_with_arguments_and_transmit(window, qtbot):
    panel = window.run_panel
    assert panel.select_function("comms_send_status")
    _set_var(panel, "g_active_fault", "FAULT_DRY_RUN")
    with qtbot.waitSignal(panel.run_finished, timeout=10000):
        panel.run()
    r = window.results.outcome.result
    assert [t.value for t in r.transmitted][:4] == [0x7E, 0x01, 0x06, 0x30]
    assert window.results.sent_table.rowCount() == 10
