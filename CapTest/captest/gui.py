"""PyQt6 + pyqtgraph application of the CapTest tool (one window for every board).

The board is chosen at the top of the window; everything that depends on it (channel
grids, R_bias table, settings, result columns, VMID targets, flash buttons, simulator)
is generated from the board's descriptor and driver (boards.json).
"""

from __future__ import annotations

import sys
import time
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets

from . import __version__, baseline as baseline_module, capfit, flasher, runner
from .help_content import COLUMN_HELP, build_help_html
from . import settings as settings_module
from .boards import DEFAULT_BOARD_ID, BoardDriver, get_registry, load_driver
from .client import CapTestClient, FirmwareInfo
from .link import LinkError, RealClock, SerialLink, VirtualClock, list_serial_ports
from .settings import Channel, Settings
from .simbase import SimLink

TOOLTIPS = {
    "baseline_subtract": "Reports C_net = C_total minus the C_total this input read with no sensor connected "
                         "(the PCB routing and mux/op-amp path, a few to about 20 pF, different per ADC and "
                         "channel). Needs a saved board baseline taken by the same method. The C_total columns "
                         "stay as measured; the bar chart shows C_net while this is on.",
    "baseline_save": "Stores the results on screen as the board baseline of this board "
                     "(settings/<board>_baseline.json). Measure every input with NO sensor connected first.",
    "r_bias":"R_bias tolerance maps 1:1 to C_total (C_total = tau / R_bias). Confirm the "
              "resistor values with a meter.",
    "v_s0": "Assumed sensor start voltage. Use 0 V only when the sensor is known to be uncharged. "
            "A power cycle or the configured discharge wait does not guarantee this.",
    "discharge": "A sensor is left floating after a burst and discharges only by leakage "
                 "(estimated 20+ ms at 25 C on the TestBoard_7953; on the TestBoard_ADC124 "
                 "sensors can stay near VMID for seconds or minutes: run the discharge check). "
                 "Measuring a sensor again sooner makes V_s0 = 0 wrong.",
    "gap": "CS-high time between frames. 200 ns is the minimum that keeps the frame period "
           ">= 1.0 us and the ADC acquisition >= 325 ns at 20 MHz.",
    "fcd": "Assumed delay from CS fall to the first SCLK edge (default 25 ns). It shifts the "
           "connection instant t_c; verify with a scope.",
    "c_par": "Assumption: parasitic capacitance (trace, mux pin) subtracted from the "
             "sensor-side capacitance to give C_PZT.",
    "fit": "Two exponentials describe the PZT charge-up (fast and slow component) far better than "
           "one; tau and C_total then use the slow time constant. 'Two' falls back to one "
           "exponential when the second is not needed (for example an empty channel). The "
           "other model's C_total is shown as '% vs 1 exp'. How the fast/slow components map to "
           "capacitances is not established.",
    "vs0": "Estimated sensor start voltage from the other channels sharing the same mux-output "
           "node (needs at least 5 measured channels). VS0_HIGH beyond +-50 mV: the sensor started "
           "charged (or the estimate is off), so its V0, C_D/C_total and C_D are unreliable; tau is not affected.",
    "injection": "Charge injection from the Vmid channel switching off makes V_D slightly lower "
                 "than Vmid (unspecified in the datasheet): a few percent effect on the ratio, "
                 "none on tau.",
    "pre": "Frames taken while the bank is still parked on VMID before the mux is switched: the "
           "pre-connect reading V_D of every converted mux (rounded up to whole round-robin cycles).",
    "hold": "ADC124S101 hold instant in SCLK periods after the frame start (assumed 3, not checked "
            "with a scope). Shifts all sample times.",
    "enable_delay": "Mux turn-on time after the enable edge (assumed 25 ns; TMUX datasheets give "
                    "tens of ns). Shifts the connection instant.",
    "pop_latency": "The firmware time-stamps each frame when it reads the result: this much later "
                   "than the true end of the frame (assumed 100 ns, polling loop + receive).",
    "baseline": "Number of the last pre-connect samples per input averaged into V_D.",
    "per_address": "One burst per address reads every mux there (up to 4 channels per burst). "
                   "Unticked: one burst per channel; the other muxes are still connected.",
    "method": "Vmid between samples (TestBoard_7953 default, CapTest firmware 1.3.0): one sensor frame, "
              "then Vmid frames, repeated; every second cycle stays connected longer. Gives C_total and "
              "C_share (an effective connection capacitance, not C_D) from one burst without assuming the sensor start voltage. One connection per burst: "
              "the sensor stays connected for the whole burst and an exponential is fitted.",
    "vmid_frames": "Vmid frames after each sensor frame (Vmid between samples). One resets the node fully.",
    "gap_long": "CS-high gap of the frames during which the sensor is connected in every second cycle "
                "(Vmid between samples). The difference from the normal gap sets the accuracy of tau. "
                "auto (0) = 0.3 x R_bias x the Auto-N guess C, 4-40 us.",
    "samples": "Samples per burst (per channel on the TestBoard_ADC124). 0 = automatic: covers 6 tau "
               "of the guessed capacitance.",
}

ID_COLUMNS_7953: List[Tuple[str, str, str]] = [("ADC", "adc", "%d"), ("Ch", "channel", "%d")]
COMMON_COLUMNS: List[Tuple[str, str, str]] = [
    ("Fit", "fit_model", "%s"),
    ("tau [us]", "tau_us", "%.2f"), ("tau fast [us]", "tau_fast_us", "%.2f"),
    ("Fast part [%]", "fast_pct", "%.0f"),
    ("C_total [pF]", "c_total_pf", "%.2f"), ("+/- tol [pF]", "c_total_tol_pf", "%.2f"),
    ("C_total from", "c_total_method", "%s"),
    ("Board baseline [pF]", "c_board_pf", "%.2f"), ("C_net [pF]", "c_net_pf", "%.2f"),
    ("C_total vs 1 exp [%]", "c_total_shift_pct", "%+.0f"),
    ("C_total 1 exp [pF]", "c_total_1exp_pf", "%.2f"), ("C_total 2 exp [pF]", "c_total_2exp_pf", "%.2f"),
    ("C_D/C_total", "ratio", "%.3f"),
    ("ratio 1 exp", "ratio_1exp", "%.3f"), ("ratio 2 exp", "ratio_2exp", "%.3f"), ("C_D [pF]", "c_d_pf", "%.2f"),
    ("C_share/C_total", "c_share_ratio", "%.3f"), ("C_share [pF]", "c_share_pf", "%.2f"),
    ("C sensor [pF]", "c_sensor_pf", "%.2f"), ("C_PZT [pF]", "c_pzt_pf", "%.2f"),
    ("Leak [nA]", "leak_na", "%.1f"), ("V0 [V]", "v0_v", "%.4f"),
    ("Vs0 est [V]", "vs0_est_v", "%+.3f"), ("Vinf [V]", "vinf_v", "%.4f"),
    ("Resid [LSB]", "residual_rms_lsb", "%.2f"), ("Amp [LSB]", "amplitude_lsb", "%.0f"),
    ("M", "bursts_used", "%d"), ("Since prev [s]", "since_previous_s", "%.2f"),
    ("Fresh", "fresh", "%s"), ("Flags", "flags", "%s"),
]
COLUMNS: List[Tuple[str, str, str]] = ID_COLUMNS_7953 + COMMON_COLUMNS   # TestBoard_7953 table


LN_TITLE_IDLE = "ln(d): Vmid between samples only"
RATIO_TITLE_STEP = "C_D / C_total"
RATIO_TITLE_SHARE = "C_share / C_total (effective connection, not C_D)"
RATIO_TITLE_BOTH = "C_D / C_total (one connection) or C_share / C_total (Vmid between samples)"

BAR_OUTLIER_FACTOR = 10.0     # bars above this multiple of the median are drawn cut off
BAR_CUT_HEIGHT = 1.2          # ... at this multiple of the tallest bar that is not cut


def clip_outliers(values: np.ndarray, factor: float = BAR_OUTLIER_FACTOR):
    """Bars above factor x the median of the positive finite values are outliers (a broken or
    misfitted channel). The median, not the mean: a single huge value raises the mean enough to
    hide itself. An outlier is drawn only BAR_CUT_HEIGHT x the tallest normal bar high, so the
    others stay readable; the caller labels it with its true value.
    Returns (heights to draw, outlier mask, outlier threshold)."""
    values = np.asarray(values, dtype=np.float64)
    usable = values[np.isfinite(values) & (values > 0)]
    if len(usable) < 2:
        return np.nan_to_num(values), np.zeros(len(values), dtype=bool), float("nan")
    cap = factor * float(np.median(usable))
    clipped = np.isfinite(values) & (values > cap)
    normal = np.nan_to_num(values)[~clipped]
    drawn = BAR_CUT_HEIGHT * float(normal.max()) if len(normal) and normal.max() > 0 else cap
    return np.where(clipped, drawn, np.nan_to_num(values)), clipped, cap


def SimConfig(board: Optional[str] = None, **kw):
    """Simulator configuration of a board (default: the TestBoard_7953's)."""
    return load_driver(board).sim_config(**kw)


def SimulatedDevice(config, clock=None, r_bias_table=None):
    """Emulated device built from a configuration returned by SimConfig()."""
    return config.create_device(clock=clock, r_bias_table=r_bias_table)


class FieldSpec(NamedTuple):
    setting: str
    attr: str
    label: str
    kind: str                 # int | float | percent | samples | auto_int | bool | fit | method
    low: float = 0.0
    high: float = 0.0
    decimals: int = 0
    step: float = 1.0
    suffix: str = ""
    tip: str = ""


FIELD_SPECS: Dict[str, FieldSpec] = {s.setting: s for s in [
    FieldSpec("n_samples", "s_samples", "Samples per burst (N)", "samples", 0, 4096, 0, tip="samples"),
    FieldSpec("c_guess_pf", "s_guess", "Auto-N guess C", "float", 1, 10000, 0, suffix=" pF"),
    FieldSpec("park_us", "s_park", "Park time", "int", 0, 10000, 0, suffix=" us"),
    FieldSpec("pre_samples", "s_pre", "Pre-connect frames", "int", 1, 64, 0, tip="pre"),
    FieldSpec("gap_ns", "s_gap", "Frame gap (CS high)", "int", 0, 100000, 0, suffix=" ns", tip="gap"),
    FieldSpec("first_clock_delay_ns", "s_fcd", "First-clock delay", "int", 0, 1000, 0, suffix=" ns", tip="fcd"),
    FieldSpec("spi_clock_hz", "s_clock", "SPI clock", "int", 100000, 20000000, 0, 1000000, " Hz"),
    FieldSpec("hold_clocks", "s_hold", "ADC hold instant", "float", 0, 16, 2, 0.1, " SCLK", "hold"),
    FieldSpec("enable_delay_ns", "s_enable_delay", "Mux turn-on delay", "float", 0, 1000, 0, 5, " ns",
              "enable_delay"),
    FieldSpec("pop_latency_ns", "s_pop", "Frame stamp latency", "float", 0, 2000, 0, 10, " ns", "pop_latency"),
    FieldSpec("baseline_samples", "s_baseline", "V_D baseline samples", "int", 1, 64, 0, tip="baseline"),
    FieldSpec("per_address", "s_per_address", "One burst per address", "bool", tip="per_address"),
    FieldSpec("bursts", "s_bursts", "Bursts per channel (M)", "int", 1, 50, 0),
    FieldSpec("discharge_ms", "s_discharge", "Discharge time", "float", 0, 600000, 0, suffix=" ms",
              tip="discharge"),
    FieldSpec("discard_k", "s_discard", "Discard first K", "int", 0, 100, 0),
    FieldSpec("vref", "s_vref", "VREF", "float", 1.0, 5.0, 3, 0.01, " V"),
    FieldSpec("v_s0", "s_vs0", "Sensor start V_s0", "float", -1.0, 3.0, 4, 0.01, " V", "v_s0"),
    FieldSpec("c_parasitic_pf", "s_cpar", "C parasitic", "float", 0, 1000, 1, suffix=" pF", tip="c_par"),
    FieldSpec("bias_tolerance", "s_tol", "R_bias tolerance", "percent", 0, 100, 1, suffix=" %", tip="r_bias"),
    FieldSpec("fit_model", "s_fit", "Fit model", "fit", tip="fit"),
    FieldSpec("method", "s_method", "Method", "method", tip="method"),
    FieldSpec("vmid_frames", "s_vmid_frames", "Vmid frames between samples", "int", 1, 8, 0, tip="vmid_frames"),
    FieldSpec("gap_long_ns", "s_gap_long", "Long-cycle gap", "auto_int", 0, 100000, 0, suffix=" ns", tip="gap_long"),
]}

METHODS = (("Vmid between samples", "alternate"), ("One connection per burst", "burst"))


class Worker(QtCore.QThread):
    log = QtCore.pyqtSignal(str)
    warning = QtCore.pyqtSignal(str)
    progress = QtCore.pyqtSignal(object)
    burst = QtCore.pyqtSignal(object)
    result = QtCore.pyqtSignal(object)
    vinf = QtCore.pyqtSignal(object)
    finished_job = QtCore.pyqtSignal(object, str)

    def __init__(self, session: runner.MeasurementSession, job: Callable, parent=None):
        super().__init__(parent)
        self.session = session
        self.job = job
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        s = self.session
        s.on_log = self.log.emit
        s.on_warning = self.warning.emit
        s.on_progress = self.progress.emit
        s.on_burst = self.burst.emit
        s.on_result = self.result.emit
        s.on_vinf = self.vinf.emit
        s.should_stop = lambda: self._stop
        try:
            value, error = self.job(s), ""
        except Exception as exc:  # reported in the log, never crashes the GUI thread
            value, error = None, "%s: %s" % (type(exc).__name__, exc)
        # Detach from this worker's signals before it is deleted; later calls from
        # the GUI thread (e.g. refresh_boot) must not touch a dead QObject.
        s.on_log = s.on_warning = lambda text: None
        s.on_progress = s.on_burst = s.on_result = s.on_vinf = lambda item: None
        s.should_stop = lambda: False
        self.finished_job.emit(value, error)


class ScriptWorker(QtCore.QThread):
    line = QtCore.pyqtSignal(str)
    finished_job = QtCore.pyqtSignal(int, str)

    def __init__(self, kind: str, port: Optional[str], dry_run: bool = False, parent=None,
                 board: Optional[str] = None):
        super().__init__(parent)
        self.kind, self.port, self.dry_run, self.board = kind, port, dry_run, board

    def run(self) -> None:
        try:
            code = flasher.run_script(self.kind, self.port, self.line.emit, self.dry_run,
                                      board=self.board)
        except Exception as exc:
            self.line.emit("ERROR: %s" % exc)
            code = -1
        self.finished_job.emit(code, self.kind)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, settings: Optional[Settings] = None, board_id: Optional[str] = None,
                 use_saved: bool = False):
        """`use_saved`: start on the last used board with its saved settings (the application
        does; tests and scripts get the defaults, independent of the user's files)."""
        super().__init__()
        self.registry = get_registry()
        self.use_saved = use_saved
        if settings is not None:
            board_id = settings.board_id
        elif board_id is None and use_saved:
            board_id = settings_module.last_board_id()
        entry = self.registry.get(board_id) if board_id else None
        if entry is None or not entry.enabled:
            entry = self.registry.get(DEFAULT_BOARD_ID) if (
                self.registry.get(DEFAULT_BOARD_ID) and self.registry.get(DEFAULT_BOARD_ID).enabled
            ) else self.registry.first_enabled()
        self.entry = entry
        self.driver: BoardDriver = entry.driver
        if settings is None:
            settings = (settings_module.load_board_settings(self.driver.id) if use_saved else None) \
                or (Settings() if self.driver.id == DEFAULT_BOARD_ID else Settings.for_board(self.driver.id))
        self.settings = settings
        self.resize(1500, 950)
        self.session: Optional[runner.MeasurementSession] = None
        self.sim_device = None
        self.sim_virtual_clock = False       # tests: instant discharge waits
        self.offer_flash_on_production = True
        self.firmware: Optional[FirmwareInfo] = None
        self.worker: Optional[Worker] = None
        self.script_worker: Optional[ScriptWorker] = None
        self.port_in_use: Optional[str] = None
        self.channel_boxes: Dict[Channel, QtWidgets.QCheckBox] = {}
        self.auto_adc_checks: Dict[int, QtWidgets.QCheckBox] = {}
        self.table_rows: Dict[Channel, int] = {}
        self.columns: List[Tuple[str, str, str]] = []
        self.warnings: List[str] = []
        self.force_panel = None
        self._build_ui()
        self.populate_board()
        self.refresh_ports()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.update_channel_status)
        self._timer.start(1000)
        self.set_connected(False)

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.addLayout(self._build_connection_bar())

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        root.addWidget(split, 1)
        self.tabs = QtWidgets.QTabWidget()
        split.addWidget(self.tabs)

        right = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        right.addWidget(self._build_plots())
        bottom = self.bottom_tabs = QtWidgets.QTabWidget()
        self.table = self._build_table()
        bottom.addTab(self._build_results_page(), "Results")
        bottom.addTab(self._build_bar_chart(), "Bar chart")
        bottom.addTab(self._build_ln_plot(), "ln(d)")
        bottom.addTab(self._build_vinf_page(), "Vinf")
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        bottom.addTab(self.log_view, "Log")
        right.addWidget(bottom)
        right.setSizes([420, 480])
        # The right side is a stack: the burst plots and tables, or (Force Calibration tab) the
        # force workspace, so that tab can use the whole area.
        self.right_stack = QtWidgets.QStackedWidget()
        self.right_stack.addWidget(right)
        split.addWidget(self.right_stack)
        split.setSizes([430, 1070])
        split.setStretchFactor(0, 0)             # the plots take the extra width, not the settings
        split.setStretchFactor(1, 1)
        self.main_split = split
        self.tabs.currentChanged.connect(self._tab_changed)

        status = QtWidgets.QHBoxLayout()
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_label = QtWidgets.QLabel("idle")
        self.warning_label = QtWidgets.QLabel("")
        self.warning_label.setStyleSheet("color: #b45309; font-weight: bold;")
        status.addWidget(self.progress_bar, 2)
        status.addWidget(self.progress_label, 2)
        status.addWidget(self.warning_label, 3)
        root.addLayout(status)
        self._build_menu()

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&Help")
        self.docs_actions: Dict[str, QtGui.QAction] = {}
        for kind, text in (("protocol", "Firmware protocol and design"),
                           ("user_guide", "User guide"), ("checklist", "Verification checklist")):
            action = menu.addAction(text)
            action.triggered.connect(lambda _=False, k=kind: self.open_doc(k))
            self.docs_actions[kind] = action
        menu.addSeparator()
        about = menu.addAction("About this board")
        about.triggered.connect(self.show_about)

    def open_doc(self, kind: str) -> bool:
        path = self.entry.docs.get(kind)
        if path is None or not path.is_file():
            self.append_log("Document %r of %s not found." % (kind, self.entry.id))
            return False
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
        return True

    def about_text(self) -> str:
        fw = self.entry.firmware
        docs = "\n".join("  %s: %s" % (k, v) for k, v in self.entry.docs.items())
        return ("%s\n\nCapTest %s\nTest firmware: %s (%s, env %s)\nProduction firmware: %s (env %s)\n"
                "Documentation:\n%s" % (self.entry.name, __version__,
                                        fw["firmware_name"], self.entry.project_dir("test"),
                                        fw["test_env"], self.entry.project_dir("production"),
                                        fw["production_env"], docs))

    def show_about(self) -> None:
        QtWidgets.QMessageBox.information(self, "About " + self.entry.id, self.about_text())

    def _build_connection_bar(self) -> QtWidgets.QVBoxLayout:
        outer = QtWidgets.QVBoxLayout()
        board_row = QtWidgets.QHBoxLayout()
        self.board_combo = QtWidgets.QComboBox()
        self.board_combo.setMinimumWidth(380)
        for index, entry in enumerate(self.registry.entries):
            self.board_combo.addItem(entry.name if entry.enabled else "%s (disabled)" % entry.name,
                                     entry.id)
            if not entry.enabled:
                item = self.board_combo.model().item(index)
                item.setEnabled(False)
                item.setToolTip(entry.error)
        self.board_combo.currentIndexChanged.connect(self.on_board_selected)
        self.detect_button = QtWidgets.QPushButton("Detect")
        self.detect_button.setToolTip("Ask the attached firmware who it is (mcu*, capstatus*) and "
                                      "select the board it belongs to.")
        self.detect_button.clicked.connect(self.detect_board)
        board_row.addWidget(QtWidgets.QLabel("Board:"))
        board_row.addWidget(self.board_combo)
        board_row.addWidget(self.detect_button)
        board_row.addStretch(1)
        outer.addLayout(board_row)

        bar = QtWidgets.QHBoxLayout()
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(220)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_ports)
        self.sim_check = QtWidgets.QCheckBox("Simulation mode")
        self.sim_check.setToolTip("Emulated board speaking the real protocol (no hardware).")
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.connect_button.clicked.connect(self.toggle_connection)
        for w in (QtWidgets.QLabel("Port:"), self.port_combo, refresh, self.sim_check,
                  self.connect_button):
            bar.addWidget(w)
        bar.addStretch(1)
        outer.addLayout(bar)

        second = QtWidgets.QHBoxLayout()
        self.firmware_label = QtWidgets.QLabel("not connected")
        self.flash_test_button = QtWidgets.QPushButton("Flash test firmware")
        self.flash_test_button.clicked.connect(lambda: self.flash("test"))
        self.restore_button = QtWidgets.QPushButton("Restore production firmware")
        self.restore_button.clicked.connect(lambda: self.flash("production"))
        self.vmid_adc = QtWidgets.QComboBox()
        self.vmid_button = QtWidgets.QPushButton("VMID read")
        self.vmid_button.clicked.connect(self.read_vmid)
        second.addWidget(QtWidgets.QLabel("Firmware:"))
        second.addWidget(self.firmware_label, 1)
        for w in (self.vmid_adc, self.vmid_button, self.flash_test_button, self.restore_button):
            second.addWidget(w)
        outer.addLayout(second)

        # Always says why the measure buttons are enabled or disabled.
        self.reason_label = QtWidgets.QLabel("")
        self.reason_label.setWordWrap(True)
        self.reason_label.setMargin(6)
        outer.addWidget(self.reason_label)
        return outer

    # ----------------------------------------------------- board-dependent parts
    def populate_board(self) -> None:
        """(Re)build everything that depends on the selected board."""
        driver = self.driver
        self.setWindowTitle("CapTest - %s" % self.entry.name)
        self.board_combo.blockSignals(True)
        self.board_combo.setCurrentIndex(max(self.board_combo.findData(self.entry.id), 0))
        self.board_combo.blockSignals(False)
        self.columns = [(h, k, f) for h, k, f in driver.id_columns()] + COMMON_COLUMNS
        self.table.setColumnCount(len(self.columns))
        self.table.setHorizontalHeaderLabels([c[0] for c in self.columns])
        for column, (_, name, _) in enumerate(self.columns):
            self.table.horizontalHeaderItem(column).setToolTip(COLUMN_HELP.get(name, "Channel identifier."))
        self.table.setRowCount(0)
        self.table_rows.clear()
        self.channel_boxes = {}
        self.auto_adc_checks = {}
        self.tabs.clear()
        self.tabs.addTab(self._build_auto_tab(), "Automatic")
        self.tabs.addTab(self._build_manual_tab(), "Manual")
        self.tabs.addTab(self._build_settings_tab(), "Settings")
        if self.force_panel is not None:
            self.force_panel.shutdown()
            self.right_stack.removeWidget(self.force_panel.workspace)
            self.force_panel.workspace.deleteLater()
            self.force_panel.deleteLater()
            self.force_panel = None
        self.right_stack.setCurrentIndex(0)
        if driver.supports_force_calibration:
            from .force_calibration_panel import ForceCalibrationPanel

            self.force_panel = ForceCalibrationPanel(self)
            self.tabs.addTab(self.force_panel, "Force Calibration")
            self.right_stack.addWidget(self.force_panel.workspace)
        self.tabs.addTab(self._build_help_tab(), "Help")
        targets = driver.descriptor["vmid_read"]
        self.vmid_adc.clear()
        for target in targets["targets"]:
            self.vmid_adc.addItem(targets["label"].format(target), target)
        for kind, action in self.docs_actions.items():
            action.setEnabled(kind in self.entry.docs and self.entry.docs[kind].is_file())
        self.apply_settings(self.settings)
        conflicts = driver.bias_conflicts()
        if conflicts:
            self.append_log("WARNING: bias-resistor sources disagree: " + "; ".join(conflicts[:5])
                            + (" ..." if len(conflicts) > 5 else ""))
        self.update_channel_status()
        self.clear_plots()

    def _tab_changed(self, _index: int) -> None:
        """The Force Calibration tab takes the whole right-hand area for its workspace."""
        panel = getattr(self, "force_panel", None)
        if panel is not None and self.tabs.currentWidget() is panel:
            self.right_stack.setCurrentWidget(panel.workspace)
            total = sum(self.main_split.sizes())
            if total > 0 and self.main_split.sizes()[0] > 0.4 * total:
                self.main_split.setSizes([int(0.3 * total), total - int(0.3 * total)])
        else:
            self.right_stack.setCurrentIndex(0)

    def clear_plots(self) -> None:
        for curve in (self.data_curve, self.fit_curve, self.other_curve, self.resid_curve,
                      self.pre_curve):
            curve.setData([], [])
        self.burst_plot.setTitle("Latest burst and fit")
        for curve in (self.ln_data, self.ln_model, self.ln_slope):
            curve.setData([], [])
        self.ln_plot.setTitle(LN_TITLE_IDLE)
        self.bar_total.clear()
        self.bar_ratio.clear()
        self.compare_label.setText("")

    def _build_auto_tab(self) -> QtWidgets.QWidget:
        driver = self.driver
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        box = QtWidgets.QGroupBox("Measure all valid channels")
        form = QtWidgets.QFormLayout(box)
        row = QtWidgets.QHBoxLayout()
        for group in driver.groups():
            check = QtWidgets.QCheckBox("%s (%d ch)" % (group.title, len(group.channels)))
            check.setChecked(True)
            self.auto_adc_checks[group.id] = check
            row.addWidget(check)
        form.addRow(row)
        self.auto_bursts = QtWidgets.QSpinBox()
        self.auto_bursts.setRange(1, 50)
        self.auto_bursts.setToolTip("Bursts per channel M (results are averaged: mean / std).")
        form.addRow("Bursts per channel (M):", self.auto_bursts)
        self.auto_per_address = QtWidgets.QCheckBox("One burst per address (all muxes at once)")
        self.auto_per_address.setToolTip(TOOLTIPS["per_address"])
        self.auto_per_address.setVisible("per_address" in driver.settings_fields)
        self.auto_per_address.toggled.connect(self.on_per_address_toggled)
        form.addRow(self.auto_per_address)
        layout.addWidget(box)
        buttons = QtWidgets.QHBoxLayout()
        self.run_auto_button = QtWidgets.QPushButton("Run automatic")
        self.run_auto_button.clicked.connect(self.run_auto)
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop_job)
        self.remeasure_button = QtWidgets.QPushButton("Re-measure failed")
        self.remeasure_button.clicked.connect(self.remeasure_failed)
        for b in (self.run_auto_button, self.stop_button, self.remeasure_button):
            buttons.addWidget(b)
        layout.addLayout(buttons)

        check_box = QtWidgets.QGroupBox("Discharge check")
        check_form = QtWidgets.QFormLayout(check_box)
        self.discharge_edit = QtWidgets.QLineEdit(driver.discharge_check["default_channel"])
        self.discharge_edit.setToolTip(TOOLTIPS["discharge"])
        waits = "/".join("%g" % w for w in driver.discharge_check["waits_ms"])
        self.discharge_button = QtWidgets.QPushButton("Run discharge check (%s ms)" % waits)
        self.discharge_button.clicked.connect(self.run_discharge_check)
        check_form.addRow("Channel (%s):" % driver.syntax, self.discharge_edit)
        check_form.addRow(self.discharge_button)
        layout.addWidget(check_box)
        layout.addStretch(1)
        exports = QtWidgets.QGroupBox("Data")
        grid = QtWidgets.QGridLayout(exports)
        for index, (text, slot) in enumerate([
                ("Export CSV", self.export_csv), ("Export JSON", self.export_json),
                ("Save raw bursts", self.save_raw), ("Load raw (re-analyse)", self.load_raw),
                ("Save plots as images", self.save_plots)]
                + ([("Export Force Calculation Data", self.export_force_data)]
                   if self.driver.supports_force_calibration else [])):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(slot)
            grid.addWidget(button, index // 2, index % 2)
        layout.addWidget(exports)
        return tab

    def _build_manual_tab(self) -> QtWidgets.QWidget:
        driver = self.driver
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        top = QtWidgets.QHBoxLayout()
        for text, slot in (("Select all", lambda: self.select_all(True)),
                           ("Select none", lambda: self.select_all(False)),
                           ("Invert", self.invert_selection)):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(slot)
            top.addWidget(button)
        layout.addLayout(top)
        for group in driver.groups():
            box = QtWidgets.QGroupBox(group.title)
            grid = QtWidgets.QGridLayout(box)
            offset = 1 if group.row_labels and group.id in self._labelled_groups() else 0
            for r, cells in enumerate(group.grid):
                if offset:
                    grid.addWidget(QtWidgets.QLabel(group.row_labels[r]), r, 0)
                for c, channel in enumerate(cells):
                    if channel is None:
                        continue
                    check = QtWidgets.QCheckBox(driver.cell_label(channel))
                    check.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
                    check.customContextMenuRequested.connect(
                        lambda pos, ch=channel, b=check: self.channel_menu(ch, b.mapToGlobal(pos)))
                    self.channel_boxes[channel] = check
                    grid.addWidget(check, r, c + offset)
            row = QtWidgets.QHBoxLayout()
            for text, value in (("all", True), ("none", False)):
                b = QtWidgets.QPushButton(text)
                b.setMaximumWidth(50)
                b.clicked.connect(lambda _, a=group.id, v=value: self.select_all(v, [a]))
                row.addWidget(b)
            inv = QtWidgets.QPushButton("invert")
            inv.setMaximumWidth(60)
            inv.clicked.connect(lambda _, a=group.id: self.invert_selection([a]))
            row.addWidget(inv)
            row.addStretch(1)
            grid.addLayout(row, len(group.grid), 0, 1, len(group.col_labels) + offset)
            layout.addWidget(box)
        spec_row = QtWidgets.QHBoxLayout()
        self.spec_edit = QtWidgets.QLineEdit()
        self.spec_edit.setPlaceholderText(driver.example_spec)
        self.spec_edit.setToolTip("Syntax %s; '*' and ranges such as 0-3 are accepted." % driver.syntax)
        apply_button = QtWidgets.QPushButton("Apply")
        apply_button.clicked.connect(self.apply_spec)
        self.spec_edit.returnPressed.connect(self.apply_spec)
        spec_row.addWidget(QtWidgets.QLabel("Channels (%s):" % driver.syntax))
        spec_row.addWidget(self.spec_edit, 1)
        spec_row.addWidget(apply_button)
        layout.addLayout(spec_row)
        self.manual_bursts = QtWidgets.QSpinBox()
        self.manual_bursts.setRange(1, 50)
        form = QtWidgets.QFormLayout()
        form.addRow("Bursts per channel (M):", self.manual_bursts)
        layout.addLayout(form)
        self.measure_selected_button = QtWidgets.QPushButton("Measure selected")
        self.measure_selected_button.clicked.connect(self.measure_selected)
        layout.addWidget(self.measure_selected_button)
        self.vinf_button = QtWidgets.QPushButton("Measure Vinf of selected (separate mode)")
        self.vinf_button.setToolTip(
            "Vinf mode: per channel, a Vmid-between-samples burst (calculated Vinf), a long burst "
            "alternating the sensor with Vmid (measured Vinf = median of the settled samples, as "
            "production does for its baseline) and a burst holding the sensor connected. Results go "
            "to the Vinf tab, not to the capacitance table.")
        self.vinf_button.clicked.connect(self.measure_vinf_selected)
        layout.addWidget(self.vinf_button)
        hint = QtWidgets.QLabel(
            "Right-click a channel for 'Measure this channel now'. Tooltips show the time since it "
            "was last connected%s; * marks channels still fresh since power-up."
            % (" (also as a side effect of measuring a neighbour: a burst connects %s)"
               % self.driver.touches_text if self.driver.touches_text else ""))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        return tab

    def _labelled_groups(self):
        """Groups whose grid rows carry a label (the ADC124's mux rows)."""
        return {g.id for g in self.driver.groups() if g.row_labels and
                g.row_labels[0].upper().startswith("MUX")}

    def _build_settings_tab(self) -> QtWidgets.QWidget:
        driver = self.driver
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        form = QtWidgets.QFormLayout()
        min_hz, max_hz, _ = driver.spi_range
        self._field_names: List[str] = []
        for name in driver.settings_fields:
            spec = FIELD_SPECS[name]
            if spec.kind == "fit":
                widget = QtWidgets.QComboBox()
                widget.addItem("Two exponentials (one when not needed)", capfit.FIT_TWO)
                widget.addItem("One exponential", capfit.FIT_ONE)
                widget.currentIndexChanged.connect(lambda _: self.on_fit_model_changed(self.s_fit))
            elif spec.kind == "method":
                widget = QtWidgets.QComboBox()
                for text, value in METHODS:
                    widget.addItem(text, value)
            elif spec.kind == "bool":
                widget = QtWidgets.QCheckBox()
                widget.toggled.connect(self.on_per_address_setting_toggled)
            else:
                widget = QtWidgets.QDoubleSpinBox()
                low, high = spec.low, spec.high
                if name == "spi_clock_hz":
                    low, high = min_hz, max_hz
                widget.setRange(low, high)
                widget.setDecimals(spec.decimals)
                widget.setSingleStep(spec.step)
                if spec.suffix:
                    widget.setSuffix(spec.suffix)
                if spec.kind in ("samples", "auto_int"):
                    widget.setSpecialValueText("auto")
            if spec.tip:
                widget.setToolTip(TOOLTIPS[spec.tip])
            setattr(self, spec.attr, widget)
            self._field_names.append(name)
            form.addRow(spec.label + ":", widget)
        layout.addLayout(form)
        layout.addWidget(self._build_baseline_box())
        layout.addWidget(QtWidgets.QLabel("R_bias per channel (%s) [ohm] (editable):" % driver.syntax))
        self.bias_table = QtWidgets.QTableWidget(0, 2)
        self.bias_table.setHorizontalHeaderLabels([driver.syntax, "R_bias [ohm]"])
        self.bias_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.bias_table, 1)
        row = QtWidgets.QHBoxLayout()
        for text, slot in (("Save settings", self.save_settings), ("Load settings", self.load_settings)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        layout.addLayout(row)
        caveat = QtWidgets.QLabel(TOOLTIPS["injection"])
        caveat.setWordWrap(True)
        layout.addWidget(caveat)
        return tab

    def _build_baseline_box(self) -> QtWidgets.QGroupBox:
        """Board baseline: C_total of every input with no sensor connected, subtracted on request."""
        box = QtWidgets.QGroupBox("Board baseline (PCB routing capacitance)")
        layout = QtWidgets.QVBoxLayout(box)
        self.s_subtract_baseline = QtWidgets.QCheckBox("Subtract board baseline (report C_net = C_total - baseline)")
        self.s_subtract_baseline.setToolTip(TOOLTIPS["baseline_subtract"])
        self.s_subtract_baseline.toggled.connect(self.on_subtract_baseline_toggled)
        layout.addWidget(self.s_subtract_baseline)
        self.baseline_label = QtWidgets.QLabel()
        self.baseline_label.setWordWrap(True)
        layout.addWidget(self.baseline_label)
        self.baseline_save_button = QtWidgets.QPushButton("Save current results as board baseline...")
        self.baseline_save_button.setToolTip(TOOLTIPS["baseline_save"])
        self.baseline_save_button.clicked.connect(self.save_board_baseline)
        layout.addWidget(self.baseline_save_button)
        return box

    def refresh_baseline_label(self) -> None:
        """One line on the saved baseline of this board and whether it is in use."""
        try:
            saved = baseline_module.load(self.driver.id)
            error = ""
        except baseline_module.BaselineError as exc:
            saved, error = None, str(exc)
        if error:
            text = "Baseline file cannot be used: %s" % error
        elif saved is None:
            text = "No board baseline saved for %s. Measure the inputs with no sensor connected, then save the results." % self.driver.id
        else:
            text = "Saved baseline: %s." % saved.summary()
            notes = saved.mismatches(self.settings)
            if notes:
                text += " Measured with other settings: %s." % "; ".join(notes)
        self.baseline_label.setText(text)

    def on_subtract_baseline_toggled(self, checked: bool) -> None:
        """Switch C_net on or off for the results already on screen (nothing is measured again)."""
        self.settings.subtract_baseline = bool(checked)
        self.refresh_baseline_label()
        if self.session is not None:
            self.session.settings.subtract_baseline = bool(checked)
            self.session.apply_baseline(reload=True)
            self.rebuild_results_view()

    def save_board_baseline(self) -> bool:
        """Save the results on screen as the board baseline."""
        if self.session is None or not self.session.results:
            self.append_log("Nothing to save: measure the inputs with no sensor connected first.")
            return False
        if self.worker is not None and self.worker.isRunning():
            self.append_log("Wait for the running measurement before saving a baseline.")
            return False
        count = len([r for r in self.session.results.values() if not r.failed])
        if not self._confirm(
                "Save the %d usable results as the board baseline of %s?\n\nThey must come from a run with "
                "NO sensor connected; the file replaces any earlier baseline." % (count, self.driver.id),
                "Save board baseline"):
            return False
        try:
            path = self.session.save_baseline(source="GUI")
        except (baseline_module.BaselineError, OSError) as exc:
            self.append_log("Cannot save the baseline: %s" % exc)
            return False
        self.append_log("Board baseline saved to %s (%s)." % (path, self.session.baseline.summary()))
        self.refresh_baseline_label()
        if self.settings.subtract_baseline:
            self.rebuild_results_view()
        return True

    def _build_help_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        sections = QtWidgets.QComboBox()
        for title, anchor in (("Measurement and calculations", "measurement"),
                              ("Results columns", "columns"), ("Quality flags", "flags"),
                              ("Settings", "settings")):
            sections.addItem(title, anchor)
        layout.addWidget(sections)
        self.help_browser = QtWidgets.QTextBrowser()
        self.help_browser.setHtml(build_help_html(self.driver, self.columns, FIELD_SPECS))
        sections.currentIndexChanged.connect(
            lambda index: self.help_browser.scrollToAnchor(sections.itemData(index)))
        layout.addWidget(self.help_browser, 1)
        return tab

    def _build_plots(self) -> QtWidgets.QWidget:
        self.plot_layout = pg.GraphicsLayoutWidget()
        self.burst_plot = self.plot_layout.addPlot(row=0, col=0, title="Latest burst and fit")
        self.burst_plot.setLabel("bottom", "time since connection", "s")
        self.burst_plot.setLabel("left", "voltage", "V")
        self.burst_plot.addLegend()
        self.pre_curve = self.burst_plot.plot(pen=None, symbol="t", symbolSize=5,
                                              symbolBrush=(90, 160, 90), name="pre-connect baseline")
        self.data_curve = self.burst_plot.plot(pen=None, symbol="o", symbolSize=3,
                                               symbolBrush=(70, 130, 180), name="samples")
        self.fit_curve = self.burst_plot.plot(pen=pg.mkPen((220, 50, 50), width=2), name="fit")
        self.other_curve = self.burst_plot.plot(
            pen=pg.mkPen((150, 150, 150), width=1.5, style=QtCore.Qt.PenStyle.DashLine), name="other model")
        self.connect_line = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen((120, 120, 120), width=1,
                                            style=QtCore.Qt.PenStyle.DotLine))
        self.burst_plot.addItem(self.connect_line)
        self.resid_plot = self.plot_layout.addPlot(row=1, col=0, title="Residuals")
        self.resid_plot.setLabel("bottom", "time since connection", "s")
        self.resid_plot.setLabel("left", "residual", "LSB")
        self.resid_plot.setXLink(self.burst_plot)
        self.resid_curve = self.resid_plot.plot(pen=None, symbol="o", symbolSize=3,
                                                symbolBrush=(90, 90, 90))
        self.plot_layout.ci.layout.setRowStretchFactor(0, 3)
        self.plot_layout.ci.layout.setRowStretchFactor(1, 1)
        return self.plot_layout

    def _build_bar_chart(self) -> QtWidgets.QWidget:
        self.bar_layout = pg.GraphicsLayoutWidget()
        self.bar_total = self.bar_layout.addPlot(row=0, col=0, title="C_total per channel")
        self.bar_total.setLabel("left", "C_total", "pF")
        self.bar_ratio = self.bar_layout.addPlot(row=1, col=0, title=RATIO_TITLE_STEP)
        self.bar_ratio.setXLink(self.bar_total)
        return self.bar_layout

    def _build_ln_plot(self) -> QtWidgets.QWidget:
        """Vmid between samples: ln of the sensor's distance below V_D at the end of each cycle
        against the connected time only. Disconnected time does not decay the sensor, so the
        decay is a straight line of slope -1/tau here, with a small step at every connection."""
        self.ln_layout = pg.GraphicsLayoutWidget()
        self.ln_plot = self.ln_layout.addPlot(title=LN_TITLE_IDLE)
        self.ln_plot.setLabel("bottom", "connected time, accumulated over the cycles", "us")
        self.ln_plot.setLabel("left", "ln(d / 1 V),  d = V_D - sensor")
        self.ln_plot.addLegend()
        self.ln_data = self.ln_plot.plot(pen=None, symbol="o", symbolSize=5,
                                         symbolBrush=(70, 130, 180), name="samples")
        self.ln_model = self.ln_plot.plot(pen=pg.mkPen((220, 50, 50), width=2), name="fitted model, run freely")
        self.ln_slope = self.ln_plot.plot(
            pen=pg.mkPen((150, 150, 150), width=1.5, style=QtCore.Qt.PenStyle.DashLine),
            name="slope -1/tau from the first sample (no connection loss)")
        return self.ln_layout

    def show_ln_plot(self, m: capfit.Measurement) -> None:
        if not len(m.alt_d_v):
            for curve in (self.ln_data, self.ln_model, self.ln_slope):
                curve.setData([], [])
            self.ln_plot.setTitle(LN_TITLE_IDLE)
            return
        x = m.alt_connected_s * 1e6
        y_data = np.log(m.alt_d_v)
        y_model = np.log(m.alt_d_model_v)
        self.ln_data.setData(x, y_data, connect="finite")
        self.ln_model.setData(x, y_model, connect="finite")
        first = int(np.flatnonzero(np.isfinite(y_data))[0]) if np.isfinite(y_data).any() else None
        if first is None or not np.isfinite(m.tau):
            self.ln_slope.setData([], [])
        else:
            span = np.array([x[first], x[-1]])
            self.ln_slope.setData(span, y_data[first] - (span - x[first]) / (m.tau * 1e6))
        name = self.driver.display(m.ref) if m.ref else "ADC%d:%d" % (m.adc, m.channel)
        self.ln_plot.setTitle("%s  tau=%.2f us  (dashed: decay at 1/tau only; the dots also lose ln(keep) "
                              "per connection and level off at the settling level)" % (name, m.tau * 1e6))

    VINF_COLUMNS = (
        ("Channel", "label", "%s"), ("V_D (Vmid) [V]", "v_d", "%.4f"),
        ("Vinf alternating [V]", "v_alt", "%.4f"), ("alt - Vmid [mV]", "offset_alt_mv", "%+.1f"),
        ("Vinf held [V]", "v_held", "%.4f"), ("held - Vmid [mV]", "offset_held_mv", "%+.1f"),
        ("Vinf calculated [V]", "v_calc", "%.4f"), ("calc - Vmid [mV]", "offset_calc_mv", "%+.1f"),
        ("alt - calc [mV]", "alt_minus_calc_mv", "%+.1f"), ("held - calc [mV]", "held_minus_calc_mv", "%+.1f"),
        ("sd alt [mV]", "v_alt_sd_mv", "%.2f"), ("sd held [mV]", "v_held_sd_mv", "%.2f"),
        ("alt spread [LSB]", "alt_spread_lsb", "%.2f"), ("M", "bursts", "%d"), ("Flags", "flags", "%s"))

    def _build_vinf_page(self) -> QtWidgets.QWidget:
        """Vinf mode results: each sensor's measured and calculated settling voltage against
        Vmid (the zero line), and a table."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QtWidgets.QHBoxLayout()
        self.vinf_save_button = QtWidgets.QPushButton("Save Vinf CSV")
        self.vinf_save_button.clicked.connect(self.export_vinf_csv)
        bar.addWidget(self.vinf_save_button)
        note = QtWidgets.QLabel(
            "Alternating: median of the settled sensor samples of a long burst that alternates the sensor with "
            "Vmid, one connected time, as production does for its baseline. Held: median of the settled end of a "
            "burst that keeps the sensor connected. Calculated: the settling level of the Vmid-between-samples "
            "fit (long and short cycles). The frame sequences differ, so differences are expected.")
        note.setWordWrap(True)
        bar.addWidget(note, 1)
        layout.addLayout(bar)
        self.vinf_layout = pg.GraphicsLayoutWidget()
        self.vinf_plot = self.vinf_layout.addPlot(title="Vinf against Vmid")
        self.vinf_plot.setLabel("left", "voltage relative to Vmid (V_D)", "mV")
        self.vinf_plot.addLegend()
        self.vinf_plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen((90, 160, 90), width=2)))
        self.vinf_plot.plot([], [], pen=pg.mkPen((90, 160, 90), width=2), name="Vmid (V_D)")
        self.vinf_calc_curve = self.vinf_plot.plot(pen=None, symbol="t", symbolSize=9,
                                                   symbolBrush=(220, 50, 50), name="Vinf calculated")
        self.vinf_held_curve = self.vinf_plot.plot(pen=None, symbol="s", symbolSize=7,
                                                   symbolBrush=(150, 150, 150), name="Vinf held")
        self.vinf_alt_curve = self.vinf_plot.plot(pen=None, symbol="o", symbolSize=9,
                                                  symbolBrush=(70, 130, 180),
                                                  name="Vinf alternating (production-like)")
        self.vinf_error = pg.ErrorBarItem(pen=pg.mkPen((70, 130, 180), width=1.5))
        self.vinf_plot.addItem(self.vinf_error)
        self.vinf_table = QtWidgets.QTableWidget(0, len(self.VINF_COLUMNS))
        self.vinf_table.setHorizontalHeaderLabels([c[0] for c in self.VINF_COLUMNS])
        self.vinf_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        split.addWidget(self.vinf_layout)
        split.addWidget(self.vinf_table)
        layout.addWidget(split, 1)
        self.vinf_page = page
        self.vinf_shown: Dict[Channel, capfit.VinfReading] = {}
        return page

    def on_vinf_reading(self, reading: capfit.VinfReading) -> None:
        self.vinf_shown[tuple(reading.ref)] = reading
        self.refresh_vinf_view()
        self.bottom_tabs.setCurrentWidget(self.vinf_page)
        self.append_log("%s: Vinf from Vmid: alternating %+.1f mV, held %+.1f mV, calculated %+.1f mV "
                        "(%d burst%s)%s" % (
                            reading.label, reading.offset_alt_mv, reading.offset_held_mv, reading.offset_calc_mv,
                            reading.alt_bursts, "" if reading.alt_bursts == 1 else "s",
                            "  " + " ".join(reading.flags) if reading.flags else ""))

    def refresh_vinf_view(self) -> None:
        readings = [self.vinf_shown[c] for c in sorted(self.vinf_shown)]
        x = np.arange(len(readings), dtype=float)
        alt = np.array([r.offset_alt_mv for r in readings])
        held = np.array([r.offset_held_mv for r in readings])
        calc = np.array([r.offset_calc_mv for r in readings])
        sd = np.nan_to_num(np.array([r.v_alt_sd for r in readings]) * 1e3)
        self.vinf_alt_curve.setData(x, alt)
        self.vinf_held_curve.setData(x, held)
        self.vinf_calc_curve.setData(x, calc)
        self.vinf_error.setData(x=x, y=np.nan_to_num(alt), height=2 * sd)
        self.vinf_plot.getAxis("bottom").setTicks([[(float(i), r.label) for i, r in enumerate(readings)]])
        self.vinf_table.setRowCount(len(readings))
        for row, reading in enumerate(readings):
            values = {"label": reading.label, "v_d": reading.v_d, "v_alt": reading.v_alt,
                      "offset_alt_mv": reading.offset_alt_mv, "v_held": reading.v_held,
                      "offset_held_mv": reading.offset_held_mv, "v_calc": reading.v_calc,
                      "offset_calc_mv": reading.offset_calc_mv,
                      "alt_minus_calc_mv": reading.alt_minus_calc_mv,
                      "held_minus_calc_mv": reading.held_minus_calc_mv,
                      "v_alt_sd_mv": reading.v_alt_sd * 1e3, "v_held_sd_mv": reading.v_held_sd * 1e3,
                      "alt_spread_lsb": reading.alt_spread_lsb, "bursts": reading.alt_bursts,
                      "flags": " ".join(reading.flags)}
            colour = QtGui.QColor("#fee2e2") if not (reading.alt_bursts or reading.bursts) else (
                QtGui.QColor("#fef3c7") if reading.flags else QtGui.QColor("#dcfce7"))
            for column, (_, name, fmt) in enumerate(self.VINF_COLUMNS):
                value = values[name]
                text = "-" if isinstance(value, float) and not np.isfinite(value) else fmt % value
                item = QtWidgets.QTableWidgetItem(text)
                item.setBackground(colour)
                item.setForeground(QtGui.QColor("#111827"))
                self.vinf_table.setItem(row, column, item)

    def measure_vinf_selected(self) -> bool:
        channels = self.selected_channels()
        if not channels:
            self.append_log("No channel selected.")
            return False
        if not self.driver.supports_vinf:
            self.append_log("%s has no Vinf mode." % self.driver.name)
            return False
        m = int(self.manual_bursts.value())
        self.vinf_shown.clear()
        self.refresh_vinf_view()
        return self.start_job(lambda s: s.run_vinf(channels, m))

    def export_vinf_csv(self) -> None:
        readings = [self.vinf_shown[c] for c in sorted(self.vinf_shown)]
        if not readings:
            self.append_log("No Vinf readings to save.")
            return
        path = self._ask_save("Export Vinf CSV", "captest_vinf.csv", "CSV (*.csv)")
        if path:
            runner.write_vinf_csv(readings, self.driver, path)

    def _build_results_page(self) -> QtWidgets.QWidget:
        """Toolbar (fit model, re-analyse) above the results table."""
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QtWidgets.QHBoxLayout()
        self.fit_combo = QtWidgets.QComboBox()
        self.fit_combo.addItem("Two exponentials (one when not needed)", capfit.FIT_TWO)
        self.fit_combo.addItem("One exponential", capfit.FIT_ONE)
        self.fit_combo.setToolTip("Switch the fit model for all results below. The recorded samples are "
                                  "analysed again immediately; no measurement is repeated. " + TOOLTIPS["fit"])
        self.fit_combo.currentIndexChanged.connect(lambda _: self.on_fit_model_changed(self.fit_combo))
        self.reanalyze_button = QtWidgets.QPushButton("Re-analyse with current settings")
        self.reanalyze_button.setToolTip("Recompute every result from its recorded samples with the current "
                                         "Settings tab values (fit model, R_bias, VREF, V_s0, discard K, ...).")
        self.reanalyze_button.clicked.connect(self.reanalyze_results)
        bar.addWidget(QtWidgets.QLabel("Fit model:"))
        bar.addWidget(self.fit_combo)
        bar.addWidget(self.reanalyze_button)
        bar.addStretch(1)
        layout.addLayout(bar)
        self.compare_label = QtWidgets.QLabel("")
        self.compare_label.setWordWrap(True)
        layout.addWidget(self.compare_label)
        layout.addWidget(self.table)
        return page

    def _build_table(self) -> QtWidgets.QTableWidget:
        table = QtWidgets.QTableWidget(0, 0)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self.table_menu)
        table.horizontalHeader().setStretchLastSection(True)
        table.itemSelectionChanged.connect(self.show_selected_result)
        return table

    # ---------------------------------------------------------------- settings
    def apply_settings(self, s: Settings) -> None:
        self.settings = s
        for name in self._field_names:
            spec = FIELD_SPECS[name]
            widget = getattr(self, spec.attr)
            value = getattr(s, name)
            if spec.kind == "samples":
                widget.setValue(value or 0)
            elif spec.kind == "percent":
                widget.setValue(value * 100.0)
            elif spec.kind == "bool":
                widget.blockSignals(True)
                widget.setChecked(bool(value))
                widget.blockSignals(False)
            elif spec.kind in ("fit", "method"):
                widget.blockSignals(True)
                widget.setCurrentIndex(max(widget.findData(value), 0))
                widget.blockSignals(False)
            else:
                widget.setValue(value)
        self.s_subtract_baseline.blockSignals(True)
        self.s_subtract_baseline.setChecked(bool(s.subtract_baseline))
        self.s_subtract_baseline.blockSignals(False)
        self.refresh_baseline_label()
        self.auto_bursts.setValue(s.bursts)
        self.manual_bursts.setValue(s.bursts)
        self.auto_per_address.blockSignals(True)
        self.auto_per_address.setChecked(bool(s.per_address))
        self.auto_per_address.blockSignals(False)
        self.fit_combo.blockSignals(True)
        self.fit_combo.setCurrentIndex(max(self.fit_combo.findData(s.fit_model), 0))
        self.fit_combo.blockSignals(False)
        self.bias_table.setRowCount(0)
        for channel in self.driver.channels():
            row = self.bias_table.rowCount()
            self.bias_table.insertRow(row)
            item = QtWidgets.QTableWidgetItem(self.driver.key(channel))
            item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
            self.bias_table.setItem(row, 0, item)
            self.bias_table.setItem(row, 1, QtWidgets.QTableWidgetItem("%g" % s.r_bias(*channel)))

    def collect_settings(self) -> Settings:
        s = self.settings
        for name in self._field_names:
            spec = FIELD_SPECS[name]
            widget = getattr(self, spec.attr)
            if spec.kind == "samples":
                s.n_samples = int(widget.value()) or None
            elif spec.kind == "percent":
                s.bias_tolerance = widget.value() / 100.0
            elif spec.kind == "bool":
                setattr(s, name, bool(widget.isChecked()))
            elif spec.kind == "fit":
                s.fit_model = widget.currentData() or capfit.FIT_TWO
            elif spec.kind == "method":
                s.method = widget.currentData() or "burst"
            elif spec.kind in ("int", "auto_int"):
                setattr(s, name, int(widget.value()))
            else:
                setattr(s, name, widget.value())
        s.subtract_baseline = bool(self.s_subtract_baseline.isChecked())
        for row in range(self.bias_table.rowCount()):
            try:
                s.bias[self.bias_table.item(row, 0).text()] = float(self.bias_table.item(row, 1).text())
            except (ValueError, AttributeError):
                pass
        return s

    def save_settings(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save settings", "captest_settings.json",
                                                        "JSON (*.json)")
        if path:
            self.collect_settings().save(path)

    def load_settings(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load settings", "", "JSON (*.json)")
        if not path:
            return
        loaded = Settings.load(path)
        if loaded.board_id != self.driver.id:
            if not self.select_board(loaded.board_id, ask=True):
                self.append_log("Settings file is for %s, not for %s." % (loaded.board_id, self.driver.id))
                return
        self.apply_settings(loaded)

    def on_per_address_toggled(self, checked: bool) -> None:
        self.settings.per_address = bool(checked)
        if hasattr(self, "s_per_address"):
            self.s_per_address.blockSignals(True)
            self.s_per_address.setChecked(bool(checked))
            self.s_per_address.blockSignals(False)

    def on_per_address_setting_toggled(self, checked: bool) -> None:
        self.on_per_address_toggled(checked)
        self.auto_per_address.blockSignals(True)
        self.auto_per_address.setChecked(bool(checked))
        self.auto_per_address.blockSignals(False)

    # ------------------------------------------------------------ board selection
    def on_board_selected(self, index: int) -> None:
        board_id = self.board_combo.itemData(index)
        if board_id and board_id != self.driver.id:
            if not self.select_board(board_id):
                self.board_combo.blockSignals(True)
                self.board_combo.setCurrentIndex(max(self.board_combo.findData(self.driver.id), 0))
                self.board_combo.blockSignals(False)

    def select_board(self, board_id: str, ask: bool = False) -> bool:
        """Switch to another board: disconnect, keep the old board's settings, rebuild."""
        entry = self.registry.get(board_id)
        if entry is None or not entry.enabled:
            self.append_log("Board %s is not available%s." % (
                board_id, ": " + entry.error if entry is not None else ""))
            return False
        if entry.id == self.entry.id:
            return True
        if self.force_panel is not None and self.force_panel.is_busy():
            self.append_log("Finish the force capture or saved-data analysis before changing the board.")
            return False
        if self.worker is not None and self.worker.isRunning():
            self.append_log("Stop the running measurement before changing the board.")
            return False
        if self.script_worker is not None and self.script_worker.isRunning():
            self.append_log("Wait for the firmware upload to finish before changing the board.")
            return False
        if ask and not self._confirm("Switch to %s?" % entry.name, "Change board"):
            return False
        self.save_board_state()
        if self.session is not None:
            self.disconnect_device()
        self.entry = entry
        self.driver = entry.driver
        saved = settings_module.load_board_settings(entry.id) if self.use_saved else None
        self.settings = saved or (Settings() if entry.id == DEFAULT_BOARD_ID else Settings.for_board(entry.id))
        self.session = None
        self.populate_board()
        self.set_connected(False)
        self.append_log("Board: %s" % entry.name)
        if self.use_saved:
            settings_module.save_last_board(entry.id)
        return True

    def save_board_state(self) -> None:
        """Remember the settings of the current board and the last used board."""
        if not self.use_saved:
            return
        try:
            self.collect_settings()
            settings_module.save_board_settings(self.settings)
            settings_module.save_last_board(self.driver.id)
        except OSError as exc:
            self.append_log("Could not save settings: %s" % exc)

    def detect_board(self) -> None:
        """Identify the attached firmware (connecting first when needed)."""
        if self.session is None:
            self.connect_device()
        else:
            self.identify()

    # -------------------------------------------------------------- connection
    def refresh_ports(self) -> None:
        current = self.port_combo.currentText()
        self.port_combo.clear()
        for port in list_serial_ports():
            label = port["device"] + ("  (Teensy)" if port["teensy"] else "")
            self.port_combo.addItem(label, port["device"])
        if current:
            self.port_combo.setEditText(current)

    def selected_port(self) -> str:
        data = self.port_combo.currentData()
        text = self.port_combo.currentText().split()[0] if self.port_combo.currentText() else ""
        return data if data and self.port_combo.currentText().startswith(str(data)) else text

    def toggle_connection(self) -> None:
        if self.session is not None:
            self.disconnect_device()
        else:
            self.connect_device()

    def connect_device(self) -> None:
        self.collect_settings()
        try:
            if self.sim_check.isChecked():
                clock = VirtualClock() if self.sim_virtual_clock else RealClock()
                self.sim_device = SimulatedDevice(SimConfig(board=self.driver.id), clock=clock,
                                                  r_bias_table=self.settings.bias)
                client = CapTestClient(SimLink(self.sim_device))
                self.port_in_use = None
            else:
                port = self.selected_port()
                if not port:
                    self.append_log("Select a serial port first.")
                    return
                clock = RealClock()
                client = CapTestClient(SerialLink(port))
                self.sim_device = None
                self.port_in_use = port
        except LinkError as exc:
            self.append_log("Cannot open port: %s" % exc)
            return
        self.session = runner.MeasurementSession(client, self.settings, clock,
                                                 simulated=self.sim_device is not None,
                                                 driver=self.driver)
        self.identify()

    def identify(self) -> None:
        if self.session is None:
            return
        self.firmware = self.session.client.identify()
        self.firmware_label.setText(self.firmware.label)
        self.append_log("Firmware: " + self.firmware.label)
        fw = self.firmware
        if fw.board_id and fw.board_id != self.driver.id and self.sim_device is None:
            other = self.registry.get(fw.board_id)
            what = {"captest": "capacitance-test", "production": "production",
                    "diagnostic": "diagnostic"}.get(fw.kind, fw.kind)
            self.append_log("The attached %s firmware belongs to %s, not to %s."
                            % (what, fw.board_id, self.driver.id))
            if other is not None and other.enabled and self._confirm(
                    "The attached firmware is the %s firmware of %s.\n\nSelect that board?"
                    % (what, other.name), "Board detected"):
                port, simulated = self.port_in_use, self.sim_device is not None
                self.disconnect_device()
                if self.select_board(other.id):
                    self.port_in_use = port
                    if port:
                        self.port_combo.setEditText(port)
                    self.connect_device()
                return
        if fw.kind == "captest" and fw.board_id in ("", self.driver.id):
            try:
                self.session.refresh_boot()
                if self.driver.setup_device(self.session.client, self.settings) is False:
                    self.append_log("Board rejected the SPI clock setting.")
            except Exception as exc:
                self.append_log("capstatus failed: %s" % exc)
        elif fw.kind == "production" and fw.board_id in ("", self.driver.id):
            self.append_log("Production firmware is running: use 'Flash test firmware' to measure.")
        self.set_connected(True)
        if (fw.kind == "production" and fw.board_id in ("", self.driver.id) and self.sim_device is None
                and self.offer_flash_on_production):
            # Offer the switch right away (the usual reason the buttons are disabled).
            QtCore.QTimer.singleShot(0, lambda: self.flash("test"))

    def firmware_is_ready(self) -> bool:
        fw = self.firmware
        return bool(fw is not None and fw.kind == "captest" and fw.board_id in ("", self.driver.id))

    def disconnect_device(self) -> None:
        if self.force_panel is not None:
            self.force_panel.stop_capture()          # nothing keeps streaming after a disconnect
        if self.worker is not None:
            self.worker.request_stop()
            self.worker.wait(5000)
        if self.session is not None:
            self.session.park()
            self.session.client.link.close()
        self.session = None
        self.sim_device = None
        self.firmware = None
        self.firmware_label.setText("not connected")
        self.set_connected(False)

    def set_connected(self, connected: bool) -> None:
        captest = connected and self.firmware_is_ready()
        busy = self.worker is not None and self.worker.isRunning()
        force_busy = self.force_panel is not None and self.force_panel.is_busy()
        script_busy = self.script_worker is not None and self.script_worker.isRunning()
        self.connect_button.setText("Disconnect" if connected else "Connect")
        self.sim_check.setEnabled(not connected)
        self.port_combo.setEnabled(not connected)
        self.board_combo.setEnabled(not busy and not force_busy and not script_busy)
        self.detect_button.setEnabled(not busy and not force_busy and not script_busy and not self.sim_check.isChecked())
        for button in (self.run_auto_button, self.measure_selected_button, self.discharge_button,
                       self.vmid_button, self.remeasure_button):
            button.setEnabled(captest and not busy and not force_busy)
        self.vinf_button.setEnabled(captest and not busy and not force_busy and self.driver.supports_vinf)
        self.stop_button.setEnabled(busy)
        has_results = self.session is not None and bool(self.session.results)
        self.fit_combo.setEnabled(not busy)
        self.reanalyze_button.setEnabled(has_results and not busy)
        simulated = self.sim_check.isChecked()
        flash_ok = not simulated and not busy and not force_busy and not script_busy
        self.flash_test_button.setEnabled(flash_ok)
        self.restore_button.setEnabled(flash_ok)
        self.flash_test_button.setToolTip("Disabled in simulation mode." if simulated else "")
        self.restore_button.setToolTip("Disabled in simulation mode." if simulated else "")
        if self.force_panel is not None:
            self.force_panel.refresh_state()
        self.update_reason(connected, captest, busy, script_busy)

    def update_reason(self, connected: bool, captest: bool, busy: bool, script_busy: bool) -> None:
        """Say in words why the measure buttons are (not) usable."""
        warn = "background: #7c2d12; color: white;"
        ok = "background: #14532d; color: white;"
        fw = self.firmware
        if script_busy:
            text, style = "Uploading firmware ... wait for the script to finish (see the Log tab).", warn
        elif not connected:
            text, style = ("Not connected. Choose the board and the Teensy port (or tick Simulation "
                           "mode) and press Connect."), warn
        elif fw is not None and fw.board_id and fw.board_id != self.driver.id:
            text = ("The attached firmware belongs to %s, but %s is selected. Select the matching "
                    "board (press Detect) or flash the %s test firmware ('Flash test firmware')."
                    % (fw.board_id, self.driver.id, self.driver.id))
            style = warn
        elif fw is None or fw.kind in ("production", "diagnostic"):
            text = ("The board runs the PRODUCTION firmware, which cannot do the capacitance test. "
                    "Press 'Flash test firmware' (top right) to load the test firmware; "
                    "'Restore production firmware' puts it back afterwards.")
            style = warn
        elif not captest:
            detail = fw.detail if fw else ""
            text = ("No answer from the capacitance-test firmware (%s). Check the port, "
                    "disconnect and connect again, or press 'Flash test firmware'." % (detail or "no reply"))
            style = warn
        elif busy:
            text, style = "A measurement is running. Press Stop to end it.", ok
        else:
            text, style = "Ready: use 'Run automatic' (Automatic tab) or 'Measure selected' (Manual tab).", ok
        self.reason_label.setText(text)
        self.reason_label.setStyleSheet(style)
        for button in (self.run_auto_button, self.measure_selected_button, self.discharge_button,
                       self.vmid_button, self.remeasure_button):
            button.setToolTip("" if button.isEnabled() else text)

    # -------------------------------------------------------------------- jobs
    def start_job(self, job: Callable) -> bool:
        if self.session is None or (self.worker is not None and self.worker.isRunning()):
            return False
        if self.force_panel is not None and self.force_panel.controller is not None and self.force_panel.controller.session is not None:
            self.append_log("Finish or cancel the force trial before running another board measurement.")
            return False
        if not self.firmware_is_ready():
            self.append_log("The capacitance-test firmware of %s is not running." % self.driver.id)
            return False
        self.collect_settings()
        self.session.settings = self.settings
        self.warnings.clear()
        self.warning_label.setText("")
        worker = Worker(self.session, job, self)
        worker.log.connect(self.append_log)
        worker.warning.connect(self.on_warning)
        worker.progress.connect(self.on_progress)
        worker.burst.connect(self.on_burst)
        worker.result.connect(self.on_result)
        worker.vinf.connect(self.on_vinf_reading)
        worker.finished_job.connect(self.on_job_finished)
        self.worker = worker
        worker.start()
        self.set_connected(True)       # after start(): the buttons must see a running job
        return True

    def run_auto(self) -> bool:
        groups = [a for a, c in self.auto_adc_checks.items() if c.isChecked()]
        if not groups:
            self.append_log("Select at least one %s." % self.driver.descriptor["channels"]["group_noun"])
            return False
        self.settings.bursts = int(self.auto_bursts.value())
        if hasattr(self, "s_bursts"):
            self.s_bursts.setValue(self.settings.bursts)
        per_address = self.auto_per_address.isChecked()
        self.settings.per_address = per_address
        return self.start_job(lambda s: s.run_auto(groups, self.auto_bursts.value(), per_address))

    def selected_channels(self) -> List[Channel]:
        return [c for c, b in self.channel_boxes.items() if b.isChecked()]

    def measure_selected(self) -> bool:
        channels = self.selected_channels()
        if not channels:
            self.append_log("No channel selected.")
            return False
        m = int(self.manual_bursts.value())
        return self.start_job(lambda s: s.run_manual(channels, m))

    def measure_one(self, channel: Channel) -> bool:
        m = int(self.manual_bursts.value())
        return self.start_job(lambda s: s.run_manual([channel], m, False))

    def remeasure_failed(self) -> bool:
        return self.start_job(lambda s: s.remeasure_failed())

    def run_discharge_check(self) -> bool:
        try:
            channel = self.driver.parse_spec(self.discharge_edit.text())[0]
        except (ValueError, IndexError) as exc:
            self.append_log("Bad channel: %s" % exc)
            return False
        return self.start_job(lambda s: s.discharge_check(channel))

    def read_vmid(self) -> bool:
        target = self.vmid_adc.currentData()
        return self.start_job(lambda s: s.vmid_read(target))

    def stop_job(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.request_stop()
            self.append_log("Stopping after the current burst; the board will be parked on Vmid.")

    def on_job_finished(self, value, error: str) -> None:
        if error:
            self.append_log("ERROR: " + error)
        elif isinstance(value, dict) and "points" in value:
            self.show_discharge_check(value)
        elif isinstance(value, dict) and "mean_code" in value:
            self.append_log(self.driver.vmid_text(value, self.settings.vref))
        if self.worker is not None:
            self.worker.wait()
            self.worker.deleteLater()
        self.worker = None
        self.progress_label.setText("idle" if not error else "error")
        self.set_connected(self.session is not None)
        self.update_channel_status()

    def wait_idle(self, timeout_s: float = 60.0) -> bool:
        """Process events until the running job (if any) finished (used by tests)."""
        deadline = time.monotonic() + timeout_s
        app = QtWidgets.QApplication.instance()
        while self.worker is not None and time.monotonic() < deadline:
            app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)
            time.sleep(0.005)
        app.processEvents()
        return self.worker is None

    # ----------------------------------------------------------------- results
    def append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def on_warning(self, text: str) -> None:
        self.warnings.append(text)
        self.warning_label.setText(text)

    def on_progress(self, p: runner.Progress) -> None:
        self.progress_bar.setMaximum(max(p.total, 1))
        self.progress_bar.setValue(p.done)
        remaining = "?" if not np.isfinite(p.remaining_s) else "%.0f s" % p.remaining_s
        self.progress_label.setText("%d/%d  elapsed %.0f s  remaining %s  %s"
                                    % (p.done, p.total, p.elapsed_s, remaining, p.message))

    def on_burst(self, m: capfit.Measurement) -> None:
        self.show_measurement(m)

    def show_measurement(self, m: capfit.Measurement) -> None:
        fast = "" if not np.isfinite(m.tau_fast) else " (fast %.1f us)" % (m.tau_fast * 1e6)
        name = self.driver.display(m.ref) if m.ref else "ADC%d:%d" % (m.adc, m.channel)
        share = m.c_total_method == capfit.C_TOTAL_VMID_CYCLES
        self.burst_plot.setTitle("%s  %s  tau=%.2f us%s  C_total=%.2f pF  %s=%.3f  %s" % (
            name, m.fit_model_used, m.tau * 1e6, fast, m.c_total * 1e12,
            "C_share/C_total" if share else "ratio", m.c_share_ratio if share else m.ratio,
            " ".join(m.flags)))
        self.burst_plot.setLabel("bottom", "time since connection", "s")
        self.connect_line.setVisible(True)
        if len(m.t):
            self.data_curve.setData(m.t, m.v)
            self.fit_curve.setData(m.t, m.fitted if len(m.fitted) else m.v * np.nan)
            if len(m.fitted_other):
                self.other_curve.setData(m.t, m.fitted_other, name=m.other_label)
            else:
                self.other_curve.setData([], [])
            self.resid_curve.setData(m.t[:len(m.residuals_lsb)], m.residuals_lsb)
        self.pre_curve.setData(m.pre_t, m.pre_v)
        self.show_ln_plot(m)

    def on_result(self, result: runner.ChannelResult) -> None:
        self._put_row(result)
        self.update_bar_chart()
        self.update_channel_status()
        self.update_compare_label()

    def _put_row(self, result: runner.ChannelResult) -> None:
        # Keep physical channel order even when shared-address bursts arrive
        # MUX-interleaved, or a manually measured channel arrives out of order.
        with QtCore.QSignalBlocker(self.table):
            self._put_sorted_row(result)

    def _put_sorted_row(self, result: runner.ChannelResult) -> None:
        channel = result.ref
        row_index = self.table_rows.get(channel)
        if row_index is None:
            row_index = sum(ref < channel for ref in self.table_rows)
            for ref, index in self.table_rows.items():
                if index >= row_index:
                    self.table_rows[ref] = index + 1
            self.table.insertRow(row_index)
            self.table_rows[channel] = row_index
        row = result.row(self.settings.bias_tolerance)
        colour = QtGui.QColor("#fee2e2") if result.failed else (
            QtGui.QColor("#fef3c7") if result.flags else QtGui.QColor("#dcfce7"))
        for column, (_, name, fmt) in enumerate(self.columns):
            value = row[name]
            if value is None or (isinstance(value, float) and not np.isfinite(value)):
                text = "-"
            elif name == "fresh":
                text = "yes" if value else "no"
            else:
                text = fmt % value
            item = QtWidgets.QTableWidgetItem(text)
            item.setBackground(colour)
            item.setForeground(QtGui.QColor("#111827"))   # dark text: readable on the pastel row colours in any theme
            item.setData(QtCore.Qt.ItemDataRole.UserRole, channel)
            if name == "c_net_pf" and result.net_note:
                item.setToolTip(result.net_note)
            self.table.setItem(row_index, column, item)

    # --------------------------------------------------- re-analysis of recorded data
    def on_fit_model_changed(self, source: QtWidgets.QComboBox) -> None:
        """Either fit-model combo changed: mirror it, then re-analyse the stored samples."""
        model = source.currentData() or capfit.FIT_TWO
        for combo in (self.s_fit, self.fit_combo):
            if combo is not source:
                combo.blockSignals(True)
                combo.setCurrentIndex(max(combo.findData(model), 0))
                combo.blockSignals(False)
        self.settings.fit_model = model
        if self.session is not None and self.session.results:
            if self.worker is not None and self.worker.isRunning():
                self.append_log("Fit model changed while measuring: press 'Re-analyse' when it has finished.")
            else:
                self.reanalyze_results()

    def reanalyze_results(self) -> bool:
        """Recompute all results from the recorded samples with the current settings."""
        if self.session is None or not self.session.results:
            return False
        if self.worker is not None and self.worker.isRunning():
            self.append_log("Wait for the running measurement before re-analysing.")
            return False
        settings = self.collect_settings()
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            started = time.monotonic()
            redone = self.session.reanalyze(settings)
            self.rebuild_results_view()
            self.append_log("Re-analysed %d channels from the recorded samples (%s, %.1f s); no "
                            "measurement was repeated." % (len(redone), settings.fit_model,
                                                           time.monotonic() - started))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        return True

    def rebuild_results_view(self) -> None:
        """Redraw table, bar chart, plot and summary from session.results."""
        selected = None
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if rows:
            item = self.table.item(rows[0].row(), 0)
            selected = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table_rows.clear()
        for channel in sorted(self.session.results):
            self._put_row(self.session.results[channel])
        self.table.blockSignals(False)
        self.update_bar_chart()
        self.update_channel_status()
        self.update_compare_label()
        shown = self.session.results.get(selected) if selected is not None else None
        if shown is None and self.session.latest is not None:
            shown = self.session.results.get(tuple(self.session.latest.ref))
        if shown is not None and shown.measurements:
            if selected in self.table_rows:
                self.table.selectRow(self.table_rows[selected])
            self.show_measurement(shown.measurements[-1])

    def update_compare_label(self) -> None:
        """One line comparing the two models over all results."""
        results = list(self.session.results.values()) if self.session else []
        good = [r for r in results if not r.failed]
        if not good:
            self.compare_label.setText("")
            return

        def median(name):
            values = [r.aggregate.mean[name] for r in good if np.isfinite(r.aggregate.mean[name])]
            return float(np.median(values)) if values else float("nan")

        two = sum(1 for r in good if r.fit_used == capfit.FIT_TWO)
        c1, c2 = median("c_total_1exp") * 1e12, median("c_total_2exp") * 1e12
        shift = (c2 / c1 - 1.0) * 100.0 if np.isfinite(c1) and np.isfinite(c2) and c1 else float("nan")
        self.compare_label.setText(
            "Showing: %s (two exponentials used on %d of %d channels).  Median C_total: single %.1f pF, "
            "two %.1f pF (%+.0f %%).  Median residual: single %.2f LSB, two %.2f LSB."
            % ("two exponentials" if self.settings.fit_model == capfit.FIT_TWO else "one exponential",
               two, len(good), c1, c2, shift, median("rms_1exp"), median("rms_2exp")))

    def update_bar_chart(self) -> None:
        if self.session is None:
            return
        results = [self.session.results[c] for c in sorted(self.session.results)]
        self.bar_total.clear()
        self.bar_ratio.clear()
        if not results:
            return
        # Channels are grouped (7953: by ADC, ADC124: by array and mux, since C_D belongs to
        # the mux-output node): a gap and a label separate the groups.
        x, position, previous = [], 0.0, None
        for r in results:
            if previous is not None and r.chart_group != previous:
                position += 1.0
            x.append(position)
            position += 1.0
            previous = r.chart_group
        x = np.array(x)
        c_total = np.array([r.aggregate.mean["c_total"] * 1e12 for r in results])
        # With a board baseline in use the first chart shows C_net: C_total minus the baseline.
        net = np.array([r.c_net_pf for r in results])
        if self.settings.subtract_baseline and np.isfinite(net).any():
            c_total = net
            self.bar_total.setTitle("C_net per channel (C_total minus board baseline)")
            self.bar_total.setLabel("left", "C_net", "pF")
        else:
            self.bar_total.setTitle("C_total per channel")
            self.bar_total.setLabel("left", "C_total", "pF")
        # Vmid between samples gives C_share, an effective connection capacitance, in place of
        # the node capacitance C_D: the second chart shows whichever the row has, and says so.
        shares = [r.c_total_method == capfit.C_TOTAL_VMID_CYCLES for r in results]
        ratio = np.array([r.aggregate.mean["c_share_ratio" if share else "ratio"]
                          for r, share in zip(results, shares)])
        self.bar_ratio.setTitle(RATIO_TITLE_SHARE if all(shares)
                                else RATIO_TITLE_BOTH if any(shares) else RATIO_TITLE_STEP)
        err = np.array([r.aggregate.std["c_total"] * 1e12 for r in results])
        brushes = [pg.mkBrush("#dc2626" if r.failed else "#d97706" if r.flags else "#16a34a")
                   for r in results]
        shown_total, cut_total, _ = clip_outliers(c_total)
        shown_ratio, cut_ratio, _ = clip_outliers(ratio)
        self.bar_total.addItem(pg.BarGraphItem(x=x, height=shown_total, width=0.8, brushes=brushes))
        self.bar_total.addItem(pg.ErrorBarItem(x=x[~cut_total], y=shown_total[~cut_total],
                                               height=2 * np.nan_to_num(err[~cut_total])))
        self.bar_ratio.addItem(pg.BarGraphItem(x=x, height=shown_ratio, width=0.8, brushes=brushes))
        # A cut-off bar gets a red hatch and dashed outline, and its true value on top.
        for plot, shown, cut, values, text in ((self.bar_total, shown_total, cut_total, c_total, "%.0f pF"),
                                               (self.bar_ratio, shown_ratio, cut_ratio, ratio, "%.3g")):
            if not cut.any():
                continue
            plot.addItem(pg.BarGraphItem(
                x=x[cut], height=shown[cut], width=0.8,
                brush=pg.mkBrush(color=(220, 30, 30), style=QtCore.Qt.BrushStyle.BDiagPattern),
                pen=pg.mkPen((220, 30, 30), width=2, style=QtCore.Qt.PenStyle.DashLine)))
            plot.setYRange(0.0, float(shown.max()) * 1.15, padding=0)     # room for the value label
            for xi, height, value in zip(x[cut], shown[cut], values[cut]):
                label = pg.TextItem("↑ " + text % value, anchor=(0.5, 1.0), color=(200, 30, 30))
                label.setPos(float(xi), float(height))
                plot.addItem(label)
        ticks = [(float(xi), self.driver.key(r.ref)) for xi, r in zip(x, results)]
        for plot in (self.bar_total, self.bar_ratio):
            plot.getAxis("bottom").setTicks([ticks])
        for group in dict.fromkeys(r.chart_group for r in results):
            members = [xi for xi, r in zip(x, results) if r.chart_group == group]
            label = pg.TextItem(group, anchor=(0.5, 1.0), color=(110, 110, 110))
            label.setPos(float(np.mean(members)), float(np.nanmax(shown_total)) or 1.0)
            self.bar_total.addItem(label)

    def show_selected_result(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows or self.session is None:
            return
        item = self.table.item(rows[0].row(), 0)
        channel = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
        result = self.session.results.get(channel)
        if result and result.measurements:
            self.show_measurement(result.measurements[-1])

    def show_discharge_check(self, verdict: dict) -> None:
        waits = [p["wait_ms"] for p in verdict["points"]]
        v0 = [p["v0"] for p in verdict["points"]]
        self.burst_plot.setTitle("Discharge check %s: V0 versus wait (plateau: %s)"
                                 % (verdict["channel"], "yes" if verdict["plateau"] else "NO"))
        self.burst_plot.setLabel("bottom", "wait", "ms")
        self.connect_line.setVisible(False)
        self.data_curve.setData(waits, v0)
        self.fit_curve.setData([], [])
        self.other_curve.setData([], [])
        self.resid_curve.setData([], [])
        self.pre_curve.setData([], [])
        outcome = ("plateau reached, sufficient wait = %s ms" % verdict["sufficient_wait_ms"]
                   if verdict["plateau"] else
                   "NO plateau: V0 still changes after the longest wait, so the sensor does not "
                   "return to a fixed start voltage")
        self.append_log("Discharge check %s: V0 = %s V for waits %s ms; %s"
                        % (verdict["channel"], ", ".join("%.4f" % v for v in v0), waits, outcome))

    # ----------------------------------------------------------- manual helpers
    def select_all(self, value: bool, groups=None) -> None:
        for channel, box in self.channel_boxes.items():
            if groups is None or self.driver.group_of(channel) in groups:
                box.setChecked(value)

    def invert_selection(self, groups=None) -> None:
        for channel, box in self.channel_boxes.items():
            if groups is None or self.driver.group_of(channel) in groups:
                box.setChecked(not box.isChecked())

    def apply_spec(self) -> bool:
        try:
            channels = self.driver.parse_spec(self.spec_edit.text())
        except ValueError as exc:
            self.append_log("Bad channel list: %s" % exc)
            return False
        self.select_all(False)
        for channel in channels:
            self.channel_boxes[channel].setChecked(True)
        return True

    def channel_menu(self, channel: Channel, position) -> None:
        menu = QtWidgets.QMenu(self)
        action = menu.addAction("Measure this channel now (%s)" % self.driver.display(channel))
        action.triggered.connect(lambda: self.measure_one(channel))
        menu.exec(position)

    def table_menu(self, position) -> None:
        item = self.table.itemAt(position)
        if item is None:
            return
        channel = item.data(QtCore.Qt.ItemDataRole.UserRole)
        menu = QtWidgets.QMenu(self)
        action = menu.addAction("Measure this channel now (%s)" % self.driver.display(channel))
        action.triggered.connect(lambda: self.measure_one(channel))
        menu.exec(self.table.viewport().mapToGlobal(position))

    def update_channel_status(self) -> None:
        discharge_s = self.settings.discharge_ms / 1000.0
        for channel, box in self.channel_boxes.items():
            since = self.session.since_last_s(channel) if self.session else None
            fresh = self.session is None or self.session.is_fresh(channel)
            box.setText("%s%s" % (self.driver.cell_label(channel), "*" if fresh else ""))
            name = self.driver.display(channel)
            if fresh is None:
                box.setToolTip("%s: no record of earlier connections on this boot" % name)
                box.setStyleSheet("")
            elif since is None and fresh:
                box.setToolTip("%s: not connected since power-up (fresh)" % name)
                box.setStyleSheet("")
            elif since is None:
                box.setToolTip("%s: connected in an earlier session on this boot, time not known" % name)
                box.setStyleSheet("")
            else:
                soon = since < discharge_s
                box.setToolTip("%s: last connected %.1f s ago%s" % (
                    name, since,
                    " - less than the discharge time, the sensor may not be discharged" if soon else ""))
                box.setStyleSheet("color: #b45309;" if soon else "")

    # ------------------------------------------------------------------ export
    def _ask_save(self, title: str, name: str, filt: str) -> Optional[str]:
        from pathlib import Path

        start = settings_module.results_dir(self.driver.id) / name
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, title, str(start), filt)
        return path or None

    def export_csv(self) -> None:
        path = self._ask_save("Export CSV", "captest_results.csv", "CSV (*.csv)")
        if path and self.session:
            self.session.save_csv(path)

    def export_json(self) -> None:
        path = self._ask_save("Export JSON", "captest_results.json", "JSON (*.json)")
        if path and self.session:
            self.session.save_results(path)

    def export_force_data(self) -> bool:
        """Add the packages measured so far to the force-calculation input file (partial files
        are allowed and updated later). See the Force Calibration tab."""
        if self.force_panel is None:
            return False
        self.tabs.setCurrentWidget(self.force_panel)
        return self.force_panel.add_results_to_file()

    def save_raw(self) -> None:
        path = self._ask_save("Save raw bursts", "captest_raw.json", "JSON (*.json)")
        if path and self.session:
            self.session.save_raw(path)

    def load_raw(self) -> None:
        start = str(settings_module.results_dir(self.driver.id))
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load raw bursts", start, "JSON (*.json)")
        if path:
            self.load_raw_file(path)

    def load_raw_file(self, path: str) -> bool:
        """Re-analyse a saved raw file; selects the board the file was recorded on."""
        try:
            saved, _ = runner.load_raw(path)
        except (OSError, ValueError, KeyError) as exc:
            self.append_log("Cannot read %s: %s" % (path, exc))
            return False
        if saved.board_id != self.driver.id:
            if not self.select_board(saved.board_id, ask=True):
                self.append_log("%s was recorded on %s; select that board to re-analyse it."
                                % (path, saved.board_id))
                return False
        self.collect_settings()
        for result in runner.reanalyze(path, self.settings):
            self.load_offline_result(result)
        self.append_log("Re-analysed %s" % path)
        return True

    def load_offline_result(self, result: runner.ChannelResult) -> None:
        if self.session is None:
            clock = VirtualClock()
            device = self.driver.create_simulator(clock, self.settings.bias,
                                                  self.driver.sim_config(kind="silent"))
            self.session = runner.MeasurementSession(CapTestClient(SimLink(device)), self.settings,
                                                     clock, simulated=True, driver=self.driver)
        self.session.results[result.ref] = result
        self.on_result(result)
        self.reanalyze_button.setEnabled(True)
        if result.measurements:
            self.show_measurement(result.measurements[-1])

    def save_plots(self) -> None:
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, "Folder for plot images")
        if not directory:
            return
        self.export_plot_images(directory)

    def export_plot_images(self, directory: str) -> List[str]:
        from pathlib import Path
        from pyqtgraph.exporters import ImageExporter

        paths = []
        for name, plot in (("burst_fit", self.burst_plot), ("residuals", self.resid_plot),
                           ("ln_d", self.ln_plot), ("c_total", self.bar_total), ("ratio", self.bar_ratio)):
            path = str(Path(directory) / (name + ".png"))
            ImageExporter(plot).export(path)
            paths.append(path)
        return paths

    # ---------------------------------------------------------- firmware switch
    def _confirm(self, text: str, title: str = "Replace firmware") -> bool:
        return QtWidgets.QMessageBox.question(
            self, title, text,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No) == QtWidgets.QMessageBox.StandardButton.Yes

    def flash(self, kind: str, dry_run: bool = False) -> bool:
        if self.sim_check.isChecked():
            return False
        port = self.port_in_use or self.selected_port() or None
        if not self._confirm(flasher.describe(kind, port, self.entry)):
            return False
        if self.worker is not None and self.worker.isRunning():
            self.append_log("Stop the running measurement first.")
            return False
        # The serial port must be free for the uploader.
        self.disconnect_device()
        self.script_worker = ScriptWorker(kind, port, dry_run, self, board=self.entry.id)
        self.script_worker.line.connect(self.append_log)
        self.script_worker.finished_job.connect(self.on_flash_finished)
        self.set_connected(False)
        self.flash_test_button.setEnabled(False)
        self.restore_button.setEnabled(False)
        self.script_worker.start()
        return True

    def on_flash_finished(self, code: int, kind: str) -> None:
        self.append_log("Upload script finished with exit code %d" % code)
        port = None
        if code == 0 and not (self.script_worker and self.script_worker.dry_run):
            self.append_log("Waiting for the Teensy to re-enumerate ...")
            port = flasher.wait_for_teensy(30.0, self.port_in_use)
        if self.script_worker is not None:
            self.script_worker.wait()
            self.script_worker.deleteLater()
        self.script_worker = None
        if port:
            self.refresh_ports()
            self.port_combo.setEditText(port)
            self.port_in_use = port
            time.sleep(1.0)
            self.connect_device()
        else:
            self.set_connected(False)

    def closeEvent(self, event) -> None:
        if self.force_panel is not None:
            self.force_panel.shutdown()
        self.save_board_state()
        self.disconnect_device()
        super().closeEvent(event)


def run(args=None) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    settings = None
    board_id = getattr(args, "board", None) if args is not None else None
    if args is not None and getattr(args, "settings", None):
        settings = Settings.load(args.settings)
    window = MainWindow(settings, board_id=board_id, use_saved=True)
    if args is not None and getattr(args, "simulate", False):
        window.sim_check.setChecked(True)
        if not getattr(args, "sim_realtime", False):
            window.sim_virtual_clock = True
    elif args is not None and getattr(args, "port", None):
        window.port_combo.setEditText(args.port)
    window.show()
    return app.exec()
