"""Real QSPICE runs (skipped without QSPICE): a known force through the models and the calculated force F.

The ideal charge model loses charge to the multiplexer node and the bias resistor, so a short pulse is recovered with a stated loss
and a long pulse drifts; the leakage-compensated model (default) recovers the generated charge within a few percent.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from qspice_sim import model_loader, runner
from qspice_sim.adc_model import build_sample_table
from qspice_sim.force_calc import FORCE_DEFAULTS, compute_all

QSPICE_DIR = Path(__file__).resolve().parents[2]
AMP = 0.05                                      # N, about 0.5 V open circuit on the default sensor: well inside the 0 .. 2.5 V range


def _force_run(tmp_path: Path, width_us: float, leak_comp: bool, cycles: int = 4):
    try:
        qspice = runner.find_qspice()
    except runner.SimulationError:
        pytest.skip("QSPICE is not installed")
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_7953")
    cfg = model.default_config()
    cfg["timing"]["num_cycles"] = cycles
    cfg["adc"]["bits"] = 16                     # quantisation is not what these tests are about
    # the force rises just before the first conversion of channel 2 (42 us) and stays for width_us
    cfg["forces"] = [{"address": 2, "t_start_us": 36.0, "rise_us": 1.0, "width_us": width_us, "fall_us": 1.0, "amp_N": AMP}]
    prepared = runner.prepare(model, cfg)
    raw = runner.run_netlist(prepared.netlist, ["sh", "f2"], tmp_path, qspice)
    try:
        adc = model.adc_params(cfg)
        times = np.array([s["t"] for s in prepared.samples])
        table = build_sample_table(prepared.samples, np.interp(times, raw.time, raw.column("sh")), adc)
        force = raw.column("f2")
        fin_at = lambda ch, t: np.interp(t, raw.time, force) if ch == 2 else np.zeros(len(t))  # noqa: E731
        settings = dict(FORCE_DEFAULTS, leak_comp=leak_comp)
        results = compute_all(table, [0, 2], model.sensor_info(cfg), settings, cfg["circuit"]["vmid_v"], fin_at)
    finally:
        raw.close()
    return results


# ------------------------------------------------------------------ ideal charge model (checkbox off)
def test_ideal_model_a_short_pulse_is_recovered_within_the_charge_sharing_loss(tmp_path):
    results = _force_run(tmp_path, width_us=15.0, leak_comp=False)
    cf = results[2]
    assert cf.fin[0] == pytest.approx(AMP) and cf.fin[1:].tolist() == [0.0] * (len(cf.fin) - 1)
    # the first conversion sees the pulse: the 10 pF multiplexer node takes about 15 % of the 64 pF sensor charge, and the bias
    # resistor drains a little during the connection, so F is 60 .. 90 % of the applied force (0.73 in this model)
    assert 0.6 * AMP < cf.F[0] < 0.9 * AMP
    # after the pulse the calculated force returns to about zero (|F| below 30 % of the peak)
    assert np.all(np.abs(cf.F[1:]) < 0.3 * cf.F[0])
    # a sensor without force stays at zero within a few LSB (0.6 mV of ADC LSB are 0.06 mN)
    assert np.max(np.abs(results[0].F)) < 2e-3


def test_ideal_model_a_long_pulse_drifts_because_the_bias_resistor_drains_the_sensor(tmp_path):
    results = _force_run(tmp_path, width_us=200.0, leak_comp=False)
    cf = results[2]
    assert np.all(cf.fin == pytest.approx(AMP)) and len(cf.F) == 4
    assert cf.F[0] > 0.6 * AMP and np.all(np.diff(cf.F) < 0)                  # monotonic decay while the force is constant
    assert cf.F[-1] < 0.6 * cf.F[0]                                           # the ideal charge model loses more than 40 % in 4 sweeps
    assert cf.error_final < -0.25 * AMP                                        # the calculated force ends far below the applied one


# ------------------------------------------------------------------ leakage and charge-sharing compensation (default)
def test_compensated_short_pulse_is_recovered_and_returns_to_zero(tmp_path):
    cf = _force_run(tmp_path, width_us=15.0, leak_comp=True)[2]
    assert cf.leak_used
    assert 0.93 * AMP < cf.F[0] < 1.05 * AMP                                  # measured 0.0484 N: the ideal model gave 0.0364 N
    assert np.all(np.abs(cf.F[1:]) < 0.1 * AMP)                               # back to zero (measured |F| < 2.6 mN)


def test_compensated_long_pulse_holds_the_applied_force(tmp_path):
    cf = _force_run(tmp_path, width_us=200.0, leak_comp=True, cycles=8)[2]
    assert np.all(cf.fin[:4] == pytest.approx(AMP)) and np.all(cf.fin[4:] == 0.0)
    # the four conversions during the pulse stay within 12 % of the applied force (measured 0.0484 .. 0.0461 N; the ideal model
    # fell from 0.0364 to 0.0162 N), the release returns to zero within 12 %
    assert np.all(np.abs(cf.F[:4] - AMP) < 0.12 * AMP)
    assert np.all(np.abs(cf.F[4:]) < 0.12 * AMP)


def test_compensated_adc124_plateau_over_many_scan_cycles(tmp_path):
    try:
        qspice = runner.find_qspice()
    except runner.SimulationError:
        pytest.skip("QSPICE is not installed")
    model = model_loader.load_model(QSPICE_DIR / "TestBoard_ADC124")
    cfg = model.default_config()
    cfg["timing"]["num_cycles"] = 24
    cfg["adc"]["bits"] = 16
    cfg["forces"] = [{"address": 2, "t_start_us": 100.0, "rise_us": 5.0, "width_us": 2000.0, "fall_us": 5.0, "amp_N": AMP}]
    prepared = runner.prepare(model, cfg)
    raw = runner.run_netlist(prepared.netlist, ["ain", "f2"], tmp_path, qspice)
    try:
        times = np.array([s["t"] for s in prepared.samples])
        table = build_sample_table(prepared.samples, np.interp(times, raw.time, raw.column("ain")), model.adc_params(cfg))
        force = raw.column("f2")
        fin_at = lambda ch, t: np.interp(t, raw.time, force) if ch == 2 else np.zeros(len(t))  # noqa: E731
        ideal = compute_all(table, [2], model.sensor_info(cfg), dict(FORCE_DEFAULTS, leak_comp=False), cfg["circuit"]["vmid_v"], fin_at)[2]
        comp = compute_all(table, [2], model.sensor_info(cfg), FORCE_DEFAULTS, cfg["circuit"]["vmid_v"], fin_at)[2]
    finally:
        raw.close()
    on = comp.fin > 0.99 * AMP                                                # conversions inside the 2 ms plateau
    assert on.sum() >= 15 and comp.leak_used
    # measured: the compensated force stays within +5 % of the applied one over 2 ms, the ideal one decays to 3 % of it
    assert np.all(np.abs(comp.F[on] - AMP) < 0.08 * AMP)
    assert ideal.F[on][-1] < 0.1 * AMP
    assert np.all(np.abs(comp.F[~on & (comp.t > 2.3e-3)]) < 0.08 * AMP)       # and back to zero after the release (measured < 2.8 mN)
