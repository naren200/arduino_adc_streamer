"""End-to-end runs on the simulated device, parametrised over EVERY board of boards.json:
automatic mode (all channels, address-grouped order, progress, Stop leaves the board
parked) and manual mode (selected channels, the "too soon" warning including side-effect
connections, boot-id reset), the discharge check, exports, raw files and the CLI."""

import csv
import json

import pytest

from captest import capfit, cli, runner
from captest.boards import DEFAULT_BOARD_ID, get_registry, load_driver
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings
from captest.simbase import SimLink

BOARDS = [e.id for e in get_registry().enabled]


def make(board_id, **config):
    driver = load_driver(board_id)
    settings = Settings() if board_id == DEFAULT_BOARD_ID else Settings.for_board(board_id)
    clock = VirtualClock()
    device = driver.create_simulator(clock, settings.bias, driver.sim_config(**config))
    session = runner.MeasurementSession(CapTestClient(SimLink(device)), settings, clock,
                                        simulated=True, driver=driver)
    return session, device, clock, driver


def bursts(device):
    return [c for c in device.commands if c.startswith(("capburst", "capalt"))]


def test_every_board_is_covered():
    assert set(BOARDS) >= {"TestBoard_7953", "TestBoard_ADC124"}


@pytest.mark.parametrize("board", BOARDS)
def test_auto_mode_all_channels_order_and_progress(board):
    session, device, clock, driver = make(board)
    progress = []
    session.on_progress = progress.append
    results = session.run_auto()
    expected = driver.auto_channels()
    plan = driver.plan(expected, session.settings)
    assert {r.ref for r in results} == set(expected) and len(results) == len(expected)
    assert [r.ref for r in results] == [ref for c in plan for ref in c.reads]    # connection order
    assert all(not r.failed for r in results)
    assert len(bursts(device)) == len(plan)                                      # one burst per connection
    measuring = [p.done for p in progress if p.message.startswith("measuring")]
    assert measuring == list(range(len(plan)))
    assert progress[-1].message == "finished" and progress[-1].done == len(plan)
    assert device.parked and device.commands[-1] == "capabort"
    for r in results:
        truth = device.true_values(*r.ref)
        assert r.fresh and not r.flags, (r.ref, r.flags)
        assert r.aggregate.mean["c_total"] == pytest.approx(truth["c_total"], rel=0.03), r.ref
        name = "c_share_ratio" if r.c_total_method == "vmid_cycles" else "ratio"    # 7953 default: C_share
        assert abs(r.aggregate.mean[name] - truth["ratio"]) < 0.015, r.ref


def test_adc124_automatic_mode_uses_one_burst_per_address_and_both_modes_agree():
    session, device, _, driver = make("TestBoard_ADC124")
    session.run_auto()
    commands = bursts(device)
    assert len(commands) == 14                                      # 7 addresses x 2 arrays
    assert [c.split()[1:3] for c in commands] == [[str(a), str(x)] for a in (1, 2) for x in range(7)]
    assert [c.split()[3] for c in commands[:7]] == ["15"] * 6 + ["8"]          # address 6: MUX4 only
    per_channel, device2, _, _ = make("TestBoard_ADC124")
    per_channel.run_auto([1], per_address=False)
    assert len(bursts(device2)) == 25 and {c.split()[3] for c in bursts(device2)} <= {"1", "2", "4", "8"}
    for ref, result in per_channel.results.items():
        assert result.aggregate.mean["c_total"] == pytest.approx(
            session.results[ref].aggregate.mean["c_total"], rel=0.05), ref


@pytest.mark.parametrize("board", BOARDS)
def test_auto_mode_subset_of_groups(board):
    session, device, _, driver = make(board)
    first = driver.groups()[0]
    results = session.run_auto([first.id])
    assert {r.ref for r in results} == set(first.channels)
    assert all(driver.group_of(r.ref) == first.id for r in results)


@pytest.mark.parametrize("board", BOARDS)
def test_auto_stop_leaves_board_parked(board):
    session, device, _, driver = make(board)
    count = {"n": 0}
    session.on_burst = lambda m: count.__setitem__("n", count["n"] + 1)
    session.should_stop = lambda: count["n"] >= 7
    results = session.run_auto()
    assert 7 <= len(results) < len(driver.auto_channels())
    assert device.parked and device.commands[-1] == "capabort"


@pytest.mark.parametrize("board", BOARDS)
def test_failed_connection_is_recorded_the_run_continues_and_it_can_be_remeasured(board):
    session, device, _, driver = make(board)
    state = {"calls": 0}

    def flaky(original):
        def handler(arguments):
            state["calls"] += 1
            if state["calls"] == 2:
                device.corrupt_next_packet = True
            return original(arguments)
        return handler

    device._cmd_burst = flaky(device._cmd_burst)
    if hasattr(device, "_cmd_alternate"):
        device._cmd_alternate = flaky(device._cmd_alternate)
    group = driver.groups()[-1].id
    results = session.run_auto([group])
    plan = driver.plan(driver.auto_channels([group]), session.settings)
    failed = session.failed_channels()
    assert sorted(failed) == sorted(plan[1].reads) and len(results) == len(driver.auto_channels([group]))
    assert all(session.results[c].failed for c in failed)
    again = session.remeasure_failed()
    assert sorted(r.ref for r in again) == sorted(failed) and not any(r.failed for r in again)
    assert session.failed_channels() == []


@pytest.mark.parametrize("board", BOARDS)
def test_auto_multiple_bursts_respects_discharge(board):
    session, device, clock, driver = make(board)
    session.settings.discharge_ms = 400
    session.settings.bursts = 3
    logs = []
    session.on_log = logs.append
    group = driver.groups()[0].id
    results = session.run_auto([group])
    plan = driver.plan(driver.auto_channels([group]), session.settings)
    assert all(r.aggregate.count == 3 for r in results)
    # Pass-major order: at most the first connection of pass 2 and 3 has to wait (the others
    # were measured long enough ago); no result is flagged as measured too soon.
    assert 1 <= sum("discharging" in line for line in logs) <= 2 * len(plan)
    for r in results:
        assert capfit.TOO_SOON not in r.flags


@pytest.mark.parametrize("board", BOARDS)
def test_manual_selected_channels_and_too_soon_warning(board):
    session, device, clock, driver = make(board)
    warnings = []
    session.on_warning = warnings.append
    channels = driver.channels()[1:3] + [driver.channels()[-1]]
    first = session.run_manual(channels)
    assert sorted(r.ref for r in first) == sorted(channels) and not warnings
    assert all(r.fresh and not r.failed for r in first)
    target = channels[1]
    clock.advance(0.020)
    result = session.measure_channel(target)
    assert len(warnings) == 1 and driver.display(target) in warnings[0]
    assert capfit.TOO_SOON in result.flags and capfit.VS0_SUSPECT in result.flags and not result.fresh
    clock.advance(0.5)
    count = len(warnings)
    again = session.measure_channel(target)
    assert len(warnings) == count and capfit.TOO_SOON not in again.flags
    assert session.since_last_s(target) == pytest.approx(0.0, abs=1e-6)


def test_adc124_connections_touch_the_neighbouring_muxes_too():
    """Measuring MUX1 at an address connects the sensors of MUX2..4 at that address as well:
    they are no longer fresh and measuring them soon afterwards is warned about."""
    session, device, clock, driver = make("TestBoard_ADC124")
    warnings = []
    session.on_warning = warnings.append
    session.run_manual([(1, 1, 3)])
    neighbours = [(1, 2, 3), (1, 3, 3), (1, 4, 3)]
    for ref in neighbours:
        assert not session.is_fresh(ref) and session.since_last_s(ref) is not None
    assert session.is_fresh((1, 1, 4)) and session.is_fresh((2, 1, 3))        # other address / array
    clock.advance(0.020)
    result = session.measure_channel((1, 3, 3))
    assert not result.fresh and capfit.TOO_SOON in result.flags
    assert len(warnings) == 1 and "1:3:3" in warnings[0]
    # Reading several muxes in one burst: the warning names every one that was connected too recently.
    warnings.clear()
    clock.advance(0.020)
    session.run_manual([(1, 2, 3), (1, 4, 3)])
    assert len(warnings) == 2 and "1:2:3" in warnings[0] and "1:4:3" in warnings[1]


@pytest.mark.parametrize("board", BOARDS)
def test_boot_id_reset_restores_fresh_flags(board):
    session, device, clock, driver = make(board)
    channel = driver.channels()[0]
    session.measure_channel(channel)
    assert not session.is_fresh(channel)
    logs = []
    session.on_log = logs.append
    device.power_cycle()
    clock.advance(1.0)
    session.refresh_boot()
    assert session.is_fresh(channel) and session.since_last_s(channel) is None
    assert any("rebooted" in line for line in logs)
    assert session.measure_channel(channel).fresh


@pytest.mark.parametrize("board", BOARDS)
def test_discharge_check_plateau(board):
    session, device, clock, driver = make(board, leak_tau_s=0.02)
    channel = driver.parse_spec(driver.discharge_check["default_channel"])[0]
    result = session.discharge_check(channel)
    waits = driver.discharge_check["waits_ms"]
    assert [p["wait_ms"] for p in result["points"]] == list(waits)
    assert result["plateau"] and result["channel"] == driver.key(channel)
    v0 = [p["v0"] for p in result["points"]]
    assert v0[0] > v0[1] > v0[-1]
    assert result["sufficient_wait_ms"] in waits[2:]
    assert device.commands[-1] == "capabort"


@pytest.mark.parametrize("board", BOARDS)
def test_save_and_reload_raw_for_offline_reanalysis(board, tmp_path):
    session, device, clock, driver = make(board)
    channels = driver.channels()
    session.run_manual([channels[0], channels[-1]] + ([channels[1]] if board == "TestBoard_ADC124" else []))
    raw = tmp_path / "raw.json"
    session.save_raw(raw)
    data = json.loads(raw.read_text())
    assert data["format"] == ("captest-raw-1" if board == DEFAULT_BOARD_ID else "captest-raw-2")
    saved, _ = runner.load_raw(raw)
    assert saved.board_id == board
    results = runner.reanalyze(raw)
    live = {r.ref: r for r in session.results.values()}
    assert {r.ref for r in results} == set(live)
    for r in results:
        assert r.aggregate.mean["c_total"] == pytest.approx(live[r.ref].aggregate.mean["c_total"], rel=1e-9)
    other = Settings() if board == DEFAULT_BOARD_ID else Settings.for_board(board)
    ref = results[0].ref
    other.bias[driver.key(ref)] = session.settings.r_bias(*ref) * 2
    changed = {r.ref: r for r in runner.reanalyze(raw, other)}
    assert changed[ref].aggregate.mean["c_total"] == pytest.approx(
        live[ref].aggregate.mean["c_total"] / 2, rel=1e-6)
    wrong = Settings.for_board("TestBoard_ADC124") if board == DEFAULT_BOARD_ID else Settings()
    with pytest.raises(ValueError):
        runner.reanalyze(raw, wrong)


def test_adc124_raw_file_stores_a_shared_packet_once(tmp_path):
    session, device, clock, driver = make("TestBoard_ADC124")
    session.run_manual([(1, m, 0) for m in (1, 2, 3, 4)])
    raw = tmp_path / "raw.json"
    session.save_raw(raw)
    data = json.loads(raw.read_text())
    assert len(data["bursts"]) == 4 and len(data["packets"]) == 1 and data["board"] == "TestBoard_ADC124"
    assert {tuple(b["ref"]) for b in data["bursts"]} == {(1, m, 0) for m in (1, 2, 3, 4)}


@pytest.mark.parametrize("board", BOARDS)
def test_exports_carry_the_board_columns(board, tmp_path):
    session, device, clock, driver = make(board)
    ref = driver.channels()[0]
    session.run_manual([ref])
    session.save_results(tmp_path / "r.json")
    session.save_csv(tmp_path / "r.csv")
    data = json.loads((tmp_path / "r.json").read_text())
    columns = [c[1] for c in driver.id_columns()]
    row = data["rows"][0]
    assert [row[c] for c in columns] == list(ref) and row["c_total_pf"] > 0
    assert ("board" in data) == (board != DEFAULT_BOARD_ID)
    header = next(csv.reader(open(tmp_path / "r.csv", newline="")))
    assert header[:len(columns) + 1] == columns + ["tau_us"]


@pytest.mark.parametrize("board", BOARDS)
def test_cli_auto_manual_and_reanalyze(board, tmp_path, capsys):
    out, raw = tmp_path / "res.json", tmp_path / "raw.json"
    driver = load_driver(board)
    first = driver.groups()[0]
    code = cli.main(["--board", board, "--simulate", "--auto", "--adc", str(first.id), "--out", str(out),
                     "--raw-out", str(raw), "--discharge-ms", "10"])
    assert code == 0 and len(json.loads(out.read_text())["rows"]) == len(first.channels)
    spec = driver.format_spec(driver.channels()[:3])
    code = cli.main(["--board", board, "--simulate", "--channels", spec, "--bursts", "2"])
    captured = capsys.readouterr().out
    assert code == 0 and driver.key(driver.channels()[2]) in captured
    assert "capacitance-test firmware" in captured
    assert cli.main(["--reanalyze", str(raw)]) == 0
    capsys.readouterr()
    assert cli.main(["--board", board, "--simulate", "--detect"]) == 0
    assert "captest of board " + driver.name in capsys.readouterr().out


def test_cli_board_selection_and_listing(capsys):
    assert cli.main(["--list-boards"]) == 0
    listing = capsys.readouterr().out
    assert "TestBoard_7953" in listing and "TestBoard_ADC124" in listing
    with pytest.raises(SystemExit):
        cli.main(["--board", "NoSuchBoard", "--simulate", "--status"])
    assert cli.resolve_board(cli.build_parser().parse_args(["--board", "TestBoard_ADC124"])) == "TestBoard_ADC124"
    assert cli.resolve_board(cli.build_parser().parse_args([])) == "TestBoard_7953"          # first entry
    assert cli.main(["--simulate", "--channels", "1:2", "--per-channel"]) == 0      # default board: 7953
    capsys.readouterr()


def test_cli_adc124_options(capsys, tmp_path):
    settings_file = tmp_path / "s.json"
    Settings.for_board("TestBoard_ADC124").save(settings_file)
    code = cli.main(["--settings", str(settings_file), "--simulate", "--channels", "1:1:0,1:2:0,1:4:6",
                     "--per-channel", "--vmid", "1"])
    out = capsys.readouterr().out
    assert code == 0 and "VMID array 1" in out and "1:4:6" in out
    code = cli.main(["--board", "TestBoard_ADC124", "--simulate", "--channels", "1:3:1", "--discharge-check",
                     "1:3:1", "--out", str(tmp_path / "bare.json")])
    assert code == 0


@pytest.mark.parametrize("board", BOARDS)
def test_cli_reports_production_firmware(board, capsys, monkeypatch):
    driver = load_driver(board)
    original = driver.sim_config
    monkeypatch.setattr(driver, "sim_config", lambda **kw: original(kind="production", **kw))
    if board == DEFAULT_BOARD_ID:
        monkeypatch.setattr(cli, "SimConfig", lambda **kw: original(kind="production", **kw))
    code = cli.main(["--board", board, "--simulate", "--status"])
    assert code == 2
    assert "production firmware is running" in capsys.readouterr().out


def test_cli_warns_when_the_attached_firmware_belongs_to_another_board(capsys, monkeypatch):
    other = load_driver("TestBoard_ADC124")
    device_factory = other.create_simulator
    driver7953 = load_driver("TestBoard_7953")
    monkeypatch.setattr(driver7953, "create_simulator", lambda clock=None, bias_table=None, config=None:
                        device_factory(clock, None, other.sim_config()))
    monkeypatch.setattr(cli, "SimulatedDevice", lambda config, clock=None, r_bias_table=None:
                        device_factory(clock, None, other.sim_config()))
    code = cli.main(["--board", "TestBoard_7953", "--simulate", "--status"])
    err = capsys.readouterr().err
    assert code == 2 and "belongs to TestBoard_ADC124" in err
