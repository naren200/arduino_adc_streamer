import math

import numpy as np
import pytest

from captest import capture as cap, force_calibration as fc, force_model as fm, protocol, runner
from captest.client import CapTestClient
from captest.link import LinkError, VirtualClock
from captest.settings import Settings, all_channels
from captest.sim import SimConfig, SimLink, SimulatedDevice

G = fm.STANDARD_GRAVITY
GROUP = [(2, i) for i in range(5)]


def make(version="1.4.0", leak=1e6, noise=1.0, **config):
    clock = VirtualClock()
    settings = Settings()
    device = SimulatedDevice(SimConfig(firmware_version=version, leak_tau_s=leak, noise_lsb=noise,
                                       **config), clock=clock, r_bias_table=settings.bias)
    return CapTestClient(SimLink(device)), device, settings


def full_config(lag_us=1500, **kw):
    return cap.ScanConfig.for_routes(all_channels(), lag_us=lag_us, **kw)


def run_until(session, seconds, max_polls=5000, stop_on_problem=False):
    for _ in range(max_polls):
        session.poll()
        if session.device_time_s >= seconds or not session.running:
            return
        if stop_on_problem and session.assembler.problems:
            session.poll()
            return
    raise AssertionError("capture did not reach %.1f s" % seconds)


# ------------------------------------------------------------------------- codec
def make_chunk(n_sweeps=3, n_routes=4, **kw):
    rng = np.random.default_rng(1)
    shape = (n_sweeps, n_routes)
    args = dict(session=0xABCD, boot_id=0x1234, seq=5, first_sweep=15, n_routes=n_routes, flags=0,
                dropped_total=0, cpu_hz=600_000_000,
                ts_conn=rng.integers(0, 2**32, shape, dtype=np.uint64).astype(np.uint32),
                ts_rel=rng.integers(0, 2**32, shape, dtype=np.uint64).astype(np.uint32),
                sensor=rng.integers(0, 65536, shape).astype(np.uint16),
                vmid=rng.integers(0, 65536, shape).astype(np.uint16))
    args.update(kw)
    return cap.Chunk(**args)


def test_chunk_roundtrip_length_and_crc():
    chunk = make_chunk()
    data = cap.encode_chunk(chunk)
    assert len(data) == cap.HEADER_BYTES + 3 * 4 * cap.RECORD_BYTES + 2
    assert cap.chunk_length_from_header(data[:cap.HEADER_BYTES]) == len(data)
    back = cap.decode_chunk(data)
    assert (back.session, back.boot_id, back.seq, back.first_sweep) == (0xABCD, 0x1234, 5, 15)
    for name in ("ts_conn", "ts_rel", "sensor", "vmid"):
        np.testing.assert_array_equal(getattr(back, name), getattr(chunk, name))
    bad = bytearray(data)
    bad[50] ^= 0x01
    with pytest.raises(protocol.CrcError):
        cap.decode_chunk(bytes(bad))
    with pytest.raises(protocol.TruncatedPacketError):
        cap.decode_chunk(data[:-5])
    with pytest.raises(protocol.FormatError):
        cap.chunk_length_from_header(b"XX" + data[2:cap.HEADER_BYTES])
    with pytest.raises(protocol.TruncatedPacketError):
        cap.chunk_length_from_header(data[:10])


def test_scan_config_validation_and_grouping():
    cfg = cap.ScanConfig.for_routes([(4, 1), (2, 3), (4, 0), (2, 1)])
    assert cfg.routes == ((2, 3), (2, 1), (4, 1), (4, 0)) and cfg.adcs() == [2, 4]
    with pytest.raises(cap.CaptureError, match="grouped"):
        cap.ScanConfig(((1, 0), (2, 0), (1, 1)))
    with pytest.raises(cap.CaptureError, match="duplicate"):
        cap.ScanConfig(((1, 0), (1, 0)))
    with pytest.raises(cap.CaptureError):
        cap.ScanConfig(())
    with pytest.raises(cap.CaptureError):
        cap.ScanConfig(((1, 0),), vmid_frames=2)
    with pytest.raises(cap.CaptureError):
        cap.ScanConfig(((1, 0),), lag_us=-1)
    lines = cfg.command_lines()
    assert lines[0] == "capcfg begin 200 25 0 1*" and lines[-1] == "capcfg commit*" and len(lines) == 6
    assert "lag_us=0" in cfg.signature()
    assert cap.ScanConfig(((1, 0),), lag_us=5).signature() != cap.ScanConfig(((1, 0),)).signature()


def test_schedule_mismatches_ignore_lag_and_routes():
    from captest import force_export as fx
    sig = fx.acquisition_signature(Settings())
    assert cap.schedule_mismatches(sig, full_config(lag_us=900)) == []
    other = cap.ScanConfig(((2, 0),), gap_ns=300)
    msgs = cap.schedule_mismatches(sig, other)
    assert len(msgs) == 1 and "gap_ns" in msgs[0]


# ----------------------------------------------------------------- feature gating
def test_old_firmware_reports_capture_unavailable_but_bursts_work():
    client, device, settings = make(version="1.3.0")
    with pytest.raises(cap.CaptureUnavailable, match="no continuous capture"):
        client.capture_require()
    session = runner.MeasurementSession(client, settings, device.clock, simulated=True)
    assert not session.measure_channel((2, 0)).failed          # existing workflow unchanged
    s = cap.CaptureSession(client, full_config(), GROUP)
    with pytest.raises(cap.CaptureUnavailable):
        s.start()
    assert not s.running


def test_new_firmware_advertises_capture():
    client, device, _ = make()
    caps = client.capture_require()
    assert caps["capture"] == "1" and int(caps["max_routes"]) == cap.MAX_ROUTES


def test_invalid_configuration_is_rejected_by_the_device():
    client, device, _ = make()
    assert client.command("capcfg route 2 0*")[0] is False             # no begin
    assert client.command("capcfg begin 200 25 0 2*")[0] is False       # two Vmid frames
    assert client.command("capstart 10*")[0] is False                   # nothing committed


# --------------------------------------------------------------- capturing
def test_capture_records_all_routes_in_order_without_problems(tmp_path):
    client, device, _ = make()
    cfg = full_config()
    s = cap.CaptureSession(client, cfg, GROUP, record_path=tmp_path / "t.cap", meta={"x": 1})
    s.start()
    run_until(s, 1.0)
    result = s.stop()
    assert result.valid, result.reasons()
    assert result.sweeps > 300 and len(result.samples) == 5 and result.config.routes == cfg.routes
    d = result.samples[(2, 0)]
    assert len(d["t"]) == result.sweeps and np.all(np.diff(d["t"]) > 0)
    # connected time and revisit interval are measured from the time stamps
    frame = 16 / 20e6 + 200e-9 + 100e-9
    assert result.tc_us[(2, 0)] == pytest.approx(frame * 1e6, rel=0.05)
    sweep_s = (2 * 50 + 2 * 4) * frame + 1500e-6
    assert result.revisit_us[(2, 0)] == pytest.approx(sweep_s * 1e6, rel=0.05)
    assert set(result.vd_by_adc) == {1, 2, 3, 4}
    assert result.vd_by_adc[2]["mean_v"] == pytest.approx(1.25, abs=0.003)
    assert device.parked and not device.scan.streaming          # ADCs parked after stop
    assert all(v == 0 for v in result.tag_errors.values())
    # volts near Vmid for an undisturbed package
    assert np.allclose(d["volts"], 1.25, atol=0.01)


def test_drift_snapshot_skips_timing_statistics_but_preserves_quality_and_samples():
    client, _, _ = make()
    session = cap.CaptureSession(client, full_config(), GROUP)
    session.start()
    try:
        run_until(session, .5)
        full = session.assembler.result()
        quick = session.assembler.result(include_timing=False)
        assert full.revisit_us and full.tc_us
        assert quick.revisit_us == {} and quick.tc_us == {}
        assert quick.vd_by_adc == full.vd_by_adc
        assert quick.valid == full.valid and quick.problems == full.problems
        assert quick.sweeps == full.sweeps and quick.duration_s == full.duration_s
        for ref in GROUP:
            for name in full.samples[ref]:
                np.testing.assert_array_equal(quick.samples[ref][name], full.samples[ref][name])
        assert session.running
    finally:
        session.stop()


def test_live_snapshots_match_the_final_result_and_stay_cheap(tmp_path):
    """Plot snapshots are taken repeatedly while chunks arrive (and merge the small pieces);
    the final result must be identical to a capture that was never snapshotted."""
    client, _, _ = make()
    s = cap.CaptureSession(client, full_config(), GROUP)
    s.start()
    seen = 0
    for _ in range(40):
        run_until(s, s.device_time_s + 0.05)
        samples, sweeps = s.assembler.live_samples()
        assert sweeps >= seen
        seen = sweeps
        assert len(samples[(2, 0)]["t"]) == sweeps
    assert len(s.assembler._t) < 5 and len(s.assembler._all_t_rel) <= 41    # one piece per snapshot, not per chunk
    live, sweeps = s.assembler.live_samples()
    result = s.stop()
    for ref in GROUP:
        n = len(live[ref]["t"])
        assert n <= len(result.samples[ref]["t"]) and n == sweeps
        assert np.array_equal(live[ref]["t"], result.samples[ref]["t"][:n])
        assert np.array_equal(live[ref]["volts"], result.samples[ref]["volts"][:n])
    assert result.valid, result.reasons()


def test_route_association_follows_block_order():
    # distinct start voltages per route: every route must show its own level in its own column
    levels = {"1:3": 0.4, "2:0": 0.7, "2:4": 1.0, "3:2": 0.55, "4:9": 0.9, "4:14": 0.3}
    client, device, _ = make(v_s_overrides=levels, noise=0.0)
    cfg = full_config(lag_us=5000)
    refs = [tuple(int(x) for x in k.split(":")) for k in levels]
    s = cap.CaptureSession(client, cfg, refs)
    s.start()
    run_until(s, 0.02)
    result = s.stop()
    assert result.valid, result.reasons()
    for k, level in levels.items():
        ref = tuple(int(x) for x in k.split(":"))
        first = result.samples[ref]["volts"][0]
        c_s = device.c_sensor[k]
        c_d = device.c_d(ref[0])
        v0 = (c_d * 1.25 + c_s * level) / (c_s + c_d)
        tc = result.samples[ref]["tc"][0]
        tau = device.r_actual[k] * (c_s + c_d)
        expected = 1.25 - (1.25 - v0) * math.exp(-tc / tau)
        assert first == pytest.approx(expected, abs=2 * 2.5 / 4096), (k, first, expected)


def test_32_bit_cycle_counter_wrap_is_extended():
    client, device, _ = make()
    device.scan.faults.wrap_start = 2**32 - 20_000           # wraps inside the first sweeps
    s = cap.CaptureSession(client, full_config(), GROUP)
    s.start()
    run_until(s, 0.3)
    result = s.stop()
    assert result.valid, result.reasons()
    t = result.samples[(2, 2)]["t"]
    assert np.all(np.diff(t) > 0) and t.max() < 1.0          # no 7 s jump from a wrapped counter


def test_recording_replays_identically(tmp_path):
    client, device, _ = make()
    s = cap.CaptureSession(client, full_config(), GROUP, record_path=tmp_path / "r.cap")
    s.start()
    run_until(s, 0.4)
    live = s.stop()
    assert (tmp_path / "r.cap").stat().st_size > 10_000 and live.raw_sha256
    again = cap.replay_file(tmp_path / "r.cap", keep_routes=GROUP)
    assert again.valid and again.sweeps == live.sweeps and again.raw_sha256 == live.raw_sha256
    for ref in GROUP:
        np.testing.assert_array_equal(again.samples[ref]["volts"], live.samples[ref]["volts"])
        np.testing.assert_allclose(again.samples[ref]["t"], live.samples[ref]["t"])
    with pytest.raises(cap.CaptureError):
        (tmp_path / "bad.cap").write_bytes(b"nope")
        cap.replay_file(tmp_path / "bad.cap")


def test_max_sweeps_ends_the_capture_with_a_final_chunk():
    client, device, _ = make()
    s = cap.CaptureSession(client, full_config(), GROUP)
    s.start(max_sweeps=40)
    for _ in range(100):
        s.poll()
        if not s.running:
            break
    result = s.stop()
    assert result.sweeps == 40 and result.valid, result.reasons()


# ------------------------------------------------------------------ fault handling
def faulty(**faults):
    client, device, _ = make()
    for k, v in faults.items():
        setattr(device.scan.faults, k, v)
    s = cap.CaptureSession(client, full_config(), GROUP)
    s.start()
    run_until(s, 0.4, stop_on_problem=True)
    return s, s.stop(), device


def kinds(result):
    return {p.kind for p in result.problems}


def test_lost_chunk_is_a_gap_and_invalidates():
    s, result, _ = faulty(drop_chunk_seq=10)
    assert not result.valid and cap.GAP in kinds(result)


def test_corrupt_chunk_fails_crc_and_leaves_a_gap():
    s, result, _ = faulty(corrupt_chunk_seq=12)
    assert not result.valid and {cap.CRC, cap.GAP} <= kinds(result)


def test_board_overrun_is_reported_not_hidden():
    s, result, _ = faulty(overrun_after_sweep=80, overrun_count=5)
    assert not result.valid and cap.OVERRUN in kinds(result) and cap.GAP in kinds(result)
    assert any("dropped" in str(p) for p in result.problems)


def test_wrong_tag_and_failed_reset_are_flagged_per_route():
    s, result, _ = faulty(tag_error_at_sweep=30)
    assert cap.TAG in kinds(result) and result.tag_errors[(1, 0)] == 1
    s, result, _ = faulty(reset_failure_at_sweep=30)
    assert cap.RESET in kinds(result) and result.reset_failures[(1, 0)] == 1


def test_clipping_reaches_the_samples_for_range_checks():
    client, device, _ = make()
    device.scan.faults.clip_route = (2, 1)
    device.scan.faults.clip_from_s = 0.1
    s = cap.CaptureSession(client, full_config(), GROUP)
    s.start()
    run_until(s, 0.3)
    result = s.stop()
    models = {r: fm.RouteModel(r, 470e3, 80e-12, 1.25) for r in GROUP}
    assert fm.range_report(models[(2, 1)], result.samples[(2, 1)]["volts"])["clipped"]
    assert not fm.range_report(models[(2, 0)], result.samples[(2, 0)]["volts"])["clipped"]


def test_reboot_during_capture_is_a_session_problem():
    s, result, _ = faulty(reboot_at_chunk=8)
    assert not result.valid and cap.SESSION in kinds(result)


def test_disconnect_marks_the_capture_and_releases_resources(tmp_path):
    client, device, _ = make()
    device.scan.faults.disconnect_at_chunk = 6
    s = cap.CaptureSession(client, full_config(), GROUP, record_path=tmp_path / "d.cap")
    s.start()
    for _ in range(200):
        s.poll()
        if not s.running:
            break
    result = s.stop()
    assert not result.valid and not s.running and s.closed
    assert s.recorder.closed                                   # file handle released


def test_cancel_stops_parks_and_marks_invalid(tmp_path):
    client, device, _ = make()
    s = cap.CaptureSession(client, full_config(), GROUP, record_path=tmp_path / "c.cap")
    s.start()
    run_until(s, 0.1)
    result = s.cancel()
    assert not result.valid and "cancelled" in result.reasons()[-1]
    assert device.parked and not device.scan.streaming and s.recorder.closed
    # the board can capture again afterwards
    again = cap.CaptureSession(client, full_config(), GROUP)
    again.start()
    run_until(again, 0.1)
    assert again.stop().valid


def test_double_start_and_closed_session_are_rejected():
    client, device, _ = make()
    s = cap.CaptureSession(client, full_config(), GROUP)
    s.start()
    with pytest.raises(protocol.NotOkError):
        client.capture_start(0)                                # already running
    s.stop()
    with pytest.raises(cap.CaptureError):
        s.start()


def test_kept_routes_must_be_in_the_scan():
    client, _, _ = make()
    with pytest.raises(cap.CaptureError, match="not in the scan"):
        cap.CaptureSession(client, cap.ScanConfig(((1, 0),)), [(2, 0)])


# ------------------------------------------------- capture -> trial analysis (sim)
def scalar_force(mass_g, t_place=3.0, t_remove=11.0):
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


def test_simulated_trial_end_to_end_through_capture_and_analysis(tmp_path):
    client, device, settings = make(noise=0.3)
    device.scan.d33_true = 60e-12
    device.scan.weights = {r: (0.2 if r in GROUP else 0.0) for r in all_channels()}
    device.scan.force = scalar_force(100)
    cfg = full_config(lag_us=1500)
    s = cap.CaptureSession(client, cfg, GROUP, record_path=tmp_path / "trial.cap")
    s.start()
    run_until(s, 15.0, max_polls=200000)
    result = s.stop()
    assert result.valid, result.reasons()
    models = {}
    for ref in GROUP:
        k = "%d:%d" % ref
        c_s, c_d = device.c_sensor[k], device.c_d(ref[0])
        models[ref] = fm.RouteModel(ref, device.r_actual[k], c_s + c_d, 1.25, c0=c_d / (c_s + c_d))
    capture = fc.Capture(result.samples, 2.5, "sim")
    marks = fc.Marks(baseline=(0.3, 2.5), place=3.0, place_clear=3.8, remove=11.0, remove_clear=11.8)
    trial = fc.analyze_trial(capture, models, marks, 100, 1)
    assert trial.accepted, trial.reasons
    tc = float(np.mean([result.tc_us[r] for r in GROUP])) * 1e-6
    tau = float(np.mean([models[r].tau for r in GROUP]))
    expected = math.exp(-tc / tau) * 60e-12 * 0.1 * G          # ideal-sharing limit: E * d33 * F
    assert trial.events[0].delta_q == pytest.approx(expected, rel=0.08)
    assert trial.events[1].delta_q == pytest.approx(-expected, rel=0.08)
    assert trial.load_removal_ratio == pytest.approx(1.0, rel=0.08)
    # the measured sampling rate of every channel is kept with the trial and summarised
    assert set(trial.sampling) == {"%d:%d" % r for r in GROUP}
    sweep_s = (2 * 50 + 2 * 4) * (16 / 20e6 + 200e-9 + 100e-9) + 1500e-6
    for info in trial.sampling.values():
        assert info["rate_hz"] == pytest.approx(1.0 / sweep_s, rel=0.05)
        assert info["median_interval_us"] == pytest.approx(sweep_s * 1e6, rel=0.05)
        assert info["interval_max_us"] >= info["median_interval_us"] * 0.999
    summary = fc.sampling_summary([trial])
    assert summary["2:0"]["rate_hz"] == pytest.approx(1.0 / sweep_s, rel=0.05)
    assert "sampling" in trial.to_json()


def test_sampling_rates_of_short_or_empty_data_are_skipped():
    assert fc.sampling_rates({}) == {}
    assert fc.sampling_rates({(1, 0): {"t": np.array([0.0, 1.0])}}) == {}
    live = fc.sampling_rates({(1, 0): {"t": np.arange(100) * 0.001}}, last=10)
    assert live["1:0"]["rate_hz"] == pytest.approx(1000.0) and live["1:0"]["samples"] == 10


def test_fast_crc_equals_the_reference_and_the_standard_check_value():
    import os

    assert protocol.crc16_ccitt_false(b"123456789") == 0x29B1          # CRC-16/CCITT-FALSE check value
    for n in (0, 1, 37, 638, 1838):
        data = os.urandom(n)
        assert protocol.crc16_ccitt_false(data) == protocol.crc16_ccitt_false_reference(data)


def test_resync_discards_stale_bytes_and_a_late_acknowledgement():
    client, device, _ = make()
    # what a failed capture can leave on the line: half a chunk, a late capstop acknowledgement
    device._outbox += b"CK\x01$" + bytes(300) + b"\n#OK capstop\n"
    client.resync()
    assert not device._outbox
    info = client.capture_require()                           # gets its own reply, not the stale one
    assert int(info["capture"]) == 1


def test_resync_stops_a_capture_that_is_still_running():
    client, device, _ = make()
    s = cap.CaptureSession(client, full_config(), GROUP)
    s.start()
    run_until(s, 0.05)
    assert device.scan.streaming
    client.resync()
    assert not device.scan.streaming and device.parked


def test_a_session_can_start_again_right_after_a_failed_one():
    client, device, _ = make()
    device.scan.faults.overrun_after_sweep = 20
    device.scan.faults.overrun_count = 1000
    first = cap.CaptureSession(client, full_config(), GROUP)
    first.start()
    run_until(first, 1.0, stop_on_problem=True)
    assert not first.stop().valid
    device.scan.faults.overrun_after_sweep, device.scan.faults.overrun_count = None, 0
    second = cap.CaptureSession(client, full_config(), GROUP)
    second.start()
    run_until(second, 0.5)
    assert second.stop().valid
