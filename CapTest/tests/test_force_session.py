import json
import math
from pathlib import Path

import numpy as np
import pytest

from captest import (capture as cap, force_calibration as fc, force_export as fx, force_model as fm,
                     force_session as fs, runner, sensor_groups)
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings, all_channels
from captest.sim import SimConfig, SimLink, SimulatedDevice

G = fm.STANDARD_GRAVITY
GID = "A1_PZT1"                       # ADC2 inputs 0-4
D33_TRUE = 60e-12
T_PLACE, T_CLEAR, HOLD = 3.0, 3.7, 10.0
T_REMOVE = T_CLEAR + HOLD + 0.4
T_REMOVE_CLEAR = T_REMOVE + 0.7


@pytest.fixture(scope="module")
def gm():
    return sensor_groups.resolve("TestBoard_7953")


def make_rig(gm, noise=0.3, tmp_path=None, measure=True, **sim):
    clock = VirtualClock()
    settings = Settings()
    device = SimulatedDevice(SimConfig(firmware_version="1.4.0", leak_tau_s=1e6, noise_lsb=noise,
                                       **sim), clock=clock, r_bias_table=settings.bias)
    client = CapTestClient(SimLink(device))
    session = runner.MeasurementSession(client, settings, clock, simulated=True)
    group = gm.get(GID)
    forces = fx.ForceInputs.new(gm, settings, board_instance="b1", simulated=True)
    if measure:
        for ref in group.refs:
            session.measure_channel(ref, bursts=2)
        forces.add_package(group, fx.results_for_group(session.results, group), settings, gm,
                           board_instance="b1", simulated=True)
    device.scan.d33_true = D33_TRUE
    device.scan.weights = {r: (0.2 if r in group.refs else 0.0) for r in all_channels()}
    return client, device, settings, forces


def scalar_force(mass_g, t_place=T_PLACE, t_remove=T_REMOVE):
    peak = mass_g * 1e-3 * G

    def force(t):
        total = 0.0
        for t0, sign in ((t_place, 1.0), (t_remove, -1.0)):
            x = t - t0
            if x > 0:
                rise = min(x / 0.15, 1.0)
                ring = 0.2 * math.exp(-x / 0.08) * math.sin(2 * math.pi * 15 * x) if x > 0.15 else 0.0
                total += sign * peak * (rise + ring)
        return total

    return force


def advance_to(ctl, t, max_ticks=100000):
    for _ in range(max_ticks):
        ctl.tick()
        if ctl.now >= t or ctl.session is None:
            return
    raise AssertionError("device time %.2f not reached (at %.2f)" % (t, ctl.now))


def drive_trial(ctl, device):
    """Play the user's part of one trial with marks at the instants the simulated force acts."""
    device.scan.force = scalar_force(ctl.wizard.current.mass_g)
    ctl.start_trial()
    advance_to(ctl, T_PLACE + 0.05)
    assert ctl.wizard.phase == fc.PLACE_ARMED and ctl.wizard.armed
    ctl.mark_placement(T_PLACE)
    advance_to(ctl, T_CLEAR + 0.05)
    ctl.hand_clear_placement(T_CLEAR)
    advance_to(ctl, T_REMOVE + 0.05)
    assert ctl.wizard.phase == fc.REMOVE_ARMED
    ctl.mark_removal(T_REMOVE)
    advance_to(ctl, T_REMOVE_CLEAR + 0.05)
    ctl.hand_clear_removal(T_REMOVE_CLEAR)
    for _ in range(100000):
        ctl.tick()
        if ctl.session is None:
            break


def make_controller(gm, rig, tmp_path, **kw):
    client, device, settings, forces = rig
    args = dict(d33_initial=10e-9, scan_lag_us=1500, repeats=2, masses=(50, 100), hold_s=HOLD,
                baseline_s=2.5, unloaded_s=3.0, board_instance="b1", results_dir=tmp_path,
                simulated=True)
    args.update(kw)
    return fs.ForceCalibrationController(forces, gm, GID, client, settings, **args)


def expected_proxy(ctl, mass_g):
    res = ctl.last_result
    tc = float(np.mean([res.tc_us[r] for r in ctl.group.refs])) * 1e-6
    tau = float(np.mean([m.tau for m in ctl.models.values()]))
    return math.exp(-tc / tau) * D33_TRUE * mass_g * 1e-3 * G


def test_controller_refuses_unready_package_and_bad_gain(gm, tmp_path):
    rig = make_rig(gm, measure=False)
    with pytest.raises(fx.ForceExportError, match="not usable"):
        make_controller(gm, rig, tmp_path)
    rig = make_rig(gm)
    for bad in (0.0, float("nan")):
        with pytest.raises(fs.SessionError):
            make_controller(gm, rig, tmp_path, d33_initial=bad)


def test_schedule_mismatch_blocks_start_unless_accepted(gm, tmp_path):
    client, device, settings, forces = make_rig(gm)
    other = Settings.from_dict(json.loads(settings.to_json()))
    other.gap_ns = 300
    ctl = fs.ForceCalibrationController(forces, gm, GID, client, other, d33_initial=10e-9,
                                        scan_lag_us=1500, repeats=1, masses=(50,))
    assert any("gap_ns" in p for p in ctl.readiness())
    with pytest.raises(fs.SessionError, match="gap_ns"):
        ctl.start_trial()
    assert not device.scan.streaming and ctl.session is None
    ctl.start_trial(accept_mismatch=True)
    assert any("accepted by the user" in line for line in ctl.log)
    ctl.cancel_trial()


def test_signature_carries_schedule_and_lag(gm, tmp_path):
    rig = make_rig(gm)
    a = make_controller(gm, rig, tmp_path, scan_lag_us=1500)
    b = make_controller(gm, rig, tmp_path, scan_lag_us=3000)
    assert a.signature() != b.signature() and a.key.digest() != b.key.digest()
    assert "lag_us=1500" in a.signature() and "routes=50" in a.signature()


def test_full_guided_trial_gives_events_and_files(gm, tmp_path):
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path)
    drive_trial(ctl, rig[1])
    trial = ctl.trials[-1]
    assert trial.accepted, trial.reasons
    assert ctl.wizard.steps[0].status == fc.DONE_STEP and ctl.wizard.current.repeat == 2
    assert not rig[1].scan.streaming and rig[1].parked
    expected = expected_proxy(ctl, 50)
    assert trial.events[0].delta_q == pytest.approx(expected, rel=0.12)
    assert trial.events[1].delta_q == pytest.approx(-expected, rel=0.12)
    assert trial.companion and (tmp_path / trial.companion).exists() and trial.capture_sha256
    assert Path(trial.capture_file).exists()
    assert Path(trial.sidecar).exists()
    again = fc.Capture.load(tmp_path / trial.companion, trial.companion_sha256)
    assert again.routes() == list(ctl.group.refs)
    json.dumps(trial.to_json(), allow_nan=False)
    assert trial.marks.baseline[0] == pytest.approx(fs.BASELINE_SKIP_S)


def test_live_view_is_available_while_capturing_and_does_not_change_the_capture(gm, tmp_path):
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path)
    rig[1].scan.force = scalar_force(50)
    ctl.start_trial()
    advance_to(ctl, 1.0)
    view = ctl.live_view()
    assert view is not None and view.phase == fc.BASELINE and len(view.t) > 100
    assert set(view.residuals) == set(ctl.group.refs) and not view.problems
    assert all(not h["clipped"] for h in view.headroom.values())
    n_before = ctl.session.assembler.sweeps
    for _ in range(20):
        ctl.live_view()                          # plotting only reads: sampling is unaffected
    assert ctl.session.assembler.sweeps == n_before
    ctl.cancel_trial()


def test_capture_problem_rejects_the_trial_and_allows_retry(gm, tmp_path):
    rig = make_rig(gm)
    rig[1].scan.faults.drop_chunk_seq = 40
    ctl = make_controller(gm, rig, tmp_path)
    rig[1].scan.force = scalar_force(50)
    ctl.start_trial()
    for _ in range(100000):
        phase = ctl.tick()
        if ctl.session is None:
            break
    trial = ctl.trials[-1]
    assert not trial.accepted and any("GAP" in r for r in trial.reasons)
    assert ctl.wizard.steps[0].status == fc.FAILED_STEP
    assert not rig[1].scan.streaming and rig[1].parked
    rig[1].scan.faults.drop_chunk_seq = None
    ctl.wizard.retry()
    assert ctl.wizard.current.repeat == 1 and ctl.wizard.phase == fc.PREPARE
    rig[1].scan.force = scalar_force(50)
    drive_trial(ctl, rig[1])
    assert ctl.trials[-1].accepted


def test_cancel_releases_the_board_and_keeps_the_step(gm, tmp_path):
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path)
    rig[1].scan.force = scalar_force(50)
    ctl.start_trial()
    advance_to(ctl, 0.5)
    ctl.cancel_trial()
    assert ctl.session is None and rig[1].parked and not rig[1].scan.streaming
    assert ctl.wizard.phase == fc.PREPARE and ctl.wizard.current.mass_g == 50
    assert ctl.trials == []
    with pytest.raises(fc.CalibrationError):
        ctl.mark_removal()                      # not allowed while nothing runs


def test_removal_cannot_be_marked_during_the_hold(gm, tmp_path):
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path)
    rig[1].scan.force = scalar_force(50)
    ctl.start_trial()
    advance_to(ctl, T_PLACE + 0.05)
    ctl.mark_placement(T_PLACE)
    ctl.hand_clear_placement(T_CLEAR)
    with pytest.raises(fs.SessionError, match="hold"):
        ctl.mark_removal()
    ctl.cancel_trial()


def test_two_masses_two_repeats_fit_validate_and_save(gm, tmp_path):
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path, masses=(50, 100), repeats=2)
    while ctl.wizard.current is not None:
        drive_trial(ctl, rig[1])
    assert len(ctl.trials) == 4 and all(t.accepted for t in ctl.trials), [t.reasons for t in ctl.trials]
    fit = ctl.fit_gain()
    tc = float(np.mean([ctl.last_result.tc_us[r] for r in ctl.group.refs])) * 1e-6
    tau = float(np.mean([m.tau for m in ctl.models.values()]))
    assert fit.d33_eff == pytest.approx(math.exp(-tc / tau) * D33_TRUE, rel=0.12)
    assert fit.d33_eff != ctl.d33_initial                       # initial value did not bias the fit
    val = ctl.validate(limit_n=0.15)
    assert val.test_repeats == (2,) and val.passed
    store = fc.CalibrationStore(tmp_path / "cal")
    path = ctl.save(store)
    rec = fc.read_record(path)
    assert rec["status"] == "validated" and rec["origin"] == "simulated"
    assert rec["d33_eff_fitted_c_per_n"] == pytest.approx(val.fit.d33_eff)
    assert rec["supported_range"]["masses_g"] == [50, 100]
    assert rec["key"]["group_id"] == GID and rec["schedule"]["lag_us"] == 1500
    assert len(rec["trials"]) == 4
    assert fc.compatibility(rec, board_instance="b1", layout_hash=gm.layout_hash,
                            capture_signature=ctl.signature(),
                            manifest_sha256=ctl.forces.electrical_hash()) == []
    # embedding the fitted sensitivity in the bundle keeps the record's link valid
    ctl.forces.set_d33_eff(GID, fit.d33_eff, {"record": path.name})
    assert fc.compatibility(rec, board_instance="b1", layout_hash=gm.layout_hash,
                            capture_signature=ctl.signature(),
                            manifest_sha256=ctl.forces.electrical_hash()) == []
    # saving again produces a new revision of the same record, nothing else
    ctl.save(store)
    assert store.load(ctl.key)["revision"] == 2 and len(store.list()) == 1
    assert "validated_range" in rec and rec["validated_range"]["position_independent"] is False
    assert ctl.validate(limit_n=1e-15).passed is False
    provisional = ctl.build_record()
    assert provisional["status"] == "fitted_provisional"
    assert "validated_domain" not in provisional and "validated_range" not in provisional
    assert provisional["tested_range"]["masses_g"] == [50, 100]


def test_validation_needs_two_repeats_and_leakage_is_not_applied_unidentified(gm, tmp_path):
    rig = make_rig(gm)
    ctl = make_controller(gm, rig, tmp_path, masses=(100,), repeats=1)
    drive_trial(ctl, rig[1])
    with pytest.raises(fs.SessionError, match="two accepted repeats"):
        ctl.validate()
    with pytest.raises(fs.SessionError, match="not identified"):
        ctl.apply_leakage()
    leak = ctl.evaluate_leakage()
    assert leak.status == "not_identified"                     # no contrast: stays uncorrected
    with pytest.raises(fs.SessionError):
        ctl.apply_leakage()
    assert ctl.leakage_models == {}
