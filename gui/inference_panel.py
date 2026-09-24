"""
Inference (TouchID) Panel GUI Component
========================================
Live texture-classification tab: buffers incoming PZT sweeps, runs the
texture_piezo feature pipeline + ANN v2 model on a rolling window, and
displays smoothed class probabilities.
"""

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
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import re

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
from inference.mode import TouchIdMode
from inference.quality_gate import load_idle_baseline, save_idle_baseline
from inference.replay_fastforward import ReplayFastForward
from inference.smoothing import WindowedVoteSmoother, is_guilty_candidate
from touchid_inference.quality_gate import IDLE_CAPTURE_DURATION_S, fit_idle_baseline
from inference.stream_processor import TouchIdStreamProcessor
from constants.plotting import PLOT_COLORS

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

# Decimation factor for the Signal Stream plot's rendered curves (pyqtgraph
# setDownsampling, method='peak' -- keeps both the min and max sample per
# decimated bucket, so spikes still render at full height; this only skips
# points that would've overlapped on the same pixel column anyway). Kept
# deliberately low/near-dormant: ds=15 measured ~53% cheaper repaint but was
# visibly worse (per user feedback) -- this small value trades away most of
# that repaint-cost saving in exchange for a curve that looks effectively
# unchanged from full resolution. Raise this (a single-line change) if more
# headroom is needed later.
_TOUCHID_STREAM_DOWNSAMPLE_FACTOR = 2


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

        self.touchid_smoother = WindowedVoteSmoother(
            class_names=self.touchid_config.class_names,
            window_n=self.touchid_config.smoothing_window_n,
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

        # Manual on/off switch for running windows through the classifier
        # (see on_touchid_stop_inference_clicked). Independent of
        # touchid_mode: an idle baseline capture ALSO forces inference off
        # regardless of this flag's value (see update_touchid_display), but
        # leaves this flag itself untouched so inference resumes to whatever
        # state the user had it in once the capture finishes.
        self.touchid_inference_enabled = True

        # Cache of the smoother's last output, reused for display on a tick
        # whose window got excluded by the guilty-clip filter (see
        # _on_touchid_classified) -- excluding a window from the vote must
        # not blank the display, it just means this tick doesn't move the
        # smoothed reading.
        self._touchid_last_smoothed: dict | None = None
        self._touchid_last_smoothed_top: tuple[str, float] | None = None

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
        if self.touchid_idle_baseline is not None:
            # The baseline's own persisted k is the actual active value --
            # keep touchid_config.idle_gate_k (and the spinbox it seeds) in
            # sync with it rather than a possibly stale touchid_settings value.
            self.touchid_config.idle_gate_k = self.touchid_idle_baseline.k
        self.touchid_idle_capture_active = False
        self.touchid_idle_capture_samples: list[np.ndarray] = []
        self.touchid_idle_capture_n_samples = 0

        # Which non-default activity (if any) the TouchID tab is doing right
        # now -- see inference/mode.py's module docstring for why this does
        # NOT also encode "is live streaming happening" (that's is_capturing's
        # job, consulted separately by should_update_touchid_display()).
        self.touchid_mode = TouchIdMode.NORMAL
        # Populated only while touchid_mode is REPLAYING -- see
        # on_touchid_run_on_source_clicked.
        self.touchid_replay_results: list[tuple] = []

        # Snapshot object (identity, not value) + raw predictions from the
        # most recently completed 'Run on Analysis Source' replay -- lets
        # 'Load Last Inference' reapply those predictions to the Analysis
        # overlay without re-running the model, as long as analysis_snapshot
        # is still that exact object (see on_touchid_load_last_inference_clicked).
        self._touchid_last_inference_snapshot = None
        self._touchid_last_inference_predictions: list[dict] = []

        # Whether a replay skips its per-tick animation to run at full
        # speed -- see inference/replay_fastforward.py.
        self.touchid_fast_forward = ReplayFastForward()

        # Owns buffering/derivation/segmentation (inference/stream_processor.py)
        # -- the single source of truth shared by live streaming and offline
        # replay. Rebuilt (not reset in place) on a window/hop resize, a PZT
        # sensor switch (discontinuous input stream), or a new idle baseline
        # (see _touchid_new_processor).
        self.touchid_processor = self._touchid_new_processor()
        # A fresh processor means span_ids restart from 0 -- reset the color
        # cycle state too so a stale span_id from before the reset can't
        # coincidentally collide with a new span's id.
        self._touchid_reset_region_coloring()
        # Absolute-index read cursor for update_touchid_display's sweep
        # read -- see _touchid_reset_read_cursor's docstring.
        self._touchid_reset_read_cursor()

        # Rolling history feeding the Signal Stream plot -- separate from
        # touchid_processor's own buffering (which only holds one inference
        # window's worth at a time). Keeps the last _TOUCHID_STREAM_HISTORY_S
        # seconds of real elapsed capture time so the plot scrolls forward
        # instead of resetting its x-axis every hop.
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

        Refuses to (re)start the timer while a replay is in progress
        (touchid_mode == REPLAYING) -- a capture start/stop firing during
        replay would otherwise interleave live serial data into replay's
        own TouchIdStreamProcessor. Replay's own driver timer resumes
        touchid_timer itself once it finishes, via this same method.
        """
        if not hasattr(self, 'visualization_tabs') or self.visualization_tabs is None:
            return
        if not hasattr(self, 'touchid_timer'):
            return
        if getattr(self, 'touchid_mode', TouchIdMode.NORMAL) == TouchIdMode.REPLAYING:
            if self.touchid_timer.isActive():
                self.touchid_timer.stop()
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

        control_layout.addWidget(QLabel('Smoothing N:'), 0, 4)
        self.touchid_smoothing_n_spin = QSpinBox()
        self.touchid_smoothing_n_spin.setRange(1, 30)
        self.touchid_smoothing_n_spin.setSingleStep(1)
        self.touchid_smoothing_n_spin.setValue(self.touchid_config.smoothing_window_n)
        self.touchid_smoothing_n_spin.setToolTip(
            "Number of recent windows the majority-vote/median smoother pools: the displayed "
            "class is the mode of the last N windows' top-1 predictions, and its confidence is "
            "the per-class median over the same N windows. Larger N is steadier but slower to "
            "react to a real texture change; smaller N reacts faster but flickers more."
        )
        self.touchid_smoothing_n_spin.valueChanged.connect(self.on_touchid_smoothing_n_changed)
        control_layout.addWidget(self.touchid_smoothing_n_spin, 0, 5)

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

        self.touchid_stop_inference_btn = QPushButton('Stop Inference')
        self.touchid_stop_inference_btn.setToolTip(
            'Pause running windows through the classifier. The signal stream plot, sample-rate '
            'readout, and idle-baseline capture keep working -- only classification stops. '
            'Click again to resume.'
        )
        self.touchid_stop_inference_btn.clicked.connect(self.on_touchid_stop_inference_clicked)
        control_layout.addWidget(self.touchid_stop_inference_btn, 0, 17)

        self.touchid_run_on_source_btn = QPushButton('Run on Analysis Source')
        self.touchid_run_on_source_btn.setToolTip(
            "Run the selected model over whatever is currently loaded in the Analysis tab "
            "(In-memory cache or CSV plus JSON, per its Source selector), then switch to "
            "Analysis to view the predicted labels overlaid on the trace."
        )
        self.touchid_run_on_source_btn.clicked.connect(self.on_touchid_run_on_source_clicked)
        control_layout.addWidget(self.touchid_run_on_source_btn, 0, 14)

        self.touchid_stop_replay_btn = QPushButton('Stop')
        self.touchid_stop_replay_btn.setToolTip(
            "Stop the in-progress 'Run on Analysis Source' replay early. Windows classified "
            "so far are kept and still summarized in the final results."
        )
        self.touchid_stop_replay_btn.setEnabled(False)
        self.touchid_stop_replay_btn.clicked.connect(self.on_touchid_stop_replay_clicked)
        control_layout.addWidget(self.touchid_stop_replay_btn, 0, 16)

        self.touchid_fast_forward_check = QCheckBox('Fast Forward')
        self.touchid_fast_forward_check.setToolTip(
            "Skip the per-tick stream-plot animation during 'Run on Analysis Source' so the "
            "replay runs at full speed instead of animating like live capture. Windows are "
            "still classified one by one, same result either way -- this only affects how "
            "fast it gets there. Can be toggled mid-replay."
        )
        self.touchid_fast_forward_check.stateChanged.connect(self.on_touchid_fast_forward_toggled)
        control_layout.addWidget(self.touchid_fast_forward_check, 0, 19)

        self.touchid_load_last_inference_btn = QPushButton('Load Last Inference')
        self.touchid_load_last_inference_btn.setToolTip(
            "Reapply the predicted labels from the most recent 'Run on Analysis Source' "
            "replay to the Analysis overlay without re-running the model. Only enabled "
            "while the Analysis tab's loaded source is still the exact same one that "
            "replay ran over -- Load a new source or re-run and this disables until the "
            "next replay completes."
        )
        self.touchid_load_last_inference_btn.setEnabled(False)
        self.touchid_load_last_inference_btn.clicked.connect(self.on_touchid_load_last_inference_clicked)
        control_layout.addWidget(self.touchid_load_last_inference_btn, 0, 18)

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

        self.touchid_guilty_filter_check = QCheckBox('Guilty-clip filter')
        self.touchid_guilty_filter_check.setChecked(self.touchid_config.guilty_clip_filter_enabled)
        self.touchid_guilty_filter_check.setToolTip(
            "Excludes a window from the smoothing vote when its raw prediction disagrees with "
            "the smoother's current majority label, or its raw softmax isn't a clean call (a "
            "real runner-up class even though the top class is under 79% confidence). Fit/"
            "validated on labeled replay captures: catches ~83% of windows that would otherwise "
            "corrupt the smoothed output, at an ~11.7% cost on windows that were actually fine."
        )
        self.touchid_guilty_filter_check.stateChanged.connect(self.on_touchid_guilty_filter_toggled)
        pzt_layout.addWidget(self.touchid_guilty_filter_check)

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
        summary_row.addWidget(QLabel('Idle gate k:'))
        self.touchid_idle_gate_k_spin = QDoubleSpinBox()
        self.touchid_idle_gate_k_spin.setRange(1.0, 30.0)
        self.touchid_idle_gate_k_spin.setDecimals(1)
        self.touchid_idle_gate_k_spin.setSingleStep(0.5)
        self.touchid_idle_gate_k_spin.setValue(self.touchid_config.idle_gate_k)
        self.touchid_idle_gate_k_spin.setToolTip(
            "Idle-band width, in multiples of the captured baseline's per-channel std. "
            "A micro-chunk only counts as active (and gets inferenced) if a sample falls "
            "outside [mean - k*std, mean + k*std]. Higher k = only stronger, more clearly "
            "above-noise-floor signals are inferenced; lower k = more borderline activity "
            "gets through. Applies immediately to the loaded baseline, and is used as the "
            "default for the next captured baseline."
        )
        self.touchid_idle_gate_k_spin.valueChanged.connect(self.on_touchid_idle_gate_k_changed)
        summary_row.addWidget(self.touchid_idle_gate_k_spin)

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
        self.touchid_plot_widget.setMouseEnabled(x=False, y=False)
        self.touchid_plot_widget.getPlotItem().setMenuEnabled(False)
        self.touchid_plot_widget.getPlotItem().hideButtons()
        display_layout.addWidget(self.touchid_plot_widget)

        plots_col.addWidget(display_group, 1)

        root_layout.addLayout(plots_col, 1)

        return tab

    def _touchid_new_processor(self, derived_channels=None) -> TouchIdStreamProcessor:
        """Build a fresh TouchIdStreamProcessor at the current config/idle
        baseline. Pass `derived_channels` to carry an existing
        CausalDerivedChannels instance's causal state (bounded sums, causal
        medians) forward into the new processor instead of starting it fresh
        -- used by _rebuild_touchid_buffers (see its docstring for why a
        window/hop resize is NOT a discontinuity in the underlying raw
        sample stream, unlike a sensor switch or a new idle baseline)."""
        processor = TouchIdStreamProcessor(
            pzt_columns=self.touchid_config.pzt_columns,
            window_size_s=self.touchid_config.window_size_s,
            hop_size_s=self.touchid_config.hop_size_s,
            span_stale_timeout_s=self.touchid_config.span_stale_timeout_s,
            min_span_fill_ratio=self.touchid_config.min_span_fill_ratio,
            idle_baseline=self.touchid_idle_baseline,
        )
        if derived_channels is not None:
            processor.derived_channels = derived_channels
        return processor

    def _touchid_reset_read_cursor(self):
        """Reset update_touchid_display's absolute sweep read cursor
        (touchid_read_cursor_abs).

        None means "no cursor yet" -- the next tick initializes it to
        whatever raw_data_buffer's current write position is (buffer_write_
        index) and reads nothing that tick, rather than reading backlog that
        predates the reset. Called wherever touchid_processor is rebuilt for
        a genuine discontinuity (sensor switch, fresh idle baseline, end of
        replay) so a stale cursor spanning the discontinuity can't hand the
        fresh processor a backlog read that includes samples from before it
        -- and from CaptureLifecycleMixin.start_capture, since a fresh
        capture zeroes buffer_write_index itself (a stale cursor from the
        previous capture would otherwise be larger than the restarted
        counter, making every read look like "nothing pending"). NOT called
        on a plain window/hop resize (_rebuild_touchid_buffers) -- that's
        not a discontinuity in the raw stream, so the read cursor should
        keep progressing normally."""
        self.touchid_read_cursor_abs = None

    def _touchid_reset_region_coloring(self):
        """Reset the inference-region color cycle state -- called whenever
        touchid_processor is rebuilt, since a fresh ActiveSampleQueue means
        span_ids restart from 0 and a stale span_id from before the reset
        could otherwise coincidentally collide with a new span's id."""
        self.touchid_region_span_id = None
        self.touchid_region_color_index = -1

    def _rebuild_touchid_buffers(self):
        """Recreate touchid_processor at the current window/hop sizing.
        Carries the existing derived_channels instance forward -- its causal
        state (bounded sums, causal medians) stays valid across a
        window/hop resize since the underlying raw sample stream is
        unbroken; only the windowing (not the derivation) is changing.
        window_size_s/hop_size_s feed directly into ActiveSampleQueue's own
        sizing (merge-gap threshold, window/hop-in-samples), so a resize
        needs a fresh queue -- the fresh processor also drops the continuous
        store, which is fine since the underlying raw stream is unbroken and
        will simply refill it (unlike a sensor switch, there's no
        discontinuity here, just an easier restart than trying to re-derive
        queue bookkeeping for the old sizing's spans)."""
        self.touchid_processor = self._touchid_new_processor(
            derived_channels=self.touchid_processor.derived_channels
        )
        self._touchid_reset_region_coloring()

    def on_touchid_window_changed(self, value):
        self.touchid_config.window_size_s = float(value)
        self._rebuild_touchid_buffers()
        self.save_last_touchid_settings()

    def on_touchid_hop_changed(self, value):
        self.touchid_config.hop_size_s = float(value)
        self._rebuild_touchid_buffers()
        self.touchid_timer.setInterval(max(1, int(self.touchid_config.hop_size_s * 1000)))
        self.save_last_touchid_settings()

    def on_touchid_smoothing_n_changed(self, value):
        self.touchid_config.smoothing_window_n = int(value)
        self.touchid_smoother.set_window_n(int(value))
        self.save_last_touchid_settings()

    def _touchid_reset_smoother(self):
        self.touchid_smoother.reset()
        self._touchid_last_smoothed = None
        self._touchid_last_smoothed_top = None

    def on_touchid_threshold_changed(self, value):
        self.touchid_config.confidence_threshold = float(value)
        self.save_last_touchid_settings()

    def on_touchid_guilty_filter_toggled(self, state):
        self.touchid_config.guilty_clip_filter_enabled = bool(state)
        self.save_last_touchid_settings()

    def on_touchid_idle_gate_k_changed(self, value):
        self.touchid_config.idle_gate_k = float(value)
        # Apply immediately to the already-captured baseline (mean/std are
        # unaffected -- k only rescales the accept band), instead of forcing
        # a recapture, and persist so it survives a restart.
        if self.touchid_idle_baseline is not None:
            self.touchid_idle_baseline.k = float(value)
            # ActiveSampleQueue copies baseline.k into its own self.k once at
            # construction (segmentation.py) rather than reading
            # self.baseline.k live on every chunk -- mutating the baseline
            # object above does NOT reach an already-built queue, so the
            # live queue (if one exists yet) needs its k updated directly or
            # this spin box would silently do nothing until the next
            # discontinuity (sensor switch, new baseline) rebuilt the queue.
            active_queue = getattr(self.touchid_processor, 'active_queue', None)
            if active_queue is not None:
                active_queue.k = float(value)
            try:
                save_idle_baseline(self.touchid_idle_baseline)
            except Exception as e:
                if hasattr(self, 'log_status'):
                    self.log_status(f'Warning: could not save idle baseline: {e}')
            self._update_touchid_idle_gate_label()
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
        # A sensor switch means a discontinuous physical input stream (a
        # different PZT board), so the causal state (bounded windowed sums,
        # unbounded causal medians) from the old sensor must NOT carry
        # forward, unlike a plain window/hop resize (_rebuild_touchid_buffers)
        # -- build a wholly fresh processor (fresh CausalDerivedChannels AND
        # a dropped continuous store/queue, since any live spans referencing
        # the old store are no longer valid either), same reasoning as
        # touchid_smoother.reset() below. Skipping this would silently
        # poison every window after the switch with a stale baseline from
        # the previous sensor.
        #
        # Idle baseline is also per-channel-set -- a different PZT board has
        # a different noise floor, so a baseline captured for the old sensor
        # must not silently gate the new one's windows.
        self.touchid_idle_baseline = load_idle_baseline(new_columns)
        self.touchid_processor = self._touchid_new_processor()
        self._touchid_reset_region_coloring()
        self._touchid_reset_read_cursor()
        self._touchid_reset_smoother()
        self._clear_touchid_stream_curves()
        self._touchid_clear_inference_regions()
        self._touchid_reset_stream_display()
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

    def on_touchid_stop_inference_clicked(self):
        """Toggle classification on/off without touching the stream plot,
        sample-rate readout, or idle-baseline capture -- see
        update_touchid_display's inference gate."""
        self.touchid_inference_enabled = not self.touchid_inference_enabled
        if self.touchid_inference_enabled:
            self.touchid_stop_inference_btn.setText('Stop Inference')
            self.touchid_stop_inference_btn.setStyleSheet('')
        else:
            self.touchid_stop_inference_btn.setText('Resume Inference')
            self.touchid_stop_inference_btn.setStyleSheet('font-weight: bold; color: #cc0000;')
            # Nothing new will be classified while stopped -- clear the live
            # readout immediately instead of leaving it frozen on a stale
            # prediction, same as the existing stale-prediction timeout.
            self._touchid_maybe_clear_stale_prediction_immediate()
        if hasattr(self, 'log_status'):
            self.log_status(
                'TouchID: inference stopped' if not self.touchid_inference_enabled
                else 'TouchID: inference resumed'
            )

    def _touchid_maybe_clear_stale_prediction_immediate(self):
        """Same clearing behavior as _touchid_maybe_clear_stale_prediction, but
        unconditional -- used when the user explicitly stops inference rather
        than waiting out _TOUCHID_PREDICTION_STALE_TIMEOUT_S."""
        self._touchid_reset_smoother()
        self.touchid_class_label.setText('-')
        self.touchid_confidence_label.setText('confidence: -')
        n = len(self.touchid_config.class_names)
        self.touchid_bar_item.setOpts(
            height=[0.0] * n, brushes=[pg.mkBrush(_TOUCHID_BELOW_THRESHOLD_COLOR)] * n,
        )
        self.touchid_last_classification_time = None

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
        self._touchid_reset_smoother()

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
        """Kick off a replay: run the currently selected/loaded model over
        whatever's loaded in the Analysis tab (in-memory cache or a loaded
        CSV -- whichever its Source selector currently points at;
        analysis_snapshot is populated the same way regardless of source,
        see analysis_panel.load_analysis_source), reusing the EXACT SAME
        TouchIdStreamProcessor + TouchIdClassifyWorker classes as live
        streaming so the resulting numbers are identical to what live would
        have produced for the same raw samples -- see
        inference/stream_processor.py's module docstring. The actual replay
        runs tick-by-tick on _touchid_replay_tick (driven by a
        zero-interval QTimer so it still yields to the Qt event loop,
        letting the Signal Stream plot/bar chart/region highlights animate
        like a real live capture); this method just validates preconditions
        and arms it.
        """
        if self.touchid_mode != TouchIdMode.NORMAL:
            QMessageBox.warning(
                self, 'TouchID',
                'Cannot start a replay while an idle baseline capture is in progress.'
                if self.touchid_mode == TouchIdMode.CAPTURING_BASELINE
                else 'A replay is already in progress.',
            )
            return
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

        fs = float(snapshot.sample_rate_hz)
        if fs <= 0:
            QMessageBox.warning(self, 'TouchID', 'Loaded source has no valid sample rate.')
            return
        try:
            channel_indices = [snapshot.channel_labels.index(col) for col in self.touchid_config.pzt_columns]
        except ValueError as exc:
            QMessageBox.warning(self, 'TouchID', f'Source is missing a configured PZT column: {exc}')
            return
        if snapshot.sweep_count <= 0:
            QMessageBox.warning(self, 'TouchID', 'Loaded source has no samples.')
            return

        self.touchid_mode = TouchIdMode.REPLAYING
        self.touchid_replay_results = []
        self.touchid_run_on_source_btn.setEnabled(False)
        self.touchid_stop_replay_btn.setEnabled(True)
        if hasattr(self, 'log_status'):
            self.log_status('TouchID: starting replay over analysis source')

        if hasattr(self, 'visualization_tabs') and hasattr(self, 'touchid_tab_index'):
            self.visualization_tabs.setCurrentIndex(self.touchid_tab_index)
        if self.touchid_timer.isActive():
            self.touchid_timer.stop()

        # Fresh processor for replay, built at the snapshot's OWN measured
        # fs -- same discontinuity reasoning as a sensor switch (replaying a
        # past recording is a discontinuous input stream relative to
        # whatever live session state exists), using the CURRENT
        # touchid_config/touchid_idle_baseline: the baseline is a property
        # of the sensor/hardware, not of live-vs-replay, so reusing the live
        # session's baseline here is correct.
        self.touchid_processor = self._touchid_new_processor()
        self._touchid_reset_region_coloring()
        self._clear_touchid_stream_curves()
        self._touchid_clear_inference_regions()
        self._touchid_reset_stream_display()
        self._touchid_reset_smoother()

        self._touchid_replay_snapshot = snapshot
        self._touchid_replay_channel_indices = channel_indices
        self._touchid_replay_fs = fs
        self._touchid_replay_cursor = 0
        self._touchid_replay_hop_n = max(1, round(self.touchid_config.hop_size_s * fs))

        self._touchid_replay_timer = QTimer()
        self._touchid_replay_timer.setInterval(0)
        self._touchid_replay_timer.timeout.connect(self._touchid_replay_tick)
        self._touchid_replay_timer.start()

    def _touchid_replay_tick(self):
        """One replay step: feed the next hop_size_s-worth-of-sweeps slice of
        the snapshot into touchid_processor, same chunk size live uses per
        QTimer tick, then paint + submit exactly like live -- except replay
        must WAIT for a still-in-flight classification rather than drop it
        (see touchid_worker_busy's check below): the hard constraint is that
        every window live's algorithm would have decided to classify
        actually gets classified and included in the resulting overlay, so
        neither submitting a new window nor advancing the feed cursor is
        allowed while one is still pending.
        """
        if self.touchid_worker_busy:
            return  # wait -- do not drop, do not advance (see docstring above)

        snapshot = self._touchid_replay_snapshot
        total = snapshot.sweep_count
        cursor = self._touchid_replay_cursor
        if cursor >= total:
            self._touchid_finish_replay()
            return

        fs = self._touchid_replay_fs
        end = min(cursor + self._touchid_replay_hop_n, total)
        channel_samples = {
            col: snapshot.data[cursor:end, idx].astype(np.float64)
            for col, idx in zip(self.touchid_config.pzt_columns, self._touchid_replay_channel_indices)
        }
        channel_samples = self.touchid_processor.filter_raw(channel_samples)
        if snapshot.timestamps_s.size:
            timestamps = np.asarray(snapshot.timestamps_s[cursor:end], dtype=np.float64)
        else:
            timestamps = np.arange(cursor, end, dtype=np.float64) / fs
        self._touchid_replay_cursor = end

        render_tick = self.touchid_fast_forward.should_render_tick()
        if render_tick:
            self._touchid_push_stream_display(channel_samples, timestamps)
            self._update_touchid_stream_plot()

        # now_t must NOT be wall-clock time here (unlike live) -- replay runs
        # the whole capture as fast as possible, so time.monotonic() would
        # barely advance between ticks and every span would look far younger
        # than span_stale_timeout_s regardless of how much (sample) time it
        # actually spans, silently disabling ActiveSampleQueue.expire.
        # end/fs instead reproduces the same real-time deltas the algorithm
        # would have seen live for this exact recording (equivalent to the
        # snapshot's own timestamps_s), so segmentation decisions come out
        # identical to live's for the same raw samples.
        now_t = end / fs
        ready_windows = self.touchid_processor.push_chunk(channel_samples, timestamps, fs, now_t=now_t)

        if render_tick:
            for window in ready_windows:
                self._touchid_set_inference_region(window.window_ts, is_inferenced=True, span_id=window.frag_id)
        if ready_windows:
            # Submit the newest of this tick's windows, same policy as live
            # -- but unlike live, nothing here drops the rest going forward:
            # touchid_worker_busy gates the very next tick (see top of this
            # method), so the next hop can't start until this result is back.
            self._touchid_submit_window(ready_windows[-1], fs)

    def on_touchid_stop_replay_clicked(self):
        """Stop an in-progress 'Run on Analysis Source' replay early.
        Windows classified up to this point are kept -- _touchid_finish_replay
        summarizes exactly whatever's in touchid_replay_results so far, same
        as a replay that ran to completion."""
        if self.touchid_mode != TouchIdMode.REPLAYING:
            return
        self._touchid_finish_replay(stopped_early=True)

    def on_touchid_fast_forward_toggled(self, *_args):
        self.touchid_fast_forward.set_enabled(self.touchid_fast_forward_check.isChecked())

    def on_touchid_load_last_inference_clicked(self):
        """Reapply the last completed replay's predictions to the Analysis
        overlay without re-running the model, as long as the Analysis tab's
        analysis_snapshot is still the exact object that replay ran over
        (identity check -- load_analysis_source always assigns a brand new
        snapshot object, so any intervening Load, including a re-Load of the
        same file, invalidates this)."""
        snapshot = getattr(self, 'analysis_snapshot', None)
        if (
            snapshot is None
            or self._touchid_last_inference_snapshot is None
            or snapshot is not self._touchid_last_inference_snapshot
        ):
            QMessageBox.warning(
                self, 'TouchID',
                'The Analysis source has changed since the last replay -- '
                "use 'Run on Analysis Source' to classify it.",
            )
            self.touchid_load_last_inference_btn.setEnabled(False)
            return

        self.analysis_predicted_labels = self._touchid_last_inference_predictions
        if hasattr(self, 'visualization_tabs') and hasattr(self, 'analysis_tab_index'):
            self.visualization_tabs.setCurrentIndex(self.analysis_tab_index)
        if hasattr(self, 'analysis_inner_tabs'):
            self.analysis_inner_tabs.setCurrentIndex(0)
        self._touchid_show_predicted_labels_exclusively()
        if hasattr(self, 'log_status'):
            self.log_status(
                f'TouchID: reloaded {len(self._touchid_last_inference_predictions)} cached '
                'predictions onto analysis source'
            )

    def _touchid_show_predicted_labels_exclusively(self):
        """Show the predicted-labels overlay and hide the manual-labels
        overlay, in that order: uncheck 'Enable Labeling' first (and let it
        re-render, clearing manual regions) before checking 'Show Predicted
        Labels' (and re-rendering again to draw predicted regions). Showing
        both together is ambiguous -- overlapping regions from two label
        sets on the same trace are hard to read -- so a replay finishing or
        'Load Last Inference' always leaves exactly the predicted overlay
        visible, never both at once."""
        if (
            hasattr(self, 'analysis_labeling_enabled_check')
            and self.analysis_labeling_enabled_check.isChecked()
        ):
            self.analysis_labeling_enabled_check.setChecked(False)
        if (
            hasattr(self, 'analysis_show_predicted_labels_check')
            and not self.analysis_show_predicted_labels_check.isChecked()
        ):
            self.analysis_show_predicted_labels_check.setChecked(True)
        if hasattr(self, '_render_label_regions'):
            self._render_label_regions()

    def _touchid_replay_breakdown_lines(self, predictions: list[dict], label: str) -> list[str]:
        """Per-class share of total windows and mean top-1 confidence within
        that class, plus an overall mean confidence, for ONE prediction
        series (either raw window-level or smoothed-N-window) -- the
        "which classes is this run actually confident about" readout,
        not just a raw window count."""
        total = len(predictions)
        if total == 0:
            return [label, '  No windows.']

        per_class = {}
        for cls in self.touchid_config.class_names:
            confs = [p['confidence'] for p in predictions if p['class'] == cls]
            pct = 100.0 * len(confs) / total
            avg_conf = (sum(confs) / len(confs)) if confs else 0.0
            per_class[cls] = (len(confs), pct, avg_conf)

        # Most source files/captures are single-material -- the dominant
        # class's share IS the purity of this run against that assumption,
        # so surface it up front rather than making the reader scan the
        # per-class table to find it. Low purity (dominant class well under
        # 100%) or a low dominant avg confidence both point at the same
        # tuning knobs: window/hop size, confidence threshold, idle gate k,
        # smoothing N.
        dominant_cls = max(per_class, key=lambda c: per_class[c][0])
        dom_count, dom_pct, dom_avg_conf = per_class[dominant_cls]
        overall_avg_conf = sum(p['confidence'] for p in predictions) / total

        lines = [
            f'{label} ({total} windows)',
            f'  Dominant class: {dominant_cls}  ({dom_pct:.1f}% of windows, avg confidence {dom_avg_conf:.2f})',
        ]
        for cls in self.touchid_config.class_names:
            count, pct, avg_conf = per_class[cls]
            lines.append(f'    {cls:<14} {count:>5} windows  ({pct:5.1f}%)  avg confidence {avg_conf:.2f}')
        lines.append(f'  Overall mean confidence: {overall_avg_conf:.2f}')
        return lines

    def _touchid_replay_summary_text(
        self, raw_predictions: list[dict], smoothed_predictions: list[dict], stopped_early: bool,
    ) -> str:
        """Two separate breakdowns side by side in one report: raw
        window-level predictions (exactly what each individual window's
        argmax said) and smoothed-N-window predictions (the majority-vote
        label / median confidence the live display actually shows) -- so a
        single-material run (e.g. all-tiona) can be checked for how much
        the smoother's N actually improves purity/confidence over the raw
        per-window numbers, not just what the final smoothed number is."""
        header = 'TouchID replay results'
        if stopped_early:
            header += ' (stopped early)'
        if not raw_predictions:
            return f'{header}\nNo windows were classified.'

        lines = [header, '']
        lines += self._touchid_replay_breakdown_lines(raw_predictions, 'Window-level (raw, unsmoothed)')
        lines.append('')
        lines += self._touchid_replay_breakdown_lines(
            smoothed_predictions, f'Smoothed (N={self.touchid_config.smoothing_window_n})')
        return '\n'.join(lines)

    def _touchid_finish_replay(self, stopped_early: bool = False):
        """Replay is done -- either the snapshot was exhausted and the final
        submission's result came back, or the user clicked Stop early.
        Converts touchid_replay_results into the overlay shape the Analysis
        tab expects, computes+shows the aggregate per-class confidence
        summary, restores live streaming state, and switches to the Analysis
        tab so the user can view the predictions, mirroring
        on_touchid_run_on_source_clicked's old end-of-run behavior."""
        if self._touchid_replay_timer is not None:
            self._touchid_replay_timer.stop()
            self._touchid_replay_timer.deleteLater()
            self._touchid_replay_timer = None

        raw_predictions = []
        smoothed_predictions = []
        for window_ts, probs, smoothed_top_class, smoothed_top_conf in self.touchid_replay_results:
            top_class, top_conf = max(probs.items(), key=lambda kv: kv[1])
            start_s, end_s = float(window_ts[0]), float(window_ts[-1])
            raw_predictions.append({
                'start_s': start_s, 'end_s': end_s,
                'class': top_class, 'confidence': float(top_conf), 'probs': probs,
            })
            smoothed_predictions.append({
                'start_s': start_s, 'end_s': end_s,
                'class': smoothed_top_class, 'confidence': float(smoothed_top_conf),
            })
        # The Analysis-tab overlay shows the raw per-window labels -- exactly
        # what each window's own classification produced, unaffected by the
        # smoother -- so it isn't itself smeared by majority-vote lag.
        self.analysis_predicted_labels = raw_predictions
        self._touchid_last_inference_snapshot = self._touchid_replay_snapshot
        self._touchid_last_inference_predictions = raw_predictions
        if hasattr(self, 'touchid_load_last_inference_btn'):
            self.touchid_load_last_inference_btn.setEnabled(bool(raw_predictions))

        summary = self._touchid_replay_summary_text(raw_predictions, smoothed_predictions, stopped_early)
        if hasattr(self, 'log_status'):
            self.log_status(
                f'TouchID: replay predicted {len(raw_predictions)} windows on analysis source'
                + (' (stopped early)' if stopped_early else '')
            )
            for line in summary.splitlines():
                self.log_status(line)
        QMessageBox.information(self, 'TouchID', summary)

        self.touchid_run_on_source_btn.setEnabled(True)
        self.touchid_stop_replay_btn.setEnabled(False)

        self._touchid_show_predicted_labels_exclusively()

        self.touchid_mode = TouchIdMode.NORMAL
        self.touchid_replay_results = []
        # Discontinuous stream after a replay -- rebuild a fresh live
        # processor, same reasoning as a sensor switch, so live streaming
        # resumes clean rather than carrying replay's causal state forward.
        self.touchid_processor = self._touchid_new_processor()
        self._touchid_reset_region_coloring()
        self._touchid_reset_read_cursor()
        self._clear_touchid_stream_curves()
        self._touchid_clear_inference_regions()
        self._touchid_reset_stream_display()
        self._touchid_reset_smoother()
        self.sync_touchid_timer_state()

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
        if self.touchid_mode != TouchIdMode.NORMAL:
            QMessageBox.warning(
                self, 'TouchID',
                'Cannot record an idle baseline while a replay is in progress. Wait for it to finish first.'
                if self.touchid_mode == TouchIdMode.REPLAYING
                else 'An idle baseline capture is already in progress.',
            )
            return
        if self.touchid_idle_capture_active:
            return
        self.touchid_mode = TouchIdMode.CAPTURING_BASELINE
        self.touchid_idle_capture_active = True
        self.touchid_idle_capture_samples = []
        self.touchid_idle_capture_n_samples = 0
        self.touchid_capture_idle_btn.setEnabled(False)
        self._update_touchid_idle_gate_label('recording, keep the sensor untouched...')

    def _finish_touchid_idle_capture(self, fs: float):
        samples = np.concatenate(self.touchid_idle_capture_samples, axis=0)
        baseline = fit_idle_baseline(
            samples, self.touchid_config.pzt_columns, fs, k=self.touchid_config.idle_gate_k)
        self.touchid_idle_baseline = baseline
        # A freshly captured baseline means segmentation should start fresh
        # under it rather than replaying already-elapsed history (which was
        # never being stored while no baseline existed) through a new queue.
        self.touchid_processor = self._touchid_new_processor()
        self._touchid_reset_region_coloring()
        self._touchid_reset_read_cursor()
        self.touchid_mode = TouchIdMode.NORMAL
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
        self._touchid_reset_smoother()
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

        # Read exactly the sweeps written since the last tick's read, using
        # raw_data_buffer's own absolute write position (buffer_write_index)
        # as the cursor -- NOT a duration estimate. An estimate sized off
        # get_measured_sweep_rate_hz() drifts against the buffer's actual
        # position (that rate is itself a measurement), so on any tick where
        # the true arrival count exceeds the estimate, the oldest sweeps in
        # that gap are never read by any tick -- a slow, compounding leak,
        # not just an occasional stall. Reading off the real write index has
        # nothing to drift: pending is exactly right every tick.
        if not hasattr(self, 'buffer_lock'):
            self._update_touchid_idle_gate_label('waiting for sweep data')
            return
        with self.buffer_lock:
            current_write_index = self.buffer_write_index

        if self.touchid_read_cursor_abs is None:
            # First tick since start (or since a discontinuity reset) --
            # start exactly from here rather than reading whatever backlog
            # happened to accumulate before TouchID started watching.
            self.touchid_read_cursor_abs = current_write_index

        pending_sweeps = current_write_index - self.touchid_read_cursor_abs
        if pending_sweeps <= 0:
            self._update_touchid_idle_gate_label('waiting for sweep data')
            return

        max_buffer = getattr(self, 'MAX_SWEEPS_BUFFER', None)
        if max_buffer and pending_sweeps > max_buffer:
            # Fell behind by more than the ring buffer holds -- the oldest
            # pending sweeps have already been overwritten and can't be
            # recovered. Catch up to what's still actually available
            # instead of reading stale/wrapped data.
            dropped = pending_sweeps - max_buffer
            if hasattr(self, 'log_status'):
                self.log_status(
                    f'TouchID: fell behind by {dropped} sweeps (ring buffer overrun) -- catching up'
                )
            self.touchid_read_cursor_abs = current_write_index - max_buffer
            pending_sweeps = max_buffer

        extracted = self._extract_recent_sweeps(pending_sweeps) if hasattr(self, '_extract_recent_sweeps') else None
        if extracted is None:
            self._update_touchid_idle_gate_label('waiting for sweep data')
            return
        data_array, sweep_timestamps = extracted
        # Advance by exactly how many sweeps were actually returned (not by
        # pending_sweeps) so a short read never leaves the cursor ahead of
        # data that was never actually consumed.
        self.touchid_read_cursor_abs += len(data_array)

        channel_samples = {
            col: data_array[:, idx] for col, idx in index_map.items()
        }
        channel_samples = self.touchid_processor.filter_raw(channel_samples)

        if self.touchid_mode == TouchIdMode.CAPTURING_BASELINE:
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

        fs = self.get_measured_sweep_rate_hz()
        if fs <= 0:
            return

        if hasattr(self, 'touchid_sample_rate_label'):
            self.touchid_sample_rate_label.setText(f'Per-channel rate: {fs:.2f} Hz')

        if self.touchid_classifier is None:
            return

        # Inference is off either because the user explicitly stopped it, or
        # because an idle baseline capture is in progress (stopped by
        # default there -- classifying against not-yet-baselined signal
        # would be meaningless, and the capture wants the sensor untouched
        # anyway). The stream plot/sample-rate readout above still update.
        if not self.touchid_inference_enabled or self.touchid_mode == TouchIdMode.CAPTURING_BASELINE:
            # During a baseline capture, the capture branch above already set
            # its own "recording Xs/Ys..." status -- don't clobber it.
            if self.touchid_mode != TouchIdMode.CAPTURING_BASELINE:
                self._update_touchid_idle_gate_label('inference stopped')
            self._touchid_maybe_clear_stale_prediction()
            return

        # Advance the derived-channel causal state by exactly this newly-
        # pushed chunk (not the whole rolling window), so shear/normal stay
        # continuous across window/hop boundaries, then run it through
        # whichever windowing branch is active (fixed grid vs.
        # ActiveSampleQueue segmentation) -- see TouchIdStreamProcessor.push_chunk.
        ready_windows = self.touchid_processor.push_chunk(
            channel_samples, sweep_timestamps, fs, now_t=time.monotonic(),
        )
        self._touchid_handle_ready_windows(ready_windows, fs)

    def _touchid_handle_ready_windows(self, ready_windows: list, fs: float):
        """Paint the "being inferenced" highlight for only the newest window
        this tick's push_chunk produced, then submit that same window to the
        classify worker -- matching touchid_worker_busy's existing policy:
        while a classification is still in flight, don't submit another
        (the worker's own queue is bounded to 1 anyway, so a new submission
        would just evict a still-pending one), skip straight to leaving the
        rest unclassified rather than paying for the array packing below.

        Painting used to loop over every ready window, creating one
        LinearRegionItem (a real Qt widget insertion into the plot's scene
        graph) per window -- so a tick that produced several windows (a
        sustained shear/touch, where push_chunk finalizes multiple
        fragments/hops at once) paid a per-tick GUI cost that scaled with
        how many windows were ready. That's exactly the tick that then runs
        long enough to delay the next QTimer fire and starve the buffer
        read, so the highlight was itself feeding the sample-loss problem it
        was drawn to explain. Painting only the newest window bounds this
        tick's GUI cost to a constant one region, same as classification
        already does -- at the cost of not drawing a separate highlight per
        sub-window during a burst, which was always a display aid, not
        something classification/segmentation correctness depends on."""
        if not ready_windows:
            if self.touchid_processor.idle_baseline is not None:
                self._update_touchid_idle_gate_label('no window ready (idle / accumulating)')
                self._touchid_maybe_clear_stale_prediction()
            return
        self._update_touchid_idle_gate_label()

        newest = ready_windows[-1]
        self._touchid_set_inference_region(newest.window_ts, is_inferenced=True, span_id=newest.frag_id)

        if self.touchid_worker_busy:
            return

        self._touchid_submit_window(newest, fs)

    def _touchid_submit_window(self, window, fs: float):
        self.touchid_worker_busy = True
        self.touchid_classify_worker.submit({
            'window_adc': window.window_adc,
            'window_integrated': window.window_integrated,
            'window_shear_lr': window.window_shear_lr,
            'window_shear_tb': window.window_shear_tb,
            'window_normal': window.window_normal,
            'fs': fs,
            'classifier': self.touchid_classifier,
            'window_ts': window.window_ts,
        })

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

        # Same now_t domain the processor itself uses for this mode (see
        # push_chunk's now_t in update_touchid_display vs. _touchid_replay_tick):
        # replay must NOT use wall-clock time here, since replay runs a whole
        # capture as fast as possible and time.monotonic() would barely
        # advance between windows, defeating the smoother's age-based pruning.
        # window_ts[-1] is replay's own synthetic elapsed-time domain
        # (end/fs), equivalent to what _touchid_replay_tick passed as now_t.
        smoother_now_t = (
            float(window_ts[-1]) if self.touchid_mode == TouchIdMode.REPLAYING else time.monotonic()
        )

        excluded = self.touchid_config.guilty_clip_filter_enabled and is_guilty_candidate(
            probs, self.touchid_smoother.majority_label())
        if excluded and self._touchid_last_smoothed is not None:
            smoothed = self._touchid_last_smoothed
            smoothed_top_class, smoothed_top_conf = self._touchid_last_smoothed_top
        else:
            smoothed = self.touchid_smoother.update(probs, now_t=smoother_now_t)
            # Computed unconditionally (not just when "Show smoothed" is checked)
            # so a replay run always has both the raw window-level label and the
            # windowed-vote/median label available to compare, regardless of
            # what the live display happens to be showing.
            smoothed_top_class, smoothed_top_conf = self.touchid_smoother.top_class(smoothed)
            self._touchid_last_smoothed = smoothed
            self._touchid_last_smoothed_top = (smoothed_top_class, smoothed_top_conf)

        if self.touchid_mode == TouchIdMode.REPLAYING:
            # Collected here (rather than at submission time) so both the raw
            # probs used for the final Analysis-tab overlay AND the smoothed
            # label/confidence at this exact point in the sequence are
            # exactly what the classifier/smoother produced -- see
            # _touchid_finish_replay, which builds separate raw-window-level
            # and smoothed-N-window summaries from this.
            self.touchid_replay_results.append((window_ts, probs, smoothed_top_class, smoothed_top_conf))

        if self.touchid_show_smoothed:
            display_probs = smoothed
            top_class, top_conf = smoothed_top_class, smoothed_top_conf
        else:
            display_probs = probs
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
                curve.setDownsampling(ds=_TOUCHID_STREAM_DOWNSAMPLE_FACTOR, auto=False, method='peak')
                self.touchid_stream_curves[col] = curve
            curve.setData(x=x, y=y)

        # Pin the view to exactly the rolling history window the curve data
        # itself covers -- pyqtgraph's default auto-range instead fits the
        # bounding box of EVERY scene item, including the inference-region
        # highlights below. A long-open fragment's regions can span well
        # past the curves' own _TOUCHID_STREAM_HISTORY_S window (each
        # region only ages out once ITS OWN end falls outside that window,
        # so an old fragment's early windows linger visually for a while),
        # which without this call stretched the x-axis wide open and showed
        # a highlighted span with no curve data behind most of it.
        if len(x):
            self.touchid_stream_plot_widget.setXRange(float(x[0]), float(x[-1]), padding=0)

        self._touchid_prune_inference_regions()

    def _touchid_region_color_for_span(self, frag_id):
        """Pick the RGB color for one inference-region highlight.

        frag_id is None in the no-idle-baseline fixed-grid fallback branch,
        which has no fragment concept at all (see
        _TOUCHID_REGION_FALLBACK_COLOR's docstring) -- always the same flat
        color there. With a baseline present, color is keyed to the
        FRAGMENT (one real touch event), not the individual window: every
        window drawn from the same still-open fragment reuses the current
        color, and the color only advances when frag_id actually changes
        (a new touch started). Heavily overlapping windows (hop_size_s <<
        window_size_s) therefore paint as one growing same-colored block per
        touch, rather than strobing through a new color every hop -- which
        read as a segmentation bug even though the underlying windowing was
        correct (the strobe was only ever showing hop cadence, not the
        window span). Distinct colors are still used across DIFFERENT
        fragments so consecutive separate touches remain visually distinct."""
        if frag_id is None:
            return _TOUCHID_REGION_FALLBACK_COLOR
        if frag_id != self.touchid_region_span_id:
            self.touchid_region_span_id = frag_id
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
        span_id (a ReadyWindow.frag_id) maps to a color."""
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
