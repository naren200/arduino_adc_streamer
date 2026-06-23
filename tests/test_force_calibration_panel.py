"""
Tests for Force Calibration Panel
==================================
Unit tests for the Force Calibration tab and calibration workflow.
"""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import json
import tempfile

from data_processing.force_calibration_state import (
    ForceCalibrationState,
    CalibrationRow,
    ActiveMeasurementWindow,
    build_default_force_calibration_state,
    get_calibration_rows_for_family,
)


class TestForceCalibrationState(unittest.TestCase):
    """Test the force calibration state model."""
    
    def test_default_state_is_initialized(self):
        """Test that default state initializes correctly."""
        state = build_default_force_calibration_state()
        self.assertIsInstance(state, ForceCalibrationState)
        self.assertEqual(state.selected_sensor_family, "PZT")
        self.assertEqual(state.selected_sensor_number, 1)
        self.assertFalse(state.is_capturing)
        self.assertTrue(state.autosave_enabled)
    
    def test_calibration_rows_are_separate_per_family(self):
        """Test that rows are stored separately per sensor family."""
        state = build_default_force_calibration_state()
        
        # Add PZT row
        pzt_rows = get_calibration_rows_for_family(state, "PZT")
        pzt_rows.append(CalibrationRow(
            sensor_family="PZT",
            sensor_number=1,
            max_force_x=1.0,
            max_force_z=2.0,
            max_sensor_value=100.0,
        ))
        
        # Add PZR row
        pzr_rows = get_calibration_rows_for_family(state, "PZR")
        pzr_rows.append(CalibrationRow(
            sensor_family="PZR",
            sensor_number=1,
            max_force_x=1.5,
            max_force_z=2.5,
            max_sensor_value=50000.0,
        ))
        
        # Verify separation
        self.assertEqual(len(pzt_rows), 1)
        self.assertEqual(len(pzr_rows), 1)
        self.assertEqual(len(state.rosette_calibration_rows), 0)
        self.assertEqual(pzt_rows[0].sensor_family, "PZT")
        self.assertEqual(pzr_rows[0].sensor_family, "PZR")
    
    def test_active_measurement_window_tracks_peaks(self):
        """Test that the measurement window correctly tracks peak values."""
        window = ActiveMeasurementWindow()
        
        window.force_x_peaks.extend([0.1, 0.5, 0.3])
        window.force_z_peaks.extend([0.2, 1.0, 0.8])
        window.sensor_values.extend([10.0, 50.0, 30.0])
        
        self.assertEqual(window.get_max_force_x(), 0.5)
        self.assertEqual(window.get_max_force_z(), 1.0)
        self.assertEqual(window.get_max_sensor_value(), 50.0)
        self.assertEqual(window.get_min_sensor_value(), 10.0)
    
    def test_active_measurement_window_reset(self):
        """Test that the measurement window resets correctly."""
        window = ActiveMeasurementWindow()
        window.force_x_peaks.extend([0.1, 0.5])
        window.force_z_peaks.extend([0.2, 1.0])
        window.sensor_values.extend([10.0, 50.0])
        
        self.assertGreater(len(window.force_x_peaks), 0)
        
        window.reset()
        self.assertEqual(len(window.force_x_peaks), 0)
        self.assertEqual(len(window.force_z_peaks), 0)
        self.assertEqual(len(window.sensor_values), 0)
        self.assertEqual(window.get_max_force_x(), 0.0)
    
    def test_calibration_row_creation(self):
        """Test that a calibration row is created with all expected fields."""
        row = CalibrationRow(
            sensor_family="PZT",
            sensor_number=3,
            max_force_x=5.0,
            max_force_z=10.0,
            max_sensor_value=200.0,
            min_sensor_value=50.0,
            timestamp=1234567890.0,
            integration_samples=20,
        )
        
        self.assertEqual(row.sensor_family, "PZT")
        self.assertEqual(row.sensor_number, 3)
        self.assertEqual(row.max_force_x, 5.0)
        self.assertEqual(row.max_force_z, 10.0)
        self.assertEqual(row.max_sensor_value, 200.0)
        self.assertEqual(row.min_sensor_value, 50.0)
        self.assertEqual(row.integration_samples, 20)


class TestForceCalibrationPersistence(unittest.TestCase):
    """Test save/load of calibration files."""
    
    def test_calibration_save_structure(self):
        """Test that calibration is saved with correct JSON structure."""
        state = build_default_force_calibration_state()
        
        # Add sample rows
        state.pzt_calibration_rows.append(CalibrationRow(
            sensor_family="PZT",
            sensor_number=1,
            max_force_x=1.0,
            max_force_z=2.0,
            max_sensor_value=100.0,
            timestamp=1234567890.0,
            integration_samples=10,
        ))
        
        state.pzr_calibration_rows.append(CalibrationRow(
            sensor_family="PZR",
            sensor_number=2,
            max_force_x=3.0,
            max_force_z=4.0,
            max_sensor_value=50000.0,
            min_sensor_value=40000.0,
            timestamp=1234567890.0,
            integration_samples=0,
        ))
        
        # Simulate save payload
        payload = {
            "version": 1,
            "calibrations": {
                "PZT": [
                    {
                        "sensor_number": row.sensor_number,
                        "max_force_x": row.max_force_x,
                        "max_force_z": row.max_force_z,
                        "max_sensor_value": row.max_sensor_value,
                        "min_sensor_value": row.min_sensor_value,
                        "integration_samples": row.integration_samples,
                        "timestamp": row.timestamp,
                    }
                    for row in state.pzt_calibration_rows
                ],
                "PZR": [
                    {
                        "sensor_number": row.sensor_number,
                        "max_force_x": row.max_force_x,
                        "max_force_z": row.max_force_z,
                        "max_sensor_value": row.max_sensor_value,
                        "min_sensor_value": row.min_sensor_value,
                        "integration_samples": row.integration_samples,
                        "timestamp": row.timestamp,
                    }
                    for row in state.pzr_calibration_rows
                ],
                "Rosette": [],
            }
        }
        
        # Verify structure
        self.assertEqual(payload["version"], 1)
        self.assertIn("calibrations", payload)
        self.assertEqual(len(payload["calibrations"]["PZT"]), 1)
        self.assertEqual(len(payload["calibrations"]["PZR"]), 1)
        self.assertEqual(payload["calibrations"]["PZT"][0]["sensor_number"], 1)
        self.assertEqual(payload["calibrations"]["PZR"][0]["max_force_z"], 4.0)
    
    def test_calibration_load_structure(self):
        """Test that calibration is loaded correctly from JSON."""
        payload = {
            "version": 1,
            "calibrations": {
                "PZT": [
                    {
                        "sensor_number": 1,
                        "max_force_x": 1.0,
                        "max_force_z": 2.0,
                        "max_sensor_value": 100.0,
                        "min_sensor_value": None,
                        "integration_samples": 10,
                        "timestamp": 1234567890.0,
                    }
                ],
                "PZR": [],
                "Rosette": [],
            }
        }
        
        state = build_default_force_calibration_state()
        
        # Simulate load
        calibrations = payload.get("calibrations", {})
        for row_dict in calibrations.get("PZT", []):
            row = CalibrationRow(
                sensor_family="PZT",
                sensor_number=row_dict.get("sensor_number", 1),
                max_force_x=row_dict.get("max_force_x", 0.0),
                max_force_z=row_dict.get("max_force_z", 0.0),
                max_sensor_value=row_dict.get("max_sensor_value", 0.0),
                min_sensor_value=row_dict.get("min_sensor_value"),
                integration_samples=row_dict.get("integration_samples", 0),
                timestamp=row_dict.get("timestamp"),
            )
            state.pzt_calibration_rows.append(row)
        
        # Verify load
        self.assertEqual(len(state.pzt_calibration_rows), 1)
        loaded_row = state.pzt_calibration_rows[0]
        self.assertEqual(loaded_row.sensor_number, 1)
        self.assertEqual(loaded_row.max_force_x, 1.0)
        self.assertEqual(loaded_row.max_force_z, 2.0)
        self.assertEqual(loaded_row.integration_samples, 10)


class TestForceCalibrationMeasurement(unittest.TestCase):
    """Test measurement capture and row creation."""
    
    def test_measurement_creates_row_with_captured_values(self):
        """Test that stopping a measurement creates a row from captured peaks."""
        window = ActiveMeasurementWindow()
        window.force_x_peaks.extend([0.0, 0.5, 0.3, 0.2])
        window.force_z_peaks.extend([0.0, 1.0, 0.8, 0.5])
        window.sensor_values.extend([0.0, 100.0, 80.0, 50.0])
        
        # Create row from window
        row = CalibrationRow(
            sensor_family="PZT",
            sensor_number=1,
            max_force_x=window.get_max_force_x(),
            max_force_z=window.get_max_force_z(),
            max_sensor_value=window.get_max_sensor_value(),
            min_sensor_value=window.get_min_sensor_value(),
            integration_samples=10,
        )
        
        self.assertEqual(row.max_force_x, 0.5)
        self.assertEqual(row.max_force_z, 1.0)
        self.assertEqual(row.max_sensor_value, 100.0)
        self.assertEqual(row.min_sensor_value, 0.0)
    
    def test_measurement_with_no_data_returns_zero(self):
        """Test that empty measurement window returns zero values."""
        window = ActiveMeasurementWindow()
        
        self.assertEqual(window.get_max_force_x(), 0.0)
        self.assertEqual(window.get_max_force_z(), 0.0)
        self.assertEqual(window.get_max_sensor_value(), 0.0)
        self.assertIsNone(window.get_min_sensor_value())


if __name__ == "__main__":
    unittest.main()
