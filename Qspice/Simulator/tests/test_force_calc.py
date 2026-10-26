"""B5: force extraction from the ADC values (ideal charge model) and the Force tab."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import numpy as np
import pytest

from qspice_sim import model_loader
from qspice_sim.adc_model import AdcParams, build_sample_table
from qspice_sim.force_calc import (FORCE_DEFAULTS, REF_FIRST, REF_VMID, SRC_ADC_OUT, SRC_IDEAL, SensorParams, compute_all,
                                   compute_channel, extract_force, formulas_html, resolve_params, summary_row)

QSPICE_DIR = Path(__file__).resolve().parents[2]
INFO = {"d33_C_per_N": 600e-12, "cp_F": 60e-12, "cextra_F": 0.0}


def _table(v, bits=16, vmid=1.25, vref=2.5, address=3):
    n = len(v)
    samples = [{"t": (k + 1) * 1e-4, "label": "x", "address": address} for k in range(n)]
    return build_sample_table(samples, np.asarray(v, dtype=float), AdcParams(bits=bits, vref=vref, vmid=vmid))


# ------------------------------------------------------------------ the formulas
def test_dF_and_F_match_a_hand_computed_result():
    params = SensorParams(d33=600e-12, c_sensor=60e-12, c_extra=0.0, gain=1.0)
    assert params.sensitivity == pytest.approx(10.0) and params.force_per_volt() == pytest.approx(0.1)
    dF, F = extract_force(np.array([1.35, 1.35, 1.25, 1.55]), params, v_start=1.25)
    assert dF == pytest.approx([0.01, 0.0, -0.01, 0.03]) and F == pytest.approx([0.01, 0.01, 0.0, 0.03])


def test_reference_choice_changes_only_the_first_step():
    table = _table([1.35, 1.45, 1.45])
    p = resolve_params(FORCE_DEFAULTS, INFO)
    vmid = compute_channel(table, 3, p, dict(FORCE_DEFAULTS, source=SRC_IDEAL, reference=REF_VMID), vmid=1.25)
    first = compute_channel(table, 3, p, dict(FORCE_DEFAULTS, source=SRC_IDEAL, reference=REF_FIRST), vmid=1.25)
    assert vmid.dF == pytest.approx([0.01, 0.01, 0.0]) and first.dF == pytest.approx([0.0, 0.01, 0.0])
    assert vmid.F[-1] - first.F[-1] == pytest.approx(0.01)


def test_overriding_d33_capacitance_extra_capacitance_and_gain_scales_the_result_exactly():
    table = _table([1.35, 1.45, 1.30])
    base = compute_channel(table, 3, resolve_params(FORCE_DEFAULTS, INFO), dict(FORCE_DEFAULTS, source=SRC_IDEAL), vmid=1.25)

    def run(**override):
        s = dict(FORCE_DEFAULTS, source=SRC_IDEAL, **override)
        return compute_channel(table, 3, resolve_params(s, INFO), s, vmid=1.25)

    assert run(d33_pC_per_N=1200.0).F == pytest.approx(base.F / 2)          # twice the charge per newton, half the force
    assert run(cp_pF=120.0).F == pytest.approx(base.F * 2)                  # twice the capacitance, twice the force
    assert run(cextra_pF=60.0).F == pytest.approx(base.F * 2)               # the extra node capacitance adds to the sensor
    assert run(gain=4.0).F == pytest.approx(base.F / 4)
    assert run(d33_pC_per_N=None).F == pytest.approx(base.F)                # empty = follow the component value


def test_no_override_follows_the_model_values_and_bad_values_are_rejected():
    p = resolve_params(FORCE_DEFAULTS, dict(INFO, cextra_F=3e-12))
    assert (p.d33, p.c_sensor, p.c_extra, p.gain) == (600e-12, 60e-12, 3e-12, 1.0) and p.c_total == pytest.approx(63e-12)
    with pytest.raises(ValueError, match="capacitance"):
        resolve_params(dict(FORCE_DEFAULTS, cp_pF=0.0), dict(INFO, cp_F=0.0))
    with pytest.raises(ValueError, match="d33"):
        resolve_params(dict(FORCE_DEFAULTS, gain=0.0), INFO)


def test_ideal_inputs_with_a_tiny_lsb_recover_the_applied_force():
    sens = 600e-12 / 60e-12
    force = np.array([0.0, 0.02, 0.05, 0.05, 0.01, 0.0, -0.03])
    table = _table(1.25 + sens * force, bits=16)
    cf = compute_channel(table, 3, resolve_params(FORCE_DEFAULTS, INFO), FORCE_DEFAULTS, vmid=1.25, fin_at=lambda ch, t: force)
    lsb_force = table.adc.lsb * 0.1                                         # N per LSB
    assert cf.F == pytest.approx(force, abs=lsb_force) and cf.error_final == pytest.approx(0.0, abs=lsb_force)
    assert cf.dF.sum() == pytest.approx(cf.F[-1])


def test_quantisation_limits_the_resolution_to_the_force_per_lsb():
    sens = 10.0
    force = np.linspace(0.0, 0.05, 40)
    table = _table(1.25 + sens * force, bits=8)                             # 9.8 mV LSB = about 1 mN
    cf = compute_channel(table, 3, resolve_params(FORCE_DEFAULTS, INFO), FORCE_DEFAULTS, vmid=1.25)
    half_lsb_force = table.adc.lsb * 0.1 / 2
    assert np.max(np.abs(cf.F - force)) <= half_lsb_force + 1e-12
    assert np.max(np.abs(cf.F - force)) > half_lsb_force / 10               # visibly quantised


def test_a_channel_without_conversions_is_skipped_and_the_summary_row_has_the_documented_columns():
    table = _table([1.3, 1.4])
    assert compute_channel(table, 5, resolve_params(FORCE_DEFAULTS, INFO), FORCE_DEFAULTS, 1.25) is None
    results = compute_all(table, [3, 5], {3: INFO, 5: INFO}, FORCE_DEFAULTS, 1.25, lambda ch, t: np.array([0.0, 0.05]))
    assert list(results) == [3]
    row = summary_row(results[3])
    assert row[:3] == [3, 2, 0] and row[3] == pytest.approx(0.005, abs=1e-4) and row[8] == 0.05 and row[9] == pytest.approx(results[3].F[-1] - 0.05)
    assert summary_row(compute_channel(table, 3, resolve_params(FORCE_DEFAULTS, INFO), FORCE_DEFAULTS, 1.25))[8:] == [None, None]


def test_formulas_show_the_live_numbers_and_the_ideal_model_caveat():
    p = resolve_params(FORCE_DEFAULTS, INFO)
    html = formulas_html(p, 0.8e-3, 3, REF_VMID, SRC_ADC_OUT)
    for text in ("dF[k]", "d33", "Sensitivity", "10 V/N", "600 pC/N", "60 pF", "drifts"):
        assert text in html
    assert "ideal charge model" in html.lower() and "force per lsb" in html.lower()
    assert "the first sample" in formulas_html(p, None, 3, REF_FIRST, SRC_IDEAL)


# ------------------------------------------------------------------ manifest
def test_models_declare_the_force_calculator_and_per_sensor_defaults():
    for mid, n in (("TestBoard_ADC124", 7), ("TestBoard_7953", 15)):
        model = model_loader.load_model(QSPICE_DIR / mid)
        assert model.problems == [] and model.force_calc["force_node"] == "f{k}"
        info = model.sensor_info(model.default_config())
        assert sorted(info) == list(range(n))
        assert info[0]["d33_C_per_N"] == pytest.approx(600e-12) and info[0]["cp_F"] == pytest.approx(60.7e-12, rel=2e-3)
        assert info[0]["cextra_F"] == 0.0
        cfg = model.default_config()
        cfg["sensor_overrides"]["3"] = {"cp_pF": 25.0, "force_gain": 0.5}
        assert model.sensor_info(cfg)[3] == {"d33_C_per_N": pytest.approx(300e-12), "cp_F": pytest.approx(25e-12), "cextra_F": 0.0}
        assert model.force_node(4) == "f4" and model.forced_channels(cfg) == {2, 4}


def test_manifest_validation_checks_the_force_calc_block():
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    manifest = dict(model.manifest)
    assert model_loader.validate_manifest(manifest, model.defaults) == []
    assert any("adc_out" in p for p in model_loader.validate_manifest({k: v for k, v in manifest.items() if k != "adc_out"}, model.defaults))
    bad = dict(manifest, force_calc={"enabled": True, "force_node": "force", "force_tables": ["nope"]})
    problems = " | ".join(model_loader.validate_manifest(bad, model.defaults))
    assert "{k}" in problems and "nope" in problems


# ------------------------------------------------------------------ GUI
pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6 import QtWidgets  # noqa: E402

from derived_helpers import install_result, tick  # noqa: E402
from qspice_sim.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture()
def window(app):
    win = MainWindow([model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124"), model_loader.load_model(QSPICE_DIR / "TestBoard_7953")])
    win.settings.clear()
    win.resize(1500, 900)
    win.show()
    yield win
    win.close()


SENS = 600e-12 / 60.7e-12                     # open-circuit V/N of the default ADC124 sensor


def _step(t):
    return np.where(t > 2.0e-4, 0.05, 0.0)    # a 0.05 N step during the second scan cycle


def _ain(t):
    return 1.65 + SENS * _step(t)


def _field(window, path):
    return next(r for r in window.force_tab.form.rows if r.path == path)


def _table_rows(window):
    tab = window.force_tab.table
    return [[tab.item(r, c).text() for c in range(tab.columnCount())] for r in range(tab.rowCount())]


def _fake_force_run(window):
    install_result(window, {"ain": _ain, "f2": _step, "f4": _step})
    tick(window, "ain", "adcout_ch2")
    return window.force_results


def test_force_tab_sits_between_simulation_and_visualization_and_has_the_documented_fields(window):
    names = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert names.index("Force") == names.index("Simulation") + 1 == names.index("Visualization") - 1
    labels = [r.item["label"] for r in window.force_tab.form.rows]
    assert labels == ["d33 charge constant", "Sensor capacitance", "Extra node capacitance", "Front-end gain G",
                      "Reference for the first sample", "Voltage source", "Sensors to extract"]
    assert [r.path for r in window.force_tab.form.rows if r.kind == "choice"] == ["force.reference", "force.source"]
    assert window.force == FORCE_DEFAULTS
    html = window.force_tab.formulas.toPlainText()
    assert "dF[k]" in html and "ideal charge model" in html.lower()


def test_force_results_table_lists_every_sensor_with_the_final_error(window):
    results = _fake_force_run(window)
    assert sorted(results) == [0, 1, 2, 3, 4, 5, 6]                          # every channel that has conversions
    rows = {int(r[0]): r for r in _table_rows(window)}
    assert len(rows) == 7 and rows[2][1] == "4"                              # four conversions of channel 2
    cf = results[2]
    assert cf.F[-1] == pytest.approx(0.05, abs=2e-4) and cf.fin[-1] == pytest.approx(0.05)
    assert abs(cf.error_final) < 2e-4 and float(rows[2][9]) == pytest.approx(cf.error_final, rel=1e-3)
    assert float(rows[2][7]) == pytest.approx(cf.F[-1], rel=1e-3) and float(rows[2][8]) == pytest.approx(0.05)    # shown with 4 digits
    assert results[0].fin[-1] == 0.0 and results[0].F[-1] == pytest.approx(0.05, abs=2e-4)     # unforced channel: error = the whole F
    assert float(rows[0][9]) == pytest.approx(0.05, abs=2e-4)


def test_overriding_a_value_scales_the_result_marks_the_field_and_replots_without_a_run(window):
    results = _fake_force_run(window)
    base = results[2].F.copy()
    row = _field(window, "force.d33_pC_per_N")
    assert row.editor.placeholderText() == "model: 600 pC/N" and not row.reset.isEnabled()
    row.editor.setText("1200")
    row.editor.editingFinished.emit()
    assert window.force["d33_pC_per_N"] == 1200.0 and window.worker is None
    assert window.force_results[2].F == pytest.approx(base / 2)
    assert row.reset.isEnabled() and "fff4cc" in row.editor.styleSheet()     # highlighted like any changed parameter
    assert "1200 pC/N" in window.force_tab.formulas.toPlainText()
    row._on_reset()                                                          # back to "follow components"
    assert window.force["d33_pC_per_N"] is None and window.force_results[2].F == pytest.approx(base)
    cp = _field(window, "force.cp_pF")
    cp.editor.setText("121.4")
    cp.editor.editingFinished.emit()
    assert window.force_results[2].F == pytest.approx(base * 2, rel=1e-3)
    _field(window, "force.cextra_pF").editor.setText("0")
    _field(window, "force.cextra_pF").editor.editingFinished.emit()
    gain = _field(window, "force.gain")
    cp.editor.setText("")
    cp.editor.editingFinished.emit()
    gain.editor.setText("2")
    gain.editor.editingFinished.emit()
    assert window.force_results[2].F == pytest.approx(base / 2)


def test_source_reference_and_sensor_selection(window):
    _fake_force_run(window)
    channels = _field(window, "force.channels")
    channels.editor.setText("2, 4")
    channels.editor.editingFinished.emit()
    assert window.force["channels"] == [2, 4] and [r[0] for r in _table_rows(window)] == ["2", "4"]
    channels.editor.setText("")
    channels.editor.editingFinished.emit()
    assert window.force["channels"] is None and len(_table_rows(window)) == 7
    source = _field(window, "force.source")
    window.cfg["adc"]["bits"] = 4                                            # coarse ADC: the ideal input is unaffected
    window._on_param_changed("adc.bits")
    coarse = window.force_results[2].F.copy()
    source.editor.setCurrentText(SRC_IDEAL)
    ideal = window.force_results[2].F
    assert abs(ideal[-1] - 0.05) < abs(coarse[-1] - 0.05) or np.array_equal(ideal, coarse) is False
    assert ideal[-1] == pytest.approx(0.05, abs=2e-4)
    _field(window, "force.reference").editor.setCurrentText(REF_FIRST)
    assert window.force_results[2].dF[0] == 0.0


def test_force_settings_round_trip_through_save_and_load(window, tmp_path, monkeypatch):
    _fake_force_run(window)
    for path, text in (("force.d33_pC_per_N", "900"), ("force.gain", "1.5"), ("force.channels", "2")):
        _field(window, path).editor.setText(text)
        _field(window, path).editor.editingFinished.emit()
    _field(window, "force.reference").editor.setCurrentText(REF_FIRST)
    saved = dict(window.force)
    target = tmp_path / "settings.json"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "")))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(target), "")))
    window.save_settings()
    assert json.loads(target.read_text(encoding="utf-8"))["force"] == saved
    window._reset_all()
    assert window.force == FORCE_DEFAULTS and window.force_tab.form.rows[0].editor.text() == ""
    window.load_settings()
    assert window.force == saved and _field(window, "force.d33_pC_per_N").editor.text() == "900"
    assert _field(window, "force.reference").editor.currentText() == REF_FIRST


def test_per_sample_table_export(window, tmp_path, monkeypatch):
    _fake_force_run(window)
    path = tmp_path / "force.csv"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(path), "")))
    window.force_tab.export_btn.click()
    rows = list(csv.reader(path.open(encoding="utf-8")))
    assert rows[0] == ["t", "address", "V", "dF", "F", "Fin"] and len(rows) == 1 + 28
    last2 = [r for r in rows[1:] if r[1] == "2"][-1]
    assert float(last2[4]) == pytest.approx(0.05, abs=2e-4) and float(last2[5]) == pytest.approx(0.05)


def test_the_tab_explains_what_is_missing(window):
    assert "Run a simulation first" in window.force_tab.note.text() and window.force_tab.table.rowCount() == 0
    install_result(window, {"muxd": _ain})
    tick(window, "muxd")
    assert "did not save the ADC sample node" in window.force_tab.note.text()
    install_result(window, {"ain": _ain})
    tick(window, "adcout_ch2")
    assert "Applied force not saved" in window.force_tab.note.text()         # forces exist on 2 and 4, f2 / f4 were not saved
    assert any(r[8] == "n/a" for r in _table_rows(window) if r[0] in ("2", "4"))


def test_a_model_without_force_calc_has_no_force_tab(app):
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    model.manifest.pop("force_calc")
    win = MainWindow([model])
    win.settings.clear()
    assert win.force_tab is None and "Force" not in [win.tabs.tabText(i) for i in range(win.tabs.count())]
    win.close()
