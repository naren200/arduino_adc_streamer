"""
Tests for Force Calibration Panel
=================================
Unit tests for the Force Calibration tab and live 5-sensor capture workflow.
"""

import unittest

from data_processing.force_calibration_state import (
    ActiveMeasurementWindow,
    CalibrationRow,
    build_default_force_calibration_state,
    get_calibration_rows_for_family,
)


class TestForceCalibrationState(unittest.TestCase):
    def test_default_state_is_initialized(self):
        state = build_default_force_calibration_state()
        self.assertEqual(state.selected_sensor_family, "PZT")
        self.assertEqual(state.selected_signal_source, "heatmap")
        self.assertEqual(state.selected_sensor_number, 1)
        self.assertFalse(state.is_capturing)
        self.assertTrue(state.autosave_enabled)
        self.assertIsNone(state.active_row_index)

    def test_measurement_window_tracks_latest_sensor_values(self):
        window = ActiveMeasurementWindow()
        window.update_live_sensor_values([1.0, 2.0, 3.0, 4.0, 5.0])

        self.assertEqual(window.latest_sensor_values, [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(window.latest_sensor_total, 15.0)
        self.assertEqual(window.sample_count, 1)

    def test_measurement_window_reset(self):
        window = ActiveMeasurementWindow()
        window.update_live_sensor_values([1.0, 2.0, 3.0, 4.0, 5.0])
        window.reset()

        self.assertEqual(window.latest_sensor_values, [])
        self.assertEqual(window.latest_sensor_total, 0.0)
        self.assertEqual(window.sample_count, 0)

    def test_calibration_row_supports_5_sensor_readings(self):
        row = CalibrationRow(
            sensor_family="PZT",
            sensor_number=3,
            signal_source="raw",
            sensor_top=10.0,
            sensor_bottom=11.0,
            sensor_left=12.0,
            sensor_right=13.0,
            sensor_center=14.0,
            sensor_total=60.0,
            max_force_x=5.0,
            max_force_z=10.0,
            max_sensor_value=200.0,
            integration_samples=20,
        )

        self.assertEqual(row.sensor_top, 10.0)
        self.assertEqual(row.signal_source, "raw")
        self.assertEqual(row.sensor_bottom, 11.0)
        self.assertEqual(row.sensor_left, 12.0)
        self.assertEqual(row.sensor_right, 13.0)
        self.assertEqual(row.sensor_center, 14.0)
        self.assertEqual(row.sensor_total, 60.0)
        self.assertEqual(row.integration_samples, 20)

    def test_rows_are_separate_per_family(self):
        state = build_default_force_calibration_state()
        pzt_rows = get_calibration_rows_for_family(state, "PZT")
        pzr_rows = get_calibration_rows_for_family(state, "PZR")

        pzt_rows.append(CalibrationRow(sensor_family="PZT", sensor_number=1))
        pzr_rows.append(CalibrationRow(sensor_family="PZR", sensor_number=2))

        self.assertEqual(len(state.pzt_calibration_rows), 1)
        self.assertEqual(len(state.pzr_calibration_rows), 1)
        self.assertEqual(len(state.rosette_calibration_rows), 0)


class TestForceCalibrationLiveMapping(unittest.TestCase):
    def test_sensor_order_mapping_matches_table_columns(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        top, bottom, left, right, center, total = self._map(values)

        self.assertEqual(top, 10.0)
        self.assertEqual(bottom, 20.0)
        self.assertEqual(left, 40.0)
        self.assertEqual(right, 30.0)
        self.assertEqual(center, 50.0)
        self.assertEqual(total, 150.0)

    @staticmethod
    def _map(sensor_values):
        normalized = list(sensor_values) + [0.0] * max(0, 5 - len(sensor_values))
        top = normalized[0]
        bottom = normalized[1]
        right = normalized[2]
        left = normalized[3]
        center = normalized[4]
        total = sum(normalized[:5])
        return top, bottom, left, right, center, total


if __name__ == "__main__":
    unittest.main()
