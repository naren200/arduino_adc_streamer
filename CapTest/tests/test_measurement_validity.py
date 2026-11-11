"""Plan 4.3 / 4.4 / 5: validity flags, c_total_method, and freshness kept across sessions."""

import json

import numpy as np
import pytest

from captest import capfit, runner
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings
from captest.sim import SimConfig, SimLink, SimulatedDevice


def make_session(state_file=None, simulated=True, device=None, clock=None, **config):
    clock = clock or VirtualClock()
    settings = Settings()
    device = device or SimulatedDevice(SimConfig(**config), clock=clock, r_bias_table=settings.bias)
    session = runner.MeasurementSession(CapTestClient(SimLink(device)), settings, clock,
                                        simulated=simulated, state_file=state_file)
    return session, device, clock


def burst_measurement(tau_scale, seed):
    """A 7953 single-connection measurement whose tau is scaled (bias resistor stand-in)."""
    settings = Settings(method="burst")
    device = SimulatedDevice(SimConfig(seed=seed, c_pzt_spread=0.0, r_bias_tolerance=0.0),
                             clock=VirtualClock(), r_bias_table=settings.bias)
    burst = CapTestClient(SimLink(device)).burst(2, 0, 300, 200, 200, 25)
    return capfit.analyze_burst(burst, 470e3 * tau_scale, settings.analysis())


def test_unstable_tau_flags_a_channel_whose_bursts_disagree():
    steady = capfit.aggregate([burst_measurement(1.0, 1), burst_measurement(1.0, 2),
                               burst_measurement(1.0, 3)])
    assert capfit.UNSTABLE_TAU not in steady.flags
    # tau is the same, R_bias differs: C_total changes but tau does not, so build the spread
    # on tau directly.
    ms = [burst_measurement(1.0, seed) for seed in (1, 2, 3)]
    for m, factor in zip(ms, (1.0, 1.2, 0.85)):
        m.tau *= factor
    assert capfit.UNSTABLE_TAU in capfit.aggregate(ms).flags


def test_single_burst_is_never_unstable():
    assert capfit.UNSTABLE_TAU not in capfit.aggregate([burst_measurement(1.0, 1)]).flags


def test_single_connection_marks_step_values_not_valid_and_a_relative_reading():
    m = burst_measurement(1.0, 1)
    assert not m.step_valid and capfit.FRAME_DEPENDENT in m.flags and m.ok
    assert m.c_total_method == capfit.C_TOTAL_TAU_OVER_R
    agg = capfit.aggregate([m])
    assert np.isfinite(agg.mean["c_total"]) and np.isnan(agg.mean["ratio"])
    assert np.isnan(agg.mean["v0"]) and np.isnan(agg.mean["c_pzt"])


def test_alternate_method_is_labelled_and_keeps_its_share_value():
    session, _, _ = make_session()
    result = session.measure_channel((2, 5))
    assert result.c_total_method == capfit.C_TOTAL_VMID_CYCLES
    assert capfit.FRAME_DEPENDENT not in result.flags
    assert result.row()["c_total_method"] == capfit.C_TOTAL_VMID_CYCLES


def test_second_session_on_the_same_boot_is_not_fresh(tmp_path):
    state = tmp_path / "TestBoard_7953_session.json"
    first, device, clock = make_session(state)
    assert first.measure_channel((2, 5)).fresh
    assert json.loads(state.read_text())["connected"] == [[2, 5]]
    second, _, _ = make_session(state, device=device, clock=clock)
    assert not second.measure_channel((2, 5)).fresh        # connected by the first session
    assert second.measure_channel((2, 6)).fresh            # never connected on this boot


def test_a_reboot_makes_the_sensors_fresh_again(tmp_path):
    state = tmp_path / "s.json"
    first, device, clock = make_session(state)
    first.measure_channel((2, 5))
    device.power_cycle()
    clock.advance(1.0)
    second, _, _ = make_session(state, device=device, clock=clock)
    assert second.measure_channel((2, 5)).fresh


def test_a_board_the_app_has_no_record_of_is_unknown_not_fresh(tmp_path):
    state = tmp_path / "s.json"
    session, device, clock = make_session(state, simulated=False)
    result = session.measure_channel((2, 5))
    assert result.fresh is None and session.is_fresh((2, 6)) is None
    assert session.is_fresh((2, 5)) is False                 # connected just now
    assert result.row()["fresh"] is None
    # The record of an incomplete history does not turn into a complete one later.
    later, _, _ = make_session(state, simulated=False, device=device, clock=clock)
    assert later.measure_channel((2, 6)).fresh is None
    # Seeing the board reboot gives a known history.
    device.power_cycle()
    clock.advance(1.0)
    later.refresh_boot()
    assert later.is_fresh((2, 5)) is True


def test_simulated_sessions_keep_no_state_file():
    session, _, _ = make_session()
    assert session.state_file is None
