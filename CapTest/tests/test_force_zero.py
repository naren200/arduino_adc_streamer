"""Unloaded correction must remove constant bias without erasing held force."""
import copy
from pathlib import Path

import numpy as np
import pytest

from captest import force_zero as zero, force_model as fm, force_calibration as fc, sensor_groups
from captest.force_evidence import read_json, replay_trial


def synthetic(duration=10., bias=.0005, changing=False, with_weight=False):
    model = fm.RouteModel((2, 0), 470e3, 100e-12, 1.25, .08, .01, .001, 0., 1.2)
    t = np.arange(0., duration + .001, .002)
    tc = np.full(len(t), 1.5e-6)
    f = np.zeros(len(t))
    if with_weight:
        f = .05 * fm.STANDARD_GRAVITY * (np.clip((t - 3.) / .15, 0, 1) - np.clip((t - 14.) / .15, 0, 1))
    voltage = np.zeros(len(t))
    voltage[0] = 1.2
    for k in range(1, len(t)):
        e = bias + (.0004 if changing and t[k] > 6 else 0.)
        voltage[k] = fm.predict(model, voltage[k-1], tc[k]) + e + 300e-12 * (f[k] - f[k-1]) / model.c_total
    return {model.ref: model}, {model.ref: {"t": t, "volts": voltage, "tc": tc}}


def test_constant_unloaded_bias_is_removed_from_charge_increments():
    models, samples = synthetic()
    result = zero.analyse(models, samples, duration_s=10, model_basis="model", signature="scan")
    assert result["status"] == "qualified"
    assert result["routes"]["2:0"]["bias_v"] == pytest.approx(.0005)
    assert result["check_raw_q_rate_c_per_s"] > 2e-11
    assert abs(result["check_corrected_q_rate_c_per_s"]) < 1e-22
    q = fm.accumulate(models, samples, zero.bias_values(result))
    assert np.ptp(q.q_proxy) < 1e-20


def test_changing_unloaded_signal_cannot_be_silently_fitted_as_constant_leakage():
    models, samples = synthetic(changing=True)
    result = zero.analyse(models, samples, duration_s=10, model_basis="model", signature="scan")
    assert result["status"] == "rejected" and "still changing" in result["reasons"][0]


def test_discarded_startup_voltage_does_not_reject_quiet_window_but_its_input_does():
    models, samples = synthetic()
    data = next(iter(samples.values()))
    data["volts"][0] = .001  # outside headroom and model range, discarded before settling
    result = zero.analyse(models, samples, duration_s=10, model_basis="model", signature="scan")
    assert result["status"] == "qualified", result["reasons"]
    first = np.flatnonzero(data["t"] >= 2.)[0]
    data["volts"][first - 1] = .001  # predictor input to the first retained residual
    result = zero.analyse(models, samples, duration_s=10, model_basis="model", signature="scan")
    assert result["status"] == "rejected"
    assert any("headroom" in reason for reason in result["reasons"])


def test_frozen_unloaded_correction_preserves_loading_hold_and_removal():
    models, unloaded = synthetic()
    correction = zero.analyse(models, unloaded, duration_s=10, model_basis="model", signature="scan")
    _, loaded = synthetic(duration=18, with_weight=True)
    marks = fc.Marks((.3, 2.5), 3., 3.7, 14., 14.7)
    trial = fc.analyze_trial(fc.Capture(loaded), models, marks, 50, 1,
        fixed_bias=zero.bias_values(correction), bias_source="zero-test")
    assert trial.accepted, trial.reasons
    expected = .05 * fm.STANDARD_GRAVITY * 300e-12
    assert trial.events[0].plain_delta_q == pytest.approx(expected, rel=1e-8)
    assert trial.events[1].plain_delta_q == pytest.approx(-expected, rel=1e-8)
    assert abs(trial.hold_drift_c_per_s) < 1e-20
    assert trial.bias["2:0"]["source"] == "unloaded_drift"
    assert trial.bias["2:0"]["bias_v"] == pytest.approx(.0005)
    changed = copy.deepcopy(loaded)
    # The trial baseline must diagnose a shift without changing the frozen correction.
    shifted = fc.analyze_trial(fc.Capture(changed), models, marks, 50, 1,
        fixed_bias={(2, 0): .0003})
    assert "UNLOADED_DRIFT_SHIFT" in shifted.flags
    assert shifted.bias["2:0"]["bias_v"] == .0003


def measure(ctl, duration=6):
    ctl.start_trial(unloaded_duration_s=duration)
    for _ in range(100000):
        ctl.tick()
        if ctl.session is None:
            return
    raise AssertionError("unloaded measurement did not finish")


def test_controller_unloaded_stage_preserves_plan_raw_evidence_and_replay(tmp_path):
    from test_force_session import make_rig, make_controller, drive_trial, advance_to
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path)
    measure(ctl)
    assert ctl.last_unloaded["status"] == "qualified", ctl.last_unloaded
    correction = copy.deepcopy(ctl.unloaded_correction)
    assert ctl.trials == [] and ctl.wizard.current.mass_g == 50
    sidecar = Path(correction["sidecar"])
    assert read_json(sidecar)["state"] == "unloaded_qualified"
    assert sidecar.with_suffix(".json").exists()
    rig[1].scan.force = lambda t: 0.
    ctl.start_trial()
    advance_to(ctl, .5)
    view = ctl.live_view()
    assert np.all(np.isfinite(view.force_n))  # already corrected from the beginning
    assert ctl._live_bias(ctl.session.assembler.live_samples()[0], {}) == zero.bias_values(correction)
    ctl.cancel_trial()
    assert ctl.unloaded_correction == correction
    drive_trial(ctl, rig[1])
    trial = ctl.trials[-1]
    assert all(b["source"] == "unloaded_drift" for b in trial.bias.values())
    raw = Path(trial.capture_file).read_bytes()
    _, _, replayed, _ = replay_trial(trial.sidecar)
    assert replayed.accepted == trial.accepted and replayed.flags == trial.flags
    assert [e.delta_q for e in replayed.events] == pytest.approx([e.delta_q for e in trial.events])
    ctl.clear_unloaded_correction()
    assert ctl.unloaded_correction is None and ctl.fit is None and ctl.validation is None
    assert ctl.trials[-1].analysis_revision == 2
    assert Path(trial.capture_file).read_bytes() == raw
    rig[1].scan.force = lambda t: 0.
    measure(ctl)
    assert ctl.last_unloaded["status"] == "qualified"
    assert ctl.last_unloaded_result.duration_s < 7
    assert ctl.last_weight_result.duration_s > 15  # later zero captures must not replace the weight plot's samples
    assert Path(trial.capture_file).read_bytes() == raw


def test_cancelled_or_bad_unloaded_measurement_keeps_previous_correction(tmp_path):
    from test_force_session import make_rig, make_controller, advance_to
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path)
    measure(ctl)
    old = copy.deepcopy(ctl.unloaded_correction)
    ctl.start_trial(unloaded_duration_s=6)
    advance_to(ctl, .5)
    ctl.cancel_trial()
    assert ctl.unloaded_correction == old and ctl.trials == []
    rig[1].scan.faults.overrun_after_sweep = 30
    rig[1].scan.faults.overrun_count = 1
    measure(ctl)
    assert ctl.last_unloaded["status"] == "rejected"
    assert ctl.unloaded_correction == old and ctl.wizard.current.mass_g == 50


def test_leakage_predictor_change_invalidates_unloaded_bias(tmp_path):
    from test_force_session import make_rig, make_controller
    gm = sensor_groups.resolve("TestBoard_7953")
    ctl = make_controller(gm, make_rig(gm), tmp_path)
    measure(ctl)
    assert ctl.unloaded_correction
    ctl.leakage = fm.LeakageFit("identified", fm.LeakageModel(.999, None, "effective"), "test", 20, 0, {}, {})
    ctl.apply_leakage()
    assert ctl.unloaded_correction is None


def test_startup_plot_waits_for_a_bias_estimate_instead_of_showing_raw_drift(tmp_path):
    from test_force_session import make_rig, make_controller, advance_to
    gm = sensor_groups.resolve("TestBoard_7953")
    ctl = make_controller(gm, make_rig(gm), tmp_path)
    ctl.start_trial()
    advance_to(ctl, .5)
    assert np.all(np.isnan(ctl.live_view().force_n))
    advance_to(ctl, 3.)
    assert np.all(np.isfinite(ctl.live_view().force_n))
    ctl.cancel_trial()


def test_linear_section_search_rejects_startup_curve_and_checks_later_data():
    model = fm.RouteModel((2, 0), 470e3, 100e-12, 1.25, .08, .01, .001, 0., 1.2)
    t = np.arange(0., 35., .002)
    tc = np.full(len(t), 1.5e-6)
    v = np.full(len(t), 1.2)
    # Initially downward curvature, then a constant positive raw charge drift.
    bias = .0002 - .001 * np.exp(-t / 3.)
    for k in range(1, len(t)):
        v[k] = fm.predict(model, v[k-1], tc[k]) + bias[k]
    samples = {model.ref: {"t": t, "volts": v, "tc": tc}}
    args = dict(window_s=10, model_basis="model", signature="scan", max_rate_error_c_per_s=1e-12)
    early = zero.analyse_linear({model.ref: model}, samples, end_s=12, **args)
    late = zero.analyse_linear({model.ref: model}, samples, end_s=30, **args)
    assert early["status"] == "rejected"
    assert late["status"] == "qualified" and late["fit_window_s"] == (20, 25)
    assert late["correction_kind"] == "charge_rate"
    assert late["check_raw_q_rate_c_per_s"] > 0
    assert abs(late["check_corrected_q_rate_c_per_s"]) < 1e-12


def test_charge_rate_correction_uses_actual_visit_time_and_preserves_force():
    model = fm.RouteModel((2, 0), 470e3, 100e-12, 1.25, .08, .01, .001)
    dt = np.resize([.001, .003, .002], 3000)
    t = np.cumsum(dt)
    tc = np.full(len(t), 1.5e-6)
    rate = 20e-12
    v = np.full(len(t), 1.2)
    added = np.zeros(len(t)); added[1500] = 10e-12
    for k in range(1, len(t)):
        v[k] = fm.predict(model, v[k-1], tc[k]) + (rate * dt[k] + added[k]) / model.c_total
    samples = {model.ref: {"t": t, "volts": v, "tc": tc}}
    q = fm.accumulate({model.ref: model}, samples, drift_rate={model.ref: rate})
    assert abs(q.q_proxy[1499]) < 1e-20
    assert q.q_proxy[-1] == pytest.approx(10e-12, abs=1e-20)


def test_continuous_drift_pretest_arms_weight_without_restarting_capture(tmp_path):
    from test_force_session import make_rig, make_controller, advance_to, scalar_force
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm, noise=0)
    ctl = make_controller(gm, rig, tmp_path)
    ctl.start_trial(unloaded_duration_s=6, linear_pretest=True)
    original_session = ctl.session
    for _ in range(100000):
        ctl.tick()
        if ctl._unloaded_duration_s is None:
            break
    assert ctl.session is original_session
    assert ctl.last_unloaded["status"] == "qualified"
    assert ctl.unloaded_correction["correction_kind"] == "charge_rate"
    assert ctl.wizard.phase == fc.BASELINE and ctl.trials == []
    origin = ctl.wizard.marks["capture_start"]
    assert origin >= 8
    advance_to(ctl, origin + ctl.baseline_s + .05)
    assert ctl.wizard.phase == fc.PLACE_ARMED
    place = ctl.now + .1
    clear = place + .7
    remove = clear + ctl.wizard.hold_s + .4
    rig[1].scan.force = scalar_force(50, t_place=place, t_remove=remove)
    advance_to(ctl, place + .05); ctl.mark_placement(place)
    advance_to(ctl, clear + .05); ctl.hand_clear_placement(clear)
    advance_to(ctl, remove + .05); ctl.mark_removal(remove)
    advance_to(ctl, remove + .75); ctl.hand_clear_removal(remove + .7)
    for _ in range(100000):
        ctl.tick()
        if ctl.session is None:
            break
    trial = ctl.trials[-1]
    assert trial.accepted, trial.reasons
    assert trial.marks.baseline[0] > origin
    evidence = read_json(trial.sidecar)
    assert evidence["pretest_end_s"] == origin and evidence["unloaded_measurement"]["status"] == "qualified"
    assert all("drift_rate_c_per_s" in b for b in trial.bias.values())
    _, capture, again, _ = replay_trial(trial.sidecar)
    assert capture.valid and again.accepted == trial.accepted
    assert [e.delta_q for e in again.events] == pytest.approx([e.delta_q for e in trial.events])
    assert abs(trial.events[0].delta_q) > 1e-12  # the loading step survives drift correction
    zero.verify_source(ctl.unloaded_correction)
    frozen = copy.deepcopy(ctl.unloaded_correction)
    rig[1].scan.force = lambda t: 0.
    ctl.start_trial()
    assert ctl._checking_frozen_drift
    for _ in range(100000):
        ctl.tick()
        if ctl.wizard.phase == fc.PLACE_ARMED or ctl.session is None:
            break
    assert ctl.session is not None and ctl.wizard.phase == fc.PLACE_ARMED
    assert ctl.unloaded_correction == frozen  # later weights check the same rate, never refit it
    assert ctl._evidence["frozen_drift_verification"]["status"] == "qualified"
    ctl.cancel_trial()


def test_unstable_continuous_pretest_times_out_without_fitting_the_transient(tmp_path, monkeypatch):
    from test_force_session import make_rig, make_controller
    from captest import force_session as fs
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm, noise=0)
    ctl = make_controller(gm, rig, tmp_path)
    measure(ctl)
    previous = copy.deepcopy(ctl.unloaded_correction)
    monkeypatch.setattr(fs, "LINEAR_SETTLE_TIMEOUT_S", 8.)
    monkeypatch.setattr(zero, "analyse_linear", lambda *a, **kw: {
        "status": "rejected", "reasons": ["slope changing"], "routes": {}})
    ctl.start_trial(unloaded_duration_s=6, linear_pretest=True)
    assert "Waiting for enough quiet samples" in ctl.drift_precheck_message()
    assert "Waiting for enough quiet samples" in ctl.drift_precheck_message()
    for _ in range(100000):
        ctl.tick()
        if ctl.session is None:
            break
    assert ctl.session is None and ctl.last_unloaded["status"] == "rejected"
    assert ctl.unloaded_correction == previous and ctl.trials == []
    assert ctl.wizard.current.mass_g == 50


def test_recording_prefix_is_valid_after_append_but_detects_changed_training_data(tmp_path):
    from captest.capture import ChunkRecorder, ScanConfig
    recorder = ChunkRecorder(tmp_path / "continuous.cap", ScanConfig(((2, 0),)))
    recorder.write(b"training evidence")
    length, sha = recorder.snapshot_hash()
    record = {"capture_file": str(recorder.path), "capture_prefix_bytes": length, "capture_prefix_sha256": sha}
    recorder.write(b"later weight evidence")
    recorder.close()
    zero.verify_source(record)
    data = recorder.path.read_bytes()
    recorder.path.write_bytes(b"X" + data[1:])
    with pytest.raises(fm.ModelError, match="source recording hash changed"):
        zero.verify_source(record)


def test_new_stable_slope_must_not_replace_the_frozen_drift_during_later_trials(tmp_path, monkeypatch):
    from test_force_session import make_rig, make_controller
    from captest import force_session as fs
    gm = sensor_groups.resolve("TestBoard_7953")
    ctl = make_controller(gm, make_rig(gm, noise=0), tmp_path)
    ctl.start_trial(unloaded_duration_s=6, linear_pretest=True)
    for _ in range(100000):
        ctl.tick()
        if ctl._unloaded_duration_s is None:
            break
    ctl.cancel_trial()
    frozen = copy.deepcopy(ctl.unloaded_correction)
    original = zero.analyse_linear
    def different_stable_line(*args, **kwargs):
        record = original(*args, **kwargs)
        for row in record["routes"].values():
            row["q_rate_c_per_s"] += 1e-9
            row["check_q_rate_c_per_s"] += 1e-9
        return record
    monkeypatch.setattr(zero, "analyse_linear", different_stable_line)
    monkeypatch.setattr(fs, "LINEAR_SETTLE_TIMEOUT_S", 8.)
    ctl.start_trial()
    for _ in range(100000):
        ctl.tick()
        if ctl.session is None:
            break
    assert ctl.session is None and ctl.wizard.phase == fc.PREPARE
    assert ctl.unloaded_correction == frozen
    assert any("does not match the frozen correction" in r for r in ctl.last_unloaded["reasons"])
    assert ctl.trials == [] and ctl.wizard.current.mass_g == 50
