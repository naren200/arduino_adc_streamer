"""Analysis-tab gate preview: a read-only replica of what TouchID "Run on Analysis Source" windows.

The setup comes from the TouchID tab (never from controls here), the replay runs on
``AnalysisComputeWorker`` through the shared ``ReplayDriver`` (``data_processing.gate_replay``),
and this mixin only renders the returned ``GateReplayResult``: the gate lanes, the mask on the raw
signal and the burst-score plots. Settings are shown as text; they are edited on the TouchID tab.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QCheckBox, QGroupBox, QLabel, QVBoxLayout

from core.texture_piezo.application.gating_trace import COMBINED_LANE, IDLE_LANE, STRENGTH_LANE
from core.texture_piezo.application.replay_setup import (
    BASELINE_FROM_FILE_LABEL, BASELINE_FROM_GLOBAL, FILE_BASELINE_CLASS, ReplaySetup,
)
from data_processing.replay_support import replay_unsupported_reason
from data_processing.gate_replay import GateReplayResult, label_overlap_line, result_line

ACCEPTED_LANE = "accepted"
GATE_STAGE_LANES = (STRENGTH_LANE, IDLE_LANE, COMBINED_LANE)
BASELINE_SOURCE_TEXT = {
    BASELINE_FROM_FILE_LABEL: "this file's 'baseline' label",
    BASELINE_FROM_GLOBAL: "the TouchID tab's baseline",
    "none": "none",
}
STRONG_RGB = (220, 90, 40)
WEAK_RGB = (150, 150, 150)
STAGE_GOOD_RGBA = (60, 180, 75, 60)
ACCEPTED_MASK_RGBA = (40, 170, 70, 55)
REJECTED_MASK_RGBA = (120, 120, 120, 70)
LANE_RGB = {
    STRENGTH_LANE: (220, 90, 40), IDLE_LANE: (60, 120, 220), COMBINED_LANE: (150, 80, 190), ACCEPTED_LANE: (40, 170, 70),
}
LANE_HALF_HEIGHT = 0.35
MASK_Z = -20
BANNER_STYLE = "background-color: #fff3cd; color: #664d03; border: 1px solid #ffe69c; padding: 4px;"
STALE_PREDICTIONS_TEXT = "Predictions are from different settings than the gate overlay."
EDIT_ON_TOUCHID_TEXT = "Read-only: edit these settings on the TouchID tab."


class GatingPreviewPanelMixin:
    # ---- widgets ---------------------------------------------------------------------------

    def create_gating_preview_group(self) -> QGroupBox:
        group = QGroupBox("Burst Score / Gate Preview")
        layout = QVBoxLayout(group)
        self.analysis_burst_check = QCheckBox("Enable Burst Score")
        self.analysis_burst_check.setToolTip(
            "Replay the loaded capture through the TouchID tab's gating, exactly as 'Run on Analysis Source' "
            "would, and draw what it decided: the gate lanes, the accepted signal and the burst scores."
        )
        self.analysis_burst_check.stateChanged.connect(self.on_gating_preview_toggled)
        layout.addWidget(self.analysis_burst_check)
        self.analysis_gating_baseline_banner = self._gating_banner()
        self.analysis_gating_stale_banner = self._gating_banner()
        self.analysis_gating_info_label = QLabel("")
        self.analysis_gating_info_label.setWordWrap(True)
        self.analysis_gating_info_label.setStyleSheet("font-family: monospace;")
        self.analysis_gating_info_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for widget in (self.analysis_gating_baseline_banner, self.analysis_gating_stale_banner,
                       self.analysis_gating_info_label):
            layout.addWidget(widget)
        self._gating_header = ""
        return group

    @staticmethod
    def _gating_banner() -> QLabel:
        banner = QLabel("")
        banner.setWordWrap(True)
        banner.setStyleSheet(BANNER_STYLE)
        banner.setVisible(False)
        return banner

    def create_gating_preview_plots(self, signal_plot) -> None:
        for name, height in (("analysis_burst_plot", 150), ("analysis_rolling_plot", 150), ("analysis_gate_plot", 130)):
            plot = pg.PlotWidget()
            plot.setBackground("w")
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setMinimumHeight(height)
            plot.setXLink(signal_plot)
            plot.setVisible(False)
            setattr(self, name, plot)
        self._gating_mask_items: list = []

    # ---- request and banners (GUI thread) --------------------------------------------------

    def on_gating_preview_toggled(self, *_args) -> None:
        if hasattr(self, "analysis_gate_plot"):
            self.refresh_analysis_plot()

    def gating_preview_request(self) -> ReplaySetup | None:
        """The setup the worker replays under; None when the preview is off, cannot run, or must not
        draw an overlay (a required idle baseline is missing)."""
        current = self._current_gate_replay_or_report()
        if current is None:
            return None
        setup, baseline_source = current
        self._gating_header = self._gating_header_text(setup, baseline_source)
        self.analysis_gating_info_label.setText(self._gating_header)
        if setup.is_missing_required_baseline:
            return None
        return setup.with_detached_baseline()

    def refresh_gating_preview_banners(self) -> None:
        """Re-evaluate the banners without replaying (the predictions or the TouchID settings changed)."""
        self._current_gate_replay_or_report()

    def _current_gate_replay_or_report(self) -> tuple[ReplaySetup, str] | None:
        """The TouchID replay setup and its baseline source for the loaded capture, with the banners
        updated to match; None (banners hidden or explaining why) when there is nothing to replay."""
        snapshot = self.analysis_snapshot
        if snapshot is None or not self.analysis_burst_check.isChecked():
            self._set_gating_banners("", "")
            return None
        unsupported_reason = replay_unsupported_reason(snapshot)
        if unsupported_reason:
            self._set_gating_banners(f"Gate preview unavailable: {unsupported_reason}.", "")
            return None
        try:
            setup, resolved = self._touchid_resolved_replay(snapshot)
        except ValueError as exc:
            self._set_gating_banners(f"Gate preview unavailable: the source lacks a configured channel ({exc}).", "")
            return None
        self._set_gating_banners(self._missing_baseline_text(setup), self._stale_predictions_text(setup))
        return setup, resolved.source

    @staticmethod
    def _missing_baseline_text(setup: ReplaySetup) -> str:
        if not setup.is_missing_required_baseline:
            return ""
        return ("Idle baseline missing: the gate needs one and none exists for this capture. Capture one on the "
                "TouchID tab or label a 'baseline' region here. No gate overlay is drawn.")

    def _stale_predictions_text(self, setup: ReplaySetup) -> str:
        predicted_setup = self.analysis_predicted_setup
        if not self.analysis_predicted_labels or predicted_setup is None or predicted_setup == setup:
            return ""
        return STALE_PREDICTIONS_TEXT

    def _set_gating_banners(self, baseline_text: str, stale_text: str) -> None:
        for banner, text in ((self.analysis_gating_baseline_banner, baseline_text),
                             (self.analysis_gating_stale_banner, stale_text)):
            banner.setText(text)
            banner.setVisible(bool(text))

    @staticmethod
    def _gating_header_text(setup: ReplaySetup, baseline_source: str) -> str:
        return (f"Gating {setup.gating_version}; idle baseline: {BASELINE_SOURCE_TEXT[baseline_source]}\n"
                f"{EDIT_ON_TOUCHID_TEXT}")

    def _gating_label_spans(self, first_s: float, last_s: float) -> list[tuple[float, float]]:
        """Hand-labelled touch segments in absolute seconds, clipped to the replayed window
        [first_s, last_s]; the idle `baseline` class is not a touch."""
        segments = getattr(self, "analysis_label_segments", None) or []
        clipped = (
            (max(float(segment["start_s"]), first_s), min(float(segment["end_s"]), last_s))
            for segment in segments if segment["class"] != FILE_BASELINE_CLASS
        )
        return [(start, end) for start, end in clipped if end > start]

    # ---- drawing (GUI thread: items only, no computation) -----------------------------------

    def render_gating_preview(self, result: GateReplayResult | None) -> dict[str, bool]:
        """Draw a worker-computed replay; returns which preview plots to show, by plot key."""
        self._clear_gating_preview()
        if result is None:
            return {}
        self.analysis_gating_info_label.setText(self._gating_header + "\n" + self._gating_details(result))
        if not len(result.timestamps_s):
            return {}
        lane_names = [name for name in GATE_STAGE_LANES if any(lane.name == name for lane in result.trace.lanes)]
        self._draw_gate_lanes(result, lane_names + [ACCEPTED_LANE])
        self._draw_signal_mask(result)
        self._label_gating_plots()
        has_burst = result.burst is not None
        if has_burst:
            self._draw_burst_plot(result)
            self._draw_rolling_plot(result)
        return {"burst": has_burst, "rolling": has_burst, "gate": True}

    def _gating_details(self, result: GateReplayResult) -> str:
        lines = [result.logic_text, result_line(result)]
        label_spans = self._gating_label_spans(float(result.timestamps_s[0]), float(result.timestamps_s[-1]))
        if label_spans:
            lines.append(label_overlap_line(result.accepted_spans_s(), label_spans))
        return "\n".join(line for line in lines if line)

    def _clear_gating_preview(self) -> None:
        self.analysis_burst_plot.clear()
        self.analysis_rolling_plot.clear()
        self.analysis_gate_plot.clear()
        for item in self._gating_mask_items:
            self.analysis_signal_plot.removeItem(item)
        self._gating_mask_items = []

    def _gating_x(self, seconds):
        """Capture seconds (scalar or array) in the plot's x units, the same conversion the labels use."""
        return self._analysis_label_seconds_to_x(seconds)

    def _gating_x_spans(self, spans_s) -> list[tuple[float, float]]:
        return [(float(self._gating_x(start)), float(self._gating_x(end))) for start, end in spans_s]

    def _draw_burst_plot(self, result: GateReplayResult) -> None:
        plot, series = self.analysis_burst_plot, result.burst
        for start, end in self._gating_x_spans(result.lane_spans_s(STRENGTH_LANE)):
            plot.addItem(self._shaded_region(start, end, STAGE_GOOD_RGBA, z=-10))
        starts, ends = self._gating_x(series.block_start_s), self._gating_x(series.block_end_s)
        for strong, color in ((True, STRONG_RGB), (False, WEAK_RGB)):
            picked = series.is_strong == strong
            plot.addItem(pg.BarGraphItem(
                x0=starts[picked], width=(ends - starts)[picked], height=series.scores[picked],
                brush=pg.mkBrush(*color), pen=pg.mkPen(None),
            ))
        plot.addItem(self._burst_min_line(series.burst_min))

    def _draw_rolling_plot(self, result: GateReplayResult) -> None:
        """Sum of |normal_jerk| over the trailing block_s at every sample: the burst score as a line."""
        plot, series = self.analysis_rolling_plot, result.burst
        curve = plot.plot(self._gating_x(result.timestamps_s), series.rolling_sum, pen=pg.mkPen(STRONG_RGB, width=1))
        curve.setClipToView(True)
        curve.setDownsampling(auto=True, method="peak")
        plot.addItem(self._burst_min_line(series.burst_min))

    @staticmethod
    def _burst_min_line(burst_min: float) -> pg.InfiniteLine:
        return pg.InfiniteLine(
            pos=burst_min, angle=0, pen=pg.mkPen((200, 0, 0), width=2, style=Qt.PenStyle.DashLine),
            label="burst_min", labelOpts={"position": 0.05, "color": (200, 0, 0)},
        )

    def _draw_gate_lanes(self, result: GateReplayResult, lane_names: list[str]) -> None:
        plot = self.analysis_gate_plot
        row_of = {name: len(lane_names) - 1 - index for index, name in enumerate(lane_names)}
        for name, row in row_of.items():
            spans = self._gating_x_spans(
                result.accepted_spans_s() if name == ACCEPTED_LANE else result.lane_spans_s(name))
            if not spans:
                continue
            plot.addItem(pg.BarGraphItem(
                x0=np.array([start for start, _end in spans]), x1=np.array([end for _start, end in spans]),
                y0=row - LANE_HALF_HEIGHT, y1=row + LANE_HALF_HEIGHT,
                brush=pg.mkBrush(*LANE_RGB[name]), pen=pg.mkPen(None),
            ))
        plot.getAxis("left").setTicks([[(row, name) for name, row in row_of.items()]])
        plot.setYRange(-0.6, len(lane_names) - 0.4, padding=0)

    def _draw_signal_mask(self, result: GateReplayResult) -> None:
        """Green over what reached the classifier, grey over what the gate threw away."""
        first, last = (float(self._gating_x(result.timestamps_s[index])) for index in (0, -1))
        cursor = first
        for start, end in self._gating_x_spans(result.accepted_spans_s()):
            if start > cursor:
                self._add_mask(cursor, start, REJECTED_MASK_RGBA)
            self._add_mask(start, end, ACCEPTED_MASK_RGBA)
            cursor = end
        if cursor < last:
            self._add_mask(cursor, last, REJECTED_MASK_RGBA)

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
            for plot in (self.analysis_gate_plot, self.analysis_burst_plot, self.analysis_rolling_plot):
                plot.setLabel("bottom", prepared.x_label, units=prepared.x_units)
