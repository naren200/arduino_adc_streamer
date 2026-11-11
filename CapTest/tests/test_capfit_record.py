"""capfit's board-neutral entry point (analyze_record) and the plateau check relative to
the pre-connect level."""

import numpy as np
import pytest

from captest import capfit


def decay(tau=40e-6, v0=0.25, vinf=1.65, n=300, step=1.04e-6, noise_lsb=1.0, seed=2, lsb=3.3 / 4096):
    rng = np.random.default_rng(seed)
    t = step * (np.arange(n) + 3)
    v = vinf - (vinf - v0) * np.exp(-t / tau) + rng.normal(0, noise_lsb * lsb, n)
    return t, np.round(v / lsb) * lsb


def record(**kw):
    t, v = decay()
    fields = dict(ref=(1, 2, 3), t=t, v=v, v_d=1.652, flags=[], period_ns={"min": 960.0, "mean": 1037.0, "max": 1120.0},
                  label="1:2:3")
    fields.update(kw)
    return capfit.BurstRecord(**fields)


def settings(vref=3.3, model=capfit.FIT_ONE):
    return capfit.AnalysisSettings(vref=vref, fit_model=model)


def test_record_analysis_recovers_tau_c_total_and_the_step_ratio():
    m = capfit.analyze_record(record(), 470e3, settings())
    assert m.ref == (1, 2, 3) and m.label == "1:2:3" and (m.adc, m.channel) == (1, 3)
    assert m.tau == pytest.approx(40e-6, rel=0.01) and m.c_total == pytest.approx(40e-6 / 470e3, rel=0.01)
    assert m.v0 == pytest.approx(0.25, abs=0.01)
    assert m.ratio == pytest.approx(0.25 / 1.652, abs=0.01) and m.c_d == pytest.approx(m.ratio * m.c_total)
    assert m.flags == [] and m.n_used == 300 and m.residual_rms_lsb == pytest.approx(1.0, abs=0.3)


def test_record_flags_and_degenerate_inputs():
    m = capfit.analyze_record(record(v_d=float("nan")), 470e3, settings())
    assert capfit.NO_VMID in m.flags and np.isnan(m.ratio) and m.tau > 0
    t, v = decay(n=10)
    short = capfit.analyze_record(record(t=t, v=v), 470e3, settings())
    assert capfit.FEW_SAMPLES in short.flags and not short.ok
    empty = capfit.analyze_record(record(t=np.zeros(0), v=np.zeros(0)), 470e3, settings())
    assert capfit.FEW_SAMPLES in empty.flags and capfit.INCOMPLETE in empty.flags
    flagged = capfit.analyze_record(record(flags=[capfit.PERIOD_SHORT, capfit.INCOMPLETE]), 470e3, settings())
    assert capfit.PERIOD_SHORT in flagged.flags and not flagged.ok
    soon = capfit.analyze_record(record(), 470e3, settings(), since_last_ms=20.0, discharge_ms=300.0)
    assert {capfit.TOO_SOON, capfit.VS0_SUSPECT} <= set(soon.flags)
    later = capfit.analyze_record(record(), 470e3, settings(), since_last_ms=400.0, discharge_ms=300.0)
    assert capfit.TOO_SOON not in later.flags


def test_record_start_voltage_and_low_amplitude():
    start = capfit.analyze_record(record(), 470e3, capfit.AnalysisSettings(vref=3.3, v_s0=0.1, fit_model=capfit.FIT_ONE))
    assert start.ratio == pytest.approx((0.25 - 0.1) / (1.652 - 0.1), abs=0.01)
    t, v = decay(v0=1.62)                                           # a sensor that already sits at VMID
    flat = capfit.analyze_record(record(t=t, v=v), 470e3, settings())
    assert capfit.LOW_AMPLITUDE in flat.flags


def test_two_exponential_selection_applies_to_records_too():
    t = 1.04e-6 * (np.arange(400) + 3)
    lsb = 3.3 / 4096
    v = 1.65 - 0.9 * (0.6 * np.exp(-t / 60e-6) + 0.4 * np.exp(-t / 12e-6))
    v = np.round((v + np.random.default_rng(3).normal(0, lsb, len(t))) / lsb) * lsb
    one = capfit.analyze_record(record(t=t, v=v), 470e3, settings(model=capfit.FIT_ONE))
    two = capfit.analyze_record(record(t=t, v=v), 470e3, settings(model=capfit.FIT_TWO))
    assert one.fit_model_used == capfit.FIT_ONE and two.fit_model_used == capfit.FIT_TWO
    assert two.tau == pytest.approx(60e-6, rel=0.08) and one.rms_1exp > 2 * two.rms_2exp


def test_plateau_check_relative_to_the_pre_connect_level():
    waits = [2000, 6000, 15000, 40000, 90000]
    drifting = [1.6019, 1.6114, 1.6283, 1.6574, 1.6847]            # measured on the board
    absolute = capfit.plateau_check(waits, drifting, vref=3.3)
    assert absolute["plateau"] is False or absolute["tolerance_v"] > 0.05     # 5 % of 1.68 V hides the drift
    relative = capfit.plateau_check(waits, drifting, vref=3.3, reference=1.652)
    assert relative["plateau"] is False and relative["tolerance_v"] < 0.005
    settled = capfit.plateau_check(waits, [0.60, 0.40, 0.30, 0.2502, 0.2500], vref=3.3, reference=1.652)
    assert settled["plateau"] and settled["sufficient_wait_ms"] == 15000
