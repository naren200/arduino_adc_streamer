"""End-to-end runs on the simulated device: automatic and manual modes, CLI."""

import json

import pytest

from captest import capfit, cli, protocol, runner
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings, all_channels, parse_channel_spec
from captest.sim import SimConfig, SimLink, SimulatedDevice


def make_session(**config):
    clock = VirtualClock()
    settings = Settings()
    device = SimulatedDevice(SimConfig(**config), clock=clock, r_bias_table=settings.bias)
    session = runner.MeasurementSession(CapTestClient(SimLink(device)), settings, clock,
                                        simulated=True)
    return session, device, clock


def test_auto_mode_all_channels_order_and_progress():
    session, device, clock = make_session()
    progress = []
    session.on_progress = progress.append
    results = session.run_auto()
    expected = all_channels()
    assert len(expected) == 50
    assert [(r.adc, r.channel) for r in results] == expected
    assert all(not r.failed for r in results)
    # Order of the bursts the device served == ADC1 0..9, ADC2 0..14, ...
    bursts = [c for c in device.commands if c.startswith(("capburst", "capalt"))]
    served = [(int(c.split()[1]), int(c.split()[2])) for c in bursts]
    assert served == expected
    assert [p.done for p in progress if p.message.startswith("measuring")] == list(range(50))
    assert progress[-1].message == "finished" and progress[-1].done == 50
    assert device.parked and device.commands[-1] == "capabort"
    # Every channel was fresh and accurate against ground truth.
    for r in results:
        truth = device.true_values(r.adc, r.channel)
        assert r.fresh
        assert r.aggregate.mean["c_total"] == pytest.approx(truth["c_total"], rel=0.03)
        # Vmid between samples gives C_share (= the simulated node capacitance), not C_D.
        assert abs(r.aggregate.mean["c_share_ratio"] - truth["ratio"]) < 0.015
        assert r.aggregate.mean["ratio"] != r.aggregate.mean["ratio"]


def test_auto_mode_subset_of_adcs():
    session, device, _ = make_session()
    results = session.run_auto([1, 4])
    assert {r.adc for r in results} == {1, 4} and len(results) == 25


def test_auto_stop_leaves_board_parked():
    session, device, _ = make_session()
    count = {"n": 0}

    def on_burst(m):
        count["n"] += 1

    session.on_burst = on_burst
    session.should_stop = lambda: count["n"] >= 7
    results = session.run_auto()
    assert len(results) == 7
    assert device.parked and device.commands[-1] == "capabort"


def test_auto_failed_channel_recorded_and_remeasured():
    session, device, _ = make_session()
    state = {"failed": 0}

    def flaky(original):
        def handler(arguments):
            if arguments.startswith("2 4 ") and state["failed"] == 0:
                state["failed"] = 1
                device.corrupt_next_packet = True
            return original(arguments)
        return handler

    device._cmd_burst = flaky(device._cmd_burst)
    device._cmd_alternate = flaky(device._cmd_alternate)
    results = session.run_auto([2])
    assert len(results) == 15
    assert session.failed_channels() == [(2, 4)]
    again = session.remeasure_failed()
    assert [(r.adc, r.channel) for r in again] == [(2, 4)] and not again[0].failed
    assert session.failed_channels() == []


def test_auto_multiple_bursts_respects_discharge():
    session, device, clock = make_session()
    session.settings.discharge_ms = 400
    session.settings.bursts = 3
    waits = []
    session.on_log = waits.append
    results = session.run_auto([1])      # 10 channels, pass-major, M=3
    assert all(r.aggregate.count == 3 for r in results)
    # Pass-major order: only the first channel of pass 2 and pass 3 has to wait; by
    # the time the other channels are revisited their discharge time has passed.
    assert sum("discharging" in line for line in waits) == 2
    for r in results:
        assert capfit.TOO_SOON not in r.flags


def test_manual_selected_channels_and_too_soon_warning():
    session, device, clock = make_session()
    warnings = []
    session.on_warning = warnings.append
    channels = parse_channel_spec("2:0,2:5,4:1-3")
    assert channels == [(2, 0), (2, 5), (4, 1), (4, 2), (4, 3)]
    first = session.run_manual(channels)
    assert [(r.adc, r.channel) for r in first] == channels and not warnings
    assert all(r.fresh for r in first)
    # Re-measure one channel immediately: warned, not blocked, flagged.
    clock.advance(0.020)
    result = session.measure_channel((2, 5))
    assert len(warnings) == 1 and "2:5" in warnings[0]
    assert capfit.TOO_SOON in result.flags and capfit.VS0_SUSPECT in result.flags
    assert not result.fresh
    # After the discharge time the warning does not repeat.
    clock.advance(0.5)
    n = len(warnings)
    again = session.measure_channel((2, 5))
    assert len(warnings) == n and capfit.TOO_SOON not in again.flags
    assert session.since_last_s((2, 5)) == pytest.approx(0.0, abs=1e-6)


def test_manual_too_soon_changes_v0_physically():
    session, device, clock = make_session(leak_tau_s=0.05)
    session.settings.method = "burst"       # V0 of the exponential fit (alternate leaves it out: ALT_SMALL_RANGE)
    first = session.measure_channel((2, 0))
    clock.advance(0.005)
    soon = session.measure_channel((2, 0))
    # The single-connection step values are not exported, but each burst still has its V0.
    assert soon.measurements[0].v0 > first.measurements[0].v0 + 0.02


def test_boot_id_reset_restores_fresh_flags():
    session, device, clock = make_session()
    session.measure_channel((1, 0))
    assert not session.is_fresh((1, 0))
    logs = []
    session.on_log = logs.append
    device.power_cycle()
    clock.advance(1.0)
    session.refresh_boot()
    assert session.is_fresh((1, 0)) and session.since_last_s((1, 0)) is None
    assert any("rebooted" in line for line in logs)
    again = session.measure_channel((1, 0))
    assert again.fresh


def test_discharge_check_plateau():
    session, device, clock = make_session(leak_tau_s=0.02)
    result = session.discharge_check((2, 0))
    assert result["plateau"]
    v0 = [p["v0"] for p in result["points"]]
    assert v0[0] > v0[1] > v0[-1]
    assert result["sufficient_wait_ms"] in (100, 300)
    assert device.commands[-1] == "capabort"


def test_save_and_reload_raw_for_offline_reanalysis(tmp_path):
    session, device, clock = make_session()
    session.run_manual([(2, 0), (4, 2)])
    raw = tmp_path / "raw.json"
    session.save_raw(raw)
    results = runner.reanalyze(raw)
    live = {(r.adc, r.channel): r for r in session.results.values()}
    for r in results:
        assert r.aggregate.mean["c_total"] == pytest.approx(
            live[(r.adc, r.channel)].aggregate.mean["c_total"], rel=1e-9)
    # New settings (a different R_bias) change C_total offline.
    other = Settings()
    other.bias["2:0"] = 940e3
    changed = runner.reanalyze(raw, other)
    c0 = [r for r in changed if (r.adc, r.channel) == (2, 0)][0]
    assert c0.aggregate.mean["c_total"] == pytest.approx(
        live[(2, 0)].aggregate.mean["c_total"] / 2, rel=1e-6)


def test_exports(tmp_path):
    session, _, _ = make_session()
    session.run_manual([(2, 0)])
    session.save_results(tmp_path / "r.json")
    session.save_csv(tmp_path / "r.csv")
    data = json.loads((tmp_path / "r.json").read_text())
    assert data["rows"][0]["adc"] == 2 and data["rows"][0]["c_total_pf"] > 0
    assert (tmp_path / "r.csv").read_text().startswith("adc,channel,tau_us")


def test_cli_auto_and_manual(tmp_path, capsys):
    out = tmp_path / "res.json"
    raw = tmp_path / "raw.json"
    code = cli.main(["--simulate", "--auto", "--adc", "1", "--out", str(out),
                     "--raw-out", str(raw), "--discharge-ms", "10"])
    assert code == 0
    assert len(json.loads(out.read_text())["rows"]) == 10
    code = cli.main(["--simulate", "--channels", "2:0,2:5,4:1-3", "--bursts", "2"])
    captured = capsys.readouterr().out
    assert code == 0 and "4:3" in captured and "capacitance-test firmware" in captured
    assert cli.main(["--reanalyze", str(raw)]) == 0


def test_cli_reports_production_firmware(capsys, monkeypatch):
    original = cli.SimConfig
    monkeypatch.setattr(cli, "SimConfig", lambda **kw: original(kind="production", **kw))
    code = cli.main(["--simulate", "--status"])
    assert code == 2
    assert "production firmware is running" in capsys.readouterr().out
