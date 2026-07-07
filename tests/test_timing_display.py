import unittest

import numpy as np

from data_processing.timing_display import TimingDisplayMixin


class FakeLabel:
    def __init__(self):
        self.text = None
        self.visible = True

    def setText(self, text):
        self.text = text

    def setVisible(self, value):
        self.visible = bool(value)


class TimingHarness(TimingDisplayMixin):
    def __init__(self):
        self.config = {
            'cf_farads': 100e-9,
            'rb_ohms': 1000.0,
            'rk_ohms': 500.0,
        }
        self.device_mode = '555'
        self.charge_time_label = FakeLabel()
        self.discharge_time_label = FakeLabel()


class AdcTimingHarness(TimingDisplayMixin):
    def __init__(self, num_signals, *, sweep_period_s=None, sweep_count=0, samples_per_sweep=0):
        self._num_signals = num_signals
        self.device_mode = 'adc'
        self.is_full_view = True
        self.sweep_count = sweep_count
        self.samples_per_sweep = samples_per_sweep
        # Evenly spaced sweep timestamps drive the measured (actual) rate/overhead.
        if sweep_period_s and sweep_count > 1:
            self.sweep_timestamps = np.arange(sweep_count, dtype=np.float64) * sweep_period_s
        else:
            self.sweep_timestamps = np.empty(0, dtype=np.float64)
        self.per_channel_rate_label = FakeLabel()
        self.total_rate_label = FakeLabel()
        self.between_samples_label = FakeLabel()
        self.block_gap_label = FakeLabel()
        self.sweep_overhead_label = FakeLabel()
        self._timing_state = self._create_timing_state()

    def get_display_channel_specs(self):
        return [{'label': f'S{i}', 'sample_indices': [i]} for i in range(self._num_signals)]

    def log_status(self, message):
        pass


class TimingDisplayTests(unittest.TestCase):
    def test_shows_actual_rate_and_total_overhead(self):
        # 15 signals at 47 µs each = 705 µs of ADC time; measured sweep period 1100 µs
        # -> actual per-channel rate ~909 Hz and ~395 µs total overhead.
        harness = AdcTimingHarness(
            num_signals=15,
            sweep_period_s=0.0011,
            sweep_count=101,
            samples_per_sweep=15,
        )
        harness.timing_state.arduino_sample_times.append(47.0)

        harness.update_timing_display()

        # Total (theoretical) conversion rate is unchanged.
        self.assertEqual(harness.total_rate_label.text, '21276.60 Hz')
        # Per-channel label now reflects the measured sweep rate, not total / 15 (1418 Hz).
        self.assertEqual(harness.per_channel_rate_label.text, '909.09 Hz')
        # Overhead is the wall-clock period minus pure ADC time (1100 - 705 = 395 µs).
        self.assertEqual(harness.sweep_overhead_label.text, '0.40 ms')

    def test_actual_rate_blank_without_enough_sweeps(self):
        harness = AdcTimingHarness(num_signals=15, sweep_count=0, samples_per_sweep=15)
        harness.timing_state.arduino_sample_times.append(47.0)

        harness.update_timing_display()

        self.assertEqual(harness.per_channel_rate_label.text, '- Hz')
        self.assertEqual(harness.sweep_overhead_label.text, '- ms')

    def test_format_time_auto_uses_scaled_units(self):
        harness = TimingHarness()
        self.assertEqual(harness._format_time_auto(0.0000005), '0.50 \u00b5s')
        self.assertEqual(harness._format_time_auto(0.05), '50.00 ms')
        self.assertEqual(harness._format_time_auto(1.25), '1.2500 s')

    def test_update_555_timing_readouts_formats_charge_and_discharge(self):
        harness = TimingHarness()

        harness.update_555_timing_readouts({1: 1000.0, 3: 2000.0})

        self.assertTrue(harness.charge_time_label.visible)
        self.assertTrue(harness.discharge_time_label.visible)
        self.assertEqual(
            harness.charge_time_label.text,
            'Charge time: Ch1: 173.29 \u00b5s | Ch3: 242.60 \u00b5s',
        )
        self.assertEqual(harness.discharge_time_label.text, 'Discharge time: 69.31 \u00b5s')


if __name__ == '__main__':
    unittest.main()
