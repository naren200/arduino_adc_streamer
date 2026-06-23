"""
Force Calibration Panel Mixin
==============================
GUI components for the Force Calibration tab.
Allows users to measure and persist force-sensor calibration rows.
"""

from __future__ import annotations

import time
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QPushButton, QComboBox, QSpinBox, QTableWidget, QTableWidgetItem,
    QFileDialog, QHeaderView, QScrollArea
)
from PyQt6.QtCore import Qt

from constants.ui import FORCE_CALIBRATION_TAB_NAME
from constants.force import (
    FORCE_CALIBRATION_DEFAULT_INTEGRATION_SAMPLES,
    FORCE_CALIBRATION_SETTINGS_DIRNAME,
    FORCE_CALIBRATION_SETTINGS_SUBDIR,
)
from data_processing.force_calibration_state import (
    build_default_force_calibration_state,
    CalibrationRow,
    get_calibration_rows_for_family,
)
from data_processing.force_state import get_force_runtime_state
from file_operations.settings_persistence import save_settings_payload, load_settings_payload


class ForceCalibrationPanelMixin:
    """Mixin for Force Calibration tab and workflow."""
    
    def create_force_calibration_tab(self) -> QWidget:
        """Create the Force Calibration tab widget.
        
        Returns:
            QWidget containing controls and calibration table.
        """
        self._force_calibration_settings_loading = False
        self._force_calibration_autosave_enabled = True
        
        tab = QWidget()
        tab.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QVBoxLayout(tab)
        
        # Controls group
        controls_group = QGroupBox("Calibration Controls")
        controls_layout = QGridLayout(controls_group)
        
        # Sensor family selector
        controls_layout.addWidget(QLabel("Sensor Family:"), 0, 0)
        self.force_calib_family_combo = QComboBox()
        self.force_calib_family_combo.addItems(["PZT", "PZR", "Rosette"])
        self.force_calib_family_combo.setCurrentText("PZT")
        self.force_calib_family_combo.currentTextChanged.connect(self._on_force_calib_family_changed)
        controls_layout.addWidget(self.force_calib_family_combo, 0, 1)
        
        # Sensor number selector
        controls_layout.addWidget(QLabel("Sensor Number:"), 0, 2)
        self.force_calib_sensor_number_spin = QSpinBox()
        self.force_calib_sensor_number_spin.setRange(1, 16)
        self.force_calib_sensor_number_spin.setValue(1)
        controls_layout.addWidget(self.force_calib_sensor_number_spin, 0, 3)
        
        # Integration samples selector
        controls_layout.addWidget(QLabel("Integration Samples:"), 0, 4)
        self.force_calib_integration_spin = QSpinBox()
        self.force_calib_integration_spin.setRange(1, 1000)
        self.force_calib_integration_spin.setValue(FORCE_CALIBRATION_DEFAULT_INTEGRATION_SAMPLES)
        controls_layout.addWidget(self.force_calib_integration_spin, 0, 5)
        
        # Start/Stop button
        self.force_calib_start_stop_btn = QPushButton("Start Measure")
        self.force_calib_start_stop_btn.setEnabled(False)  # Disabled until force is connected
        self.force_calib_start_stop_btn.clicked.connect(self._on_force_calib_start_stop_clicked)
        controls_layout.addWidget(self.force_calib_start_stop_btn, 1, 0, 1, 2)
        
        # Clear table button
        self.force_calib_clear_btn = QPushButton("Clear Table")
        self.force_calib_clear_btn.clicked.connect(self._on_force_calib_clear_clicked)
        controls_layout.addWidget(self.force_calib_clear_btn, 1, 2, 1, 2)
        
        # Save/Load buttons
        self.force_calib_save_btn = QPushButton("Save Calibration")
        self.force_calib_save_btn.clicked.connect(self._on_force_calib_save_clicked)
        controls_layout.addWidget(self.force_calib_save_btn, 1, 4)
        
        self.force_calib_load_btn = QPushButton("Load Calibration")
        self.force_calib_load_btn.clicked.connect(self._on_force_calib_load_clicked)
        controls_layout.addWidget(self.force_calib_load_btn, 1, 5)
        
        layout.addWidget(controls_group)
        
        # Status label
        self.force_calib_status_label = QLabel("Disconnected - No force sensor connected")
        self.force_calib_status_label.setStyleSheet("color: orange; font-weight: bold;")
        layout.addWidget(self.force_calib_status_label)
        
        # Calibration table
        self.force_calib_table = QTableWidget()
        self.force_calib_table.setColumnCount(7)
        self.force_calib_table.setHorizontalHeaderLabels([
            "Sensor", "Max Force X (N)", "Max Force Z (N)", 
            "Max Value", "Min Value", "Integration", "Timestamp"
        ])
        self.force_calib_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.force_calib_table.setMaximumHeight(300)
        layout.addWidget(self.force_calib_table)
        
        layout.addStretch()
        return tab
    
    def init_force_calibration_state(self):
        """Initialize force calibration state during app startup."""
        self.force_calibration_state = build_default_force_calibration_state()
        self._force_calibration_settings_loading = False
        self._force_calibration_autosave_enabled = True
    
    def enable_force_calibration_start_stop(self, enabled: bool):
        """Enable/disable the start/stop button based on force connection."""
        if hasattr(self, "force_calib_start_stop_btn"):
            self.force_calib_start_stop_btn.setEnabled(enabled)
            self.force_calib_start_stop_btn.setText("Start Measure" if not self.force_calibration_state.is_capturing else "Stop Measure")
            if enabled:
                self.force_calib_status_label.setText("Connected - Ready to measure")
                self.force_calib_status_label.setStyleSheet("color: green; font-weight: bold;")
            else:
                self.force_calib_status_label.setText("Disconnected - No force sensor connected")
                self.force_calib_status_label.setStyleSheet("color: orange; font-weight: bold;")
    
    def _on_force_calib_family_changed(self, family: str):
        """Handle sensor family selection change."""
        self.force_calibration_state.selected_sensor_family = family
        self._on_force_calib_table_refresh()
        if not getattr(self, "_force_calibration_settings_loading", False):
            self.save_force_calibration_last_state()
    
    def _on_force_calib_start_stop_clicked(self):
        """Toggle measurement capture."""
        force_state = get_force_runtime_state(self)
        port = getattr(self, "force_serial_port", None)
        
        if port is None or not getattr(port, "is_open", False):
            self.log_status("ERROR: Force sensor not connected")
            return
        
        if self.force_calibration_state.is_capturing:
            # Stop measurement
            self._stop_force_calibration_measurement()
        else:
            # Start measurement
            self._start_force_calibration_measurement()
    
    def _start_force_calibration_measurement(self):
        """Begin a new measurement window."""
        self.force_calibration_state.is_capturing = True
        self.force_calibration_state.active_measurement_window.reset()
        self.force_calibration_state.integration_samples = self.force_calib_integration_spin.value()
        self.force_calib_start_stop_btn.setText("Stop Measure")
        self.force_calib_family_combo.setEnabled(False)
        self.force_calib_sensor_number_spin.setEnabled(False)
        self.log_status(
            f"Started measuring {self.force_calibration_state.selected_sensor_family} "
            f"sensor {self.force_calibration_state.selected_sensor_number}..."
        )
    
    def _stop_force_calibration_measurement(self):
        """End measurement and commit a new row."""
        self.force_calibration_state.is_capturing = False
        self.force_calib_start_stop_btn.setText("Start Measure")
        self.force_calib_family_combo.setEnabled(True)
        self.force_calib_sensor_number_spin.setEnabled(True)
        
        window = self.force_calibration_state.active_measurement_window
        if not window.sensor_values:
            self.log_status("WARNING: No sensor data captured during measurement")
            return
        
        # Create a new calibration row
        row = CalibrationRow(
            sensor_family=self.force_calibration_state.selected_sensor_family,
            sensor_number=self.force_calibration_state.selected_sensor_number,
            max_force_x=window.get_max_force_x(),
            max_force_z=window.get_max_force_z(),
            max_sensor_value=window.get_max_sensor_value(),
            min_sensor_value=window.get_min_sensor_value(),
            timestamp=time.time(),
            integration_samples=self.force_calibration_state.integration_samples,
        )
        
        # Append to the appropriate family list
        rows = get_calibration_rows_for_family(
            self.force_calibration_state,
            self.force_calibration_state.selected_sensor_family
        )
        rows.append(row)
        
        self._on_force_calib_table_refresh()
        self.log_status(
            f"Captured calibration row: "
            f"Force X={row.max_force_x:.2f}N, Z={row.max_force_z:.2f}N, "
            f"Sensor={row.max_sensor_value:.2f}"
        )
        self.save_force_calibration_last_state()
    
    def _on_force_calib_table_refresh(self):
        """Repopulate the calibration table from state."""
        rows = get_calibration_rows_for_family(
            self.force_calibration_state,
            self.force_calibration_state.selected_sensor_family
        )
        
        self.force_calib_table.setRowCount(len(rows))
        for idx, row in enumerate(rows):
            sensor_label = f"{row.sensor_family} {row.sensor_number}"
            self.force_calib_table.setItem(idx, 0, QTableWidgetItem(sensor_label))
            self.force_calib_table.setItem(idx, 1, QTableWidgetItem(f"{row.max_force_x:.2f}"))
            self.force_calib_table.setItem(idx, 2, QTableWidgetItem(f"{row.max_force_z:.2f}"))
            self.force_calib_table.setItem(idx, 3, QTableWidgetItem(f"{row.max_sensor_value:.4f}"))
            
            min_val = f"{row.min_sensor_value:.4f}" if row.min_sensor_value is not None else "—"
            self.force_calib_table.setItem(idx, 4, QTableWidgetItem(min_val))
            
            self.force_calib_table.setItem(idx, 5, QTableWidgetItem(str(row.integration_samples)))
            
            ts = f"{row.timestamp:.0f}" if row.timestamp else "—"
            self.force_calib_table.setItem(idx, 6, QTableWidgetItem(ts))
    
    def _on_force_calib_clear_clicked(self):
        """Clear the calibration table for the selected family."""
        family = self.force_calibration_state.selected_sensor_family
        rows = get_calibration_rows_for_family(self.force_calibration_state, family)
        rows.clear()
        self._on_force_calib_table_refresh()
        self.log_status(f"Cleared {family} calibration table")
        self.save_force_calibration_last_state()
    
    def _on_force_calib_save_clicked(self):
        """Save calibration to file."""
        family = self.force_calibration_state.selected_sensor_family
        default_dir = Path.home() / FORCE_CALIBRATION_SETTINGS_DIRNAME / FORCE_CALIBRATION_SETTINGS_SUBDIR
        default_name = f"force_calibration_{family.lower()}.json"
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save Force Calibration", 
            str(default_dir / default_name), 
            "JSON Files (*.json);;All Files (*)"
        )
        
        if file_path:
            self.save_force_calibration_to_path(file_path, log_message=True)
    
    def _on_force_calib_load_clicked(self):
        """Load calibration from file."""
        family = self.force_calibration_state.selected_sensor_family
        default_dir = Path.home() / FORCE_CALIBRATION_SETTINGS_DIRNAME / FORCE_CALIBRATION_SETTINGS_SUBDIR
        
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Load Force Calibration",
            str(default_dir),
            "JSON Files (*.json);;All Files (*)"
        )
        
        if file_path:
            self.load_force_calibration_from_path(file_path, log_message=True)
    
    def save_force_calibration_to_path(self, file_path, log_message=True):
        """Persist calibration to a file."""
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
                    for row in self.force_calibration_state.pzt_calibration_rows
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
                    for row in self.force_calibration_state.pzr_calibration_rows
                ],
                "Rosette": [
                    {
                        "sensor_number": row.sensor_number,
                        "max_force_x": row.max_force_x,
                        "max_force_z": row.max_force_z,
                        "max_sensor_value": row.max_sensor_value,
                        "min_sensor_value": row.min_sensor_value,
                        "integration_samples": row.integration_samples,
                        "timestamp": row.timestamp,
                    }
                    for row in self.force_calibration_state.rosette_calibration_rows
                ],
            }
        }
        
        save_settings_payload(
            file_path,
            payload,
            log_callback=self.log_status if log_message else None,
            success_message="Saved force calibration: {path}",
        )
    
    def load_force_calibration_from_path(self, file_path, log_message=True):
        """Load calibration from file."""
        path, payload = load_settings_payload(file_path)
        
        self._force_calibration_settings_loading = True
        try:
            # Validate version
            if payload.get("version") != 1:
                self.log_status(f"WARNING: Unknown calibration file version: {payload.get('version')}")
                return
            
            calibrations = payload.get("calibrations", {})
            
            # Load each family
            self.force_calibration_state.pzt_calibration_rows.clear()
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
                self.force_calibration_state.pzt_calibration_rows.append(row)
            
            self.force_calibration_state.pzr_calibration_rows.clear()
            for row_dict in calibrations.get("PZR", []):
                row = CalibrationRow(
                    sensor_family="PZR",
                    sensor_number=row_dict.get("sensor_number", 1),
                    max_force_x=row_dict.get("max_force_x", 0.0),
                    max_force_z=row_dict.get("max_force_z", 0.0),
                    max_sensor_value=row_dict.get("max_sensor_value", 0.0),
                    min_sensor_value=row_dict.get("min_sensor_value"),
                    integration_samples=row_dict.get("integration_samples", 0),
                    timestamp=row_dict.get("timestamp"),
                )
                self.force_calibration_state.pzr_calibration_rows.append(row)
            
            self.force_calibration_state.rosette_calibration_rows.clear()
            for row_dict in calibrations.get("Rosette", []):
                row = CalibrationRow(
                    sensor_family="Rosette",
                    sensor_number=row_dict.get("sensor_number", 1),
                    max_force_x=row_dict.get("max_force_x", 0.0),
                    max_force_z=row_dict.get("max_force_z", 0.0),
                    max_sensor_value=row_dict.get("max_sensor_value", 0.0),
                    min_sensor_value=row_dict.get("min_sensor_value"),
                    integration_samples=row_dict.get("integration_samples", 0),
                    timestamp=row_dict.get("timestamp"),
                )
                self.force_calibration_state.rosette_calibration_rows.append(row)
            
            self._on_force_calib_table_refresh()
            if log_message:
                self.log_status(f"Loaded force calibration: {path}")
        finally:
            self._force_calibration_settings_loading = False
    
    def save_force_calibration_last_state(self):
        """Autosave the current calibration state."""
        if not getattr(self, "_force_calibration_autosave_enabled", False) or getattr(self, "_force_calibration_settings_loading", False):
            return
        
        try:
            path = Path.home() / FORCE_CALIBRATION_SETTINGS_DIRNAME / FORCE_CALIBRATION_SETTINGS_SUBDIR / "last_used_force_calibration.json"
            self.save_force_calibration_to_path(path, log_message=False)
        except Exception as exc:
            self.log_status(f"WARNING: could not save last force calibration state: {exc}")
    
    def load_last_force_calibration_state(self):
        """Load the autosaved calibration state."""
        path = Path.home() / FORCE_CALIBRATION_SETTINGS_DIRNAME / FORCE_CALIBRATION_SETTINGS_SUBDIR / "last_used_force_calibration.json"
        if path.exists():
            try:
                self.load_force_calibration_from_path(path, log_message=False)
            except Exception as exc:
                self.log_status(f"WARNING: could not load last force calibration state: {exc}")
