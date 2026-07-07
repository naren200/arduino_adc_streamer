import unittest

import numpy as np

from data_processing.adc_filter_engine import (
    ADCFilterEngine,
    SCIPY_FILTERS_AVAILABLE,
    build_default_filter_settings,
)


class ADCFilterEngineTests(unittest.TestCase):
    def test_default_filter_settings_shape(self):
        settings = build_default_filter_settings()

        self.assertIn("enabled", settings)
        self.assertIn("main_type", settings)
        self.assertIn("notches", settings)
        self.assertEqual(len(settings["notches"]), 3)

    def test_validate_settings_rejects_invalid_bandpass(self):
        engine = ADCFilterEngine()
        settings = build_default_filter_settings()
        settings["main_type"] = "bandpass"
        settings["low_cutoff_hz"] = 500.0
        settings["high_cutoff_hz"] = 100.0
        settings["notches"] = []

        valid, error = engine.validate_settings(settings, channel_fs_hz=2000.0)

        self.assertFalse(valid)
        self.assertIn("low cutoff", error.lower())

    @unittest.skipUnless(SCIPY_FILTERS_AVAILABLE, "SciPy not available")
    def test_build_runtime_plan_and_filter_block(self):
        engine = ADCFilterEngine()
        settings = build_default_filter_settings()
        settings["main_type"] = "none"
        settings["notches"] = []

        plan = engine.build_runtime_plan(settings, total_fs_hz=1000.0, channels=[0, 1], repeat_count=1)
        block = np.array([[1.0, 2.0], [3.0, 4.0]], dtype=np.float32)

        filtered = engine.filter_block(plan, block)

        self.assertEqual(set(plan.keys()), {0, 1})
        np.testing.assert_allclose(filtered, block)

    def test_estimate_channel_sample_rates_uses_sweep_timestamps(self):
        engine = ADCFilterEngine()
        sweep_rate_hz = 1488.0
        sweep_timestamps = np.arange(16, dtype=np.float64) / sweep_rate_hz

        rates = engine.estimate_channel_sample_rates(
            total_fs_hz=83333.33,
            channels=[0, 1, 2, 3, 4],
            repeat_count=1,
            sweep_timestamps_sec=sweep_timestamps,
        )

        self.assertEqual(set(rates.keys()), {0, 1, 2, 3, 4})
        self.assertAlmostEqual(rates[0], sweep_rate_hz, delta=5.0)

    def test_channel_grouping_merges_reused_pins(self):
        # Documents the pre-fix defect: array-PZT sweeps reuse physical pins for
        # different sensors, so grouping by channel number collapses distinct signals.
        engine = ADCFilterEngine()
        index_map = engine.build_channel_index_map([5, 6, 5], repeat_count=1)

        self.assertEqual(set(index_map.keys()), {5, 6})
        np.testing.assert_array_equal(index_map[5], np.array([0, 2]))

    def test_estimate_channel_sample_rates_with_stream_index_map_is_uniform(self):
        engine = ADCFilterEngine()
        total_fs_hz = 1500.0
        samples_per_sweep = 3
        sweep_rate_hz = total_fs_hz / samples_per_sweep
        sweep_timestamps = np.arange(20, dtype=np.float64) / sweep_rate_hz

        # Three named signals, one column each: every signal is sampled once per sweep,
        # so all three must report the same (sweep) rate regardless of their column.
        index_map = {
            "PZT3_B": np.array([0], dtype=np.int32),
            "PZT6_B": np.array([1], dtype=np.int32),
            "PZT3_T": np.array([2], dtype=np.int32),
        }
        rates = engine.estimate_channel_sample_rates(
            total_fs_hz,
            channels=[],
            repeat_count=1,
            sweep_timestamps_sec=sweep_timestamps,
            index_map=index_map,
        )

        self.assertEqual(set(rates.keys()), {"PZT3_B", "PZT6_B", "PZT3_T"})
        for rate in rates.values():
            self.assertAlmostEqual(rate, sweep_rate_hz, delta=5.0)

    def test_estimate_channel_sample_rates_stream_map_fallback_without_timestamps(self):
        engine = ADCFilterEngine()
        index_map = {
            "PZT3_B": np.array([0], dtype=np.int32),
            "PZT6_B": np.array([1], dtype=np.int32),
            "PZT3_T": np.array([2], dtype=np.int32),
        }
        rates = engine.estimate_channel_sample_rates(
            total_fs_hz=1500.0,
            channels=[],
            repeat_count=1,
            sweep_timestamps_sec=None,
            index_map=index_map,
        )

        # Fallback splits total rate across the three streams evenly (1500 / 3).
        self.assertEqual(set(rates.keys()), {"PZT3_B", "PZT6_B", "PZT3_T"})
        for rate in rates.values():
            self.assertAlmostEqual(rate, 500.0, delta=1e-6)

    @unittest.skipUnless(SCIPY_FILTERS_AVAILABLE, "SciPy not available")
    def test_build_runtime_plan_uses_supplied_index_map(self):
        engine = ADCFilterEngine()
        settings = build_default_filter_settings()
        settings["main_type"] = "none"
        settings["notches"] = []

        index_map = {
            "A": np.array([0], dtype=np.int32),
            "B": np.array([2], dtype=np.int32),
        }
        plan = engine.build_runtime_plan(
            settings,
            total_fs_hz=1500.0,
            channels=[],
            repeat_count=1,
            sweep_timestamps_sec=np.arange(10, dtype=np.float64) / 500.0,
            index_map=index_map,
        )

        self.assertEqual(set(plan.keys()), {"A", "B"})
        np.testing.assert_array_equal(plan["A"].indices, np.array([0]))
        np.testing.assert_array_equal(plan["B"].indices, np.array([2]))

    @unittest.skipUnless(SCIPY_FILTERS_AVAILABLE, "SciPy not available")
    def test_filter_signal_preserves_constant_level_without_zero_drop(self):
        engine = ADCFilterEngine()
        settings = build_default_filter_settings()
        settings["main_type"] = "lowpass"
        settings["low_cutoff_hz"] = 50.0
        settings["notches"] = []
        samples = np.full(64, 1.65, dtype=np.float64)

        filtered = engine.filter_signal(settings, samples, channel_fs_hz=1500.0)

        self.assertAlmostEqual(float(filtered[0]), 1.65, places=3)


if __name__ == "__main__":
    unittest.main()
