import threading
import unittest

import numpy as np

from data_processing.adc_plotting import ADCPlottingMixin


class RosettePlottingHarness(ADCPlottingMixin):
    def __init__(self):
        self.samples_per_sweep = 2
        self.MAX_SWEEPS_BUFFER = 8
        self.sweep_count = 4
        self.buffer_write_index = 4
        self.buffer_lock = threading.Lock()
        self.raw_data_buffer = np.array(
            [
                [10.0, 100.0],
                [20.0, 200.0],
                [30.0, 300.0],
                [40.0, 400.0],
            ],
            dtype=np.float32,
        )
        self.sweep_timestamps_buffer = np.arange(4, dtype=np.float64)
        self.rosette_plot_baselines = {}
        self.logged = []

    def get_active_data_buffer(self):
        return self.raw_data_buffer

    def get_rosette_display_channel_specs(self):
        return [
            {
                "key": ("rs", "PZT1", 1, 8),
                "label": "PZT1_RS1",
                "sample_indices": [0],
                "color_slot": 0,
                "stream": "rs",
            },
            {
                "key": ("rs", "PZT1", 2, 9),
                "label": "PZT1_RS2",
                "sample_indices": [1],
                "color_slot": 1,
                "stream": "rs",
            },
        ]

    def log_status(self, message):
        self.logged.append(message)


class FakeComboBox:
    def __init__(self, text):
        self._text = text

    def currentText(self):
        return self._text


class FakeSpinBox:
    def __init__(self, value):
        self._value = value

    def value(self):
        return self._value


class FakePlotWidget:
    def __init__(self):
        self.auto_range_calls = []
        self.y_range = None

    def enableAutoRange(self, axis=None, enable=True):
        self.auto_range_calls.append((axis, enable))

    def setYRange(self, low, high, padding=0.0):
        self.y_range = (low, high, padding)


class RosettePlottingTests(unittest.TestCase):
    def test_trailing_moving_average_preserves_length(self):
        values = np.array([1.0, 2.0, 3.0, 4.0])

        smoothed = ADCPlottingMixin._apply_trailing_moving_average(values, 3)

        np.testing.assert_allclose(smoothed, [1.0, 1.5, 2.0, 3.0])

    def test_rosette_baseline_uses_latest_samples_per_channel(self):
        harness = RosettePlottingHarness()

        self.assertTrue(harness.capture_current_rosette_plot_baselines(sample_count=2))

        self.assertEqual(harness.rosette_plot_baselines[("rs", "PZT1", 1, 8)], 35.0)
        self.assertEqual(harness.rosette_plot_baselines[("rs", "PZT1", 2, 9)], 350.0)
        self.assertTrue(any("Zeroed Rosette signals" in message for message in harness.logged))

    def test_rosette_y_axis_adaptive_uses_auto_range(self):
        harness = RosettePlottingHarness()
        harness.rosette_plot_widget = FakePlotWidget()
        harness.rosette_yaxis_range_combo = FakeComboBox("Adaptive")

        harness.apply_rosette_y_axis_range()

        self.assertEqual(harness.rosette_plot_widget.auto_range_calls, [('y', True)])
        self.assertIsNone(harness.rosette_plot_widget.y_range)

    def test_rosette_y_axis_fixed_uses_configured_min_max(self):
        harness = RosettePlottingHarness()
        harness.rosette_plot_widget = FakePlotWidget()
        harness.rosette_yaxis_range_combo = FakeComboBox("Fixed")
        harness.rosette_yaxis_min_spin = FakeSpinBox(100.0)
        harness.rosette_yaxis_max_spin = FakeSpinBox(2500.0)

        harness.apply_rosette_y_axis_range()

        self.assertEqual(harness.rosette_plot_widget.auto_range_calls, [('y', False)])
        self.assertEqual(harness.rosette_plot_widget.y_range, (100.0, 2500.0, 0.0))


if __name__ == "__main__":
    unittest.main()
