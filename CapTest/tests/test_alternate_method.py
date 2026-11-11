"""Vmid between samples (capalt, CapTest firmware 1.3.0): the TestBoard_7953 default method."""

from pathlib import Path

import numpy as np
import pytest

from captest import capfit, protocol, runner
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings
from captest.sim import SimConfig, SimLink, SimulatedDevice


def make_session(version="1.3.0", **config):
    clock = VirtualClock()
    settings = Settings()
    device = SimulatedDevice(SimConfig(firmware_version=version, **config), clock=clock,
                             r_bias_table=settings.bias)
    session = runner.MeasurementSession(CapTestClient(SimLink(device)), settings, clock, simulated=True)
    return session, device


def test_default_method_is_vmid_between_samples_with_one_vmid_frame():
    settings = Settings.for_board("TestBoard_7953")
    assert settings.method == "alternate" and settings.vmid_frames == 1 and settings.gap_long_ns == 0
    assert Settings.from_dict({"gap_ns": 200}).method == "alternate"     # files saved before the setting


def test_automatic_long_gap_follows_the_guessed_tau():
    settings = Settings()
    assert settings.long_gap_ns(4, 0) == 7500           # 249 kOhm x 100 pF x 0.3
    assert settings.long_gap_ns(1, 0) == 30000          # 1 MOhm
    settings.c_guess_pf = 1000.0
    assert settings.long_gap_ns(1, 0) == 40000          # limited
    settings.gap_long_ns = 9000
    assert settings.long_gap_ns(1, 0) == 9000


@pytest.mark.parametrize("ref, c_pzt", [((1, 0), 70.0), ((2, 7), 240.0), ((4, 1), 30.0)])
def test_alternate_bursts_recover_c_total_and_the_shared_node(ref, c_pzt):
    session, device = make_session(c_pzt_overrides={"%d:%d" % ref: c_pzt})
    result = session.measure_channel(ref, bursts=3)
    truth = device.true_values(*ref)
    assert not result.failed and result.flags == []
    assert result.fit_used == capfit.FIT_ALT
    assert result.aggregate.mean["c_total"] == pytest.approx(truth["c_total"], rel=0.03)
    assert result.aggregate.mean["c_share"] == pytest.approx(truth["c_d"], abs=1e-12)
    command = [c for c in device.commands if c.startswith("capalt")][0].split()
    assert command[6:8] == ["1", "1"] and int(command[9]) == session.settings.long_gap_ns(*ref)


def test_the_start_voltage_does_not_bias_the_result():
    session, device = make_session(v_s_overrides={"2:5": 0.6})
    charged = session.measure_channel((2, 5)).aggregate.mean["c_total"]
    assert charged == pytest.approx(device.true_values(2, 5)["c_total"], rel=0.03)


def test_pattern_is_read_from_the_tags_and_reanalysis_follows_the_packet(tmp_path):
    session, _ = make_session()
    session.settings.vmid_frames = 2
    session.measure_channel((2, 0))
    burst = protocol.decode_burst_packet(session.raw[0].packet)
    assert burst.flags & protocol.FLAG_ALTERNATE and protocol.alternate_pattern(burst) == (1, 2)
    before = session.results[(2, 0)].aggregate.mean["c_total"]
    session.save_raw(tmp_path / "raw.json")
    other = Settings(method="burst")                     # the setting does not change saved data
    again = runner.reanalyze(tmp_path / "raw.json", other)
    assert again[0].fit_used == capfit.FIT_ALT
    assert again[0].aggregate.mean["c_total"] == pytest.approx(before)


def test_one_connected_time_cannot_separate_tau_and_is_reported():
    session, device = make_session()
    client = session.client
    client.set_spi_clock(20_000_000)
    packet = client.burst_packet("capalt 2 0 64 200 200 1 1 25*", protocol.HEADER_BYTES,
                                 protocol.packet_length_from_header)
    m = capfit.analyze_alternate(protocol.decode_burst_packet(packet), 470e3)
    assert capfit.FIT_FAILED in m.flags and not m.ok


def test_older_firmware_gets_a_clear_error():
    session, _ = make_session(version="1.2.0")
    logged = []
    session.on_log = logged.append
    assert session.run_manual([(2, 0)])[0].failed
    assert any("1.3.0" in line for line in logged)
    with pytest.raises(protocol.NotOkError, match="1.3.0"):
        session.driver.measure(session.client, session.driver.single((2, 0)), session.settings)


def test_sensor_side_values_stay_empty():
    session, _ = make_session()
    mean = session.measure_channel((2, 5)).aggregate.mean
    assert mean["c_share"] > 0
    assert mean["c_sensor_side"] != mean["c_sensor_side"] and mean["c_pzt"] != mean["c_pzt"]   # NaN


def test_connection_capacitance_has_its_own_values_not_the_node_columns():
    session, device = make_session()
    result = session.measure_channel((2, 5), bursts=2)
    truth = device.true_values(2, 5)
    mean, row = result.aggregate.mean, result.row()
    assert mean["c_share"] == pytest.approx(truth["c_d"], abs=1e-12)
    assert 0 < mean["c_share_ratio"] < 1
    assert mean["c_d"] != mean["c_d"] and mean["ratio"] != mean["ratio"]            # NaN: not C_D
    assert row["c_share_pf"] == pytest.approx(mean["c_share"] * 1e12)
    assert row["c_d_pf"] != row["c_d_pf"] and row["ratio"] != row["ratio"]


def test_vinf_is_the_fixed_cycle_of_the_nonlinear_model():
    keep, curve, q, tau = 0.93, -0.012, 0.006, 40e-6
    tc = np.array([1.1e-6, 9.0e-6] * 20)
    d_short, d_long = capfit._alternate_equilibrium(keep, curve, q, tc, tau)
    f = lambda d: keep * d + curve * d * d + q
    assert d_short == pytest.approx(np.exp(-1.1e-6 / tau) * f(d_long), abs=1e-9)
    assert d_long == pytest.approx(np.exp(-9.0e-6 / tau) * f(d_short), abs=1e-9)
    assert d_long < d_short                                  # a long cycle settles nearer V_D
    # The earlier constant-short-cycle formula leaves out the curvature and the long cycles.
    short = np.exp(-1.1e-6 / tau)
    assert abs(short * q / (1.0 - short * keep) - d_short) > 1e-3


RAW_ALTERNATE = Path(__file__).resolve().parents[1] / "Results" / "TestBoard_7953" / "2026-11-03_alternate_method_check2_raw.json"


@pytest.mark.skipif(not RAW_ALTERNATE.exists(), reason="recorded board data not available")
def test_vinf_on_a_recorded_burst_follows_the_fitted_model():
    results = {r.ref: r for r in runner.reanalyze(RAW_ALTERNATE)}
    m = results[(2, 5)].measurements[0]
    # Iterating the burst's own fitted model gives about 1.2421 V after a short and 1.2436 V
    # after a long cycle; the constant-short-cycle formula gave 1.2320 V.
    assert m.vinf == pytest.approx(1.2421, abs=0.001)
    assert m.connect_info["vinf_long_v"] == pytest.approx(1.2436, abs=0.001)


def shifted_packet(raw, offset_v, every=1, first=0):
    """The packet with the Vmid readings closing the cycles moved by `offset_v` volts."""
    burst = protocol.decode_burst_packet(raw.packet)
    sensor, vmid = protocol.alternate_pattern(burst)
    length = sensor + vmid
    lead = protocol.LEAD_FRAMES
    words = burst.words.copy()
    cycles = (len(words) - lead) // length
    for j in range(first, cycles, every):
        index = lead + j * length + length - 1
        code = int((words[index] & 0x0FFF) + offset_v / 2.5 * 4096)
        words[index] = (words[index] & 0xF000) | code
    burst.words = words
    return protocol.encode_burst_packet(burst), burst


def test_a_cycle_whose_vmid_reset_failed_is_left_out_and_flagged():
    session, _ = make_session()
    raw = session.measure_channel((2, 5)).raw_bursts[0]
    settings, driver = session.settings, session.driver
    clean = driver.analyze(driver.decode(raw.packet), [(2, 5)], settings, {(2, 5): None})[(2, 5)]
    assert capfit.VMID_RESET_FAILED not in clean.flags
    packet, burst = shifted_packet(raw, -0.1)               # no reset at any cycle
    bad = driver.analyze(burst, [(2, 5)], settings, {(2, 5): None})[(2, 5)]
    assert capfit.VMID_RESET_FAILED in bad.flags
    assert not bad.ok or bad.n_used < clean.n_used
    packet, burst = shifted_packet(raw, -0.1, every=7, first=3)     # a few cycles
    some = driver.analyze(burst, [(2, 5)], settings, {(2, 5): None})[(2, 5)]
    assert capfit.VMID_RESET_FAILED in some.flags and some.ok
    assert some.n_used < clean.n_used
    assert some.c_total == pytest.approx(clean.c_total, rel=0.03)    # the good pairs still agree


def test_bursts_of_different_methods_are_never_averaged(tmp_path):
    session, device = make_session()
    alternate = session.measure_channel((2, 5), bursts=2)
    session.settings.method = "burst"
    burst = session.measure_channel((2, 5), bursts=2)
    assert alternate.c_total_method == capfit.C_TOTAL_VMID_CYCLES
    assert burst.c_total_method == capfit.C_TOTAL_TAU_OVER_R
    assert capfit.MIXED_METHODS not in alternate.flags and capfit.MIXED_METHODS not in burst.flags
    session.save_raw(tmp_path / "raw.json")
    settings, _ = runner.load_raw(tmp_path / "raw.json")
    [mixed] = runner.reanalyze(tmp_path / "raw.json")
    assert capfit.MIXED_METHODS in mixed.flags and mixed.aggregate.count == 2
    assert mixed.c_total_method == capfit.C_TOTAL_VMID_CYCLES          # the one that gives a capacitance
    assert mixed.aggregate.mean["c_total"] == pytest.approx(alternate.aggregate.mean["c_total"], rel=1e-9)
    assert "tau_over_r" in mixed.message
    # Flags of the left-out bursts do not leak into the averaged result.
    assert capfit.FRAME_DEPENDENT not in mixed.flags
    both = runner.reanalyze(tmp_path / "raw.json", all_methods=True)
    assert sorted(r.c_total_method for r in both) == ["tau_over_r", "vmid_cycles"]
    for result in both:                                    # each row holds one method's bursts only
        assert len({m.c_total_method for m in result.used}) == 1 and capfit.MIXED_METHODS not in result.flags


def test_ln_view_arrays_follow_the_fit():
    session, _ = make_session()
    m = session.measure_channel((1, 0)).measurements[0]
    n = len(m.alt_connected_s)
    assert n > 8 and len(m.alt_d_v) == n and len(m.alt_d_model_v) == n
    assert np.all(np.diff(m.alt_connected_s) > 0)                       # connected time accumulates
    ok = np.isfinite(m.alt_d_v) & np.isfinite(m.alt_d_model_v)
    assert ok.sum() > 8
    # The freely run model stays near the data while d is well above the noise floor.
    big = ok & (m.alt_d_v > 0.1)
    assert np.all(np.abs(np.log(m.alt_d_model_v[big] / m.alt_d_v[big])) < 0.15)
    burst = make_session()[0]
    burst.settings.method = "burst"
    assert len(burst.measure_channel((1, 0)).measurements[0].alt_d_v) == 0


def test_a_diverging_fit_gives_no_settling_level_and_no_warning(recwarn):
    d_short, d_long = capfit._alternate_equilibrium(0.9, 40.0, 0.5, np.array([1e-6, 30e-6] * 5), 70e-6)
    assert np.isnan(d_short) and np.isnan(d_long)
    assert not [w for w in recwarn.list if issubclass(w.category, RuntimeWarning)]
