"""Analysis-tab Burst Score / gating v2 preview: settings group, plots and the mask on the raw signal.

All gate decisions come from ``data_processing.gating_preview`` (live's own ``V2Gating``), computed
by ``AnalysisComputeWorker`` like every other Analysis plot. This mixin only owns the widgets, their
persisted state, the request it hands to that worker, and drawing the returned result.
"""

from __future__ import annotations

from dataclasses import asdict, replace

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.texture_piezo.application.inference_config import InferenceConfig
from data_processing.gating_preview import GatePreview, GatePreviewSettings, GatingPreviewData, LiveGateParams

GATING_PREVIEW_STATE_KEY = "gating_preview"
IDLE_LABEL_CLASS = "baseline"
STRONG_RGB = (220, 90, 40)
WEAK_RGB = (150, 150, 150)
STAGE_GOOD_RGBA = (60, 180, 75, 60)
ACCEPTED_MASK_RGBA = (40, 170, 70, 55)
REJECTED_MASK_RGBA = (120, 120, 120, 70)
LANES = (("strength", 3), ("idle", 2), ("combined", 1), ("accepted", 0))
LANE_RGB = {"strength": (220, 90, 40), "idle": (60, 120, 220), "combined": (150, 80, 190), "accepted": (40, 170, 70)}
LANE_HALF_HEIGHT = 0.35
MASK_Z = -20


class GatingPreviewPanelMixin:
    def _init_gating_preview_state(self) -> dict:
        return {"enabled": False, **asdict(GatePreviewSettings())}

    # ---- widgets ---------------------------------------------------------------------------

    def create_gating_preview_group(self) -> QGroupBox:
        group = QGroupBox("Burst Score / Gating v2 Preview")
        layout = QVBoxLayout(group)
        self.analysis_burst_check = QCheckBox("Enable Burst Score")
        self.analysis_burst_check.setToolTip(
            "Run TouchID gating v2 over the loaded capture: plot the per-block burst score, each gate "
            "stage's good stretches, and mask the finally accepted signal on the raw plot."
        )
        self.analysis_burst_check.stateChanged.connect(self.on_gating_preview_settings_changed)
        layout.addWidget(self.analysis_burst_check)

        self.analysis_burst_values_widget = QWidget()
        grid = QGridLayout(self.analysis_burst_values_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.analysis_burst_values_widget)

        self.analysis_strength_gate_check = self._gating_check("Strength gate", "Sustained |normal_jerk| bursts.")
        grid.addWidget(self.analysis_strength_gate_check, 0, 0)
        self.analysis_burst_block_spin = self._gating_spin(QDoubleSpinBox(), 0.005, 5.0, 3, " s")
        self.analysis_burst_min_spin = self._gating_spin(QDoubleSpinBox(), 0.0, 1000.0, 4)
        self.analysis_burst_sustain_min_spin = self._gating_spin(QSpinBox(), 0, 1000)
        self.analysis_burst_sustain_blocks_spin = self._gating_spin(QSpinBox(), 1, 1000)
        for column, (label, spin, tip) in enumerate((
            ("Block (s)", self.analysis_burst_block_spin, "Block length the |normal_jerk| sum is taken over."),
            ("Burst min", self.analysis_burst_min_spin, "A block is strong when its sum is above this."),
            ("Sustain min", self.analysis_burst_sustain_min_spin,
             "Good when MORE than this many of the last Sustain blocks blocks are strong."),
            ("Sustain blocks", self.analysis_burst_sustain_blocks_spin, "Lookback length in blocks."),
        )):
            spin.setToolTip(tip)
            grid.addWidget(QLabel(label), 1, column)
            grid.addWidget(spin, 2, column)

        self.analysis_idle_gate_check = self._gating_check(
            "Idle gate", "Active chunks vs the TouchID tab's captured idle baseline (needs one)."
        )
        grid.addWidget(self.analysis_idle_gate_check, 3, 0)
        self.analysis_concat_check = self._gating_check(
            "Concatenate", "Let a window be made of several non-adjacent good stretches."
        )
        grid.addWidget(self.analysis_concat_check, 3, 1)
        self.analysis_expiry_spin = self._gating_spin(QDoubleSpinBox(), 0.01, 600.0, 2, " s")
        self.analysis_expiry_spin.setToolTip("Good stretches older than this are dropped; must be >= the window size.")
        grid.addWidget(QLabel("Expiry (s)"), 3, 2)
        grid.addWidget(self.analysis_expiry_spin, 3, 3)

        self.analysis_gating_logic_label = QLabel("")
        self.analysis_gating_logic_label.setWordWrap(True)
        self.analysis_gating_logic_label.setStyleSheet("font-family: monospace;")
        grid.addWidget(self.analysis_gating_logic_label, 4, 0, 1, 4)
        return group

    def _gating_check(self, text: str, tooltip: str) -> QCheckBox:
        check = QCheckBox(text)
        check.setToolTip(tooltip)
        check.stateChanged.connect(self.on_gating_preview_settings_changed)
        return check

    def _gating_spin(self, spin, low, high, decimals=None, suffix=""):
        spin.setRange(low, high)
        if decimals is not None:
            spin.setDecimals(decimals)
            spin.setSingleStep(10 ** -min(decimals, 2))
        if suffix:
            spin.setSuffix(suffix)
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(self.on_gating_preview_settings_changed)
        return spin

    def create_gating_preview_plots(self, signal_plot) -> None:
        for name, height in (("analysis_burst_plot", 150), ("analysis_rolling_plot", 150), ("analysis_gate_plot", 130)):
            plot = pg.PlotWidget()
            plot.setBackground("w")
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setMinimumHeight(height)
            plot.setXLink(signal_plot)
            plot.setVisible(False)
            setattr(self, name, plot)
        self.analysis_gate_plot.getAxis("left").setTicks([[(lane, name) for name, lane in LANES]])
        self._gating_mask_items: list = []

    # ---- state -----------------------------------------------------------------------------

    def _gating_preview_settings(self) -> GatePreviewSettings:
        return GatePreviewSettings(
            block_s=float(self.analysis_burst_block_spin.value()),
            burst_min=float(self.analysis_burst_min_spin.value()),
            sustain_min=int(self.analysis_burst_sustain_min_spin.value()),
            sustain_blocks=int(self.analysis_burst_sustain_blocks_spin.value()),
            strength_enabled=self.analysis_strength_gate_check.isChecked(),
            idle_enabled=self.analysis_idle_gate_check.isChecked(),
            concat_enabled=self.analysis_concat_check.isChecked(),
            expiry_s=float(self.analysis_expiry_spin.value()),
        )

    def apply_gating_preview_state_to_widgets(self) -> None:
        state = {**self._init_gating_preview_state(), **self.analysis_state.get(GATING_PREVIEW_STATE_KEY, {})}
        self.analysis_burst_check.setChecked(bool(state["enabled"]))
        self.analysis_burst_values_widget.setVisible(self.analysis_burst_check.isChecked())
        self.analysis_burst_block_spin.setValue(float(state["block_s"]))
        self.analysis_burst_min_spin.setValue(float(state["burst_min"]))
        self.analysis_burst_sustain_min_spin.setValue(int(state["sustain_min"]))
        self.analysis_burst_sustain_blocks_spin.setValue(int(state["sustain_blocks"]))
        self.analysis_strength_gate_check.setChecked(bool(state["strength_enabled"]))
        self.analysis_idle_gate_check.setChecked(bool(state["idle_enabled"]))
        self.analysis_concat_check.setChecked(bool(state["concat_enabled"]))
        self.analysis_expiry_spin.setValue(float(state["expiry_s"]))

    def on_gating_preview_settings_changed(self, *_args) -> None:
        if getattr(self, "_analysis_settings_loading", False) or not hasattr(self, "analysis_gate_plot"):
            return
        self.analysis_burst_values_widget.setVisible(self.analysis_burst_check.isChecked())
        self.analysis_state[GATING_PREVIEW_STATE_KEY] = {
            "enabled": bool(self.analysis_burst_check.isChecked()),
            **asdict(self._gating_preview_settings()),
        }
        self.save_last_analysis_settings()
        self.refresh_analysis_plot()

    def gating_preview_request(self) -> dict | None:
        """What AnalysisComputeWorker needs to compute the preview; None while it is off.
        Live values are copied here because the worker reads them on another thread."""
        if not self.analysis_burst_check.isChecked():
            return None
        live = self._gating_live_params()
        baseline = None if live.idle_baseline is None else replace(live.idle_baseline)
        return {
            "settings": self._gating_preview_settings(), "live": replace(live, idle_baseline=baseline),
            "label_spans": self._gating_label_spans(),
        }

    def _gating_label_spans(self) -> tuple[tuple[float, float], ...]:
        """Hand-labelled touch segments in seconds; the idle `baseline` class is not a touch."""
        segments = getattr(self, "analysis_label_segments", None) or []
        return tuple(
            (float(segment["start_s"]), float(segment["end_s"]))
            for segment in segments if segment["class"] != IDLE_LABEL_CLASS
        )

    # ---- drawing (GUI thread: items only, no computation) -----------------------------------

    def render_gating_preview(self, gating: GatingPreviewData | None) -> dict[str, bool]:
        """Draw a worker-computed preview; returns which preview plots to show, by plot key."""
        self._clear_gating_preview()
        if gating is None:
            return {}
        preview = gating.preview
        self.analysis_gating_logic_label.setText(preview.error or preview.logic_text)
        if preview.error:
            return {}
        if gating.burst is not None:
            self._draw_burst_plot(gating.burst)
            self._draw_rolling_plot(gating.burst, gating.x)
        self._draw_stage_lanes(preview)
        self._draw_signal_mask(preview, gating.x)
        self._label_gating_plots()
        has_burst = gating.burst is not None
        return {"burst": has_burst, "rolling": has_burst, "gate": True}

    def _clear_gating_preview(self) -> None:
        self.analysis_burst_plot.clear()
        self.analysis_rolling_plot.clear()
        self.analysis_gate_plot.clear()
        for item in self._gating_mask_items:
            self.analysis_signal_plot.removeItem(item)
        self._gating_mask_items = []

    def _gating_live_params(self) -> LiveGateParams:
        config = getattr(self, "touchid_config", None) or InferenceConfig()
        return LiveGateParams(
            window_size_s=config.window_size_s, hop_size_s=config.hop_size_s, onset_skip_s=config.onset_skip_s,
            idle_baseline=getattr(self, "touchid_idle_baseline", None),
        )

    def _draw_burst_plot(self, series) -> None:
        plot = self.analysis_burst_plot
        for start, end in series.good_x_ranges:
            plot.addItem(self._shaded_region(start, end, STAGE_GOOD_RGBA, z=-10))
        widths = series.block_x1 - series.block_x0
        for strong, color in ((True, STRONG_RGB), (False, WEAK_RGB)):
            picked = series.is_strong == strong
            plot.addItem(pg.BarGraphItem(
                x0=series.block_x0[picked], width=widths[picked], height=series.scores[picked],
                brush=pg.mkBrush(*color), pen=pg.mkPen(None),
            ))
        plot.addItem(pg.InfiniteLine(
            pos=series.burst_min, angle=0, pen=pg.mkPen((200, 0, 0), width=2, style=Qt.PenStyle.DashLine),
            label="burst_min", labelOpts={"position": 0.05, "color": (200, 0, 0)},
        ))

    def _draw_rolling_plot(self, series, x: np.ndarray) -> None:
        """Sum of |normal_jerk| over the trailing block_s at every sample: the burst score as a line."""
        plot = self.analysis_rolling_plot
        curve = plot.plot(x, series.rolling_sum, pen=pg.mkPen(STRONG_RGB, width=1))
        curve.setClipToView(True)
        curve.setDownsampling(auto=True, method="peak")
        plot.addItem(pg.InfiniteLine(
            pos=series.burst_min, angle=0, pen=pg.mkPen((200, 0, 0), width=2, style=Qt.PenStyle.DashLine),
            label="burst_min", labelOpts={"position": 0.05, "color": (200, 0, 0)},
        ))

    def _draw_stage_lanes(self, preview: GatePreview) -> None:
        for name, lane in LANES:
            spans = getattr(preview, name)
            if not spans:
                continue
            starts = np.array([start for start, _end in spans])
            ends = np.array([end for _start, end in spans])
            self.analysis_gate_plot.addItem(pg.BarGraphItem(
                x0=starts, x1=ends, y0=lane - LANE_HALF_HEIGHT, y1=lane + LANE_HALF_HEIGHT,
                brush=pg.mkBrush(*LANE_RGB[name]), pen=pg.mkPen(None),
            ))
        self.analysis_gate_plot.setYRange(-0.6, 3.6, padding=0)

    def _draw_signal_mask(self, preview: GatePreview, x: np.ndarray) -> None:
        """Green over what reached the classifier, grey over what the gate threw away."""
        if not len(x):
            return
        cursor = float(x[0])
        for start, end in preview.accepted:
            if start > cursor:
                self._add_mask(cursor, start, REJECTED_MASK_RGBA)
            self._add_mask(start, end, ACCEPTED_MASK_RGBA)
            cursor = end
        if cursor < float(x[-1]):
            self._add_mask(cursor, float(x[-1]), REJECTED_MASK_RGBA)

    def _add_mask(self, start: float, end: float, rgba) -> None:
        region = self._shaded_region(start, end, rgba, z=MASK_Z)
        self.analysis_signal_plot.addItem(region)
        self._gating_mask_items.append(region)

    @staticmethod
    def _shaded_region(start: float, end: float, rgba, z: int) -> pg.LinearRegionItem:
        region = pg.LinearRegionItem(values=(start, end), brush=pg.mkBrush(*rgba), movable=False)
        region.setZValue(z)
        for line in region.lines:
            line.setPen(pg.mkPen(None))
        return region

    def _label_gating_plots(self) -> None:
        prepared = self.analysis_prepared
        self.analysis_burst_plot.setLabel("left", "Σ|normal_jerk| per block", units="V")
        self.analysis_rolling_plot.setLabel("left", "Σ|normal_jerk| over past block_s", units="V")
        self.analysis_gate_plot.setLabel("left", "Gate stages")
        if prepared is not None:
            self.analysis_gate_plot.setLabel("bottom", prepared.x_label, units=prepared.x_units)
            self.analysis_burst_plot.setLabel("bottom", prepared.x_label, units=prepared.x_units)
            self.analysis_rolling_plot.setLabel("bottom", prepared.x_label, units=prepared.x_units)
