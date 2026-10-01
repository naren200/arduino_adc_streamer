import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from config.adc_config_state import ADCConfigurationState
from constants.force import X_FORCE_SENSOR_TO_NEWTON, Z_FORCE_SENSOR_TO_NEWTON
from constants.pzt_force import PZT_FORCE_DEFAULT_SETTINGS
from data_processing.adc_mux_timing import calculate_adc_mux_timing_for_acquisition
from data_processing.analysis_workbench import (
    AnalysisSourceSnapshot,
    _build_offline_stream_index_map,
    _expanding_median,
    _load_filtered_snapshot,
    _owner_analysis_timing_metadata,
    build_force_based_shear_normal_traces,
    build_in_memory_snapshot,
    build_overlay_traces,
    build_snapshot_from_archive,
    counts_to_volts,
    integrate_voltage_series_causal_median,
    load_exported_csv_snapshot,
    prepare_analysis_data,
    reorder_circular_capture,
    resolve_analysis_pzt_mux_leak_dt_s,
)
from core.piezo_engine.normal_force_calculator import NormalForceCalculator
from core.piezo_engine.force_integrator import (
    PztChannelPhysicalParams,
    PztForceChannelIntegrator,
    calculate_pzt_force_from_settings,
    calculate_pzt_force_from_voltage,
    compute_pzt_force_rate_series,
    estimate_pzt_quiet_baseline,
    pzt_capacitance_to_farads,
)
from core.piezo_engine.shear_detector import ShearDetector


class OfflineStreamIndexMapTests(unittest.TestCase):
    def test_distinct_signal_names_stay_independent(self):
        # Array-PZT exports reuse physical channel numbers, so column identity must come
        # from the exported signal name, not the channel list.
        config = {"exported_signal_columns": ["PZT3_B", "PZT6_B", "PZT3_T"]}
        index_map = _build_offline_stream_index_map(config, samples_per_sweep=3)

        self.assertEqual(set(index_map.keys()), {"PZT3_B", "PZT6_B", "PZT3_T"})
        np.testing.assert_array_equal(index_map["PZT3_B"], np.array([0]))
        np.testing.assert_array_equal(index_map["PZT6_B"], np.array([1]))
        np.testing.assert_array_equal(index_map["PZT3_T"], np.array([2]))

    def test_repeated_names_group_together(self):
        # Non-array oversampling produces repeated column names that should share a stream.
        config = {"exported_signal_columns": ["CH5", "CH5", "CH6"]}
        index_map = _build_offline_stream_index_map(config, samples_per_sweep=3)

        np.testing.assert_array_equal(index_map["CH5"], np.array([0, 1]))
        np.testing.assert_array_equal(index_map["CH6"], np.array([2]))

    def test_returns_none_when_names_missing_or_mismatched(self):
        self.assertIsNone(_build_offline_stream_index_map({}, samples_per_sweep=3))
        self.assertIsNone(
            _build_offline_stream_index_map({"exported_signal_columns": ["A", "B"]}, samples_per_sweep=3)
        )


class AnalysisWorkbenchTests(unittest.TestCase):
    def _archive_owner(self, archive_sweeps, archive_timestamps):
        owner = SimpleNamespace(
            buffer_lock=threading.Lock(),
            raw_data_buffer=np.asarray([[1, 2], [3, 4]], dtype=np.float32),
            sweep_timestamps_buffer=np.asarray([0.0, 0.01], dtype=np.float64),
            sweep_count=2,
            buffer_write_index=2,
            MAX_SWEEPS_BUFFER=2,
            config=ADCConfigurationState(channels=[1, 2], repeat=1, sample_rate=200),
            force_state=SimpleNamespace(data=[]),
            finalize_calls=0,
        )
        owner.get_display_channel_specs = lambda: [
            {"label": "CH1", "sample_indices": [0]},
            {"label": "CH2", "sample_indices": [1]},
        ]
        owner.get_rosette_display_channel_specs = lambda: []

        def finalize():
            owner.finalize_calls += 1

        owner._finalize_archive_if_active = finalize
        owner.load_archive_data = lambda: (archive_sweeps, archive_timestamps)
        return owner

    def test_build_snapshot_from_archive_uses_complete_archive(self):
        owner = self._archive_owner(
            [[10, 11], [20, 21], [30, 31]],
            [0.0, 0.01, 0.02],
        )

        snapshot = build_snapshot_from_archive(owner)

        self.assertEqual(owner.finalize_calls, 1)
        self.assertEqual(snapshot.source_id, "archive")
        self.assertEqual(snapshot.metadata["source"], "archive")
        # build_snapshot_from_archive blip-filters PZT voltage columns via
        # _load_filtered_snapshot, so the median-of-3 filter applies to these
        # unlabeled fallback columns: the last row's median of (10, 20, 30) /
        # (11, 21, 31) is (20, 21), not a raw pass-through.
        np.testing.assert_array_equal(
            snapshot.data,
            np.asarray([[10, 11], [20, 21], [20, 21]], dtype=np.float32),
        )
        np.testing.assert_allclose(snapshot.timestamps_s, [0.0, 0.01, 0.02])

    def test_build_snapshot_from_archive_falls_back_to_ring_buffer(self):
        owner = self._archive_owner([], [])

        snapshot = build_snapshot_from_archive(owner)

        self.assertEqual(owner.finalize_calls, 1)
        self.assertEqual(snapshot.source_id, "in_memory")
        np.testing.assert_array_equal(snapshot.data, owner.raw_data_buffer)

    def test_causal_median_integration_matches_expanding_baseline(self):
        # window=2 -> index 0 has no full window and is dropped entirely;
        # only indices 1-3 (each backed by a genuine 2-sample window) remain.
        result = integrate_voltage_series_causal_median(
            {"C": np.asarray([0.0, 0.0, 10.0, 10.0])},
            integration_window_samples=2,
        )

        np.testing.assert_allclose(result["C"], [0.0, 10.0, 15.0])

    def test_causal_median_integration_average_mode_divides_by_actual_window_fill(self):
        # Same series/window as the sum-mode test above: window=2, so index 0
        # (partial 1-sample window) is dropped from both outputs, and every
        # remaining index has a full 2-sample window.
        sum_result = integrate_voltage_series_causal_median(
            {"C": np.asarray([0.0, 0.0, 10.0, 10.0])},
            integration_window_samples=2,
        )
        avg_result = integrate_voltage_series_causal_median(
            {"C": np.asarray([0.0, 0.0, 10.0, 10.0])},
            integration_window_samples=2,
            mode="average",
        )

        # index 1: window-fill count 2 -> sum/2
        # index 2: window-fill count 2 -> sum/2
        # index 3: window-fill count 2 -> sum/2
        expected = np.asarray(sum_result["C"]) / 2.0
        np.testing.assert_allclose(avg_result["C"], expected)
        # Sanity: average never exceeds the peak per-sample deviation the way
        # a growing sum would over a longer window.
        self.assertTrue(np.all(np.abs(avg_result["C"]) <= 10.0 + 1e-9))

    def test_causal_median_integration_drops_leading_samples_shorter_than_window(self):
        # A whole capture shorter than the window has no genuine full window
        # anywhere -- output must be empty, not partial-window values.
        result = integrate_voltage_series_causal_median(
            {"C": np.asarray([1.0, 2.0])},
            integration_window_samples=5,
        )
        self.assertEqual(result["C"].size, 0)

    def test_causal_median_integration_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            integrate_voltage_series_causal_median(
                {"C": np.asarray([0.0, 1.0])},
                integration_window_samples=2,
                mode="bogus",
            )

    def test_reorder_circular_capture_returns_oldest_to_newest(self):
        data = np.asarray(
            [
                [40, 41],
                [50, 51],
                [10, 11],
                [20, 21],
                [30, 31],
            ],
            dtype=np.float32,
        )
        timestamps = np.asarray([4, 5, 1, 2, 3], dtype=np.float64)

        ordered, ordered_timestamps = reorder_circular_capture(
            data,
            timestamps,
            sweep_count=7,
            write_index=2,
            max_sweeps=5,
        )

        np.testing.assert_array_equal(
            ordered,
            np.asarray([[10, 11], [20, 21], [30, 31], [40, 41], [50, 51]], dtype=np.float32),
        )
        np.testing.assert_array_equal(ordered_timestamps, np.asarray([1, 2, 3, 4, 5], dtype=np.float64))

    def test_build_in_memory_snapshot_accepts_typed_config_state(self):
        owner = SimpleNamespace(
            buffer_lock=threading.Lock(),
            raw_data_buffer=np.asarray([[1, 2], [3, 4]], dtype=np.float32),
            sweep_timestamps_buffer=np.asarray([0.0, 0.01], dtype=np.float64),
            sweep_count=2,
            buffer_write_index=2,
            MAX_SWEEPS_BUFFER=10,
            config=ADCConfigurationState(channels=[1, 2], repeat=1, sample_rate=200),
            force_state=SimpleNamespace(data=[]),
        )
        owner.get_display_channel_specs = lambda: [
            {"label": "PZT6_B", "sample_indices": [0]},
            {"label": "PZT6_C", "sample_indices": [1]},
        ]
        owner.get_rosette_display_channel_specs = lambda: []

        snapshot = build_in_memory_snapshot(owner)

        self.assertEqual(snapshot.channel_labels, ["PZT6_B", "PZT6_C"])
        self.assertEqual(snapshot.metadata["configuration"]["channels"], [1, 2])
        np.testing.assert_array_equal(snapshot.data, owner.raw_data_buffer)

    def test_build_in_memory_snapshot_converts_force_counts_to_newtons(self):
        owner = SimpleNamespace(
            buffer_lock=threading.Lock(),
            raw_data_buffer=np.asarray([[1, 2], [3, 4]], dtype=np.float32),
            sweep_timestamps_buffer=np.asarray([0.0, 0.01], dtype=np.float64),
            sweep_count=2,
            buffer_write_index=2,
            MAX_SWEEPS_BUFFER=10,
            config=ADCConfigurationState(channels=[1, 2], repeat=1, sample_rate=200),
            force_state=SimpleNamespace(
                data=[
                    (0.0, 2.0 * X_FORCE_SENSOR_TO_NEWTON, 3.0 * Z_FORCE_SENSOR_TO_NEWTON),
                    (0.1, 4.0 * X_FORCE_SENSOR_TO_NEWTON, 5.0 * Z_FORCE_SENSOR_TO_NEWTON),
                ]
            ),
        )
        owner.get_display_channel_specs = lambda: []
        owner.get_rosette_display_channel_specs = lambda: []

        snapshot = build_in_memory_snapshot(owner)

        np.testing.assert_allclose(snapshot.force_x_n, [2.0, 4.0])
        np.testing.assert_allclose(snapshot.force_z_n, [3.0, 5.0])

    def test_build_in_memory_snapshot_hides_unlabeled_buffer_columns_when_specs_exist(self):
        owner = SimpleNamespace(
            buffer_lock=threading.Lock(),
            raw_data_buffer=np.asarray([[10, 11, 12, 13], [20, 21, 22, 23]], dtype=np.float32),
            sweep_timestamps_buffer=np.asarray([0.0, 0.01], dtype=np.float64),
            sweep_count=2,
            buffer_write_index=2,
            MAX_SWEEPS_BUFFER=10,
            config=ADCConfigurationState(channels=[1, 2], repeat=1, sample_rate=400),
            force_state=SimpleNamespace(data=[]),
        )
        owner.get_display_channel_specs = lambda: [
            {"label": "PZT3_B", "sample_indices": [0]},
            {"label": "PZT3_L", "sample_indices": [2]},
        ]
        owner.get_rosette_display_channel_specs = lambda: []

        snapshot = build_in_memory_snapshot(owner)
        prepared = prepare_analysis_data(snapshot, vref_voltage=3.3)

        self.assertEqual(snapshot.channel_labels, ["PZT3_B", "PZT3_L"])
        self.assertEqual(snapshot.channel_indices, [0, 2])
        self.assertEqual([trace.label for trace in prepared.traces], ["PZT3_B", "PZT3_L"])

    def test_prepare_analysis_data_converts_adc_counts_to_volts(self):
        snapshot = AnalysisSourceSnapshot(
            data=np.asarray([[0, 2000, 474.6]], dtype=np.float32),
            timestamps_s=np.asarray([0.0], dtype=np.float64),
            channel_labels=["PZT6_B", "PZT6_C", "PZT6_RS1"],
            metadata={"configuration": {"channels": [1, 2, 3], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=1000.0,
        )

        prepared = prepare_analysis_data(snapshot, vref_voltage=3.3)
        values_by_label = {trace.label: trace.y for trace in prepared.traces}

        np.testing.assert_allclose(values_by_label["PZT6_B"], [0.0])
        np.testing.assert_allclose(values_by_label["PZT6_C"], [3.3 * 2000.0 / 4095.0])
        np.testing.assert_allclose(values_by_label["PZT6_RS1"], [474.6])

    def test_prepare_analysis_data_median3_filters_isolated_pzt_spike(self):
        samples = np.asarray([100, 100, 100, 4000, 100, 100, 100], dtype=np.float32)
        rs_samples = np.asarray([500, 500, 500, 9000, 500, 500, 500], dtype=np.float32)
        snapshot = AnalysisSourceSnapshot(
            data=np.stack([samples, rs_samples], axis=1),
            timestamps_s=np.arange(7, dtype=np.float64) * 0.001,
            channel_labels=["PZT6_B", "PZT6_RS1"],
            metadata={"configuration": {"channels": [1, 2], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=1000.0,
        )
        snapshot = _load_filtered_snapshot(snapshot)

        prepared = prepare_analysis_data(snapshot, vref_voltage=3.3)
        values_by_label = {trace.label: trace.y for trace in prepared.traces}

        # PZT voltage column: isolated spike is median-of-3 rejected.
        np.testing.assert_allclose(
            values_by_label["PZT6_B"], counts_to_volts(np.full(7, 100.0), 3.3)
        )
        # Resistance-like column is excluded from the filter and passes through raw.
        np.testing.assert_allclose(values_by_label["PZT6_RS1"], rs_samples)

    def test_calculate_pzt_force_uses_leakage_model(self):
        force = calculate_pzt_force_from_voltage(
            np.asarray([1.0, 1.5], dtype=np.float64),
            np.asarray([0.0, 0.001], dtype=np.float64),
            capacitance_f=1e-9,
            rleak_ohm=1e9,
            d33_c_per_n=600e-12,
            noise_threshold_v=0.1,
        )

        centered = np.asarray([-0.25, 0.25], dtype=np.float64)
        expected_second = (1e-9 / 600e-12) * (centered[1] - (np.exp(-0.001 / 1.0) * centered[0]))
        np.testing.assert_allclose(force, [0.0, expected_second])

    def test_calculate_pzt_force_uses_mux_leak_exposure_when_supplied(self):
        force = calculate_pzt_force_from_voltage(
            np.asarray([1.0, 1.5], dtype=np.float64),
            np.asarray([0.0, 0.320], dtype=np.float64),
            capacitance_f=1e-9,
            rleak_ohm=1e9,
            d33_c_per_n=600e-12,
            noise_threshold_v=0.1,
            leak_dt_s=0.030,
        )

        centered = np.asarray([-0.25, 0.25], dtype=np.float64)
        expected_second = (1e-9 / 600e-12) * (centered[1] - (np.exp(-0.030 / 1.0) * centered[0]))
        np.testing.assert_allclose(force, [0.0, expected_second])

    def test_calculate_pzt_force_corrects_new_charge_for_pre_sample_decay(self):
        force = calculate_pzt_force_from_voltage(
            np.asarray([1.0, 1.5], dtype=np.float64),
            np.asarray([0.0, 0.320], dtype=np.float64),
            capacitance_f=1e-9,
            rleak_ohm=1e9,
            d33_c_per_n=600e-12,
            noise_threshold_v=0.1,
            leak_dt_s=0.030,
            pre_sample_decay_dt_s=20.80e-6,
        )

        centered = np.asarray([-0.25, 0.25], dtype=np.float64)
        expected_second = (1e-9 / 600e-12) * np.exp(20.80e-6 / 1.0) * (
            centered[1] - (np.exp(-0.030 / 1.0) * centered[0])
        )
        np.testing.assert_allclose(force, [0.0, expected_second])

    def test_calculated_pzt_force_zeroes_after_bipolar_event(self):
        force = calculate_pzt_force_from_voltage(
            np.asarray([0.0, 1.0, -1.0, 0.0], dtype=np.float64),
            np.asarray([0.0, 0.001, 0.002, 0.003], dtype=np.float64),
            capacitance_f=1e-9,
            rleak_ohm=1e9,
            d33_c_per_n=600e-12,
            noise_threshold_v=0.2,
        )

        self.assertAlmostEqual(float(force[-1]), 0.0)

    def test_calculated_pzt_force_ignores_voltage_below_noise_threshold(self):
        force = calculate_pzt_force_from_voltage(
            np.asarray([0.0, 0.05, -0.05, 0.0], dtype=np.float64),
            np.asarray([0.0, 0.001, 0.002, 0.003], dtype=np.float64),
            capacitance_f=1e-9,
            rleak_ohm=1e9,
            d33_c_per_n=600e-12,
            noise_threshold_v=0.1,
        )

        np.testing.assert_allclose(force, np.zeros(4))

    def test_estimate_pzt_quiet_baseline_uses_percentile_threshold_with_mad_diagnostics(self):
        estimate = estimate_pzt_quiet_baseline(
            np.asarray([1.00, 1.01, 0.99, 1.50], dtype=np.float64),
            np.asarray([0.0, 0.1, 0.2, 2.0], dtype=np.float64),
            quiet_duration_s=0.25,
            noise_sigma_multiplier=5.0,
        )

        self.assertAlmostEqual(estimate.vmid_v, 1.0)
        self.assertAlmostEqual(estimate.mad_v, 0.01)
        self.assertAlmostEqual(estimate.noise_threshold_v, 0.01)
        self.assertAlmostEqual(estimate.sigma_v, 0.01 / 5.0)
        self.assertEqual(estimate.sample_count, 3)

    def test_estimate_pzt_quiet_baseline_uses_same_percentile_method_when_mad_is_zero(self):
        estimate = estimate_pzt_quiet_baseline(
            np.asarray([1.0, 1.0, 1.0, 1.0, 1.002], dtype=np.float64),
            np.asarray([0.0, 0.1, 0.2, 0.3, 0.4], dtype=np.float64),
            quiet_duration_s=1.0,
            noise_sigma_multiplier=5.0,
        )

        self.assertAlmostEqual(estimate.vmid_v, 1.0)
        self.assertAlmostEqual(estimate.mad_v, 0.0)
        self.assertGreater(estimate.noise_threshold_v, 0.0)
        self.assertLessEqual(estimate.noise_threshold_v, 0.002)

    def test_calculated_pzt_force_uses_explicit_vmid_and_threshold(self):
        force = calculate_pzt_force_from_voltage(
            np.asarray([1.0, 1.2], dtype=np.float64),
            np.asarray([0.0, 0.001], dtype=np.float64),
            capacitance_f=1e-9,
            rleak_ohm=1e9,
            d33_c_per_n=600e-12,
            noise_threshold_v=0.05,
            vmid_v=1.0,
        )

        expected_second = (1e-9 / 600e-12) * 0.2
        np.testing.assert_allclose(force, [0.0, expected_second])

    def test_prepare_analysis_data_reports_pzt_timing_failure_without_raising(self):
        # "PZT Channel Force" (the standalone per-channel display) is gone --
        # only MUX leak-timing resolution remains here, feeding Shear/Normal
        # Force. An unavailable auto-mode timing source must surface as a
        # status message, not raise out of prepare_analysis_data.
        snapshot = AnalysisSourceSnapshot(
            data=np.asarray([[1000], [1200]], dtype=np.float32),
            timestamps_s=np.asarray([0.0, 0.01], dtype=np.float64),
            channel_labels=["PZT6_C"],
            metadata={"configuration": {"channels": [1], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=200.0,
        )

        prepared = prepare_analysis_data(
            snapshot,
            visible_labels=["PZT6_C"],
            vref_voltage=3.3,
            pzt_force_settings={
                "capacitance_value": 1.0,
                "capacitance_unit": "nF",
                "rleak_ohm": 1e9,
                "d33_pc_per_n": 600.0,
                "noise_threshold_v": 0.01,
                "mux_timing_mode": "auto",
            },
        )

        self.assertIn("PZT force timing skipped", prepared.status)

    def test_resolve_analysis_pzt_mux_leak_dt_prefers_metadata_timing(self):
        snapshot = AnalysisSourceSnapshot(
            data=np.asarray([[1], [2]], dtype=np.float32),
            timestamps_s=np.asarray([0.0, 0.1], dtype=np.float64),
            channel_labels=["PZT6_C"],
            metadata={
                "configuration": {"channels": [1], "repeat_count": 1},
                "timing": {"arduino_sample_time_us": 30000.0},
            },
            source_id="unit",
            sample_rate_hz=10.0,
        )

        leak_dt, status = resolve_analysis_pzt_mux_leak_dt_s(snapshot, {"enabled": True, "mux_timing_mode": "auto"})

        self.assertAlmostEqual(leak_dt, 0.030)
        self.assertIn("arduino_sample_time_us", status)

    def test_resolve_analysis_pzt_mux_leak_dt_reads_block_timing_sidecar(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            csv_path = temp_path / "capture.csv"
            sidecar_path = temp_path / "timing.csv"
            csv_path.write_text("", encoding="utf-8")
            sidecar_path.write_text(
                "sample_count,samples_per_sweep,sweeps_in_block,avg_dt_us,block_start_us,block_end_us,mcu_gap_us\n"
                "10,5,2,30000,0,100000,200000\n"
                "10,5,2,31000,100000,200000,200000\n",
                encoding="utf-8",
            )
            snapshot = AnalysisSourceSnapshot(
                data=np.asarray([[1], [2]], dtype=np.float32),
                timestamps_s=np.asarray([0.0, 0.1], dtype=np.float64),
                channel_labels=["PZT6_C"],
                metadata={
                    "configuration": {"channels": [1], "repeat_count": 1},
                    "block_timing_csv": str(sidecar_path),
                },
                source_id=f"csv:{csv_path.resolve()}|json:{(temp_path / 'capture_metadata.json').resolve()}",
                sample_rate_hz=10.0,
            )

            leak_dt, status = resolve_analysis_pzt_mux_leak_dt_s(snapshot, {"enabled": True, "mux_timing_mode": "auto"})

        self.assertAlmostEqual(leak_dt, 0.0305)
        self.assertIn("block_timing_csv", status)

    def test_owner_timing_metadata_prefers_calculator_over_cached_average(self):
        # A calculator-capable owner's physical t_connected must win even when a
        # cached average sample time is also present (Part A of the natural-reset
        # plan: the cached-average block used to overwrite the correct value).
        owner = SimpleNamespace(
            current_mcu="Array_PZT_PZR1.7",
            config={"osr": 4, "gain": 1, "repeat": 4, "channels": [1], "use_ground": False, "buffer": 10},
            _cached_avg_sample_time_sec=45e-6,
        )
        calculated = calculate_adc_mux_timing_for_acquisition(owner.current_mcu, owner.config)

        result = _owner_analysis_timing_metadata(owner)

        self.assertEqual(result["pzt_mux_connected_time_s"], calculated.sensor_connected_s)
        self.assertEqual(result["pzt_mux_connected_time_source"], "adc_mux_timing.t_connected_s")

    def test_owner_timing_metadata_falls_back_to_cached_average_when_unsupported(self):
        owner = SimpleNamespace(
            current_mcu="Unsupported.1",
            config={},
            _cached_avg_sample_time_sec=45e-6,
        )

        result = _owner_analysis_timing_metadata(owner)

        self.assertEqual(result["pzt_mux_connected_time_s"], 45e-6)
        self.assertEqual(result["pzt_mux_connected_time_source"], "_cached_avg_sample_time_sec")

    def test_resolve_analysis_pzt_mux_leak_dt_uses_calculator_value_end_to_end(self):
        owner = SimpleNamespace(
            current_mcu="Array_PZT_PZR1.7",
            config={"osr": 4, "gain": 1, "repeat": 4, "channels": [1], "use_ground": False, "buffer": 10},
            _cached_avg_sample_time_sec=45e-6,
        )
        calculated = calculate_adc_mux_timing_for_acquisition(owner.current_mcu, owner.config)
        timing_metadata = _owner_analysis_timing_metadata(owner)
        snapshot = AnalysisSourceSnapshot(
            data=np.asarray([[1], [2]], dtype=np.float32),
            timestamps_s=np.asarray([0.0, 0.1], dtype=np.float64),
            channel_labels=["PZT6_C"],
            metadata={
                "configuration": {"channels": [1], "repeat_count": 1},
                "timing": timing_metadata,
            },
            source_id="unit",
            sample_rate_hz=10.0,
        )

        leak_dt, status = resolve_analysis_pzt_mux_leak_dt_s(snapshot, {"enabled": True, "mux_timing_mode": "auto"})

        self.assertAlmostEqual(leak_dt, calculated.sensor_connected_s)
        self.assertIn("adc_mux_timing.t_connected_s", status)

    def test_pzt_capacitance_units_convert_to_farads(self):
        self.assertAlmostEqual(pzt_capacitance_to_farads(10.0, "pF"), 10e-12)
        self.assertAlmostEqual(pzt_capacitance_to_farads(2.0, "nF"), 2e-9)
        self.assertAlmostEqual(pzt_capacitance_to_farads(3.0, "F"), 3.0)

    def test_pzt_force_settings_helper_uses_shared_defaults(self):
        self.assertEqual(PZT_FORCE_DEFAULT_SETTINGS["capacitance_value"], 150.0)
        self.assertEqual(PZT_FORCE_DEFAULT_SETTINGS["center_capacitance_value"], 150.0)
        self.assertEqual(PZT_FORCE_DEFAULT_SETTINGS["outer_capacitance_value"], 150.0)
        self.assertEqual(PZT_FORCE_DEFAULT_SETTINGS["capacitance_unit"], "pF")
        self.assertEqual(PZT_FORCE_DEFAULT_SETTINGS["rleak_ohm"], 1_000_000.0)
        self.assertEqual(PZT_FORCE_DEFAULT_SETTINGS["mux_timing_mode"], "auto")
        force = calculate_pzt_force_from_settings(
            np.asarray([1.0, 1.2], dtype=np.float64),
            np.asarray([0.0, 0.001], dtype=np.float64),
            {"enabled": True},
        )

        self.assertEqual(force.shape, (2,))

    def test_pzt_force_settings_selects_center_or_outer_capacitance(self):
        settings = {
            "center_capacitance_value": 2.0,
            "outer_capacitance_value": 1.0,
            "capacitance_unit": "nF",
            "rleak_ohm": 1e12,
            "d33_pc_per_n": 600.0,
            "noise_threshold_v": 0.0,
        }
        voltage = np.asarray([0.0, 0.1], dtype=np.float64)
        timestamps = np.asarray([0.0, 0.001], dtype=np.float64)

        center_force = calculate_pzt_force_from_settings(
            voltage, timestamps, settings, sensor_position="C", vmid_v=0.0
        )
        outer_force = calculate_pzt_force_from_settings(
            voltage, timestamps, settings, sensor_position="L", vmid_v=0.0
        )

        self.assertAlmostEqual(center_force[-1], outer_force[-1] * 2.0, places=12)

    def test_pzt_force_settings_legacy_capacitance_applies_to_both_positions(self):
        settings = {
            "capacitance_value": 1.0,
            "capacitance_unit": "nF",
            "rleak_ohm": 1e12,
            "d33_pc_per_n": 600.0,
            "noise_threshold_v": 0.0,
        }
        voltage = np.asarray([0.0, 0.1], dtype=np.float64)
        timestamps = np.asarray([0.0, 0.001], dtype=np.float64)

        center_force = calculate_pzt_force_from_settings(
            voltage, timestamps, settings, sensor_position="C", vmid_v=0.0
        )
        outer_force = calculate_pzt_force_from_settings(
            voltage, timestamps, settings, sensor_position="R", vmid_v=0.0
        )

        np.testing.assert_allclose(center_force, outer_force)

    def test_load_exported_csv_snapshot_accepts_matching_metadata_column_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            csv_path = temp_path / "capture.csv"
            metadata_path = temp_path / "capture_metadata.json"
            # A leading dummy row is dropped by the capture-start settle trim
            # (data_mod.capture_start_settle_sample_count) before this test's
            # assertions look at the remaining two real rows.
            csv_path.write_text(
                "Timestamp,CH1,CH2,Force_X_N,Force_Z_N\n"
                "00:00:00.000000,99,99,9.9,9.9\n"
                "00:00:00.010000,1,2,0.5,1.5\n"
                "00:00:00.020000,3,4,0.6,1.6\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                json.dumps(
                    {
                        "configuration": {"channels": [1, 2], "repeat_count": 1},
                        "capture_duration_seconds": 0.02,
                        "timing": {"arduino_sample_rate_hz": 2.0},
                    }
                ),
                encoding="utf-8",
            )

            snapshot = load_exported_csv_snapshot(csv_path, metadata_path)

            self.assertEqual(snapshot.channel_labels, ["CH1", "CH2"])
            self.assertEqual(snapshot.data.shape, (2, 2))
            np.testing.assert_allclose(snapshot.timestamps_s, [0.01, 0.02])
            np.testing.assert_allclose(snapshot.force_x_n, [0.5, 0.6])

    def test_load_exported_csv_snapshot_tolerates_legacy_col_placeholders_and_metadata_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            csv_path = temp_path / "legacy_array.csv"
            metadata_path = temp_path / "legacy_array_metadata.json"
            # A leading dummy row is dropped by the capture-start settle trim
            # (data_mod.capture_start_settle_sample_count) before this test's
            # assertions look at the remaining two real rows.
            csv_path.write_text(
                "Timestamp,PZT3_B,Col1,PZT3_L,Col3,PZT3_C,Col5,PZT3_R,Col7,PZT3_T,Col9,Force_X_N,Force_Z_N\n"
                "13:29:44.970000,9999.0,9999.0,9999.0,9999.0,9999.0,9999.0,9999.0,9999.0,9999.0,9999.0,0.0,0.0\n"
                "13:29:44.971321,2045.0,2046.0,2043.0,2047.0,2047.0,2047.0,2040.0,2046.0,2049.0,2047.0,0.0,0.0\n"
                "13:29:44.971931,2047.0,2046.0,2046.0,2049.0,2048.0,2048.0,2040.0,2046.0,2049.0,2047.0,0.0,0.0\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                json.dumps(
                    {
                        "configuration": {
                            "channels": [1, 2, 3, 4, 5],
                            "repeat_count": 4,
                            "buffer_total_samples": 20,
                        },
                        "capture_duration_seconds": 0.01,
                        "timing": {"arduino_sample_rate_hz": 2.0},
                    }
                ),
                encoding="utf-8",
            )

            snapshot = load_exported_csv_snapshot(csv_path, metadata_path)

            self.assertEqual(snapshot.channel_labels, ["PZT3_B", "PZT3_L", "PZT3_C", "PZT3_R", "PZT3_T"])
            self.assertEqual(snapshot.data.shape, (2, 5))
            self.assertIn("analysis_warnings", snapshot.metadata)
            self.assertIn("metadata expects 20 signal columns", snapshot.metadata["analysis_warnings"][0])

    def test_load_exported_csv_snapshot_converts_legacy_force_columns_to_newtons(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            csv_path = temp_path / "legacy_force.csv"
            metadata_path = temp_path / "legacy_force_metadata.json"
            # A leading dummy row is dropped by the capture-start settle trim
            # (data_mod.capture_start_settle_sample_count) before this test's
            # assertions look at the remaining two real rows.
            csv_path.write_text(
                "Timestamp,CH1,Force_X,Force_Z\n"
                "00:00:00.000000,0,0,0\n"
                f"00:00:00.010000,1,{2.0 * X_FORCE_SENSOR_TO_NEWTON},{3.0 * Z_FORCE_SENSOR_TO_NEWTON}\n"
                f"00:00:00.020000,2,{4.0 * X_FORCE_SENSOR_TO_NEWTON},{5.0 * Z_FORCE_SENSOR_TO_NEWTON}\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                json.dumps(
                    {
                        "configuration": {"channels": [1], "repeat_count": 1},
                        "capture_duration_seconds": 0.02,
                        "timing": {"arduino_sample_rate_hz": 2.0},
                    }
                ),
                encoding="utf-8",
            )

            snapshot = load_exported_csv_snapshot(csv_path, metadata_path)

            np.testing.assert_allclose(snapshot.force_x_n, [2.0, 4.0])
            np.testing.assert_allclose(snapshot.force_z_n, [3.0, 5.0])

    def test_load_exported_csv_snapshot_accepts_array_export_force_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            csv_path = temp_path / "array.csv"
            metadata_path = temp_path / "array_metadata.json"
            # A leading dummy row is dropped by the capture-start settle trim
            # (data_mod.capture_start_settle_sample_count) before this test's
            # assertions look at the remaining two real rows.
            csv_path.write_text(
                "PZT6_B,PZT6_L,PZT6_C,PZT6_R,PZT6_T,PZT6_RS1,PZT6_RS2,Force_X,Force_Z\n"
                "9999,9999,9999,9999,9999,9999,9999,0,0\n"
                "2046,2052,2039,2049,2044,474.6,455.42,0,0\n"
                "2044,2052,2038,2050,2044,474.6,455.42,0,0\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                json.dumps(
                    {
                        "configuration": {
                            "channels": [10, 11, 12, 13, 14],
                            "repeat_count": 1,
                            "buffer_total_samples": 7,
                        },
                        "capture_duration_seconds": 0.01,
                        "timing": {"arduino_sample_rate_hz": 2.0},
                    }
                ),
                encoding="utf-8",
            )

            snapshot = load_exported_csv_snapshot(csv_path, metadata_path)

            self.assertEqual(snapshot.channel_labels, [
                "PZT6_B",
                "PZT6_L",
                "PZT6_C",
                "PZT6_R",
                "PZT6_T",
                "PZT6_RS1",
                "PZT6_RS2",
            ])
            self.assertEqual(snapshot.data.shape, (2, 7))
            np.testing.assert_allclose(snapshot.force_x_n, [0.0, 0.0])

    def test_load_exported_csv_snapshot_filters_blip_before_settle_trim(self):
        """Regression test for the ordering bug: a blip planted right where
        the settle-trim boundary lands must still be removed by the blip
        filter, because filtering runs on the FULL raw capture first and the
        settle-trim only afterward. Under the old (buggy) order -- trim
        first, then filter the already-trimmed array -- this exact blip
        would land inside the causal median filter's own always-unfiltered
        first (window - 1) samples and survive, which is the bug the user
        actually saw in the Analysis tab's Raw Signal panel."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            csv_path = temp_path / "blip_at_trim_boundary.csv"
            metadata_path = temp_path / "blip_at_trim_boundary_metadata.json"
            # fs=3.0 -> settle_count = ceil(0.4 * 3.0) = 2, so rows 0-1 are
            # discarded and row 2 (the blip) becomes the new first row.
            # window=3 (default) needs one full window of real neighbors on
            # both sides to median-out row 2's blip -- rows 0,1,2 supply that
            # only if filtering sees the FULL 5-row array, not the trimmed one.
            csv_path.write_text(
                "Timestamp,PZT3_B,PZT3_L,PZT3_C,PZT3_R,PZT3_T,Force_X_N,Force_Z_N\n"
                "00:00:00.000000,2048,2048,2048,2048,2048,0.0,0.0\n"
                "00:00:00.333000,2048,2048,2048,2048,2048,0.0,0.0\n"
                "00:00:00.666000,9999,9999,9999,9999,9999,0.0,0.0\n"
                "00:00:01.000000,2048,2048,2048,2048,2048,0.0,0.0\n"
                "00:00:01.333000,2048,2048,2048,2048,2048,0.0,0.0\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                json.dumps(
                    {
                        "configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1},
                        "capture_duration_seconds": 1.333,
                        "timing": {"arduino_sample_rate_hz": 3.0},
                    }
                ),
                encoding="utf-8",
            )

            snapshot = load_exported_csv_snapshot(csv_path, metadata_path)

            self.assertEqual(snapshot.data.shape, (3, 5))
            self.assertTrue(np.all(snapshot.data < 3000.0), snapshot.data)

    def test_prepare_analysis_data_builds_requested_overlays(self):
        snapshot = AnalysisSourceSnapshot(
            data=np.asarray(
                [
                    [100, -100, 80, 40, -40],
                    [120, -120, 100, 50, -50],
                    [140, -140, 120, 60, -60],
                ],
                dtype=np.float32,
            ),
            timestamps_s=np.asarray([0.0, 0.01, 0.02], dtype=np.float64),
            channel_labels=["C", "L", "R", "T", "B"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=500.0,
        )

        prepared = prepare_analysis_data(
            snapshot,
            axis_mode="time_ms",
            overlay_flags={"shear": True, "normal": True, "integration": True},
            vref_voltage=3.3,
            integration_window_samples=1,
            hpf_cutoff_hz=0.0,
        )

        overlay_labels = {trace.label for trace in prepared.overlay_traces}
        self.assertIn("Shear L/R Jerk [V]", overlay_labels)
        self.assertIn("Shear T/B Jerk [V]", overlay_labels)
        self.assertIn("Normal Jerk [V]", overlay_labels)
        self.assertIn("Integrated C [V samples]", overlay_labels)

        direct_overlays = build_overlay_traces(
            snapshot,
            snapshot.data,
            axis_mode="samples",
            overlay_flags={"shear": True},
            vref_voltage=3.3,
            integration_window_samples=1,
            hpf_cutoff_hz=0.0,
        )
        self.assertEqual([trace.label for trace in direct_overlays], ["Shear L/R Jerk [V]", "Shear T/B Jerk [V]"])

    def test_baseline_removed_overlay_tracks_drift_and_keeps_pulses(self):
        # Slow drift (a level shift the causal expanding median gradually
        # catches up to, since it's a median over ALL past samples, not a
        # moving window) across all rows, plus one sharp pulse -- the
        # baseline-removed overlay should collapse the drifted region back
        # toward zero once the median has caught up, while still showing
        # the pulse clearly.
        # _expanding_median is a median over ALL past samples (no decay), so
        # it only "catches up" to a level shift once samples at the new
        # level outnumber everything seen before, including the warmup
        # preamble this helper prepends -- use a long-enough hold at the
        # new level, well before the pulse, for that crossover to happen.
        sample_count = 200
        pulse_index = 150
        counts = np.full((sample_count, 5), 1600.0, dtype=np.float32)
        counts[5:, :] = 1900.0  # level shift the expanding median must absorb
        counts[pulse_index, :] += 600.0

        timestamps_s = np.arange(sample_count, dtype=np.float64) * 0.01
        data = counts
        snapshot = AnalysisSourceSnapshot(
            data=data,
            timestamps_s=timestamps_s,
            channel_labels=["C", "L", "R", "T", "B"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=100.0,
        )

        overlays = build_overlay_traces(
            snapshot,
            snapshot.data,
            axis_mode="samples",
            overlay_flags={"shear": False, "normal": False, "integration": False, "baseline_removed": True},
            vref_voltage=3.3,
            integration_window_samples=1,
            hpf_cutoff_hz=0.0,
        )
        by_label = {trace.label: trace for trace in overlays}
        self.assertEqual(
            set(by_label),
            {f"{position} Baseline Removed [V]" for position in ["C", "L", "R", "T", "B"]},
        )

        center_trace = by_label["C Baseline Removed [V]"]
        # Once the expanding median has caught up with the level shift
        # (well before the pulse, after warmup), the drift is absorbed and
        # the residual sits near zero.
        settled_region = center_trace.y[100:110]
        self.assertTrue(np.all(np.abs(settled_region) < 0.05), settled_region)
        # The pulse itself is still clearly visible in the output.
        pulse_value = center_trace.y[pulse_index]
        self.assertGreater(abs(pulse_value), 0.3, center_trace.y)

    def test_baseline_removed_overlay_matches_integration_centered_array(self):
        # Single-source-of-truth check: the overlay's values must be the
        # exact same centered array integrate_voltage_series_causal_median
        # computes internally for the Shear/Normal Jerk path -- not a second,
        # independently recomputed _expanding_median call.
        data = np.asarray(
            [
                [200, 300, -300, 100, -100],
                [260, 500, -420, 140, -140],
                [260, 500, -420, 140, -140],
                [180, 260, -260, 80, -80],
            ],
            dtype=np.float32,
        )
        timestamps_s = np.asarray([0.0, 0.01, 0.02, 0.03], dtype=np.float64)
        snapshot = AnalysisSourceSnapshot(
            data=data,
            timestamps_s=timestamps_s,
            channel_labels=["C", "L", "R", "T", "B"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=100.0,
        )
        vref_voltage = 3.3

        overlays = build_overlay_traces(
            snapshot,
            snapshot.data,
            axis_mode="samples",
            overlay_flags={"baseline_removed": True},
            vref_voltage=vref_voltage,
            integration_window_samples=1,
            hpf_cutoff_hz=0.0,
        )
        by_label = {trace.label: trace for trace in overlays}

        volts_by_position = {
            position: counts_to_volts(data[:, index], vref_voltage)
            for index, position in enumerate(["C", "L", "R", "T", "B"])
        }
        _integrated, centered_by_position = integrate_voltage_series_causal_median(
            volts_by_position,
            integration_window_samples=1,
            sample_rate_hz=100.0,
            return_centered=True,
        )

        for position in ["C", "L", "R", "T", "B"]:
            np.testing.assert_array_equal(
                by_label[f"{position} Baseline Removed [V]"].y,
                centered_by_position[position],
            )

    def test_force_based_shear_normal_traces_match_reference_integration(self):
        # C, L, R, T, B counts across 4 rows; L/R carry an opposite-sign shear
        # component so the reference must actually exercise shear removal.
        data = np.asarray(
            [
                [200, 300, -300, 100, -100],
                [260, 500, -420, 140, -140],
                [260, 500, -420, 140, -140],
                [180, 260, -260, 80, -80],
            ],
            dtype=np.float32,
        )
        timestamps_s = np.asarray([0.0, 0.01, 0.02, 0.03], dtype=np.float64)
        snapshot = AnalysisSourceSnapshot(
            data=data,
            timestamps_s=timestamps_s,
            channel_labels=["C", "L", "R", "T", "B"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=100.0,
        )
        pzt_force_settings = {
            "enabled": True,
            "center_capacitance_value": 150.0,
            "outer_capacitance_value": 150.0,
            "capacitance_unit": "pF",
            "rleak_ohm": 1_000_000.0,
            "d33_pc_per_n": 600.0,
            "noise_threshold_n": 0.0,
        }
        vref_voltage = 3.3

        traces = build_force_based_shear_normal_traces(
            snapshot,
            snapshot.data,
            axis_mode="samples",
            overlay_flags={"shear_force": True, "normal_force": True},
            vref_voltage=vref_voltage,
            pzt_force_settings=pzt_force_settings,
        )
        by_label = {trace.label: trace for trace in traces}
        self.assertEqual(
            set(by_label), {"Shear Force L/R [N]", "Shear Force T/B [N]", "Normal Force [N]"}
        )

        # Hand-computed reference: per-channel, causally expanding-median
        # centered voltage -> stateless per-channel RC-charge force RATE
        # (own capacitance per position) -> ShearDetector/NormalForceCalculator
        # -> one independent PztForceChannelIntegrator per combined output
        # series. This must match the function under test exactly, since
        # it's meant to be the same computation, not an approximation of it.
        max_adc = float((2 ** 12) - 1)
        volts_by_position = {
            position: (data[:, index] / max_adc) * vref_voltage
            for index, position in enumerate(["C", "L", "R", "T", "B"])
        }

        def _channel_params(sensor_position):
            capacitance_f = pzt_capacitance_to_farads(
                pzt_force_settings["center_capacitance_value" if sensor_position == "C" else "outer_capacitance_value"],
                pzt_force_settings["capacitance_unit"],
            )
            return PztChannelPhysicalParams(
                capacitance_f=capacitance_f,
                rleak_ohm=pzt_force_settings["rleak_ohm"],
                d33_c_per_n=pzt_force_settings["d33_pc_per_n"] * 1e-12,
            )

        rate_by_position = {
            position: compute_pzt_force_rate_series(
                volts_by_position[position] - _expanding_median(volts_by_position[position]),
                snapshot.timestamps_s,
                _channel_params(position),
            )
            for position in ["C", "L", "R", "T", "B"]
        }
        # Shear is detected from a 6-sample causal trailing average of each
        # outer channel's raw dF (partial window for the first 5 samples),
        # but the smoothed shear removed is subtracted from the RAW
        # instantaneous dF at each position -- Normal Jerk stays raw.
        window = 6
        def _trailing_average(series):
            out = []
            for row in range(len(series)):
                start = max(0, row - window + 1)
                out.append(sum(series[start:row + 1]) / (row - start + 1))
            return out

        smoothed_rate_by_position = {
            position: _trailing_average(rate_by_position[position])
            for position in ["L", "R", "T", "B"]
        }

        detector = ShearDetector()
        calculator = NormalForceCalculator()
        normal_jerk_ref, shear_lr_jerk_ref, shear_tb_jerk_ref = [], [], []
        for row in range(len(snapshot.timestamps_s)):
            smoothed_values = {
                position: float(smoothed_rate_by_position[position][row])
                for position in ["L", "R", "T", "B"]
            }
            smoothed_values["C"] = float(rate_by_position["C"][row])
            shear = detector.detect(smoothed_values)
            residual = {
                position: float(rate_by_position[position][row]) - shear.strain_vector[position]
                for position in ["C", "L", "R", "T", "B"]
            }
            normal_jerk_ref.append(calculator.compute(residual).total_force)
            shear_lr_jerk_ref.append(shear.b_lr)
            shear_tb_jerk_ref.append(shear.b_tb)

        def _integrate(series, sensor_position):
            capacitance_f = pzt_capacitance_to_farads(
                pzt_force_settings["center_capacitance_value" if sensor_position == "C" else "outer_capacitance_value"],
                pzt_force_settings["capacitance_unit"],
            )
            integrator_out = []
            integrator = PztForceChannelIntegrator(
                capacitance_f=capacitance_f,
                rleak_ohm=pzt_force_settings["rleak_ohm"],
                d33_c_per_n=pzt_force_settings["d33_pc_per_n"] * 1e-12,
                noise_threshold_v=pzt_force_settings["noise_threshold_n"],
                accumulate_raw=True,
            )
            for row, value in enumerate(series):
                step = integrator.process_centered_sample(float(value), float(snapshot.timestamps_s[row]))
                integrator_out.append(step.accumulated_force_n)
            return integrator_out

        normal_ref = _integrate(normal_jerk_ref, "C")
        shear_lr_ref = _integrate(shear_lr_jerk_ref, "L")
        shear_tb_ref = _integrate(shear_tb_jerk_ref, "T")

        np.testing.assert_allclose(by_label["Normal Force [N]"].y, normal_ref, rtol=1e-6, atol=1e-12)
        np.testing.assert_allclose(by_label["Shear Force L/R [N]"].y, shear_lr_ref, rtol=1e-6, atol=1e-12)
        np.testing.assert_allclose(by_label["Shear Force T/B [N]"].y, shear_tb_ref, rtol=1e-6, atol=1e-12)

        # Existing voltage-based Shear/Normal Jerk path (moving SUM) is
        # untouched by enabling the new Force overlay flags.
        pressure_overlays = build_overlay_traces(
            snapshot, snapshot.data, axis_mode="samples",
            overlay_flags={"shear": True, "normal": True}, vref_voltage=vref_voltage,
            integration_window_samples=1, hpf_cutoff_hz=0.0,
        )
        self.assertEqual(
            {trace.label for trace in pressure_overlays},
            {"Shear L/R Jerk [V]", "Shear T/B Jerk [V]", "Normal Jerk [V]"},
        )

    def test_force_based_shear_normal_traces_matches_with_shared_median_baseline(self):
        # prepare_analysis_data hoists one _expanding_median(raw) per
        # position and shares it between Force and the Shear/Normal Jerk
        # overlay instead of each computing it independently -- passing that
        # precomputed (UNTRIMMED) baseline in must reproduce bit-for-bit the
        # same output as build_force_based_shear_normal_traces computing its
        # own baseline internally.
        data = np.asarray(
            [
                [200, 300, -300, 100, -100],
                [260, 500, -420, 140, -140],
                [260, 500, -420, 140, -140],
                [180, 260, -260, 80, -80],
            ],
            dtype=np.float32,
        )
        timestamps_s = np.asarray([0.0, 0.01, 0.02, 0.03], dtype=np.float64)
        snapshot = AnalysisSourceSnapshot(
            data=data,
            timestamps_s=timestamps_s,
            channel_labels=["C", "L", "R", "T", "B"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=100.0,
        )
        pzt_force_settings = {
            "enabled": True,
            "center_capacitance_value": 150.0,
            "outer_capacitance_value": 150.0,
            "capacitance_unit": "pF",
            "rleak_ohm": 1_000_000.0,
            "d33_pc_per_n": 600.0,
            "noise_threshold_n": 0.0,
        }
        vref_voltage = 3.3

        unshared_traces = build_force_based_shear_normal_traces(
            snapshot, snapshot.data, axis_mode="samples",
            overlay_flags={"shear_force": True, "normal_force": True}, vref_voltage=vref_voltage,
            pzt_force_settings=pzt_force_settings,
        )

        volts_by_position = {
            position: counts_to_volts(data[:, index], vref_voltage)
            for index, position in enumerate(["C", "L", "R", "T", "B"])
        }
        median_by_position = {
            position: _expanding_median(volts) for position, volts in volts_by_position.items()
        }
        shared_traces = build_force_based_shear_normal_traces(
            snapshot, snapshot.data, axis_mode="samples",
            overlay_flags={"shear_force": True, "normal_force": True}, vref_voltage=vref_voltage,
            pzt_force_settings=pzt_force_settings,
            median_by_position=median_by_position,
        )

        unshared_by_label = {trace.label: trace for trace in unshared_traces}
        shared_by_label = {trace.label: trace for trace in shared_traces}
        for label in ("Normal Force [N]", "Shear Force L/R [N]", "Shear Force T/B [N]"):
            np.testing.assert_array_equal(shared_by_label[label].y, unshared_by_label[label].y)

    def test_force_based_shear_normal_traces_fixes_capacitance_mixing(self):
        # Original bug: Normal Force summed raw voltage across C (center
        # capacitance) and L/R/T/B (outer capacitance) BEFORE any
        # capacitance-aware conversion, so a diverged center/outer
        # capacitance silently mis-weighted the center channel's
        # contribution. The per-channel rate stage converts each position
        # with its OWN capacitance before combination, so this must no
        # longer reproduce the naive (uncorrected) sum.
        data = np.asarray(
            [
                [200, 300, -300, 100, -100],
                [260, 500, -420, 140, -140],
                [260, 500, -420, 140, -140],
                [180, 260, -260, 80, -80],
            ],
            dtype=np.float32,
        )
        timestamps_s = np.asarray([0.0, 0.01, 0.02, 0.03], dtype=np.float64)
        snapshot = AnalysisSourceSnapshot(
            data=data,
            timestamps_s=timestamps_s,
            channel_labels=["C", "L", "R", "T", "B"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=100.0,
        )
        vref_voltage = 3.3
        # Center capacitance is 3x the outer capacitance -- if the center
        # channel's voltage were summed in raw (uncorrected) instead of
        # converted through its own capacitance first, Normal Force would
        # come out 3x too large relative to the correctly-weighted result.
        pzt_force_settings = {
            "enabled": True,
            "center_capacitance_value": 450.0,
            "outer_capacitance_value": 150.0,
            "capacitance_unit": "pF",
            "rleak_ohm": 1_000_000.0,
            "d33_pc_per_n": 600.0,
            "noise_threshold_n": 0.0,
        }

        traces = build_force_based_shear_normal_traces(
            snapshot, snapshot.data, axis_mode="samples",
            overlay_flags={"normal_force": True}, vref_voltage=vref_voltage,
            pzt_force_settings=pzt_force_settings,
        )
        normal_force = next(trace.y for trace in traces if trace.label == "Normal Force [N]")

        # Naive (buggy) reference: sum raw voltage across all 5 positions
        # using only the CENTER capacitance for every position (the actual
        # pre-fix behavior: shear/normal separation ran on raw voltage with
        # no per-channel capacitance conversion at all, and the combined
        # stage's own integrator used the center capacitance).
        max_adc = float((2 ** 12) - 1)
        volts_by_position = {
            position: (data[:, index] / max_adc) * vref_voltage
            for index, position in enumerate(["C", "L", "R", "T", "B"])
        }
        centered_by_position = {
            position: volts_by_position[position] - _expanding_median(volts_by_position[position])
            for position in ["C", "L", "R", "T", "B"]
        }
        detector = ShearDetector()
        calculator = NormalForceCalculator()
        naive_normal_jerk = []
        for row in range(len(snapshot.timestamps_s)):
            values = {position: float(centered_by_position[position][row]) for position in ["C", "L", "R", "T", "B"]}
            shear = detector.detect(values)
            naive_normal_jerk.append(calculator.compute(shear.residual).total_force)
        center_capacitance_f = pzt_capacitance_to_farads(450.0, "pF")
        naive_integrator = PztForceChannelIntegrator(
            capacitance_f=center_capacitance_f,
            rleak_ohm=pzt_force_settings["rleak_ohm"],
            d33_c_per_n=pzt_force_settings["d33_pc_per_n"] * 1e-12,
            noise_threshold_v=pzt_force_settings["noise_threshold_n"],
        )
        naive_normal = [
            naive_integrator.process_centered_sample(float(value), float(snapshot.timestamps_s[row])).accumulated_force_n
            for row, value in enumerate(naive_normal_jerk)
        ]

        self.assertFalse(
            np.allclose(normal_force, naive_normal, rtol=1e-6, atol=1e-12),
            msg="Normal Force must no longer match the capacitance-mixing-bug reference",
        )

    def test_force_based_shear_normal_traces_raises_when_positions_missing(self):
        # Only 3 of 5 required C/L/R/T/B positions -- a genuine "no data" case.
        snapshot = AnalysisSourceSnapshot(
            data=np.asarray([[100, 100, -100]], dtype=np.float32),
            timestamps_s=np.asarray([0.0], dtype=np.float64),
            channel_labels=["C", "L", "R"],
            metadata={"configuration": {"channels": [1, 2, 3], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=100.0,
        )
        with self.assertRaises(ValueError):
            build_force_based_shear_normal_traces(
                snapshot, snapshot.data, axis_mode="samples",
                overlay_flags={"normal_force": True}, vref_voltage=3.3,
                pzt_force_settings={"center_capacitance_value": 150.0, "outer_capacitance_value": 150.0},
            )

    def test_force_based_shear_normal_traces_idle_stretch_stays_near_zero(self):
        # A long quiet stretch (sub-noise-threshold jitter around 0) must not
        # drift under the RC integrator -- this is the test that would catch
        # a baseline/centering regression in the moving-average path.
        rng_values = np.asarray([0.0, 0.0002, -0.0001, 0.0001, -0.0002, 0.0] * 10, dtype=np.float32)
        data = np.tile(rng_values.reshape(-1, 1), (1, 5)) * 100.0  # small ADC-count jitter, all positions
        timestamps = np.arange(data.shape[0], dtype=np.float64) * 0.01
        snapshot = AnalysisSourceSnapshot(
            data=data,
            timestamps_s=timestamps,
            channel_labels=["C", "L", "R", "T", "B"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=100.0,
        )
        pzt_force_settings = {
            "center_capacitance_value": 150.0,
            "outer_capacitance_value": 150.0,
            "capacitance_unit": "pF",
            "rleak_ohm": 1_000_000.0,
            "d33_pc_per_n": 600.0,
            "noise_threshold_n": 0.05,
        }

        traces = build_force_based_shear_normal_traces(
            snapshot, snapshot.data, axis_mode="samples",
            overlay_flags={"normal_force": True}, vref_voltage=3.3,
            pzt_force_settings=pzt_force_settings,
        )
        normal_force = next(trace.y for trace in traces if trace.label == "Normal Force [N]")
        self.assertTrue(np.all(np.abs(normal_force) < 1e-6), msg=f"drifted: {normal_force}")

    def test_shear_normal_default_settings_match_shared_legacy_values(self):
        # The new per-role keys must default to exactly the same value as
        # their pre-existing shared counterpart, so nobody's behavior changes
        # until they explicitly diverge shear from normal. Noise threshold is
        # excluded here: it was renamed *_threshold_v -> *_threshold_n when
        # the combined-stage input changed from a voltage to a force-rate, so
        # it intentionally no longer shares a value with the volt-scale
        # generic "noise_threshold_v" (a different physical quantity now).
        self.assertEqual(
            PZT_FORCE_DEFAULT_SETTINGS["shear_force_zero_band_min_n"],
            PZT_FORCE_DEFAULT_SETTINGS["force_zero_band_min_n"],
        )
        self.assertEqual(
            PZT_FORCE_DEFAULT_SETTINGS["normal_force_zero_band_min_n"],
            PZT_FORCE_DEFAULT_SETTINGS["force_zero_band_min_n"],
        )
        self.assertEqual(
            PZT_FORCE_DEFAULT_SETTINGS["shear_force_zero_min_event_peak_n"],
            PZT_FORCE_DEFAULT_SETTINGS["force_zero_min_event_peak_n"],
        )
        self.assertEqual(
            PZT_FORCE_DEFAULT_SETTINGS["normal_force_zero_min_event_peak_n"],
            PZT_FORCE_DEFAULT_SETTINGS["force_zero_min_event_peak_n"],
        )

    def test_shear_normal_force_noise_thresholds_are_independent(self):
        # Same tap-like input to both paths; a very high Normal noise
        # threshold must silence Normal Force while a low Shear noise
        # threshold still lets Shear Force respond, and vice versa.
        data = np.asarray(
            [
                [200, 300, -300, 100, -100],
                [260, 500, -420, 140, -140],
                [260, 500, -420, 140, -140],
                [180, 260, -260, 80, -80],
            ],
            dtype=np.float32,
        )
        timestamps_s = np.asarray([0.0, 0.01, 0.02, 0.03], dtype=np.float64)
        snapshot = AnalysisSourceSnapshot(
            data=data,
            timestamps_s=timestamps_s,
            channel_labels=["C", "L", "R", "T", "B"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=100.0,
        )
        base_settings = {
            "center_capacitance_value": 150.0,
            "outer_capacitance_value": 150.0,
            "capacitance_unit": "pF",
            "rleak_ohm": 1_000_000.0,
            "d33_pc_per_n": 600.0,
        }

        # Normal silenced (huge threshold), Shear responsive (near-zero threshold).
        traces = build_force_based_shear_normal_traces(
            snapshot, snapshot.data, axis_mode="samples",
            overlay_flags={"shear_force": True, "normal_force": True}, vref_voltage=3.3,
            pzt_force_settings={
                **base_settings,
                "normal_force_noise_threshold_n": 100.0,
                "shear_force_noise_threshold_n": 0.0,
            },
        )
        by_label = {trace.label: trace for trace in traces}
        self.assertTrue(np.all(by_label["Normal Force [N]"].y == 0.0))
        self.assertTrue(np.any(by_label["Shear Force L/R [N]"].y != 0.0))

        # Flip it: Shear silenced, Normal responsive.
        traces = build_force_based_shear_normal_traces(
            snapshot, snapshot.data, axis_mode="samples",
            overlay_flags={"shear_force": True, "normal_force": True}, vref_voltage=3.3,
            pzt_force_settings={
                **base_settings,
                "normal_force_noise_threshold_n": 0.0,
                "shear_force_noise_threshold_n": 100.0,
            },
        )
        by_label = {trace.label: trace for trace in traces}
        self.assertTrue(np.all(by_label["Shear Force L/R [N]"].y == 0.0))
        self.assertTrue(np.all(by_label["Shear Force T/B [N]"].y == 0.0))
        self.assertTrue(np.any(by_label["Normal Force [N]"].y != 0.0))

    def test_shear_normal_force_threshold_falls_back_to_legacy_shared_key(self):
        # A caller that only sets the generic shared "noise_threshold_n" (not
        # either role-specific key) must keep getting that value applied to
        # both Normal and Shear, not the role-specific default.
        snapshot = AnalysisSourceSnapshot(
            data=np.asarray(
                [[200, 300, -300, 100, -100], [260, 500, -420, 140, -140]], dtype=np.float32
            ),
            timestamps_s=np.asarray([0.0, 0.01], dtype=np.float64),
            channel_labels=["C", "L", "R", "T", "B"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=100.0,
        )
        pzt_force_settings = {
            "center_capacitance_value": 150.0,
            "outer_capacitance_value": 150.0,
            "capacitance_unit": "pF",
            "rleak_ohm": 1_000_000.0,
            "d33_pc_per_n": 600.0,
            "noise_threshold_n": 100.0,  # generic shared key only, no per-role keys
        }
        traces = build_force_based_shear_normal_traces(
            snapshot, snapshot.data, axis_mode="samples",
            overlay_flags={"shear_force": True, "normal_force": True}, vref_voltage=3.3,
            pzt_force_settings=pzt_force_settings,
        )
        by_label = {trace.label: trace for trace in traces}
        # Threshold of 100N silences everything, matching pre-split behavior.
        self.assertTrue(np.all(by_label["Normal Force [N]"].y == 0.0))
        self.assertTrue(np.all(by_label["Shear Force L/R [N]"].y == 0.0))
        self.assertTrue(np.all(by_label["Shear Force T/B [N]"].y == 0.0))

    def test_force_based_shear_normal_traces_independent_of_jerk_display_toggles(self):
        snapshot = AnalysisSourceSnapshot(
            data=np.asarray(
                [[200, 300, -300, 100, -100], [260, 500, -420, 140, -140]], dtype=np.float32
            ),
            timestamps_s=np.asarray([0.0, 0.01], dtype=np.float64),
            channel_labels=["C", "L", "R", "T", "B"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=100.0,
        )
        pzt_force_settings = {
            "center_capacitance_value": 150.0,
            "outer_capacitance_value": 150.0,
            "capacitance_unit": "pF",
            "rleak_ohm": 1_000_000.0,
            "d33_pc_per_n": 600.0,
            "noise_threshold_n": 0.0,
        }
        # Shear/Normal Jerk display checkboxes ("shear"/"normal") both OFF --
        # Shear Force / Normal Force must still compute, since it runs its
        # own moving-average computation, not the displayed Jerk values.
        prepared = prepare_analysis_data(
            snapshot, axis_mode="samples",
            overlay_flags={"normal_force": True, "shear_force": True, "shear": False, "normal": False},
            vref_voltage=3.3,
            pzt_force_settings=pzt_force_settings,
        )
        force_labels = {trace.label for trace in prepared.force_traces}
        self.assertIn("Normal Force [N]", force_labels)
        self.assertIn("Shear Force L/R [N]", force_labels)
        overlay_labels = {trace.label for trace in prepared.overlay_traces}
        self.assertNotIn("Normal Jerk [V]", overlay_labels)

    def test_force_based_shear_normal_traces_skipped_on_invalid_settings(self):
        snapshot = AnalysisSourceSnapshot(
            data=np.asarray([[100, 100, -100, 50, -50]], dtype=np.float32),
            timestamps_s=np.asarray([0.0], dtype=np.float64),
            channel_labels=["C", "L", "R", "T", "B"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=100.0,
        )
        invalid_settings = {"enabled": True, "center_capacitance_value": 0.0}

        prepared = prepare_analysis_data(
            snapshot, axis_mode="samples",
            overlay_flags={"normal_force": True}, vref_voltage=3.3,
            pzt_force_settings=invalid_settings,
        )
        self.assertIn("Shear Force / Normal Force skipped", prepared.status)
        self.assertNotIn("Normal Force [N]", {trace.label for trace in prepared.force_traces})

    def test_force_based_shear_normal_traces_no_longer_expose_pzt_channel_force(self):
        """"PZT Channel Force" was removed as a standalone display feature --
        Normal Force still computes and displays on its own, and no
        "PZT Channel Force - ..." trace is ever produced, regardless of the
        (now-unused) ``enabled`` settings key."""
        snapshot = AnalysisSourceSnapshot(
            data=np.asarray(
                [[200, 300, -300, 100, -100], [260, 500, -420, 140, -140]], dtype=np.float32
            ),
            timestamps_s=np.asarray([0.0, 0.01], dtype=np.float64),
            channel_labels=["C", "L", "R", "T", "B"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=100.0,
        )
        pzt_force_settings = {
            "mux_timing_mode": "continuous",
            "center_capacitance_value": 150.0,
            "outer_capacitance_value": 150.0,
            "capacitance_unit": "pF",
            "rleak_ohm": 1_000_000.0,
            "d33_pc_per_n": 600.0,
            "noise_threshold_n": 0.0,
        }
        prepared = prepare_analysis_data(
            snapshot, axis_mode="samples",
            overlay_flags={"normal_force": True}, vref_voltage=3.3,
            pzt_force_settings=pzt_force_settings,
        )
        force_labels = {trace.label for trace in prepared.force_traces}
        self.assertIn("Normal Force [N]", force_labels)
        self.assertFalse(any(label.startswith("PZT Channel Force") for label in force_labels))

    def test_prepare_analysis_data_builds_integration_for_generic_channel_labels(self):
        snapshot = AnalysisSourceSnapshot(
            data=np.asarray(
                [
                    [100, -100, 80, 40, -40],
                    [120, -120, 100, 50, -50],
                    [140, -140, 120, 60, -60],
                ],
                dtype=np.float32,
            ),
            timestamps_s=np.asarray([0.0, 0.01, 0.02], dtype=np.float64),
            channel_labels=["CH1", "CH2", "CH3", "CH4", "CH5"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=500.0,
        )

        prepared = prepare_analysis_data(
            snapshot,
            axis_mode="time_ms",
            visible_labels=["CH1", "CH2", "CH3", "CH4", "CH5"],
            overlay_flags={"integration": True},
            vref_voltage=3.3,
            integration_window_samples=1,
            hpf_cutoff_hz=0.0,
        )

        self.assertEqual(
            [trace.label for trace in prepared.overlay_traces if trace.group == "integration"],
            [
                "Integrated CH1 [V samples]",
                "Integrated CH2 [V samples]",
                "Integrated CH3 [V samples]",
                "Integrated CH4 [V samples]",
                "Integrated CH5 [V samples]",
            ],
        )


if __name__ == "__main__":
    unittest.main()
