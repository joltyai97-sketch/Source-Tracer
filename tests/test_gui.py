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
