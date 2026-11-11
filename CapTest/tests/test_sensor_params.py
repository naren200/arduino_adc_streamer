"""The small export must reproduce charge calculation without inventing calibration."""
import copy
import json

import numpy as np
import pytest

from captest import force_export as fx, force_model as fm, sensor_groups, sensor_params as sp
from test_force_export import make_session

GID = "A1_PZT1"


@pytest.fixture(scope="module")
def measured_inputs():
    gm = sensor_groups.resolve("TestBoard_7953")
    session, _ = make_session()
    group = gm.get(GID)
    for ref in group.refs:
        session.measure_channel(ref, bursts=2)
    inputs = fx.ForceInputs.new(gm, session.settings, simulated=True)
    inputs.add_package(group, fx.results_for_group(session.results, group), session.settings, gm,
                       simulated=True)
    return inputs


@pytest.fixture
def inputs(measured_inputs):
    return fx.ForceInputs(copy.deepcopy(measured_inputs.data))


def load_small(inputs):
    return json.loads(inputs.sensor_params_path.read_text())


def snapshot(inputs, kind="charge_rate", status="workflow_tested"):
    signs = {"%d:%d" % ref: sign for ref, sign in inputs.route_polarity(GID).items()}
    return {"electrical_manifest_sha256": inputs.electrical_hash(), "route_polarity": signs,
        "capture_signature": "scan-at-500us", "force_status": status,
        "tested_at": "2026-11-10T12:00:00Z", "drift_measured_at": "2026-11-10T11:59:00Z",
        "d33_eff_c_per_n": -320e-12 if status == "calibrated" else None,
        "unloaded_correction": {"status": "qualified", "signature": "scan-at-500us",
            "correction_kind": kind,
            "routes": {key: {"q_rate_c_per_s": (i+1)*1e-12, "bias_v": (i+1)*1e-5}
                       for i, key in enumerate(signs)}}}


def test_first_save_creates_small_companion_and_reload_keeps_filename(inputs, tmp_path):
    full = inputs.save(tmp_path / "full.json")
    small = inputs.sensor_params_path
    assert small.name.startswith("TestBoard_7953_SensorParams_") and small.suffix == ".json"
    body = load_small(inputs)
    assert body["format"] == sp.FORMAT and len(body["groups"]) == 10
    assert sum(len(g["channels"]) for g in body["groups"].values()) == 50
    assert body["source_file"] == full.name and body["adc_bits"] == 12
    group = body["groups"][GID]
    assert group["d33_eff_c_per_n"] is None and not group["force_ready"]
    assert all(r["captest_status"] == "complete" and r["drift_correction"] is None for r in group["channels"])
    assert all(r["captest_status"] == "not_measured" for r in body["groups"]["A1_PZT3"]["channels"])
    assert all(g["d33_eff_c_per_n"] is None for g in body["groups"].values())
    for forbidden in ("raw_packets", "per_burst", "merge_history", "analysis_history"):
        assert forbidden not in small.read_text()
    created = body["created"]
    again = fx.ForceInputs.load(full)
    again.save(full)
    assert again.sensor_params_path == small
    assert load_small(again)["created"] == created
    assert len(list(tmp_path.glob("*SensorParams*.json"))) == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_companion_names_do_not_collide_for_independent_manifests(inputs, tmp_path):
    other = fx.ForceInputs(copy.deepcopy(inputs.data))
    inputs.save(tmp_path / "one.json")
    other.save(tmp_path / "two.json")
    assert other.sensor_params_path != inputs.sensor_params_path
    assert load_small(inputs)["source_file"] == "one.json"
    assert load_small(other)["source_file"] == "two.json"


def test_failed_high_residual_and_missing_channels_are_distinct(inputs):
    routes = inputs.latest(GID)["routes"]
    routes[0]["status"] = "failed"
    routes[0]["reason"] = "fit failed"
    routes[1]["per_burst"][0]["flags"].append("HIGH_RESIDUAL")
    routes.pop()
    channels = sp.build(inputs)["groups"][GID]["channels"]
    assert [r["captest_status"] for r in channels] == ["failed", "high_residual", "complete", "complete", "not_measured"]
    assert "HIGH_RESIDUAL" in channels[1]["captest_issues"]


@pytest.mark.parametrize("status", ["fitted_provisional", "stale", "not_fitted"])
def test_unvalidated_gain_is_never_published(inputs, status):
    inputs.set_d33_eff(GID, 600e-12, {"status": status})
    group = sp.build(inputs)["groups"][GID]
    assert group["d33_eff_c_per_n"] is None and not group["force_ready"]


def test_validated_existing_gain_is_exported_and_becomes_stale_on_sign_change(inputs):
    inputs.set_d33_eff(GID, -320e-12, {"status": "validated", "capture_signature": "scan"})
    assert sp.build(inputs)["groups"][GID]["d33_eff_c_per_n"] == -320e-12
    signs = inputs.relative_polarity(GID); signs["C"] = -1
    inputs.set_relative_polarity(GID, signs)
    group = sp.build(inputs)["groups"][GID]
    assert group["force_calibration_status"] == "stale" and group["d33_eff_c_per_n"] is None


@pytest.mark.parametrize("kind", ["charge_rate", "residual_bias"])
def test_exported_parameters_reproduce_signed_charge_and_force(inputs, kind):
    signs = inputs.relative_polarity(GID); signs["C"] = -1
    inputs.set_relative_polarity(GID, signs)
    inputs.update_sensor_params(GID, snapshot(inputs, kind=kind, status="calibrated"))
    body = sp.build(inputs); group = body["groups"][GID]
    original = inputs.route_models(GID); rebuilt = {}; bias = {}; drift = {}; samples = {}; route_signs = {}
    for row in group["channels"]:
        ref = row["adc"], row["input"]
        rebuilt[ref] = fm.RouteModel(ref, row["r_bias_ohm"], row["c_total_f"], row["v_d_v"],
            row["c0"], row["c1_per_v"], row["qv_v"], row["d_min_v"], row["d_max_v"])
        samples[ref] = {"t": np.arange(5)*.000657,
                        "volts": row["v_d_v"] + np.array([-.01,-.009,.012,.011,-.001]),
                        "tc": np.full(5, 1.4e-6)}
        route_signs[ref] = row["relative_polarity"]
        correction = row["drift_correction"]
        bias[ref] = correction.get("bias_v", 0.)
        if kind == "charge_rate":
            drift[ref] = correction["q_rate_c_per_s"]
    expected = fm.accumulate(original, samples, bias, body["common_polarity"],
                              drift_rate=drift or None, route_polarity=route_signs)
    actual = fm.accumulate(rebuilt, samples, bias, body["common_polarity"],
                            drift_rate=drift or None, route_polarity=route_signs)
    np.testing.assert_array_equal(actual.q_proxy, expected.q_proxy)
    np.testing.assert_array_equal(actual.force(group["d33_eff_c_per_n"]), expected.force(-320e-12))


def test_state_is_persisted_without_changing_electrical_identity(inputs, tmp_path):
    path = inputs.save(tmp_path / "full.json")
    electrical = inputs.electrical_hash()
    inputs.update_sensor_params(GID, snapshot(inputs))
    reloaded = fx.ForceInputs.load(path)
    assert reloaded.electrical_hash() == electrical
    group = load_small(reloaded)["groups"][GID]
    assert group["force_calibration_status"] == "workflow_tested"
    assert group["d33_eff_c_per_n"] is None
    assert all(r["drift_status"] == "included" for r in group["channels"])
    # Changing electrical coefficients must suppress both gain and correction.
    reloaded.latest(GID)["routes"][0]["c0"] += .001
    reloaded.save(path)
    group = load_small(reloaded)["groups"][GID]
    assert group["force_calibration_status"] == "stale"
    assert all(r["drift_correction"] is None and r["drift_status"] == "stale" for r in group["channels"])


def test_drift_with_wrong_scan_or_invalid_rate_is_not_exported(inputs):
    state = snapshot(inputs)
    state["unloaded_correction"]["signature"] = "different-rate"
    inputs.update_sensor_params(GID, state)
    assert all(r["drift_correction"] is None for r in sp.build(inputs)["groups"][GID]["channels"])
    state = snapshot(inputs)
    state["unloaded_correction"]["routes"]["2:0"]["q_rate_c_per_s"] = float("nan")
    inputs.update_sensor_params(GID, state)
    body = sp.build(inputs)
    assert body["groups"][GID]["channels"][0]["drift_correction"] is None
    json.dumps(body, allow_nan=False)


def test_atomic_companion_failure_preserves_previous_file(inputs, tmp_path, monkeypatch):
    path = inputs.save(tmp_path / "full.json")
    small = inputs.sensor_params_path; before = small.read_bytes()
    def fail(*args):
        raise OSError("disk failure")
    monkeypatch.setattr(sp.os, "replace", fail)
    with pytest.raises(OSError, match="disk failure"):
        sp.write(inputs, small)
    assert small.read_bytes() == before and path.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_controller_automatically_publishes_and_clears_measured_drift(tmp_path):
    from test_force_session import make_rig, make_controller
    from test_force_zero import measure
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm)
    rig[3].save(tmp_path / "full.json")
    ctl = make_controller(gm, rig, tmp_path)
    measure(ctl)
    assert ctl.last_unloaded["status"] == "qualified"
    group = load_small(ctl.forces)["groups"][GID]
    assert all(r["drift_status"] == "included" for r in group["channels"])
    assert group["d33_eff_c_per_n"] is None
    assert group["capture_signature"] == ctl.signature()
    ctl.clear_unloaded_correction()
    assert all(r["drift_correction"] is None for r in load_small(ctl.forces)["groups"][GID]["channels"])


def test_actual_weight_validation_publishes_gain_and_invalidation_removes_it(tmp_path):
    from test_force_session import make_rig, make_controller, drive_trial
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm); rig[3].save(tmp_path / "full.json")
    ctl = make_controller(gm, rig, tmp_path)
    while ctl.wizard.current is not None:
        drive_trial(ctl, rig[1])
    ctl.fit_gain()
    assert load_small(ctl.forces)["groups"][GID]["d33_eff_c_per_n"] is None
    assert ctl.validate(limit_n=.15).passed
    group = load_small(ctl.forces)["groups"][GID]
    assert group["force_calibration_status"] == "calibrated"
    assert group["d33_eff_c_per_n"] == ctl.fit.d33_eff
    assert group["d33_eff_c_per_n"] != ctl.d33_initial
    ctl.invalidate("acceptance limits changed")
    group = load_small(ctl.forces)["groups"][GID]
    assert group["force_calibration_status"] == "stale" and group["d33_eff_c_per_n"] is None


def test_automatic_polarity_outcome_is_distinct_from_applied_signs(tmp_path):
    from test_force_session import make_rig, make_controller, scalar_force
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm, noise=0); rig[3].save(tmp_path / "full.json")
    ctl = make_controller(gm, rig, tmp_path)
    rig[1].scan.d33_true = 600e-12
    rig[1].scan.force = scalar_force(50, t_place=.8, t_remove=1.05)
    ctl.start_trial(polarity_test=True)
    for _ in range(10000):
        ctl.tick()
        if ctl.session is None:
            break
    assert ctl.last_polarity_check["status"] == "ready_for_review"
    group = load_small(ctl.forces)["groups"][GID]
    assert group["force_calibration_status"] == "polarity_tested"
    assert group["d33_eff_c_per_n"] is None
    assert all(r["polarity_status"] == "not_tested" for r in group["channels"])
    ctl.forces.set_relative_polarity(GID, ctl.forces.relative_polarity(GID), press_check=ctl.last_polarity_check)
    ctl.forces.save(ctl.forces.source_path)
    assert all(r["polarity_status"] == "applied" for r in load_small(ctl.forces)["groups"][GID]["channels"])


def test_companion_write_error_does_not_discard_capture(tmp_path, monkeypatch):
    from test_force_session import make_rig, make_controller, advance_to
    gm = sensor_groups.resolve("TestBoard_7953")
    rig = make_rig(gm); rig[3].save(tmp_path / "full.json")
    ctl = make_controller(gm, rig, tmp_path)
    def fail(*args):
        raise OSError("companion destination unavailable")
    monkeypatch.setattr(sp, "write", fail)
    ctl.start_trial()
    try:
        advance_to(ctl, .3)
        assert ctl.session is not None and ctl.session.running
        assert "companion destination unavailable" in ctl.sensor_params_error
    finally:
        ctl.cancel_trial()
    from captest.force_evidence import replay_trial
    body, result, _, _ = replay_trial(ctl._sidecar)
    assert body["state"] == "cancelled" and result.sweeps > 0
    assert rig[1].parked and not rig[1].scan.streaming
