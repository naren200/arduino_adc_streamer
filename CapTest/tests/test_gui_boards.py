"""Headless Qt tests (QT_QPA_PLATFORM=offscreen) parametrised over every board: the window is
generated from the board's descriptor, switching boards rebuilds it and keeps separate
saved settings, and both run modes work through the GUI on the simulated board."""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from captest import capfit, gui, runner, settings as settings_module
from captest.boards import DEFAULT_BOARD_ID, get_registry, load_driver
from captest.settings import Settings

BOARDS = [e.id for e in get_registry().enabled]


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPTEST_SETTINGS_DIR", str(tmp_path / "settings"))
    monkeypatch.setenv("CAPTEST_RESULTS_DIR", str(tmp_path / "results"))


def points(curve):
    """Number of points of a pyqtgraph curve (getData() returns None for an empty one)."""
    x, _ = curve.getData()
    return 0 if x is None else len(x)


def connected(board, **kw):
    win = gui.MainWindow(board_id=board, **kw)
    win.sim_virtual_clock = True
    win.sim_check.setChecked(True)
    win.connect_device()
    return win


@pytest.fixture(params=BOARDS)
def window(request, app):
    win = connected(request.param)
    yield win
    win.disconnect_device()
    win.close()


def test_window_is_generated_from_the_descriptor(window):
    driver = window.driver
    assert window.entry.name in window.windowTitle()
    assert window.firmware.kind == "captest" and window.firmware.board_id == driver.id
    assert not window.flash_test_button.isEnabled() and not window.restore_button.isEnabled()
    assert window.run_auto_button.isEnabled() and window.measure_selected_button.isEnabled()
    assert set(window.channel_boxes) == set(driver.channels())
    assert set(window.auto_adc_checks) == {g.id for g in driver.groups()}
    headers = [window.table.horizontalHeaderItem(i).text() for i in range(window.table.columnCount())]
    assert headers[:len(driver.id_columns())] == [c[0] for c in driver.id_columns()]
    assert window.bias_table.rowCount() == len(driver.channels())
    assert window.spec_edit.placeholderText() == driver.example_spec
    assert window.vmid_adc.count() == len(driver.descriptor["vmid_read"]["targets"])
    assert window.discharge_edit.text() == driver.discharge_check["default_channel"]
    assert window.s_vref.value() == pytest.approx(driver.vref)
    low, high, default = driver.spi_range
    assert (window.s_clock.minimum(), window.s_clock.maximum(), window.s_clock.value()) == (low, high, default)
    assert window.auto_per_address.isVisibleTo(window) == (driver.id == "TestBoard_ADC124")


def test_automatic_mode_first_group_through_the_gui(window, monkeypatch):
    driver = window.driver
    snapshots = []
    original = window._put_row

    def record_order(result):
        original(result)
        snapshots.append([window.table.item(r, 0).data(gui.QtCore.Qt.ItemDataRole.UserRole)
                          for r in range(window.table.rowCount())])

    monkeypatch.setattr(window, "_put_row", record_order)
    groups = driver.groups()
    for group in groups[1:]:
        window.auto_adc_checks[group.id].setChecked(False)
    assert window.run_auto()
    assert not window.run_auto()                          # a job is already running
    assert window.wait_idle()
    channels = groups[0].channels
    plan = driver.plan(channels, window.settings)
    assert window.table.rowCount() == len(channels) and len(window.session.results) == len(channels)
    assert window.progress_bar.value() == len(plan)
    assert window.sim_device.parked and window.sim_device.commands[-1] == "capabort"
    column = [c[1] for c in window.columns].index("ratio")
    ratios = [float(window.table.item(r, column).text()) for r in range(window.table.rowCount())]
    assert all(0.10 < x < 0.25 for x in ratios)
    assert window.data_curve.getData()[0] is not None
    assert (points(window.pre_curve) > 0) == (driver.id == "TestBoard_ADC124")
    assert snapshots and all(order == sorted(order) for order in snapshots)
    assert snapshots[-1] == sorted(channels)
    assert window.table_rows == {channel: i for i, channel in enumerate(sorted(channels))}


def test_out_of_order_results_keep_selection_updates_and_reanalysis(window, tmp_path, monkeypatch):
    channels = window.driver.channels()
    first, middle, last = channels[0], channels[len(channels) // 2], channels[-1]
    assert window.measure_one(last) and window.wait_idle()
    window.table.selectRow(0)
    for ref in (middle, first, middle):
        assert window.measure_one(ref) and window.wait_idle()
        expected = sorted(window.session.results)
        assert [window.table.item(i, 0).data(gui.QtCore.Qt.ItemDataRole.UserRole)
                for i in range(window.table.rowCount())] == expected
        assert window.table_rows == {channel: i for i, channel in enumerate(expected)}
        selected = window.table.selectionModel().selectedRows()[0].row()
        assert window.table.item(selected, 0).data(gui.QtCore.Qt.ItemDataRole.UserRole) == last
    assert window.table.rowCount() == 3
    shown = []
    monkeypatch.setattr(window, "show_measurement", lambda m: shown.append(m.ref))
    window.show_selected_result()
    assert shown[-1] == last
    commands = len(window.sim_device.commands)
    assert window.reanalyze_results()
    assert len(window.sim_device.commands) == commands
    assert window.table_rows == {ref: i for i, ref in enumerate(sorted((first, middle, last)))}
    assert shown[-1] == last
    path = tmp_path / "raw.json"
    window.session.save_raw(path)
    assert window.load_raw_file(str(path))
    assert window.table_rows == {ref: i for i, ref in enumerate(sorted((first, middle, last)))}


def test_help_tab_explains_current_columns_flags_and_settings(window):
    tabs = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert "Help" in tabs
    window.tabs.setCurrentIndex(tabs.index("Help"))
    text = window.help_browser.toPlainText()
    assert window.entry.name in text
    for title, key, _ in window.columns:
        assert title in text
        assert window.table.horizontalHeaderItem([c[1] for c in window.columns].index(key)).toolTip()
    for name in window.driver.settings_fields:
        assert gui.FIELD_SPECS[name].label in text
    for name, value in vars(capfit).items():
        if name.isupper() and isinstance(value, str) and value not in capfit.FIT_MODELS:
            assert value in text
    assert "C_total = tau / R_bias" in text
    assert "(V0 - V_s0) / (V_D - V_s0)" in text
    assert "software history, not proof" in text
    assert "does not prove V_s0 = 0" in text
    selector = window.tabs.currentWidget().findChild(QtWidgets.QComboBox)
    assert [selector.itemData(i) for i in range(selector.count())] == [
        "measurement", "columns", "flags", "settings"]


def test_help_rebuilds_for_selected_board(app):
    win = gui.MainWindow(board_id="TestBoard_ADC124")
    try:
        assert "Pre-connect frames" in win.help_browser.toPlainText()
        assert win.select_board("TestBoard_7953")
        text = win.help_browser.toPlainText()
        assert win.entry.name in text
        assert "Frame gap (CS high)" in text
        assert "Pre-connect frames" not in text.rsplit("\nSettings\n", 1)[-1]
        assert win.select_board("TestBoard_ADC124")
        assert "Pre-connect frames" in win.help_browser.toPlainText()
    finally:
        win.close()


def test_manual_mode_selection_and_status(window):
    driver = window.driver
    first, second, third = driver.channels()[:3]
    window.spec_edit.setText(driver.format_spec([first, third]))
    assert window.apply_spec()
    assert window.selected_channels() == [first, third]
    window.invert_selection([driver.group_of(first)])
    assert first not in window.selected_channels() and second in window.selected_channels()
    window.select_all(False)
    window.apply_spec()
    assert window.measure_selected()
    assert window.wait_idle()
    assert window.table.rowCount() == 2
    assert window.channel_boxes[first].text() == driver.cell_label(first)          # no longer fresh
    far = driver.channels()[-1]
    assert window.channel_boxes[far].text() == driver.cell_label(far) + "*"        # still fresh
    assert not window.warnings

    window.s_discharge.setValue(1000)
    window.session.clock.advance(0.01)
    assert window.measure_one(third)
    assert window.wait_idle()
    assert len(window.warnings) == 1 and driver.display(third) in window.warnings[0]
    assert driver.display(third) in window.warning_label.text()
    row = window.table_rows[third]
    assert capfit.TOO_SOON in window.table.item(row, window.table.columnCount() - 1).text()
    assert "less than the discharge time" in window.channel_boxes[third].toolTip()


def test_adc124_neighbours_show_the_side_effect_connection(app):
    win = connected("TestBoard_ADC124")
    win.spec_edit.setText("1:1:2")
    win.apply_spec()
    assert win.measure_selected() and win.wait_idle()
    # MUX2..4 at the same address were connected too: no longer fresh, with a tooltip saying when.
    assert win.channel_boxes[(1, 2, 2)].text() == "2" and win.channel_boxes[(1, 4, 2)].text() == "2"
    assert "last connected" in win.channel_boxes[(1, 3, 2)].toolTip()
    assert win.channel_boxes[(1, 1, 3)].text() == "3*"                   # other address: still fresh
    win.s_discharge.setValue(5000)
    win.spec_edit.setText("1:3:2")
    win.apply_spec()
    assert win.measure_selected() and win.wait_idle()
    assert any("1:3:2" in w for w in win.warnings)
    win.disconnect_device()


def test_per_address_toggle_changes_the_number_of_bursts(app):
    win = connected("TestBoard_ADC124")
    for group in win.driver.groups()[1:]:
        win.auto_adc_checks[group.id].setChecked(False)
    assert win.auto_per_address.isChecked() and win.s_per_address.isChecked()
    assert win.run_auto() and win.wait_idle()
    assert len([c for c in win.sim_device.commands if c.startswith("capburst")]) == 7
    win.auto_per_address.setChecked(False)                                # also updates the settings box
    assert not win.s_per_address.isChecked() and not win.collect_settings().per_address
    count = len(win.sim_device.commands)
    assert win.run_auto() and win.wait_idle()
    assert len([c for c in win.sim_device.commands[count:] if c.startswith("capburst")]) == 25
    win.disconnect_device()


def test_stop_leaves_board_parked(window):
    assert window.run_auto()
    assert window.stop_button.isEnabled() and not window.run_auto_button.isEnabled()
    window.stop_job()
    assert window.wait_idle()
    assert window.sim_device.parked and window.sim_device.commands[-1] == "capabort"
    assert window.table.rowCount() < len(window.driver.auto_channels())
    assert not window.stop_button.isEnabled() and window.run_auto_button.isEnabled()


def test_vmid_read_and_discharge_check(window):
    assert window.read_vmid() and window.wait_idle()
    assert "VMID" in window.log_view.toPlainText()
    assert window.run_discharge_check() and window.wait_idle()
    assert "Discharge check" in window.log_view.toPlainText()
    assert window.bar_total is not None


def test_settings_round_trip(window, tmp_path):
    window.s_vs0.setValue(0.05)
    window.s_discharge.setValue(450)
    first_key = window.driver.key(window.driver.channels()[0])
    window.bias_table.item(0, 1).setText("999000")
    s = window.collect_settings()
    assert s.v_s0 == pytest.approx(0.05) and s.discharge_ms == 450 and s.bias[first_key] == 999000
    assert s.board_id == window.driver.id
    s.save(tmp_path / "s.json")
    loaded = Settings.load(tmp_path / "s.json")
    assert loaded.board_id == window.driver.id
    window.apply_settings(loaded)
    assert window.s_discharge.value() == 450


def test_exports_plots_and_reanalysis(window, tmp_path):
    driver = window.driver
    window.spec_edit.setText(driver.format_spec(driver.channels()[:2]))
    window.apply_spec()
    assert window.measure_selected() and window.wait_idle()
    window.session.save_csv(tmp_path / "r.csv")
    window.session.save_raw(tmp_path / "raw.json")
    paths = window.export_plot_images(str(tmp_path))
    assert all(os.path.getsize(p) > 0 for p in paths)
    assert len(runner.reanalyze(tmp_path / "raw.json")) == 2
    window.fit_combo.setCurrentIndex(window.fit_combo.findData(capfit.FIT_ONE))     # re-analyses at once
    assert window.s_fit.currentData() == capfit.FIT_ONE
    # 7953 measures with Vmid between samples by default; its analysis has no fit-model choice.
    expected = capfit.FIT_ALT if driver.id == "TestBoard_7953" else capfit.FIT_ONE
    assert window.session.latest.fit_model_used == expected


def test_flash_buttons_use_the_selected_boards_scripts(window, monkeypatch):
    window.disconnect_device()
    window.sim_check.setChecked(False)
    window.port_combo.setEditText("COM9")
    asked = []
    window._confirm = lambda text, title="": asked.append(text) or True
    monkeypatch.setattr(gui.flasher, "wait_for_teensy", lambda *a, **k: None)
    assert window.flash("test", dry_run=True)
    deadline = time.monotonic() + 60
    while window.script_worker is not None and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.01)
    fw = window.entry.firmware
    assert fw["firmware_name"] in asked[0] and "COM9" in asked[0] and "production" in asked[0]
    log = window.log_view.toPlainText()
    assert "dry run" in log and fw["test_project"].split("/")[-1] in log
    assert "-e %s -t upload --upload-port COM9" % fw["test_env"] in log


def test_help_menu_and_about_name_the_documents(window):
    text = window.about_text()
    for path in window.entry.docs.values():
        assert str(path) in text
    assert all(action.isEnabled() for action in window.docs_actions.values())


# ------------------------------------------------------------ board switching
def test_switching_the_board_rebuilds_the_window_and_keeps_settings_per_board(app):
    win = gui.MainWindow(board_id="TestBoard_7953", use_saved=True)
    win.sim_virtual_clock = True
    win.sim_check.setChecked(True)
    win.connect_device()
    win.s_discharge.setValue(123)
    win.spec_edit.setText("1:0")
    win.apply_spec()
    assert win.measure_selected() and win.wait_idle() and win.table.rowCount() == 1
    assert win.select_board("TestBoard_ADC124")
    assert win.session is None and win.table.rowCount() == 0 and win.firmware is None
    assert "TestBoard_ADC124" in win.windowTitle() and win.board_combo.currentData() == "TestBoard_ADC124"
    assert win.s_vref.value() == pytest.approx(3.3) and win.s_discharge.value() == 300     # its own defaults
    assert len(win.channel_boxes) == 50 and (1, 4, 6) in win.channel_boxes
    assert win.table.horizontalHeaderItem(1).text() == "MUX"
    win.s_discharge.setValue(777)
    assert win.select_board("TestBoard_7953")
    assert win.s_discharge.value() == 123 and win.s_vref.value() == pytest.approx(2.5)   # restored
    files = {p.name for p in settings_module.settings_dir().iterdir()}
    assert {"TestBoard_7953.json", "TestBoard_ADC124.json", "last_board.json"} <= files
    assert Settings.load(settings_module.settings_dir() / "TestBoard_ADC124.json").discharge_ms == 777
    assert settings_module.last_board_id() == "TestBoard_7953"
    win.select_board("TestBoard_ADC124")
    win.close()
    # The next start comes up on the last used board with its own settings.
    again = gui.MainWindow(use_saved=True)
    assert again.driver.id == "TestBoard_ADC124" and again.s_discharge.value() == 777
    again.close()
    # Without use_saved the defaults are used whatever the files say.
    assert gui.MainWindow().driver.id == DEFAULT_BOARD_ID


def test_a_running_job_blocks_a_board_change(app):
    win = connected("TestBoard_7953")
    assert win.run_auto()
    assert not win.select_board("TestBoard_ADC124") and win.driver.id == "TestBoard_7953"
    win.stop_job()
    assert win.wait_idle()
    assert win.select_board("TestBoard_ADC124")
    win.close()


def attach(monkeypatch, board, kind="captest"):
    """Make 'COM9' answer like a real board of `board` running `kind` firmware."""
    driver = load_driver(board)
    monkeypatch.setattr(gui, "SerialLink", lambda port: gui.SimLink(
        driver.create_simulator(gui.VirtualClock(), None, driver.sim_config(kind=kind))))


def test_connecting_to_another_boards_firmware_offers_to_select_that_board(app, monkeypatch):
    attach(monkeypatch, "TestBoard_ADC124")
    win = gui.MainWindow(board_id="TestBoard_7953")
    asked = []
    win._confirm = lambda text, title="": asked.append(text) or True
    win.port_combo.setEditText("COM9")
    win.connect_device()
    assert asked and "TestBoard_ADC124" in asked[0]
    assert win.driver.id == "TestBoard_ADC124" and win.firmware.kind == "captest"
    assert win.reason_label.text().startswith("Ready") and win.run_auto_button.isEnabled()
    win.disconnect_device()
    win.close()


def test_declining_the_board_change_blocks_measuring_with_an_explanation(app, monkeypatch):
    attach(monkeypatch, "TestBoard_ADC124")
    win = gui.MainWindow(board_id="TestBoard_7953")
    win._confirm = lambda text, title="": False
    win.port_combo.setEditText("COM9")
    win.connect_device()
    assert win.driver.id == "TestBoard_7953" and win.firmware.board_id == "TestBoard_ADC124"
    assert "belongs to TestBoard_ADC124" in win.reason_label.text()
    assert not win.run_auto_button.isEnabled() and win.run_auto() is False
    win.disconnect_device()
    win.close()


@pytest.mark.parametrize("board", BOARDS)
def test_production_firmware_is_reported_for_every_board(app, monkeypatch, board):
    attach(monkeypatch, board, "production")
    win = gui.MainWindow(board_id=board)
    asked = []
    win._confirm = lambda text, title="": asked.append(text) or False
    win.port_combo.setEditText("COM9")
    win.connect_device()
    for _ in range(20):
        app.processEvents()
    assert win.firmware.kind == "production" and "production firmware is running" in win.firmware_label.text()
    assert "PRODUCTION firmware" in win.reason_label.text() and not win.run_auto_button.isEnabled()
    assert asked and win.entry.firmware["firmware_name"] in asked[0]            # offers the test firmware
    win.disconnect_device()
    win.close()


def test_loading_a_raw_file_selects_the_matching_board(app, tmp_path):
    source = connected("TestBoard_ADC124")
    source.spec_edit.setText("1:1:0,1:2:0")
    source.apply_spec()
    assert source.measure_selected() and source.wait_idle()
    raw = tmp_path / "adc124_raw.json"
    source.session.save_raw(raw)
    source.disconnect_device()
    win = gui.MainWindow(board_id="TestBoard_7953")
    asked = []
    win._confirm = lambda text, title="": asked.append(text) or False
    assert not win.load_raw_file(str(raw))                       # declined: refused, 7953 untouched
    assert win.driver.id == "TestBoard_7953" and "TestBoard_ADC124" in win.log_view.toPlainText()
    win._confirm = lambda text, title="": asked.append(text) or True
    assert win.load_raw_file(str(raw))
    assert win.driver.id == "TestBoard_ADC124" and win.table.rowCount() == 2
    assert win.session.results[(1, 1, 0)].aggregate.mean["c_total"] > 0
    assert points(win.pre_curve) > 0
    win.close()
    source.close()


def test_loading_settings_of_another_board_switches_the_board(app, tmp_path):
    Settings.for_board("TestBoard_ADC124").save(tmp_path / "adc124.json")
    win = gui.MainWindow(board_id="TestBoard_7953")
    win._confirm = lambda text, title="": True
    win.select_board(Settings.load(tmp_path / "adc124.json").board_id, ask=True)
    assert win.driver.id == "TestBoard_ADC124"
    win.close()
