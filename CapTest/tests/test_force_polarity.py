"""Fixed channel signs align charge contributions without rectifying unloading."""
import copy
from pathlib import Path

import numpy as np
import pytest

from captest import force_model as fm, force_export as fx, force_calibration as fc, force_zero, sensor_groups, force_polarity
from captest.force_evidence import read_json, replay_trial


def mixed_samples(relative_signs=None):
    refs = [(2, k) for k in range(5)]
    signs = {r: (relative_signs[r[1]] if relative_signs is not None else (-1 if r[1] == 2 else 1)) for r in refs}
    models = {r: fm.RouteModel(r, 470e3, 100e-12, 1.25, .08, .01, .001) for r in refs}
    t = np.arange(0., 18., .002)
    tc = np.full(len(t), 1.5e-6)
    force = .05 * fm.STANDARD_GRAVITY * (np.clip((t - 3.) / .15, 0, 1) - np.clip((t - 14.) / .15, 0, 1))
    samples = {}
    for ref, model in models.items():
        v = np.full(len(t), 1.2)
        for k in range(1, len(t)):
            v[k] = fm.predict(model, v[k-1], tc[k]) + .0003 + signs[ref] * .2 * 300e-12 * (force[k] - force[k-1]) / model.c_total
        samples[ref] = {"t": t, "tc": tc, "volts": v}
    return models, samples, signs, force


def quick_samples(pattern=(1, 1, -1, 1, 1), *, release=.72, force_scale=1., noise=0.):
    rng = np.random.default_rng(89)
    refs = [(2, k) for k in range(5)]
    models = {r: fm.RouteModel(r, 470e3, 100e-12, 1.25, .08, .01, .001) for r in refs}
    t = np.arange(0., 2.5, .002)
    force = force_scale * .5 * np.clip((t - .6) / .04, 0, 1)
    if release is not None:
        force -= force_scale * .5 * np.clip((t - release) / .04, 0, 1)
    samples = {}
    for ref, model in models.items():
        v = np.full(len(t), .15)  # include ordinary RC startup/relaxation
        tc = np.full(len(t), 1.5e-6)
        for k in range(1, len(t)):
            v[k] = (fm.predict(model, v[k-1], tc[k]) + .0003 + .00005 * ref[1]
                + pattern[ref[1]] * .2 * 300e-12 * (force[k] - force[k-1]) / model.c_total
                + rng.normal(0, noise))
        samples[ref] = {"t": t, "tc": tc, "volts": v}
    return models, samples


@pytest.mark.parametrize("pattern", [(1, 1, -1, 1, 1), (1, 1, 1, 1, 1), (1, -1, 1, -1, 1), (-1, -1, 1, -1, -1)])
def test_automatic_polarity_needs_no_marks_or_minimum_hold(pattern):
    models, samples = quick_samples(pattern, release=.65, noise=.0001)
    report = force_polarity.detect_press(models, samples)
    assert report["complete"] and report["status"] == "ready_for_review", report
    assert report["windows_s"]["release"][1] < 1.
    assert report["windows_s"]["release"][0] - report["windows_s"]["press"][0] < .1
    majority = 1 if sum(pattern) > 0 else -1
    assert [report["routes"]["2:%d" % i]["suggested_sign"] for i in range(5)] == [p * majority for p in pattern]


def test_automatic_polarity_does_not_treat_rc_relaxation_or_noise_as_release():
    models, samples = quick_samples(release=None)
    held = force_polarity.detect_press(models, samples)
    assert held["phase"] == "pressed" and not held["complete"]
    models, samples = quick_samples(force_scale=0, noise=.0003)
    quiet = force_polarity.detect_press(models, samples)
    assert quiet["phase"] == "listening" and not quiet["complete"]
    for d in samples.values():
        d["volts"][300] += .02  # one brief common spike must not become a complete pair
    assert not force_polarity.detect_press(models, samples)["complete"]


@pytest.mark.parametrize("code", [0, 8, 4089, 4095])
def test_automatic_polarity_allows_clipping_as_a_note_but_not_weak_signs(code):
    models, samples = quick_samples((1, 1, 0, 1, 1))
    weak = force_polarity.detect_press(models, samples)
    assert weak["complete"] and weak["status"] == "unclear"
    assert weak["routes"]["2:2"]["suggested_sign"] is None
    models, samples = quick_samples()
    samples[(2, 2)]["volts"][320] = code * 2.5 / 4096
    clipped = force_polarity.detect_press(models, samples)
    assert clipped["complete"] and clipped["status"] == "ready_for_review", clipped
    row = clipped["routes"]["2:2"]
    assert row["suggested_sign"] == -1 and not row["reasons"]
    saturated = code in (0, 4095)
    assert row["range"]["saturated_sample_count"] == int(saturated)
    assert any(("ADC clipped" if saturated else "headroom") in note for note in row["notes"])
    assert clipped["clipping_policy"] == "note_only"


def test_automatic_polarity_still_blocks_nonfinite_input():
    models, samples = quick_samples()
    samples[(2, 2)]["volts"][330] = np.nan
    report = force_polarity.detect_press(models, samples)
    assert report["complete"] and report["status"] == "unclear"
    assert report["routes"]["2:2"]["suggested_sign"] is None
    assert "non-finite voltage" in report["routes"]["2:2"]["reasons"]


def test_automatic_capture_stops_without_weight_stages_and_preserves_plan_and_raw_evidence(tmp_path):
    from test_force_session import make_rig, make_controller, scalar_force
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm, noise=0)
    ctl = make_controller(gm, rig, tmp_path)
    before = copy.deepcopy(ctl.wizard.__dict__)
    rig[1].scan.d33_true = 600e-12
    rig[1].scan.force = scalar_force(50, t_place=.8, t_remove=1.05)
    ctl.start_trial(polarity_test=True)
    for _ in range(100000):
        ctl.tick()
        if ctl.session is None:
            break
    assert ctl.session is None
    assert ctl.wizard.__dict__ == before and not ctl.trials
    assert ctl.reference_kind == "known_mass" and ctl.fit is None
    report = ctl.check_press_polarity()
    assert report["status"] == "ready_for_review", report
    assert ctl.last_polarity_result.duration_s < 3.
    assert ctl.last_weight_result is None
    body, raw, trial, models = replay_trial(report["sidecar"])
    assert raw.valid and trial is None and body["purpose"] == "polarity_test"
    fresh = force_polarity.detect_press(models, raw.samples)
    assert fresh["windows_s"] == report["windows_s"]
    assert [r["suggested_sign"] for r in fresh["routes"].values()] == [r["suggested_sign"] for r in report["routes"].values()]
    assert Path(report["capture_file"]).exists() and Path(report["capture_file"]).with_suffix(".npz").exists()


def test_automatic_capture_timeout_and_cancel_do_not_consume_weight_trials(tmp_path, monkeypatch):
    from test_force_session import make_rig, make_controller
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm, noise=0)
    ctl = make_controller(gm, rig, tmp_path)
    monkeypatch.setattr(force_polarity, "AUTO_TIMEOUT_S", 1.)
    rig[1].scan.force = lambda t: 0.
    before = copy.deepcopy(ctl.wizard.__dict__)
    ctl.start_trial(polarity_test=True)
    for _ in range(100000):
        ctl.tick()
        if ctl.session is None:
            break
    assert ctl.last_polarity_check["status"] == "unclear" and not ctl.trials
    assert "no clear" in ctl.last_polarity_check["reasons"][0]
    ctl.start_trial(polarity_test=True)
    ctl.tick()
    ctl.cancel_trial()
    assert ctl.wizard.__dict__ == before
    assert "cancelled" in ctl.last_polarity_check["reasons"][0]
    rig[1].scan.faults.overrun_after_sweep = 30
    rig[1].scan.faults.overrun_count = 1
    ctl.start_trial(polarity_test=True)
    for _ in range(100000):
        ctl.tick()
        if ctl.session is None:
            break
    assert ctl.session is None and not ctl.last_polarity_result.valid
    assert ctl.last_polarity_check["status"] == "unclear"
    assert all(row["suggested_sign"] is None for row in ctl.last_polarity_check["routes"].values())
    assert ctl.wizard.__dict__ == before


def test_automatic_polarity_capture_keeps_existing_force_fit_and_trial_evidence(tmp_path):
    from test_force_session import make_rig, make_controller, drive_trial, scalar_force
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm, noise=0)
    ctl = make_controller(gm, rig, tmp_path)
    drive_trial(ctl, rig[1])
    drive_trial(ctl, rig[1])
    fitted = ctl.fit_gain()
    previous = [trial.to_json() for trial in ctl.trials]
    basis = ctl._fit_basis
    rig[1].scan.d33_true = 600e-12
    rig[1].scan.force = scalar_force(50, t_place=.8, t_remove=1.05)
    ctl.start_trial(polarity_test=True)
    for _ in range(100000):
        ctl.tick()
        if ctl.session is None:
            break
    assert ctl.session is None
    assert ctl.fit is fitted and ctl._fit_basis == basis
    assert [trial.to_json() for trial in ctl.trials] == previous


@pytest.mark.parametrize("pattern", [(1, 1, -1, 1, 1), (1, 1, 1, 1, 1), (1, -1, 1, -1, 1), (-1, -1, 1, -1, -1)])
def test_press_check_discovers_actual_channel_signs_without_assuming_centre_is_reversed(pattern):
    models, samples, signs, _ = mixed_samples(pattern)
    before = copy.deepcopy(samples)
    marks = fc.Marks((.3, 2.5), 3., 3.7, 14., 14.7)
    report = force_polarity.check_press(models, samples, marks)
    assert report["status"] == "ready_for_review"
    majority = 1 if sum(pattern) > 0 else -1
    for ref in models:
        row = report["routes"]["%d:%d" % ref]
        assert row["suggested_sign"] == signs[ref] * majority
        assert row["press_q_c"] * row["release_q_c"] < 0
        np.testing.assert_array_equal(samples[ref]["volts"], before[ref]["volts"])


def test_marked_polarity_allows_clipping_as_a_note_but_not_weak_signs():
    marks = fc.Marks((.3, 2.5), 3., 3.7, 14., 14.7)
    models, samples, _, _ = mixed_samples((1, 1, 0, 1, 1))
    report = force_polarity.check_press(models, samples, marks)
    assert report["status"] == "unclear"
    assert report["routes"]["2:2"]["suggested_sign"] is None
    models, samples, _, _ = mixed_samples()
    samples[(2, 2)]["volts"][2500] = 4095 * 2.5 / 4096
    report = force_polarity.check_press(models, samples, marks)
    assert report["status"] == "ready_for_review", report
    assert report["routes"]["2:2"]["suggested_sign"] == -1
    assert any("ADC clipped" in note for note in report["routes"]["2:2"]["notes"])


def test_conflicting_release_cannot_define_a_polarity():
    models, samples, _, _ = mixed_samples()
    ref = (2, 0)
    data = samples[ref]
    residual = fm.residuals(models[ref], data["volts"], data["tc"])
    for k in range(1, len(residual)):
        e = .0006 - residual[k] if data["t"][k] >= 14 else residual[k]
        data["volts"][k] = fm.predict(models[ref], data["volts"][k-1], data["tc"][k]) + e
    report = force_polarity.check_press(models, samples, fc.Marks((.3, 2.5), 3., 3.7, 14., 14.7))
    assert report["status"] == "unclear"
    assert report["routes"]["2:0"]["suggested_sign"] is None
    assert any("opposite" in s for s in report["routes"]["2:0"]["reasons"])


def test_controller_checks_recorded_routes_without_modifying_signs_or_raw_evidence(tmp_path):
    from test_force_session import make_rig, make_controller, drive_trial
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path, reference_kind="workflow_only")
    drive_trial(ctl, rig[1])
    before = Path(ctl.trials[-1].capture_file).read_bytes()
    signs = ctl.route_polarity.copy()
    report = ctl.check_press_polarity()
    assert report["status"] == "ready_for_review", report
    assert report["group_id"] == ctl.group.group_id
    assert report["capture_sha256"] == ctl.trials[-1].capture_sha256
    assert ctl.route_polarity == signs and ctl.fit is None
    assert Path(ctl.trials[-1].capture_file).read_bytes() == before


def test_relative_centre_polarity_restores_total_and_preserves_negative_removal():
    models, samples, signs, force = mixed_samples()
    bias = {r: .0003 for r in models}
    raw = fm.accumulate(models, samples, bias)
    aligned = fm.accumulate(models, samples, bias, route_polarity=signs)
    expected = force.max() * 300e-12
    assert raw.q_at(10.) == pytest.approx(.6 * expected, abs=1e-20)
    assert aligned.q_at(10.) == pytest.approx(expected, abs=1e-20)
    assert aligned.q_at(17.) == pytest.approx(0., abs=1e-20)
    for ref, (t, q) in aligned.per_route_q.items():
        assert q[np.searchsorted(t, 10.)] > 0
        assert q[np.searchsorted(t, 17.)] - q[np.searchsorted(t, 10.)] < 0
    flipped = fm.accumulate(models, samples, bias, polarity=-1, route_polarity=signs)
    np.testing.assert_allclose(flipped.q_proxy, -aligned.q_proxy, atol=1e-20)
    with pytest.raises(fm.ModelError, match="relative polarity"):
        fm.accumulate(models, samples, bias, route_polarity={(2, 2): 0})


def test_signed_drift_rates_and_trial_analysis_use_the_same_coordinates():
    models, samples, signs, force = mixed_samples()
    rates = {r: signs[r] * models[r].c_total * .0003 / .002 for r in models}
    charge = fm.accumulate(models, samples, drift_rate=rates, route_polarity=signs)
    assert charge.q_at(10.) == pytest.approx(force.max() * 300e-12, abs=1e-20)
    assert charge.q_at(17.) == pytest.approx(0., abs=1e-20)
    marks = fc.Marks((.3, 2.5), 3., 3.7, 14., 14.7)
    trial = fc.analyze_trial(fc.Capture(samples), models, marks, 50, 1,
        fixed_drift=rates, route_polarity=signs)
    assert trial.accepted, trial.reasons
    assert "UNLOADED_DRIFT_SHIFT" not in trial.flags
    assert trial.events[0].plain_delta_q == pytest.approx(force.max() * 300e-12, abs=1e-20)
    assert trial.events[1].plain_delta_q == pytest.approx(-force.max() * 300e-12, abs=1e-20)


def test_polarity_is_saved_per_array_and_group_without_changing_electrical_evidence(tmp_path):
    from test_force_hardening import inputs_with_bad_route
    inputs, gm, group = inputs_with_bad_route()
    original = copy.deepcopy(inputs.data["packages"])
    electrical = inputs.electrical_hash()
    inputs.set_d33_eff(group.group_id, 300e-12, {"status": "validated"})
    values = {p: (-1 if p == "C" else 1) for p in gm.positions}
    inputs.set_relative_polarity(group.group_id, values)
    assert inputs.data["packages"] == original and inputs.electrical_hash() == electrical
    assert inputs.data["force_inputs"]["d33_eff_c_per_n"][group.group_id]["status"] == "stale"
    assert inputs.relative_polarity("A2_PZT1")["C"] == 1
    assert inputs.relative_polarity("A1_PZT6")["C"] == 1
    path = tmp_path / "relative.json"
    inputs.save(path)
    loaded = fx.ForceInputs.load(path, gm)
    assert loaded.relative_polarity(group.group_id) == values
    assert loaded.route_polarity(group.group_id)[group.refs[2]] == -1
    with pytest.raises(fx.ForceExportError, match="every position"):
        inputs.set_relative_polarity(group.group_id, {"C": -1})


def test_polarity_change_clears_corrections_and_fit_but_replays_old_and_new_signs(tmp_path):
    from test_force_session import make_rig, make_controller, drive_trial
    from test_force_zero import measure
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path)
    measure(ctl)
    drive_trial(ctl, rig[1]); drive_trial(ctl, rig[1])
    ctl.fit_gain()
    old_signature = ctl.signature()
    old = copy.deepcopy(ctl.trials[0])
    raw = Path(old.capture_file).read_bytes()
    values = {r.position: (-1 if r.position == "C" else 1) for r in ctl.group.routes}
    ctl.forces.set_relative_polarity(ctl.group.group_id, values)
    ctl.set_route_polarity(ctl.forces.route_polarity(ctl.group.group_id))
    assert ctl.unloaded_correction is None and ctl.fit is None and ctl.validation is None
    assert ctl.signature() != old_signature and ctl.key.capture_signature == ctl.signature()
    assert ctl.trials[0].analysis_revision == 2
    assert Path(old.capture_file).read_bytes() == raw
    evidence = read_json(old.sidecar)
    assert evidence["analyses"][0]["route_polarity"]["2:2"] == 1
    assert evidence["analyses"][-1]["route_polarity"]["2:2"] == -1
    _, _, first, _ = replay_trial(old.sidecar, 0)
    _, _, revised, _ = replay_trial(old.sidecar)
    assert [e.delta_q for e in first.events] == pytest.approx([e.delta_q for e in old.events])
    assert [e.delta_q for e in revised.events] == pytest.approx([e.delta_q for e in ctl.trials[0].events])
    assert ctl.build_record()["route_polarity"]["2:2"] == -1


def test_unloaded_linear_fit_learns_drift_after_each_channel_sign():
    models, samples, signs, _ = mixed_samples()
    quiet = {}
    for ref, data in samples.items():
        v = np.full(len(data["t"]), 1.2)
        for k in range(1, len(v)):
            v[k] = fm.predict(models[ref], v[k-1], data["tc"][k]) + .0003
        quiet[ref] = {**data, "volts": v}
    # The standalone bias diagnostics acquire signs only at charge conversion.
    result = force_zero.analyse(models, quiet, duration_s=10, model_basis="test", signature="scan",
                                route_polarity=signs)
    assert result["routes"]["2:2"]["bias_v"] > 0
    assert force_zero.bias_values(result)[(2, 2)] > 0  # raw residual bias itself is not inverted
    linear = force_zero.analyse_linear(models, quiet, end_s=12, window_s=10,
        model_basis="test", signature="scan", max_rate_error_c_per_s=1e-12, route_polarity=signs)
    assert linear["status"] == "qualified"
    assert force_zero.drift_values(linear)[(2, 2)] < 0
    assert force_zero.drift_values(linear)[(2, 0)] > 0
    q = fm.accumulate(models, quiet, drift_rate=force_zero.drift_values(linear), route_polarity=signs)
    assert np.ptp(q.q_proxy) < 1e-20
