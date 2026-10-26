"""B3: white noise added to the ADC input at every conversion, before quantisation."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from qspice_sim import model_loader, runner
from qspice_sim.adc_model import AdcParams, build_sample_table, quantize

QSPICE_DIR = Path(__file__).resolve().parents[2]
ADC = AdcParams(bits=12, vref=2.5, vmid=1.25)


def _samples(n: int, address: int = 0) -> list[dict]:
    return [{"t": k * 1e-6, "label": "x", "address": address} for k in range(n)]


def test_zero_noise_gives_exactly_the_quantised_ideal_value():
    v = np.linspace(0.1, 2.4, 50)
    table = build_sample_table(_samples(50), v, ADC, noise_mv=0.0, seed=7)
    assert not table.noise_v.any()
    assert np.array_equal(table.v_out, quantize(v, ADC)[1]) and np.array_equal(table.code, quantize(v, ADC)[0])


def test_same_seed_gives_identical_noise_and_another_seed_a_different_one():
    v = np.full(500, 1.0)
    a = build_sample_table(_samples(500), v, ADC, noise_mv=3.0, seed=5)
    b = build_sample_table(_samples(500), v, ADC, noise_mv=3.0, seed=5)
    c = build_sample_table(_samples(500), v, ADC, noise_mv=3.0, seed=6)
    assert np.array_equal(a.noise_v, b.noise_v) and np.array_equal(a.code, b.code)
    assert not np.array_equal(a.noise_v, c.noise_v)


def test_measured_standard_deviation_matches_the_setting():
    n = 100_000
    table = build_sample_table(_samples(n), np.full(n, 1.25), ADC, noise_mv=4.0, seed=1)
    assert np.std(table.noise_v) == pytest.approx(4e-3, rel=0.02)
    assert abs(np.mean(table.noise_v)) < 4e-3 * 0.02
    # independent samples: no correlation between neighbours (white)
    assert abs(np.corrcoef(table.noise_v[:-1], table.noise_v[1:])[0, 1]) < 0.02
    # the noise is added before the quantisation: a 4 mV RMS spreads many LSBs (0.61 mV) around the ideal code
    assert np.std(table.v_out) == pytest.approx(np.hypot(4e-3, table.adc.lsb / np.sqrt(12)), rel=0.03)
    assert np.array_equal(table.v_in, np.full(n, 1.25))                 # the ideal value is not touched


def test_noise_far_above_the_range_clips_and_negative_noise_is_rejected():
    table = build_sample_table(_samples(1000), np.full(1000, 2.499), ADC, noise_mv=50.0, seed=1)
    assert table.clipped.sum() > 100 and table.code.max() == ADC.max_code
    with pytest.raises(ValueError, match="negative"):
        build_sample_table(_samples(3), np.zeros(3), ADC, noise_mv=-1.0)


def test_noise_does_not_change_the_netlist_or_the_saved_nodes():
    for mid in ("TestBoard_ADC124", "TestBoard_7953"):
        model = model_loader.load_model(QSPICE_DIR / mid)
        cfg = model.default_config()
        clean = model.build(cfg)
        cfg["simulation"].update(adc_noise_mV=12.0, noise_seed=99)
        noisy = model.build(cfg)
        assert noisy["netlist"] == clean["netlist"] and noisy["samples"] == clean["samples"]
        assert model.problems == [] and model.adc_out["noise_mv_path"] == "simulation.adc_noise_mV"


pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6 import QtWidgets  # noqa: E402

from derived_helpers import install_result, plot_traces, tick  # noqa: E402
from qspice_sim.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture()
def window(app):
    win = MainWindow([model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")])
    win.settings.clear()
    win.resize(1500, 900)
    win.show()
    yield win
    win.close()


def _field(window, path):
    return next(r for f in window._tab_forms for r in f.rows if r.path == path)


def test_noise_fields_are_on_the_simulation_tab_with_an_explanation(window):
    row = _field(window, "simulation.adc_noise_mV")
    assert row.unit.text() == "mV" and "conversion" in row.editor.toolTip() and "not to the continuous waveform" in row.editor.toolTip()
    assert window.cfg["simulation"]["adc_noise_mV"] == 0 and window.cfg["simulation"]["noise_seed"] == 1
    assert _field(window, "simulation.noise_seed").kind == "int"
    prepared = runner.prepare(window.model, window.cfg)
    window.viz["nodes"] = {"adcout_ch2"}
    assert window._nodes_to_save(prepared) == ["ain"]               # the noise adds no QSPICE node


def test_changing_the_noise_replots_the_adc_out_trace_without_a_run_and_is_repeatable(window):
    install_result(window, {"ain": lambda t: 0.8 + 0 * t})
    tick(window, "ain", "adcout_ch2")
    flat = plot_traces(window)["adcout_ch2"].v[0::2].copy()
    assert len(np.unique(flat)) == 1                                # constant input, no noise: one code
    row = _field(window, "simulation.adc_noise_mV")
    row.editor.setText("20")
    row.editor.editingFinished.emit()
    noisy = plot_traces(window)["adcout_ch2"].v[0::2].copy()
    assert window.worker is None and len(np.unique(noisy)) > 1
    ideal = np.interp(window.sample_table.t[window.sample_table.channel_mask(2)], window.result.qraw.time, np.full(40001, 0.8))
    assert plot_traces(window)["ain"].v == pytest.approx(0.8)       # the continuous waveform is untouched
    assert np.std(noisy - flat[0]) > 0 and ideal == pytest.approx(0.8)
    row.editor.setText("0")                                         # back to zero: the clean trace again
    row.editor.editingFinished.emit()
    assert np.array_equal(plot_traces(window)["adcout_ch2"].v[0::2], flat)
    seed = _field(window, "simulation.noise_seed")
    row.editor.setText("20")
    row.editor.editingFinished.emit()
    first = plot_traces(window)["adcout_ch2"].v.copy()
    seed.editor.setText("2")
    seed.editor.editingFinished.emit()
    assert not np.array_equal(plot_traces(window)["adcout_ch2"].v, first)
    seed.editor.setText("1")
    seed.editor.editingFinished.emit()
    assert np.array_equal(plot_traces(window)["adcout_ch2"].v, first)    # same seed, same noise


def test_noisy_input_markers_are_optional_and_the_flag_round_trips(window, tmp_path, monkeypatch):
    install_result(window, {"ain": lambda t: 0.8 + 0 * t})
    window.cfg["simulation"]["adc_noise_mV"] = 10.0
    tick(window, "ain", "adcout_ch2")
    assert not window.noisy_chk.isHidden() and not window.viz["show_noisy"]
    window.noisy_chk.setChecked(True)
    assert window.viz["show_noisy"]
    path = tmp_path / "settings.json"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(path), "")))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(path), "")))
    window.save_settings()
    window.noisy_chk.setChecked(False)
    window.cfg["simulation"]["adc_noise_mV"] = 0.0
    window.load_settings()
    assert window.viz["show_noisy"] and window.noisy_chk.isChecked() and window.cfg["simulation"]["adc_noise_mV"] == 10.0
