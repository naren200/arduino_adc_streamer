import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from constants.pressure_map import (
    PRESSURE_MAP_PLOT_UPDATE_INTERVAL_SEC,
    PRESSURE_MAP_REFRESH_DUTY_FACTOR,
)
from data_processing.binary_processor import BinaryProcessorMixin
from data_processing.refresh_schedule import next_refresh_allowed_at
from gui.signal_integration_panel import PressureMapPanelMixin

MIN_INTERVAL = 1.0 / 30.0
DUTY = 2.0


class NextRefreshAllowedAtTests(unittest.TestCase):
    def test_first_refresh_is_allowed_at_any_time(self):
        self.assertEqual(next_refresh_allowed_at(None, 0.0, MIN_INTERVAL, DUTY), float("-inf"))

    def test_cheap_refresh_is_limited_by_min_interval(self):
        allowed_at = next_refresh_allowed_at(100.0, 0.005, MIN_INTERVAL, DUTY)
        self.assertAlmostEqual(allowed_at, 100.0 + MIN_INTERVAL)

    def test_slow_refresh_is_limited_by_duty_factor(self):
        allowed_at = next_refresh_allowed_at(100.0, 0.19, MIN_INTERVAL, DUTY)
        self.assertAlmostEqual(allowed_at, 100.0 + 0.38)

    def test_boundary_where_duty_equals_min_interval(self):
        allowed_at = next_refresh_allowed_at(10.0, MIN_INTERVAL / DUTY, MIN_INTERVAL, DUTY)
        self.assertAlmostEqual(allowed_at, 10.0 + MIN_INTERVAL)

    def test_zero_duration_uses_min_interval(self):
        self.assertAlmostEqual(next_refresh_allowed_at(5.0, 0.0, MIN_INTERVAL, DUTY), 5.0 + MIN_INTERVAL)

    def test_zero_interval_and_duration_allows_at_finish_time(self):
        self.assertEqual(next_refresh_allowed_at(7.0, 0.0, 0.0, DUTY), 7.0)


class GateHarness(BinaryProcessorMixin):
    pass


class PanelHarness(PressureMapPanelMixin):
    def __init__(self, snapshot):
        self.signal_integration_plot_widget = object()
        self._signal_integration_updating_plot = False
        self._signal_integration_last_refresh_finished_at = None
        self._signal_integration_last_refresh_duration_sec = 0.0
        self.config = {"channels": [1]}
        self.signal_integration_status_label = MagicMock()
        self._snapshot = snapshot
        self.logged = []

    def _should_refresh_signal_integration_plot(self):
        return True

    def _get_signal_integration_show_graph(self):
        return True

    def _apply_signal_integration_graph_visibility(self):
        return None

    def get_display_channel_specs(self):
        return [object()]

    def _get_signal_integration_timeline_specs(self):
        return []

    def _get_signal_integration_timeline_mode(self):
        return "PZT"

    def _get_signal_integration_raw_snapshot(self):
        return self._snapshot

    def _hide_all_signal_integration_curves(self):
        return None

    def _clear_shear_visualization(self):
        return None

    def log_status(self, message):
        self.logged.append(message)


def run_refresh(panel, start, finish):
    """Run one refresh with the panel clock advancing from start to finish."""
    with patch("gui.signal_integration_panel.time.time", side_effect=[start, finish]):
        panel.update_signal_integration_plot()


class RefreshGateIntegrationTests(unittest.TestCase):
    def setUp(self):
        data = np.zeros((4, 2))
        self.heavy_panel = PanelHarness((data, np.arange(4.0), 0.0))
        self.gate = GateHarness()
        self.gate._signal_integration_last_refresh_finished_at = None
        self.gate._signal_integration_last_refresh_duration_sec = 0.0

    def _copy_state(self):
        self.gate._signal_integration_last_refresh_finished_at = (
            self.heavy_panel._signal_integration_last_refresh_finished_at
        )
        self.gate._signal_integration_last_refresh_duration_sec = (
            self.heavy_panel._signal_integration_last_refresh_duration_sec
        )

    def test_first_refresh_is_due_immediately(self):
        self.assertTrue(self.gate._is_signal_integration_refresh_due(0.0))

    def test_slow_refresh_keeps_gate_closed_then_reopens(self):
        run_refresh(self.heavy_panel, start=100.0, finish=100.19)
        self._copy_state()
        wait = PRESSURE_MAP_REFRESH_DUTY_FACTOR * 0.19

        self.assertFalse(self.gate._is_signal_integration_refresh_due(100.19))
        self.assertFalse(self.gate._is_signal_integration_refresh_due(100.19 + wait - 0.01))
        self.assertTrue(self.gate._is_signal_integration_refresh_due(100.19 + wait + 0.01))

    def test_cheap_refresh_reopens_after_min_interval(self):
        run_refresh(self.heavy_panel, start=100.0, finish=100.001)
        self._copy_state()
        interval = PRESSURE_MAP_PLOT_UPDATE_INTERVAL_SEC

        self.assertFalse(self.gate._is_signal_integration_refresh_due(100.001 + interval - 0.005))
        self.assertTrue(self.gate._is_signal_integration_refresh_due(100.001 + interval + 0.001))

    def test_early_return_refresh_does_not_delay_next_one(self):
        waiting_panel = PanelHarness(None)
        with patch("gui.signal_integration_panel.time.time", side_effect=AssertionError("clock read")):
            waiting_panel.update_signal_integration_plot()

        self.assertIsNone(waiting_panel._signal_integration_last_refresh_finished_at)
        self.gate._signal_integration_last_refresh_finished_at = (
            waiting_panel._signal_integration_last_refresh_finished_at
        )
        self.assertTrue(self.gate._is_signal_integration_refresh_due(0.0))


if __name__ == "__main__":
    unittest.main()
