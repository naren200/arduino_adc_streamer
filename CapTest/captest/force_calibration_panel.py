"""Force Calibration tab of the CapTest window (PyQt6 + pyqtgraph).

The panel only displays and wires: every decision lives in ``force_session`` (controller),
``force_calibration`` (wizard, trial analysis, records), ``force_export`` (electrical inputs
file) and ``capture`` (continuous capture). A dedicated serial receiver drains hardware
captures while the worker decodes and analyses them; plots use immutable snapshots.
"""

from __future__ import annotations

import time
import copy
from pathlib import Path
from typing import List, Optional

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtWidgets

from . import capture as capture_module
from . import force_calibration as fc, force_export, force_model as fm, force_session, sensor_groups
from . import settings as settings_module, force_estimates, force_zero

PLOT_REFRESH_MS = 100
MAX_PLOT_POINTS = 1500
DEFAULT_LAG_US = 1500          # the first hardware test overflowed the board's buffer at 0 us
FRAME_EXTRA_S = 0.58e-6        # measured per-frame overhead beyond clocks and gap (1.58 us/frame)
ROUTES_IN_SCAN = 50
RECORD_BYTES = 12
TOO_FAST_KBPS = 1000.0


def failure_text(trial) -> str:
    """One readable paragraph for a rejected trial (the table keeps the full reasons)."""
    kinds = []
    for reason in trial.reasons:
        parts = [p.strip() for p in reason.split(":")]
        kind = parts[1] if len(parts) > 2 and parts[0] == "CAPTURE" else parts[0]
        if kind not in kinds:
            kinds.append(kind)
    text = "The last trial was rejected (%s)." % ", ".join(kinds[:5])
    if {"OVERRUN", "GAP", "CRC", "LINK"} & set(kinds):
        text += (" Capture data were lost or corrupted; force integration needs an intact stream. "
                 "Press 'Retry failed' and 'Start trial'. If this repeats, check the USB connection "
                 "and increase 'Added lag per sweep' to reduce the data rate.")
    return text


def _thin(t: np.ndarray, y: np.ndarray, limit: int = MAX_PLOT_POINTS):
    """Plot at most ``limit`` points: a long capture holds far more than the screen can show."""
    step = max(1, len(t) // limit)
    return t[::step], y[::step]


def _residual_data(models, samples, leakage=None):
    return {ref: (np.asarray(d["t"]), fm.residuals(models[ref], d["volts"], d["tc"],
                d.get("valid"), d.get("generation"), (leakage or {}).get(ref, fm.NO_LEAKAGE)))
            for ref, d in samples.items() if ref in models}


class CaptureWorker(QtCore.QThread):
    """Reads the capture and advances the timed phases until the trial ends."""

    phase = QtCore.pyqtSignal(str)
    done = QtCore.pyqtSignal(str)

    def __init__(self, controller: force_session.ForceCalibrationController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        last = None
        error = ""
        try:
            while not self._stop and self.controller.session is not None:
                phase = self.controller.tick()
                self.controller.refresh_view(force=phase != last)
                if phase != last:
                    last = phase
                    self.phase.emit(phase)
                self.msleep(2)
        except Exception as exc:                 # reported in the log, never crashes the GUI
            error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            if self.controller.session is not None and (self._stop or error):
                try:
                    self.controller._evidence["worker_error"] = error
                    self.controller.cancel_trial()
                except Exception as exc:
                    error += " Cleanup: " + str(exc)
        self.done.emit(error)


class AnalysisWorker(QtCore.QThread):
    """CPU/disk analysis without blocking Qt or consuming a live capture stream."""
    result = QtCore.pyqtSignal(object, str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.result.emit(self.operation(), "")
        except Exception as exc:
            self.result.emit(None, "%s: %s" % (type(exc).__name__, exc))


class ForceCalibrationPanel(QtWidgets.QWidget):
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window
        self.group_map: Optional[sensor_groups.GroupMap] = None
        self.forces: Optional[force_export.ForceInputs] = None
        self.forces_path: Optional[Path] = None
        self.controller: Optional[force_session.ForceCalibrationController] = None
        self.worker: Optional[CaptureWorker] = None
        self.store: Optional[fc.CalibrationStore] = None
        self._drawn_view = None
        self._failure = ""
        self._measured = ""
        self._last_measure_time = 0.0
        self._controllers = {}
        self._offline_worker = None
        self._polarity_overrides = {}
        self._build()
        self._load_groups()
        self.update_rate_label()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh_plots)
        self._timer.start(PLOT_REFRESH_MS)
        self.refresh_state()

    # ------------------------------------------------------------------- layout
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        controls = QtWidgets.QWidget()
        self.controls_scroll = QtWidgets.QScrollArea()
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.controls_scroll.setWidget(controls)
        outer.addWidget(self.controls_scroll)
        layout = QtWidgets.QVBoxLayout(controls)
        layout.setContentsMargins(4, 4, 4, 4)

        inputs = QtWidgets.QGroupBox("1. Electrical inputs of the package")
        form = QtWidgets.QGridLayout(inputs)
        self.array_combo = QtWidgets.QComboBox()
        self.pzt_combo = QtWidgets.QComboBox()
        self.instance_edit = QtWidgets.QLineEdit()
        self.instance_edit.setPlaceholderText("board instance id (e.g. serial or label)")
        self.routes_label = QtWidgets.QLabel("")
        self.routes_label.setWordWrap(True)
        self.coverage_label = QtWidgets.QLabel("no force-input file")
        self.coverage_label.setWordWrap(True)
        form.addWidget(QtWidgets.QLabel("Array"), 0, 0)
        form.addWidget(self.array_combo, 0, 1)
        form.addWidget(QtWidgets.QLabel("PZT"), 0, 2)
        form.addWidget(self.pzt_combo, 0, 3)
        form.addWidget(QtWidgets.QLabel("Board instance"), 1, 0)
        form.addWidget(self.instance_edit, 1, 1, 1, 3)
        form.addWidget(self.routes_label, 2, 0, 1, 4)
        self.measure_button = QtWidgets.QPushButton("Measure this package")
        self.add_button = QtWidgets.QPushButton("Add results to file...")
        self.load_inputs_button = QtWidgets.QPushButton("Load file...")
        input_actions = QtWidgets.QHBoxLayout()
        for b in (self.measure_button, self.add_button, self.load_inputs_button):
            input_actions.addWidget(b)
        form.addLayout(input_actions, 3, 0, 1, 4)
        form.addWidget(self.coverage_label, 4, 0, 1, 4)
        self.estimate_button = QtWidgets.QPushButton("Use estimated electrical parameters...")
        form.addWidget(self.estimate_button, 5, 0, 1, 4)
        layout.addWidget(inputs)

        cal = QtWidgets.QGroupBox("2. Guided weight calibration")
        grid = QtWidgets.QGridLayout(cal)
        self.d33_spin = QtWidgets.QDoubleSpinBox()
        self.d33_spin.setRange(-1e6, 1e6)
        self.d33_spin.setDecimals(3)
        self.d33_spin.setValue(600.0)
        self.d33_spin.setSuffix(" pC/N")
        self.d33_spin.setToolTip("Initial EFFECTIVE system sensitivity (signed, nonzero). It scales the "
                                 "live display only; the fit does not need a starting value.")
        self.lag_spin = QtWidgets.QSpinBox()
        self.lag_spin.setRange(0, 1_000_000)
        self.lag_spin.setValue(DEFAULT_LAG_US)
        self.lag_spin.setSuffix(" us")
        self.lag_spin.setToolTip("Idle time added after every sweep of all 50 routes. Each lag is a "
                                 "separate calibration (bias grows with the visit rate).")
        self.repeats_spin = QtWidgets.QSpinBox()
        self.repeats_spin.setRange(1, 20)
        self.repeats_spin.setValue(fc.DEFAULT_REPEATS)
        self.repeats_spin.setToolTip("Number of placement/hold/removal trials for each entered mass. "
            "Two different weights are sufficient with repeated trials. Held-out validation needs accepted repeats for both training and testing.")
        self.masses_edit = QtWidgets.QLineEdit(", ".join("%g" % m for m in fc.MASSES_G))
        self.masses_edit.setToolTip("Known calibration masses in grams, separated by commas, semicolons or spaces. "
            "For example: 20, 50, 100, 200 or just 12.5, 25. Trials run from smallest to largest; "
            "Repeats per mass controls repetitions. Changing this list selects a separate trial sequence.")
        self.masses_error_label = QtWidgets.QLabel("")
        self.masses_error_label.setWordWrap(True)
        self.masses_error_label.setStyleSheet("color: #b91c1c;")
        self.hold_spin = QtWidgets.QDoubleSpinBox()
        self.hold_spin.setRange(10.0, 30.0)
        self.hold_spin.setValue(fc.DEFAULT_HOLD_S)
        self.hold_spin.setSuffix(" s")
        self.mount_edit = QtWidgets.QLineEdit()
        self.mount_edit.setPlaceholderText("Optional: fixture, support, load-spreading plate")
        self.mount_edit.setToolTip("Optional note describing how the sensor is mounted, supported or covered by a plate. "
                                  "Saved with the record and logged when a trial starts. Editing this note does not reset trials or change the force calculation.")
        self.contact_edit = QtWidgets.QLineEdit()
        self.contact_edit.setPlaceholderText("Optional: where the weight contacts the plate")
        self.contact_edit.setToolTip("Optional note describing where the weight touches the sensor or plate, such as centre or left edge. "
                                    "Recorded with each trial for later comparison. Editing it does not reset trials or change the force calculation.")
        self.workflow_check = QtWidgets.QCheckBox("Workflow test only (no gain fitting)")
        self.polarity_controls = QtWidgets.QWidget()
        sign_layout = QtWidgets.QHBoxLayout(self.polarity_controls)
        sign_layout.setContentsMargins(0, 0, 0, 0)
        sign_layout.setSpacing(3)
        self.polarity_combos = {}
        for position in sensor_groups.DEFAULT_POSITIONS:
            column = QtWidgets.QVBoxLayout()
            column.setSpacing(0)
            label = QtWidgets.QLabel(position)
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            combo = QtWidgets.QComboBox()
            combo.addItem("+1", 1)
            combo.addItem("−1", -1)
            combo.setMaximumWidth(52)
            self.polarity_combos[position] = combo
            column.addWidget(label)
            column.addWidget(combo)
            sign_layout.addLayout(column)
        self.polarity_controls.setToolTip("Fixed relative sign of each channel before summing group charge. "
            "+1 keeps its sign; −1 reverses it for both loading and removal. For an opposite centre channel, set C to −1. "
            "No absolute values or rectification are applied. Settings are saved separately for each Array/PZT group. "
            "Changing polarity clears drift/leakage corrections, replays preserved trials and invalidates the force fit. "
            "Raw residual plots retain their electrical signs. Signed d33_eff determines the overall force direction. "
            "Clipping in the polarity test is recorded as a note. Known-weight force calibration still requires unclipped measurements.")
        polarity_test_controls = QtWidgets.QWidget()
        test_layout = QtWidgets.QHBoxLayout(polarity_test_controls)
        test_layout.setContentsMargins(0, 0, 0, 0)
        self.polarity_test_button = QtWidgets.QPushButton("Record press test")
        self.polarity_review_button = QtWidgets.QPushButton("Review press signs…")
        test_layout.addWidget(self.polarity_test_button)
        test_layout.addWidget(self.polarity_review_button)
        polarity_test_controls.setToolTip("Check this package before drift and weight calibration. "
            "Click Record press test, then gently press and release the intended plate once. "
            "The app detects both events and stops automatically; no stage buttons or minimum hold. "
            "Review individual press/release signs; unclear channels are never guessed. "
            "A finger test checks relative response signs, not force gain. Repeat at another contact position to check mounting effects.")
        self.workflow_check.setToolTip("Use for a finger press or any unknown force. Captures are preserved, but nominal masses cannot be used to fit gain.")
        self.error_limit_spin = QtWidgets.QDoubleSpinBox()
        self.error_limit_spin.setRange(0, 100)
        self.error_limit_spin.setDecimals(5)
        self.error_limit_spin.setSuffix(" N")
        self.error_limit_spin.setSpecialValueText("Unset — provisional")
        self.error_limit_spin.setToolTip("Maximum acceptable absolute force error in the held-out validation trials, in newtons. "
            "For example, 0.005 N requires each tested loading/removal step to agree with its known reference force within 0.005 N. "
            "This is an accuracy pass/fail criterion, not the largest force you may apply. "
            "Leave it unset to fit and save a provisional calibration without claiming validated accuracy.")
        self.drift_limit_spin = QtWidgets.QDoubleSpinBox()
        self.drift_limit_spin.setRange(.001, 1)
        self.drift_limit_spin.setValue(.15)
        self.drift_limit_spin.setDecimals(3)
        self.drift_limit_spin.setToolTip("Allowed change in accumulated charge during the constant-weight hold, divided by the placement charge step. "
            "0.150 means the reading may change by at most 15% of the loading step between the stable loaded windows after placement and before removal. "
            "Exceeding this keeps the calibration provisional. It does not correct or zero the drift; evaluate leakage separately.")
        self.guard_spin = QtWidgets.QDoubleSpinBox()
        self.guard_spin.setRange(0, 5)
        self.guard_spin.setDecimals(3)
        self.guard_spin.setSuffix(" V above VD")
        self.guard_spin.setSpecialValueText("Off (above-VD guard)")
        self.guard_spin.setToolTip("Optional software cutoff for experiments with a chosen above-VD voltage range. "
            "Normally leave this Off. If set to 0.5 V, a sample above VD + 0.5 V rejects that trial and blocks heavier masses until a successful retry. "
            "Above-VD signals are expected from a PZT; there is no default ceiling. ADC headroom/clipping checks remain active when this guard is Off.")
        fields = [("Initial d33_eff", self.d33_spin),
                                               ("Added lag per sweep", self.lag_spin),
                                               ("Calibration masses [g]", self.masses_edit),
                                               ("Repeats per mass", self.repeats_spin),
                                               ("Hold time", self.hold_spin),
                                               ("Relative polarity", self.polarity_controls),
                                               ("Polarity check", polarity_test_controls),
                                               ("Mounting notes (optional)", self.mount_edit),
                                               ("Contact position (optional)", self.contact_edit),
                                               ("Max. validation error", self.error_limit_spin),
                                               ("Hold drift / step limit", self.drift_limit_spin),
                                               ("Optional excursion guard", self.guard_spin),
                                               ("", self.workflow_check)]
        for row, (label, widget) in enumerate(fields):
            caption = QtWidgets.QLabel(label)
            caption.setToolTip(widget.toolTip())
            grid.addWidget(caption, row, 0)
            grid.addWidget(widget, row, 1)
        grid.addWidget(self.masses_error_label, len(fields), 0, 1, 2)
        hint = QtWidgets.QLabel("The prompts, the action buttons (Start trial, Mass placed now, ...), the "
                                "plots and the step table are on the right of the window.")
        hint.setWordWrap(True)
        grid.addWidget(hint, len(fields) + 1, 0, 1, 2)
        self.zero_duration_spin = QtWidgets.QDoubleSpinBox()
        self.zero_duration_spin.setRange(6, 120)
        self.zero_duration_spin.setValue(10)
        self.zero_duration_spin.setSuffix(" s")
        self.zero_duration_spin.setToolTip("Unloaded drift measurement: discard the first 2 s for settling, "
            "fit a constant residual per channel on the first half of the remaining data and check it on the last half. "
            "Keep the same wiring, scan and plate, with no added weights and hands off. This measures bias/drift, not physical Roff.")
        grid.addWidget(QtWidgets.QLabel("Unloaded measurement"), len(fields) + 2, 0)
        grid.addWidget(self.zero_duration_spin, len(fields) + 2, 1)
        self.zero_button = QtWidgets.QPushButton("Measure unloaded drift")
        self.clear_zero_button = QtWidgets.QPushButton("Clear unloaded correction")
        grid.addWidget(self.zero_button, len(fields) + 3, 0)
        grid.addWidget(self.clear_zero_button, len(fields) + 3, 1)
        self.linear_zero_button = QtWidgets.QPushButton("Drift test → first weight trial")
        self.linear_zero_button.setToolTip("Before collecting weights, keep the same wiring/plate, no added load, hands off. "
            "Find a linear raw-charge section, fit its drift and check on a later window. "
            "Then continue into the first weight trial without stopping acquisition. The correction stays frozen. "
            "The unloaded measurement field sets the fit/check window; settling may take longer (up to 120 s). "
            "Reset trials to begin a new run after weights have already been captured.")
        grid.addWidget(self.linear_zero_button, len(fields) + 4, 0, 1, 2)
        self.zero_label = QtWidgets.QLabel("No separate unloaded correction. Each trial will measure its own baseline.")
        self.zero_label.setWordWrap(True)
        self.zero_label.setToolTip("A successful unloaded measurement supplies a frozen residual bias for each channel. "
            "It is subtracted before dQ = Ctotal × residual in both the live plot and trial analysis. "
            "It never adapts during force changes or holds. Changing scan/model requires a new measurement.")
        grid.addWidget(self.zero_label, len(fields) + 5, 0, 1, 2)
        layout.addWidget(cal)

        # The workspace is shown in the large right-hand area of the window while this tab is
        # selected: prompt, action buttons, rate, plots and the step table.
        self.workspace = QtWidgets.QWidget()
        wlay = QtWidgets.QVBoxLayout(self.workspace)
        wlay.setContentsMargins(4, 4, 4, 4)
        self.prompt_label = QtWidgets.QLabel("Select a package with a complete electrical record.")
        self.prompt_label.setWordWrap(True)
        self.prompt_label.setStyleSheet("font-size: 20px; font-weight: bold; padding: 6px;")
        self.prompt_label.setMinimumHeight(90)
        self.prompt_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
        wlay.addWidget(self.prompt_label)
        self.start_button = QtWidgets.QPushButton("Start trial")
        self.placed_button = QtWidgets.QPushButton("Mass placed now")
        self.clear_button = QtWidgets.QPushButton("Hand clear")
        self.removed_button = QtWidgets.QPushButton("Mass removed now")
        self.cancel_button = QtWidgets.QPushButton("Cancel trial")
        self.retry_button = QtWidgets.QPushButton("Retry failed")
        self.skip_button = QtWidgets.QPushButton("Skip this mass")
        self.reset_button = QtWidgets.QPushButton("Reset trials")
        self.reset_button.setToolTip("Discard the trials of this calibration (their capture files stay on "
                                     "disk) and start again from the first mass.")
        buttons = QtWidgets.QGridLayout()
        for i, b in enumerate((self.start_button, self.placed_button, self.clear_button,
                               self.removed_button, self.cancel_button, self.retry_button,
                               self.skip_button, self.reset_button)):
            b.setMinimumHeight(38)
            buttons.addWidget(b, i // 4, i % 4)
        wlay.addLayout(buttons)
        self.rate_label = QtWidgets.QLabel("")
        self.rate_label.setWordWrap(True)
        wlay.addWidget(self.rate_label)
        self.headroom_label = QtWidgets.QLabel("")
        self.headroom_label.setWordWrap(True)
        wlay.addWidget(self.headroom_label)

        result = QtWidgets.QGroupBox("3. Fit, validate and save")
        rbox = QtWidgets.QGridLayout(result)
        self.fit_button = QtWidgets.QPushButton("Fit and validate")
        self.leak_button = QtWidgets.QPushButton("Evaluate leakage")
        self.save_button = QtWidgets.QPushButton("Save calibration")
        self.load_cal_button = QtWidgets.QPushButton("Browse saved...")
        self.apply_leak_button = QtWidgets.QPushButton("Apply correction")
        self.rollback_leak_button = QtWidgets.QPushButton("Roll back correction")
        for b in (self.apply_leak_button, self.rollback_leak_button):
            b.setToolTip("Replay original trials, re-estimate then freeze baseline bias, invalidate the gain and validation. Refit afterwards.")
        self.review_button = QtWidgets.QPushButton("Review last trial windows...")
        self.replay_button = QtWidgets.QPushButton("Open saved trial evidence...")
        for i, b in enumerate((self.fit_button, self.leak_button, self.save_button,
                               self.load_cal_button)):
            rbox.addWidget(b, i // 2, i % 2)
        rbox.addWidget(self.apply_leak_button, 2, 0)
        rbox.addWidget(self.rollback_leak_button, 2, 1)
        rbox.addWidget(self.review_button, 3, 0, 1, 2)
        rbox.addWidget(self.replay_button, 4, 0, 1, 2)
        layout.addWidget(result)
        layout.addStretch(1)

        self.force_plot = pg.PlotWidget(title="Group relative force (initial d33_eff)")
        self.force_plot.setLabel("left", "force", units="N")
        self.force_plot.setLabel("bottom", "device time", units="s")
        self.force_curve = self.force_plot.plot(pen=pg.mkPen("#1d4ed8", width=2))
        self.force_zero_line = pg.InfiniteLine(pos=0, angle=0, movable=False,
            pen=pg.mkPen("#ff0000", width=1.5, style=QtCore.Qt.PenStyle.DashLine))
        self.force_zero_line.setZValue(5)
        self.force_plot.addItem(self.force_zero_line)
        self.resid_plot = pg.PlotWidget(title="Raw residual per route (mV)")
        self.resid_plot.setToolTip("Electrical prediction residuals before baseline/drift and relative channel polarity correction. "
                                  "Relative polarity changes the charge/force sum, while this diagnostic plot keeps the original electrical signs.")
        self.resid_plot.setLabel("bottom", "device time", units="s")
        self.resid_plot.setLabel("left", "residual", units="mV")
        for plot in (self.force_plot, self.resid_plot):
            plot.getAxis("left").setWidth(70)
        # Residuals provide the shared time bounds even before a force estimate
        # exists. Linked views pan/zoom together while keeping independent Y axes.
        self.force_plot.setXLink(self.resid_plot)
        self.resid_curves = []
        plots = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        plots.addWidget(self.force_plot)
        plots.addWidget(self.resid_plot)
        plots.setSizes([420, 260])
        self.steps_table = QtWidgets.QTableWidget(0, 5)
        self.steps_table.setHorizontalHeaderLabels(["Mass [g]", "Force [N]", "Repeat", "Status", "Reason"])
        self.steps_table.horizontalHeaderItem(1).setToolTip(
            "Expected total group force for the known mass: F = mass [g] / 1000 × g. "
            "Uses the calibration's gravity (default 9.80665 m/s²). This is the reference force, not a measured result.")
        self.steps_table.horizontalHeader().setStretchLastSection(True)
        self.result_label = QtWidgets.QLabel("")
        self.result_label.setWordWrap(True)
        self.result_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
        lower = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        lower.addWidget(self.steps_table)
        lower.addWidget(self.result_label)
        lower.setSizes([650, 350])
        main = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        main.addWidget(plots)
        main.addWidget(lower)
        main.setSizes([600, 280])
        wlay.addWidget(main, 1)

        self.lag_spin.valueChanged.connect(self.update_rate_label)
        self.array_combo.currentIndexChanged.connect(self._array_changed)
        self.pzt_combo.currentIndexChanged.connect(self._group_changed)
        self.measure_button.clicked.connect(self.measure_package)
        self.add_button.clicked.connect(self.add_results_to_file)
        self.load_inputs_button.clicked.connect(self.load_inputs_dialog)
        self.start_button.clicked.connect(self.start_trial)
        self.polarity_test_button.clicked.connect(self.record_polarity_press)
        self.polarity_review_button.clicked.connect(self.review_press_polarity)
        self.placed_button.clicked.connect(lambda: self._mark("placement"))
        self.clear_button.clicked.connect(self._hand_clear)
        self.removed_button.clicked.connect(lambda: self._mark("removal"))
        self.cancel_button.clicked.connect(self.cancel_trial)
        self.retry_button.clicked.connect(self.retry_failed)
        self.skip_button.clicked.connect(self.skip_mass)
        self.reset_button.clicked.connect(self.reset_trials)
        self.fit_button.clicked.connect(self.fit_and_validate)
        self.leak_button.clicked.connect(self.evaluate_leakage)
        self.save_button.clicked.connect(self.save_calibration)
        self.load_cal_button.clicked.connect(self.browse_saved)
        self.estimate_button.clicked.connect(self.estimate_parameters)
        self.apply_leak_button.clicked.connect(lambda: self.apply_correction(False))
        self.rollback_leak_button.clicked.connect(lambda: self.apply_correction(True))
        self.review_button.clicked.connect(self.review_last_trial)
        self.replay_button.clicked.connect(self.open_trial_evidence)
        self.zero_button.clicked.connect(self.measure_unloaded_drift)
        self.linear_zero_button.clicked.connect(self.start_linear_drift_trial)
        self.clear_zero_button.clicked.connect(self.clear_unloaded_correction)
        for widget in (self.lag_spin, self.d33_spin, self.repeats_spin, self.hold_spin, self.guard_spin):
            widget.valueChanged.connect(self._configuration_changed)
        self.workflow_check.toggled.connect(self._configuration_changed)
        self.masses_edit.textChanged.connect(self._configuration_changed)
        self.mount_edit.textChanged.connect(self._notes_changed)
        self.contact_edit.textChanged.connect(self._notes_changed)
        self.instance_edit.textChanged.connect(self._configuration_changed)
        self.error_limit_spin.valueChanged.connect(self._limits_changed)
        self.drift_limit_spin.valueChanged.connect(self._limits_changed)
        for combo in self.polarity_combos.values():
            combo.currentIndexChanged.connect(self._polarity_changed)

    def _limits_changed(self, *_):
        if self.controller is not None:
            self.controller.invalidate("acceptance limits changed; replay, refit and validate")
            self.result_label.setText("Acceptance limits changed. Fit and validate to replay the trials with these limits.")
        self.refresh_state()

    def _context_key(self):
        return (self.group_id(), self.lag_spin.value(), self.d33_spin.value(), self.repeats_spin.value(),
                self.hold_spin.value(), self.instance(), self.workflow_check.isChecked(), self.guard_spin.value(),
                self.masses_edit.text().strip(), tuple(self.relative_polarity_values().values()))

    def relative_polarity_values(self):
        return {position: combo.currentData() for position, combo in self.polarity_combos.items()}

    def _load_polarity_controls(self):
        gid = self.group_id()
        values = (self.forces.relative_polarity(gid) if self.forces is not None and gid
                  else self._polarity_overrides.get(gid, {}))
        for position, combo in self.polarity_combos.items():
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(values.get(position, 1)))
            combo.blockSignals(False)

    def _polarity_changed(self, *_, press_check=None):
        gid = self.group_id()
        if gid is None or self.is_busy():
            return
        values = self.relative_polarity_values()
        changed = self.forces is None or values != self.forces.relative_polarity(gid)
        previous = copy.deepcopy(self.forces.data) if self.forces is not None else None
        try:
            if self.forces is not None:
                self.forces.set_relative_polarity(gid, values, press_check=press_check)
                if self.forces_path:
                    self.forces.save(self.forces_path)
        except (force_export.ForceExportError, OSError) as exc:
            if previous is not None:
                self.forces.data = previous
            self._load_polarity_controls()
            self.result_label.setText("Relative polarity not saved: " + str(exc))
            return
        self._polarity_overrides[gid] = values
        if not changed:
            self.result_label.setText("Press polarity check saved for %s. Current signs agree; no corrections changed." % gid)
            self.refresh_state()
            return
        for key, cached in list(self._controllers.items()):
            if cached.group.group_id == gid:
                cached.invalidate("relative channel polarity changed")
                self._controllers.pop(key)
        ctl = self.controller
        if ctl is None:
            self.refresh_state()
            return
        signs = {route.ref: values[route.position] for route in ctl.group.routes}
        def completed(_):
            self._controllers[self._context_key()] = ctl
            self.result_label.setText("Relative polarity updated. Preserved trials replayed; remeasure drift, refit and validate. "
                                      "Reset trials to start a new drift-to-weight run.")
            self._show_last_trial()
        self._run_analysis(lambda: ctl.set_route_polarity(signs), completed)

    def record_polarity_press(self):
        if self.is_busy():
            return self._refuse("Finish the current capture or analysis first.")
        return self.start_trial(polarity_test=True)

    def review_press_polarity(self):
        ctl = self.controller
        if ctl is None or not (ctl.trials or ctl.last_polarity_check):
            return self._refuse("Record a complete press and release test first.")
        return self._run_analysis(ctl.check_press_polarity, self._show_polarity_review)

    def _show_polarity_review(self, report):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("%s — press/release polarity" % report["group_id"])
        dialog.resize(850, 390)
        layout = QtWidgets.QVBoxLayout(dialog)
        note = QtWidgets.QLabel(report["note"] + "\nSigns are relative to the majority press direction. "
            + ("Events were detected automatically. " if report.get("automatic") else "Review event marks if the action began before its button mark. ")
            + "No signs change until you apply them.")
        note.setWordWrap(True)
        layout.addWidget(note)
        table = QtWidgets.QTableWidget(5, 6)
        table.setHorizontalHeaderLabels(["Channel", "Press [pC]", "Release [pC]", "Status", "Sign", "Notes / reason"])
        for i, route in enumerate(self.controller.group.routes):
            row = report["routes"]["%d:%d" % route.ref]
            notes = list(row.get("notes", []))
            if not notes and (row["range"]["outside_fitted_range"] or row["range"]["above_vd_count"]):
                notes.append("model extrapolation")
            values = (route.position,
                "%.3g" % (row["press_q_c"] * 1e12) if "press_q_c" in row else "—",
                "%.3g" % (row["release_q_c"] * 1e12) if "release_q_c" in row else "—",
                row["status"], "%+d" % row["suggested_sign"] if row["suggested_sign"] else "unclear",
                "; ".join(row["reasons"] + notes))
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                item.setToolTip(value)
                table.setItem(i, col, item)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table)
        status = QtWidgets.QLabel("All channels have clear, opposite press/release responses. Repeat the test to check consistency."
            if report["status"] == "ready_for_review" else
            "Some signs are unclear. Nothing will be applied. Check the plate/contact and press/release directions, then repeat.")
        status.setWordWrap(True)
        layout.addWidget(status)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        apply = buttons.addButton("Apply signs to this package", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        apply.setEnabled(report["status"] == "ready_for_review")
        apply.clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self._apply_press_polarity(report)

    def _apply_press_polarity(self, report):
        ctl = self.controller
        if (self.is_busy() or ctl is None or report["status"] != "ready_for_review"
                or report["group_id"] != self.group_id() or report["model_revision"] != ctl.model_revision()):
            return self._refuse("Polarity evidence is unclear or its package/model changed; review a new test.")
        for route in ctl.group.routes:
            combo = self.polarity_combos[route.position]
            combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(report["routes"]["%d:%d" % route.ref]["suggested_sign"]))
            combo.blockSignals(False)
        self._polarity_changed(press_check=report)
        return True

    def _notes_changed(self, *_):
        if self.controller is not None and not self.is_busy():
            self.controller.mounting = self.mount_edit.text().strip()
            self.controller.contact_position = self.contact_edit.text().strip()

    def _configuration_changed(self, *_):
        if self.controller is not None and self.controller.session is not None:
            return
        self.controller = self._controllers.get(self._context_key())
        self._notes_changed()
        self.clear_displays()
        if self.controller is None:
            self.steps_table.setRowCount(0)
        self.refresh_state()

    def _run_analysis(self, operation, completed):
        if self._offline_worker is not None and self._offline_worker.isRunning():
            self.result_label.setText("Wait for the current analysis to finish.")
            return False
        if self.worker is not None and self.worker.isRunning():
            return False
        self._offline_worker = AnalysisWorker(operation, self)
        self._analysis_owner, self._analysis_completed = self.controller, completed
        self._offline_worker.result.connect(self._analysis_done)
        self._offline_worker.finished.connect(self.refresh_state)
        self._offline_worker.finished.connect(self._refresh_connection)
        self.result_label.setText("Analysing saved data...")
        self._offline_worker.start()
        self.window.set_connected(self.window.session is not None)
        self.refresh_state()
        return True

    @QtCore.pyqtSlot(object, str)
    def _analysis_done(self, value, error):
        if self.sender() is not self._offline_worker or self.controller is not self._analysis_owner:
            return
        completed, self._analysis_completed = self._analysis_completed, None
        if error:
            self.result_label.setText("Analysis did not complete: " + error)
        elif completed is not None:
            completed(value)
        if self.controller is not None and self.controller.sensor_params_error:
            self.window.append_log(self.controller.sensor_params_error)

    @QtCore.pyqtSlot()
    def _refresh_connection(self):
        self.window.set_connected(self.window.session is not None)

    def is_busy(self):
        return ((self.worker is not None and self.worker.isRunning()) or
                (self._offline_worker is not None and self._offline_worker.isRunning()))

    def estimate_parameters(self):
        if self.forces is None or self.group_id() is None:
            return False
        from .force_estimation_panel import EstimateDialog
        dialog = EstimateDialog(self.forces, self.group_id(), self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted or dialog.proposed is None:
            return False
        try:
            force_estimates.apply_estimate(self.forces, self.group_id(), dialog.proposed)
            if self.forces_path:
                self.forces.save(self.forces_path)
        except (force_export.ForceExportError, OSError) as exc:
            self.result_label.setText("Estimate not applied: " + str(exc))
            return False
        for ctl in self._controllers.values():
            ctl.invalidate("electrical estimate changed; create a new calibration")
        self._controllers.clear()
        self.controller = None
        self.result_label.setText("Estimated model saved as a new revision. Package force remains provisional until independently validated.")
        self.refresh_state()
        return True

    def update_rate_label(self) -> None:
        """Estimated sweep period and USB data rate for the chosen added lag."""
        st = self.window.settings
        frame = 16.0 / float(st.spi_clock_hz) + st.gap_ns * 1e-9 + FRAME_EXTRA_S
        sweep_s = (2 * ROUTES_IN_SCAN + 8) * frame + self.lag_spin.value() * 1e-6
        rate = 1.0 / sweep_s
        kbps = rate * ROUTES_IN_SCAN * RECORD_BYTES / 1000.0
        text = ("Estimated sampling rate per channel: about %.0f Hz (revisit every %.0f us), "
                "about %.0f kB/s over USB." % (rate, sweep_s * 1e6, kbps))
        if kbps > TOO_FAST_KBPS:
            text += (" Too fast: at 0 us (about 3500 kB/s) the board's buffer overflowed on the first "
                     "hardware test. Use 1000 us or more.")
        if self._measured:
            text += "\n" + self._measured
        self.rate_label.setText(text)

    def _measured_text(self, sampling: dict) -> str:
        """'Measured sampling rate per channel: B 598 Hz, L 598 Hz ...' for the selected package."""
        gid = self.group_id()
        group = self.group_map.get(gid) if (self.group_map is not None and gid) else None
        if group is None or not sampling:
            return ""
        parts, intervals = [], []
        for r in group.routes:
            info = sampling.get("%d:%d" % (r.adc, r.input))
            if info is None:
                continue
            parts.append("%s %.1f Hz" % (r.position, info["rate_hz"]))
            intervals.append(info["median_interval_us"])
        if not parts:
            return ""
        return ("Measured sampling rate per channel: %s (revisit every %.0f us; worst gap %.0f us)." % (
            ", ".join(parts), float(np.median(intervals)),
            max(i["interval_max_us"] for i in sampling.values() if i is not None)))

    def _show_measured(self, sampling: dict) -> None:
        text = self._measured_text(sampling)
        if text and text != self._measured:     # keep the last measurement when a trial is aborted
            self._measured = text
            self.update_rate_label()

    # ------------------------------------------------------------------- groups
    def _load_groups(self) -> None:
        profile = self.window.driver.force_board_profile
        try:
            self.group_map = sensor_groups.resolve(profile)
        except sensor_groups.GroupMappingError as exc:
            self.group_map = None
            self.coverage_label.setText("Sensor-library mapping unavailable: %s" % exc)
            self.window.append_log("Force calibration: %s" % exc)
            return
        self.array_combo.blockSignals(True)
        self.array_combo.clear()
        for a in self.group_map.arrays():
            self.array_combo.addItem("Array %d" % a, a)
        self.array_combo.blockSignals(False)
        self._array_changed()

    def _array_changed(self) -> None:
        if self.group_map is None:
            return
        array = self.array_combo.currentData()
        self.pzt_combo.blockSignals(True)
        self.pzt_combo.clear()
        for g in self.group_map.ordered():
            if g.array == array:
                self.pzt_combo.addItem(g.pzt, g.group_id)
        self.pzt_combo.blockSignals(False)
        self._group_changed()

    def group_id(self) -> Optional[str]:
        return self.pzt_combo.currentData()

    def _group_changed(self) -> None:
        gid = self.group_id()
        if gid is None or self.group_map is None:
            return
        group = self.group_map.get(gid)
        self._load_polarity_controls()
        routes = ", ".join("%s: ADC%d input %d" % (r.position, r.adc, r.input) for r in group.routes)
        self.routes_label.setText("%s - %s" % (gid, routes))
        ctl = self.controller
        if ctl is None or (ctl.session is None and ctl.group.group_id != gid):
            self.clear_displays()
            self.controller = self._controllers.get(self._context_key())
            self._notes_changed()
        self.refresh_state()

    # ------------------------------------------------------------ electrical file
    def instance(self) -> str:
        return self.instance_edit.text().strip()

    def default_inputs_path(self) -> Path:
        name = "force_inputs_%s.json" % (self.instance() or "board")
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        return settings_module.results_dir(self.window.driver.id) / safe

    def measure_package(self) -> bool:
        gid = self.group_id()
        if gid is None or self.window.session is None:
            return False
        group = self.group_map.get(gid)
        self.window.append_log("Measuring %s (%d routes)" % (gid, len(group.refs)))
        return self.window.start_job(lambda s: s.run_manual(list(group.refs)))

    def _new_inputs(self) -> force_export.ForceInputs:
        return force_export.ForceInputs.new(
            self.group_map, self.window.settings, board_instance=self.instance(),
            simulated=bool(self.window.sim_check.isChecked()),
            firmware=self._firmware_info())

    def _firmware_info(self) -> dict:
        fw = self.window.firmware
        if fw is None or fw.status is None:
            return {}
        return {"name": fw.status.firmware, "version": fw.status.version, "git": fw.status.git}

    def add_results_to_file(self, path: Optional[str] = None) -> bool:
        """Add every package measured so far to the force-input file (created or updated)."""
        session = self.window.session
        if session is None or not session.results or self.group_map is None:
            self.window.append_log("Measure a package first.")
            return False
        target = Path(path) if path else None
        if target is None:
            chosen, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Force-input file", str(self.default_inputs_path()), "JSON (*.json)")
            if not chosen:
                return False
            target = Path(chosen)
        settings = self.window.settings
        try:
            forces = (force_export.ForceInputs.load(target, self.group_map) if target.exists()
                      else self._new_inputs())
        except force_export.ForceExportError as exc:
            self.window.append_log("Cannot update %s: %s" % (target, exc))
            return False
        added = []
        for group in self.group_map.ordered():
            mine = force_export.results_for_group(session.results, group)
            if not mine:
                continue
            try:
                entry = forces.add_package(
                    group, mine, settings, self.group_map, board_instance=self.instance(),
                    simulated=bool(self.window.sim_check.isChecked()), boot_id=session.boot_id)
            except force_export.ForceExportError as exc:
                self.window.append_log(str(exc))
                return False
            added.append("%s %s (rev %d)" % (group.group_id, entry["status"], entry["revision"]))
        if not added:
            self.window.append_log("No measured routes belong to a PZT package.")
            return False
        for gid, signs in self._polarity_overrides.items():
            forces.set_relative_polarity(gid, signs)
        forces.save(target)
        self.forces, self.forces_path = forces, target
        self.controller = None
        self._controllers.clear()
        self._load_polarity_controls()
        self.clear_displays()
        self.window.append_log("Force-input file %s: %s. %s" % (target.name, "; ".join(added),
                                                              forces.summary_text()))
        self.window.append_log("Compact sensor parameters: %s" % forces.sensor_params_path)
        self.refresh_state()
        return True

    def load_inputs_dialog(self) -> None:
        chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load force-input file", str(settings_module.results_dir(self.window.driver.id)),
            "JSON (*.json)")
        if chosen:
            self.load_inputs(chosen)

    def load_inputs(self, path) -> bool:
        try:
            forces = force_export.ForceInputs.load(path, self.group_map)
        except force_export.ForceExportError as exc:
            self.window.append_log("Force-input file refused: %s" % exc)
            return False
        self.forces, self.forces_path = forces, Path(path)
        self._polarity_overrides.clear()
        self.controller = None
        self._controllers.clear()
        self._load_polarity_controls()
        self.instance_edit.setText(forces.data["provenance"].get("board_instance", ""))
        self.window.append_log("Loaded %s: %s" % (Path(path).name, forces.summary_text()))
        self.refresh_state()
        return True

    # ------------------------------------------------------------------ trials
    def prepare(self) -> bool:
        """Create the controller for the selected package from the form."""
        gid = self.group_id()
        if self.forces is None or gid is None or self.window.session is None:
            return False
        try:
            self.controller = force_session.ForceCalibrationController(
                self.forces, self.group_map, gid, self.window.session.client, self.window.settings,
                d33_initial=self.d33_spin.value() * 1e-12, scan_lag_us=self.lag_spin.value(),
                masses=fc.parse_masses(self.masses_edit.text()),
                repeats=self.repeats_spin.value(), hold_s=self.hold_spin.value(),
                board_instance=self.instance(),
                results_dir=self._calibration_dir(), simulated=bool(self.window.sim_check.isChecked()),
                reference_kind="workflow_only" if self.workflow_check.isChecked() else "known_mass",
                limits={"max_error_n": self.error_limit_spin.value() or None,
                        "hold_drift_fraction": self.drift_limit_spin.value()},
                contact_position=self.contact_edit.text().strip(),
                excursion_guard_v=self.guard_spin.value() or None)
        except (force_export.ForceExportError, fc.CalibrationError, capture_module.CaptureError) as exc:
            self.window.append_log("Force calibration cannot start: %s" % exc)
            self.controller = None
            return False
        self.store = fc.CalibrationStore(self._calibration_dir())
        self._notes_changed()
        self._controllers[self._context_key()] = self.controller
        return True

    def _calibration_dir(self) -> Path:
        return settings_module.results_dir(self.window.driver.id) / "force_calibration"

    def _refuse(self, text: str) -> bool:
        """A start that cannot happen says why, on screen and in the log."""
        self._failure = text
        self.window.append_log(text)
        self.refresh_state()
        return False

    def clear_displays(self) -> None:
        """Empty the plots and the live texts (a new trial or another package starts clean)."""
        self._failure = ""
        self._measured = ""
        self._drawn_view = None
        self.force_curve.setData([], [])
        for curve in self.resid_curves:
            curve.setData([], [])
        self.resid_plot.enableAutoRange(axis=pg.ViewBox.XAxis)
        self.headroom_label.setText("")
        self.update_rate_label()

    def reset_trials(self) -> bool:
        """Throw away this calibration's trials and start again from the first mass."""
        if self.worker is not None and self.worker.isRunning():
            return self._refuse("Cancel the running trial first.")
        if self._offline_worker is not None and self._offline_worker.isRunning():
            return self._refuse("Wait for the saved-data analysis to finish.")
        ctl = self.controller
        if ctl is not None and ctl.accepted_events() and ctl.fit is not None and not self._confirm_reset():
            return False
        self.controller = None
        self._controllers.pop(self._context_key(), None)
        self.clear_displays()
        self.steps_table.setRowCount(0)
        self.result_label.setText("")
        self.window.append_log("Force calibration trials reset (capture files stay on disk).")
        self.refresh_state()
        return True

    def _confirm_reset(self) -> bool:
        answer = QtWidgets.QMessageBox.question(
            self, "Reset trials", "The fitted calibration has not been discarded. Reset the trials "
            "anyway? (Saved calibration records are not affected.)")
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    def measure_unloaded_drift(self):
        return self.start_trial(unloaded_duration_s=self.zero_duration_spin.value())

    def start_linear_drift_trial(self):
        return self.start_trial(unloaded_duration_s=self.zero_duration_spin.value(), linear_pretest=True)

    def clear_unloaded_correction(self):
        ctl = self.controller
        if ctl is None:
            return False
        def completed(_):
            self.result_label.setText("Unloaded correction cleared. Trials replayed with their own baseline bias; refit and validate.")
            self.force_curve.setData([], [])
            self._show_last_trial()
        return self._run_analysis(ctl.clear_unloaded_correction, completed)

    def start_trial(self, checked=False, *, unloaded_duration_s=None, linear_pretest=False, polarity_test=False) -> bool:
        try:
            fc.parse_masses(self.masses_edit.text())
        except fc.CalibrationError as exc:
            return self._refuse(str(exc))
        if self._offline_worker is not None and self._offline_worker.isRunning():
            return self._refuse("Wait for the saved-data analysis to finish.")
        if self.worker is not None and self.worker.isRunning():
            return self._refuse("A trial is still running or stopping. Wait for it to end or press "
                                "'Cancel trial'.")
        if self.window.worker is not None and self.window.worker.isRunning():
            return self._refuse("Wait for the running measurement to finish.")
        if self.window.session is None:
            return self._refuse("Connect to the board first.")
        if self.controller is None or self.controller.group.group_id != self.group_id():
            self.clear_displays()
            if not self.prepare():
                return self._refuse("Force calibration cannot start (see the log).")
        ctl = self.controller
        self.clear_displays()                        # every trial starts with empty plots
        try:
            ctl.client.resync()                      # nothing stale from an earlier capture
            ctl.client.capture_require()
        except capture_module.CaptureUnavailable as exc:
            return self._refuse(str(exc))
        except Exception as exc:                     # link or protocol problem
            return self._refuse("The board did not answer: %s. Check the connection or reconnect." % exc)
        try:
            ctl.start_trial(unloaded_duration_s=unloaded_duration_s, linear_pretest=linear_pretest, polarity_test=polarity_test)
        except Exception as exc:
            return self._refuse("Cannot start the trial: %s" % exc)
        if ctl.mounting or ctl.contact_position:
            self.window.append_log("Trial notes — Mounting: %s; Contact position: %s" %
                                   (ctl.mounting or "(none)", ctl.contact_position or "(none)"))
        self.worker = CaptureWorker(ctl, self)
        self.worker.phase.connect(self.refresh_state)
        self.worker.done.connect(self._worker_done)
        self.worker.finished.connect(self.refresh_state)
        self.worker.finished.connect(self._refresh_connection)
        self.worker.start()
        self.window.set_connected(True)
        self.refresh_state()
        return True

    def _worker_done(self, error: str) -> None:
        if error:
            self.window.append_log("Capture worker failed: %s" % error)
        ctl = self.controller
        if ctl is not None and ctl.sensor_params_error:
            self.window.append_log(ctl.sensor_params_error)
        if ctl is not None and ctl.capture_purpose == "polarity_test":
            report = ctl.last_polarity_check
            if report:
                signs = ", ".join("%s %s" % (route.position,
                    "%+d" % report["routes"]["%d:%d" % route.ref]["suggested_sign"]
                    if report["routes"]["%d:%d" % route.ref]["suggested_sign"] else "unclear") for route in ctl.group.routes)
                self.result_label.setText("Polarity %s: %s. Review press signs to apply or repeat." % (report["status"].replace("_", " "), signs))
                self.window.append_log(self.result_label.text())
            if ctl.last_polarity_result is not None:
                self._draw_traces([], [], _residual_data(ctl.models, ctl.last_polarity_result.samples,
                    ctl.leakage_models), reset_x=True)
            self.refresh_state()
            return
        if ctl is not None and ctl.capture_purpose == "unloaded_drift":
            result = ctl.last_unloaded
            if result is not None:
                text = "Unloaded measurement %s." % result["status"]
                if result["status"] == "qualified":
                    text += " Frozen correction applied to plots and force calculation. Held-out drift %.4g → %.4g N/s (initial display sensitivity)." % (
                        result["check_raw_q_rate_c_per_s"] / ctl.d33_initial,
                        result["check_corrected_q_rate_c_per_s"] / ctl.d33_initial)
                    q = fm.accumulate(ctl.models, ctl.last_unloaded_result.samples,
                        force_zero.bias_values(ctl.unloaded_correction), ctl.polarity, ctl.leakage_models,
                        force_zero.drift_values(ctl.unloaded_correction), ctl.route_polarity)
                    self._draw_traces(q.t, q.force(ctl.d33_initial, result["fit_window_s"][1]),
                        _residual_data(ctl.models, ctl.last_unloaded_result.samples, ctl.leakage_models), reset_x=True)
                else:
                    text += " " + "; ".join(result.get("reasons", [])) + ". Previous correction retained."
                    last_check = ctl._evidence.get("linear_pretest_last_check")
                    if last_check:
                        text += " Last drift check: " + ctl.drift_precheck_message(last_check)
                    self._failure = text
                    if ctl.last_unloaded_result is not None:
                        self._draw_traces([], [], _residual_data(ctl.models, ctl.last_unloaded_result.samples,
                            ctl.leakage_models), reset_x=True)
                self.result_label.setText(text)
                self.window.append_log(text)
            self.refresh_state()
            return
        if ctl is not None and ctl.trials:
            last = ctl.trials[-1]
            if ctl.last_weight_result is ctl.last_result:
                self._show_last_trial()
            self._show_measured(last.sampling)
            self._failure = "" if last.accepted else failure_text(last)
            self.window.append_log("Trial %g g repeat %d: %s%s" % (
                last.mass_g, last.repeat, "accepted" if last.accepted else "REJECTED",
                "" if last.accepted else " (" + "; ".join(last.reasons[:6]) + ")"))
            if ctl.reference_kind == "workflow_only" and ctl.last_weight_result is not None and ctl.last_weight_result.valid:
                self._failure = ""
                self.result_label.setText("Finger test recorded. Use 'Review press signs…' to compare individual channels. "
                    "Steady-weight criteria in the table do not determine polarity-test success; no force gain is fitted.")
        self.refresh_state()

    def _mark(self, which: str) -> None:
        ctl = self.controller
        if ctl is None:
            return
        try:
            (ctl.mark_placement if which == "placement" else ctl.mark_removal)()
        except fc.CalibrationError as exc:
            self.window.append_log(str(exc))
        self.refresh_state()

    def _hand_clear(self) -> None:
        ctl = self.controller
        if ctl is None:
            return
        try:
            if ctl.wizard.phase == fc.PLACE_ARMED:
                ctl.hand_clear_placement()
            elif ctl.wizard.phase == fc.REMOVE_ARMED:
                ctl.hand_clear_removal()
        except fc.CalibrationError as exc:
            self.window.append_log(str(exc))
        self.refresh_state()

    def cancel_trial(self) -> None:
        if self.worker is not None:
            self.worker.request_stop()
            self.worker.wait(5000)
        if self.controller is not None:
            self.controller.cancel_trial()
        self.refresh_state()

    def retry_failed(self) -> None:
        if self.controller is None:
            return
        try:
            self.controller.wizard.retry()
            self.clear_displays()
        except fc.CalibrationError as exc:
            self.window.append_log(str(exc))
        self.refresh_state()

    def skip_mass(self) -> None:
        ctl = self.controller
        if ctl is None or ctl.wizard.current is None:
            return
        try:
            ctl.wizard.skip_mass(ctl.wizard.current.mass_g)
        except fc.CalibrationError as exc:
            self.window.append_log(str(exc))
        self.refresh_state()

    # --------------------------------------------------------------- fit and save
    def fit_and_validate(self) -> bool:
        ctl = self.controller
        if ctl is None:
            return False
        limits = {**ctl.limits, "max_error_n": self.error_limit_spin.value() or None,
                  "hold_drift_fraction": self.drift_limit_spin.value()}
        def calculate():
            if limits != ctl.limits:
                ctl.invalidate("acceptance limits changed")
                ctl.limits = limits
                ctl.reanalyse_trials()
            ctl.fit_gain()
            validation_text = ""
            try:
                val = ctl.validate()
                validation_text = " Held-out repeat %s: max force error %.4f N; %s." % (
                    ",".join(str(r) for r in val.test_repeats), val.max_force_error_n,
                    "error limit passed" if val.passed else "error limit failed" if val.passed is False
                    else "specify an error limit")
            except (fc.CalibrationError, fm.ModelError) as exc:
                validation_text = " Validation: %s." % exc
            fit = ctl.fit  # validate freezes the training gain as the deployed gain
            record = ctl.build_record()
            text = ("%s: d33_eff %.3f pC/N (initial %.3f). R2 %.4f, deployed RMS force error %.4f N, "
                    "intercept %.3g pC.%s Quality: %s. Skipped masses: %s." % (
                        record["status"].replace("_", " "), fit.d33_pc_per_n,
                        ctl.d33_initial * 1e12, fit.r2, fit.rms_force_error_n, fit.intercept * 1e12,
                        validation_text, "; ".join(record["quality_reasons"]) or "checks passed",
                        ", ".join("%g g" % m for m in ctl.wizard.skipped_masses()) or "none"))
            return text
        def completed(text):
            self.result_label.setText(text)
            self._show_last_trial()
        return self._run_analysis(calculate, completed)

    def evaluate_leakage(self) -> bool:
        ctl = self.controller
        if ctl is None:
            return False
        def completed(fit):
            self.result_label.setText(
                "Leakage %s%s. Evaluation has not applied a correction; a hold slope alone never measures Roff." % (
                    fit.status.replace("_", " "), (": " + fit.reason) if fit.reason else
                    " (effective lam %.4f)" % fit.model.lam))
        return self._run_analysis(ctl.evaluate_leakage, completed)

    def apply_correction(self, rollback=False):
        ctl = self.controller
        if ctl is None:
            return False
        def completed(_):
            self.result_label.setText("Correction %s. Recordings replayed; the previous gain and validation are stale. Refit and validate." %
                                     ("removed" if rollback else "applied"))
            self._show_last_trial()
        return self._run_analysis(ctl.rollback_leakage if rollback else ctl.apply_leakage, completed)

    def review_last_trial(self):
        ctl = self.controller
        if ctl is None or not ctl.trials or ctl.trials[-1].marks is None:
            return False
        old = ctl.trials[-1]
        marks = old.marks
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Review last trial — device seconds")
        form = QtWidgets.QFormLayout(dialog)
        values = {"baseline_start": marks.baseline[0], "baseline_end": marks.baseline[1],
                  "place": marks.place, "place_clear": marks.place_clear,
                  "remove": marks.remove, "remove_clear": marks.remove_clear}
        defaults = {"post_place": (marks.place_clear + ctl.guard_s,
                                    marks.place_clear + ctl.guard_s + ctl.window_s),
                    "pre_remove": (marks.remove - ctl.guard_s - ctl.window_s,
                                    marks.remove - ctl.guard_s),
                    "post_remove": (marks.remove_clear + ctl.guard_s,
                                     marks.remove_clear + ctl.guard_s + ctl.window_s)}
        for key, fallback in defaults.items():
            value = old.windows.get(key) or fallback
            values[key + "_start"], values[key + "_end"] = value
        widgets = {}
        for name, value in values.items():
            widget = QtWidgets.QDoubleSpinBox()
            widget.setRange(0, 1e6)
            widget.setDecimals(5)
            widget.setValue(value)
            form.addRow(name.replace("_", " "), widget)
            widgets[name] = widget
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                             QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return False
        v = {key: widget.value() for key, widget in widgets.items()}
        selected = {key: (v[key + "_start"], v[key + "_end"]) for key in
                    ("post_place", "pre_remove", "post_remove") if key + "_start" in v}
        try:
            revised = fc.Marks((v["baseline_start"], v["baseline_end"]), v["place"], v["place_clear"],
                               v["remove"], v["remove_clear"], **selected)
        except (fc.CalibrationError, fm.ModelError, OSError, ValueError) as exc:
            self.result_label.setText("Windows not changed: " + str(exc))
            return False
        def completed(_):
            self.result_label.setText("Trial reanalysed; refit and validate the revised windows.")
            self._show_last_trial()
        return self._run_analysis(lambda: ctl.edit_trial_marks(len(ctl.trials) - 1, revised), completed)

    def _show_last_trial(self):
        ctl = self.controller
        if ctl is None or not ctl.trials or ctl.last_weight_result is None:
            return
        trial = ctl.trials[-1]
        residuals = _residual_data(ctl.models, ctl.last_weight_result.samples, ctl.leakage_models)
        if not trial.bias:
            self._draw_traces([], [], residuals, reset_x=True)
            return
        bias = {tuple(int(n) for n in key.split(":")): value["bias_v"] for key, value in trial.bias.items()}
        q = fm.accumulate(ctl.models, ctl.last_weight_result.samples, bias, ctl.polarity, ctl.leakage_models,
                          force_zero.drift_values(ctl.unloaded_correction), ctl.route_polarity)
        force = q.force(ctl.fit.d33_eff if ctl.fit else ctl.d33_initial,
                        trial.marks.baseline[1] if trial.marks else None)
        keep = q.t >= trial.marks.baseline[0] if trial.marks else np.ones(len(q.t), dtype=bool)
        self._draw_traces(q.t[keep], force[keep], residuals, reset_x=True)

    def open_trial_evidence(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open saved trial evidence",
            str(self._calibration_dir()), "Trial evidence (*.trial.json)")
        if path:
            return self.replay_trial_evidence(path)
        return False

    def replay_trial_evidence(self, path):
        from .force_evidence import replay_trial
        def calculate():
            body, result, trial, models = replay_trial(path)
            curve = None
            correction = (body["analyses"][-1].get("applied_correction") if trial is not None else body.get("applied_leakage"))
            leak = fm.LeakageModel(correction["lam"], correction.get("v_leak_v"), correction["kind"]) if correction else fm.NO_LEAKAGE
            residuals = _residual_data(models, result.samples, {r: leak for r in models})
            if trial is not None:
                bias = {tuple(int(n) for n in key.split(":")): value["bias_v"] for key, value in trial.bias.items()}
                if bias:
                    q = fm.accumulate(models, result.samples, bias, body["polarity"], {r: leak for r in models},
                                      force_zero.drift_values(body["analyses"][-1].get("unloaded_correction")),
                                      fm.route_signs_from_json(body["analyses"][-1].get("route_polarity", body.get("route_polarity"))))
                    keep = q.t >= trial.marks.baseline[0]
                    curve = (q.t[keep], q.force(body["initial_d33_c_per_n"], trial.marks.baseline[1])[keep])
            elif body.get("unloaded_measurement", {}).get("status") == "qualified":
                measured = body["unloaded_measurement"]
                # The saved unloaded bias was measured against the recorded electrical/leakage model.
                q = fm.accumulate(models, result.samples, force_zero.bias_values(measured),
                                  body["polarity"], {r: leak for r in models}, force_zero.drift_values(measured),
                                  fm.route_signs_from_json(body.get("route_polarity")))
                keep = (q.t >= measured["fit_window_s"][0]) & (q.t <= measured["check_window_s"][1])
                curve = (q.t[keep], q.force(body["initial_d33_c_per_n"], measured["fit_window_s"][1])[keep])
            return body, result, trial, curve, residuals
        def completed(value):
            body, result, trial, curve, residuals = value
            t, force = curve if curve is not None else ([], [])
            self._draw_traces(t, force, residuals, reset_x=True)
            if trial is not None:
                self.result_label.setText("Offline replay: %s; flags: %s; reasons: %s. Force uses the saved initial scale, not an independently validated calibration." %
                    ("accepted for fitting" if trial.accepted else "rejected", ", ".join(trial.flags) or "none", "; ".join(trial.reasons) or "none"))
            elif body.get("purpose") == "polarity_test":
                report = body.get("polarity_check", {})
                signs = ", ".join("%s %s" % (ref, "%+d" % row["suggested_sign"] if row.get("suggested_sign") else "unclear")
                                  for ref, row in report.get("routes", {}).items())
                self.result_label.setText("Offline automatic polarity evidence: %s; %s. Active signs were not changed." %
                    (report.get("status", body["state"]), signs))
            elif body.get("purpose") == "unloaded_drift":
                measured = body.get("unloaded_measurement", {})
                self.result_label.setText("Offline unloaded-drift evidence: %s. %s No correction was applied to the active calibration." %
                    (measured.get("status", body["state"]), "; ".join(measured.get("reasons", []))))
            else:
                self.result_label.setText("Diagnostic recording (%s): %s. No completed event analysis." %
                                          (body["state"], "; ".join(result.reasons()) or "no complete button marks"))
            self._show_measured(fc.sampling_rates(result.samples))
            self.headroom_label.setText("Offline evidence: " + str(path))
        return self._run_analysis(calculate, completed)

    def save_calibration(self) -> bool:
        ctl = self.controller
        if ctl is None or self.store is None:
            return False
        try:
            path = ctl.save(self.store)
        except (fc.CalibrationError, OSError) as exc:
            self.window.append_log("Calibration not saved: %s" % exc)
            return False
        self.window.append_log("Saved %s" % path)
        if ctl.sensor_params_error:
            self.window.append_log(ctl.sensor_params_error)
        self.result_label.setText(self.result_label.text() + " Saved: %s" % path.name)
        self._embed_in_bundle(ctl, path)
        return True

    def _embed_in_bundle(self, ctl, record_path: Path) -> None:
        """Updated bundle: the fitted sensitivity is added to the force-input file (its
        electrical inputs and the record's link to them are unchanged)."""
        if ctl.fit is None or self.forces is None or self.forces_path is None or ctl.build_record()["status"] != "validated":
            return
        if self.forces.electrical_hash() != ctl.forces.electrical_hash():
            self.window.append_log("Force-input file changed; recalibrate before embedding this sensitivity.")
            return
        try:
            self.forces.set_d33_eff(ctl.group.group_id, ctl.fit.d33_eff, {
                "record": record_path.name, "capture_signature": ctl.signature(),
                "status": "validated", "charge_conversion_model": fm.CHARGE_MODEL, "origin": "simulated"
                if ctl.simulated else "hardware"})
            self.forces.save(self.forces_path)
        except (force_export.ForceExportError, OSError) as exc:
            self.window.append_log("Force-input file not updated with the sensitivity: %s" % exc)

    def browse_saved(self) -> List[dict]:
        store = fc.CalibrationStore(self._calibration_dir())
        records = store.list()
        lines = []
        for rec in records:
            problems = []
            if self.controller is not None:
                problems = fc.compatibility(
                    rec, board_instance=self.instance(), layout_hash=self.group_map.layout_hash,
                    capture_signature=self.controller.signature())
            d33 = rec["d33_eff_fitted_c_per_n"]
            lines.append("%s rev %d %s%s%s" % (
                rec["key"]["group_id"], rec["revision"], rec["status"],
                "" if d33 is None else " d33_eff %.2f pC/N" % (d33 * 1e12),
                ("  [review: " + "; ".join(problems) + "]") if problems else ""))
        self.result_label.setText("Saved calibrations:\n" + ("\n".join(lines) or "none"))
        return records

    # ------------------------------------------------------------------ display
    @QtCore.pyqtSlot()
    def refresh_state(self) -> None:
        try:
            fc.parse_masses(self.masses_edit.text())
            mass_error = ""
        except fc.CalibrationError as exc:
            mass_error = str(exc)
        self.masses_error_label.setText(mass_error)
        self.masses_error_label.setVisible(bool(mass_error))
        gid = self.group_id()
        issues = self.forces.quality_issues(gid) if self.forces is not None and gid else []
        ready = self.forces is not None and gid is not None and not issues
        if self.forces is not None:
            status = self.forces.status(gid) if gid else ""
            provenance = self.forces.model_provenance(gid) if gid else ""
            self.coverage_label.setText("%s | %s: %s; %s. %s" %
                (self.forces.summary_text(), gid, status, provenance, "; ".join(issues) or "Model qualified for provisional force fitting"))
        ctl = self.controller
        capture_running = self.worker is not None and self.worker.isRunning()
        running = capture_running or (self._offline_worker is not None and self._offline_worker.isRunning())
        busy = self.window.worker is not None and self.window.worker.isRunning()
        connected = self.window.session is not None
        phase = ctl.wizard.phase if ctl is not None else fc.PREPARE
        polarity_running = ctl is not None and ctl.session is not None and ctl.capture_purpose == "polarity_test"
        finger = self.workflow_check.isChecked()
        self.placed_button.setText("Press starts now" if finger else "Mass placed now")
        self.removed_button.setText("Release starts now" if finger else "Mass removed now")
        self.clear_button.setText(("Press established" if phase == fc.PLACE_ARMED else "Finger clear") if finger else "Hand clear")
        self.measure_button.setEnabled(connected and not busy and not running)
        self.add_button.setEnabled(connected and not running and not busy and bool(self.window.session and self.window.session.results))
        self.start_button.setEnabled(connected and ready and not mass_error and not running and not busy
                                     and (ctl is None or ctl.wizard.phase in (fc.PREPARE, fc.FIT, fc.DONE)
                                          and ctl.wizard.current is not None or ctl is None))
        armed = ctl is not None and ctl.wizard.armed and not polarity_running
        self.placed_button.setEnabled(armed and phase == fc.PLACE_ARMED and "place" not in ctl.wizard.marks)
        self.removed_button.setEnabled(armed and phase == fc.REMOVE_ARMED and "remove" not in ctl.wizard.marks)
        self.clear_button.setEnabled(ctl is not None and not polarity_running and (
            (phase == fc.PLACE_ARMED and "place" in ctl.wizard.marks)
            or (phase == fc.REMOVE_ARMED and "remove" in ctl.wizard.marks)))
        self.cancel_button.setEnabled(capture_running)
        self.array_combo.setEnabled(not running)
        self.pzt_combo.setEnabled(not running)
        self.reset_button.setEnabled(not running and ctl is not None)
        self.retry_button.setEnabled(ctl is not None and not running and any(
            s.status == fc.FAILED_STEP for s in ctl.wizard.steps))
        self.skip_button.setEnabled(ctl is not None and not running and ctl.wizard.current is not None)
        self.fit_button.setEnabled(ctl is not None and not running and bool(ctl.accepted_events()))
        self.leak_button.setEnabled(ctl is not None and not running and bool(ctl.trials))
        self.save_button.setEnabled(ctl is not None and not running)
        self.estimate_button.setEnabled(self.forces is not None and gid is not None and bool(issues) and not running)
        self.apply_leak_button.setEnabled(ctl is not None and ctl.leakage is not None and
                                         ctl.leakage.status == "identified" and not running)
        self.rollback_leak_button.setEnabled(ctl is not None and ctl._applied_leakage is not None and not running)
        self.review_button.setEnabled(ctl is not None and bool(ctl.trials) and ctl.trials[-1].marks is not None and not running)
        self.replay_button.setEnabled(not running)
        self.zero_button.setEnabled(connected and ready and not mass_error and not running and not busy)
        self.linear_zero_button.setEnabled(connected and ready and not mass_error and not running and not busy
                                          and (ctl is None or not ctl.trials))
        self.clear_zero_button.setEnabled(ctl is not None and ctl.unloaded_correction is not None and not running)
        self.zero_duration_spin.setEnabled(not running)
        self.polarity_controls.setEnabled(not running)
        self.polarity_test_button.setEnabled(connected and ready and not mass_error and not running and not busy)
        self.polarity_review_button.setEnabled(ctl is not None and bool(ctl.trials or ctl.last_polarity_check) and not running and not busy)
        if ctl is not None and ctl.unloaded_correction:
            self.zero_label.setText("Frozen unloaded correction active for this package and scan; applied before charge integration.")
        else:
            self.zero_label.setText("No separate unloaded correction. Each trial will measure its own baseline.")
        if ctl is not None and ctl.session is not None and ctl._linear_pretest:
            if ctl._unloaded_duration_s is not None:
                self.zero_label.setText("Searching for a linear unloaded section: %.1f s elapsed. The slope may be nonzero; it must stop changing." % ctl.now)
            elif ctl.unloaded_correction:
                measured = ctl.last_unloaded if ctl._checking_frozen_drift else ctl.unloaded_correction
                prefix = "Frozen drift rechecked, correction unchanged" if ctl._checking_frozen_drift else "Drift test passed"
                self.result_label.setText(prefix + ": fitted %.1f–%.1f s, checked %.1f–%.1f s. "
                    "Held-out slope %.4g → %.4g N/s (initial display scale). Frozen correction active; acquisition continues into the weight trial." % (
                        *measured["fit_window_s"], *measured["check_window_s"],
                        measured["check_raw_q_rate_c_per_s"] / ctl.d33_initial,
                        measured["check_corrected_q_rate_c_per_s"] / ctl.d33_initial))
        for widget in (self.lag_spin, self.d33_spin, self.masses_edit, self.repeats_spin, self.hold_spin, self.mount_edit,
                       self.instance_edit, self.contact_edit, self.workflow_check, self.guard_spin, self.load_inputs_button,
                       self.error_limit_spin, self.drift_limit_spin):
            widget.setEnabled(not running)
        if ctl is not None:
            prompt = ctl.wizard.prompt()
            if finger:
                prompt = {
                    fc.PLACE_ARMED: "Press 'Press starts now' as you begin a gentle press on the plate, then 'Press established'.",
                    fc.HOLD: "Keep the press approximately steady for %g s, then release when prompted. Finger force is not a gain reference." % ctl.wizard.hold_s,
                    fc.REMOVE_ARMED: "Press 'Release starts now' as you begin releasing, then 'Finger clear' once your hand is away.",
                    fc.FIT: "Finger test captured. Use 'Review press signs…'. Reset trials to repeat, or turn off workflow test for weight calibration.",
                }.get(ctl.wizard.phase, prompt)
            if ctl.session is not None and ctl._unloaded_duration_s is not None:
                prompt = "Measuring unloaded drift for %g s: no added weights, keep wiring/plate unchanged, hands off." % ctl._unloaded_duration_s
                if ctl._linear_pretest:
                    prompt = "Drift test: waiting for a linear unloaded section and checking its slope. Keep hands off; do not place a weight until prompted."
                    if ctl._checking_frozen_drift:
                        prompt = "Checking the same frozen drift after settling. Keep hands off; no refitting and no weights until prompted."
                    prompt += "\n" + ctl.drift_precheck_message()
            if ctl.capture_purpose == "polarity_test":
                prompt = ctl.polarity_prompt()
            if self._failure and not running:
                prompt = self._failure + "\n\n" + prompt
            self.prompt_label.setText(prompt)
            self._fill_steps(ctl)
        elif not ready:
            self.prompt_label.setText("Select a package with a complete electrical record "
                                      "(measure it, then add it to a force-input file).")
        else:
            prompt = ("%s is ready. Set the added lag and repeats, then press 'Start trial'. The "
                      "board scans both arrays continuously; the prompts tell you when to place "
                      "and remove each mass." % gid)
            if self._failure:
                prompt = self._failure + "\n\n" + prompt
            self.prompt_label.setText(prompt)
        if ctl is None and not ready and self._failure:
            self.prompt_label.setText(self._failure + "\n\n" + self.prompt_label.text())

    def _fill_steps(self, ctl) -> None:
        steps = ctl.wizard.steps
        self.steps_table.setRowCount(len(steps))
        for row, s in enumerate(steps):
            reference_n = s.mass_g * 1e-3 * ctl.g
            for col, text in enumerate(("%g" % s.mass_g, "%.6f" % reference_n, str(s.repeat), s.status, s.reason)):
                item = QtWidgets.QTableWidgetItem(text)
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                if col == 1:
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, reference_n)
                self.steps_table.setItem(row, col, item)

    def refresh_plots(self) -> None:
        ctl = self.controller
        if ctl is None or ctl.session is None:
            return
        view = ctl.latest_view()
        if view is None or view is self._drawn_view:
            return                                   # nothing new: do not redraw
        self._drawn_view = view
        if view.phase == "unloaded_drift" and ctl._linear_pretest:
            self.prompt_label.setText(view.prompt)
            self.zero_label.setText("Unloaded drift check: %.1f s elapsed. Waiting for the quiet-data checks shown above." % view.device_time_s)
        self.force_plot.setTitle("Polarity listening — individual channel responses below" if view.phase == "polarity_test" else
                                "Unloaded drift test — raw charge (initial force scale)"
                                 if view.phase == "unloaded_drift" and ctl._linear_pretest
                                 else "Group relative force (initial d33_eff)")
        now = time.monotonic()
        if now - self._last_measure_time >= 1.0:
            self._last_measure_time = now
            self._show_measured(view.sampling)
        self._draw_traces(view.t, view.force_n, view.residuals, last_s=30.)
        clipped = [("%d:%d" % r) for r, h in view.headroom.items() if h["clipped"]]
        text = "ADC headroom OK." if not clipped else "ADC headroom limit on %s: reject this trial." % ", ".join(clipped)
        if clipped and view.phase == "polarity_test":
            text = "ADC clipping/headroom noted on %s; polarity direction check continues." % ", ".join(clipped)
        extra = ["%d:%d (+%.3f V)" % (*r, h["max_above_vd_v"]) for r, h in view.headroom.items()
                 if h["above_vd_count"]]
        if extra:
            text += " Above-VD extrapolation: " + ", ".join(extra) + ". Force validity remains provisional."
        if any(h["outside_fitted_range"] for h in view.headroom.values()):
            text += " Below-fit voltage: model coverage exceeded."
        if view.problems:
            text += "  Capture problems: " + "; ".join(view.problems[:3])
        self.headroom_label.setText(text)

    def _draw_traces(self, t, force, residuals, *, last_s=None, reset_x=False):
        """Use one capture-clock interval for both plots, live and after replay."""
        t, force = np.asarray(t), np.asarray(force)
        axes = [np.asarray(rt) for rt, _ in residuals.values() if len(rt)]
        if len(t):
            start, end = float(t[0]), float(t[-1])
        elif axes:
            start, end = min(float(rt[0]) for rt in axes), max(float(rt[-1]) for rt in axes)
        else:
            start, end = 0., 0.
        if last_s is not None:
            start = max(start, end - last_s)
        keep = (t >= start) & (t <= end)
        self.force_curve.setData(*_thin(t[keep], force[keep]))
        while len(self.resid_curves) < len(residuals):
            colour = pg.intColor(len(self.resid_curves), hues=max(len(residuals), 5))
            curve = self.resid_plot.plot(pen=pg.mkPen(colour))
            # Keep the full selected data; peak downsampling preserves short
            # press/release spikes that uniform point skipping can miss.
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            self.resid_curves.append(curve)
        for curve in self.resid_curves:
            curve.setData([], [])
        for curve, (rt, e) in zip(self.resid_curves, residuals.values()):
            rt, e = np.asarray(rt), np.asarray(e)
            keep = (rt >= start) & (rt <= end)
            curve.setData(rt[keep], e[keep] * 1e3)
        if reset_x:
            self.resid_plot.enableAutoRange(axis=pg.ViewBox.XAxis)

    def stop_capture(self) -> None:
        """Stop a running capture and park the board (disconnect, window closing)."""
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_stop()
            self.worker.wait(5000)
        if self.controller is not None and self.controller.session is not None:
            self.controller.cancel_trial()

    def shutdown(self) -> None:
        self.stop_capture()
        if self._offline_worker is not None:
            self._offline_worker.wait()
        self._timer.stop()
