import json
from dataclasses import replace

import numpy as np
import pytest

from captest import force_calibration as fc, force_model as fm
from force_sim import REFS, make_models, simulate

D33_TRUE = 20e-12
COFF_PF = 60.0
G = fm.STANDARD_GRAVITY


@pytest.mark.parametrize("text, expected", [
    ("20, 50, 100, 200", (20., 50., 100., 200.)),
    ("25; 12.5", (12.5, 25.)), ("10 30", (10., 30.)), ("5", (5.,)),
])
def test_custom_masses_are_parsed_and_run_smallest_first(text, expected):
    masses = fc.parse_masses(text)
    assert masses == expected
    wizard = fc.Wizard(masses, repeats=2)
    assert [(s.mass_g, s.repeat) for s in wizard.steps] == [(m, r) for m in expected for r in (1, 2)]


@pytest.mark.parametrize("text", ["", "0, 20", "-5, 20", "nan", "inf", "20 g", "20, 20.0"])
def test_invalid_or_duplicate_mass_lists_are_rejected(text):
    with pytest.raises(fc.CalibrationError):
        fc.parse_masses(text)


def gain_true(models):
    return D33_TRUE * sum(m.c_total for m in models.values()) / len(models) / (COFF_PF * 1e-12)


def smooth_force(t, mass_g, t_place=3.0, t_remove=14.0, ring=0.25, remove_fraction=1.0):
    """Mass placed at t_place and removed at t_remove, with a ramp and a decaying ring."""
    f = np.zeros(len(t))
    peak = mass_g * 1e-3 * G
    for t0, sign in ((t_place, +1), (t_remove, -remove_fraction)):
        x = t - t0
        rise = np.clip(x / 0.15, 0, 1)
        ringing = ring * np.exp(-np.clip(x, 0, None) / 0.08) * np.sin(2 * np.pi * 15 * np.clip(x, 0, None))
        f += sign * peak * (rise + np.where(x > 0, ringing, 0.0) * (x > 0.15))
    return f


MARKS = fc.Marks(baseline=(0.3, 2.5), place=3.0, place_clear=3.7, remove=14.0, remove_clear=14.7)


def capture_for(mass_g, models, qv_err=1e-5, noise_v=2e-5, seed=1, span=2.5, **kw):
    t = np.arange(0, 18.0, 2e-3)
    s = simulate(models, t, smooth_force(t, mass_g), d33_true=D33_TRUE, coff_pf=COFF_PF,
                 qv_err=qv_err, noise_v=noise_v, seed=seed, **kw)
    return fc.Capture(s, span, "sim")


@pytest.fixture(scope="module")
def models():
    return make_models()


@pytest.fixture(scope="module")
def trial20(models):
    return fc.analyze_trial(capture_for(20, models), models, MARKS, 20, 1)


def test_trial_recovers_placement_and_removal_steps(models, trial20):
    t = trial20
    assert t.accepted, t.reasons
    assert [e.direction for e in t.events] == [+1, -1]
    expected = gain_true(models) * 0.02 * G
    assert t.events[0].delta_q == pytest.approx(expected, rel=0.05)
    assert t.events[1].delta_q == pytest.approx(-expected, rel=0.05)
    assert t.load_removal_ratio == pytest.approx(1.0, rel=0.08)
    assert "LOAD_REMOVAL_INCONSISTENT" not in t.flags
    # windows are outside the mechanical transient (placement ends by about 3.5 s)
    assert t.windows["post_place"][0] >= MARKS.place_clear + fc.DEFAULT_GUARD_S - 1e-9
    assert t.windows["pre_place"][1] <= MARKS.place - fc.DEFAULT_GUARD_S + 1e-9
    assert set(t.bias) == {"%d:%d" % r for r in REFS}
    json.dumps(t.to_json(), allow_nan=False)


def test_range_checks_ignore_discarded_startup_but_keep_raw_diagnostics_and_legacy_policy(models):
    cap = capture_for(20, models)
    ref = next(iter(models))
    cap.samples[ref]["volts"][0] = .001
    trial = fc.analyze_trial(cap, models, MARKS, 20, 1)
    assert trial.accepted, trial.reasons
    report = trial.ranges["%d:%d" % ref]
    assert report["capture_v_min"] == .001 and report["v_min"] > .001
    assert report["analysis_start_s"] == MARKS.baseline[0]
    legacy = fc.analyze_trial(cap, models, MARKS, 20, 1, range_policy=fc.LEGACY_RANGE_POLICY)
    assert not legacy.accepted and any("ADC_HEADROOM_LIMIT" in r for r in legacy.reasons)
    # A low sample immediately before the baseline is still used by its first prediction.
    data = cap.samples[ref]
    first = np.flatnonzero(data["t"] >= MARKS.baseline[0])[0]
    data["volts"][first - 1] = .001
    checked = fc.analyze_trial(cap, models, MARKS, 20, 1)
    assert not checked.accepted and any("ADC_HEADROOM_LIMIT" in r for r in checked.reasons)


def test_low_voltage_during_weight_phase_still_reports_model_extrapolation(models):
    models = {ref: replace(model, d_min=0., d_max=1.1) for ref, model in models.items()}
    cap = capture_for(20, models)
    data = cap.samples[next(iter(models))]
    data["volts"][np.flatnonzero(data["t"] >= 5.)[0]] = .01
    trial = fc.analyze_trial(cap, models, MARKS, 20, 1)
    assert any("OUTSIDE_FITTED_RANGE" in r for r in trial.reasons)


def test_plain_difference_would_have_been_biased_but_trial_is_not(models):
    cap = capture_for(20, models, qv_err=6e-5, noise_v=0.0)
    t = fc.analyze_trial(cap, models, MARKS, 20, 1)
    expected = gain_true(models) * 0.02 * G
    assert t.accepted
    assert t.events[0].delta_q == pytest.approx(expected, rel=0.03)
    # bias is frozen on the baseline, so residual drift (if any) is the model error only
    assert abs(t.hold_drift_c_per_s) < 0.05 * abs(expected)


def test_clipping_rejects_trial_and_stops_escalation(models):
    t = np.arange(0, 18.0, 2e-3)
    big = simulate(models, t, smooth_force(t, 200), d33_true=D33_TRUE * 12, coff_pf=COFF_PF,
                   qv_err=1e-5)
    # drive the volts into the rail
    for d in big.values():
        d["volts"] = np.clip(d["volts"], None, 2.5)
        d["volts"][int(3.2 / 2e-3):int(3.6 / 2e-3)] = 2.4999
    result = fc.analyze_trial(fc.Capture(big, 2.5), models, MARKS, 200, 1)
    assert not result.accepted and result.supports_mass is False
    assert any("CLIPPED" in r for r in result.reasons)
    assert all(not e.accepted for e in result.events)


def test_outside_fitted_range_rejected():
    models = {r: fm.RouteModel(m.ref, m.r_bias, m.c_total, m.v_d, m.c0, m.c1, m.qv,
                               d_min=0.0, d_max=0.001)
              for r, m in make_models().items()}
    t = fc.analyze_trial(capture_for(20, models), models, MARKS, 20, 1)
    assert not t.accepted and any("OUTSIDE_FITTED_RANGE" in r for r in t.reasons)
    assert t.supports_mass is False


def test_unstable_signal_gives_no_window(models):
    cap = capture_for(20, models)
    # the user declares hand clear at 17.5 s: too little capture is left for any stable window
    marks = fc.Marks(baseline=(0.3, 2.5), place=3.0, place_clear=3.7, remove=14.0, remove_clear=17.5)
    t = fc.analyze_trial(cap, models, marks, 20, 1)
    assert not t.accepted and "NO_STABLE_POST_REMOVAL" in t.reasons


def test_user_confirmed_windows_override_detection(models):
    marks = fc.Marks(baseline=(0.3, 2.5), place=3.0, place_clear=3.7, remove=14.0, remove_clear=14.7,
                     post_place=(5.0, 5.5), post_remove=(16.0, 16.5))
    t = fc.analyze_trial(capture_for(20, models), models, marks, 20, 1)
    assert t.accepted and t.windows["post_place"] == (5.0, 5.5)
    assert t.events[0].delta_q == pytest.approx(gain_true(models) * 0.02 * G, rel=0.05)


def test_hold_drift_is_reported_not_hidden(models):
    t = np.arange(0, 18.0, 2e-3)
    s = simulate(models, t, smooth_force(t, 20), d33_true=D33_TRUE, coff_pf=COFF_PF, qv_err=1e-5,
                 noise_v=2e-5, creep_v_per_s=1.5e-3, creep_window=(4.0, 13.0))
    res = fc.analyze_trial(fc.Capture(s, 2.5), models, MARKS, 20, 1)
    assert res.hold_drift_c_per_s is not None and res.hold_drift_c_per_s > 0
    assert "HOLD_DRIFT" in res.flags            # flagged, not corrected away by picking endpoints
    clean = fc.analyze_trial(capture_for(20, models), models, MARKS, 20, 1)
    assert "HOLD_DRIFT" not in clean.flags


def test_removal_smaller_than_loading_is_flagged(models):
    t = np.arange(0, 18.0, 2e-3)
    s = simulate(models, t, smooth_force(t, 20, remove_fraction=0.6), d33_true=D33_TRUE,
                 coff_pf=COFF_PF, qv_err=1e-5, noise_v=2e-5)
    res = fc.analyze_trial(fc.Capture(s, 2.5), models, MARKS, 20, 1)
    assert res.load_removal_ratio == pytest.approx(0.6, rel=0.1)
    assert "LOAD_REMOVAL_INCONSISTENT" in res.flags


# ------------------------------------------------------------------------ capture IO
def test_capture_roundtrip_and_hash(tmp_path, models):
    cap = capture_for(20, models)
    path = tmp_path / "trial.npz"
    sha = cap.save(path)
    again = fc.Capture.load(path, sha)
    assert again.routes() == cap.routes() and again.span_volts == 2.5
    np.testing.assert_array_equal(again.samples[REFS[1]]["volts"], cap.samples[REFS[1]]["volts"])
    with pytest.raises(fc.CalibrationError, match="hash"):
        fc.Capture.load(path, "0" * 64)
    assert not list(tmp_path.glob("*.tmp"))


# --------------------------------------------------------------------------- wizard
def run_trial(w, t0=0.0):
    w.begin(t0)
    w.baseline_done(t0 + 2.5)
    assert w.armed and "Place" in w.prompt()
    w.mark_placement(t0 + 3.0)
    w.hand_clear_placement(t0 + 3.7)
    w.hold_done(t0 + 14.0)
    assert w.armed and "Remove" in w.prompt()
    w.mark_removal(t0 + 14.0)
    w.hand_clear_removal(t0 + 14.7)
    w.unloaded_done(t0 + 17.0)
    return w.trial_marks()


def result(mass, repeat, accepted=True, supports=True, reasons=()):
    return fc.TrialResult(mass, repeat, accepted, list(reasons), [], [], {}, {}, None, None, {}, {},
                          supports_mass=supports)


def test_wizard_prompts_every_mass_in_order_with_repeats():
    w = fc.Wizard(repeats=2)
    assert len(w.steps) == 8 and [s.mass_g for s in w.steps][::2] == [20, 50, 100, 200]
    seen = []
    while w.current is not None:
        seen.append((w.current.mass_g, w.current.repeat))
        assert "Select the Array" in w.prompt()
        marks = run_trial(w)
        assert marks.place == 3.0 and marks.remove_clear == 14.7
        w.finish_trial(result(w.current.mass_g, w.current.repeat))
    assert seen == [(20, 1), (20, 2), (50, 1), (50, 2), (100, 1), (100, 2), (200, 1), (200, 2)]
    assert w.phase == fc.FIT and w.covered_masses() == [20, 50, 100, 200]
    assert w.supported_max_g == 200


def test_wizard_enforces_action_order_and_hold_time():
    w = fc.Wizard(repeats=1)
    with pytest.raises(fc.CalibrationError):
        w.mark_placement(1.0)
    w.begin(0.0)
    with pytest.raises(fc.CalibrationError):
        w.hand_clear_placement(1.0)
    w.baseline_done(2.5)
    with pytest.raises(fc.CalibrationError, match="mark the placement first"):
        w.hand_clear_placement(3.0)
    w.mark_placement(3.0)
    w.hand_clear_placement(3.7)
    with pytest.raises(fc.CalibrationError, match="hold lasted"):
        w.hold_done(8.0)
    w.hold_done(13.8)
    with pytest.raises(fc.CalibrationError):
        fc.Wizard(hold_s=5.0)
    with pytest.raises(fc.CalibrationError):
        fc.Wizard(repeats=0)


def test_failed_trials_are_kept_and_can_be_retried_or_skipped():
    w = fc.Wizard(repeats=1)
    run_trial(w)
    w.finish_trial(result(20, 1, accepted=False, reasons=["NO_STABLE_POST_PLACEMENT"]))
    assert w.steps[0].status == fc.FAILED_STEP and "NO_STABLE" in w.steps[0].reason
    assert w.current.mass_g == 50 and w.phase == fc.PREPARE
    w.retry()
    assert w.steps[0].status == fc.PENDING and w.current.mass_g == 20
    run_trial(w)
    w.finish_trial(result(20, 1))
    w.skip_mass(50)
    assert w.skipped_masses() == [50] and w.current.mass_g == 100
    with pytest.raises(fc.CalibrationError):
        w.skip_mass(50)
    assert w.covered_masses() == [20]
    with pytest.raises(fc.CalibrationError):
        fc.Wizard(repeats=1).retry()


def test_clipping_stops_escalation_but_allows_retry_before_restoring_heavier_steps():
    w = fc.Wizard(repeats=2)
    for mass in (20, 20, 50, 50):
        run_trial(w)
        w.finish_trial(result(mass, w.current.repeat))
    run_trial(w)
    w.finish_trial(result(100, 1, accepted=False, supports=False, reasons=["CLIPPED"]))
    assert w.stopped_at_g == 100 and w.current is None and w.phase == fc.FIT
    assert w.supported_max_g == 50 and w.skipped_masses() == [100, 200]
    w.retry()
    assert w.current.mass_g == 100 and w.current.repeat == 1
    assert w.skipped_masses() == [100, 200]
    run_trial(w)
    w.finish_trial(result(100, 1))
    assert w.stopped_at_g is None and w.current.mass_g == 100 and w.current.repeat == 2
    assert w.skipped_masses() == []


def test_cancel_trial_keeps_step_pending():
    w = fc.Wizard(repeats=1)
    w.begin(0.0)
    w.baseline_done(2.5)
    w.cancel_trial()
    assert w.phase == fc.PREPARE and w.current.mass_g == 20 and w.marks == {}


# --------------------------------------------------------------------------- records
def make_key(group="A1_PZT1", signature="a=1", mounting=""):
    return fc.CalibrationKey("board-7", "h" * 64, group, signature, mounting)


def sample_events(gain=7.5e-9):
    ev = []
    for r in (1, 2, 3):
        for m in fc.MASSES_G:
            for s in (+1, -1):
                ev.append(fm.CalEvent("e%d_%d_%d" % (r, m, s), m, s, r, gain * s * m * 1e-3 * G))
    return ev


def make_record(key, previous=None, gain=7.5e-9):
    events = sample_events(gain)
    fit = fm.fit_d33(events)
    val = fm.validate_holdout(events, [3], limit_n=0.05)
    trials = [fc.TrialResult(m, r, True, [], [], [], {}, {}, None, None, {}, {})
              for r in (1, 2, 3) for m in fc.MASSES_G]
    wizard = fc.Wizard(repeats=3)
    for s in wizard.steps:
        s.status = fc.DONE_STEP
    return fc.build_record(key, array=1, pzt="PZT1", d33_initial=6e-9, fit=fit, validation=val,
                           trials=trials, wizard=wizard, polarity=1.0, g=G,
                           manifest_sha256="m" * 64, simulated=True, previous=previous)


def test_record_contents_and_unchanged_initial_value(tmp_path):
    rec = make_record(make_key())
    assert rec["status"] == "validated" and rec["charge_conversion_model"] == fm.CHARGE_MODEL
    assert rec["d33_eff_initial_c_per_n"] == 6e-9
    assert rec["d33_eff_fitted_c_per_n"] == pytest.approx(7.5e-9)
    assert rec["supported_range"]["max_mass_g"] == 200
    assert rec["leakage_status"] == "not_evaluated" and rec["origin"] == "simulated"
    with pytest.raises(fc.CalibrationError):
        fc.build_record(make_key(), array=1, pzt="PZT1", d33_initial=0.0, fit=None, validation=None,
                        trials=[], wizard=None, polarity=1, g=G, manifest_sha256="")
    uncal = fc.build_record(make_key(), array=1, pzt="PZT1", d33_initial=6e-9, fit=None,
                            validation=None, trials=[], wizard=None, polarity=1, g=G,
                            manifest_sha256="")
    assert uncal["status"] == "not_fitted" and uncal["d33_eff_fitted_c_per_n"] is None


def test_groups_are_stored_independently(tmp_path):
    store = fc.CalibrationStore(tmp_path)
    keys = [make_key("A1_PZT1"), make_key("A1_PZT3"), make_key("A2_PZT1"),
            make_key("A1_PZT1", signature="a=2")]
    for i, key in enumerate(keys):
        store.save(make_record(key, gain=(5 + i) * 1e-9))
    assert len({p.name for p in tmp_path.glob("force_cal_*.json")}) == 4
    for i, key in enumerate(keys):
        assert store.load(key)["d33_eff_fitted_c_per_n"] == pytest.approx((5 + i) * 1e-9)
    assert len(store.list()) == 4
    # a new revision of one group leaves the others alone
    old = store.load(keys[0])
    store.save(make_record(keys[0], previous=old, gain=9e-9))
    assert store.load(keys[0])["revision"] == 2
    assert store.load(keys[1])["d33_eff_fitted_c_per_n"] == pytest.approx(6e-9)
    with pytest.raises(fc.CalibrationError, match="newer or equal"):
        store.save(make_record(keys[0], gain=1e-9))


def test_record_tamper_and_strictness(tmp_path):
    store = fc.CalibrationStore(tmp_path)
    path = store.save(make_record(make_key()))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["d33_eff_fitted_c_per_n"] = 1.0
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(fc.CalibrationError, match="hash mismatch"):
        fc.read_record(path)
    path.write_text(path.read_text().replace('"revision": 1', '"revision": NaN'), encoding="utf-8")
    with pytest.raises(fc.CalibrationError, match="strict JSON"):
        fc.read_record(path)
    other = tmp_path / "x.json"
    other.write_text(json.dumps({"format": "x"}), encoding="utf-8")
    with pytest.raises(fc.CalibrationError, match="not a"):
        fc.read_record(other)
    old_model = make_record(make_key("A1_PZT5"))
    old_model["charge_conversion_model"] = "analytic_v0"
    fc.write_record(old_model, tmp_path / "old.json")
    with pytest.raises(fc.CalibrationError, match="not supported"):
        fc.read_record(tmp_path / "old.json")
    assert not list(tmp_path.glob("*.tmp"))


def test_compatibility_reports_stale_records_without_applying():
    rec = make_record(make_key())
    ok = fc.compatibility(rec, board_instance="board-7", layout_hash="h" * 64,
                          capture_signature="a=1", manifest_sha256="m" * 64)
    assert ok == []
    bad = fc.compatibility(rec, board_instance="b2", layout_hash="x", capture_signature="a=2",
                           manifest_sha256="n" * 64)
    assert len(bad) == 4
    rec["status"] = "not_calibrated"
    assert any("not independently validated" in p for p in fc.compatibility(
        rec, board_instance="board-7", layout_hash="h" * 64, capture_signature="a=1"))


def test_end_to_end_two_masses_through_wizard_and_fit(tmp_path, models):
    w = fc.Wizard(masses=(20, 50), repeats=2)
    trials, results = [], []
    seed = 0
    while w.current is not None:
        step = w.current
        marks = run_trial(w)
        seed += 1
        cap = capture_for(step.mass_g, models, seed=seed)
        res = fc.analyze_trial(cap, models, marks, step.mass_g, step.repeat)
        trials.append(res)
        w.finish_trial(res)
    assert all(t.accepted for t in trials), [t.reasons for t in trials]
    events = [e for t in trials for e in t.events]
    fit = fm.fit_d33(events)
    assert fit.d33_eff == pytest.approx(gain_true(models), rel=0.04)
    rec = fc.build_record(make_key(), array=1, pzt="PZT1", d33_initial=5e-9, fit=fit,
                          validation=fm.validate_holdout(events, [2]), trials=trials, wizard=w,
                          polarity=1.0, g=G, manifest_sha256="m" * 64, simulated=True)
    path = fc.CalibrationStore(tmp_path).save(rec)
    assert fc.read_record(path)["supported_range"]["max_mass_g"] == 50
