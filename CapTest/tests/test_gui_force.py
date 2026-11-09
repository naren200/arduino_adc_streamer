"""Headless test of the Force Calibration tab and the Export Force Calculation Data action."""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from captest import force_calibration as fc, force_export as fx, gui


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("CAPTEST_RESULTS_DIR", str(tmp_path / "results"))
    win = gui.MainWindow()
    win.sim_virtual_clock = True
    win.sim_check.setChecked(True)
    win.connect_device()
    yield win
    win.disconnect_device()
    win.close()


def pump(app, condition, timeout=60.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def measure_group(window, app, array=1, pzt="PZT1"):
    panel = window.force_panel
    panel.array_combo.setCurrentIndex(panel.array_combo.findData(array))
    panel.pzt_combo.setCurrentIndex(panel.pzt_combo.findText(pzt))
    assert panel.measure_package()
    assert window.wait_idle()
    app.processEvents()


def test_tab_exists_for_the_7953_with_all_ten_packages(window):
    panel = window.force_panel
    assert panel is not None
    assert window.tabs.indexOf(panel) >= 0 and window.tabs.tabText(window.tabs.indexOf(panel)) == "Force Calibration"
    assert [panel.array_combo.itemText(i) for i in range(panel.array_combo.count())] == ["Array 1", "Array 2"]
    assert sorted(panel.pzt_combo.itemText(i) for i in range(panel.pzt_combo.count())) == [
        "PZT1", "PZT3", "PZT5", "PZT6", "PZT7"]
    panel.pzt_combo.setCurrentIndex(panel.pzt_combo.findText("PZT1"))
    assert "A1_PZT1" in panel.routes_label.text() and "ADC2 input 4" in panel.routes_label.text()
    assert not panel.start_button.isEnabled()                       # no electrical record yet
    assert "Select a package" in panel.prompt_label.text()


def test_other_boards_do_not_get_the_tab(app):
    win = gui.MainWindow(board_id="TestBoard_ADC124")
    try:
        assert win.force_panel is None
        texts = [win.tabs.tabText(i) for i in range(win.tabs.count())]
        assert "Force Calibration" not in texts
    finally:
        win.close()


def test_measure_package_then_export_partial_file_and_update(window, app, tmp_path):
    panel = window.force_panel
    panel.instance_edit.setText("board-9")
    measure_group(window, app, 1, "PZT1")
    assert len(window.session.results) == 5
    path = tmp_path / "force_inputs.json"
    assert panel.add_results_to_file(str(path))
    assert "1 of 10 packages complete" in panel.coverage_label.text()
    forces = fx.ForceInputs.load(path, panel.group_map)
    assert forces.ready_groups() == ["A1_PZT1"] and forces.data["provenance"]["origin"] == "simulated"
    assert forces.data["provenance"]["board_instance"] == "board-9"
    assert forces.data["provenance"]["firmware"]["version"] == "1.4.0"
    # later: measure another package and update the same file
    measure_group(window, app, 1, "PZT6")
    assert panel.add_results_to_file(str(path))
    again = fx.ForceInputs.load(path, panel.group_map)
    assert sorted(again.ready_groups()) == ["A1_PZT1", "A1_PZT6"]
    assert [r["revision"] for r in again.revisions("A1_PZT1")] == [1, 2]      # re-added: new revision
    # a different board instance cannot be merged into this file
    panel.instance_edit.setText("other-board")
    assert not panel.add_results_to_file(str(path))
    assert any("board instance" in line for line in window.log_view.toPlainText().splitlines())
    assert fx.ForceInputs.load(path, panel.group_map).data["provenance"]["board_instance"] == "board-9"


def test_export_button_is_in_the_data_group_and_uses_the_dialog(window, app, tmp_path, monkeypatch):
    texts = [b.text() for b in window.findChildren(QtWidgets.QPushButton)]
    assert "Export Force Calculation Data" in texts
    measure_group(window, app, 2, "PZT3")
    target = tmp_path / "from_dialog.json"
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))
    assert window.export_force_data()
    assert fx.ForceInputs.load(target).status("A2_PZT3") == fx.COMPLETE
    assert window.tabs.currentWidget() is window.force_panel


def test_load_refuses_a_damaged_file(window, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"format": "captest-force-inputs-1"}', encoding="utf-8")
    assert not window.force_panel.load_inputs(bad)
    assert "refused" in window.log_view.toPlainText()


def test_trial_start_prompts_marks_and_cancel_parks_the_board(window, app, tmp_path):
    panel = window.force_panel
    measure_group(window, app, 1, "PZT1")
    assert panel.add_results_to_file(str(tmp_path / "f.json"))
    panel.lag_spin.setValue(1500)
    panel.repeats_spin.setValue(1)
    assert panel.start_button.isEnabled()
    assert panel.start_trial()
    assert not panel.start_trial()                                    # one trial at a time
    assert not window.start_job(lambda session: session.vmid(1))
    ctl = panel.controller
    assert pump(app, lambda: ctl.wizard.phase == fc.PLACE_ARMED), ctl.wizard.phase
    assert pump(app, lambda: "Place 20 g now" in panel.prompt_label.text(), 5.0)   # the signal is queued
    assert ctl.wizard.armed   # armed before the prompt
    assert panel.placed_button.isEnabled() and not panel.removed_button.isEnabled()
    assert not panel.clear_button.isEnabled()
    panel.placed_button.click()
    assert panel.clear_button.isEnabled() and not panel.placed_button.isEnabled()
    ctl.refresh_view(force=True)                                      # the worker computes snapshots
    panel.refresh_plots()                                             # the plot only reads them
    assert panel.headroom_label.text().startswith("ADC headroom OK")
    assert len(panel.steps_table.item(0, 0).text()) > 0
    panel.cancel_button.click()
    assert ctl.session is None and window.sim_device.parked and not window.sim_device.scan.streaming
    assert ctl.wizard.phase == fc.PREPARE and ctl.wizard.current.mass_g == 20
    assert panel.start_button.isEnabled() and not panel.cancel_button.isEnabled()


def test_disconnect_while_capturing_stops_and_parks(window, app, tmp_path):
    panel = window.force_panel
    measure_group(window, app, 1, "PZT1")
    panel.add_results_to_file(str(tmp_path / "f.json"))
    assert panel.start_trial()
    device = window.sim_device
    assert pump(app, lambda: device.scan.streaming and panel.controller.now > 0.2)
    window.disconnect_device()
    assert not device.scan.streaming and device.parked
    assert panel.controller.session is None


def test_missing_capture_feature_is_reported_and_bursts_still_work(app, tmp_path, monkeypatch):
    monkeypatch.setenv("CAPTEST_RESULTS_DIR", str(tmp_path / "results"))
    win = gui.MainWindow()
    win.sim_virtual_clock = True
    win.sim_check.setChecked(True)
    win.connect_device()
    try:
        win.sim_device.config.firmware_version = "1.3.0"             # an older firmware
        panel = win.force_panel
        measure_group(win, app, 1, "PZT1")
        assert panel.add_results_to_file(str(tmp_path / "f.json"))
        assert not panel.start_trial()
        assert "no continuous capture" in panel.prompt_label.text()
        assert len(win.session.results) == 5                          # burst workflow unaffected
    finally:
        win.disconnect_device()
        win.close()


def test_saving_a_calibration_embeds_the_sensitivity_without_changing_the_electrical_link(window, app, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    panel = window.force_panel
    measure_group(window, app, 1, "PZT1")
    path = tmp_path / "f.json"
    assert panel.add_results_to_file(str(path))
    link = panel.forces.electrical_hash()
    stub = SimpleNamespace(fit=SimpleNamespace(d33_eff=7.5e-9), simulated=True,
                           forces=panel.forces,
                           build_record=lambda: {"status": "validated"},
                           group=panel.group_map.get("A1_PZT1"), signature=lambda: "sig")
    panel._embed_in_bundle(stub, Path("record.json"))
    again = fx.ForceInputs.load(path, panel.group_map)
    entry = again.data["force_inputs"]["d33_eff_c_per_n"]["A1_PZT1"]
    assert entry["value"] == 7.5e-9 and entry["provenance"]["record"] == "record.json"
    assert again.electrical_hash() == link


def test_provisional_gain_is_not_embedded(window, app, tmp_path):
    from types import SimpleNamespace
    panel = window.force_panel
    measure_group(window, app)
    path = tmp_path / "inputs.json"
    assert panel.add_results_to_file(str(path))
    before = path.read_bytes()
    ctl = SimpleNamespace(fit=SimpleNamespace(d33_eff=1e-9),
                          build_record=lambda: {"status": "fitted_provisional"})
    panel._embed_in_bundle(ctl, path)
    assert path.read_bytes() == before


def test_analysis_worker_keeps_qt_responsive_and_locks_competing_actions(window, app):
    import threading
    from PyQt6 import QtCore
    panel = window.force_panel
    release = threading.Event()
    ticks, completed = [], []
    timer = QtCore.QTimer(panel)
    timer.timeout.connect(lambda: ticks.append(True))
    timer.start(5)
    try:
        assert panel._run_analysis(lambda: release.wait(5), lambda value: completed.append(value))
        assert pump(app, lambda: len(ticks) >= 3, 2)
        assert not panel.array_combo.isEnabled() and not window.board_combo.isEnabled()
        assert not panel.start_trial()
        assert not window.select_board("TestBoard_ADC124")
    finally:
        release.set()
        assert pump(app, lambda: not panel.is_busy(), 5)
        assert pump(app, lambda: bool(completed), 5)
        timer.stop()
    assert panel.array_combo.isEnabled()


def test_group_switch_restores_cached_session_and_new_electrical_revision_clears_it(window, app, tmp_path):
    panel = window.force_panel
    measure_group(window, app)
    path = tmp_path / "inputs.json"
    assert panel.add_results_to_file(str(path))
    assert panel.prepare()
    original = panel.controller
    panel.pzt_combo.setCurrentIndex(panel.pzt_combo.findText("PZT3"))
    assert panel.controller is None
    panel.pzt_combo.setCurrentIndex(panel.pzt_combo.findText("PZT1"))
    assert panel.controller is original
    assert panel.add_results_to_file(str(path))
    assert panel.controller is None and not panel._controllers


def test_offline_trial_review_reproduces_decision_without_replacing_active_context(window, app, tmp_path):
    from test_force_session import make_rig, make_controller, drive_trial
    panel = window.force_panel
    rig = make_rig(panel.group_map)
    ctl = make_controller(panel.group_map, rig, tmp_path)
    drive_trial(ctl, rig[1])
    assert ctl.trials[-1].accepted
    active = panel.controller
    assert panel.replay_trial_evidence(ctl.trials[-1].sidecar)
    assert pump(app, lambda: "Offline replay: accepted" in panel.result_label.text(), 15)
    assert panel.controller is active
    assert pump(app, lambda: not panel.is_busy(), 5)
    force_t = panel.force_curve.xData
    assert len(panel.resid_curves) == 5
    for curve in panel.resid_curves:
        assert curve.xData[0] <= force_t[0] + .01
        assert curve.xData[-1] >= force_t[-1] - .02
        assert curve.xData[-1] - curve.xData[0] > 10.  # full trial, not a 5 s tail


def test_force_zero_reference_and_residuals_share_the_live_time_range(window, app):
    import numpy as np
    from types import SimpleNamespace
    from PyQt6 import QtCore
    panel = window.force_panel
    window.show()
    window.tabs.setCurrentWidget(panel)
    panel._timer.stop()
    app.processEvents()
    t = np.linspace(0., 60., 6001)
    residuals = {}
    for i in range(5):
        e = np.full(len(t), .001 * i)
        e[3501] = .1  # brief spike that uniform point skipping could lose
        residuals[(4, 5 + i)] = (t, e)
    view = SimpleNamespace(t=t, force_n=1 + t / 60, residuals=residuals,
        phase=fc.HOLD, sampling={}, headroom={}, problems=[])
    previous = panel.controller
    try:
        panel.controller = SimpleNamespace(session=object(), latest_view=lambda: view, _linear_pretest=False)
        panel._last_measure_time = time.monotonic()
        panel.refresh_plots()
        app.processEvents()
        assert panel.force_curve.xData[[0, -1]] == pytest.approx([30., 60.])
        for curve in panel.resid_curves:
            assert curve.xData[[0, -1]] == pytest.approx([30., 60.])
            assert np.nanmax(curve.getData()[1]) == pytest.approx(100.)
        line = panel.force_zero_line
        assert line.value() == 0 and line.pen.color().name() == "#ff0000"
        assert line.pen.style() == QtCore.Qt.PenStyle.DashLine
        assert panel.force_plot.viewRange()[1][0] < 0  # zero stays visible for a positive force trace
        panel.force_plot.setXRange(34., 39., padding=0)
        app.processEvents()
        assert panel.resid_plot.viewRange()[0] == pytest.approx([34., 39.])
        panel.resid_plot.setXRange(42., 46., padding=0)
        app.processEvents()
        assert panel.force_plot.viewRange()[0] == pytest.approx([42., 46.])
    finally:
        panel.controller = previous


def test_completed_trial_without_stable_events_still_shows_force_and_full_residuals(window, tmp_path):
    from test_force_session import make_rig, make_controller, drive_trial
    panel = window.force_panel
    rig = make_rig(panel.group_map)
    ctl = make_controller(panel.group_map, rig, tmp_path)
    drive_trial(ctl, rig[1])
    ctl.trials[-1].events = []
    ctl.trials[-1].accepted = False
    ctl.trials[-1].reasons = ["NO_STABLE_POST_PLACEMENT"]
    previous = panel.controller
    try:
        panel.controller = ctl
        panel._show_last_trial()
        assert len(panel.force_curve.xData) > 100
        for curve in panel.resid_curves:
            assert curve.xData[0] < 1. and curve.xData[-1] > 17.
    finally:
        panel.controller = previous


def test_default_lag_and_rate_estimate_warn_about_a_link_that_is_too_fast(window):
    panel = window.force_panel
    assert panel.lag_spin.value() == 1500                              # not 0: lag 0 overflowed the board
    assert "sampling rate per channel" in panel.rate_label.text() and "Too fast" not in panel.rate_label.text()
    panel.lag_spin.setValue(0)
    assert "Too fast" in panel.rate_label.text()


def test_custom_two_weight_plan_and_force_column_use_actual_calibration_gravity(window, app, tmp_path):
    panel = window.force_panel
    assert fc.parse_masses(panel.masses_edit.text()) == fc.MASSES_G
    measure_group(window, app)
    assert panel.add_results_to_file(str(tmp_path / "inputs.json"))
    panel.masses_edit.setText("25, 12.5")
    panel.repeats_spin.setValue(2)
    assert panel.prepare()
    ctl = panel.controller
    assert ctl.wizard.masses == (12.5, 25.) and len(ctl.wizard.steps) == 4
    ctl.g = 9.81
    panel.refresh_state()
    assert panel.steps_table.columnCount() == 5
    assert panel.steps_table.horizontalHeaderItem(1).text() == "Force [N]"
    assert float(panel.steps_table.item(0, 1).text()) == pytest.approx(.0125 * ctl.g, abs=5e-7)
    assert float(panel.steps_table.item(2, 1).text()) == pytest.approx(.025 * ctl.g, abs=5e-7)
    assert panel.steps_table.item(0, 2).text() == "1"
    panel.masses_edit.setText("20, 50")
    assert panel.controller is None and panel.steps_table.rowCount() == 0
    panel.masses_edit.setText("25, 12.5")
    assert panel.controller is ctl  # each mass plan keeps its own state


def test_invalid_mass_input_disables_start_and_explains_why(window, app, tmp_path):
    panel = window.force_panel
    measure_group(window, app)
    assert panel.add_results_to_file(str(tmp_path / "inputs.json"))
    panel.masses_edit.setText("20, -10")
    assert not panel.start_button.isEnabled()
    assert "greater than zero" in panel.masses_error_label.text()
    assert not panel.prepare() and panel.controller is None
    assert not panel.start_trial()
    assert "greater than zero" in panel.prompt_label.text()


def test_optional_notes_do_not_reset_calibration_and_are_logged_with_the_trial(window, app, tmp_path):
    from captest.force_evidence import read_json
    panel = window.force_panel
    measure_group(window, app)
    assert panel.add_results_to_file(str(tmp_path / "inputs.json"))
    assert panel.prepare()
    ctl = panel.controller
    identity = ctl.key.digest()
    panel.mount_edit.setText("plate on fixture")
    panel.contact_edit.setText("centre")
    assert panel.controller is ctl and ctl.key.digest() == identity
    assert panel.start_trial()
    assert pump(app, lambda: ctl.now >= .5)
    panel.cancel_trial()
    evidence = read_json(ctl._trial_sidecars[-1])
    assert evidence["mounting"] == "plate on fixture" and evidence["contact_position"] == "centre"
    assert "plate on fixture" in window.log_view.toPlainText()
    panel.contact_edit.setText("left edge")
    assert panel.controller is ctl
    assert read_json(ctl._trial_sidecars[-1])["contact_position"] == "centre"


def test_calibration_limit_tooltips_describe_accuracy_drift_and_optional_voltage_cutoff(window):
    panel = window.force_panel
    assert "not the largest force" in panel.error_limit_spin.toolTip()
    assert "15%" in panel.drift_limit_spin.toolTip() and "does not correct" in panel.drift_limit_spin.toolTip()
    assert "Normally leave this Off" in panel.guard_spin.toolTip()


def test_measure_unloaded_drift_then_start_weight_trial_with_a_frozen_correction(window, app, tmp_path):
    from captest.force_evidence import read_json
    panel = window.force_panel
    measure_group(window, app)
    assert panel.add_results_to_file(str(tmp_path / "inputs.json"))
    panel.zero_duration_spin.setValue(6)
    assert panel.zero_button.isEnabled() and panel.measure_unloaded_drift()
    ctl = panel.controller
    assert not panel.placed_button.isEnabled() and not panel.masses_edit.isEnabled()
    assert pump(app, lambda: not panel.worker.isRunning(), 30)
    assert pump(app, lambda: "Frozen correction applied" in panel.result_label.text(), 5), panel.result_label.text()
    assert ctl.trials == [] and ctl.wizard.current.mass_g == 20
    correction = ctl.unloaded_correction.copy()
    evidence = read_json(correction["sidecar"])
    assert evidence["purpose"] == "unloaded_drift" and evidence["state"] == "unloaded_qualified"
    assert panel.clear_zero_button.isEnabled() and "active" in panel.zero_label.text()
    assert panel.start_trial()
    assert pump(app, lambda: ctl.now >= .5)
    view = ctl.live_view()
    import numpy as np
    assert view is not None and np.isfinite(view.force_n).all()
    panel.cancel_trial()
    assert ctl.unloaded_correction == correction
    assert panel.replay_trial_evidence(correction["sidecar"])
    assert pump(app, lambda: "Offline unloaded-drift evidence: qualified" in panel.result_label.text(), 15)
    assert pump(app, lambda: not panel.is_busy(), 5)
    assert ctl.unloaded_correction == correction
    assert panel.clear_unloaded_correction()
    assert pump(app, lambda: not panel.is_busy(), 15)
    assert pump(app, lambda: "correction cleared" in panel.result_label.text(), 5)
    assert ctl.unloaded_correction is None


def test_linear_drift_button_continues_into_first_weight_prompt_without_restart(window, app, tmp_path):
    panel = window.force_panel
    measure_group(window, app)
    assert panel.add_results_to_file(str(tmp_path / "inputs.json"))
    window.sim_device.config.noise_lsb = 0
    panel.zero_duration_spin.setValue(6)
    assert panel.linear_zero_button.isEnabled() and panel.start_linear_drift_trial()
    ctl = panel.controller
    session = ctl.session
    assert not panel.placed_button.isEnabled()
    assert pump(app, lambda: ctl.wizard.phase == fc.PLACE_ARMED or ctl.session is None, 30)
    assert ctl.session is session, panel.result_label.text()
    assert ctl.unloaded_correction["correction_kind"] == "charge_rate"
    assert pump(app, lambda: "Place 20 g now" in panel.prompt_label.text(), 5)
    assert "Drift test passed" in panel.result_label.text()
    assert panel.placed_button.isEnabled()
    panel.cancel_trial()
    assert ctl.session is None and ctl.trials == []


def test_drift_wait_shows_live_rejection_reason_and_mismatch(window, app, tmp_path, monkeypatch):
    from captest import force_zero
    panel = window.force_panel
    measure_group(window, app)
    assert panel.add_results_to_file(str(tmp_path / "inputs.json"))
    monkeypatch.setattr(force_zero, "analyse_linear", lambda *a, **kw: {
        "status": "rejected", "routes": {},
        "slope_change_bound_c_per_s": 2.2e-12, "rate_error_budget_c_per_s": 1.7e-12,
        "frozen_rate_mismatch_c_per_s": 26.6e-12,
        "reasons": ["unloaded drift does not match the frozen correction; no automatic refit"]})
    panel.zero_duration_spin.setValue(6)
    assert panel.start_linear_drift_trial()
    ctl = panel.controller
    assert pump(app, lambda: ctl.last_unloaded is not None, 15)
    assert pump(app, lambda: "26.6 pC/s" in panel.prompt_label.text(), 5)
    assert "allowed 1.7 pC/s" in panel.prompt_label.text()
    assert "no automatic refit" in panel.prompt_label.text()
    assert "elapsed" in panel.zero_label.text()
    assert not panel.placed_button.isEnabled()
    panel.cancel_trial()


def test_relative_channel_polarity_is_saved_and_restored_per_array_and_pzt(window, app, tmp_path):
    panel = window.force_panel
    measure_group(window, app, array=2, pzt="PZT1")
    path = tmp_path / "signs.json"
    assert panel.add_results_to_file(str(path))
    panel.polarity_combos["C"].setCurrentIndex(panel.polarity_combos["C"].findData(-1))
    assert fx.ForceInputs.load(path).relative_polarity("A2_PZT1")["C"] == -1
    assert fx.ForceInputs.load(path).relative_polarity("A1_PZT1")["C"] == 1
    assert panel.prepare()
    original = panel.controller
    assert original.route_polarity[(4, 2)] == -1
    panel.pzt_combo.setCurrentIndex(panel.pzt_combo.findText("PZT6"))
    assert panel.polarity_combos["C"].currentData() == 1
    panel.pzt_combo.setCurrentIndex(panel.pzt_combo.findText("PZT1"))
    assert panel.polarity_combos["C"].currentData() == -1 and panel.controller is original
    assert panel.load_inputs(path)
    assert panel.polarity_combos["C"].currentData() == -1


def test_record_polarity_test_listens_without_stage_buttons_or_weight_plan_changes(window, app, tmp_path):
    from test_force_session import scalar_force
    panel = window.force_panel
    measure_group(window, app)
    assert panel.add_results_to_file(str(tmp_path / "inputs.json"))
    window.sim_device.scan.d33_true = 600e-12
    refs = set(panel.group_map.get("A1_PZT1").refs)
    window.sim_device.scan.weights = {r: .2 for r in refs}
    window.sim_device.scan.force = scalar_force(50, t_place=.8, t_remove=1.05)
    assert panel.record_polarity_press()
    ctl = panel.controller
    assert ctl.reference_kind == "known_mass" and not panel.workflow_check.isChecked()
    assert ctl.capture_purpose == "polarity_test"
    assert not panel.polarity_test_button.isEnabled() and not panel.polarity_review_button.isEnabled()
    assert not panel.placed_button.isEnabled() and not panel.clear_button.isEnabled() and not panel.removed_button.isEnabled()
    assert "No stage buttons" in panel.prompt_label.text()
    assert pump(app, lambda: not panel.worker.isRunning(), 15)
    assert ctl.session is None and ctl.last_polarity_result.duration_s < 3.
    assert ctl.wizard.phase == fc.PREPARE and not ctl.trials
    assert pump(app, lambda: "Polarity ready for review" in panel.result_label.text(), 5)
    assert panel.polarity_review_button.isEnabled()
    assert panel.start_button.isEnabled()


def test_applying_reviewed_press_signs_is_explicit_per_package_and_saves_evidence(window, app, tmp_path, monkeypatch):
    from test_force_session import make_rig, make_controller, drive_trial
    panel = window.force_panel
    rig = make_rig(panel.group_map)
    ctl = make_controller(panel.group_map, rig, tmp_path, reference_kind="workflow_only")
    drive_trial(ctl, rig[1])
    panel.controller, panel.forces = ctl, ctl.forces
    panel.forces_path = tmp_path / "signs.json"
    panel.pzt_combo.setCurrentIndex(panel.pzt_combo.findText("PZT1"))
    panel.controller = ctl
    report = ctl.check_press_polarity()
    assert all(v == 1 for v in panel.forces.relative_polarity("A1_PZT1").values())
    # Simulate a clear opposite centre result at the UI boundary; the numeric
    # detector's mixed-polarity behaviour is covered independently.
    report["routes"]["2:2"]["suggested_sign"] = -1
    unclear = dict(report, status="unclear")
    enabled = []
    def close_review(dialog):
        apply = next(b for b in dialog.findChildren(QtWidgets.QPushButton) if b.text() == "Apply signs to this package")
        enabled.append(apply.isEnabled())
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(QtWidgets.QDialog, "exec", close_review)
    panel._show_polarity_review(unclear)
    panel._show_polarity_review(report)
    assert enabled == [False, True]
    assert all(v == 1 for v in panel.forces.relative_polarity("A1_PZT1").values())
    assert not panel._apply_press_polarity(unclear)
    assert panel._apply_press_polarity(report)
    assert pump(app, lambda: not panel.is_busy(), 15)
    saved = fx.ForceInputs.load(panel.forces_path)
    assert saved.relative_polarity("A1_PZT1")["C"] == -1
    assert saved.relative_polarity("A2_PZT1")["C"] == 1
    evidence = saved.data["force_inputs"]["relative_polarity"]["A1_PZT1"]["press_check"]
    assert evidence["capture_sha256"] == ctl.trials[-1].capture_sha256


def test_clipped_polarity_review_shows_notes_and_allows_apply(window, monkeypatch):
    from types import SimpleNamespace
    from test_force_polarity import quick_samples
    from captest import force_polarity
    panel = window.force_panel
    models, samples = quick_samples()
    samples[(2, 2)]["volts"][320] = 4095 * 2.5 / 4096
    report = force_polarity.detect_press(models, samples)
    report["group_id"] = "A1_PZT1"
    assert report["status"] == "ready_for_review"
    def inspect(dialog):
        table = dialog.findChild(QtWidgets.QTableWidget)
        assert table.horizontalHeaderItem(5).text() == "Notes / reason"
        assert "ADC clipped (1 samples)" in table.item(2, 5).text()
        apply = next(b for b in dialog.findChildren(QtWidgets.QPushButton) if b.text() == "Apply signs to this package")
        assert apply.isEnabled()
        return QtWidgets.QDialog.DialogCode.Rejected
    monkeypatch.setattr(QtWidgets.QDialog, "exec", inspect)
    previous = panel.controller
    try:
        panel.controller = SimpleNamespace(group=panel.group_map.get("A1_PZT1"))
        panel._show_polarity_review(report)
    finally:
        panel.controller = previous


def test_editing_relative_polarity_replays_trials_and_locks_controls_during_capture(window, app, tmp_path):
    from captest.force_evidence import read_json
    panel = window.force_panel
    measure_group(window, app)
    assert panel.add_results_to_file(str(tmp_path / "signs.json"))
    fail_a_trial(window, app, panel)
    ctl = panel.controller
    sidecar = ctl.trials[0].sidecar
    panel.polarity_combos["C"].setCurrentIndex(panel.polarity_combos["C"].findData(-1))
    assert pump(app, lambda: not panel.is_busy(), 15)
    assert pump(app, lambda: "Relative polarity updated" in panel.result_label.text(), 5)
    assert ctl.route_polarity[(2, 2)] == -1
    assert ctl.fit is None and ctl.unloaded_correction is None
    assert read_json(sidecar)["analyses"][-1]["route_polarity"]["2:2"] == -1
    assert panel.reset_trials() and panel.start_trial()
    assert not panel.polarity_controls.isEnabled()
    panel.cancel_trial()


def test_rejected_trial_says_why_and_what_to_change(window, app, tmp_path):
    panel = window.force_panel
    measure_group(window, app, 1, "PZT1")
    assert panel.add_results_to_file(str(tmp_path / "f.json"))
    window.sim_device.scan.faults.overrun_after_sweep = 30
    window.sim_device.scan.faults.overrun_count = 1000
    assert panel.start_trial()
    assert pump(app, lambda: not panel.worker.isRunning(), 30.0)
    assert pump(app, lambda: "rejected" in panel.prompt_label.text(), 5.0), panel.prompt_label.text()
    text = panel.prompt_label.text()
    assert "OVERRUN" in text and "Added lag per sweep" in text
    assert panel.steps_table.item(0, 3).text() == "failed" and panel.retry_button.isEnabled()


def test_measured_sampling_rate_per_channel_is_shown_while_capturing(window, app, tmp_path):
    panel = window.force_panel
    measure_group(window, app, 1, "PZT1")
    assert panel.add_results_to_file(str(tmp_path / "f.json"))
    assert "Measured" not in panel.rate_label.text()                   # an estimate only, before capture
    assert panel.start_trial()
    assert pump(app, lambda: "Measured sampling rate per channel" in panel.rate_label.text(), 30.0),         panel.rate_label.text()
    text = panel.rate_label.text()
    for position in ("B ", "L ", "C ", "R ", "T "):
        assert position in text.split("Measured sampling rate per channel:")[1]
    import re

    rates = [float(x) for x in re.findall(r"[BLCRT] ([0-9.]+) Hz", text)]
    estimate = float(re.search(r"about ([0-9]+) Hz \(revisit", text).group(1))
    assert len(rates) == 5 and all(abs(r - estimate) / estimate < 0.10 for r in rates), (rates, estimate)
    panel.cancel_button.click()


def fail_a_trial(window, app, panel):
    faults = window.sim_device.scan.faults
    faults.overrun_after_sweep, faults.overrun_count = 30, 1000
    assert panel.start_trial()
    assert pump(app, lambda: not panel.worker.isRunning(), 30.0)
    assert pump(app, lambda: "rejected" in panel.prompt_label.text(), 5.0)
    faults.overrun_after_sweep, faults.overrun_count = None, 0


def test_after_a_failed_trial_retry_clears_the_screen_and_the_test_can_run_again(window, app, tmp_path):
    panel = window.force_panel
    measure_group(window, app, 1, "PZT1")
    assert panel.add_results_to_file(str(tmp_path / "f.json"))
    fail_a_trial(window, app, panel)
    assert panel.steps_table.item(0, 3).text() == "failed" and panel.retry_button.isEnabled()
    assert panel.start_button.isEnabled() and panel.pzt_combo.isEnabled()
    panel.retry_button.click()
    assert panel.steps_table.item(0, 3).text() == "pending"
    assert "rejected" not in panel.prompt_label.text() and panel.headroom_label.text() == ""
    assert panel.start_trial()
    ctl = panel.controller
    assert pump(app, lambda: ctl.wizard.phase == fc.PLACE_ARMED, 60.0), ctl.wizard.phase
    panel.cancel_button.click()


def test_reset_trials_starts_again_from_the_first_mass_and_selectors_lock_while_running(window, app, tmp_path):
    panel = window.force_panel
    measure_group(window, app, 1, "PZT1")
    assert panel.add_results_to_file(str(tmp_path / "f.json"))
    fail_a_trial(window, app, panel)
    assert any(t.accepted is False for t in panel.controller.trials)
    assert panel.reset_trials()
    assert panel.controller is None and panel.steps_table.rowCount() == 0
    assert panel.start_trial()
    assert not panel.pzt_combo.isEnabled() and not panel.array_combo.isEnabled()     # locked while running
    assert not panel.reset_trials()                                                   # not while running
    assert "Cancel the running trial" in window.log_view.toPlainText()
    panel.cancel_button.click()
    assert panel.pzt_combo.isEnabled()


def test_start_that_cannot_happen_says_why(window, app, tmp_path):
    panel = window.force_panel
    measure_group(window, app, 1, "PZT1")
    assert panel.add_results_to_file(str(tmp_path / "f.json"))
    window.sim_device.config.firmware_version = "1.3.0"
    assert not panel.start_trial()
    assert "no continuous capture" in panel.prompt_label.text()
    assert "no continuous capture" in window.log_view.toPlainText()


def test_force_tab_takes_the_whole_right_hand_area(window):
    panel = window.force_panel
    assert window.right_stack.indexOf(panel.workspace) >= 0
    window.tabs.setCurrentWidget(panel)
    assert window.right_stack.currentWidget() is panel.workspace
    for name in ("prompt_label", "start_button", "force_plot", "resid_plot", "steps_table"):
        assert panel.workspace.isAncestorOf(getattr(panel, name))                 # all in the big area
    window.tabs.setCurrentIndex(0)
    assert window.right_stack.currentIndex() == 0
