"""One- versus two-exponential fit, the model selection, and the start-voltage check."""

from pathlib import Path

import numpy as np
import pytest

from captest import capfit, cli, runner
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings
from captest.sim import SimConfig, SimLink, SimulatedDevice

REAL_RAW = (Path(__file__).resolve().parents[2] / "docs" / "testing" / "results" / "testboard-7953"
            / "capacitance-test" / "sensors_attached_raw.json")


def two_component_burst(fast_fraction=0.4, c_pzt=30.0, noise=1.0, adc=3, channel=0, seed=11, **cfg):
    settings = Settings(method="burst")
    clock = VirtualClock()
    device = SimulatedDevice(
        SimConfig(c_pzt_pf=c_pzt, c_pzt_spread=0.0, noise_lsb=noise, r_bias_tolerance=0.0, seed=seed,
                  fast_fraction=fast_fraction, fast_tau_ratio=0.4, **cfg),
        clock=clock, r_bias_table=settings.bias)
    client = CapTestClient(SimLink(device))
    burst = client.burst(adc, channel, 600, 200, 200, 25)
    return burst, device, settings


def analyze(burst, model, settings=None):
    settings = settings or Settings(method="burst")
    settings.fit_model = model
    return capfit.analyze_burst(burst, settings.r_bias(burst.adc, burst.channel), settings.analysis())


def test_two_exponential_recovers_both_time_constants():
    burst, device, settings = two_component_burst()
    truth = device.true_values(3, 0)
    m = analyze(burst, capfit.FIT_TWO)
    assert m.fit_model_used == capfit.FIT_TWO
    assert m.tau_slow == pytest.approx(truth["tau"], rel=0.04)
    assert m.tau_fast == pytest.approx(0.4 * truth["tau"], rel=0.10)
    assert m.fast_fraction == pytest.approx(0.4, abs=0.08)
    assert m.v0 == pytest.approx(truth["ratio"] * 1.25, abs=0.015)
    assert m.residual_rms_lsb < 1.6
    assert m.c_total == pytest.approx(truth["c_total"], rel=0.04)


def test_one_exponential_is_biased_and_the_shift_is_reported():
    burst, device, _ = two_component_burst()
    truth = device.true_values(3, 0)
    one = analyze(burst, capfit.FIT_ONE)
    two = analyze(burst, capfit.FIT_TWO)
    assert one.fit_model_used == capfit.FIT_ONE
    assert one.residual_rms_lsb > 3 * two.residual_rms_lsb          # structured misfit
    assert one.c_total < 0.9 * truth["c_total"]                      # biased low
    assert one.c_total_shift_pct == pytest.approx(0.0, abs=1e-9)
    # The two-exponential result reports its effect against the single exponential ...
    assert two.c_total_shift_pct == pytest.approx((two.c_total / one.c_total - 1) * 100, rel=1e-6)
    assert two.c_total_shift_pct > 10
    # ... and the single-exponential run still knows the two-exponential numbers.
    assert one.tau_slow == pytest.approx(one.tau)
    assert np.isfinite(one.rms_2exp) and np.isfinite(one.v0_2exp)
    assert one.other_label == "two exponentials" and len(one.fitted_other) == len(one.t)
    assert two.other_label == "single exponential" and len(two.fitted_other) == len(two.t)


def test_two_exponential_falls_back_when_one_is_enough():
    burst, device, _ = two_component_burst(fast_fraction=0.0)
    m = analyze(burst, capfit.FIT_TWO)
    assert m.fit_model_requested == capfit.FIT_TWO and m.fit_model_used == capfit.FIT_ONE
    assert np.isnan(m.tau_fast)
    assert m.c_total == pytest.approx(device.true_values(3, 0)["c_total"], rel=0.03)
    assert m.c_total_shift_pct == pytest.approx(0.0, abs=1e-9)


def test_fit_model_setting_round_trip_and_cli_option(tmp_path, capsys):
    settings = Settings(method="burst")
    assert settings.fit_model == capfit.FIT_TWO
    settings.fit_model = capfit.FIT_ONE
    settings.save(tmp_path / "s.json")
    assert Settings.load(tmp_path / "s.json").analysis().fit_model == capfit.FIT_ONE
    settings.fit_model = "garbage"
    assert settings.analysis().fit_model == capfit.FIT_TWO
    code = cli.main(["--simulate", "--channels", "2:0", "--fit", "1exp", "--method", "burst"])
    out = capsys.readouterr().out
    assert code == 0 and " 1exp " in out


def make_session(**config):
    clock = VirtualClock()
    settings = Settings(method="burst")
    device = SimulatedDevice(SimConfig(**config), clock=clock, r_bias_table=settings.bias)
    return runner.MeasurementSession(CapTestClient(SimLink(device)), settings, clock,
                                     simulated=True), device, settings


# The start-voltage check needs V0 to follow the charge-sharing model. The 7953 single
# connection does not provide that (plan 4.3), so the check is exercised on the ADC124.
def make_session_124(**config):
    from helpers124 import make_session as make
    session, device, _ = make(**config)
    return session, device


def test_start_voltage_flag_marks_only_the_charged_sensor():
    session, device = make_session_124(v_s_overrides={"2:1:3": 0.4}, leak_tau_s=1e9)
    session.run_manual([(2, 1, i) for i in range(6)])
    flagged = {c: r.vs0_est_v for c, r in session.results.items() if capfit.VS0_HIGH in r.flags}
    assert set(flagged) == {(2, 1, 3)}
    assert flagged[(2, 1, 3)] == pytest.approx(0.4, abs=0.06)
    others = [r.vs0_est_v for c, r in session.results.items() if c != (2, 1, 3)]
    assert max(abs(x) for x in others) < 0.03


def test_start_voltage_flag_is_two_sided():
    session, _ = make_session_124(v_s_overrides={"2:1:2": -0.3}, leak_tau_s=1e9)
    session.run_manual([(2, 1, i) for i in range(6)])
    assert capfit.VS0_HIGH in session.results[(2, 1, 2)].flags
    assert session.results[(2, 1, 2)].vs0_est_v < -capfit.VS0_THRESHOLD_V


def test_start_voltage_flag_updates_other_channels_as_results_arrive():
    session, _ = make_session_124(v_s_overrides={"2:1:1": 0.3}, leak_tau_s=1e9)
    seen = []
    session.on_result = lambda r: seen.append((r.ref, tuple(r.flags)))
    session.run_manual([(2, 1, i) for i in range(4)])
    assert not any(capfit.VS0_HIGH in flags for _, flags in seen)       # fewer than 5 channels: not judged
    session.run_manual([(2, 1, 4), (2, 1, 5)])
    assert capfit.VS0_HIGH in session.results[(2, 1, 1)].flags
    # The earlier channel was re-reported when the flag appeared.
    assert any(ref == (2, 1, 1) and capfit.VS0_HIGH in flags for ref, flags in seen)


def test_7953_single_connection_is_not_judged_for_start_voltage():
    session, device, _ = make_session(fast_fraction=0.4, v_s_overrides={"2:3": 0.4}, leak_tau_s=1e9)
    session.run_manual([(2, c) for c in range(10)])
    for result in session.results.values():
        assert capfit.VS0_HIGH not in result.flags and np.isnan(result.vs0_est_v)
        assert capfit.FRAME_DEPENDENT in result.flags and result.c_total_method == "tau_over_r"
        mean = result.aggregate.mean
        assert all(np.isnan(mean[name]) for name in ("v0", "ratio", "c_d", "c_sensor_side", "c_pzt"))
        assert np.isfinite(mean["c_total"])
    # tau (and therefore the slow-time-constant C_total) is not affected by the start voltage.
    truth = device.true_values(2, 3)
    assert session.results[(2, 3)].aggregate.mean["c_total"] == pytest.approx(truth["c_total"], rel=0.05)


def test_start_voltage_check_needs_enough_channels():
    points = {(1, c): (0.05, 20e-6) for c in range(4)}
    assert all(np.isnan(v) for v in capfit.start_voltage_check(points).values())
    points = {(1, c): (0.05, 20e-6) for c in range(8)}
    points[(1, 3)] = (0.30, 20e-6)
    estimate = capfit.start_voltage_check(points)
    assert estimate[(1, 3)] > capfit.VS0_THRESHOLD_V and abs(estimate[(1, 0)]) < 1e-6


def test_exports_carry_the_new_columns(tmp_path):
    session, _, _ = make_session(fast_fraction=0.4)
    session.run_manual([(3, 0)])
    row = session.results[(3, 0)].row()
    for name in ("fit_model", "tau_slow_us", "tau_fast_us", "fast_pct", "c_total_1exp_pf",
                 "c_total_shift_pct", "v0_1exp_v", "v0_2exp_v", "vs0_est_v"):
        assert name in row
    assert row["fit_model"] == capfit.FIT_TWO and row["tau_fast_us"] < row["tau_slow_us"]
    session.save_csv(tmp_path / "r.csv")
    header = (tmp_path / "r.csv").read_text().splitlines()[0]
    assert "tau_fast_us" in header and "vs0_est_v" in header


@pytest.mark.skipif(not REAL_RAW.exists(), reason="recorded sensor data not available")
def test_recorded_sensor_data_needs_and_gets_two_exponentials():
    """50 channels recorded on TestBoard_7953 with PZT strips attached (2026-10-23)."""
    two = {(r.adc, r.channel): r for r in runner.reanalyze(REAL_RAW, _settings(capfit.FIT_TWO))}
    one = {(r.adc, r.channel): r for r in runner.reanalyze(REAL_RAW, _settings(capfit.FIT_ONE))}
    assert len(two) == 50
    assert all(r.fit_used == capfit.FIT_TWO for r in two.values())
    rms2 = [r.aggregate.mean["residual_rms_lsb"] for r in two.values()]
    rms1 = [r.aggregate.mean["residual_rms_lsb"] for r in one.values()]
    assert np.median(rms2) < 1.5 and np.median(rms1) > 6
    # Same time constants on all four ADCs (not tied to R_bias), slow about 2-3x the fast one.
    for adc in (1, 2, 3, 4):
        slow = np.median([r.aggregate.mean["tau_slow"] for c, r in two.items() if c[0] == adc])
        fast = np.median([r.aggregate.mean["tau_fast"] for c, r in two.items() if c[0] == adc])
        assert 18e-6 < slow < 27e-6 and 6e-6 < fast < 12e-6
    shift = np.median([r.aggregate.mean["c_total_shift_pct"] for r in two.values()])
    assert 15 < shift < 35
    # Single connections: the step values and the start-voltage check are not exported.
    assert not any(capfit.VS0_HIGH in r.flags for r in two.values())
    assert all(capfit.FRAME_DEPENDENT in r.flags for r in two.values())
    assert all(np.isnan(r.aggregate.mean["ratio"]) for r in two.values())


def _settings(model):
    settings = runner.load_raw(REAL_RAW)[0]
    settings.fit_model = model
    return settings


def test_switching_the_fit_model_reanalyses_without_new_measurements():
    session, device, settings = make_session(fast_fraction=0.4, c_pzt_spread=0.1)
    session.run_manual([(3, c) for c in range(6)])
    commands = len(device.commands)
    served = device.bursts_served
    two = {c: r.aggregate.mean["c_total"] for c, r in session.results.items()}
    assert all(r.fit_used == capfit.FIT_TWO for r in session.results.values())

    settings.fit_model = capfit.FIT_ONE
    redone = session.reanalyze(settings)
    assert sorted(redone) == sorted(two)
    one = {c: r.aggregate.mean["c_total"] for c, r in session.results.items()}
    assert all(r.fit_used == capfit.FIT_ONE for r in session.results.values())
    assert all(one[c] < 0.9 * two[c] for c in two)                     # single exponential reads low
    # Nothing was sent to the board and no burst was taken.
    assert len(device.commands) == commands and device.bursts_served == served
    # Every result still carries the other model's value for comparison.
    for r in session.results.values():
        assert r.aggregate.mean["c_total_2exp"] == pytest.approx(two[(r.adc, r.channel)], rel=1e-9)

    settings.fit_model = capfit.FIT_TWO
    session.reanalyze(settings)
    again = {c: r.aggregate.mean["c_total"] for c, r in session.results.items()}
    assert again == pytest.approx(two, rel=1e-12)
    # Other analysis settings can be changed on the stored samples too.
    settings.bias["3:0"] = settings.bias["3:0"] * 2
    session.reanalyze(settings)
    assert session.results[(3, 0)].aggregate.mean["c_total"] == pytest.approx(two[(3, 0)] / 2, rel=1e-9)


def test_reanalysis_keeps_a_failed_channel_and_start_voltage_flags():
    session, device = make_session_124(v_s_overrides={"2:1:2": 0.35}, leak_tau_s=1e9)
    original = device._cmd_burst

    def flaky(arguments):
        if arguments.split()[:2] == ["2", "5"]:
            device.corrupt_next_packet = True
        return original(arguments)

    device._cmd_burst = flaky
    channels = [(2, 1, i) for i in range(6)]
    session.run_manual(channels, per_address=False)
    failed = session.failed_channels()
    assert failed and capfit.VS0_HIGH in session.results[(2, 1, 2)].flags
    session.reanalyze(session.settings)
    assert session.failed_channels() == failed
    assert capfit.VS0_HIGH in session.results[(2, 1, 2)].flags


def test_loaded_raw_results_can_be_reanalysed(tmp_path):
    session, device, settings = make_session(fast_fraction=0.4)
    session.run_manual([(2, c) for c in range(3)])
    session.save_raw(tmp_path / "raw.json")
    loaded = runner.reanalyze(tmp_path / "raw.json", settings)
    assert all(len(r.raw_bursts) == 1 for r in loaded)
    again = runner.analyze_recorded((2, 0), loaded[0].raw_bursts, _with_model(settings, capfit.FIT_ONE))
    assert again.fit_used == capfit.FIT_ONE


def _with_model(settings, model):
    settings.fit_model = model
    return settings
