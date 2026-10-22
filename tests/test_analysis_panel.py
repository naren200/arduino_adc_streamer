import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyqtgraph as pg
from types import SimpleNamespace
from PyQt6.QtWidgets import QApplication

from constants.pzt_force import ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS, PZT_FORCE_DEFAULT_SETTINGS
from gui.analysis_panel import AnalysisPanelMixin


class FakePlot:
    def __init__(self):
        self.visible = True

    def setVisible(self, visible):
        self.visible = bool(visible)

    def isVisible(self):
        return bool(self.visible)


class FakeSplitter:
    def __init__(self):
        self.minimum_height = None
        self.sizes = None

    def setMinimumHeight(self, minimum_height):
        self.minimum_height = minimum_height

    def setSizes(self, sizes):
        self.sizes = list(sizes)


class AnalysisPlotVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.harness = AnalysisPanelMixin()
        self.addCleanup(self.harness.shutdown_analysis_worker)
        self.harness.analysis_signal_plot = FakePlot()
        self.harness.analysis_integration_plot = FakePlot()
        self.harness.analysis_derived_plot = FakePlot()
        self.harness.analysis_force_plot = FakePlot()
        self.harness.analysis_burst_plot = FakePlot()
        self.harness.analysis_rolling_plot = FakePlot()
        self.harness.analysis_gate_plot = FakePlot()
        self.harness.analysis_plot_splitter = FakeSplitter()

    def test_hides_unrequested_empty_plots_and_collapses_their_space(self):
        self.harness._update_analysis_plot_visibility(
            {"signal": True, "integration": False, "derived": False, "force": False}
        )

        self.assertTrue(self.harness.analysis_signal_plot.visible)
        self.assertFalse(self.harness.analysis_integration_plot.visible)
        self.assertFalse(self.harness.analysis_derived_plot.visible)
        self.assertFalse(self.harness.analysis_force_plot.visible)
        self.assertEqual(self.harness.analysis_plot_splitter.minimum_height, 250)
        self.assertEqual(self.harness.analysis_plot_splitter.sizes, [250, 0, 0, 0, 0, 0, 0])

    def test_shows_each_available_requested_plot(self):
        self.harness._update_analysis_plot_visibility(
            {"signal": True, "integration": True, "derived": True, "force": True}
        )

        self.assertTrue(self.harness.analysis_integration_plot.visible)
        self.assertTrue(self.harness.analysis_derived_plot.visible)
        self.assertTrue(self.harness.analysis_force_plot.visible)
        self.assertEqual(self.harness.analysis_plot_splitter.minimum_height, 750)
        self.assertEqual(self.harness.analysis_plot_splitter.sizes, [250, 200, 200, 100, 0, 0, 0])


class DummySpin:
    """Small value-only stand-in for a Qt spin box."""

    def __init__(self, value=0.0):
        self._value = value

    def value(self):
        return self._value

    def setValue(self, value):
        self._value = value

    def setVisible(self, _visible):
        pass

    def setEnabled(self, _enabled):
        pass


class DummyCheck:
    """Small checked-state stand-in for a Qt check box."""

    def __init__(self, checked=False):
        self._checked = checked

    def isChecked(self):
        return self._checked

    def setChecked(self, checked):
        self._checked = bool(checked)


class DummyCombo:
    """Small index/text stand-in for a Qt combo box."""

    def __init__(self, index=0, text=""):
        self._index = index
        self._text = text

    def currentIndex(self):
        return self._index

    def setCurrentIndex(self, index):
        self._index = index

    def currentText(self):
        return self._text

    def setCurrentText(self, text):
        self._text = str(text)


class DummyTextWidget:
    """Small text-only stand-in for a Qt label/line edit."""

    def __init__(self, text=""):
        self._text = text

    def text(self):
        return self._text

    def setText(self, text):
        self._text = str(text)

    def setPlainText(self, text):
        self._text = str(text)


class AnalysisPanelHarness(AnalysisPanelMixin):
    """Minimal harness exercising the PZT force settings save/restore path."""

    def __init__(self, settings_path):
        self._settings_path = Path(settings_path)
        self._init_analysis_state()
        self.log_messages = []

        self.analysis_source_combo = DummyCombo()
        self.analysis_axis_combo = DummyCombo()
        self.analysis_zoom_combo = DummyCombo()
        self.analysis_filter_check = DummyCheck()
        self.analysis_marker_check = DummyCheck(True)
        self.analysis_shear_check = DummyCheck()
        self.analysis_normal_check = DummyCheck()
        self.analysis_integration_check = DummyCheck()
        self.analysis_baseline_removed_check = DummyCheck()
        self.analysis_shear_force_check = DummyCheck()
        self.analysis_normal_force_check = DummyCheck()

        self.analysis_pzt_center_capacitance_spin = DummySpin(float(PZT_FORCE_DEFAULT_SETTINGS["center_capacitance_value"]))
        self.analysis_pzt_outer_capacitance_spin = DummySpin(float(PZT_FORCE_DEFAULT_SETTINGS["outer_capacitance_value"]))
        self.analysis_pzt_capacitance_unit_combo = DummyCombo(text=str(PZT_FORCE_DEFAULT_SETTINGS["capacitance_unit"]))
        self.analysis_pzt_rleak_spin = DummySpin(float(PZT_FORCE_DEFAULT_SETTINGS["rleak_ohm"]))
        self.analysis_pzt_d33_spin = DummySpin(float(PZT_FORCE_DEFAULT_SETTINGS["d33_pc_per_n"]))
        self.analysis_pzt_mux_timing_combo = DummyCombo(text="Auto")
        self.analysis_pzt_mux_connected_ms_spin = DummySpin(float(PZT_FORCE_DEFAULT_SETTINGS["mux_connected_time_s"]) * 1000.0)
        self.analysis_pzt_mux_timing_status = DummyTextWidget()
        self.analysis_pzt_off_mux_leak_check = DummyCheck()
        self.analysis_pzt_off_mux_rleak_spin = DummySpin(float(PZT_FORCE_DEFAULT_SETTINGS["rleak_ohm"]))
        self.analysis_pzt_stuck_failsafe_check = DummyCheck(bool(PZT_FORCE_DEFAULT_SETTINGS["stuck_force_failsafe_enabled"]))
        self.analysis_pzt_stuck_hold_spin = DummySpin(float(PZT_FORCE_DEFAULT_SETTINGS["stuck_force_quiet_hold_s"]))
        self.analysis_pzt_stuck_tau_spin = DummySpin(float(PZT_FORCE_DEFAULT_SETTINGS["stuck_force_decay_tau_s"]))
        self.analysis_pzt_zero_band_fraction_spin = DummySpin(float(PZT_FORCE_DEFAULT_SETTINGS["force_zero_band_fraction"]))
        self.analysis_pzt_quiet_release_spin = DummySpin(float(PZT_FORCE_DEFAULT_SETTINGS["quiet_hold_release_fraction"]))
        self.analysis_pzt_quiet_hold_spin = DummySpin(float(PZT_FORCE_DEFAULT_SETTINGS["quiet_hold_clear_s"]))
        self.analysis_shear_force_noise_spin = DummySpin(float(ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS["shear_force_noise_threshold_n"]))
        self.analysis_normal_force_noise_spin = DummySpin(float(ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS["normal_force_noise_threshold_n"]))
        self.analysis_shear_force_zero_floor_spin = DummySpin(float(ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS["shear_force_zero_band_min_n"]))
        self.analysis_normal_force_zero_floor_spin = DummySpin(float(ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS["normal_force_zero_band_min_n"]))
        self.analysis_shear_force_min_event_peak_spin = DummySpin(float(ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS["shear_force_zero_min_event_peak_n"]))
        self.analysis_normal_force_min_event_peak_spin = DummySpin(float(ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS["normal_force_zero_min_event_peak_n"]))
        self.analysis_csv_path_edit = DummyTextWidget()

    def _get_last_analysis_settings_path(self):
        return self._settings_path

    def log_status(self, message):
        self.log_messages.append(message)


class AnalysisPztForceEventTunablesRoundTripTests(unittest.TestCase):
    """The shared natural-reset event tunables round trip through
    save/restore, and a legacy payload lacking them falls back to the shared
    defaults. Only ``force_zero_band_fraction``, ``quiet_hold_release_fraction``,
    and ``quiet_hold_clear_s`` remain shared/generic here -- the former
    ``force_zero_band_min_n``/``force_zero_min_event_peak_n`` generic
    spinboxes were removed from the Analysis tab along with "PZT Channel
    Force" (their only consumer); Shear Force / Normal Force always use
    their own role-specific ``shear_force_*``/``normal_force_*`` zero-band/
    min-event-peak settings instead, covered elsewhere."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_shared_event_tunables_survive_save_and_restore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "last_used_analysis_settings.json"
            harness = AnalysisPanelHarness(settings_path)
            self.addCleanup(harness.shutdown_analysis_worker)

            harness.analysis_pzt_zero_band_fraction_spin.setValue(0.2)
            harness.analysis_pzt_quiet_release_spin.setValue(0.4)
            harness.analysis_pzt_quiet_hold_spin.setValue(0.3)

            harness.on_analysis_settings_changed()

            payload = json.loads(settings_path.read_text(encoding="utf-8"))
            saved_pzt_force = payload["analysis_settings"]["pzt_force"]
            self.assertEqual(saved_pzt_force["force_zero_band_fraction"], 0.2)
            self.assertEqual(saved_pzt_force["quiet_hold_release_fraction"], 0.4)
            self.assertEqual(saved_pzt_force["quiet_hold_clear_s"], 0.3)

            restored = AnalysisPanelHarness(settings_path)
            self.addCleanup(restored.shutdown_analysis_worker)
            restored.load_last_analysis_settings()

            self.assertEqual(restored.analysis_pzt_zero_band_fraction_spin.value(), 0.2)
            self.assertEqual(restored.analysis_pzt_quiet_release_spin.value(), 0.4)
            self.assertEqual(restored.analysis_pzt_quiet_hold_spin.value(), 0.3)

    def test_legacy_payload_missing_the_shared_keys_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "legacy_analysis_settings.json"
            legacy_payload = {
                "version": 1,
                "analysis_settings": {
                    "pzt_force": {
                        "enabled": True,
                        "rleak_ohm": 2_000_000.0,
                        # The shared tunables are absent, as an older save
                        # predating them would be.
                    },
                },
            }
            settings_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

            harness = AnalysisPanelHarness(settings_path)
            self.addCleanup(harness.shutdown_analysis_worker)
            harness.analysis_pzt_zero_band_fraction_spin.setValue(0.999)  # must not survive the load

            harness.load_last_analysis_settings()

            # The Analysis tab's own fallback defaults are
            # ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS (see gui/analysis_panel.py),
            # not the shared PZT_FORCE_DEFAULT_SETTINGS.
            self.assertEqual(
                harness.analysis_pzt_zero_band_fraction_spin.value(),
                float(ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS["force_zero_band_fraction"]),
            )
            self.assertEqual(
                harness.analysis_pzt_quiet_release_spin.value(),
                float(ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS["quiet_hold_release_fraction"]),
            )
            self.assertEqual(
                harness.analysis_pzt_quiet_hold_spin.value(),
                float(ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS["quiet_hold_clear_s"]),
            )
            # An untouched pre-existing key survives alongside the defaults.
            self.assertEqual(harness.analysis_pzt_rleak_spin.value(), 2_000_000.0)


class AnalysisBaselineRemovedOverlayRoundTripTests(unittest.TestCase):
    """The "Baseline Removed" overlay checkbox round trips through
    save/restore the same way the existing shear/normal/integration overlay
    checks do (see ``overlays`` in ``_init_analysis_state`` and its
    load/save wiring in ``gui/analysis_panel.py``)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_baseline_removed_check_survives_save_and_restore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "last_used_analysis_settings.json"
            harness = AnalysisPanelHarness(settings_path)
            self.addCleanup(harness.shutdown_analysis_worker)

            harness.analysis_baseline_removed_check.setChecked(True)
            harness.on_analysis_settings_changed()

            payload = json.loads(settings_path.read_text(encoding="utf-8"))
            saved_overlays = payload["analysis_settings"]["overlays"]
            self.assertTrue(saved_overlays["baseline_removed"])

            restored = AnalysisPanelHarness(settings_path)
            self.addCleanup(restored.shutdown_analysis_worker)
            restored.load_last_analysis_settings()

            self.assertTrue(restored.analysis_baseline_removed_check.isChecked())



class FakeWindowSource:
    """Satisfies the AnalysisWindowSource contract with a fixed span."""

    def __init__(self, span_s):
        self.source_id = "fake"
        self.span_s = (0.0, float(span_s))

    def clamp_view_start(self, view_start_s):
        return min(max(0.0, view_start_s), max(0.0, self.span_s[1] - 60.0))


class StubLoadWorker:
    def __init__(self):
        self.payloads = []

    def submit(self, payload):
        self.payloads.append(payload)

    def stop(self):
        pass

    def wait(self, _timeout):
        pass


def make_window_snapshot(span_s, view_start_s):
    view_end_s = min(view_start_s + 60.0, span_s)
    resident = (max(0.0, view_start_s - 20.0), min(view_end_s + 20.0, span_s))
    return SimpleNamespace(
        span_s=(0.0, span_s), view_range_s=(view_start_s, view_end_s), resident_range_s=resident,
        sweep_count=10, samples_per_sweep=5,
    )


class AnalysisWindowNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.panel = AnalysisPanelMixin()
        self.panel._init_analysis_state()
        self.addCleanup(self.panel.shutdown_analysis_worker)
        self.panel.analysis_source_load_worker.stop()
        self.panel.analysis_source_load_worker.wait(1500)
        self.panel.analysis_source_load_worker = StubLoadWorker()
        self.controls_container = self.panel._build_analysis_window_controls()
        self.panel._set_analysis_status_text = lambda _text: None

    def load(self, span_s, view_start_s=0.0):
        self.panel.analysis_window_source = FakeWindowSource(span_s)
        self.panel.analysis_snapshot = make_window_snapshot(span_s, view_start_s)
        self.panel._update_analysis_window_controls()

    def enabled(self):
        return (
            self.panel.analysis_view_left_btn.isEnabled(),
            self.panel.analysis_view_right_btn.isEnabled(),
        )

    def test_buttons_disabled_without_source(self):
        self.panel._update_analysis_window_controls()
        self.assertEqual(self.enabled(), (False, False))

    def test_buttons_disabled_for_short_source(self):
        self.load(span_s=45.0)
        self.assertEqual(self.enabled(), (False, False))

    def test_only_right_enabled_at_start(self):
        self.load(span_s=300.0, view_start_s=0.0)
        self.assertEqual(self.enabled(), (False, True))

    def test_only_left_enabled_at_end(self):
        self.load(span_s=300.0, view_start_s=240.0)
        self.assertEqual(self.enabled(), (True, False))

    def test_both_enabled_in_middle(self):
        self.load(span_s=300.0, view_start_s=80.0)
        self.assertEqual(self.enabled(), (True, True))

    def test_right_click_submits_window_payload_and_blocks_buttons(self):
        self.load(span_s=300.0, view_start_s=0.0)
        self.panel.on_analysis_move_view(1)

        payload = self.panel.analysis_source_load_worker.payloads[-1]
        self.assertEqual(payload["view_start_s"], 40.0)
        self.assertIs(payload["window_source"], self.panel.analysis_window_source)
        self.assertEqual(self.enabled(), (False, False))

    def test_move_is_clamped_to_end(self):
        self.load(span_s=300.0, view_start_s=220.0)
        self.panel.on_analysis_move_view(1)
        self.assertEqual(self.panel.analysis_source_load_worker.payloads[-1]["view_start_s"], 240.0)

    def test_limits_cap_zoom_at_60_s_and_pan_to_resident_range(self):
        plots = [pg.PlotWidget() for _ in range(4)]
        (self.panel.analysis_signal_plot, self.panel.analysis_integration_plot,
         self.panel.analysis_derived_plot, self.panel.analysis_force_plot) = plots
        self.load(span_s=300.0, view_start_s=100.0)
        self.panel.analysis_prepared = SimpleNamespace(x_units="s")

        self.panel._apply_analysis_window_limits(force_view_range=True)

        limits = plots[0].getViewBox().state["limits"]
        self.assertEqual(limits["xRange"][1], 60.0)
        self.assertEqual(limits["xLimits"], [80.0, 180.0])
        self.assertEqual(plots[0].viewRange()[0], [100.0, 160.0])

    def test_loaded_status_names_view_and_resident_sweeps(self):
        self.load(span_s=300.0, view_start_s=40.0)
        self.panel._install_analysis_window_status()
        self.assertIn("view 40-100 s of 300 s (10 sweeps resident", self.panel._analysis_loaded_status)
        self.assertEqual(self.panel.analysis_view_range_label.text(), "View 40.0-100.0 s of 300.0 s")

    def test_label_duration_uses_full_span(self):
        self.load(span_s=300.0, view_start_s=40.0)
        self.panel.analysis_prepared = SimpleNamespace(x_units="s")
        self.assertEqual(self.panel._analysis_label_trace_duration_s(), 300.0)

    def test_buttons_disabled_while_touchid_replays_the_window(self):
        from core.texture_piezo.application.mode import TouchIdMode

        self.load(span_s=300.0, view_start_s=80.0)
        self.panel.touchid_mode = TouchIdMode.REPLAYING
        self.panel._update_analysis_window_controls()
        self.assertEqual(self.enabled(), (False, False))

    def test_sample_axis_drops_time_limits(self):
        plots = [pg.PlotWidget() for _ in range(4)]
        (self.panel.analysis_signal_plot, self.panel.analysis_integration_plot,
         self.panel.analysis_derived_plot, self.panel.analysis_force_plot) = plots
        self.load(span_s=300.0, view_start_s=100.0)
        self.panel.analysis_prepared = SimpleNamespace(x_units="s")
        self.panel._apply_analysis_window_limits(force_view_range=False)

        self.panel.analysis_prepared = SimpleNamespace(x_units="")
        self.panel._apply_analysis_window_limits(force_view_range=False)

        limits = plots[0].getViewBox().state["limits"]
        self.assertEqual(limits["xLimits"], [None, None])
        self.assertEqual(limits["xRange"], [None, None])


if __name__ == "__main__":
    unittest.main()
