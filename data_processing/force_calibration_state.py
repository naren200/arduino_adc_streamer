"""
Force Calibration State
=======================
Typed force-calibration state and persistence helpers.
Separate from force_state.py (which manages zero-offset load-cell calibration).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(slots=True)
class CalibrationRow:
    """One measured calibration entry: max forces and sensor response during a window."""
    
    sensor_family: Literal["PZT", "PZR", "Rosette"]
    sensor_number: int
    max_force_x: float  # Newtons
    max_force_z: float  # Newtons
    max_sensor_value: float  # integrated voltage (PZT), resistance (PZR/Rosette)
    min_sensor_value: float | None = None  # For PZR/Rosette: min resistance
    timestamp: float | None = None  # Unix timestamp when row was captured
    integration_samples: int = 0  # Snapshot of active integration window size


@dataclass(slots=True)
class ForceCalibrationState:
    """State for the Force Calibration tab."""
    
    # Persisted rows per sensor family
    pzt_calibration_rows: list[CalibrationRow] = field(default_factory=list)
    pzr_calibration_rows: list[CalibrationRow] = field(default_factory=list)
    rosette_calibration_rows: list[CalibrationRow] = field(default_factory=list)
    
    # Active measurement window state
    active_measurement_window: ActiveMeasurementWindow = field(default_factory=lambda: ActiveMeasurementWindow())
    
    # UI state
    selected_sensor_family: Literal["PZT", "PZR", "Rosette"] = "PZT"
    selected_sensor_number: int = 1
    integration_samples: int = 10  # Number of samples to integrate for PZT
    is_capturing: bool = False
    
    # Autosave enable flag
    autosave_enabled: bool = True


@dataclass(slots=True)
class ActiveMeasurementWindow:
    """Tracks peak values during an active measurement window."""
    
    force_x_peaks: list[float] = field(default_factory=list)
    force_z_peaks: list[float] = field(default_factory=list)
    sensor_values: list[float] = field(default_factory=list)
    
    def get_max_force_x(self) -> float:
        return max(self.force_x_peaks) if self.force_x_peaks else 0.0
    
    def get_max_force_z(self) -> float:
        return max(self.force_z_peaks) if self.force_z_peaks else 0.0
    
    def get_max_sensor_value(self) -> float:
        return max(self.sensor_values) if self.sensor_values else 0.0
    
    def get_min_sensor_value(self) -> float | None:
        return min(self.sensor_values) if self.sensor_values else None
    
    def reset(self):
        """Clear all accumulated samples."""
        self.force_x_peaks.clear()
        self.force_z_peaks.clear()
        self.sensor_values.clear()


def build_default_force_calibration_state() -> ForceCalibrationState:
    """Build a fresh calibration state."""
    return ForceCalibrationState()


def get_calibration_rows_for_family(
    state: ForceCalibrationState, 
    family: Literal["PZT", "PZR", "Rosette"]
) -> list[CalibrationRow]:
    """Return the calibration rows for a given sensor family."""
    if family == "PZT":
        return state.pzt_calibration_rows
    elif family == "PZR":
        return state.pzr_calibration_rows
    else:  # Rosette
        return state.rosette_calibration_rows
