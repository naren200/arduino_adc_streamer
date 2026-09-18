import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtWidgets import QApplication

from gui.inference_panel import InferencePanelMixin
from inference.mode import TouchIdMode
from inference.segmentation import ActiveSampleQueue
from inference.quality_gate import IdleBaseline


class FakeTabs:
    """Minimal QTabWidget-alike: one tab, whose text/current-index we control."""

    def __init__(self, text='TouchID'):
        self._idx = 0
        self._text = text

    def currentIndex(self):
        return self._idx

    def tabText(self, index):
        return self._text if index == self._idx else ''

    def set_current_text(self, text):
        self._text = text


class TouchIdHarness(InferencePanelMixin):
    """Fake ADCStreamerGUI-like host: just enough of the surrounding app for
    InferencePanelMixin to run standalone, following the harness convention
    used elsewhere under tests/ (e.g. test_adc_plotting.py, test_timing_display.py)."""

    def __init__(self):
        self.is_capturing = False
        self.visualization_tabs = FakeTabs()
        self.samples_per_sweep = 5
        self._t = 0.0
        self.init_touchid_state()
        # create_touchid_tab builds the real plot widgets update_touchid_display
        # writes into (touchid_stream_plot_widget, touchid_stream_curves, ...).
        # Keep a strong reference -- without a parent, PyQt would otherwise
        # garbage-collect the underlying C++ objects out from under later calls.
        self._tab = self.create_touchid_tab()

    def get_display_channel_specs(self):
        cols = self.touchid_config.pzt_columns
        return [{'label': c, 'sample_indices': [i]} for i, c in enumerate(cols)]

    def _extract_recent_sweeps(self, required_sweeps):
        n = required_sweeps
        data = (np.random.randn(n, 5).astype(np.float32) * 10 + 500)
        ts = self._t + np.arange(n) * (1.0 / 1000.0)
        self._t = ts[-1] + 1.0 / 1000.0
        return data, ts

    def get_measured_sweep_rate_hz(self):
        return 1000.0


class TouchIdLivePathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Keep the reference: a garbage-collected QApplication crashes Qt.
        cls.app = QApplication.instance() or QApplication([])

    def test_update_touchid_display_renders_stream_and_rate_when_capturing(self):
        """Regression test for the empty-plot/frozen-rate-label bug: once
        capturing and on the TouchID tab, update_touchid_display must
        actually draw curves and update the rate label every tick."""
        harness = TouchIdHarness()
        harness.is_capturing = True
        harness.sync_touchid_timer_state()
        self.assertTrue(harness.touchid_timer.isActive())

        for _ in range(3):
            harness.update_touchid_display()

        self.assertEqual(len(harness.touchid_stream_curves), 5)
        self.assertEqual(harness.touchid_sample_rate_label.text(), 'Per-channel rate: 1000.00 Hz')

    def test_touchid_timer_starts_when_capture_begins_while_tab_already_active(self):
        """Root cause of the reported bug: touchid_timer was only ever started
        from the visualization tab-change handler. If the user is already
        sitting on the TouchID tab when Start Capture is clicked, no tab
        change ever fires, so the timer must start some other way -- this is
        what sync_touchid_timer_state (called from capture_lifecycle's
        start_capture) provides."""
        harness = TouchIdHarness()
        self.assertFalse(harness.touchid_timer.isActive())

        # Simulate "already on the TouchID tab" -- no tab-change signal fires.
        harness.is_capturing = True
        harness.sync_touchid_timer_state()

        self.assertTrue(harness.touchid_timer.isActive())

    def test_touchid_timer_stops_when_tab_is_not_touchid(self):
        harness = TouchIdHarness()
        harness.is_capturing = True
        harness.sync_touchid_timer_state()
        self.assertTrue(harness.touchid_timer.isActive())

        harness.visualization_tabs.set_current_text('Time Series')
        harness.sync_touchid_timer_state()
        self.assertFalse(harness.touchid_timer.isActive())

    def test_channel_index_map_miss_reports_reason_instead_of_silence(self):
        """When the configured pzt_columns aren't among the currently
        streaming display channels (e.g. main channel selector not in
        'array' mode with this PZT sensor picked), the tab must say why
        instead of just sitting frozen with no visible explanation."""
        harness = TouchIdHarness()
        harness.is_capturing = True

        def mismatched_specs():
            return [{'label': 'Ch 3', 'sample_indices': [0]}]

        harness.get_display_channel_specs = mismatched_specs
        harness.update_touchid_display()

        self.assertIn('waiting for streaming channels', harness.touchid_idle_gate_label.text())


class ActiveSampleQueueFragIdTests(unittest.TestCase):
    """ready_windows() must expose a stable frag_id so callers (the GUI's
    per-segment inference-region coloring) can tell "same touch event" from
    "a new one started" without comparing index ranges."""

    def _baseline(self, n_channels=5):
        return IdleBaseline(
            pzt_columns=[f'PZT3_{c}' for c in 'BLCRT'][:n_channels],
            mean=np.zeros(n_channels),
            std=np.ones(n_channels) * 0.1,
            fs=1000.0,
            k=5.0,
            captured_duration_s=5.0,
        )

    def test_windows_from_same_fragment_share_frag_id(self):
        fs = 1000.0
        queue = ActiveSampleQueue(
            fs=fs, window_size_s=0.1, hop_size_s=0.05, baseline=self._baseline(),
        )
        chunk_n = 50  # 0.05s at 1000Hz
        active_chunk = np.ones((chunk_n, 5)) * 10.0  # far above baseline -> active
        idx = 0
        now_t = 0.0
        windows = []
        # Push several active chunks, well past one window_size_s, with no
        # idle gap -- should all belong to the same open fragment.
        for _ in range(6):
            queue.push_micro_chunk((idx, idx + chunk_n), active_chunk, now_t)
            idx += chunk_n
            now_t += chunk_n / fs
            windows.extend(queue.ready_windows())

        self.assertGreaterEqual(len(windows), 2)
        frag_ids = {w[2] for w in windows}
        self.assertEqual(len(frag_ids), 1)

    def test_new_fragment_after_idle_gets_a_new_frag_id(self):
        fs = 1000.0
        queue = ActiveSampleQueue(
            fs=fs, window_size_s=0.1, hop_size_s=0.05, baseline=self._baseline(),
        )
        chunk_n = 50
        active_chunk = np.ones((chunk_n, 5)) * 10.0
        idle_chunk = np.zeros((chunk_n, 5))
        idx = 0
        now_t = 0.0
        windows = []

        def push(chunk, n_pushes):
            nonlocal idx, now_t
            for _ in range(n_pushes):
                queue.push_micro_chunk((idx, idx + chunk_n), chunk, now_t)
                idx += chunk_n
                now_t += chunk_n / fs
                windows.extend(queue.ready_windows())

        push(active_chunk, 4)
        # Long enough idle run to force a genuine strip-and-close (not just
        # absorbed inline).
        push(idle_chunk, 10)
        push(active_chunk, 4)

        frag_ids_seen = [w[2] for w in windows]
        first_frag = frag_ids_seen[0]
        last_frag = frag_ids_seen[-1]
        self.assertNotEqual(first_frag, last_frag)


class TouchIdModeGuardTests(unittest.TestCase):
    """touchid_mode (inference/mode.py) must serialize CAPTURING_BASELINE and
    REPLAYING -- neither may start while the other is in progress, and
    sync_touchid_timer_state must not resume live streaming mid-replay (see
    those methods' docstrings for why: interleaving live serial data or a
    second concurrent activity into either's TouchIdStreamProcessor would
    silently corrupt its causal state)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_capture_idle_refused_while_replaying(self):
        harness = TouchIdHarness()
        harness.is_capturing = True
        harness.touchid_mode = TouchIdMode.REPLAYING
        with patch('gui.inference_panel.QMessageBox.warning') as warn:
            harness.on_touchid_capture_idle_clicked()
        warn.assert_called_once()
        self.assertFalse(harness.touchid_idle_capture_active)
        self.assertEqual(harness.touchid_mode, TouchIdMode.REPLAYING)

    def test_capture_idle_refused_while_already_capturing(self):
        harness = TouchIdHarness()
        harness.is_capturing = True
        harness.touchid_mode = TouchIdMode.CAPTURING_BASELINE
        with patch('gui.inference_panel.QMessageBox.warning') as warn:
            harness.on_touchid_capture_idle_clicked()
        warn.assert_called_once()

    def test_replay_refused_while_capturing_baseline(self):
        harness = TouchIdHarness()
        harness.touchid_mode = TouchIdMode.CAPTURING_BASELINE
        with patch('gui.inference_panel.QMessageBox.warning') as warn:
            harness.on_touchid_run_on_source_clicked()
        warn.assert_called_once()
        self.assertEqual(harness.touchid_mode, TouchIdMode.CAPTURING_BASELINE)

    def test_replay_refused_while_already_replaying(self):
        harness = TouchIdHarness()
        harness.touchid_mode = TouchIdMode.REPLAYING
        with patch('gui.inference_panel.QMessageBox.warning') as warn:
            harness.on_touchid_run_on_source_clicked()
        warn.assert_called_once()

    def test_sync_timer_state_refuses_to_start_while_replaying(self):
        harness = TouchIdHarness()
        harness.is_capturing = True
        harness.touchid_mode = TouchIdMode.REPLAYING
        harness.sync_touchid_timer_state()
        self.assertFalse(harness.touchid_timer.isActive())


if __name__ == '__main__':
    unittest.main()
