"""Sine force input profile ("Add sin" table): waveform generation, warnings, GUI table and a real QSPICE run."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from qspice_sim import model_loader, runner

QSPICE_DIR = Path(__file__).resolve().parents[2]
MODELS = ["TestBoard_7953", "TestBoard_ADC124"]
SIN = {"address": 2, "t_start_us": 20.0, "freq_hz": 50000.0, "cycles": 3, "amp_N": 0.05}


@pytest.fixture(params=MODELS)
def model(request):
    return model_loader.load_model(QSPICE_DIR / request.param)


def _cfg(model, **sin):
    cfg = model.default_config()
    cfg["forces"] = []
    cfg["forces_sin"] = [{**SIN, **sin}]
    return cfg


def test_default_config_has_an_empty_sine_table_and_manifest_entry(model):
    assert model.problems == []
    assert model.default_config()["forces_sin"] == []
    spec = next(t for t in model.manifest["tables"] if t["path"] == "forces_sin")
    assert spec["add_label"] == "Add sin"
    assert [c["key"] for c in spec["columns"]] == ["address", "t_start_us", "freq_hz", "cycles", "amp_N"]


def _force_lines(model, cfg, addr=2):
    text = model.build(cfg)["netlist"]
    return [ln for ln in text.splitlines() if ln.startswith((f"Vf{addr} ", f"Vfs{addr} ", f"Vf{addr}_pulse"))]


def test_sine_is_a_qspice_sin_source_with_the_row_parameters(model):
    """The sine is QSPICE's own SIN source (smooth, no piecewise-linear corners); other sensors keep a flat PWL source."""
    cfg = _cfg(model)
    assert _force_lines(model, cfg) == ["Vf2 f2 0 SIN(0 5.000000000e-02 5.000000000e+04 2.000000000e-05 0 0 3.000000000e+00)"]
    assert _force_lines(model, cfg, addr=3)[0].startswith("Vf3 f3 0 PWL(")
    assert "SINE_POINTS" not in model.adapter.bm.__dict__ and not any("PWL" in ln for ln in _force_lines(model, cfg))


def test_sine_reference_value_peaks_a_quarter_period_in(model):
    bm = model.adapter.bm
    row = _cfg(model)["forces_sin"][0]
    assert abs(bm.sine_value(row, 20e-6)) < 1e-12 and bm.sine_value(row, 0.0) == 0.0         # silent before the start
    assert bm.sine_value(row, 20e-6 + 0.25 / 50e3) == pytest.approx(0.05)                     # peak a quarter period after the start
    assert abs(bm.sine_value(row, 20e-6 + 3 / 50e3)) < 1e-12 and abs(bm.sine_value(row, 1.0)) < 1e-12   # whole cycles end at zero


def test_sine_adds_to_pulses_on_the_same_address(model):
    """A sensor with pulses and a sine gets the pulse PWL source and the SIN source in series: V(f) = V(f_pulse) + sine."""
    cfg = _cfg(model)
    cfg["forces"] = [{"address": 2, "t_start_us": 0.0, "rise_us": 1.0, "width_us": 500.0, "fall_us": 1.0, "amp_N": 0.02}]
    lines = _force_lines(model, cfg)
    assert any(ln.startswith("Vf2 f2_pulse 0 PWL(") for ln in lines) and any(ln.startswith("Vfs2 f2 f2_pulse SIN(") for ln in lines)


def test_sine_ending_away_from_a_zero_crossing_is_flagged(model):
    bm = model.adapter.bm
    assert not any("zero crossing" in w for w in model.build(_cfg(model, cycles=2.5))["warnings"])
    assert not any("zero crossing" in w for w in model.build(_cfg(model, cycles=3))["warnings"])
    cfg = _cfg(model, cycles=2.25)
    assert any("zero crossing" in w and "holds" in w for w in model.build(cfg)["warnings"])
    assert bm.sine_value(cfg["forces_sin"][0], 1.0) == pytest.approx(0.05)                   # QSPICE holds the last value after the last cycle


def test_invalid_sine_rows_are_rejected(model):
    with pytest.raises(ValueError, match="positive"):
        model.build(_cfg(model, freq_hz=0.0))
    with pytest.raises(ValueError, match="positive"):
        model.build(_cfg(model, cycles=0))
    cfg = _cfg(model)
    cfg["forces_sin"].append({**SIN, "freq_hz": 1000.0})
    with pytest.raises(ValueError, match="only one sine"):
        model.build(cfg)
    cfg["forces_sin"][1]["address"] = 3                                                      # another sensor may have its own sine
    assert len(_force_lines(model, cfg, addr=3)) == 1


def test_sine_amplitude_over_the_supply_is_flagged(model):
    assert not any("sine" in w for w in model.build(_cfg(model))["warnings"])
    assert any("Force sine" in w for w in model.build(_cfg(model, amp_N=0.5))["warnings"])


def _qspice_or_skip() -> Path:
    try:
        return runner.find_qspice()
    except runner.SimulationError:
        pytest.skip("QSPICE is not installed")


def test_real_run_shows_a_bipolar_sine_on_the_driven_sensor_only(tmp_path):
    qspice = _qspice_or_skip()
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_7953")
    cfg = _cfg(model, t_start_us=5.0, amp_N=0.02)
    cfg["timing"]["num_cycles"] = 1
    prepared = runner.prepare(model, cfg)
    raw = runner.run_netlist(prepared.netlist, ["p2", "p3"], tmp_path, qspice)
    p2, p3 = raw.column("p2"), raw.column("p3")
    assert p2.max() - p2.min() > 0.2                       # 0.02 N x 600 pC/N / 60.7 pF = 0.2 V open circuit: +/-0.2 V swing
    assert p2.max() > p2[0] + 0.1 and p2.min() < p2[0] - 0.1
    assert (p3.max() - p3.min()) < 0.2 * (p2.max() - p2.min())      # an undriven neighbour only sees the mux charge sharing and leakage
    raw.close()


def test_real_run_force_node_is_the_exact_sine_plus_the_pulse_and_holds_its_last_value(tmp_path):
    qspice = _qspice_or_skip()
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    bm = model.adapter.bm
    cfg = _cfg(model, address=0, t_start_us=5.0, freq_hz=20000.0, cycles=2.25, amp_N=0.02)       # ends on a peak: 112.5 us long
    cfg["forces"] = [{"address": 0, "t_start_us": 0.0, "rise_us": 1.0, "width_us": 500.0, "fall_us": 1.0, "amp_N": 0.01}]
    cfg["timing"].update(scan_order=[0], n_mux=1, run_duration_s=1.6e-4, num_cycles=5)
    cfg["simulation"]["max_step_ns"] = 20.0
    prepared = runner.prepare(model, cfg)
    raw = runner.run_netlist(prepared.netlist, ["f0"], tmp_path, qspice)
    t, f = raw.time, raw.column("f0")
    raw.close()
    probe = np.array([10e-6, 30e-6, 55e-6, 80e-6, 100e-6, 117e-6, 140e-6, 155e-6])             # the last two are after the sine ended
    expected = np.array([0.01 + bm.sine_value(cfg["forces_sin"][0], x) for x in probe])
    assert np.interp(probe, t, f) == pytest.approx(expected, abs=2e-5)
    assert expected[-1] == pytest.approx(0.01 + 0.02, abs=1e-9)                                  # pulse + the held peak of the sine
