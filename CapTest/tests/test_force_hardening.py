"""Independent regression checks for evidence, fallback and force-quality decisions."""
import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from captest import capfit, force_estimates as estimates, force_export as fx
from captest import force_model as fm, force_calibration as fc, sensor_groups
from captest.capture import ChunkRecorder, ScanConfig, replay_file
from captest.force_evidence import read_json
from captest.settings import Settings


def inputs_with_bad_route():
    gm = sensor_groups.resolve("TestBoard_7953")
    settings = Settings()
    inputs = fx.ForceInputs.new(gm, settings, board_instance="test", simulated=True)
    group = gm.get("A1_PZT1")
    routes = []
    for i, ref in enumerate(group.refs):
        blank = [10, 18, 15, 20, 25][i]
        routes.append({"ref": list(ref), "position": group.routes[i].position, "status": "usable",
            "reason": "", "provenance": "measured", "flags": ["HIGH_RESIDUAL"] if i == 1 else [],
            "r_bias_ohm": 470e3, "c_total_f": (65 + blank) * 1e-12,
            "tau_s": 470e3 * (65 + blank) * 1e-12, "v_d_v": 1.249,
            "c0": .08, "c1_per_v": .02, "qv_v": .001,
            "d_min_v": .007, "d_max_v": 1.1, "residual_rms_lsb": 8 if i == 1 else 1,
            "per_burst": [], "baseline": {"c_board_pf": blank, "method": capfit.C_TOTAL_VMID_CYCLES,
                                           "flags": "", "note": ""}})
    inputs.data["packages"][group.group_id] = [{"revision": 1, "status": "complete", "routes": routes,
                                              "superseded": False, "settings": json.loads(settings.to_json())}]
    inputs._refresh()
    return inputs, gm, group


def proposal(inputs, group, **kwargs):
    return estimates.estimate_route(inputs, group.group_id, group.refs[1],
        [(group.group_id, r) for r in group.refs if r != group.refs[1]],
        sensor_class="same measured geometry", wiring_class="same strip/flex", boundary="board connector",
        borrow_model=True, **kwargs)


def test_high_residual_blocks_force_readiness_even_when_coverage_is_complete():
    inputs, _, group = inputs_with_bad_route()
    assert inputs.status(group.group_id) == "complete"
    assert group.group_id not in inputs.ready_groups()
    with pytest.raises(fx.ForceExportError, match="HIGH_RESIDUAL"):
        inputs.route_models(group.group_id)


def test_negative_effective_c0_alone_is_not_an_automatic_quality_failure():
    inputs, _, group = inputs_with_bad_route()
    route = inputs.latest(group.group_id)["routes"][0]
    route["c0"] = -.001
    assert fx.route_quality(route) == []


def test_matching_blank_median_recovers_target_without_double_counting_and_keeps_evidence():
    inputs, _, group = inputs_with_bad_route()
    original = copy.deepcopy(inputs.latest(group.group_id))
    p = proposal(inputs, group)
    # True sensor-plus-strip is 65 pF, target board blank is 18 pF.
    assert p["c_total_f"] == pytest.approx(83e-12)
    assert p["estimate"]["c_added_mad_f"] == pytest.approx(0, abs=1e-25)
    estimates.apply_estimate(inputs, group.group_id, p)
    assert inputs.revisions(group.group_id)[0]["routes"] == original["routes"]
    assert inputs.model_provenance(group.group_id) == "estimated"
    assert not inputs.quality_issues(group.group_id)
    assert inputs.route_models(group.group_id)[group.refs[1]].c_total == pytest.approx(83e-12)
    inputs.latest(group.group_id)["routes"][0]["c_total_f"] *= 1.1
    assert any("donor" in x for x in inputs.quality_issues(group.group_id))


def test_missing_target_blank_bad_donor_or_changed_class_cannot_make_a_confident_estimate():
    inputs, _, group = inputs_with_bad_route()
    target = inputs.latest(group.group_id)["routes"][1]
    target["baseline"]["c_board_pf"] = None
    with pytest.raises(fx.ForceExportError, match="blank"):
        proposal(inputs, group)
    target["baseline"]["c_board_pf"] = 18
    inputs.latest(group.group_id)["routes"][0]["classification"] = {
        "sensor_class": "different size", "wiring_class": "same strip/flex"}
    with pytest.raises(fx.ForceExportError, match="classification"):
        proposal(inputs, group)


def test_optional_borrowing_is_explicit_when_target_raw_transitions_are_unavailable():
    inputs, _, group = inputs_with_bad_route()
    with pytest.raises(fx.ForceExportError, match="raw transitions"):
        estimates.estimate_route(inputs, group.group_id, group.refs[1],
            [(group.group_id, group.refs[0])], sensor_class="size", wiring_class="strip", boundary="connector")


def test_estimated_capacitance_can_constrain_a_refit_of_original_target_packets():
    from test_force_export import make_session
    session, _ = make_session()
    gm = sensor_groups.resolve("TestBoard_7953")
    group = gm.get("A1_PZT1")
    for ref in group.refs:
        session.measure_channel(ref, bursts=2)
    inputs = fx.ForceInputs.new(gm, session.settings, simulated=True)
    inputs.add_package(group, session.results, session.settings, gm, simulated=True)
    rev = inputs.latest(group.group_id)
    common_added = min(r["c_total_f"] for r in rev["routes"]) / 2
    for r in rev["routes"]:
        r["baseline"] = {"c_board_pf": (r["c_total_f"] - common_added) * 1e12,
                         "method": capfit.C_TOTAL_VMID_CYCLES, "note": "", "flags": ""}
    target = rev["routes"][1]
    target["flags"].append("HIGH_RESIDUAL")  # fit excluded from force readiness, raw packets retained
    expected_capacitance = target["c_total_f"]
    p = estimates.estimate_route(inputs, group.group_id, group.refs[1],
        [(group.group_id, r) for r in group.refs if r != group.refs[1]],
        sensor_class="declared same active area", wiring_class="fixture", boundary="board connector")
    assert p["estimate"]["method"] == "constrained_target_refit"
    assert p["c_total_f"] == pytest.approx(expected_capacitance, rel=1e-10)
    assert fx.route_quality(p) == []
    assert all(b["raw_packet_id"] for b in p["per_burst"])
    assert all(b["diagnostics"]["fixed_c_total_f"] == pytest.approx(expected_capacitance)
               for b in p["per_burst"])


def test_old_manifests_are_quality_checked_without_destroying_the_original(tmp_path):
    inputs, gm, group = inputs_with_bad_route()
    inputs.data["schema_version"] = 1
    data = inputs._body()
    data["content_sha256"] = inputs.content_hash()
    p = tmp_path / "legacy.json"
    p.write_text(json.dumps(data, allow_nan=False), encoding="utf-8")
    before = p.read_bytes()
    loaded = fx.ForceInputs.load(p, gm)
    assert loaded.quality_issues(group.group_id)
    assert p.read_bytes() == before
    loaded.save(tmp_path / "new.json")
    saved = fx.ForceInputs.load(tmp_path / "new.json")
    assert saved.data["schema_version"] == 2
    assert group.group_id not in saved.coverage()["ready_groups"]


def test_recorder_refuses_to_truncate_existing_evidence(tmp_path):
    p = tmp_path / "trial.cap"
    p.write_bytes(b"earlier recording")
    with pytest.raises(FileExistsError):
        ChunkRecorder(p, ScanConfig(((2, 0),)))
    assert p.read_bytes() == b"earlier recording"


def test_npz_companion_also_refuses_to_replace_existing_data(tmp_path):
    p = tmp_path / "trial.npz"
    p.write_bytes(b"earlier samples")
    cap = fc.Capture({(2, 0): {"t": np.arange(10.), "volts": np.ones(10), "tc": np.ones(10)}})
    with pytest.raises(FileExistsError):
        cap.save(p)
    assert p.read_bytes() == b"earlier samples"


def test_large_above_vd_is_provisional_not_clipped_and_nonfinite_data_is_invalid():
    m = fm.RouteModel((2, 0), 1e6, 75e-12, 1.25, d_min=.01, d_max=1.1)
    r = fm.range_report(m, [1.2, 1.9])
    assert not r["clipped"] and not r["outside_fitted_range"]
    assert r["above_vd_count"] == 1 and r["max_above_vd_v"] == pytest.approx(.65)
    assert fm.range_report(m, [float("nan")])["invalid_input"]
    assert fm.range_report(m, [1.9], neg_margin_v=.25)["outside_fitted_range"]


def test_error_metrics_include_the_event_intercept_and_actual_accumulator_drift():
    gain, intercept = 300e-12, 30e-12
    events = [fm.CalEvent(str((r, mass, sign)), mass, sign, r,
                         gain * sign * mass * 1e-3 * fm.STANDARD_GRAVITY + intercept,
                         plain_delta_q=gain * sign * mass * 1e-3 * fm.STANDARD_GRAVITY + intercept + 15e-12)
              for r in (1, 2, 3) for mass in (20, 50) for sign in (1, -1)]
    fit = fm.fit_d33(events)
    assert fit.max_force_error_n == pytest.approx(.15)
    assert max(abs(x) for x in fit.regression_force_errors_n) < 1e-10
    assert fm.validate_holdout(events, [3], limit_n=.02).passed is False


def test_two_repeats_one_mass_reports_insufficient_training_evidence():
    events = [fm.CalEvent(str((r, sign)), 20, sign, r, sign * 10e-12)
              for r in (1, 2) for sign in (1, -1)]
    with pytest.raises(fm.ModelError, match="insufficient training evidence"):
        fm.validate_holdout(events, [2], limit_n=.02)


def test_drifting_unvalidated_fit_is_saved_provisionally():
    from test_force_calibration import sample_events, make_key
    fit = fm.fit_d33(sample_events())
    trial = fc.TrialResult(20, 1, True, [], ["HOLD_DRIFT"], [], {}, {}, None, None, {}, {})
    record = fc.build_record(make_key(), array=1, pzt="PZT1", d33_initial=600e-12,
        fit=fit, validation=None, trials=[trial], wizard=None, polarity=1, g=9.80665, manifest_sha256="test")
    assert record["status"] == "fitted_provisional"
    assert "HOLD_DRIFT" in record["quality_reasons"]


def test_cancel_and_new_session_preserve_raw_hashes_and_marks(tmp_path):
    from test_force_session import make_rig, make_controller, advance_to
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path)
    ctl.start_trial()
    advance_to(ctl, 3.1)
    ctl.mark_placement(3.0)
    ctl.cancel_trial()
    files = list(tmp_path.glob("*.cap"))
    assert len(files) == 1
    before = files[0].read_bytes()
    metadata = read_json(files[0].with_suffix(".trial.json"))
    assert metadata["state"] == "cancelled" and metadata["marks_s"]["place"] == 3
    assert metadata["raw_sha256"] == hashlib.sha256(before).hexdigest()
    other = make_controller(gm, rig, tmp_path)
    other.start_trial()
    advance_to(other, .5)
    other.cancel_trial()
    assert len(list(tmp_path.glob("*.cap"))) == 2 and files[0].read_bytes() == before


def test_applying_and_rolling_back_leakage_replays_preserved_raw_data(tmp_path):
    from test_force_session import make_rig, make_controller, drive_trial
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path, contact_position="centre")
    drive_trial(ctl, rig[1])
    old = copy.deepcopy(ctl.trials[0])
    raw_before = Path(old.capture_file).read_bytes()
    ctl.contact_position = "left edge"  # annotation for a later trial, not a rewrite of earlier evidence
    ctl.leakage = fm.LeakageFit("identified", fm.LeakageModel(.999, None, "effective"), "test", 20, 0, {}, {})
    proposed = ctl.build_record()
    assert proposed["correction_applied"] is False and proposed["leakage_status"] == "not_evaluated"
    ctl.apply_leakage()
    assert ctl.fit is None and ctl.validation is None
    assert ctl.trials[0].analysis_revision == 2
    assert ctl.trials[0].model_revision != old.model_revision
    assert ctl.trials[0].bias != old.bias
    assert all(e.position == "centre" for e in ctl.trials[0].events)
    assert Path(old.capture_file).read_bytes() == raw_before
    ctl.rollback_leakage()
    assert ctl.trials[0].analysis_revision == 3
    assert ctl.trials[0].events[0].delta_q == pytest.approx(old.events[0].delta_q)
    assert all(e.position == "centre" for e in ctl.trials[0].events)
    assert len(read_json(old.sidecar)["analyses"]) == 3


def test_unknown_finger_force_cannot_be_assigned_the_nominal_weight(tmp_path):
    from test_force_session import make_rig, make_controller, drive_trial
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path, reference_kind="workflow_only")
    drive_trial(ctl, rig[1])
    with pytest.raises(fc.CalibrationError, match="no known force reference"):
        ctl.fit_gain()
    assert ctl.build_record()["supported_range"]["masses_g"] == []


@pytest.mark.parametrize("bad_domain", [False, True])
def test_saved_evidence_reproduces_original_trial_and_detects_raw_tampering(tmp_path, bad_domain):
    from dataclasses import replace
    from captest.force_evidence import replay_trial
    from test_force_session import make_rig, make_controller, drive_trial
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path)
    if bad_domain:
        ctl.models = {r: replace(m, d_max=0.) for r, m in ctl.models.items()}
    drive_trial(ctl, rig[1])
    original = ctl.trials[-1]
    assert original.accepted is not bad_domain
    before = Path(original.sidecar).read_bytes()
    _, result, replayed, _ = replay_trial(original.sidecar)
    assert result.valid
    assert replayed.accepted == original.accepted
    assert replayed.reasons == original.reasons and replayed.flags == original.flags
    assert replayed.windows == original.windows
    assert [e.delta_q for e in replayed.events] == pytest.approx([e.delta_q for e in original.events])
    assert replayed.model_revision == original.model_revision
    assert Path(original.sidecar).read_bytes() == before
    raw = Path(original.capture_file)
    raw.write_bytes(raw.read_bytes() + b"damaged")
    with pytest.raises(ValueError, match="raw capture hash"):
        replay_trial(original.sidecar)


def test_companion_preserves_generation_and_reference_samples(tmp_path):
    data = {"t": np.arange(12.), "volts": np.ones(12), "tc": np.ones(12),
            "generation": np.array([0] * 6 + [1] * 6), "vmid_volts": np.full(12, 1.25)}
    path = tmp_path / "capture.npz"
    sha = fc.Capture({(2, 0): data}).save(path)
    again = fc.Capture.load(path, sha)
    for name in ("generation", "vmid_volts"):
        np.testing.assert_array_equal(again.samples[(2, 0)][name], data[name])


def test_calibration_save_keeps_previous_revisions_and_accepts_unfitted_evidence(tmp_path):
    from test_force_calibration import make_key, make_record
    store = fc.CalibrationStore(tmp_path)
    record = make_record(make_key())
    path = store.save(record)
    old = path.read_bytes()
    store.save(make_record(make_key(), previous=record, gain=9e-9))
    archive = list((tmp_path / "revisions").glob("*.json"))
    assert len(archive) == 1 and archive[0].read_bytes() == old
    unfitted = fc.build_record(make_key("A2_PZT1"), array=2, pzt="PZT1", d33_initial=1e-9,
        fit=None, validation=None, trials=[], wizard=None, polarity=1, g=fm.STANDARD_GRAVITY,
        manifest_sha256="test")
    assert fc.read_record(store.save(unfitted))["status"] == "not_fitted"


def test_invalid_trial_marks_and_adc_headroom_are_distinct():
    with pytest.raises(fc.CalibrationError, match="ordered"):
        fc.Marks((0., 3.), 2., 4., 5., 6.)
    with pytest.raises(fc.CalibrationError, match="constant-force"):
        fc.Marks((0., 1.), 2., 3., 4., 5., post_place=(3., 4.5))
    model = fm.RouteModel((2, 0), 1e6, 75e-12, 1.25)
    assert fm.range_report(model, [2.497])["headroom_low"]
    assert not fm.range_report(model, [2.497])["saturated"]
    assert fm.range_report(model, [2.5])["saturated"]


def test_changed_electrical_data_marks_embedded_gain_stale():
    inputs, _, group = inputs_with_bad_route()
    inputs.set_d33_eff(group.group_id, 300e-12, {"status": "validated"})
    inputs.latest(group.group_id)["routes"][0]["c_total_f"] *= 1.01
    inputs._refresh()
    gain = inputs.data["force_inputs"]["d33_eff_c_per_n"][group.group_id]
    assert gain["status"] == "stale" and gain["value"] == 300e-12


def test_unexpected_capture_stop_rejects_instead_of_waiting_forever(tmp_path):
    from test_force_session import make_rig, make_controller, advance_to
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path)
    ctl.start_trial()
    advance_to(ctl, .5)
    ctl.client.capture_stop()
    for _ in range(100):
        ctl.tick()
        if ctl.session is None:
            break
    assert ctl.session is None
    assert not ctl.trials[-1].accepted
    assert any("before the guided trial completed" in r for r in ctl.trials[-1].reasons)
    assert ctl.trials[-1].sidecar and ctl.trials[-1].companion


def test_nonfinite_prediction_is_not_silently_accumulated_as_zero():
    model = fm.RouteModel((2, 0), 1., 1e-20, 1.25, c1=1e308)
    with np.errstate(over="ignore", invalid="ignore"), pytest.raises(fm.ModelError, match="non-finite circuit"):
        fm.residuals(model, [10., 10.], [1., 1.])


def test_analysis_failure_keeps_completed_trial_evidence(tmp_path, monkeypatch):
    from test_force_session import make_rig, make_controller, drive_trial
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path)
    def refuse(*args):
        raise fm.ModelError("invalid prediction")
    monkeypatch.setattr(ctl, "_analyse", refuse)
    drive_trial(ctl, rig[1])
    trial = ctl.trials[-1]
    assert not trial.accepted and "ANALYSIS" in trial.reasons[0]
    assert trial.capture_sha256 and trial.companion_sha256
    assert read_json(trial.sidecar)["state"] == "rejected"
