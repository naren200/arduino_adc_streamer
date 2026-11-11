"""TestBoard_ADC124 driver: channel model, address-grouped planning, the time model built
from the pop stamps, and the accuracy of C_total / C_D ratio on the simulated board."""

import re
from pathlib import Path

import numpy as np
import pytest

from captest import capfit
from captest.boards import adc124_protocol as proto
from captest.settings import Settings
from helpers124 import BOARD, burst_commands, driver, make_session

VALID_PER_MUX = {1: range(6), 2: range(6), 3: range(6), 4: range(7)}


def test_channel_model_matches_the_firmware_routes():
    drv = driver()
    channels = drv.channels()
    assert len(channels) == 50
    assert channels == [(a, m, i) for a in (1, 2) for m in (1, 2, 3, 4) for i in VALID_PER_MUX[m]]
    assert drv.valid((1, 4, 6)) and not drv.valid((1, 1, 6)) and not drv.valid((1, 4, 7))
    assert not drv.valid((3, 1, 0)) and not drv.valid((1, 5, 0)) and not drv.valid((1, 1))
    assert drv.key((2, 3, 4)) == "2:3:4" and drv.display((2, 3, 4)) == "2:3:4"
    assert drv.identity((1, 4, 6)) == {"array": 1, "mux": 4, "input": 6}
    assert drv.group_of((2, 1, 0)) == 2 and drv.shared_node((2, 3, 1)) == (2, 3)
    assert drv.chart_group((2, 3, 1)) == "A2 MUX3"


def test_groups_build_a_mux_by_input_grid():
    groups = driver().groups()
    assert [g.id for g in groups] == [1, 2] and [len(g.channels) for g in groups] == [25, 25]
    grid = groups[0].grid
    assert len(grid) == 4 and all(len(row) == 7 for row in grid)
    assert [cell for cell in grid[0]].count(None) == 1 and grid[3][6] == (1, 4, 6)
    assert groups[0].row_labels == ["MUX1", "MUX2", "MUX3", "MUX4"]


def test_spec_parsing_in_the_firmware_route_syntax():
    drv = driver()
    assert drv.parse_spec("1:1:0,1:4:6,2:2:0") == [(1, 1, 0), (1, 4, 6), (2, 2, 0)]
    assert drv.parse_spec("1:2:0-2") == [(1, 2, 0), (1, 2, 1), (1, 2, 2)]
    assert drv.parse_spec("2:*:5") == [(2, 1, 5), (2, 2, 5), (2, 3, 5), (2, 4, 5)]
    assert len(drv.parse_spec("1:*:*")) == 25 and len(drv.parse_spec("1:4:*")) == 7
    assert drv.parse_spec("1:1:0; 1:1:0") == [(1, 1, 0)]                 # duplicates collapse
    for bad in ("1:1:6", "3:1:0", "1:5:0", "1:1:7", "1:1", "1:1:a", "1:3-1:0"):
        with pytest.raises(ValueError):
            drv.parse_spec(bad)
    assert drv.format_spec([(1, 1, 0), (2, 4, 6)]) == "1:1:0,2:4:6"


def test_planning_address_groups_for_an_automatic_run():
    drv, settings = driver(), Settings.for_board(BOARD)
    plan = drv.plan(drv.auto_channels(), settings)
    assert len(plan) == 14                                         # at most 7 connections per array
    assert [c.params for c in plan] == [(a, x) for a in (1, 2) for x in range(7)]
    assert [len(c.reads) for c in plan[:7]] == [4, 4, 4, 4, 4, 4, 1]
    assert plan[6].reads == ((1, 4, 6),) and plan[6].touches == ((1, 4, 6),)   # only MUX4 has input 6
    assert plan[0].reads == ((1, 1, 0), (1, 2, 0), (1, 3, 0), (1, 4, 0)) == plan[0].touches
    assert sum(len(c.reads) for c in plan) == 50
    only_array_2 = drv.plan(drv.auto_channels([2]), settings)
    assert len(only_array_2) == 7 and all(c.params[0] == 2 for c in only_array_2)


def test_planning_manual_selection_and_per_channel_mode():
    drv, settings = driver(), Settings.for_board(BOARD)
    chosen = [(1, 2, 3), (1, 1, 0), (1, 4, 3), (2, 2, 0)]
    plan = drv.plan(chosen, settings)
    assert [(c.params, c.reads) for c in plan] == [
        ((1, 0), ((1, 1, 0),)), ((1, 3), ((1, 2, 3), (1, 4, 3))), ((2, 0), ((2, 2, 0),))]
    # The unread muxes of the address are still connected: they are touched, not read.
    assert plan[0].touches == ((1, 1, 0), (1, 2, 0), (1, 3, 0), (1, 4, 0))
    single = drv.plan(chosen, settings, per_address=False)
    assert len(single) == 4 and all(len(c.reads) == 1 for c in single)
    assert [c.reads[0] for c in single] == [(1, 1, 0), (1, 2, 3), (1, 4, 3), (2, 2, 0)]
    # Mux-major per-channel order puts different addresses next to each other.
    every = drv.plan(drv.channels([1]), settings, per_address=False)
    assert len(every) == 25
    assert all(a.params != b.params for a, b in zip(every, every[1:]))
    assert drv.single((1, 3, 5)).touches == ((1, 1, 5), (1, 2, 5), (1, 3, 5), (1, 4, 5))


def test_burst_sizing_and_selection_mask():
    drv, settings = driver(), Settings.for_board(BOARD)
    four = drv.plan([(1, m, 0) for m in (1, 2, 3, 4)], settings)[0]
    one = drv.single((1, 2, 0))
    assert drv.adc_mask(four.reads) == 0b1111 and drv.adc_mask(one.reads) == 0b0010
    assert drv.adc_mask(drv.single((1, 4, 6)).reads) == 0b1000
    for connection in (four, one):
        post = drv.post_frames_for(settings, connection)
        assert post % len(connection.reads) == 0 and 16 <= post <= 4096 - 8
    # 6 tau of 100 pF behind the largest bias resistor (1 M) at ~1.04 us per frame.
    assert drv.post_frames_for(settings, one) == pytest.approx(6 * 1e6 * 100e-12 / 1.037e-6, rel=0.02)
    settings.n_samples = 50
    assert drv.post_frames_for(settings, four) == 200 and drv.post_frames_for(settings, one) == 50
    settings.n_samples = 5000
    assert drv.post_frames_for(settings, four) <= 4096 - 8
    settings.pre_samples = 9
    assert drv.pre_frames_for(settings, 4) == 12 and drv.pre_frames_for(settings, 1) == 9


def test_bias_defaults_agree_between_routes_json_and_firmware_header():
    drv = driver()
    table = drv.default_bias_table()
    assert len(table) == 50 and drv.bias_conflicts() == []
    assert table["1:1:0"] == 470000 and table["1:2:3"] == 1000000 and table["1:4:6"] == 1000000
    assert table["2:1:0"] == 1000000 and table["2:2:5"] == 470000 and table["2:4:6"] == 470000
    header = (Path(__file__).resolve().parents[2] / "Arduino_Sketches" / "TestBoard_ADC124" / "include"
              / "ConfigurableParameters.h").read_text()
    numbers = [int(x) for x in re.findall(r"\d+", re.search(r"kBiasResistorOhms.*?=\s*\{(.*?)\};", header, re.S).group(1))]
    assert numbers[:4] == [470000, 1000000, 470000, 1000000] and numbers[4:] == [1000000, 470000, 1000000, 470000]


# ------------------------------------------------------------------ time model
def synthetic_burst(n=200, pre=8, mask=1, stall_every=0, jitter_ns=20.0, seed=3, cpu_hz=600_000_000):
    """A packet whose true frame times are known exactly: end of frame k at (k+1) * period."""
    rng = np.random.default_rng(seed)
    period = 1037.5e-9
    ends = 1.3e-6 + period * np.arange(n)
    pops = ends + 80e-9 + rng.normal(0, jitter_ns * 1e-9, n)
    if stall_every:
        pops[::stall_every] += 4e-6
        for j in range(1, n):                 # the backlog is popped back to back after a stall
            pops[j] = max(pops[j], pops[j - 1] + 120e-9)
    start = 123456
    stamps = ((np.rint(pops * cpu_hz).astype(np.int64) + start) & 0xFFFFFFFF).astype(np.uint32)
    sw0 = int(start + np.rint((pops[pre - 1] + 53e-9) * cpu_hz))
    return proto.BurstData124(
        array=1, address=0, input_mask=mask, pre_frames=pre, words=np.full(n, 2048, np.uint16),
        stamps=stamps, cpu_hz=cpu_hz, spi_clock_hz=16_000_000, start_cycles=start,
        lead_cycles=start + 800, switch_begin_cycles=sw0, switch_end_cycles=sw0 + 367,
        period_min_cycles=600, period_sum_cycles=int(period * cpu_hz * (n - 1)), period_max_cycles=700), ends


def test_frame_line_recovers_the_period_and_ignores_stalled_frames():
    drv = driver()
    burst, ends = synthetic_burst()
    a, period, keep = drv.frame_line(burst)
    assert period == pytest.approx(1037.5e-9, rel=2e-4) and keep.all()
    assert a == pytest.approx(ends[0] + 80e-9 - 1037.5e-9 * 0, abs=30e-9)          # intercept = first pop
    stalled, ends = synthetic_burst(stall_every=17)
    a2, period2, keep2 = drv.frame_line(stalled)
    assert period2 == pytest.approx(1037.5e-9, rel=3e-4)
    assert (~keep2).sum() >= 10 and not keep2[0] and keep2[10] and keep2[25]
    assert a2 == pytest.approx(a, abs=30e-9)


def test_timing_places_the_connection_and_the_sample_instants():
    drv, settings = driver(), Settings.for_board(BOARD)
    burst, ends = synthetic_burst()
    timing = drv.timing(burst, settings)
    start = burst.start_cycles
    # Disable edge = switch_begin, enable edge = switch_end - 200 ns guard, then the mux turn-on delay.
    assert timing["disable"] == pytest.approx((burst.switch_begin_cycles - start) / 600e6)
    assert timing["enable"] == pytest.approx((burst.switch_end_cycles - start) / 600e6 - 200e-9)
    assert timing["connect"] == pytest.approx(timing["enable"] + 25e-9)
    # Sample instant of frame k = end of the frame - (16 - hold) clocks - pop latency offset (80 vs 100 ns).
    expected = ends - (16 - 3.0) / 16e6
    assert np.max(np.abs(timing["sample"] - expected)) < 60e-9
    settings.hold_clocks = 4.0
    shifted = drv.timing(burst, settings)["sample"]
    assert np.allclose(shifted - timing["sample"], 1 / 16e6, atol=1e-12)
    settings.pop_latency_ns += 50
    assert np.allclose(drv.timing(burst, settings)["sample"] - shifted, -50e-9, atol=1e-12)
    # A burst without a switch has no connection instant.
    parked = proto.BurstData124(**{**burst.__dict__, "switch_begin_cycles": 0, "switch_end_cycles": 0})
    assert "connect" not in drv.timing(parked, settings)


# -------------------------------------------------------------------- accuracy
def run_one(r_bias, c_total_pf, c_d_pf=12.0, noise=1.0, seed=5, channels=((1, 1, 0),), **cfg):
    settings = Settings.for_board(BOARD)
    for ref in channels:
        settings.bias[driver().key(ref)] = r_bias
    overrides = {driver().key(ref): c_total_pf - c_d_pf for ref in channels}
    muxes = {"%d:%d" % ref[:2]: c_d_pf for ref in channels}
    session, device, _ = make_session(settings, c_pzt_overrides=overrides, c_d_overrides=muxes,
                                      c_pzt_spread=0.0, noise_lsb=noise, r_bias_tolerance=0.0,
                                      seed=seed, **cfg)
    results = session.run_manual(list(channels))
    return session, device, results


@pytest.mark.parametrize("r_bias", [470e3, 1e6])
@pytest.mark.parametrize("c_total_pf", [60.0, 80.0, 100.0])
def test_accuracy_with_one_lsb_noise(r_bias, c_total_pf):
    """C_total within 2 % and C_D/C_total within 0.01 for ~1 LSB rms noise (LSB = 0.806 mV)."""
    session, device, results = run_one(r_bias, c_total_pf)
    truth = device.true_values(1, 1, 0)
    mean = results[0].aggregate.mean
    assert mean["c_total"] == pytest.approx(truth["c_total"], rel=0.02)
    assert abs(mean["ratio"] - truth["ratio"]) < 0.01
    assert 0.5 < mean["residual_rms_lsb"] < 1.6 and not results[0].flags


def test_four_interleaved_channels_from_one_burst_each_get_their_own_fit():
    channels = ((1, 1, 2), (1, 2, 2), (1, 3, 2), (1, 4, 2))
    settings = Settings.for_board(BOARD)
    session, device, _ = make_session(settings, noise_lsb=1.0, r_bias_tolerance=0.0, seed=21)
    results = session.run_manual(list(channels))
    assert len(burst_commands(device)) == 1 and burst_commands(device)[0].startswith("capburst 1 2 15 ")
    assert [r.ref for r in results] == list(channels)
    taus = []
    for r in results:
        truth = device.true_values(*r.ref)
        assert r.aggregate.mean["c_total"] == pytest.approx(truth["c_total"], rel=0.02), r.ref
        assert abs(r.aggregate.mean["ratio"] - truth["ratio"]) < 0.015, r.ref
        taus.append(r.aggregate.mean["tau"])
    # Array 1 has 470 k on MUX1/3 and 1 M on MUX2/4: tau follows R_bias.
    assert min(taus[1], taus[3]) > 1.5 * max(taus[0], taus[2])
    assert session.raw[0].packet == session.raw[1].packet == session.raw[3].packet


def test_charge_injection_pedestal_moves_the_ratio_but_not_tau():
    _, _, clean = run_one(470e3, 80.0)
    _, _, pedestal = run_one(470e3, 80.0, vmid_pedestal_v=0.044)
    a, b = clean[0].aggregate.mean, pedestal[0].aggregate.mean
    assert b["tau"] == pytest.approx(a["tau"], rel=0.01)
    assert b["ratio"] != pytest.approx(a["ratio"], abs=0.003)


def test_sensor_state_survives_the_hold_and_latency_assumption_errors():
    """The simulated ADC holds at 3.3 clocks and the stamp latency is 80 ns; the analysis assumes
    3 clocks and 100 ns. The effect on C_total stays below the 2 % budget."""
    session, device, results = run_one(1e6, 80.0)
    session.settings.hold_clocks, session.settings.pop_latency_ns = 3.3, 80.0
    exact = session.reanalyze()
    assert exact
    truth = device.true_values(1, 1, 0)
    assert session.results[(1, 1, 0)].aggregate.mean["c_total"] == pytest.approx(truth["c_total"], rel=0.01)
    assert results[0].aggregate.mean["c_total"] == pytest.approx(truth["c_total"], rel=0.02)


def test_quality_flags_for_damaged_or_odd_bursts():
    session, device, _ = make_session(noise_lsb=1.0, seed=2)
    drv = driver()
    settings = session.settings
    outcome = drv.measure(session.client, drv.single((1, 1, 0)), settings)
    burst = outcome.decoded

    def analyze(b, ref=(1, 1, 0)):
        return drv.analyze(b, [ref], settings, {ref: None})[ref]

    assert analyze(burst).flags == []
    broken = proto.BurstData124(**{**burst.__dict__, "words": burst.words.copy()})
    broken.words[burst.pre_frames + 20] |= 0x3000                               # a data error
    assert capfit.DATA_ERRORS in analyze(broken).flags
    aborted = proto.BurstData124(**{**burst.__dict__, "flags": proto.FLAG_ABORTED})
    assert capfit.INCOMPLETE in analyze(aborted).flags
    slow = proto.BurstData124(**{**burst.__dict__, "flags": proto.FLAG_SWITCH_SLOW | proto.FLAG_STAMP_GAP})
    assert {capfit.SWITCH_JITTER, capfit.STAMP_GAP} <= set(analyze(slow).flags)
    fast_clock = proto.BurstData124(**{**burst.__dict__, "spi_clock_hz": 20_000_000})
    assert capfit.CLOCK_OUT_OF_SPEC in analyze(fast_clock).flags
    # A channel the burst did not convert (other address / other input) cannot be analysed.
    assert capfit.INCOMPLETE in analyze(burst, (1, 2, 0)).flags
    assert capfit.INCOMPLETE in analyze(burst, (1, 1, 1)).flags
    no_switch = proto.BurstData124(**{**burst.__dict__, "switch_begin_cycles": 0, "switch_end_cycles": 0})
    assert capfit.INCOMPLETE in analyze(no_switch).flags


def test_frame_rate_pattern_is_flagged():
    session, device, results = run_one(470e3, 80.0, frame_pattern_lsb=3.0)
    assert capfit.PERIODIC_RESIDUAL in results[0].flags


def test_stalled_frames_do_not_bias_the_result():
    session, device, results = run_one(470e3, 80.0, stall_probability=0.03)
    truth = device.true_values(1, 1, 0)
    assert results[0].aggregate.mean["c_total"] == pytest.approx(truth["c_total"], rel=0.03)
