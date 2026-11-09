import json
import math

import numpy as np
import pytest

from captest import force_model as fm
from force_sim import REFS, make_models, simulate, steady_v, trial_force

D33_TRUE = 20e-12
COFF_PF = 60.0


def dt_axis(seconds, dt=1e-3):
    return np.arange(0.0, seconds, dt)


def gain_true(models):
    """Proxy gain: sum_i Ctotal_i * w_i / Coff times d33_true (per newton), w_i = 1/5."""
    return D33_TRUE * sum(m.c_total for m in models.values()) / len(models) / (COFF_PF * 1e-12)


# ------------------------------------------------------------------- basics
def test_code_to_volts_uses_span_over_4096():
    assert fm.code_to_volts(4095, 2.5) == pytest.approx(2.5 * 4095 / 4096)
    assert fm.code_to_volts(2048, 2.5) == pytest.approx(1.25)
    # Data already in volts must not be converted again: the function is explicit about codes.
    assert fm.code_to_volts(np.array([0, 4096]), 2.5)[1] == pytest.approx(2.5)


def test_route_model_validation_and_summary_recovery():
    with pytest.raises(fm.ModelError):
        fm.RouteModel((1, 0), 0.0, 75e-12, 1.25)
    with pytest.raises(fm.ModelError):
        fm.RouteModel((1, 0), 1e6, float("nan"), 1.25)
    m = fm.RouteModel.from_summary((1, 0), r_bias=1e6, c_total=75e-12, v_d=1.25,
                                   c_share_ratio=0.088, c_share_1v_pf=8.4, q_cycle_pc=0.75)
    assert m.c0 == pytest.approx(0.088)
    assert m.c1 == pytest.approx((8.4e-12 - 0.088 * 75e-12) / 75e-12)
    assert m.qv == pytest.approx(0.75e-12 / 75e-12)


def test_constant_force_nonlinear_transition_gives_zero_residual():
    models = make_models()
    t = dt_axis(0.5)
    samples = simulate(models, t, np.zeros(len(t)), d33_true=D33_TRUE)
    for ref, d in samples.items():
        e = fm.residuals(models[ref], d["volts"], d["tc"])
        assert np.isnan(e[0])                      # first sample: no predecessor
        assert np.nanmax(np.abs(e)) < 1e-12
    # also from a far-from-equilibrium start: the transition itself is nonlinear but modelled
    m = models[REFS[0]]
    v = [m.v_d - 0.6]
    for _ in range(40):
        d = m.v_d - v[-1]
        v.append(m.v_d - math.exp(-20e-6 / m.tau) * ((1 - m.c0 - m.c1 * d) * d + m.qv))
    e = fm.residuals(m, np.array(v), np.full(len(v), 20e-6))
    assert np.nanmax(np.abs(e)) < 1e-12


def test_first_sample_and_invalid_samples_contribute_no_charge():
    m = make_models()[REFS[0]]
    v = np.full(20, steady_v(m, 20e-6))
    tc = np.full(20, 20e-6)
    v[10] += 0.3                                   # a spike
    valid = np.ones(20, dtype=bool)
    valid[10] = False                              # ...marked invalid
    e = fm.residuals(m, v, tc, valid)
    assert np.isnan(e[0]) and np.isnan(e[10]) and np.isnan(e[11])     # successor starts a generation
    assert np.nanmax(np.abs(e)) < 1e-9
    gen = np.zeros(20, dtype=int)
    gen[5:] = 1
    e2 = fm.residuals(m, v * 0 + steady_v(m, 20e-6), tc, generation=gen)
    assert np.isnan(e2[5]) and not np.isnan(e2[6])


def test_recovers_injected_charge_proxy_not_true_charge():
    models = make_models()
    t = dt_axis(1.0)
    force = trial_force(t, [(0.3, 100)])
    samples = simulate(models, t, force, d33_true=D33_TRUE, coff_pf=COFF_PF)
    q = fm.accumulate(models, samples)
    step = q.q_proxy[-1] - q.q_proxy[0]
    true_charge = D33_TRUE * 0.1 * 9.80665
    expected_proxy = gain_true(models) * 0.1 * 9.80665
    assert step == pytest.approx(expected_proxy, rel=1e-6)
    # The capacitance-proxy mode need not recover the true charge (Ctotal != Coff here).
    assert abs(step - true_charge) > 0.05 * true_charge
    assert np.all(np.diff(q.t) >= 0)


def test_polarity_applied_once_and_force_sign():
    models = make_models()
    t = dt_axis(0.5)
    force = trial_force(t, [(0.2, 50)])
    samples = simulate(models, t, force, d33_true=D33_TRUE, coff_pf=COFF_PF)
    plus = fm.accumulate(models, samples, polarity=1)
    minus = fm.accumulate(models, samples, polarity=-1)
    assert minus.q_proxy[-1] == pytest.approx(-plus.q_proxy[-1])
    assert plus.force(gain_true(models) / 1.0)[-1] == pytest.approx(0.05 * 9.80665, rel=1e-6)
    assert minus.force(-gain_true(models))[-1] == pytest.approx(0.05 * 9.80665, rel=1e-6)
    with pytest.raises(fm.ModelError):
        fm.accumulate(models, samples, polarity=2)
    with pytest.raises(fm.ModelError):
        plus.force(0.0)
    with pytest.raises(fm.ModelError):
        plus.force(float("nan"))


def test_no_sample_time_multiplication_and_no_hidden_reset():
    models = make_models()
    for dt in (1e-3, 4e-3):                              # different visit rates, same charge
        t = dt_axis(1.0, dt)
        force = trial_force(t, [(0.2, 50), (0.6, -50)])
        samples = simulate(models, t, force, d33_true=D33_TRUE, coff_pf=COFF_PF)
        q = fm.accumulate(models, samples)
        after_place = q.q_at(0.5)
        assert after_place == pytest.approx(gain_true(models) * 0.05 * 9.80665, rel=1e-6)
        assert q.q_at(0.95) == pytest.approx(0.0, abs=1e-18)       # returns to the origin
    # held force: the accumulator stays on its plateau (no decay or deadband)
    t = dt_axis(2.0)
    force = trial_force(t, [(0.2, 50)])
    q = fm.accumulate(models, simulate(models, t, force, d33_true=D33_TRUE, coff_pf=COFF_PF))
    plateau = q.q_at(0.5)
    assert q.q_at(1.9) == pytest.approx(plateau, rel=1e-9)


def test_independent_channel_state_and_per_route_series():
    models = make_models()
    t = dt_axis(0.5)
    w = {r: (1.0 if i == 2 else 0.0) for i, r in enumerate(REFS)}
    samples = simulate(models, t, trial_force(t, [(0.2, 100)]), d33_true=D33_TRUE,
                       coff_pf=COFF_PF, weights=w)
    q = fm.accumulate(models, samples)
    for i, ref in enumerate(REFS):
        total = q.per_route_q[ref][1][-1]
        if i == 2:
            assert total > 0
        else:
            assert abs(total) < 1e-18


# --------------------------------------------------------------- bias & event
def baseline_window(t_end=1.0):
    return (0.1, t_end)


def test_bias_estimate_frozen_and_event_fit_recovers_step_under_bias():
    models = make_models()
    t = dt_axis(6.0)
    force = trial_force(t, [(3.0, 100)])
    samples = simulate(models, t, force, d33_true=D33_TRUE, coff_pf=COFF_PF, qv_err=2e-5,
                       noise_v=2e-5)
    raw = {r: fm.residuals(models[r], d["volts"], d["tc"]) for r, d in samples.items()}
    bias = {r: fm.estimate_bias(r, samples[r]["t"], raw[r], (0.2, 2.5)) for r in REFS}
    assert all(b.frozen and b.n > 100 for b in bias.values())
    uncorrected = fm.accumulate(models, samples)
    corrected = fm.accumulate(models, samples, {r: b.bias_v for r, b in bias.items()})
    expected = gain_true(models) * 0.1 * 9.80665
    drift_uncorrected = (uncorrected.q_at(2.9) - uncorrected.q_at(0.2)) / 2.7
    assert abs(drift_uncorrected) > 0.2 * expected / 6          # the bias really ramps
    plain = fm.event_step(uncorrected.t, uncorrected.q_proxy, (1.5, 2.8), (3.3, 5.8), "none")
    ramp = fm.event_step(uncorrected.t, uncorrected.q_proxy, (1.5, 2.8), (3.3, 5.8), "joint")
    pre = fm.event_step(uncorrected.t, uncorrected.q_proxy, (1.5, 2.8), (3.3, 5.8), "pre")
    clean = fm.event_step(corrected.t, corrected.q_proxy, (1.5, 2.8), (3.3, 5.8), "none")
    assert abs(plain.delta_q - expected) > 0.1 * expected       # plain after-before is wrong
    assert ramp.delta_q == pytest.approx(expected, rel=0.03)
    assert pre.delta_q == pytest.approx(expected, rel=0.05)
    assert clean.delta_q == pytest.approx(expected, rel=0.03)
    assert ramp.drift_rate == pytest.approx(drift_uncorrected, rel=0.1)
    # the plain difference is wrong by drift times the elapsed time between the windows
    assert np.sign(ramp.drift_charge_over_gap) == np.sign(plain.delta_q - expected)



def test_drift_grows_with_visit_rate_but_signal_does_not():
    models = make_models()
    results = {}
    for dt in (1e-3, 4e-3):
        t = dt_axis(4.0, dt)
        s = simulate(models, t, trial_force(t, [(2.0, 100)]), d33_true=D33_TRUE,
                     coff_pf=COFF_PF, qv_err=3e-5)
        q = fm.accumulate(models, s)
        results[dt] = ((q.q_at(1.9) - q.q_at(0.2)) / 1.7, q.q_at(3.9) - q.q_at(2.1))
    fast, slow = results[1e-3], results[4e-3]
    assert abs(fast[0]) == pytest.approx(4.0 * abs(slow[0]), rel=0.05)


def test_bias_window_too_short_rejected():
    with pytest.raises(fm.ModelError):
        fm.estimate_bias((1, 0), np.arange(5.0), np.zeros(5), (0.0, 4.0))


def test_event_windows_validated():
    t = np.arange(0, 10, 0.01)
    q = np.zeros_like(t)
    with pytest.raises(fm.ModelError):
        fm.event_step(t, q, (1, 3), (2, 4))
    with pytest.raises(fm.ModelError):
        fm.event_step(t, q, (1, 1.05), (3, 3.05))
    with pytest.raises(fm.ModelError):
        fm.event_step(t, q, (1, 3), (4, 5), "bogus")


# ---------------------------------------------------------------- d33 fitting
def make_events(gain, masses=(20, 50, 100, 200), repeats=(1, 2, 3), g=9.80665, noise=0.0,
                intercept=0.0, seed=3, position=""):
    rng = np.random.default_rng(seed)
    events = []
    for r in repeats:
        for m in masses:
            for sign in (+1, -1):
                f = sign * m * 1e-3 * g
                q = gain * f + intercept + (rng.normal(0, noise) if noise else 0.0)
                events.append(fm.CalEvent("r%d_%d_%+d" % (r, m, sign), m, sign, r, q,
                                          position=position))
    return events


def test_d33_recovery_signed_loading_and_removal():
    gain = 7.5e-9
    fit = fm.fit_d33(make_events(gain, noise=2e-12))
    assert fit.d33_eff == pytest.approx(gain, rel=0.01)
    assert fit.r2 > 0.999 and fit.n == 24
    assert fit.placement_d33 == pytest.approx(gain, rel=0.02)
    assert fit.removal_d33 == pytest.approx(gain, rel=0.02)
    assert fit.flags == ()
    assert fit.rms_force_error_n < 0.01
    neg = fm.fit_d33(make_events(-gain))
    assert neg.d33_eff == pytest.approx(-gain, rel=1e-9)


def test_intercept_and_direction_mismatch_detected():
    gain = 7.5e-9
    big = fm.fit_d33(make_events(gain, intercept=1e-8, noise=1e-12))
    assert "LARGE_INTERCEPT" in big.flags
    assert big.intercept == pytest.approx(1e-8, rel=0.05)
    events = make_events(gain)
    skew = [fm.CalEvent(e.event_id, e.mass_g, e.direction, e.repeat,
                        e.delta_q * (1.5 if e.direction < 0 else 1.0)) for e in events]
    assert "DIRECTION_MISMATCH" in fm.fit_d33(skew).flags


def test_fit_rejects_rejected_events_and_too_few():
    gain = 7.5e-9
    events = make_events(gain)
    bad = [fm.CalEvent(e.event_id, e.mass_g, e.direction, e.repeat, e.delta_q * 10, accepted=False)
           for e in events[:4]]
    fit = fm.fit_d33(events + bad)
    assert fit.d33_eff == pytest.approx(gain, rel=1e-9) and fit.n == len(events)
    with pytest.raises(fm.ModelError):
        fm.fit_d33(events[:2])
    with pytest.raises(fm.ModelError):
        fm.fit_d33([fm.CalEvent("a", 20, 1, 1, 1e-9), fm.CalEvent("b", 20, 1, 2, 1e-9),
                    fm.CalEvent("c", 20, 1, 3, 1e-9)])


def test_holdout_validation_and_failure():
    gain = 7.5e-9
    events = make_events(gain, noise=1e-12)
    ok = fm.validate_holdout(events, [3], limit_n=0.02)
    assert ok.passed is True and ok.test_repeats == (3,) and ok.train_repeats == (1, 2)
    # Held-out trials from a different placement (gain changed 30 %) fail the validation.
    drift = [fm.CalEvent(e.event_id, e.mass_g, e.direction, e.repeat,
                         e.delta_q * (1.3 if e.repeat == 3 else 1.0)) for e in events]
    bad = fm.validate_holdout(drift, [3], limit_n=0.02)
    assert bad.passed is False and bad.max_force_error_n > 0.02
    with pytest.raises(fm.ModelError):
        fm.validate_holdout(events, [9])


def test_common_gain_fails_when_contact_positions_differ():
    gain = 7.5e-9
    a = make_events(gain, repeats=(1, 2), position="centre")
    b = [fm.CalEvent("e_" + e.event_id, e.mass_g, e.direction, e.repeat + 10, e.delta_q * 0.5,
                     position="edge") for e in make_events(gain, repeats=(1, 2))]
    fit = fm.fit_d33(a + b)
    assert "POSITION_DEPENDENT_GAIN" in fit.flags
    assert fit.rms_force_error_n > 0.05
    same = fm.fit_d33(a + [fm.CalEvent("x" + e.event_id, e.mass_g, e.direction, e.repeat + 10,
                                       e.delta_q, position="edge")
                           for e in make_events(gain, repeats=(1, 2))])
    assert "POSITION_DEPENDENT_GAIN" not in same.flags


def test_end_to_end_simulated_calibration_recovers_system_gain():
    models = make_models()
    g = fm.G_DEFAULT
    t = dt_axis(8.0)
    events = []
    for rep in (1, 2):
        for mass in (20, 50, 100, 200):
            force = trial_force(t, [(2.0, mass), (5.0, -mass)])
            s = simulate(models, t, force, d33_true=D33_TRUE, coff_pf=COFF_PF, qv_err=1e-5,
                         noise_v=2e-5, seed=10 * rep + mass)
            raw = {r: fm.residuals(models[r], d["volts"], d["tc"]) for r, d in s.items()}
            bias = {r: fm.estimate_bias(r, s[r]["t"], raw[r], (0.1, 1.5)).bias_v for r in REFS}
            q = fm.accumulate(models, s, bias)
            up = fm.event_step(q.t, q.q_proxy, (1.0, 1.9), (2.2, 4.9), "joint")
            down = fm.event_step(q.t, q.q_proxy, (3.0, 4.9), (5.3, 7.9), "joint")
            events.append(fm.CalEvent("p%d_%d" % (rep, mass), mass, +1, rep, up.delta_q, up.drift_rate))
            events.append(fm.CalEvent("r%d_%d" % (rep, mass), mass, -1, rep, down.delta_q, down.drift_rate))
    fit = fm.fit_d33(events, g)
    assert fit.d33_eff == pytest.approx(gain_true(models), rel=0.02)
    assert fit.flags == ()
    val = fm.validate_holdout(events, [2], limit_n=0.05)
    assert val.passed


# ------------------------------------------------------------------- leakage
def leak_windows():
    return [fm.ConstantWindow("baseline", 0.2, 1.8, "baseline"),
            fm.ConstantWindow("hold", 2.2, 3.8, "hold")]


def leak_models():
    return make_models(qv=0.05)


def test_leakage_identified_when_operating_points_differ():
    models = leak_models()
    t = dt_axis(4.0)
    tc = np.where(t < 2.0, 20e-6, 80e-6)             # different timing -> different d*
    s = simulate(models, t, np.zeros(len(t)), tc=tc, lam=0.97, noise_v=0.0)
    fit = fm.fit_leakage(models, s, leak_windows(), lam_bounds=(0.90, 1.0), grid=101)
    assert fit.status == "identified", fit.reason
    assert fit.model.lam == pytest.approx(0.97, abs=0.004)
    assert fit.model.kind == "effective" and "Roff" in fit.model.note
    assert fit.chi2_best < fit.chi2_none
    assert abs(fit.window_slopes_best["hold"]) < abs(fit.window_slopes_none["hold"])
    json.dumps(fit.to_json())                         # strict, serialisable


def test_leakage_not_identified_when_bias_explains_data():
    models = leak_models()
    t = dt_axis(4.0)
    s = simulate(models, t, np.zeros(len(t)), qv_err=3e-5, noise_v=1e-5)    # bias only
    fit = fm.fit_leakage(models, s, leak_windows())
    assert fit.status == "not_identified"
    assert fit.model.lam == 1.0 and not fit.model.active and fit.reason


def test_leakage_needs_baseline_and_hold():
    models = leak_models()
    t = dt_axis(2.0)
    s = simulate(models, t, np.zeros(len(t)))
    fit = fm.fit_leakage(models, s, [fm.ConstantWindow("hold", 0.5, 1.5)])
    assert fit.status == "not_identified"


def test_leakage_correction_preserves_held_force():
    models = leak_models()
    t = dt_axis(4.0)
    tc = np.where(t < 2.0, 20e-6, 80e-6)
    force = trial_force(t, [(0.5, 100)])
    s = simulate(models, t, force, tc=tc, lam=0.97, d33_true=D33_TRUE, coff_pf=COFF_PF)
    leak = {r: fm.LeakageModel(0.97, None, "effective") for r in REFS}
    q = fm.accumulate(models, s, leakage=leak)
    plateau = q.q_at(1.0)
    assert plateau == pytest.approx(gain_true(models) * 0.1 * 9.80665, rel=1e-4)
    assert q.q_at(3.9) == pytest.approx(plateau, rel=1e-4)     # constant force stays constant


def test_physical_leakage_model_validation():
    m = fm.LeakageModel.from_physical(100e-6, 1e9, 60e-12)
    assert m.lam == pytest.approx(math.exp(-100e-6 / 60e-3)) and m.kind == "physical"
    assert fm.LeakageModel().active is False
    with pytest.raises(fm.ModelError):
        fm.LeakageModel.from_physical(1e-4, 0.0, 60e-12)
    with pytest.raises(fm.ModelError):
        fm.LeakageModel(1.5)


# ---------------------------------------------------------------- range checks
def test_range_report_flags_clipping_and_outside_range():
    m = fm.RouteModel((1, 0), 1e6, 75e-12, 1.25, d_min=0.0, d_max=0.2)
    ok = fm.range_report(m, np.array([1.2, 1.1, 1.15]))
    assert not ok["clipped"] and not ok["outside_fitted_range"]
    out = fm.range_report(m, np.array([1.2, 0.9]))
    assert out["outside_fitted_range"] and not out["clipped"]
    clip = fm.range_report(m, np.array([1.25, 2.499]))
    assert clip["clipped"]
    low = fm.range_report(m, np.array([0.001]))
    assert low["clipped"]
    unknown = fm.range_report(fm.RouteModel((1, 0), 1e6, 75e-12, 1.25), np.array([0.9]))
    assert not unknown["outside_fitted_range"]


def test_a_voltage_far_above_vd_is_not_rejected_only_flagged():
    """A loaded PZT drives the node above VD: that is the signal, not an error."""
    m = fm.RouteModel((1, 0), 1e6, 75e-12, 1.25, d_min=0.01, d_max=1.1)
    up = fm.range_report(m, np.array([1.25, 1.6, 1.9]))             # 0.65 V above VD, far from clipping
    assert not up["outside_fitted_range"] and not up["clipped"]
    assert up["extrapolated_low"] and up["d_min_v"] == pytest.approx(-0.65)
    assert fm.range_report(m, np.array([1.25, 2.499]))["clipped"]   # only the ADC range limits it
    strict = fm.range_report(m, np.array([1.25, 1.6]), neg_margin_v=0.25)
    assert strict["outside_fitted_range"]                            # the optional limit still works


def test_signature_text_is_canonical():
    assert fm.signature_of(b=2, a=1) == "a=1;b=2"
