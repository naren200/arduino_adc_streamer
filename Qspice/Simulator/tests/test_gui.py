"""Headless GUI tests (Qt offscreen platform): screens, parameter editing, run, reference overlay, image export."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtTest, QtWidgets  # noqa: E402

from qspice_sim import model_loader, runner  # noqa: E402
from qspice_sim.gui.main_window import MainWindow  # noqa: E402

QSPICE_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture()
def window(app, tmp_path):
    models = [model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")]
    win = MainWindow(models)
    win.settings.clear()
    win.resize(1500, 900)
    win.show()
    yield win
    win.close()


def _field(window, path):
    for form in window._tab_forms:
        for row in form.rows:
            if row.path == path:
                return row
    raise KeyError(path)


def _wait_for_run(window, timeout_ms=120_000):
    QtTest.QTest.qWait(200)
    waited = 0
    while window.worker is not None and waited < timeout_ms:
        QtTest.QTest.qWait(200)
        waited += 200
    assert window.worker is None, "simulation did not finish"


def test_screens_exist(window):
    names = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert names == ["Model", "Components", "System", "Simulation", "Visualization"]
    assert len(_field(window, "mux.ileak_s_off_nA").widgets()) == 4
    assert window.node_tree.topLevelItemCount() >= 4


def test_editing_a_component_parameter_updates_config_and_marks_it(window):
    row = _field(window, "mux.ileak_s_off_nA")
    row.editor.setText("5n")
    row.editor.editingFinished.emit()
    assert window.cfg["mux"]["ileak_s_off_nA"] == pytest.approx(5e-9)
    assert row.reset.isEnabled()
    row._on_reset()
    assert window.cfg["mux"]["ileak_s_off_nA"] == window.defaults["mux"]["ileak_s_off_nA"]


def test_invalid_input_is_rejected(window):
    row = _field(window, "mux.ron_ohm")
    before = window.cfg["mux"]["ron_ohm"]
    row.editor.setText("abc")
    row.editor.editingFinished.emit()
    assert window.cfg["mux"]["ron_ohm"] == before


def test_nullable_and_list_fields(window):
    rate = _field(window, "timing.sampling_rate_hz")
    rate.editor.setText("5000")
    rate.editor.editingFinished.emit()
    assert window.cfg["timing"]["sampling_rate_hz"] == 5000.0
    rate.editor.setText("")
    rate.editor.editingFinished.emit()
    assert window.cfg["timing"]["sampling_rate_hz"] is None
    order = _field(window, "timing.scan_order")
    order.editor.setText("0, 2, 4")
    order.editor.editingFinished.emit()
    assert window.cfg["timing"]["scan_order"] == [0, 2, 4]


def test_preset_applies_overrides(window):
    idx = window.preset_combo.findData("baseline")
    window._apply_preset(idx)
    assert window.cfg["forces"] == [] and window.cfg["timing"]["run_duration_s"] == 0.06


def test_input_profile_table_edits_forces(window):
    table = window._tables[0]
    n = len(window.cfg["forces"])
    table._add()
    assert len(window.cfg["forces"]) == n + 1
    table.table.item(n, 5).setText("0.05")
    assert window.cfg["forces"][n]["amp_N"] == pytest.approx(0.05)
    table._clear()
    assert window.cfg["forces"] == []


def test_run_plot_reference_and_export(window, tmp_path):
    try:
        runner.find_qspice()
    except runner.SimulationError:
        pytest.skip("QSPICE is not installed")
    window.cfg["timing"]["num_cycles"] = 1
    window.viz["nodes"] = {"ain", "muxd", "f2"}
    window._populate_nodes()
    window.run()
    _wait_for_run(window)
    assert window.result is not None and window.plot.has_data()
    assert len(window.plot._panels) == 2            # ADC-input group and force group
    assert window.act_img.isEnabled()
    window.hold_reference()
    assert window.reference is not None

    # second run with other parameters, compared against the held reference
    window.cfg["mux"]["leak_scale"] = 5.0
    window.run()
    _wait_for_run(window)
    assert window.result.cfg["mux"]["leak_scale"] == 5.0
    assert "Leakage multiplier" in window._title()

    png = tmp_path / "out.png"
    window.plot.save_image(str(png), width=900)
    assert png.stat().st_size > 5000
    svg = tmp_path / "out.svg"
    window.plot.save_image(str(svg))
    assert svg.stat().st_size > 1000

    # switching the layout re-plots from the saved result without running again
    window.layout_combo.setCurrentIndex(window.layout_combo.findData("node"))
    assert len(window.plot._panels) == 3
    shot = tmp_path / "window.png"
    window.grab().save(str(shot))
    assert shot.stat().st_size > 10_000


def test_node_not_in_result_is_reported(window):
    try:
        runner.find_qspice()
    except runner.SimulationError:
        pytest.skip("QSPICE is not installed")
    window.cfg["timing"]["num_cycles"] = 1
    window.viz["nodes"] = {"ain"}
    window._populate_nodes()
    window.run()
    _wait_for_run(window)
    window.viz["nodes"] = {"ain", "s3"}
    window._populate_nodes()
    window._replot()
    assert "s3" in window.status_label.text()


def test_stop_cancels_a_long_run(window):
    try:
        runner.find_qspice()
    except runner.SimulationError:
        pytest.skip("QSPICE is not installed")
    window.cfg["timing"]["run_duration_s"] = 0.01
    window.viz["nodes"] = {"ain"}
    window._populate_nodes()
    QtWidgets.QMessageBox.question = staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes)
    window.run()
    QtTest.QTest.qWait(1200)
    assert window.worker is not None
    window.stop()
    _wait_for_run(window, 20_000)
    assert window.result is None and "cancelled" in window.status_label.text().lower()
