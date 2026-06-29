"""
Offline Analysis tab.

The panel is read-only relative to acquisition state.  It copies a source
snapshot, prepares display traces through ``data_processing.analysis_workbench``,
and renders them on stacked, X-linked plots.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from constants.plotting import PLOT_COLORS
from data_processing.analysis_workbench import (
    AnalysisPreparedData,
    AnalysisSourceSnapshot,
    build_in_memory_snapshot,
    load_exported_csv_snapshot,
    prepare_analysis_data,
)
from file_operations.settings_persistence import load_settings_payload, save_settings_payload


class AnalysisPanelMixin:
    """Mixin providing the offline Analysis tab and interactions."""

    def _init_analysis_state(self):
        self.analysis_state = {
            "source_mode": "in_memory",
            "axis_mode": "time_ms",
            "zoom_mode": "x",
            "filter_enabled": False,
            "marker_enabled": True,
            "overlays": {
                "shear": False,
                "normal": False,
                "integration": False,
            },
            "visible_labels": {},
            "csv_path": "",
            "metadata_path": "",
        }
        self.analysis_snapshot: AnalysisSourceSnapshot | None = None
        self.analysis_prepared: AnalysisPreparedData | None = None
        self.analysis_channel_checks: dict[str, QCheckBox] = {}
        self.analysis_signal_curves = {}
        self.analysis_force_curves = {}
        self.analysis_overlay_curves = {}
        self._analysis_marker_timer = QTimer()
        self._analysis_marker_timer.setSingleShot(True)
        self._analysis_pending_marker_x = None

    def _get_last_analysis_settings_path(self):
        return Path.home() / ".adc_streamer" / "analysis" / "last_used_analysis_settings.json"

    def _serialize_analysis_settings(self):
        return {"version": 1, "analysis_settings": dict(getattr(self, "analysis_state", {}))}

    def save_last_analysis_settings(self):
        try:
            save_settings_payload(self._get_last_analysis_settings_path(), self._serialize_analysis_settings())
        except Exception as exc:
            if hasattr(self, "log_status"):
                self.log_status(f"Warning: could not save Analysis settings: {exc}")

    def load_last_analysis_settings(self):
        try:
            path = self._get_last_analysis_settings_path()
            if not path.exists():
                return
            _path, payload = load_settings_payload(path, payload_key="analysis_settings")
            if isinstance(payload, dict):
                self.analysis_state.update(payload)
                overlays = payload.get("overlays")
                if isinstance(overlays, dict):
                    self.analysis_state["overlays"].update(overlays)
                visible = payload.get("visible_labels")
                if isinstance(visible, dict):
                    self.analysis_state["visible_labels"] = visible
                self._apply_analysis_settings_to_widgets()
        except Exception as exc:
            if hasattr(self, "log_status"):
                self.log_status(f"Warning: could not load Analysis settings: {exc}")

    def create_analysis_tab(self) -> QWidget:
        tab = QWidget()
        root = QVBoxLayout(tab)

        self.analysis_disabled_label = QLabel("")
        self.analysis_disabled_label.setStyleSheet("QLabel { color: #9C27B0; font-weight: bold; }")
        root.addWidget(self.analysis_disabled_label)

        controls = QGroupBox("Analysis Controls")
        controls_layout = QGridLayout(controls)

        controls_layout.addWidget(QLabel("Source:"), 0, 0)
        self.analysis_source_combo = QComboBox()
        self.analysis_source_combo.addItems(["In-memory cache", "CSV plus JSON"])
        self.analysis_source_combo.currentIndexChanged.connect(self.on_analysis_source_changed)
        controls_layout.addWidget(self.analysis_source_combo, 0, 1)

        self.analysis_load_memory_btn = QPushButton("Load Latest")
        self.analysis_load_memory_btn.clicked.connect(self.load_analysis_source)
        controls_layout.addWidget(self.analysis_load_memory_btn, 0, 2)

        controls_layout.addWidget(QLabel("X Axis:"), 0, 3)
        self.analysis_axis_combo = QComboBox()
        self.analysis_axis_combo.addItems(["Time ms", "Sample index"])
        self.analysis_axis_combo.currentIndexChanged.connect(self.on_analysis_settings_changed)
        controls_layout.addWidget(self.analysis_axis_combo, 0, 4)

        controls_layout.addWidget(QLabel("Zoom:"), 0, 5)
        self.analysis_zoom_combo = QComboBox()
        self.analysis_zoom_combo.addItems(["X only", "Y only", "X and Y"])
        self.analysis_zoom_combo.currentIndexChanged.connect(self.on_analysis_zoom_changed)
        controls_layout.addWidget(self.analysis_zoom_combo, 0, 6)

        self.analysis_csv_path_edit = QLineEdit()
        self.analysis_csv_path_edit.setPlaceholderText("CSV file")
        controls_layout.addWidget(self.analysis_csv_path_edit, 1, 0, 1, 3)
        self.analysis_browse_csv_btn = QPushButton("Browse CSV")
        self.analysis_browse_csv_btn.clicked.connect(self.on_analysis_browse_csv)
        controls_layout.addWidget(self.analysis_browse_csv_btn, 1, 3)

        self.analysis_metadata_path_edit = QLineEdit()
        self.analysis_metadata_path_edit.setPlaceholderText("Metadata JSON file")
        controls_layout.addWidget(self.analysis_metadata_path_edit, 1, 4)
        self.analysis_browse_metadata_btn = QPushButton("Browse JSON")
        self.analysis_browse_metadata_btn.clicked.connect(self.on_analysis_browse_metadata)
        controls_layout.addWidget(self.analysis_browse_metadata_btn, 1, 5)
        self.analysis_load_file_btn = QPushButton("Load CSV + JSON")
        self.analysis_load_file_btn.setToolTip("Reload the selected CSV and metadata JSON files")
        self.analysis_load_file_btn.clicked.connect(self.load_analysis_source)
        controls_layout.addWidget(self.analysis_load_file_btn, 1, 6)

        self.analysis_filter_check = QCheckBox("Spectrum filter")
        self.analysis_filter_check.stateChanged.connect(self.on_analysis_settings_changed)
        controls_layout.addWidget(self.analysis_filter_check, 2, 0)
        self.analysis_shear_check = QCheckBox("Shear")
        self.analysis_shear_check.stateChanged.connect(self.on_analysis_settings_changed)
        controls_layout.addWidget(self.analysis_shear_check, 2, 1)
        self.analysis_normal_check = QCheckBox("Normal pressure")
        self.analysis_normal_check.stateChanged.connect(self.on_analysis_settings_changed)
        controls_layout.addWidget(self.analysis_normal_check, 2, 2)
        self.analysis_integration_check = QCheckBox("Integration")
        self.analysis_integration_check.stateChanged.connect(self.on_analysis_settings_changed)
        controls_layout.addWidget(self.analysis_integration_check, 2, 3)
        self.analysis_marker_check = QCheckBox("Marker")
        self.analysis_marker_check.setChecked(True)
        self.analysis_marker_check.stateChanged.connect(self.on_analysis_marker_toggled)
        controls_layout.addWidget(self.analysis_marker_check, 2, 4)

        self.analysis_reset_view_btn = QPushButton("Reset View")
        self.analysis_reset_view_btn.clicked.connect(self.reset_analysis_view)
        controls_layout.addWidget(self.analysis_reset_view_btn, 2, 6)
        root.addWidget(controls)

        channel_group = QGroupBox("Display Channels")
        channel_layout = QVBoxLayout(channel_group)
        self.analysis_channel_container = QWidget()
        self.analysis_channel_layout = QGridLayout(self.analysis_channel_container)
        self.analysis_channel_layout.setSpacing(5)
        channel_scroll = QScrollArea()
        channel_scroll.setWidget(self.analysis_channel_container)
        channel_scroll.setWidgetResizable(True)
        channel_scroll.setMaximumHeight(95)
        channel_layout.addWidget(channel_scroll)
        channel_buttons = QHBoxLayout()
        self.analysis_select_all_btn = QPushButton("All")
        self.analysis_select_all_btn.clicked.connect(lambda: self.set_all_analysis_channels(True))
        channel_buttons.addWidget(self.analysis_select_all_btn)
        self.analysis_select_none_btn = QPushButton("None")
        self.analysis_select_none_btn.clicked.connect(lambda: self.set_all_analysis_channels(False))
        channel_buttons.addWidget(self.analysis_select_none_btn)
        channel_buttons.addStretch()
        channel_layout.addLayout(channel_buttons)
        root.addWidget(channel_group)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.analysis_signal_plot = pg.PlotWidget()
        self.analysis_signal_plot.setBackground("w")
        self.analysis_signal_plot.showGrid(x=True, y=True, alpha=0.3)
        self.analysis_signal_plot.addLegend(offset=(10, 10))
        self.analysis_force_plot = pg.PlotWidget()
        self.analysis_force_plot.setBackground("w")
        self.analysis_force_plot.showGrid(x=True, y=True, alpha=0.3)
        self.analysis_force_plot.addLegend(offset=(10, 10))
        self.analysis_force_plot.setXLink(self.analysis_signal_plot)
        splitter.addWidget(self.analysis_signal_plot)
        splitter.addWidget(self.analysis_force_plot)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, stretch=1)

        self.analysis_marker_vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#444444", width=1))
        self.analysis_signal_plot.addItem(self.analysis_marker_vline)
        self.analysis_marker_vline.setVisible(False)
        self.analysis_mouse_proxy = pg.SignalProxy(
            self.analysis_signal_plot.scene().sigMouseMoved,
            rateLimit=30,
            slot=self._on_analysis_mouse_moved,
        )
        self._analysis_marker_timer.timeout.connect(self._flush_analysis_marker_readout)

        self.analysis_status_label = QLabel("Analysis: no source loaded")
        self.analysis_status_label.setStyleSheet("font-family: monospace;")
        root.addWidget(self.analysis_status_label)

        self._apply_analysis_settings_to_widgets()
        self.update_analysis_availability()
        self.on_analysis_source_changed()
        return tab

    def _apply_analysis_settings_to_widgets(self):
        if not hasattr(self, "analysis_source_combo"):
            return
        state = self.analysis_state
        self.analysis_source_combo.setCurrentIndex(1 if state.get("source_mode") == "csv_json" else 0)
        self.analysis_axis_combo.setCurrentIndex(1 if state.get("axis_mode") == "samples" else 0)
        self.analysis_zoom_combo.setCurrentIndex({"x": 0, "y": 1, "xy": 2}.get(state.get("zoom_mode", "x"), 0))
        self.analysis_filter_check.setChecked(bool(state.get("filter_enabled", False)))
        self.analysis_marker_check.setChecked(bool(state.get("marker_enabled", True)))
        overlays = state.get("overlays", {})
        self.analysis_shear_check.setChecked(bool(overlays.get("shear", False)))
        self.analysis_normal_check.setChecked(bool(overlays.get("normal", False)))
        self.analysis_integration_check.setChecked(bool(overlays.get("integration", False)))
        self.analysis_csv_path_edit.setText(str(state.get("csv_path", "")))
        self.analysis_metadata_path_edit.setText(str(state.get("metadata_path", "")))
        self.on_analysis_zoom_changed()

    def on_analysis_source_changed(self):
        if not self._analysis_has_in_memory_capture() and self.analysis_source_combo.currentIndex() == 0:
            self.analysis_source_combo.setCurrentIndex(1)
            return

        csv_mode = self.analysis_source_combo.currentIndex() == 1
        self.analysis_state["source_mode"] = "csv_json" if csv_mode else "in_memory"
        for widget in (
            self.analysis_csv_path_edit,
            self.analysis_browse_csv_btn,
            self.analysis_metadata_path_edit,
            self.analysis_browse_metadata_btn,
            self.analysis_load_file_btn,
        ):
            widget.setEnabled(csv_mode and not bool(getattr(self, "is_capturing", False)))
        self.analysis_load_memory_btn.setEnabled(
            not csv_mode
            and self._analysis_has_in_memory_capture()
            and not bool(getattr(self, "is_capturing", False))
        )
        self.save_last_analysis_settings()

    def on_analysis_browse_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Analysis CSV", "", "CSV Files (*.csv);;All Files (*)")
        if not path:
            return
        self.analysis_csv_path_edit.setText(path)
        candidate = Path(path).with_name(Path(path).stem + "_metadata.json")
        if candidate.exists() and not self.analysis_metadata_path_edit.text().strip():
            self.analysis_metadata_path_edit.setText(str(candidate))
        self._load_analysis_file_source_when_ready()

    def on_analysis_browse_metadata(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Analysis Metadata", "", "JSON Files (*.json);;All Files (*)")
        if path:
            self.analysis_metadata_path_edit.setText(path)
            self._load_analysis_file_source_when_ready()

    def _load_analysis_file_source_when_ready(self):
        csv_path = Path(self.analysis_csv_path_edit.text().strip())
        metadata_path = Path(self.analysis_metadata_path_edit.text().strip())
        if not csv_path.exists():
            self.analysis_status_label.setText("Analysis: choose a CSV file to load.")
            return
        if not metadata_path.exists():
            self.analysis_status_label.setText("Analysis: choose the matching metadata JSON file.")
            return
        self.analysis_source_combo.setCurrentIndex(1)
        self.load_analysis_source()

    def load_analysis_source(self):
        if getattr(self, "is_capturing", False):
            self.update_analysis_availability()
            return
        try:
            if self.analysis_source_combo.currentIndex() == 1:
                self.analysis_state["csv_path"] = self.analysis_csv_path_edit.text().strip()
                self.analysis_state["metadata_path"] = self.analysis_metadata_path_edit.text().strip()
                self.analysis_snapshot = load_exported_csv_snapshot(
                    self.analysis_state["csv_path"],
                    self.analysis_state["metadata_path"],
                )
            else:
                self.analysis_snapshot = build_in_memory_snapshot(self)
            self._rebuild_analysis_channel_checks()
            self.refresh_analysis_plot()
            self.analysis_status_label.setText(
                f"Analysis loaded: {self.analysis_snapshot.sweep_count} sweeps, "
                f"{self.analysis_snapshot.samples_per_sweep} signal columns"
            )
            if hasattr(self, "log_status"):
                self.log_status(
                    f"Analysis loaded: {self.analysis_snapshot.sweep_count} sweeps, "
                    f"{self.analysis_snapshot.samples_per_sweep} signal columns"
                )
            self.save_last_analysis_settings()
        except Exception as exc:
            self.analysis_status_label.setText(f"Analysis load failed: {exc}")
            if hasattr(self, "log_status"):
                self.log_status(f"Analysis load failed: {exc}")
            QMessageBox.warning(self, "Analysis Load Failed", str(exc))

    def _rebuild_analysis_channel_checks(self):
        while self.analysis_channel_layout.count():
            item = self.analysis_channel_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.analysis_channel_checks = {}
        snapshot = self.analysis_snapshot
        if snapshot is None:
            return
        saved_visibility = self.analysis_state.get("visible_labels", {})
        for index, label in enumerate(snapshot.channel_labels):
            check = QCheckBox(label)
            check.setChecked(bool(saved_visibility.get(label, True)))
            check.stateChanged.connect(self.on_analysis_settings_changed)
            self.analysis_channel_checks[label] = check
            self.analysis_channel_layout.addWidget(check, index // 6, index % 6)

    def set_all_analysis_channels(self, checked: bool):
        for check in self.analysis_channel_checks.values():
            check.setChecked(bool(checked))
        self.on_analysis_settings_changed()

    def on_analysis_settings_changed(self, *_args):
        if not hasattr(self, "analysis_axis_combo"):
            return
        self.analysis_state["axis_mode"] = "samples" if self.analysis_axis_combo.currentIndex() == 1 else "time_ms"
        self.analysis_state["filter_enabled"] = bool(self.analysis_filter_check.isChecked())
        self.analysis_state["marker_enabled"] = bool(self.analysis_marker_check.isChecked())
        self.analysis_state["overlays"] = {
            "shear": bool(self.analysis_shear_check.isChecked()),
            "normal": bool(self.analysis_normal_check.isChecked()),
            "integration": bool(self.analysis_integration_check.isChecked()),
        }
        self.analysis_state["visible_labels"] = {
            label: bool(check.isChecked())
            for label, check in self.analysis_channel_checks.items()
        }
        self.refresh_analysis_plot()
        self.save_last_analysis_settings()

    def on_analysis_zoom_changed(self, *_args):
        mode = ["x", "y", "xy"][self.analysis_zoom_combo.currentIndex()]
        self.analysis_state["zoom_mode"] = mode
        mouse_x = mode in ("x", "xy")
        mouse_y = mode in ("y", "xy")
        if hasattr(self, "analysis_signal_plot"):
            self.analysis_signal_plot.setMouseEnabled(x=mouse_x, y=mouse_y)
            self.analysis_force_plot.setMouseEnabled(x=mouse_x, y=mouse_y)
        self.save_last_analysis_settings()

    def on_analysis_marker_toggled(self, *_args):
        enabled = bool(self.analysis_marker_check.isChecked())
        self.analysis_state["marker_enabled"] = enabled
        if hasattr(self, "analysis_marker_vline"):
            self.analysis_marker_vline.setVisible(enabled and self.analysis_prepared is not None)
        self.save_last_analysis_settings()

    def refresh_analysis_plot(self):
        snapshot = self.analysis_snapshot
        if snapshot is None or not hasattr(self, "analysis_signal_plot"):
            return
        visible_labels = [
            label for label, check in self.analysis_channel_checks.items()
            if check.isChecked()
        ] or list(snapshot.channel_labels)
        filter_settings = self.get_filter_settings_from_ui() if hasattr(self, "get_filter_settings_from_ui") else {}
        self.analysis_prepared = prepare_analysis_data(
            snapshot,
            axis_mode=self.analysis_state.get("axis_mode", "time_ms"),
            visible_labels=visible_labels,
            filter_enabled=bool(self.analysis_filter_check.isChecked()),
            filter_settings=filter_settings,
            overlay_flags=self.analysis_state.get("overlays", {}),
            vref_voltage=self.get_vref_voltage() if hasattr(self, "get_vref_voltage") else 3.3,
            integration_window_samples=int(getattr(self, "signal_integration_window_samples", 1) or 1),
            hpf_cutoff_hz=float(getattr(self, "signal_integration_hpf_cutoff_hz", 0.0) or 0.0),
        )
        self._render_analysis_prepared()

    def _render_analysis_prepared(self):
        prepared = self.analysis_prepared
        if prepared is None:
            return
        desired_signal = set()
        desired_force = set()
        for index, trace in enumerate(prepared.traces + prepared.overlay_traces):
            key = trace.label
            desired_signal.add(key)
            curve = self.analysis_signal_curves.get(key)
            if curve is None:
                color = PLOT_COLORS[index % len(PLOT_COLORS)] if trace.group == "signal" else (70, 70, 70)
                style = Qt.PenStyle.SolidLine if trace.group == "signal" else Qt.PenStyle.DashLine
                curve = self.analysis_signal_plot.plot([], [], pen=pg.mkPen(color=color, width=2, style=style), name=key)
                curve.setClipToView(True)
                curve.setDownsampling(auto=True, method="peak")
                self.analysis_signal_curves[key] = curve
            curve.setData(trace.x, trace.y)
            curve.setVisible(True)

        for index, trace in enumerate(prepared.force_traces):
            key = trace.label
            desired_force.add(key)
            curve = self.analysis_force_curves.get(key)
            if curve is None:
                curve = self.analysis_force_plot.plot(
                    [], [], pen=pg.mkPen(color=PLOT_COLORS[(index + 2) % len(PLOT_COLORS)], width=2), name=key
                )
                curve.setClipToView(True)
                curve.setDownsampling(auto=True, method="peak")
                self.analysis_force_curves[key] = curve
            curve.setData(trace.x, trace.y)
            curve.setVisible(True)

        for key, curve in self.analysis_signal_curves.items():
            if key not in desired_signal:
                curve.setVisible(False)
        for key, curve in self.analysis_force_curves.items():
            if key not in desired_force:
                curve.setVisible(False)

        self.analysis_signal_plot.setLabel("bottom", prepared.x_label, units=prepared.x_units)
        self.analysis_signal_plot.setLabel("left", "Signals", units="V")
        self.analysis_force_plot.setLabel("bottom", prepared.x_label, units=prepared.x_units)
        self.analysis_force_plot.setLabel("left", "Force", units="N")
        self.analysis_marker_vline.setVisible(bool(self.analysis_marker_check.isChecked()))
        source = self.analysis_snapshot.source_id if self.analysis_snapshot else "-"
        self.analysis_status_label.setText(
            f"Analysis: {len(prepared.traces)} signal traces, {len(prepared.overlay_traces)} overlays, "
            f"{len(prepared.force_traces)} force traces | {source} {prepared.status}".strip()
        )

    def reset_analysis_view(self):
        for plot in (self.analysis_signal_plot, self.analysis_force_plot):
            plot.enableAutoRange()

    def _on_analysis_mouse_moved(self, evt):
        if not bool(self.analysis_marker_check.isChecked()) or self.analysis_prepared is None:
            return
        pos = evt[0]
        if not self.analysis_signal_plot.sceneBoundingRect().contains(pos):
            return
        mouse_point = self.analysis_signal_plot.plotItem.vb.mapSceneToView(pos)
        self._analysis_pending_marker_x = float(mouse_point.x())
        if not self._analysis_marker_timer.isActive():
            self._analysis_marker_timer.start(25)

    def _flush_analysis_marker_readout(self):
        x = self._analysis_pending_marker_x
        prepared = self.analysis_prepared
        if x is None or prepared is None:
            return
        self.analysis_marker_vline.setPos(float(x))
        values = []
        for trace in prepared.traces + prepared.overlay_traces + prepared.force_traces:
            if trace.x.size == 0 or trace.y.size == 0:
                continue
            idx = int(np.argmin(np.abs(trace.x - float(x))))
            if idx < trace.y.size:
                values.append(f"{trace.label}={float(trace.y[idx]):.4g}")
        self.analysis_status_label.setText(f"Marker x={float(x):.3f}: " + " | ".join(values[:12]))

    def update_analysis_availability(self):
        if not hasattr(self, "analysis_disabled_label"):
            return
        capturing = bool(getattr(self, "is_capturing", False))
        has_memory = self._analysis_has_in_memory_capture()
        self.analysis_disabled_label.setText("Analysis is disabled during active acquisition." if capturing else "")

        self._set_analysis_source_item_enabled(0, has_memory and not capturing)
        self._set_analysis_source_item_enabled(1, not capturing)
        if not capturing and not has_memory and self.analysis_source_combo.currentIndex() == 0:
            self.analysis_source_combo.setCurrentIndex(1)

        for widget in (
            self.analysis_source_combo,
            self.analysis_axis_combo,
            self.analysis_zoom_combo,
            self.analysis_filter_check,
            self.analysis_shear_check,
            self.analysis_normal_check,
            self.analysis_integration_check,
            self.analysis_marker_check,
            self.analysis_reset_view_btn,
            self.analysis_select_all_btn,
            self.analysis_select_none_btn,
        ):
            widget.setEnabled(not capturing)
        for check in self.analysis_channel_checks.values():
            check.setEnabled(not capturing)
        self.on_analysis_source_changed()

    def _analysis_has_in_memory_capture(self) -> bool:
        try:
            if getattr(self, "raw_data_buffer", None) is None or getattr(self, "sweep_timestamps_buffer", None) is None:
                return False
            return int(getattr(self, "sweep_count", 0) or 0) > 0
        except Exception:
            return False

    def _set_analysis_source_item_enabled(self, index: int, enabled: bool) -> None:
        item = self.analysis_source_combo.model().item(index)
        if item is None:
            return
        flags = item.flags()
        if enabled:
            item.setFlags(flags | Qt.ItemFlag.ItemIsEnabled)
        else:
            item.setFlags(flags & ~Qt.ItemFlag.ItemIsEnabled)
