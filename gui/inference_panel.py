"""
Inference (TouchID) Panel GUI Component
========================================
Live texture-classification tab: buffers incoming PZT sweeps, runs the
texture_piezo feature pipeline + ANN v2 model on a rolling window, and
displays smoothed class probabilities.
"""

import sys
import time

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import re

from inference._paths import TEXTURE_PIEZO_SRC
from inference.buffer import RollingBuffer
from inference.classifier import TextureClassifier
from inference.classify_worker import TouchIdClassifyWorker
from inference.config import (
    InferenceConfig,
    discover_checkpoints,
    discover_model_versions,
    load_touchid_settings,
    model_checkpoint_of,
    model_version_of,
    pzt_columns_for_sensor,
    pzt_sensor_number_of,
    save_touchid_settings,
    set_model_version,
)
from inference.architectures import ARCH_REGISTRY, CHECKPOINT_DEFAULT
from inference.offline import run_inference_on_snapshot
from inference.quality_gate import (
    IDLE_CAPTURE_DURATION_S,
    MAX_WINDOW_IDLE_FRACTION,
    MICRO_CHUNK_S,
    fit_idle_baseline,
    load_idle_baseline,
    save_idle_baseline,
    window_idle_fraction,
)
from inference.segmentation import ActiveSampleQueue
from inference.smoothing import ConfidenceSmoother
from constants.plotting import PLOT_COLORS

sys.path.insert(0, str(TEXTURE_PIEZO_SRC))
from causal_derived_channels import CausalDerivedChannels  # noqa: E402

# Display label (combo box text) -> InferenceConfig.model_type key. Built from
# ARCH_REGISTRY so a new architecture registered there (architectures.py)
# shows up in the combo automatically -- no GUI change needed to add one.
_MODEL_TYPE_DISPLAY_NAMES = {
    "ann": "ANN", "cnn": "CNN", "quad": "Quad", "penta": "Penta",
}
_MODEL_TYPE_LABELS = [_MODEL_TYPE_DISPLAY_NAMES.get(key, key.upper()) for key in ARCH_REGISTRY]
_MODEL_TYPE_BY_LABEL = {label: key for key, label in zip(ARCH_REGISTRY, _MODEL_TYPE_LABELS)}

_PZT_SENSOR_LABEL_RE = re.compile(r"^PZT(\d+)_[BLCRT]$")
_FALLBACK_PZT_SENSOR_NUMBERS = ["3", "4", "5"]

# Bar-chart coloring for the confidence-threshold display: a class's bar is
# red once its probability reaches touchid_config.confidence_threshold,
# gray below it.
_TOUCHID_ABOVE_THRESHOLD_COLOR = "#cc0000"
_TOUCHID_BELOW_THRESHOLD_COLOR = "#999999"

# "Model loaded" status color. A dark #006600 reads fine on Fusion's light
# palette but is nearly invisible against Fusion's dark palette (which Qt6
# picks up from Windows 11's system dark mode) -- this brighter green keeps
# contrast in both.
_TOUCHID_STATUS_OK_COLOR = "#33cc33"

# How long the idle gate can keep skipping windows before the live
# "Prediction" readout (and its EMA) is cleared, rather than sitting frozen
# on a stale confidence from whenever a window last actually qualified for
# classification. Does NOT affect "Last Detected", which is meant to persist.
_TOUCHID_PREDICTION_STALE_TIMEOUT_S = 1.5

# How much real elapsed capture time the Signal Stream plot keeps visible at
# once. Deliberately longer than one inference window (window_size_s, e.g.
# 0.5s) so the plot scrolls forward showing recent history instead of
# snapping back to a 0-reset x-axis every hop.
_TOUCHID_STREAM_HISTORY_S = 5.0

# Alpha (0-255) for the "being inferenced" region highlight -- low enough
# that the underlying signal curves stay clearly visible through it.
_TOUCHID_REGION_ALPHA = 60

# Fallback region color for the no-idle-baseline fixed-grid branch, which has
# no span concept at all (touchid_active_queue is never used there -- every
# hop just classifies its own fixed window_size_s/hop_size_s slice). Cycling
# through PLOT_COLORS per-window there would suggest segment boundaries that
# don't actually exist (consecutive windows aren't grouped into real touch
# events), so it deliberately stays a single flat color instead, same as
# before this per-segment coloring was added.
_TOUCHID_REGION_FALLBACK_COLOR = (255, 235, 59)


class InferencePanelMixin:
    """Mixin providing the TouchID live-classification tab."""

    def init_touchid_state(self):
        """Load config, build the inference pipeline objects, and set up the timer.

        Called once from __init__, before create_touchid_tab().
        """
        self.touchid_config = InferenceConfig()
        try:
            loaded = load_touchid_settings(self.touchid_config)
            if loaded is not None:
                self.touchid_config = loaded
        except Exception:
            pass

        self.touchid_buffer = RollingBuffer(
            n_channels=len(self.touchid_config.pzt_columns),
            window_size_s=self.touchid_config.window_size_s,
            hop_size_s=self.touchid_config.hop_size_s,
        )
        # Persistent streaming state for the "integrated"/shear/normal
        # derived channels -- one CausalDerivedChannels instance for the
        # whole live session, .process()'d on each newly-pushed chunk so its
        # bounded windowed sums and unbounded causal medians carry forward
        # continuously instead of restarting every window (see
        # texture_piezo/src/causal_derived_channels.py). touchid_derived_buffer
        # is a second RollingBuffer, pushed in lockstep with touchid_buffer on
        # the SAME hop cadence, so get_window() on both together yields
        # perfectly aligned raw-ADC and derived-channel slices for one window.
        self.touchid_derived_channels = CausalDerivedChannels(pzt_columns=self.touchid_config.pzt_columns)
        self.touchid_derived_buffer = RollingBuffer(
            n_channels=len(self.touchid_config.pzt_columns) + 3,
            window_size_s=self.touchid_config.window_size_s,
            hop_size_s=self.touchid_config.hop_size_s,
        )
        self.touchid_smoother = ConfidenceSmoother(
            class_names=self.touchid_config.class_names,
            alpha=self.touchid_config.smoothing_alpha,
        )

        self.touchid_classifier = None
        self.touchid_classifier_error = None
        try:
            self.touchid_classifier = TextureClassifier(self.touchid_config)
        except Exception as exc:
            # Missing/incompatible model artifacts must not crash GUI construction —
            # the tab degrades to a status message and no-ops the per-hop prediction.
            self.touchid_classifier_error = str(exc)

        self.touchid_show_smoothed = True

        # Latched "last detected" state: only updated when a prediction's top
        # confidence crosses touchid_config.confidence_threshold, and left
        # untouched otherwise so it keeps showing the last texture that was
        # confidently recognized instead of flickering with low-confidence noise.
        self.touchid_last_confident_class = None
        self.touchid_last_confident_conf = 0.0

        # Tracks when classify_window last actually ran, so a sustained run
        # of gate-skipped windows can clear the stale live "Prediction" +
        # EMA instead of leaving them frozen on an old confidence (see
        # _touchid_maybe_clear_stale_prediction). None means "never yet" /
        # already cleared, so the very first skip after startup or a clear
        # doesn't need to wait out the timeout again.
        self.touchid_last_classification_time = None

        # Idle quality gate: persisted per-channel mean/std baseline from a
        # dedicated no-contact capture, used to skip inference on windows
        # that aren't a fully-filled clip of real signal (see quality_gate.py).
        # None until the user records a baseline (or a stale one for a
        # different pzt_columns set is discarded) -- the gate is a no-op
        # until then, so existing behavior is unchanged for anyone who
        # hasn't captured one yet.
        self.touchid_idle_baseline = load_idle_baseline(self.touchid_config.pzt_columns)
        self.touchid_idle_capture_active = False
        self.touchid_idle_capture_samples: list[np.ndarray] = []
        self.touchid_idle_capture_n_samples = 0

        # Sample-accurate segmentation (inference/segmentation.py), used only
        # once an idle baseline is available -- with no baseline, the
        # touchid_buffer/touchid_derived_buffer RollingBuffer path below is
        # used unchanged (classify every fixed hop-grid window, matching
        # is_window_quality's old no-op-without-a-baseline behavior). Built
        # lazily (touchid_active_queue stays None) since it needs a measured
        # fs, which isn't known until streaming has started.
        self._touchid_store_reset()

        # Rolling history feeding the Signal Stream plot -- separate from
        # touchid_buffer (which only holds one inference window's worth and
        # is consumed/advanced by RollingBuffer.get_window). Keeps the last
        # _TOUCHID_STREAM_HISTORY_S seconds of real elapsed capture time so
        # the plot scrolls forward instead of resetting its x-axis every hop.
        self._touchid_reset_stream_display()

        self.touchid_timer = QTimer()
        self.touchid_timer.timeout.connect(self.update_touchid_display)
        self.touchid_timer.setInterval(max(1, int(self.touchid_config.hop_size_s * 1000)))

        # Classification runs on a worker thread (see classify_worker.py) so a
        # slow model never blocks the plot/buffer loop above. touchid_worker_busy
        # tracks whether a submitted window's result is still pending -- while
        # busy, new quality-gated windows still light up the "being inferenced"
        # highlight (they were genuinely eligible) but aren't queued for
        # classification, since the worker's queue already drops anything
        # older than its single pending slot.
        self.touchid_worker_busy = False
        self.touchid_classify_worker = TouchIdClassifyWorker()
        self.touchid_classify_worker.result_ready.connect(self._on_touchid_classified)
        self.touchid_classify_worker.error_occurred.connect(self._on_touchid_classify_error)
        self.touchid_classify_worker.start()

    def shutdown_touchid_worker(self):
        worker = getattr(self, "touchid_classify_worker", None)
        if worker is not None:
            worker.stop()
            worker.wait(1500)

    def sync_touchid_timer_state(self):
        """Start/stop touchid_timer to match "TouchID is the current tab".

        Called from the visualization tab-change handler, but ALSO needs
        calling whenever is_capturing flips -- if the user is already sitting
        on the TouchID tab (e.g. opened it before clicking Start Capture),
        no tab-change signal ever fires once capture starts, so without this
        second call site the timer would never start and the tab would sit
        silently frozen (empty plot, rate label stuck on '-') even though
        should_update_touchid_display() would otherwise happily return True.
        """
        if not hasattr(self, 'visualization_tabs') or self.visualization_tabs is None:
            return
        if not hasattr(self, 'touchid_timer'):
            return
        current_tab = self.visualization_tabs.tabText(self.visualization_tabs.currentIndex())
        if current_tab == 'TouchID':
            if not self.touchid_timer.isActive():
                self.touchid_timer.start()
        else:
            if self.touchid_timer.isActive():
                self.touchid_timer.stop()

    def should_update_touchid_display(self) -> bool:
        """Gate both the buffer push and the display refresh behind tab visibility."""
        if not hasattr(self, 'visualization_tabs') or self.visualization_tabs is None:
            return False
        if not getattr(self, 'is_capturing', False):
            return False
        return self.visualization_tabs.tabText(self.visualization_tabs.currentIndex()) == 'TouchID'

    def create_touchid_tab(self) -> QWidget:
        tab = QWidget()
        root_layout = QVBoxLayout(tab)

        control_group = QGroupBox('TouchID Controls')
        control_layout = QGridLayout(control_group)

        control_layout.addWidget(QLabel('Window (s):'), 0, 0)
        self.touchid_window_spin = QDoubleSpinBox()
        self.touchid_window_spin.setRange(0.05, 10.0)
        self.touchid_window_spin.setDecimals(4)
        self.touchid_window_spin.setSingleStep(0.1)
        self.touchid_window_spin.setValue(self.touchid_config.window_size_s)
        self.touchid_window_spin.valueChanged.connect(self.on_touchid_window_changed)
        control_layout.addWidget(self.touchid_window_spin, 0, 1)

        control_layout.addWidget(QLabel('Hop (s):'), 0, 2)
        self.touchid_hop_spin = QDoubleSpinBox()
        self.touchid_hop_spin.setRange(0.01, 5.0)
        self.touchid_hop_spin.setDecimals(4)
        self.touchid_hop_spin.setSingleStep(0.01)
        self.touchid_hop_spin.setValue(self.touchid_config.hop_size_s)
        self.touchid_hop_spin.valueChanged.connect(self.on_touchid_hop_changed)
        control_layout.addWidget(self.touchid_hop_spin, 0, 3)

        control_layout.addWidget(QLabel('Smoothing α:'), 0, 4)
        self.touchid_alpha_spin = QDoubleSpinBox()
        self.touchid_alpha_spin.setRange(0.01, 1.0)
        self.touchid_alpha_spin.setDecimals(2)
        self.touchid_alpha_spin.setSingleStep(0.05)
        self.touchid_alpha_spin.setValue(self.touchid_config.smoothing_alpha)
        self.touchid_alpha_spin.valueChanged.connect(self.on_touchid_alpha_changed)
        control_layout.addWidget(self.touchid_alpha_spin, 0, 5)

        self.touchid_smoothed_check = QCheckBox('Show smoothed')
        self.touchid_smoothed_check.setChecked(True)
        self.touchid_smoothed_check.stateChanged.connect(self.on_touchid_smoothed_toggled)
        control_layout.addWidget(self.touchid_smoothed_check, 0, 6)

        control_layout.addWidget(QLabel('Model:'), 0, 7)
        self.touchid_model_type_combo = QComboBox()
        self.touchid_model_type_combo.addItems(_MODEL_TYPE_LABELS)
        self.touchid_model_type_combo.setCurrentText(
            _MODEL_TYPE_DISPLAY_NAMES.get(self.touchid_config.model_type, self.touchid_config.model_type.upper()))
        self.touchid_model_type_combo.currentTextChanged.connect(self.on_touchid_model_type_changed)
        control_layout.addWidget(self.touchid_model_type_combo, 0, 8)

        control_layout.addWidget(QLabel('Version:'), 0, 9)
        self.touchid_version_combo = QComboBox()
        self.touchid_version_combo.setToolTip(
            'Weight versions found on disk for the selected architecture (matching model + scaler + norm-stats files).'
        )

        control_layout.addWidget(QLabel('Checkpoint:'), 0, 11)
        self.touchid_checkpoint_combo = QComboBox()
        self.touchid_checkpoint_combo.setToolTip(
            'Which saved checkpoint of the selected version to load (best/final/last), '
            'or "default" when the version has only a single untagged weight file.'
        )

        self._touchid_refresh_version_combo()
        self.touchid_version_combo.currentTextChanged.connect(self.on_touchid_version_changed)
        control_layout.addWidget(self.touchid_version_combo, 0, 10)

        self.touchid_checkpoint_combo.currentTextChanged.connect(self.on_touchid_checkpoint_changed)
        control_layout.addWidget(self.touchid_checkpoint_combo, 0, 12)

        self.touchid_reload_model_btn = QPushButton('Reload Model Weights')
        self.touchid_reload_model_btn.setToolTip(
            'Stop live prediction, reload the active model (ANN or CNN) and scaler/norm-stats from disk, and resume.'
        )
        self.touchid_reload_model_btn.clicked.connect(self.on_touchid_reload_model_clicked)
        control_layout.addWidget(self.touchid_reload_model_btn, 0, 13)

        self.touchid_run_on_source_btn = QPushButton('Run on Analysis Source')
        self.touchid_run_on_source_btn.setToolTip(
            "Run the selected model over whatever is currently loaded in the Analysis tab "
            "(In-memory cache or CSV plus JSON, per its Source selector), then switch to "
            "Analysis to view the predicted labels overlaid on the trace."
        )
        self.touchid_run_on_source_btn.clicked.connect(self.on_touchid_run_on_source_clicked)
        control_layout.addWidget(self.touchid_run_on_source_btn, 0, 14)

        self.touchid_capture_idle_btn = QPushButton(f'Capture Idle Baseline ({IDLE_CAPTURE_DURATION_S:.0f}s)')
        self.touchid_capture_idle_btn.setToolTip(
            "Records this session's no-contact idle noise floor (per channel mean/std) "
            "so inference can skip windows that aren't a fully-filled clip of real "
            "signal. Don't touch the sensor while this runs. Persists across restarts."
        )
        self.touchid_capture_idle_btn.clicked.connect(self.on_touchid_capture_idle_clicked)
        control_layout.addWidget(self.touchid_capture_idle_btn, 0, 15)

        pzt_layout = QHBoxLayout()
        pzt_layout.addWidget(QLabel('PZT sensor:'))
        self.touchid_pzt_sensor_combo = QComboBox()
        self.touchid_pzt_sensor_combo.setToolTip(
            'Which physical PZT sensor board is wired up. Channel names (PZT<n>_B/L/C/R/T) '
            'are derived from this -- texture_piezo\'s feature/shear-normal code needs the '
            'columns that actually match what\'s streaming.'
        )
        self._touchid_refresh_pzt_sensor_combo()
        self.touchid_pzt_sensor_combo.currentTextChanged.connect(self.on_touchid_pzt_sensor_changed)
        pzt_layout.addWidget(self.touchid_pzt_sensor_combo)
        pzt_layout.addStretch()
        control_layout.addLayout(pzt_layout, 1, 0, 1, 10)

        root_layout.addWidget(control_group)

        # Single-line status strip: model status, sample rate, idle gate,
        # threshold control, last-detected latch, and the live top-class
        # readout all side by side -- keeps the whole at-a-glance state on
        # one row instead of spread across several, leaving more vertical
        # room for the plots below.
        summary_row = QHBoxLayout()

        self.touchid_status_label = QLabel(
            'Model not loaded: ' + self.touchid_classifier_error
            if self.touchid_classifier_error
            else f'Model loaded ({self.touchid_config.model_type.upper()})'
        )
        self.touchid_status_label.setStyleSheet(
            'color: #cc0000; font-weight: bold;'
            if self.touchid_classifier_error
            else f'color: {_TOUCHID_STATUS_OK_COLOR}; font-weight: bold;'
        )
        summary_row.addWidget(self.touchid_status_label)

        summary_row.addSpacing(16)
        self.touchid_sample_rate_label = QLabel('Per-channel rate: - Hz')
        self.touchid_sample_rate_label.setToolTip(
            'Measured per-channel sweep rate feeding the TouchID classifier, '
            'same figure as the Time Series tab\'s Per-Channel Rate readout.'
        )
        summary_row.addWidget(self.touchid_sample_rate_label)

        summary_row.addSpacing(16)
        self.touchid_idle_gate_label = QLabel()
        self.touchid_idle_gate_label.setToolTip(
            'Status of the idle quality gate -- whether a baseline is loaded, and '
            'whether the current window is being skipped as not-yet-a-quality-clip.'
        )
        self._update_touchid_idle_gate_label()
        summary_row.addWidget(self.touchid_idle_gate_label)

        summary_row.addSpacing(16)
        summary_row.addWidget(QLabel('Threshold:'))
        self.touchid_threshold_spin = QDoubleSpinBox()
        self.touchid_threshold_spin.setRange(0.0, 1.0)
        self.touchid_threshold_spin.setDecimals(2)
        self.touchid_threshold_spin.setSingleStep(0.05)
        self.touchid_threshold_spin.setValue(self.touchid_config.confidence_threshold)
        self.touchid_threshold_spin.setToolTip(
            "Bar-chart classes at or above this confidence are colored red (recognized); "
            "below it, gray (uncertain). Crossing it also latches the 'Last Detected' readout."
        )
        self.touchid_threshold_spin.valueChanged.connect(self.on_touchid_threshold_changed)
        summary_row.addWidget(self.touchid_threshold_spin)

        summary_row.addSpacing(16)
        summary_row.addWidget(QLabel('Last Detected:'))
        self.touchid_last_detected_label = QLabel('-')
        self.touchid_last_detected_label.setStyleSheet('font-size: 20pt; font-weight: bold; color: #cc0000;')
        summary_row.addWidget(self.touchid_last_detected_label)
        self.touchid_last_detected_confidence_label = QLabel('confidence: -')
        summary_row.addWidget(self.touchid_last_detected_confidence_label)

        summary_row.addSpacing(16)
        summary_row.addWidget(QLabel('Prediction:'))
        self.touchid_class_label = QLabel('-')
        self.touchid_class_label.setStyleSheet('font-size: 13pt; font-weight: bold;')
        summary_row.addWidget(self.touchid_class_label)
        self.touchid_confidence_label = QLabel('confidence: -')
        summary_row.addWidget(self.touchid_confidence_label)

        summary_row.addStretch()
        root_layout.addLayout(summary_row)

        plots_col = QVBoxLayout()

        stream_group = QGroupBox('Signal Stream (TouchID window)')
        stream_layout = QVBoxLayout(stream_group)
        self.touchid_stream_plot_widget = pg.PlotWidget()
        self.touchid_stream_plot_widget.setBackground('w')
        self.touchid_stream_plot_widget.setLabel('left', 'ADC Value', units='counts')
        self.touchid_stream_plot_widget.setLabel('bottom', 'Time', units='s')
        self.touchid_stream_plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.touchid_stream_plot_widget.addLegend(offset=(10, 10))
        # Locked view: this strip chart auto-scrolls/auto-ranges on every hop
        # (see _update_touchid_stream_plot), so letting the user zoom/pan with
        # the mouse would just fight that and immediately snap back anyway --
        # disable it outright rather than leaving a confusing half-interactive plot.
        self.touchid_stream_plot_widget.setMouseEnabled(x=False, y=False)
        self.touchid_stream_plot_widget.getPlotItem().setMenuEnabled(False)
        self.touchid_stream_plot_widget.getPlotItem().hideButtons()
        self.touchid_stream_curves = {}
        stream_layout.addWidget(self.touchid_stream_plot_widget)
        # Wide (full tab width) -- a strip-chart look matching the Time Series
        # tab's plot proportions, stacked above Class Probabilities rather
        # than squeezed beside it.
        stream_group.setMaximumHeight(320)
        plots_col.addWidget(stream_group)

        display_group = QGroupBox('Class Probabilities')
        display_layout = QVBoxLayout(display_group)

        self.touchid_plot_widget = pg.PlotWidget()
        self.touchid_plot_widget.setBackground('w')
        self.touchid_plot_widget.setLabel('left', 'Probability')
        self.touchid_plot_widget.setYRange(0.0, 1.0)
        class_names = self.touchid_config.class_names
        x = np.arange(len(class_names))
        self.touchid_bar_item = pg.BarGraphItem(
            x=x, height=[0.0] * len(class_names), width=0.6,
            brushes=[pg.mkBrush(_TOUCHID_BELOW_THRESHOLD_COLOR)] * len(class_names),
        )
        self.touchid_plot_widget.addItem(self.touchid_bar_item)
        axis = self.touchid_plot_widget.getAxis('bottom')
        axis.setTicks([[(i, name) for i, name in enumerate(class_names)]])
        class_axis_font = QFont()
        class_axis_font.setPointSize(11)
        class_axis_font.setBold(True)
        axis.setStyle(tickFont=class_axis_font)
        display_layout.addWidget(self.touchid_plot_widget)

        plots_col.addWidget(display_group, 1)

        root_layout.addLayout(plots_col, 1)

        return tab

    def _rebuild_touchid_buffers(self):
        """Recreate both rolling buffers at the current window/hop sizing.
        Does NOT reset touchid_derived_channels -- its causal state (bounded
        sums, causal medians) stays valid across a window/hop resize since
        the underlying raw sample stream is unbroken; only the windowing
        (not the derivation) is changing."""
        self.touchid_buffer = RollingBuffer(
            n_channels=len(self.touchid_config.pzt_columns),
            window_size_s=self.touchid_config.window_size_s,
            hop_size_s=self.touchid_config.hop_size_s,
        )
        self.touchid_derived_buffer = RollingBuffer(
            n_channels=len(self.touchid_config.pzt_columns) + 3,
            window_size_s=self.touchid_config.window_size_s,
            hop_size_s=self.touchid_config.hop_size_s,
        )
        # window_size_s/hop_size_s feed directly into ActiveSampleQueue's own
        # sizing (merge-gap threshold, window/hop-in-samples), so a resize
        # needs a fresh queue -- _touchid_store_reset() also drops the
        # continuous store, which is fine since the underlying raw stream
        # is unbroken and will simply refill it (unlike a sensor switch,
        # there's no discontinuity here, just an easier restart than trying
        # to re-derive queue bookkeeping for the old sizing's spans).
        self._touchid_store_reset()

    def _touchid_store_reset(self):
        """(Re)initialize the continuous raw+derived sample store (used only
        once an idle baseline exists) and drop the ActiveSampleQueue built
        on top of it -- it's rebuilt lazily (see _touchid_ensure_active_queue)
        once a measured fs is available again. Called on init, on a
        window/hop resize, on a PZT sensor switch (discontinuous input
        stream), and whenever a new idle baseline is captured."""
        n_pzt = len(self.touchid_config.pzt_columns)
        self.touchid_store_raw = np.empty((0, n_pzt))
        self.touchid_store_integrated = np.empty((0, n_pzt))
        self.touchid_store_shear_lr = np.empty(0)
        self.touchid_store_shear_tb = np.empty(0)
        self.touchid_store_normal = np.empty(0)
        self.touchid_store_ts = np.empty(0)
        self.touchid_store_base_abs = 0  # abs index of store[0]
        self.touchid_store_next_abs = 0  # abs index just past the last appended sample
        self.touchid_chunk_cursor_abs = 0  # abs index up to which micro-chunks have been pushed
        self.touchid_active_queue: ActiveSampleQueue | None = None
        # A fresh queue means span_ids restart from 0 -- reset the color
        # cycle state too so a stale span_id from before the reset can't
        # coincidentally collide with a new span's id.
        self.touchid_region_span_id = None
        self.touchid_region_color_index = -1

    def _touchid_ensure_active_queue(self, fs: float):
        """Lazily build touchid_active_queue on the first tick a measured fs
        is available -- it can't be constructed at init time since fs isn't
        known until streaming has actually started."""
        if self.touchid_active_queue is not None or self.touchid_idle_baseline is None:
            return
        self.touchid_active_queue = ActiveSampleQueue(
            fs=fs,
            window_size_s=self.touchid_config.window_size_s,
            hop_size_s=self.touchid_config.hop_size_s,
            baseline=self.touchid_idle_baseline,
        )
        self.touchid_chunk_cursor_abs = self.touchid_store_next_abs

    def _touchid_append_to_store(self, channel_samples: dict, derived: dict, sweep_timestamps: np.ndarray):
        """Append this tick's newly-pushed raw+derived samples to the
        continuous store, in lockstep, at the running absolute index
        ActiveSampleQueue's yielded (start_idx, end_idx) pairs reference."""
        pzt_columns = self.touchid_config.pzt_columns
        raw = np.stack([channel_samples[col] for col in pzt_columns], axis=1)
        integrated = np.stack([derived['integrated'][col] for col in pzt_columns], axis=1)
        self.touchid_store_raw = np.concatenate([self.touchid_store_raw, raw], axis=0)
        self.touchid_store_integrated = np.concatenate([self.touchid_store_integrated, integrated], axis=0)
        self.touchid_store_shear_lr = np.concatenate([self.touchid_store_shear_lr, derived['shear_lr']])
        self.touchid_store_shear_tb = np.concatenate([self.touchid_store_shear_tb, derived['shear_tb']])
        self.touchid_store_normal = np.concatenate([self.touchid_store_normal, derived['normal']])
        self.touchid_store_ts = np.concatenate(
            [self.touchid_store_ts, np.asarray(sweep_timestamps, dtype=np.float64)]
        )
        self.touchid_store_next_abs += len(raw)

    def _touchid_trim_store(self):
        """Drop the front of the continuous store once no live span
        (finalized or open, per touchid_active_queue.oldest_referenced_idx)
        references it anymore, keeping a window_size_s + span_stale_timeout_s
        safety margin so a still-growing open span never has its start index
        trimmed out from under it."""
        queue = self.touchid_active_queue
        if queue is None:
            return
        margin_n = round(
            (self.touchid_config.window_size_s + self.touchid_config.span_stale_timeout_s) * queue.fs
        )
        oldest_referenced = queue.oldest_referenced_idx()
        safe_abs = self.touchid_chunk_cursor_abs if oldest_referenced is None else min(
            oldest_referenced, self.touchid_chunk_cursor_abs
        )
        trim_to_abs = max(self.touchid_store_base_abs, safe_abs - margin_n)
        trim_n = trim_to_abs - self.touchid_store_base_abs
        if trim_n <= 0:
            return
        self.touchid_store_raw = self.touchid_store_raw[trim_n:]
        self.touchid_store_integrated = self.touchid_store_integrated[trim_n:]
        self.touchid_store_shear_lr = self.touchid_store_shear_lr[trim_n:]
        self.touchid_store_shear_tb = self.touchid_store_shear_tb[trim_n:]
        self.touchid_store_normal = self.touchid_store_normal[trim_n:]
        self.touchid_store_ts = self.touchid_store_ts[trim_n:]
        self.touchid_store_base_abs = trim_to_abs

    def _touchid_slice_store(self, start_abs: int, end_abs: int):
        """Slice the continuous store at an absolute (start_idx, end_idx)
        pair from ActiveSampleQueue -- returns (window_adc, window_integrated,
        window_shear_lr, window_shear_tb, window_normal, window_ts), or None
        if the range has already been trimmed out (shouldn't happen given
        _touchid_trim_store's safety margin, but guarded rather than slicing
        garbage)."""
        if start_abs < self.touchid_store_base_abs:
            return None
        start_i = start_abs - self.touchid_store_base_abs
        end_i = end_abs - self.touchid_store_base_abs
        return (
            self.touchid_store_raw[start_i:end_i],
            self.touchid_store_integrated[start_i:end_i],
            self.touchid_store_shear_lr[start_i:end_i],
            self.touchid_store_shear_tb[start_i:end_i],
            self.touchid_store_normal[start_i:end_i],
            self.touchid_store_ts[start_i:end_i],
        )

    def _touchid_drain_active_queue(self, fs: float):
        """Chunk any newly-appended continuous-store samples into 0.05s
        micro-chunks, feed them into touchid_active_queue, drain whatever
        windows it yields (painting the "being inferenced" highlight for
        every one, matching the old comment on touchid_worker_busy below,
        but submitting only the newest to the classify worker), and evict
        stale spans -- run once per hop-timer tick, same cadence as the old
        RollingBuffer.get_window() call it replaces."""
        queue = self.touchid_active_queue
        chunk_n = max(1, round(MICRO_CHUNK_S * fs))
        now_t = time.monotonic()

        while self.touchid_chunk_cursor_abs + chunk_n <= self.touchid_store_next_abs:
            start_abs = self.touchid_chunk_cursor_abs
            end_abs = start_abs + chunk_n
            start_i = start_abs - self.touchid_store_base_abs
            end_i = end_abs - self.touchid_store_base_abs
            queue.push_micro_chunk((start_abs, end_abs), self.touchid_store_raw[start_i:end_i], now_t)
            self.touchid_chunk_cursor_abs = end_abs

        windows = queue.ready_windows(store_base_abs=self.touchid_store_base_abs)
        queue.evict_stale(now_t, self.touchid_config.min_span_fill_ratio, self.touchid_config.span_stale_timeout_s)

        # A merged span can fuse several genuinely separate touch events when
        # the idle gap between them is shorter than
        # merge_gap_chunks(window_size_s) -- reject any individual window
        # straddling one of those gaps (>30% idle micro-chunks) even though
        # the span itself was accepted, rather than feeding a mixed-signal
        # window into the classifier or highlighting it as "inferenced".
        accepted_windows = []
        for start_abs, end_abs, span_id in windows:
            sliced = self._touchid_slice_store(start_abs, end_abs)
            if sliced is None:
                continue
            if window_idle_fraction(sliced[0], self.touchid_idle_baseline, fs) <= MAX_WINDOW_IDLE_FRACTION:
                accepted_windows.append((start_abs, end_abs, span_id))
        windows = accepted_windows

        if not windows:
            self._update_touchid_idle_gate_label('no window ready (idle / accumulating)')
            self._touchid_maybe_clear_stale_prediction()
            self._touchid_trim_store()
            return
        self._update_touchid_idle_gate_label()

        for start_abs, end_abs, span_id in windows:
            sliced = self._touchid_slice_store(start_abs, end_abs)
            if sliced is not None:
                self._touchid_set_inference_region(sliced[5], is_inferenced=True, span_id=span_id)

        if self.touchid_worker_busy:
            # A classification is still in flight -- don't submit another; the
            # worker's own queue is bounded to 1 anyway (a new submission would
            # just evict this one), so skip straight to leaving these windows
            # unclassified rather than paying for the array packing below.
            self._touchid_trim_store()
            return

        sliced = self._touchid_slice_store(*windows[-1][:2])
        if sliced is None:
            self._touchid_trim_store()
            return
        window_adc, window_integrated, window_shear_lr, window_shear_tb, window_normal, window_ts = sliced

        self.touchid_worker_busy = True
        self.touchid_classify_worker.submit({
            'window_adc': window_adc,
            'window_integrated': window_integrated,
            'window_shear_lr': window_shear_lr,
            'window_shear_tb': window_shear_tb,
            'window_normal': window_normal,
            'fs': fs,
            'classifier': self.touchid_classifier,
            'window_ts': window_ts,
        })
        self._touchid_trim_store()

    def on_touchid_window_changed(self, value):
        self.touchid_config.window_size_s = float(value)
        self._rebuild_touchid_buffers()
        self.save_last_touchid_settings()

    def on_touchid_hop_changed(self, value):
        self.touchid_config.hop_size_s = float(value)
        self._rebuild_touchid_buffers()
        self.touchid_timer.setInterval(max(1, int(self.touchid_config.hop_size_s * 1000)))
        self.save_last_touchid_settings()

    def on_touchid_alpha_changed(self, value):
        self.touchid_config.smoothing_alpha = float(value)
        self.touchid_smoother.alpha = float(value)
        self.save_last_touchid_settings()

    def on_touchid_threshold_changed(self, value):
        self.touchid_config.confidence_threshold = float(value)
        self.save_last_touchid_settings()

    def on_touchid_smoothed_toggled(self, state):
        self.touchid_show_smoothed = bool(state)
        self.save_last_touchid_settings()

    def _touchid_available_pzt_sensor_numbers(self) -> list[str]:
        """Discover which PZT sensor board numbers are actually available from
        the currently configured display channels, falling back to a fixed
        list if that can't be determined (e.g. before a config is loaded)."""
        try:
            specs = self.get_display_channel_specs() or []
        except Exception:
            specs = []
        found = set()
        for spec in specs:
            match = _PZT_SENSOR_LABEL_RE.match(str(spec.get('label', '')))
            if match:
                found.add(match.group(1))
        numbers = sorted(found, key=int) if found else list(_FALLBACK_PZT_SENSOR_NUMBERS)
        current = pzt_sensor_number_of(self.touchid_config.pzt_columns)
        if current not in numbers:
            numbers = sorted(set(numbers) | {current}, key=int)
        return numbers

    def _touchid_refresh_pzt_sensor_combo(self):
        """Repopulate the PZT sensor combo, preselecting whichever sensor
        number config.pzt_columns currently points at."""
        numbers = self._touchid_available_pzt_sensor_numbers()
        current = pzt_sensor_number_of(self.touchid_config.pzt_columns)
        self.touchid_pzt_sensor_combo.blockSignals(True)
        self.touchid_pzt_sensor_combo.clear()
        self.touchid_pzt_sensor_combo.addItems(numbers)
        if current in numbers:
            self.touchid_pzt_sensor_combo.setCurrentText(current)
        self.touchid_pzt_sensor_combo.blockSignals(False)

    def on_touchid_pzt_sensor_changed(self, text):
        sensor_number = text.strip()
        if not sensor_number or sensor_number == pzt_sensor_number_of(self.touchid_config.pzt_columns):
            return
        new_columns = pzt_columns_for_sensor(sensor_number)
        self.touchid_config.pzt_columns = new_columns
        self._rebuild_touchid_buffers()
        # A sensor switch means a discontinuous physical input stream (a
        # different PZT board), so the causal state (bounded windowed sums,
        # unbounded causal medians) from the old sensor must NOT carry
        # forward -- reset it, same as touchid_smoother.reset() below.
        # Skipping this would silently poison every window after the switch
        # with a stale baseline from the previous sensor.
        self.touchid_derived_channels = CausalDerivedChannels(pzt_columns=new_columns)
        self.touchid_smoother.reset()
        # A different physical sensor board means the continuous store's
        # existing samples (and any live spans referencing them) are no
        # longer valid either -- same discontinuity reasoning as the
        # CausalDerivedChannels reset just above.
        self._touchid_store_reset()
        self._clear_touchid_stream_curves()
        self._touchid_clear_inference_regions()
        self._touchid_reset_stream_display()
        # Idle baseline is per-channel-set -- a different PZT board has a
        # different noise floor, so a baseline captured for the old sensor
        # must not silently gate the new one's windows.
        self.touchid_idle_baseline = load_idle_baseline(new_columns)
        self._update_touchid_idle_gate_label()
        self.save_last_touchid_settings()

    def _clear_touchid_stream_curves(self):
        """Drop stream-plot curves keyed by the old pzt_columns names so a
        sensor switch doesn't leave stale, no-longer-updated traces visible."""
        if not hasattr(self, 'touchid_stream_plot_widget'):
            return
        for curve in self.touchid_stream_curves.values():
            self.touchid_stream_plot_widget.removeItem(curve)
        self.touchid_stream_curves.clear()

    def _touchid_reset_stream_display(self):
        """(Re)initialize the Signal Stream plot's rolling history buffers and
        its elapsed-time reference point. Called on init and whenever the
        physical sensor changes (a discontinuous input stream, like the
        causal-channel state reset alongside it)."""
        self.touchid_stream_display_samples = {col: np.empty(0) for col in self.touchid_config.pzt_columns}
        self.touchid_stream_display_timestamps = np.empty(0)
        # First real timestamp seen becomes t=0 for the plot's x-axis, so it
        # reads as "seconds since streaming started" and keeps counting up
        # instead of resetting to 0 every hop (each hop's raw timestamps are
        # already real elapsed capture time -- this just picks the origin).
        self.touchid_stream_display_t0 = None

    def _touchid_push_stream_display(self, channel_samples: dict, timestamps: np.ndarray):
        """Append one hop's worth of samples to the Signal Stream plot's
        rolling history, trimming anything older than _TOUCHID_STREAM_HISTORY_S."""
        timestamps = np.asarray(timestamps, dtype=np.float64)
        if len(timestamps) == 0:
            return
        if self.touchid_stream_display_t0 is None:
            self.touchid_stream_display_t0 = float(timestamps[0])

        self.touchid_stream_display_timestamps = np.concatenate(
            [self.touchid_stream_display_timestamps, timestamps]
        )
        for col in self.touchid_config.pzt_columns:
            self.touchid_stream_display_samples[col] = np.concatenate(
                [self.touchid_stream_display_samples[col], np.asarray(channel_samples[col])]
            )

        cutoff = self.touchid_stream_display_timestamps[-1] - _TOUCHID_STREAM_HISTORY_S
        keep = self.touchid_stream_display_timestamps >= cutoff
        if not keep.all():
            self.touchid_stream_display_timestamps = self.touchid_stream_display_timestamps[keep]
            for col in self.touchid_config.pzt_columns:
                self.touchid_stream_display_samples[col] = self.touchid_stream_display_samples[col][keep]

    def on_touchid_model_type_changed(self, text):
        """Switch the active architecture (any key in ARCH_REGISTRY) and reload it from disk.

        Every model type shares the same InferenceConfig/TextureClassifier/reload
        path — only config.model_type and which weight file gets loaded differ —
        so switching is just: update the type, then run the same reload used by
        the Reload Model Weights button.
        """
        new_type = _MODEL_TYPE_BY_LABEL.get(text, 'ann')
        if new_type == self.touchid_config.model_type:
            return
        self.touchid_config.model_type = new_type
        self._touchid_refresh_version_combo()
        self.save_last_touchid_settings()
        self._touchid_reload_model()

    def _touchid_refresh_version_combo(self):
        """Repopulate the Version combo with weight versions found on disk for the
        currently active architecture, preselecting whichever one config points at."""
        versions = discover_model_versions(self.touchid_config.model_type)
        self.touchid_version_combo.blockSignals(True)
        self.touchid_version_combo.clear()
        self.touchid_version_combo.addItems(versions)
        current = model_version_of(self.touchid_config)
        if current and current in versions:
            self.touchid_version_combo.setCurrentText(current)
        self.touchid_version_combo.blockSignals(False)
        self._touchid_refresh_checkpoint_combo()

    def on_touchid_version_changed(self, text):
        """Point config at the selected weight version (default checkpoint) for
        the active architecture, refresh the Checkpoint combo, and reload."""
        if not text or text == model_version_of(self.touchid_config):
            return
        set_model_version(self.touchid_config, text)
        self._touchid_refresh_checkpoint_combo()
        self.save_last_touchid_settings()
        self._touchid_reload_model()

    def _touchid_refresh_checkpoint_combo(self):
        """Repopulate the Checkpoint combo with checkpoint tags found on disk for
        the currently active architecture+version, preselecting whichever one
        config points at. Disabled (but still showing "default") when the
        version has only a single untagged checkpoint file."""
        version = model_version_of(self.touchid_config)
        checkpoints = discover_checkpoints(self.touchid_config.model_type, version) if version else []
        self.touchid_checkpoint_combo.blockSignals(True)
        self.touchid_checkpoint_combo.clear()
        self.touchid_checkpoint_combo.addItems(checkpoints)
        current = model_checkpoint_of(self.touchid_config)
        if current in checkpoints:
            self.touchid_checkpoint_combo.setCurrentText(current)
        self.touchid_checkpoint_combo.blockSignals(False)
        self.touchid_checkpoint_combo.setEnabled(checkpoints == [] or checkpoints != [CHECKPOINT_DEFAULT])

    def on_touchid_checkpoint_changed(self, text):
        """Point config at the selected checkpoint tag for the active
        architecture+version and reload."""
        if not text or text == model_checkpoint_of(self.touchid_config):
            return
        version = model_version_of(self.touchid_config)
        if not version:
            return
        set_model_version(self.touchid_config, version, text)
        self.save_last_touchid_settings()
        self._touchid_reload_model()

    def on_touchid_reload_model_clicked(self):
        self._touchid_reload_model()

    def _touchid_reload_model(self):
        """Stop live prediction, reload the active model + normalization artifacts
        from disk (per config.model_type: ANN or CNN), and resume.

        A single code path covers both "stop" and "restart with new weights": the
        classifier is torn down first so no in-flight predict_proba call can run
        against a half-swapped model, then rebuilt from InferenceConfig's current
        model_type/paths — which may point at freshly retrained artifacts on disk
        under the same filenames, or at the other architecture entirely. Works
        identically for ANN and CNN since TextureClassifier itself dispatches on
        config.model_type.
        """
        was_active = self.touchid_timer.isActive()
        if was_active:
            self.touchid_timer.stop()

        self.touchid_classifier = None
        self.touchid_classifier_error = None
        self.touchid_smoother.reset()

        try:
            self.touchid_classifier = TextureClassifier(self.touchid_config)
        except Exception as exc:
            self.touchid_classifier_error = str(exc)

        if self.touchid_classifier_error:
            self.touchid_status_label.setText('Model not loaded: ' + self.touchid_classifier_error)
            self.touchid_status_label.setStyleSheet('color: #cc0000; font-weight: bold;')
        else:
            self.touchid_status_label.setText(f'Model loaded ({self.touchid_config.model_type.upper()})')
            self.touchid_status_label.setStyleSheet(f'color: {_TOUCHID_STATUS_OK_COLOR}; font-weight: bold;')

        if hasattr(self, 'log_status'):
            self.log_status(
                'TouchID: model reload failed - ' + self.touchid_classifier_error
                if self.touchid_classifier_error
                else f'TouchID: {self.touchid_config.model_type.upper()} model weights reloaded'
            )

        if was_active:
            self.touchid_timer.start()

    def on_touchid_run_on_source_clicked(self):
        """Run the currently selected/loaded model over whatever's loaded in
        the Analysis tab -- in-memory cache or a loaded CSV, whichever its
        Source selector currently points at (analysis_snapshot is populated
        the same way regardless of source, see analysis_panel.load_analysis_source) --
        store per-window predictions for the overlay renderer, and switch to
        the Analysis tab so the user can view them.
        """
        snapshot = getattr(self, 'analysis_snapshot', None)
        if snapshot is None:
            QMessageBox.warning(
                self, 'TouchID',
                'Load a source in the Analysis tab first (Analysis > Source: '
                'In-memory cache or CSV plus JSON).',
            )
            return
        if self.touchid_classifier is None:
            QMessageBox.warning(
                self, 'TouchID',
                'Model not loaded: ' + (self.touchid_classifier_error or 'unknown error'),
            )
            return

        try:
            predictions = run_inference_on_snapshot(
                snapshot, self.touchid_classifier, self.touchid_config,
                idle_baseline=self.touchid_idle_baseline,
            )
        except Exception as exc:
            QMessageBox.warning(self, 'TouchID', f'Inference on analysis source failed: {exc}')
            if hasattr(self, 'log_status'):
                self.log_status(f'TouchID: inference on analysis source failed - {exc}')
            return

        self.analysis_predicted_labels = predictions
        if hasattr(self, 'log_status'):
            self.log_status(f'TouchID: predicted {len(predictions)} windows on analysis source')
        if (
            hasattr(self, 'analysis_show_predicted_labels_check')
            and not self.analysis_show_predicted_labels_check.isChecked()
        ):
            self.analysis_show_predicted_labels_check.setChecked(True)
        if hasattr(self, '_render_label_regions'):
            self._render_label_regions()
        if hasattr(self, 'visualization_tabs') and hasattr(self, 'analysis_tab_index'):
            self.visualization_tabs.setCurrentIndex(self.analysis_tab_index)
        if hasattr(self, 'analysis_inner_tabs'):
            self.analysis_inner_tabs.setCurrentIndex(0)

    def save_last_touchid_settings(self):
        try:
            save_touchid_settings(self.touchid_config)
        except Exception as e:
            if hasattr(self, 'log_status'):
                self.log_status(f'Warning: could not save TouchID settings: {e}')

    def _update_touchid_idle_gate_label(self, extra: str | None = None):
        if not hasattr(self, 'touchid_idle_gate_label'):
            return
        if self.touchid_idle_baseline is None:
            text = 'Idle gate: no baseline captured -- inference runs on every window'
        else:
            text = (
                f'Idle gate: baseline loaded ({self.touchid_idle_baseline.captured_duration_s:.1f}s, '
                f'k={self.touchid_idle_baseline.k:.0f})'
            )
        if extra:
            text += f' -- {extra}'
        self.touchid_idle_gate_label.setText(text)

    def on_touchid_capture_idle_clicked(self):
        """Start (or, if already running, this is a no-op re-click) a timed
        no-contact capture: update_touchid_display accumulates raw PZT
        samples into touchid_idle_capture_samples for IDLE_CAPTURE_DURATION_S
        seconds, then _finish_touchid_idle_capture fits and persists the
        baseline."""
        if not getattr(self, 'is_capturing', False):
            QMessageBox.warning(self, 'TouchID', 'Start capturing data before recording an idle baseline.')
            return
        if self.touchid_idle_capture_active:
            return
        self.touchid_idle_capture_active = True
        self.touchid_idle_capture_samples = []
        self.touchid_idle_capture_n_samples = 0
        self.touchid_capture_idle_btn.setEnabled(False)
        self._update_touchid_idle_gate_label('recording, keep the sensor untouched...')

    def _finish_touchid_idle_capture(self, fs: float):
        samples = np.concatenate(self.touchid_idle_capture_samples, axis=0)
        baseline = fit_idle_baseline(samples, self.touchid_config.pzt_columns, fs)
        self.touchid_idle_baseline = baseline
        # A freshly captured baseline means segmentation should start fresh
        # under it rather than replaying already-elapsed history (which was
        # never being stored while no baseline existed) through a new queue.
        self._touchid_store_reset()
        try:
            save_idle_baseline(baseline)
        except Exception as e:
            if hasattr(self, 'log_status'):
                self.log_status(f'Warning: could not save idle baseline: {e}')
        self.touchid_idle_capture_active = False
        self.touchid_idle_capture_samples = []
        self.touchid_idle_capture_n_samples = 0
        self.touchid_capture_idle_btn.setEnabled(True)
        self._update_touchid_idle_gate_label()
        if hasattr(self, 'log_status'):
            per_channel = ', '.join(
                f'{col}={m:.1f}±{s:.2f}' for col, m, s in
                zip(baseline.pzt_columns, baseline.mean, baseline.std)
            )
            self.log_status(
                f'TouchID: idle baseline captured ({baseline.captured_duration_s:.1f}s, '
                f'k={baseline.k:.0f}): {per_channel}'
            )

    def _touchid_maybe_clear_stale_prediction(self):
        """Called each time a window is gate-skipped. If it's been more than
        _TOUCHID_PREDICTION_STALE_TIMEOUT_S since classify_window last
        actually ran, reset the EMA and clear the live "Prediction" readout
        -- otherwise it sits frozen on whatever confidence happened to be
        computed right before the gate started skipping, which reads as a
        stuck/stale prediction the longer idle continues. "Last Detected"
        is deliberately left untouched; it's meant to persist across gaps."""
        if self.touchid_last_classification_time is None:
            return  # already cleared (or never classified yet) -- nothing to do
        if time.monotonic() - self.touchid_last_classification_time < _TOUCHID_PREDICTION_STALE_TIMEOUT_S:
            return
        self.touchid_smoother.reset()
        self.touchid_class_label.setText('-')
        self.touchid_confidence_label.setText('confidence: -')
        n = len(self.touchid_config.class_names)
        self.touchid_bar_item.setOpts(
            height=[0.0] * n, brushes=[pg.mkBrush(_TOUCHID_BELOW_THRESHOLD_COLOR)] * n,
        )
        self.touchid_last_classification_time = None

    def _touchid_channel_index_map(self):
        """Map configured pzt_columns names to their raw_data_buffer column index."""
        try:
            specs = self.get_display_channel_specs()
        except Exception:
            return None
        label_to_index = {}
        for spec in specs:
            indices = spec.get('sample_indices') or []
            if indices:
                label_to_index[spec.get('label')] = indices[0]
        index_map = {}
        for col in self.touchid_config.pzt_columns:
            if col not in label_to_index:
                return None
            index_map[col] = label_to_index[col]
        return index_map

    def update_touchid_display(self):
        if not self.should_update_touchid_display():
            return

        index_map = self._touchid_channel_index_map()
        if index_map is None:
            # Configured pzt_columns (e.g. PZT3_B) aren't among the currently
            # streaming display channels -- most commonly because the main
            # channel selector isn't in "array" mode with this PZT sensor
            # picked. Surface this instead of sitting silently frozen (empty
            # plot, rate label stuck on '-') with no visible explanation.
            self._update_touchid_idle_gate_label(
                f"waiting for streaming channels matching {self.touchid_config.pzt_columns}"
            )
            return

        required_sweeps = max(1, int(self.touchid_config.hop_size_s * self.get_measured_sweep_rate_hz()) + 1)
        extracted = self._extract_recent_sweeps(required_sweeps) if hasattr(self, '_extract_recent_sweeps') else None
        if extracted is None:
            self._update_touchid_idle_gate_label('waiting for sweep data')
            return
        data_array, sweep_timestamps = extracted

        channel_samples = {
            col: data_array[:, idx] for col, idx in index_map.items()
        }

        if self.touchid_idle_capture_active:
            # Accumulate for the baseline fit in parallel with the normal
            # push/plot/inference pipeline below -- NOT a return-early here,
            # since that previously starved _update_touchid_stream_plot for
            # the whole capture window and made the tab look frozen.
            raw = np.stack([channel_samples[col] for col in self.touchid_config.pzt_columns], axis=1)
            self.touchid_idle_capture_samples.append(raw)
            self.touchid_idle_capture_n_samples += len(raw)
            fs_now = self.get_measured_sweep_rate_hz()
            if fs_now > 0 and self.touchid_idle_capture_n_samples / fs_now >= IDLE_CAPTURE_DURATION_S:
                self._finish_touchid_idle_capture(fs_now)
            else:
                self._update_touchid_idle_gate_label(
                    f'recording {self.touchid_idle_capture_n_samples / fs_now:.1f}s'
                    f'/{IDLE_CAPTURE_DURATION_S:.0f}s...' if fs_now > 0 else 'recording...'
                )

        # Feed the Signal Stream plot's own rolling history independently of
        # the inference-window path below, and redraw every hop -- so the
        # plot keeps scrolling forward in real elapsed time even on hops that
        # don't yet produce a full inference window.
        self._touchid_push_stream_display(channel_samples, sweep_timestamps)
        self._update_touchid_stream_plot()

        # Advance the derived-channel causal state by exactly this newly-
        # pushed chunk (not the whole rolling window), so shear/normal stay
        # continuous across window/hop boundaries (Constraint 0 -- this and
        # the derivation formula itself are unchanged by the ActiveSampleQueue
        # redesign below; only which sample-index range gets sliced out of
        # the resulting continuous streams before featurization changes).
        pzt_columns = self.touchid_config.pzt_columns
        derived = self.touchid_derived_channels.process(channel_samples)

        fs = self.get_measured_sweep_rate_hz()
        if fs <= 0:
            return

        if hasattr(self, 'touchid_sample_rate_label'):
            self.touchid_sample_rate_label.setText(f'Per-channel rate: {fs:.2f} Hz')

        if self.touchid_classifier is None:
            return

        if self.touchid_idle_baseline is None:
            # No baseline captured yet -- fall back to the plain fixed
            # window_size_s/hop_size_s grid via RollingBuffer, classifying
            # every window unconditionally (matches is_window_quality's old
            # no-op-without-a-baseline behavior).
            derived_channel_samples = {f'integrated_{col}': derived['integrated'][col] for col in pzt_columns}
            derived_channel_samples['shear_lr'] = derived['shear_lr']
            derived_channel_samples['shear_tb'] = derived['shear_tb']
            derived_channel_samples['normal'] = derived['normal']
            self.touchid_buffer.push(channel_samples, sweep_timestamps)
            self.touchid_derived_buffer.push(derived_channel_samples, sweep_timestamps)

            window = self.touchid_buffer.get_window(fs=fs)
            if window is None:
                return
            window_adc, window_ts = window
            derived_window = self.touchid_derived_buffer.get_window(fs=fs)
            if derived_window is None:
                return
            derived_window_adc, _derived_ts = derived_window
            n_pzt = len(pzt_columns)
            window_integrated = derived_window_adc[:, :n_pzt]
            window_shear_lr = derived_window_adc[:, n_pzt]
            window_shear_tb = derived_window_adc[:, n_pzt + 1]
            window_normal = derived_window_adc[:, n_pzt + 2]

            self._touchid_set_inference_region(window_ts, is_inferenced=True)
            self._update_touchid_idle_gate_label()

            if self.touchid_worker_busy:
                # A classification is still in flight -- don't submit another; the
                # worker's own queue is bounded to 1 anyway (a new submission would
                # just evict this one), so skip straight to leaving this window
                # unclassified rather than paying for the array packing below.
                return

            self.touchid_worker_busy = True
            self.touchid_classify_worker.submit({
                'window_adc': window_adc,
                'window_integrated': window_integrated,
                'window_shear_lr': window_shear_lr,
                'window_shear_tb': window_shear_tb,
                'window_normal': window_normal,
                'fs': fs,
                'classifier': self.touchid_classifier,
                'window_ts': window_ts,
            })
            return

        # Baseline present: sample-accurate ActiveSampleQueue segmentation
        # (inference/segmentation.py) -- feed this tick's newly-derived
        # samples into the continuous store, then chunk/segment/classify off
        # of it instead of a fixed hop grid.
        self._touchid_append_to_store(channel_samples, derived, sweep_timestamps)
        self._touchid_ensure_active_queue(fs)
        if self.touchid_active_queue is None:
            return
        self._touchid_drain_active_queue(fs)

    def _on_touchid_classify_error(self, message: str):
        self.touchid_worker_busy = False
        self.touchid_status_label.setText(f'Prediction error: {message}')
        self.touchid_status_label.setStyleSheet('color: #cc0000; font-weight: bold;')

    def _on_touchid_classified(self, probs: dict, window_ts):
        """Slot for TouchIdClassifyWorker.result_ready (queued connection, so
        this always runs on the GUI thread even though the worker computed
        probs off-thread)."""
        self.touchid_worker_busy = False

        self.touchid_last_classification_time = time.monotonic()
        smoothed = self.touchid_smoother.update(probs)
        display_probs = smoothed if self.touchid_show_smoothed else probs
        top_class, top_conf = max(display_probs.items(), key=lambda kv: kv[1])

        self.touchid_class_label.setText(top_class)
        self.touchid_confidence_label.setText(f'confidence: {top_conf:.2%}')
        heights = [display_probs.get(name, 0.0) for name in self.touchid_config.class_names]
        threshold = self.touchid_config.confidence_threshold
        brushes = [
            pg.mkBrush(_TOUCHID_ABOVE_THRESHOLD_COLOR if h >= threshold else _TOUCHID_BELOW_THRESHOLD_COLOR)
            for h in heights
        ]
        self.touchid_bar_item.setOpts(height=heights, brushes=brushes)

        # Latch the "last confidently detected" panel: only overwritten when
        # this prediction actually crosses the threshold, so it keeps
        # showing the last recognized texture instead of reverting to '-'
        # (or flickering to a low-confidence guess) between confident hits.
        if top_conf >= threshold and top_class != 'idle':
            self.touchid_last_confident_class = top_class
            self.touchid_last_confident_conf = top_conf
            self.touchid_last_detected_label.setText(top_class)
            self.touchid_last_detected_confidence_label.setText(f'confidence: {top_conf:.2%}')

    def _update_touchid_stream_plot(self):
        """Render the last _TOUCHID_STREAM_HISTORY_S seconds of streamed PZT
        data, same visual language as the Time Series tab: one colored curve
        per channel, legend, time-in-seconds x-axis. Unlike a single
        inference window, the x-axis is real elapsed capture time measured
        from when streaming started (touchid_stream_display_t0) -- it keeps
        counting up and the plot scrolls forward, instead of resetting to a
        0-500ms range every hop."""
        if not hasattr(self, 'touchid_stream_plot_widget'):
            return
        if self.touchid_stream_display_t0 is None:
            return

        x = self.touchid_stream_display_timestamps - self.touchid_stream_display_t0

        for i, col in enumerate(self.touchid_config.pzt_columns):
            y = self.touchid_stream_display_samples[col]
            curve = self.touchid_stream_curves.get(col)
            if curve is None:
                color = PLOT_COLORS[i % len(PLOT_COLORS)]
                curve = self.touchid_stream_plot_widget.plot([], pen=pg.mkPen(color=color, width=2), name=col)
                self.touchid_stream_curves[col] = curve
            curve.setData(x=x, y=y)

        self._touchid_prune_inference_regions()

    def _touchid_region_color_for_span(self, span_id):
        """Pick the RGB color for one inference-region highlight.

        span_id is None in the no-idle-baseline fixed-grid fallback branch,
        which has no span concept at all (see _TOUCHID_REGION_FALLBACK_COLOR's
        docstring) -- always the same flat color there. With a baseline
        present, every call cycles to the next PLOT_COLORS entry regardless of
        span_id, so each individually-classified window gets its own distinct
        color -- same-span adjacent windows (one continuous touch event,
        hop_size_s == window_size_s) previously all shared one color and
        visually fused into a single block, making it look like one oversized
        window had been sent to inference instead of several separate ones."""
        if span_id is None:
            return _TOUCHID_REGION_FALLBACK_COLOR
        self.touchid_region_span_id = span_id
        self.touchid_region_color_index = (self.touchid_region_color_index + 1) % len(PLOT_COLORS)
        return PLOT_COLORS[self.touchid_region_color_index]

    def _touchid_set_inference_region(self, window_ts, is_inferenced: bool, span_id=None):
        """Paint the "being inferenced" highlight over an inference window's
        real time span within the scrolling Signal Stream plot, only for
        windows that were actually run through the classifier (vs.
        gate-skipped). Unlike a single reused region, every qualifying window
        gets its own LinearRegionItem so the highlight covers the whole
        rendered span of every clip that was inferenced, not just wherever
        the latest window happens to sit -- it ages out (removed) once its
        span has scrolled past the plot's rolling history window, same as the
        underlying curve data. See _touchid_region_color_for_span for how
        span_id maps to a color."""
        if not is_inferenced:
            return
        if not hasattr(self, 'touchid_stream_plot_widget'):
            return
        if not hasattr(self, 'touchid_stream_inference_regions'):
            self.touchid_stream_inference_regions = []  # list of (end_time, LinearRegionItem)

        if self.touchid_stream_display_t0 is None or len(window_ts) == 0:
            return

        start = float(window_ts[0]) - self.touchid_stream_display_t0
        end = float(window_ts[-1]) - self.touchid_stream_display_t0
        color = self._touchid_region_color_for_span(span_id)
        region = pg.LinearRegionItem(
            values=(start, end),
            brush=pg.mkBrush(color[0], color[1], color[2], _TOUCHID_REGION_ALPHA),
            pen=pg.mkPen(None), movable=False,
        )
        region.setZValue(-10)
        self.touchid_stream_plot_widget.addItem(region)
        self.touchid_stream_inference_regions.append((end, region))

    def _touchid_prune_inference_regions(self):
        """Drop inference-region highlights whose window has scrolled past the
        plot's rolling history, so old highlights don't pile up forever."""
        if not hasattr(self, 'touchid_stream_inference_regions'):
            return
        if self.touchid_stream_display_t0 is None or len(self.touchid_stream_display_timestamps) == 0:
            return
        latest = self.touchid_stream_display_timestamps[-1] - self.touchid_stream_display_t0
        cutoff = latest - _TOUCHID_STREAM_HISTORY_S
        kept = []
        for end, region in self.touchid_stream_inference_regions:
            if end < cutoff:
                self.touchid_stream_plot_widget.removeItem(region)
            else:
                kept.append((end, region))
        self.touchid_stream_inference_regions = kept

    def _touchid_clear_inference_regions(self):
        """Remove all inference-region highlights -- called alongside stream
        curve resets (sensor switch) so stale regions from before the switch
        don't linger."""
        if not hasattr(self, 'touchid_stream_inference_regions'):
            self.touchid_stream_inference_regions = []
            return
        if hasattr(self, 'touchid_stream_plot_widget'):
            for _end, region in self.touchid_stream_inference_regions:
                self.touchid_stream_plot_widget.removeItem(region)
        self.touchid_stream_inference_regions = []
