"""Board baseline: C_total with no sensor connected, subtracted on request (C_net)."""

import csv
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from captest import baseline as baseline_module, capfit, cli, runner
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings
from captest.sim import SimConfig, SimLink, SimulatedDevice

OPEN = {"2:5": 30.0, "2:6": 40.0}          # pF of the "board only" run
STRIP = {"2:5": 90.0, "2:6": 90.0}         # pF with a strip connected


@pytest.fixture(autouse=True)
def settings_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPTEST_SETTINGS_DIR", str(tmp_path / "settings"))
    return tmp_path / "settings"


def make_session(overrides, subtract=False):
    clock = VirtualClock()
    settings = Settings(subtract_baseline=subtract)
    device = SimulatedDevice(SimConfig(c_pzt_spread=0.0, c_pzt_overrides=overrides), clock=clock,
                             r_bias_table=settings.bias)
    return runner.MeasurementSession(CapTestClient(SimLink(device)), settings, clock, simulated=True)


def measured(session, channels=((2, 5), (2, 6))):
    session.on_warning = lambda text: session.warnings.append(text)
    session.warnings = []
    return [session.measure_channel(c) for c in channels]


def save_open_baseline():
    session = make_session(OPEN)
    results = measured(session)
    session.save_baseline()
    return {r.ref: r.row()["c_total_pf"] for r in results}


def test_net_is_the_total_minus_the_baseline_of_the_same_input():
    open_totals = save_open_baseline()
    session = make_session(STRIP, subtract=True)
    results = measured(session)
    assert session.warnings == []
    for result in results:
        row = result.row()
        assert row["c_board_pf"] == pytest.approx(open_totals[result.ref])
        assert row["c_net_pf"] == pytest.approx(row["c_total_pf"] - open_totals[result.ref])
        assert result.net_note == ""
    # the sensors differ by 60 pF and 50 pF between the two runs
    assert results[0].c_net_pf > results[1].c_net_pf
    assert results[0].c_net_pf - results[1].c_net_pf == pytest.approx(10.0, abs=4.0)
    assert results[0].c_net_pf == pytest.approx(60.0, rel=0.12)


def test_without_the_setting_nothing_is_subtracted_and_c_total_is_unchanged():
    save_open_baseline()
    plain = measured(make_session(STRIP))
    net = measured(make_session(STRIP, subtract=True))
    for a, b in zip(plain, net):
        assert np.isnan(a.row()["c_net_pf"]) and np.isnan(a.row()["c_board_pf"])
        assert a.row()["c_total_pf"] == pytest.approx(b.row()["c_total_pf"])


def test_the_switch_works_on_results_already_measured_and_after_re_analysis():
    save_open_baseline()
    session = make_session(STRIP)
    results = measured(session)
    assert all(np.isnan(r.c_net_pf) for r in results)
    session.settings.subtract_baseline = True
    session.apply_baseline(reload=True)
    assert all(np.isfinite(r.c_net_pf) for r in results)
    session.reanalyze()
    assert all(np.isfinite(r.c_net_pf) for r in session.results.values())
    session.settings.subtract_baseline = False
    session.apply_baseline()
    assert all(np.isnan(r.c_net_pf) for r in session.results.values())


def test_other_c_total_method_is_never_subtracted():
    save_open_baseline()
    session = make_session(STRIP, subtract=True)
    result = measured(session, [(2, 5)])[0]
    result.baseline.method = capfit.C_TOTAL_TAU_OVER_R
    assert np.isnan(result.c_net_pf) and np.isfinite(result.row()["c_total_pf"])
    assert "baseline by tau_over_r" in result.net_note


def test_input_missing_from_the_baseline_has_no_net_and_says_so():
    save_open_baseline()
    session = make_session({"2:7": 50.0}, subtract=True)
    result = measured(session, [(2, 7)])[0]
    assert np.isnan(result.c_net_pf) and np.isnan(result.row()["c_board_pf"])
    assert result.net_note == "no baseline for this input"


def test_a_missing_or_wrong_baseline_warns_once_instead_of_failing():
    session = make_session(STRIP, subtract=True)
    measured(session)
    assert len(session.warnings) == 1 and "no baseline is saved" in session.warnings[0]
    path = baseline_module.baseline_path("TestBoard_7953")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": "captest-baseline-1", "board": "TestBoard_ADC124",
                                "entries": {}}), encoding="utf-8")
    with pytest.raises(baseline_module.BaselineError):
        baseline_module.load("TestBoard_7953")
    session = make_session(STRIP, subtract=True)
    results = measured(session)
    assert "not used" in session.warnings[0] and all(np.isnan(r.c_net_pf) for r in results)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(baseline_module.BaselineError):
        baseline_module.load("TestBoard_7953")


def test_file_round_trip_keeps_values_method_and_settings():
    session = make_session(OPEN)
    measured(session)
    path = session.save_baseline()
    assert path == baseline_module.baseline_path("TestBoard_7953")
    loaded = baseline_module.load("TestBoard_7953")
    assert set(loaded.entries) == {(2, 5), (2, 6)} and loaded.methods() == [capfit.C_TOTAL_VMID_CYCLES]
    assert loaded.entry((2, 5)).c_total_pf == pytest.approx(session.results[(2, 5)].row()["c_total_pf"])
    assert loaded.mismatches(session.settings) == []
    other = Settings(vmid_frames=2)
    assert any("vmid_frames" in note for note in loaded.mismatches(other))
    with pytest.raises(baseline_module.BaselineError):
        baseline_module.load("TestBoard_ADC124", path)          # the file belongs to another board


def test_settings_json_is_unchanged_unless_the_option_is_on():
    assert "subtract_baseline" not in json.loads(Settings().to_json())
    assert json.loads(Settings(subtract_baseline=True).to_json())["subtract_baseline"] is True
    assert Settings.from_dict(json.loads(Settings(subtract_baseline=True).to_json())).subtract_baseline


def test_cli_makes_a_baseline_from_a_results_file_and_uses_it(tmp_path, capsys):
    session = make_session(OPEN)
    measured(session)
    results_file = tmp_path / "open.json"
    session.save_results(results_file)
    assert cli.main(["--make-baseline", str(results_file)]) == 0
    loaded = baseline_module.load("TestBoard_7953")
    assert set(loaded.entries) == {(2, 5), (2, 6)} and loaded.source == "open.json"
    assert loaded.entry((2, 5)).bursts == 1 and loaded.entry((2, 5)).method == capfit.C_TOTAL_VMID_CYCLES
    capsys.readouterr()
    assert cli.main(["--simulate", "--channels", "2:5,2:6", "--subtract-baseline"]) == 0
    out = capsys.readouterr().out
    assert "Cnet[pF]" in out
    assert cli.main(["--simulate", "--channels", "2:5"]) == 0
    assert "Cnet[pF]" not in capsys.readouterr().out
    assert cli.main(["--make-baseline", str(tmp_path / "missing.json")]) == 2


def test_cli_saves_the_results_of_a_run_as_the_baseline(capsys):
    assert cli.main(["--simulate", "--channels", "2:5,2:6", "--save-baseline"]) == 0
    assert set(baseline_module.load("TestBoard_7953").entries) == {(2, 5), (2, 6)}


def test_a_failed_run_cannot_become_a_baseline():
    session = make_session(OPEN)
    with pytest.raises(baseline_module.BaselineError):
        session.save_baseline()


def test_baseline_columns_are_exported(tmp_path):
    save_open_baseline()
    session = make_session(STRIP, subtract=True)
    results = measured(session)
    path = tmp_path / "r.csv"
    runner.write_results_csv(results, session.settings, path)
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert {"c_board_pf", "c_net_pf"} <= set(rows[0]) and float(rows[0]["c_net_pf"]) > 0


# ------------------------------------------------------------------------------ GUI
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")


@pytest.fixture
def window(settings_folder):
    from captest import gui

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    win = gui.MainWindow()
    win.sim_virtual_clock = True
    win.sim_check.setChecked(True)
    win.connect_device()
    yield win
    win.disconnect_device()
    win.close()
    del app


def test_gui_switch_shows_c_net_in_the_table_and_the_chart(window):
    from captest import gui

    columns = [c[1] for c in gui.COLUMNS]
    net_col = columns.index("c_net_pf")
    board_col = columns.index("c_board_pf")
    window.spec_edit.setText("2:5,2:6")
    assert window.apply_spec() and window.measure_selected() and window.wait_idle()
    assert window.table.item(0, net_col).text() == "-" and "No board baseline" in window.baseline_label.text()
    window._confirm = lambda text, title="": True
    assert window.save_board_baseline()
    assert "2 inputs" in window.baseline_label.text()
    assert window.table.item(0, net_col).text() == "-"            # saved, but the switch is still off
    window.s_subtract_baseline.setChecked(True)
    assert window.settings.subtract_baseline and window.session.settings.subtract_baseline
    assert float(window.table.item(0, board_col).text()) > 0
    assert abs(float(window.table.item(0, net_col).text())) < 1e-6     # same run: C_total equals its baseline
    assert "C_net" in window.bar_total.titleLabel.text
    window.s_subtract_baseline.setChecked(False)
    assert window.table.item(0, net_col).text() == "-" and "C_total" in window.bar_total.titleLabel.text


def test_gui_save_without_results_does_nothing(window):
    assert window.save_board_baseline() is False
    assert "Nothing to save" in window.log_view.toPlainText()
