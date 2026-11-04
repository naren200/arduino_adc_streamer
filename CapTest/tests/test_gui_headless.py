"""Headless Qt test (QT_QPA_PLATFORM=offscreen): both run modes on the simulator."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from PyQt6 import QtCore
from captest import capfit, flasher, gui


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app):
    win = gui.MainWindow()
    win.sim_virtual_clock = True
    win.sim_check.setChecked(True)
    win.connect_device()
    yield win
    win.disconnect_device()
    win.close()


def test_connect_identifies_firmware_and_disables_flash_in_simulation(window):
    assert window.firmware.kind == "captest"
    assert "capacitance-test firmware" in window.firmware_label.text()
    assert not window.flash_test_button.isEnabled()
    assert not window.restore_button.isEnabled()
    assert window.run_auto_button.isEnabled() and window.measure_selected_button.isEnabled()
    assert window.flash("test") is False       # refused in simulation mode


def test_automatic_mode_one_adc(window):
    window.auto_adc_checks[2].setChecked(False)
    window.auto_adc_checks[3].setChecked(False)
    window.auto_adc_checks[4].setChecked(False)
    assert window.run_auto()
    assert not window.run_auto()               # a job is already running
    assert window.wait_idle()
    assert window.table.rowCount() == 10
    assert window.progress_bar.value() == 10
    assert window.sim_device.parked
    col = [c[1] for c in gui.COLUMNS].index("c_share_ratio")       # the default method gives C_share
    ratios = [window.table.item(r, col).text() for r in range(10)]
    assert all(0.10 < float(x) < 0.25 for x in ratios)
    assert len(window.session.results) == 10
    assert window.data_curve.getData()[0] is not None


def test_manual_mode_selection_and_too_soon_warning(window):
    window.spec_edit.setText("2:0,2:5,4:1-3")
    assert window.apply_spec()
    assert window.selected_channels() == [(2, 0), (2, 5), (4, 1), (4, 2), (4, 3)]
    window.invert_selection([4])
    assert (4, 0) in window.selected_channels() and (4, 1) not in window.selected_channels()
    window.select_all(False)
    window.apply_spec()
    assert window.measure_selected()
    assert window.wait_idle()
    assert window.table.rowCount() == 5
    assert window.channel_boxes[(2, 0)].text() == "0"      # no longer fresh
    assert window.channel_boxes[(2, 1)].text() == "1*"     # still fresh since power-up
    assert not window.warnings

    window.s_discharge.setValue(1000)
    window.session.clock.advance(0.01)
    assert window.measure_one((2, 5))
    assert window.wait_idle()
    assert len(window.warnings) == 1 and "2:5" in window.warnings[0]
    assert "2:5" in window.warning_label.text()
    row = window.table_rows[(2, 5)]
    assert capfit.TOO_SOON in window.table.item(row, len(gui.COLUMNS) - 1).text()
    assert "less than the discharge time" in window.channel_boxes[(2, 5)].toolTip()


def test_boot_id_reset_in_gui(window):
    window.spec_edit.setText("1:0")
    window.apply_spec()
    window.measure_selected()
    assert window.wait_idle()
    assert window.channel_boxes[(1, 0)].text() == "0"
    window.sim_device.power_cycle()
    window.session.refresh_boot()
    window.update_channel_status()
    assert window.channel_boxes[(1, 0)].text() == "0*"


def test_stop_leaves_board_parked(window):
    assert window.run_auto()
    window.stop_job()
    assert window.wait_idle()
    assert window.sim_device.parked
    assert window.sim_device.commands[-1] == "capabort"
    assert window.table.rowCount() < 50


def test_settings_round_trip_and_discharge_check(window, tmp_path):
    window.s_gap.setValue(300)
    window.s_vs0.setValue(0.05)
    window.bias_table.item(0, 1).setText("999000")
    s = window.collect_settings()
    assert s.gap_ns == 300 and s.v_s0 == pytest.approx(0.05) and s.bias["1:0"] == 999000
    s.save(tmp_path / "s.json")
    window.apply_settings(type(s).load(tmp_path / "s.json"))
    assert window.s_gap.value() == 300
    window.discharge_edit.setText("2:0")
    assert window.run_discharge_check()
    assert window.wait_idle()
    assert any("Discharge check" in window.log_view.toPlainText().splitlines()[i]
               for i in range(window.log_view.document().blockCount()))


def test_export_and_plots(window, tmp_path):
    window.spec_edit.setText("2:0,2:1")
    window.apply_spec()
    window.measure_selected()
    assert window.wait_idle()
    window.session.save_csv(tmp_path / "r.csv")
    window.session.save_raw(tmp_path / "raw.json")
    assert (tmp_path / "r.csv").exists()
    paths = window.export_plot_images(str(tmp_path))
    assert all(os.path.getsize(p) > 0 for p in paths)
    assert any(p.endswith("ln_d.png") for p in paths)
    results = gui.runner.reanalyze(tmp_path / "raw.json")
    assert len(results) == 2
    # The ln(d) tab shows the Vmid-between-samples burst, and is emptied for another method.
    x, y = window.ln_data.getData()
    assert x is not None and len(x) > 8 and np.isfinite(y).sum() > 8
    assert window.ln_model.getData()[0] is not None and window.ln_slope.getData()[0] is not None
    window.show_measurement(capfit.Measurement(2, 0, 1e6))
    assert window.ln_data.getData()[0] is None or len(window.ln_data.getData()[0]) == 0
    assert window.ln_plot.titleLabel.text == gui.LN_TITLE_IDLE


def test_production_firmware_is_reported_not_failed(app):
    win = gui.MainWindow()
    win.sim_virtual_clock = True
    win.sim_check.setChecked(True)
    original = gui.SimConfig
    gui.SimConfig = lambda **kw: original(kind="production", **kw)
    try:
        win.connect_device()
    finally:
        gui.SimConfig = original
    assert win.firmware.kind == "production"
    assert "production firmware is running" in win.firmware_label.text()
    assert not win.run_auto_button.isEnabled()
    assert win.run_auto() is False
    win.disconnect_device()


def test_flash_buttons_confirm_and_stream_dry_run(app, monkeypatch):
    win = gui.MainWindow()
    win.port_combo.setEditText("COM9")
    asked = []
    win._confirm = lambda text: asked.append(text) or True
    monkeypatch.setattr(flasher, "wait_for_teensy", lambda *a, **k: None)
    assert win.flash("production", dry_run=True)
    deadline = 60
    import time
    start = time.monotonic()
    while win.script_worker is not None and time.monotonic() - start < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert "production" in asked[0] and "COM9" in asked[0]
    assert "dry run" in win.log_view.toPlainText()
    win.close()


def test_reason_banner_explains_disabled_buttons(app):
    win = gui.MainWindow()
    assert "Not connected" in win.reason_label.text()
    assert "Not connected" in win.run_auto_button.toolTip() or not win.run_auto_button.isEnabled()
    win.sim_virtual_clock = True
    win.sim_check.setChecked(True)
    original = gui.SimConfig
    gui.SimConfig = lambda **kw: original(kind="production", **kw)
    try:
        win.connect_device()
    finally:
        gui.SimConfig = original
    assert "PRODUCTION firmware" in win.reason_label.text()
    assert "Flash test firmware" in win.run_auto_button.toolTip()
    win.disconnect_device()
    win.connect_device()
    assert win.reason_label.text().startswith("Ready")
    assert win.run_auto_button.isEnabled() and win.run_auto_button.toolTip() == ""
    win.disconnect_device()


def test_connecting_to_production_hardware_offers_the_flash(app, monkeypatch):
    win = gui.MainWindow()
    asked = []
    win._confirm = lambda text: asked.append(text) or False      # user declines
    win.port_combo.setEditText("COM9")

    class FakeInfo:
        kind = "production"
        label = "production firmware is running"
        detail = ""
        status = None

    monkeypatch.setattr(gui, "SerialLink", lambda port: gui.SimLink(
        gui.SimulatedDevice(gui.SimConfig(kind="production"), clock=gui.VirtualClock())))
    win.connect_device()
    for _ in range(20):
        app.processEvents()
    assert asked and "capacitance-test firmware" in asked[0] and "COM9" in asked[0]
    win.disconnect_device()


def test_fit_model_is_selectable_and_shown(window):
    window.s_method.setCurrentIndex(window.s_method.findData("burst"))   # the fit model applies to it
    assert window.collect_settings().fit_model == capfit.FIT_TWO          # default
    window.s_fit.setCurrentIndex(window.s_fit.findData(capfit.FIT_ONE))
    assert window.collect_settings().fit_model == capfit.FIT_ONE
    window.spec_edit.setText("2:0,2:1")
    window.apply_spec()
    assert window.measure_selected()
    assert window.wait_idle()
    fit_col = [c[1] for c in gui.COLUMNS].index("fit_model")
    assert window.table.item(window.table_rows[(2, 0)], fit_col).text() == capfit.FIT_ONE
    s = window.settings
    s.fit_model = capfit.FIT_TWO
    window.apply_settings(s)
    assert window.s_fit.currentData() == capfit.FIT_TWO
    for name in ("tau_fast_us", "fast_pct", "c_total_shift_pct", "vs0_est_v"):
        assert name in [c[1] for c in gui.COLUMNS]


def test_switching_the_fit_model_in_the_results_view_reanalyses_stored_samples(window):
    window.s_method.setCurrentIndex(window.s_method.findData("burst"))   # the fit model applies to it
    window.sim_device.config.fast_fraction = 0.4          # sensors with a fast and a slow component
    window.spec_edit.setText("3:0-5")
    window.apply_spec()
    assert window.measure_selected()
    assert window.wait_idle()
    cols = [c[1] for c in gui.COLUMNS]
    fit_col, c_col = cols.index("fit_model"), cols.index("c_total_pf")
    one_col, two_col = cols.index("c_total_1exp_pf"), cols.index("c_total_2exp_pf")
    served = window.sim_device.bursts_served
    commands = len(window.sim_device.commands)

    def cell(channel, col):
        return window.table.item(window.table_rows[(3, channel)], col).text()

    two_value = float(cell(0, c_col))
    assert cell(0, fit_col) == capfit.FIT_TWO and float(cell(0, two_col)) == pytest.approx(two_value, abs=0.006)
    assert "two exponentials used on 6 of 6" in window.compare_label.text()

    window.fit_combo.setCurrentIndex(window.fit_combo.findData(capfit.FIT_ONE))     # no run needed
    assert cell(0, fit_col) == capfit.FIT_ONE
    one_value = float(cell(0, c_col))
    assert one_value < 0.9 * two_value
    assert float(cell(0, one_col)) == pytest.approx(one_value, abs=0.006)            # both models stay visible
    assert float(cell(0, two_col)) == pytest.approx(two_value, abs=0.006)
    assert window.s_fit.currentData() == capfit.FIT_ONE                              # settings tab follows
    assert window.sim_device.bursts_served == served and len(window.sim_device.commands) == commands
    assert "one exponential" in window.compare_label.text()
    assert window.session.latest.fit_model_used == capfit.FIT_ONE                     # plot shows the new fit

    window.s_fit.setCurrentIndex(window.s_fit.findData(capfit.FIT_TWO))             # from the Settings tab
    assert window.fit_combo.currentData() == capfit.FIT_TWO
    assert float(cell(0, c_col)) == pytest.approx(two_value, abs=0.006)
    assert window.sim_device.bursts_served == served

    # Other settings can be applied to the recorded samples as well.
    window.bias_table.item(window.bias_table.findItems("3:0", QtCore.Qt.MatchFlag.MatchExactly)[0].row(),
                           1).setText("940000")
    assert window.reanalyze_results()
    assert float(cell(0, c_col)) == pytest.approx(two_value / 2, abs=0.01)
    assert window.sim_device.bursts_served == served


def test_fit_controls_are_locked_while_measuring(window):
    window.spec_edit.setText("3:0-5")
    window.apply_spec()
    assert window.measure_selected()
    assert not window.fit_combo.isEnabled() and not window.reanalyze_button.isEnabled()
    assert window.wait_idle()
    assert window.fit_combo.isEnabled() and window.reanalyze_button.isEnabled()


def test_stop_button_is_enabled_while_a_run_is_active(window):
    assert window.run_auto()
    assert window.stop_button.isEnabled() and not window.run_auto_button.isEnabled()
    window.stop_job()
    assert window.wait_idle()
    assert not window.stop_button.isEnabled() and window.run_auto_button.isEnabled()


def test_bar_chart_cuts_off_outliers():
    heights, cut, cap = gui.clip_outliers(np.array([70.0, 73.0, 75.0, 250.0, 9000.0, np.nan]))
    assert cap == pytest.approx(10 * 75.0)          # median of the positive values
    assert list(cut) == [False, False, False, False, True, False]
    assert heights[4] == pytest.approx(1.2 * 250.0) and heights[3] == 250.0 and heights[5] == 0.0   # drawn just above the rest
    assert not gui.clip_outliers(np.array([1.0]))[1].any()


def test_vinf_mode_fills_its_own_tab_and_not_the_results_table(window, tmp_path):
    rows_before = window.table.rowCount()
    for channel in ((2, 0), (2, 1)):
        window.channel_boxes[channel].setChecked(True)
    assert window.vinf_button.isEnabled()
    assert window.measure_vinf_selected() and window.wait_idle()
    assert window.table.rowCount() == rows_before                  # the capacitance table is untouched
    assert window.vinf_table.rowCount() == 2
    labels = [window.vinf_table.item(r, 0).text() for r in range(2)]
    assert labels == ["ADC2:0", "ADC2:1"]
    for column in (2, 4, 6):                                       # alternating, held and calculated Vinf
        assert 1.2 < float(window.vinf_table.item(0, column).text()) < 1.3
    for curve in (window.vinf_alt_curve, window.vinf_held_curve, window.vinf_calc_curve):
        x, y = curve.getData()
        assert len(x) == 2 and np.isfinite(y).all()
    assert window.bottom_tabs.currentWidget() is window.vinf_page
    assert "alternating" in window.log_view.toPlainText()
    readings = [window.vinf_shown[c] for c in sorted(window.vinf_shown)]
    gui.runner.write_vinf_csv(readings, window.driver, tmp_path / "v.csv")
    assert (tmp_path / "v.csv").exists()


def test_vinf_button_is_off_for_a_board_without_the_mode(app):
    win = gui.MainWindow(board_id="TestBoard_ADC124")
    try:
        assert not win.vinf_button.isEnabled() and not win.driver.supports_vinf
        assert not win.measure_vinf_selected()
    finally:
        win.close()
