"""Session helper of the Force Calibration tab (no widgets, so it is testable and the panel
only displays it).

It joins the pieces: the electrical manifest of the selected package, the continuous capture
(``capture.CaptureSession``), the guided wizard and the trial analysis. Device time (the time
stamps of the latest sweep received) is the only clock used for marks and windows, so a slow
GUI never moves a window. Capturing never depends on plotting: ``tick`` only reads chunks.
"""

from __future__ import annotations

import functools
import copy
import math
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import capture as capture_module
from . import force_calibration as fc
from . import force_export, force_model as fm, sensor_groups, force_zero, force_polarity
from .capture import CaptureResult, CaptureSession, ScanConfig
from .force_evidence import digest, write_json, read_json

Channel = Tuple[int, ...]

BASELINE_SKIP_S = 0.3          # first part of the baseline is settling, not used for the bias
DEFAULT_BASELINE_S = 3.0
DEFAULT_UNLOADED_S = 3.0
VIEW_INTERVAL_S = 0.25        # plot snapshots are computed at most this often
LINEAR_SETTLE_TIMEOUT_S = 120.


def _locked(method):
    """Run under the controller lock: a capture worker ticks while the GUI thread marks."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class SessionError(fc.CalibrationError):
    """A user action the session cannot take now (also covers the wizard's order errors)."""


@dataclass
class LiveView:
    """Immutable snapshot for plotting (throttled by the caller)."""

    phase: str
    prompt: str
    device_time_s: float
    t: np.ndarray                     # seconds
    q_proxy: np.ndarray               # C, accumulated group charge proxy
    force_n: np.ndarray               # relative force with the initial d33_eff
    residuals: Dict[Channel, Tuple[np.ndarray, np.ndarray]]
    headroom: Dict[Channel, dict]
    problems: List[str]
    sampling: Dict[str, dict] = field(default_factory=dict)   # measured rate per channel


class ForceCalibrationController:
    def __init__(self, forces: force_export.ForceInputs, group_map: sensor_groups.GroupMap,
                 group_id: str, client, settings, *, d33_initial: float, scan_lag_us: int = 0,
                 repeats: int = fc.DEFAULT_REPEATS, hold_s: float = fc.DEFAULT_HOLD_S,
                 masses: Sequence[float] = fc.MASSES_G, baseline_s: float = DEFAULT_BASELINE_S,
                 unloaded_s: float = DEFAULT_UNLOADED_S, mounting: str = "", board_instance: str = "",
                 results_dir=None, simulated: bool = False, g: float = fm.STANDARD_GRAVITY,
                 window_s: float = fc.DEFAULT_WINDOW_S, guard_s: float = fc.DEFAULT_GUARD_S,
                 contact_position: str = "", reference_kind: str = "known_mass",
                 limits: Optional[dict] = None, excursion_guard_v: Optional[float] = None):
        if not (math.isfinite(d33_initial) and d33_initial != 0.0):
            raise SessionError("the initial d33_eff must be finite and nonzero")
        self.forces, self.group_map, self.client = forces, group_map, client
        self.settings = copy.deepcopy(settings)
        self.group = group_map.get(group_id)
        self.d33_initial = d33_initial
        self.models = forces.route_models(group_id)          # raises unless the package is ready
        self.polarity = -1.0 if group_map.reverse_polarity else 1.0
        self.route_polarity = forces.route_polarity(group_id)
        self.scan = ScanConfig.for_routes(
            [r for g in group_map.ordered() for r in g.refs], gap_ns=settings.gap_ns,
            first_clock_delay_ns=settings.first_clock_delay_ns, lag_us=int(scan_lag_us),
            spi_clock_hz=settings.spi_clock_hz)
        self.wizard = fc.Wizard(masses, repeats, hold_s)
        self.baseline_s, self.unloaded_s = baseline_s, unloaded_s
        self.mounting, self.board_instance = mounting, board_instance
        self.simulated, self.g = simulated, g
        self.window_s, self.guard_s = window_s, guard_s
        self.contact_position = contact_position
        if reference_kind not in ("known_mass", "workflow_only"):
            raise SessionError("unknown reference kind")
        self.reference_kind = reference_kind
        self.excursion_guard_v = excursion_guard_v
        self.limits = {"max_error_n": None, "hold_drift_fraction": .15,
                       "load_removal_fraction": .25, "intercept_fraction": .1, **(limits or {})}
        self.results_dir = Path(results_dir) if results_dir else None
        self.trials: List[fc.TrialResult] = []
        self.session: Optional[CaptureSession] = None
        self.leakage: Optional[fm.LeakageFit] = None
        self.leakage_models: Dict[Channel, fm.LeakageModel] = {}
        self.fit: Optional[fm.D33Fit] = None
        self.validation: Optional[fm.Validation] = None
        self.last_result: Optional[CaptureResult] = None
        self.last_weight_result: Optional[CaptureResult] = None
        self.last_unloaded_result: Optional[CaptureResult] = None
        self.log: List[str] = []
        self._lock = threading.RLock()
        self._view: Optional[LiveView] = None
        self._view_time = 0.0
        self._trial_index = 0
        self._previous_record: Optional[dict] = None
        self._session_id = uuid.uuid4().hex
        self._electrical_hash = forces.electrical_hash()
        self._model_provenance = forces.model_provenance(group_id)
        self._applied_leakage = None
        self._correction_revision = 0
        self._evidence = {}
        self._sidecar = None
        self._fit_basis = None
        self._stale_reason = ""
        self.analysis_history = []
        self._exploratory_reasons = []
        self._trial_sidecars = []
        self._analysis_error = ""
        self.unloaded_correction = None
        self.last_unloaded = None
        self.last_polarity_check = None
        self.last_polarity_result = None
        self._polarity_progress = {}
        self._polarity_last_check_s = 0.
        self._unloaded_duration_s = None
        self.capture_purpose = "weight_trial"
        self._linear_pretest = False
        self._checking_frozen_drift = False
        self._linear_last_check_s = 0.
        self._linear_rate_budget = None
        key = fc.CalibrationKey(board_instance, group_map.layout_hash, group_id,
                                self.signature(), mounting)
        self.key = key

    # ------------------------------------------------------------------ identity
    def signature(self) -> str:
        """Capture signature of this calibration: coefficient timing plus the schedule."""
        value = self.forces.data["acquisition"]["signature"] + "|" + self.scan.signature()
        if any(v != 1 for v in self.route_polarity.values()):
            value += "|relative_polarity=" + digest(self.route_polarity_json())
        return value

    def route_polarity_json(self):
        return {"%d:%d" % ref: int(sign) for ref, sign in self.route_polarity.items()}

    def readiness(self) -> List[str]:
        """Reasons the capture should not start (empty = ready)."""
        problems = capture_module.schedule_mismatches(self.forces.data["acquisition"]["signature"],
                                                      self.scan)
        problems.extend(self.forces.quality_issues(self.group.group_id))
        if not (self.settings.vref == self.forces.data["acquisition"]["voltage_span_v"]):
            problems.append("ADC voltage span differs from the electrical record")
        if self.route_polarity != self.forces.route_polarity(self.group.group_id):
            problems.append("relative polarity differs from the force-input file; recreate or update the calibration")
        return problems

    # ------------------------------------------------------------------- capture
    def _raw_path(self) -> Optional[Path]:
        if self.results_dir is None:
            return None
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._trial_index += 1
        return self.results_dir / ("%s_%s_%s_%03d.cap" %
                                   ("polarity" if self.capture_purpose == "polarity_test" else
                                    "unloaded" if self._unloaded_duration_s else "trial",
                                    self.group.group_id, self._session_id, self._trial_index))

    def model_revision(self):
        base = self.base_model_revision()
        return digest({"base": base, "unloaded_correction": self.unloaded_correction}) if self.unloaded_correction else base

    def base_model_revision(self):
        value = {"electrical": self._electrical_hash,
                       "models": {str(r): asdict(m) for r, m in self.models.items()},
                       "leakage": {str(r): asdict(m) for r, m in self.leakage_models.items()},
                       "correction_revision": self._correction_revision,
                       "excursion_guard_v": self.excursion_guard_v}
        if any(v != 1 for v in self.route_polarity.values()):
            value["relative_polarity"] = self.route_polarity_json()
        return digest(value)

    def _basis(self):
        return digest({"model": self.model_revision(), "trials": [t.to_json() for t in self.trials],
                       "limits": self.limits, "reference": self.reference_kind,
                       "configured_relative_polarity": self.forces.relative_polarity(self.group.group_id),
                       "electrical_current": self.forces.electrical_hash()})

    def invalidate(self, reason):
        if self.fit is not None or self.validation is not None:
            self.analysis_history.append({"reason": reason, "fit": fc._fit_json(self.fit) if self.fit else None,
                                          "validation": fc._validation_json(self.validation) if self.validation else None})
        self.fit = self.validation = None
        self._fit_basis = None
        self._stale_reason = reason

    def _persist(self, state, trial=None, result=None):
        if self._sidecar is None:
            return
        self._evidence.update(state=state, phase=self.wizard.phase, updated=fc._now())
        if self.wizard.marks and self.capture_purpose == "weight_trial":
            self._evidence["marks_s"] = dict(self.wizard.marks)
        if trial is not None:
            self._evidence["trial"] = trial.to_json()
            self._evidence.setdefault("analyses", []).append({
                "model_revision": self.model_revision(), "trial": trial.to_json(),
                "limits": self.limits.copy(), "window_s": self.window_s, "guard_s": self.guard_s,
                "unloaded_correction": copy.deepcopy(self.unloaded_correction),
                "route_polarity": self.route_polarity_json(),
                "range_policy": fc.RANGE_POLICY,
                "applied_correction": self._applied_leakage.to_json() if self._applied_leakage else None})
        if result is not None:
            self._evidence.update(raw_sha256=result.raw_sha256, capture_problems=result.reasons(),
                                  sweeps=result.sweeps, duration_s=result.duration_s)
        write_json(self._sidecar, self._evidence)

    @_locked
    def start_trial(self, accept_mismatch: bool = False, *, unloaded_duration_s=None,
                    linear_pretest=False, polarity_test=False) -> None:
        """Start the continuous capture and the unloaded baseline."""
        if self.session is not None:
            raise SessionError("a trial is already running")
        if polarity_test and (unloaded_duration_s is not None or linear_pretest):
            raise SessionError("polarity listening is separate from drift measurement")
        if unloaded_duration_s is not None and not (math.isfinite(unloaded_duration_s) and 6 <= unloaded_duration_s <= 120):
            raise SessionError("unloaded measurement duration must be 6-120 seconds")
        if linear_pretest and (unloaded_duration_s is None or self.wizard.current is None):
            raise SessionError("choose a drift window and a pending weight trial first")
        if linear_pretest and self.trials:
            raise SessionError("run the continuous drift test before collecting weights; reset trials to begin a new run")
        problems = self.readiness()
        # The override is only for an exploratory timing mismatch, never corrupt inputs.
        if self.forces.quality_issues(self.group.group_id):
            raise SessionError("; ".join(self.forces.quality_issues(self.group.group_id)))
        if self.route_polarity != self.forces.route_polarity(self.group.group_id):
            raise SessionError("relative polarity differs from the force-input file; update the calibration")
        if self.forces.electrical_hash() != self._electrical_hash:
            self.invalidate("electrical inputs changed; create a new calibration")
            raise SessionError(self._stale_reason)
        if not polarity_test:
            force_zero.verify_source(self.unloaded_correction)
        if problems and not accept_mismatch:
            raise SessionError("; ".join(problems))
        if problems:
            self.log.append("schedule mismatch accepted by the user: " + "; ".join(problems))
        self._exploratory_reasons = list(problems)
        step = self.wizard.current
        if step is None and unloaded_duration_s is None and not polarity_test:
            raise SessionError("no steps left")
        if unloaded_duration_s is None and not polarity_test:
            self.invalidate("trial collection changed")
        self._checking_frozen_drift = (not polarity_test and unloaded_duration_s is None and
            bool(self.unloaded_correction) and self.unloaded_correction.get("correction_kind") == "charge_rate")
        if self._checking_frozen_drift:
            measured = self.unloaded_correction
            unloaded_duration_s = measured["check_window_s"][1] - measured["fit_window_s"][0]
            linear_pretest = True
        self._unloaded_duration_s = unloaded_duration_s
        self._linear_pretest = linear_pretest
        self._linear_last_check_s = 0.
        error_n = self.limits["max_error_n"] or (min(self.wizard.masses) * 1e-3 * self.g * self.limits["hold_drift_fraction"])
        self._linear_rate_budget = abs(self.d33_initial) * error_n / self.wizard.hold_s
        self.capture_purpose = "polarity_test" if polarity_test else "unloaded_drift" if unloaded_duration_s is not None else "weight_trial"
        if polarity_test:
            self.last_polarity_check = self.last_polarity_result = None
            self._polarity_progress = {"phase": "quiet"}
            self._polarity_last_check_s = 0.
        path = self._raw_path()
        self._sidecar = path.with_suffix(".trial.json") if path else None
        if self._sidecar is not None:
            self._trial_sidecars.append(str(self._sidecar))
        meta = {"group": self.group.group_id,
                "mass_g": 0. if unloaded_duration_s is not None or polarity_test else step.mass_g,
                "repeat": None if unloaded_duration_s is not None or polarity_test else step.repeat,
                "purpose": self.capture_purpose,
                "signature": self.signature(), "electrical_manifest_sha256": self._electrical_hash,
                "model_revision": self.model_revision(), "reference_kind": "polarity_only" if polarity_test else self.reference_kind}
        self._evidence = {"format": "captest-force-trial-1", "session_id": self._session_id,
                          "created": fc._now(), "raw_file": str(path) if path else "",
                          **meta, "models": {str(r): asdict(m) for r, m in self.models.items()},
                          "electrical_package": copy.deepcopy(self.forces.latest(self.group.group_id)),
                          "polarity": self.polarity, "span_volts": self.settings.vref,
                          "route_polarity": self.route_polarity_json(),
                          "window_s": self.window_s, "guard_s": self.guard_s, "g": self.g,
                          "excursion_guard_v": self.excursion_guard_v,
                          "board_instance": self.board_instance, "mounting": self.mounting,
                          "contact_position": self.contact_position,
                          "limits": self.limits.copy(), "schedule": self.scan.to_json(),
                          "initial_d33_c_per_n": self.d33_initial,
                          "unloaded_correction": copy.deepcopy(self.unloaded_correction),
                          "applied_leakage": self._applied_leakage.to_json() if self._applied_leakage else None,
                          "model_provenance": self._model_provenance,
                          "linear_pretest": linear_pretest,
                          "checking_frozen_drift": self._checking_frozen_drift,
                          "exploratory_reasons": list(problems)}
        self._persist("starting")
        self.session = CaptureSession(self.client, self.scan, self.group.refs,
                                      record_path=path, span_volts=self.settings.vref,
                                      meta=meta)
        try:
            self.session.start()
        except Exception:
            result = self.session.stop()
            self.session = None
            self._persist("start_failed", result=result)
            raise
        if unloaded_duration_s is None and not polarity_test:
            self.wizard.begin(0.0)
        self._persist("capturing")
        self._started_device_time = 0.0

    @property
    def now(self) -> float:
        return self.session.device_time_s if self.session is not None else 0.0

    def tick(self) -> str:
        """Read what has arrived and advance the timed phases. Returns the wizard phase.

        Reading (serial I/O, decoding) happens outside the controller lock, so the GUI's marks
        and buttons are never queued behind a slow read."""
        session = self.session
        if session is None:
            return self.wizard.phase
        session.poll()
        with self._lock:
            if self.session is not session:          # cancelled while reading
                return self.wizard.phase
            if not session.running and not session.assembler.problems:
                session.assembler._problem(capture_module.GAP, "capture stopped before the guided trial completed")
            if session.assembler.problems:
                if self.capture_purpose == "polarity_test":
                    self._finish_polarity(reason="capture integrity check failed")
                    return "polarity_complete"
                if self._unloaded_duration_s is not None:
                    self._finish_unloaded()
                    return self.wizard.phase
                return self._abort_invalid()        # any gap/overrun/tag/reset problem ends the trial
            if self.capture_purpose == "polarity_test":
                self._check_automatic_polarity()
                return "polarity_" + self._polarity_progress.get("phase", "listening")
            if self._unloaded_duration_s is not None:
                if self._linear_pretest:
                    self._check_linear_pretest()
                elif self.now >= self._unloaded_duration_s:
                    self._finish_unloaded()
                return self.wizard.phase
            t, w = self.now, self.wizard
            if w.phase == fc.BASELINE and t - w.marks["baseline_start"] >= self.baseline_s:
                w.baseline_done(t)
                self._persist("capturing")
            elif w.phase == fc.HOLD and t - w.marks["place_clear"] >= w.hold_s:
                w.hold_done(t)
                self._persist("capturing")
            elif w.phase == fc.UNLOADED and t - w.marks["remove_clear"] >= self.unloaded_s:
                w.unloaded_done(t)
                self._finish_capture()
            return w.phase

    def refresh_view(self, force: bool = False) -> None:
        """Compute the plot snapshot (called by the capture worker, never by the GUI thread;
        throttled, because plotting must not slow the reading)."""
        now = time.monotonic()
        if not force and now - self._view_time < VIEW_INTERVAL_S:
            return
        self._view_time = now
        self._view = self.live_view()

    def latest_view(self) -> Optional["LiveView"]:
        """The newest snapshot (a cheap attribute read: safe from the GUI thread)."""
        return self._view if self.session is not None else None

    def _need_session(self) -> CaptureSession:
        if self.session is None:
            raise SessionError("no trial is running")
        return self.session

    @_locked
    def mark_placement(self, t: Optional[float] = None) -> None:
        self.wizard.mark_placement(self.now if t is None else t)
        self._persist("capturing")

    @_locked
    def hand_clear_placement(self, t: Optional[float] = None) -> None:
        self.wizard.hand_clear_placement(self.now if t is None else t)
        self._persist("capturing")

    @_locked
    def mark_removal(self, t: Optional[float] = None) -> None:
        if self.wizard.phase == fc.HOLD:
            raise SessionError("wait for the hold to finish before removing the mass")
        self.wizard.mark_removal(self.now if t is None else t)
        self._persist("capturing")

    @_locked
    def hand_clear_removal(self, t: Optional[float] = None) -> None:
        self.wizard.hand_clear_removal(self.now if t is None else t)
        self._persist("capturing")

    @_locked
    def cancel_trial(self) -> None:
        if self.session is not None and self.capture_purpose == "polarity_test":
            self._finish_polarity(reason="polarity test cancelled")
            return
        if self.session is not None:
            result = self.session.cancel()
            self.last_result = result
            self.session = None
            if result.raw_file:
                companion = Path(result.raw_file).with_suffix(".npz")
                self._evidence["companion_sha256"] = fc.Capture(
                    result.samples, self.settings.vref, result.raw_file).save(companion)
                self._evidence["companion"] = companion.name
            self._persist("cancelled", result=result)
        if self.capture_purpose == "unloaded_drift":
            self._unloaded_duration_s = None
            self.last_unloaded = {"status": "cancelled", "reasons": ["measurement cancelled; previous correction retained"]}
        else:
            self.wizard.cancel_trial()

    def _abort_invalid(self) -> str:
        """The capture failed while running (gap, overrun, disconnect...): record the rejected
        trial with its reasons and let the user retry."""
        session = self._need_session()
        result = session.stop()
        self.session = None
        self.last_result = result
        self.last_weight_result = result
        step = self.wizard.current
        trial = fc.TrialResult(step.mass_g, step.repeat, False,
                               ["CAPTURE: %s" % r for r in result.reasons()], [], [], {}, {}, None,
                               None, {}, {}, capture_file=result.raw_file,
                               capture_sha256=result.raw_sha256)
        trial.reference_kind = self.reference_kind
        trial.model_revision = self.model_revision()
        trial.sidecar = str(self._sidecar) if self._sidecar else ""
        self.trials.append(trial)
        self._save_companion(trial, fc.Capture(result.samples, self.settings.vref, result.raw_file))
        self._persist("rejected", trial, result)
        self.wizard.abort_trial(trial)
        return self.wizard.phase

    def _finish_unloaded(self, rejected_record=None):
        duration = self._unloaded_duration_s
        result = self._need_session().stop()
        self.session = None
        self.last_result = result
        self.last_unloaded_result = result
        record = rejected_record or {"status": "rejected", "reasons": result.reasons()}
        try:
            if result.valid and rejected_record is None:
                record = force_zero.analyse(self.models, result.samples, duration_s=duration,
                    model_basis=self.base_model_revision(), signature=self.signature(),
                    leakage=self.leakage_models, polarity=self.polarity, span_volts=self.settings.vref,
                    route_polarity=self.route_polarity)
                # Residual bias is measured in the raw electrical coordinate;
                # only charge/force contributions acquire relative channel signs.
                vd_trial = fc.TrialResult(0, 0, False, [], [], [], {}, {}, None, None, {}, {})
                self._mark_vd(vd_trial, result)
                if vd_trial.flags:
                    record["status"] = "rejected"
                    record["reasons"].extend(vd_trial.flags)
            record.update(capture_file=result.raw_file, capture_sha256=result.raw_sha256,
                          sidecar=str(self._sidecar) if self._sidecar else "")
            if record["status"] == "qualified":
                self._set_unloaded_correction(record)
        except (fm.ModelError, fc.CalibrationError, OSError, ValueError) as exc:
            record.update(status="rejected", reasons=[str(exc)])
        finally:
            if result.raw_file:
                companion = Path(result.raw_file).with_suffix(".npz")
                self._evidence["companion_sha256"] = fc.Capture(
                    result.samples, self.settings.vref, result.raw_file).save(companion)
                self._evidence["companion"] = companion.name
            self.last_unloaded = record
            self._evidence["unloaded_measurement"] = record
            self._persist("unloaded_" + record["status"], result=result)
            self._unloaded_duration_s = None

    def _check_linear_pretest(self):
        t = self.now
        window = self._unloaded_duration_s
        if t < window + 2 or t - self._linear_last_check_s < 1:
            return
        self._linear_last_check_s = t
        session = self._need_session()
        result = session.assembler.result(str(session.recorder.path) if session.recorder else "", include_timing=False)
        record = force_zero.analyse_linear(self.models, result.samples, end_s=t, window_s=window,
            model_basis=self.base_model_revision(), signature=self.signature(),
            max_rate_error_c_per_s=self._linear_rate_budget, leakage=self.leakage_models,
            polarity=self.polarity, span_volts=self.settings.vref, route_polarity=self.route_polarity)
        vd_trial = fc.TrialResult(0, 0, False, [], [], [], {}, {}, None, None, {}, {})
        self._mark_vd(vd_trial, result)
        if vd_trial.flags:
            record["status"] = "rejected"
            record["reasons"].extend(vd_trial.flags)
        if self._checking_frozen_drift:
            fixed = force_zero.drift_values(self.unloaded_correction)
            mismatch = sum(abs(row["check_q_rate_c_per_s"] - fixed[tuple(map(int, key.split(":")))])
                           for key, row in record["routes"].items())
            record["frozen_rate_mismatch_c_per_s"] = mismatch
            record["check_corrected_q_rate_c_per_s"] = sum(
                row["check_q_rate_c_per_s"] - fixed[tuple(map(int, key.split(":")))]
                for key, row in record["routes"].items())
            if mismatch > self._linear_rate_budget:
                record["status"] = "rejected"
                record["reasons"].append("unloaded drift does not match the frozen correction; no automatic refit")
        self.last_unloaded = record
        if record["status"] != "qualified":
            self._evidence["linear_pretest_last_check"] = record
            if t >= max(LINEAR_SETTLE_TIMEOUT_S, window + 2):
                self._finish_unloaded(rejected_record=record)
            return
        if session.recorder and not self._checking_frozen_drift:
            size, sha = session.recorder.snapshot_hash()
            record.update(capture_file=str(session.recorder.path), capture_prefix_bytes=size,
                          capture_prefix_sha256=sha, sidecar=str(self._sidecar))
        if not self._checking_frozen_drift:
            self._set_unloaded_correction(record, allow_running=True)
        self.last_unloaded_result = result
        self._evidence["frozen_drift_verification" if self._checking_frozen_drift else "unloaded_measurement"] = copy.deepcopy(record)
        self._unloaded_duration_s = None
        self.capture_purpose = "weight_trial"
        step = self.wizard.current
        self._evidence.update(purpose="weight_trial", mass_g=step.mass_g, repeat=step.repeat,
                              pretest_end_s=t)
        self.wizard.begin(t)
        self._persist("capturing")

    def _set_unloaded_correction(self, record, *, allow_running=False):
        if self.session is not None and not allow_running:
            raise SessionError("finish capture before changing the unloaded correction")
        if record and (record["model_basis"] != self.base_model_revision() or record["signature"] != self.signature()):
            raise SessionError("unloaded correction belongs to a different model or scan")
        force_zero.verify_source(record)
        previous, trials = self.unloaded_correction, copy.deepcopy(self.trials)
        self.unloaded_correction = copy.deepcopy(record)
        try:
            self.reanalyse_trials()
        except Exception as exc:
            self.unloaded_correction, self.trials = previous, trials
            self._analysis_error = str(exc)
            self.invalidate("unloaded correction replay failed; original correction retained")
            raise
        self._analysis_error = ""
        self.invalidate("unloaded correction changed; trials replayed, refit and validate")

    def clear_unloaded_correction(self):
        self._set_unloaded_correction(None)

    def check_press_polarity(self):
        if self.session is not None:
            raise SessionError("finish the press/release capture before checking polarity")
        if self.last_polarity_check is not None:
            return copy.deepcopy(self.last_polarity_check)
        if not self.trials or self.last_weight_result is None or not self.last_weight_result.valid:
            raise SessionError("record an intact press/release test first")
        trial = self.trials[-1]
        if trial.marks is None:
            raise SessionError("the press test needs baseline, press and release marks")
        report = force_polarity.check_press(self.models, self.last_weight_result.samples, trial.marks,
            span_volts=self.settings.vref, window_s=self.window_s, guard_s=self.guard_s,
            leakage=self.leakage_models or None)
        report.update(group_id=self.group.group_id, created=fc._now(),
                      capture_file=trial.capture_file, capture_sha256=trial.capture_sha256,
                      sidecar=trial.sidecar, model_revision=self.model_revision(), marks_s=trial.to_json()["marks_s"])
        return report

    def polarity_prompt(self):
        phase = self._polarity_progress.get("phase", "listening")
        return {"quiet": "Starting listener: a brief noise sample, then press and release naturally. No stage buttons or timed hold.",
                "listening": "Listening: gently press and release the plate once. No stage buttons or minimum hold.",
                "pressed": "Press detected. Release naturally when ready; no minimum hold.",
                "released": "Release detected; finishing the polarity check…",
                "complete": "Polarity test finished. Review the channel directions."}.get(phase, "Listening for a press and release.")

    def _check_automatic_polarity(self):
        if self.now - self._polarity_last_check_s < .05:
            return
        self._polarity_last_check_s = self.now
        samples, _ = self.session.assembler.live_samples()
        try:
            self._polarity_progress = force_polarity.detect_press(self.models, samples,
                span_volts=self.settings.vref, leakage=self.leakage_models or None)
        except (fm.ModelError, ValueError, ArithmeticError) as exc:
            self._finish_polarity(reason="polarity analysis: " + str(exc))
            return
        if self._polarity_progress.get("complete"):
            self._finish_polarity()
        elif self.now >= force_polarity.AUTO_TIMEOUT_S:
            self._finish_polarity(reason="no clear press/release pair detected; try another gentle press")

    def _finish_polarity(self, reason=""):
        result = self._need_session().stop()
        self.session = None
        self.last_polarity_result = result
        report = copy.deepcopy(self._polarity_progress)
        reasons = ([reason] if reason else []) + result.reasons()
        if not report.get("complete") or not result.valid or reason:
            report.update(status="unclear", complete=True, phase="complete")
            rows = report.setdefault("routes", {})
            for ref, model in self.models.items():
                row = rows.setdefault("%d:%d" % ref, {"status": "unclear", "suggested_sign": None, "reasons": []})
                row.update(status="unclear", suggested_sign=None)
                row["reasons"].extend(reasons or ["incomplete press/release"])
                row.setdefault("range", fm.range_report(model, result.samples.get(ref, {}).get("volts", []), span_volts=self.settings.vref))
        report.update(format="captest-press-polarity-2", automatic=True, phase="complete",
            group_id=self.group.group_id, created=fc._now(), model_revision=self.model_revision(),
            capture_file=result.raw_file, capture_sha256=result.raw_sha256,
            sidecar=str(self._sidecar) if self._sidecar else "", reasons=reasons)
        report.setdefault("note", "Automatic polarity test; no force gain fitted. Review unclear channels and repeat.")
        self.last_polarity_check = report
        self._polarity_progress = report
        self._evidence["polarity_check"] = copy.deepcopy(report)
        if result.raw_file:
            companion = Path(result.raw_file).with_suffix(".npz")
            self._evidence["companion_sha256"] = fc.Capture(result.samples, self.settings.vref, result.raw_file).save(companion)
            self._evidence["companion"] = companion.name
        self._persist("polarity_" + report["status"], result=result)

    def set_route_polarity(self, values):
        if self.session is not None:
            raise SessionError("finish capture before changing relative polarity")
        if set(values) != set(self.models) or any(v not in (-1, 1) for v in values.values()):
            raise SessionError("provide +1 or -1 for every channel")
        previous = (self.route_polarity, self.unloaded_correction, self.leakage_models,
                    self._applied_leakage, self.leakage, self._correction_revision, copy.deepcopy(self.trials))
        self.route_polarity = dict(values)
        self.unloaded_correction = self.leakage = self._applied_leakage = None
        self.leakage_models = {}
        self._correction_revision += 1
        self.invalidate("relative channel polarity changed; remeasure drift, refit and validate")
        try:
            self.reanalyse_trials()
        except Exception as exc:
            (self.route_polarity, self.unloaded_correction, self.leakage_models,
             self._applied_leakage, self.leakage, self._correction_revision, self.trials) = previous
            self._analysis_error = str(exc)
            raise
        self._analysis_error = ""
        self.key.capture_signature = self.signature()
        self.log.append(self._stale_reason)

    def _finish_capture(self) -> None:
        session = self._need_session()
        result = session.stop()
        self.session = None
        self.last_result = result
        self.last_weight_result = result
        w = self.wizard
        step = w.current
        if not result.valid:
            trial = fc.TrialResult(step.mass_g, step.repeat, False,
                                   ["CAPTURE: %s" % r for r in result.reasons()], [], [], {}, {},
                                   None, None, {}, {}, capture_file=result.raw_file,
                                   capture_sha256=result.raw_sha256)
        else:
            marks = w.trial_marks()
            marks = fc.Marks((marks.baseline[0] + BASELINE_SKIP_S, marks.baseline[1]), marks.place,
                             marks.place_clear, marks.remove, marks.remove_clear)
            capture = fc.Capture(result.samples, self.settings.vref, result.raw_file)
            try:
                trial = self._analyse(capture, marks, step.mass_g, step.repeat)
            except (fm.ModelError, ValueError, ArithmeticError) as exc:
                trial = fc.TrialResult(step.mass_g, step.repeat, False,
                    ["ANALYSIS: %s" % exc], [], [], {}, {}, None, None, {}, {}, marks=marks,
                    sampling=fc.sampling_rates(result.samples))
            trial.capture_file, trial.capture_sha256 = result.raw_file, result.raw_sha256
            self._mark_vd(trial, result)
        self._save_companion(trial, fc.Capture(result.samples, self.settings.vref, result.raw_file))
        trial.reference_kind = self.reference_kind
        trial.model_revision = self.model_revision()
        trial.sidecar = str(self._sidecar) if self._sidecar else ""
        self.trials.append(trial)
        self._persist("accepted_for_fit" if trial.accepted else "rejected", trial, result)
        w.finish_trial(trial)

    def _analyse(self, capture, marks, mass_g, repeat, *, contact_position=None):
        trial = fc.analyze_trial(capture, self.models, marks, mass_g, repeat,
                                polarity=self.polarity, leakage=self.leakage_models or None,
                                window_s=self.window_s, guard_s=self.guard_s, g=self.g,
                                load_removal_tol=self.limits["load_removal_fraction"],
                                hold_drift_tol=self.limits["hold_drift_fraction"],
                                position=self.contact_position if contact_position is None else contact_position,
                                fixed_bias=force_zero.bias_values(self.unloaded_correction),
                                fixed_drift=force_zero.drift_values(self.unloaded_correction),
                                route_polarity=self.route_polarity,
                                bias_source=digest(self.unloaded_correction) if self.unloaded_correction else "")
        trial.reference_kind = self.reference_kind
        trial.model_revision = self.model_revision()
        if self.excursion_guard_v is not None and any(
                v["max_above_vd_v"] > self.excursion_guard_v for v in trial.ranges.values()):
            trial.accepted = False
            trial.supports_mass = False
            trial.reasons.append("EXCURSION_GUARD: user voltage guard exceeded (not a physical calibration limit)")
        return trial

    def _mark_vd(self, trial: fc.TrialResult, result: CaptureResult) -> None:
        """VD stability: the capture's Vmid readings against the electrical record."""
        for adc, stats in result.vd_by_adc.items():
            ref = next((m for m in self.models.values() if m.ref[0] == adc), None)
            if ref is None:
                continue
            shift = stats["mean_v"] - ref.v_d
            if abs(shift) > 0.003:
                trial.flags.append("VD_SHIFT_ADC%d_%+.1fmV" % (adc, shift * 1e3))
            if stats["std_v"] > 0.002:
                trial.flags.append("VD_NOISY_ADC%d" % adc)

    def _save_companion(self, trial: fc.TrialResult, capture: fc.Capture) -> None:
        if self.results_dir is None:
            return
        name = Path(trial.capture_file).with_suffix(".npz") if trial.capture_file else None
        if name is not None:
            trial.companion = name.name
            trial.companion_sha256 = capture.save(name)

    # -------------------------------------------------------------------- display
    def drift_precheck_message(self, record=None):
        if record is None and self._linear_last_check_s == 0:
            return "Waiting for enough quiet samples to check drift."
        record = self.last_unloaded if record is None else record
        if not record:
            return "Waiting for enough quiet samples to check drift."
        parts = []
        if "slope_change_bound_c_per_s" in record:
            parts.append("Slope-change bound %.3g pC/s; allowed %.3g pC/s." % (
                record["slope_change_bound_c_per_s"] * 1e12, record["rate_error_budget_c_per_s"] * 1e12))
        if "frozen_rate_mismatch_c_per_s" in record:
            parts.append("Mismatch from saved drift %.3g pC/s." % (record["frozen_rate_mismatch_c_per_s"] * 1e12))
        if record.get("reasons"):
            parts.append("Waiting: " + "; ".join(record["reasons"]) + ".")
        return " ".join(parts) or "Drift check passed."

    def live_view(self, last_s: float = 30.0) -> Optional[LiveView]:
        """Snapshot of the running capture for plotting (cheap: only the kept routes)."""
        with self._lock:
            session = self.session
            marks = dict(self.wizard.marks)
            phase, prompt = self.wizard.phase, self.wizard.prompt()
            if self._unloaded_duration_s is not None:
                phase = "unloaded_drift"
                prompt = "Measuring unloaded drift for %g s: keep wiring/plate unchanged, no added weights, hands off." % self._unloaded_duration_s
                if self._linear_pretest:
                    prompt = "Drift test: waiting for a stable linear section, then checking its frozen slope. No weights or touching until placement is armed."
                    if self._checking_frozen_drift:
                        prompt = "Checking the same frozen drift after settling. No refitting; do not place weights until prompted."
                    prompt += "\n" + self.drift_precheck_message()
        if session is None:
            return None
        samples, _ = session.assembler.live_samples()
        if not samples:
            return None
        if self.capture_purpose == "polarity_test":
            resid, head = {}, {}
            for ref, d in samples.items():
                resid[ref] = (d["t"], fm.residuals(self.models[ref], d["volts"], d["tc"], d.get("valid"),
                    d.get("generation"), self.leakage_models.get(ref, fm.NO_LEAKAGE)))
                head[ref] = fm.range_report(self.models[ref], d["volts"], span_volts=self.settings.vref)
            t = np.asarray(next(iter(samples.values()))["t"])
            blank = np.full(len(t), np.nan)
            return LiveView("polarity_test", self.polarity_prompt(), session.device_time_s,
                t, blank, blank, resid, head, [str(p) for p in session.assembler.problems],
                fc.sampling_rates(samples, last=2000))
        try:
            measuring_linear = self._linear_pretest and self._unloaded_duration_s is not None
            charge = fm.accumulate(self.models, samples, {} if measuring_linear else self._live_bias(samples, marks),
                                   self.polarity, self.leakage_models or None,
                                   None if measuring_linear else force_zero.drift_values(self.unloaded_correction),
                                   self.route_polarity)
        except fm.ModelError:
            return None
        resid = {}
        head = {}
        range_start = marks.get("baseline_start", 0.) + BASELINE_SKIP_S
        if self._unloaded_duration_s is not None:
            range_start = (max(2., session.device_time_s - self._unloaded_duration_s)
                           if self._linear_pretest else 2.)
        for ref, d in samples.items():
            e = fm.residuals(self.models[ref], d["volts"], d["tc"], d["valid"],
                             leakage=self.leakage_models.get(ref, fm.NO_LEAKAGE))
            resid[ref] = (d["t"], e)
            _, inputs = fm.range_window(d["t"], range_start)
            head[ref] = fm.range_report(self.models[ref], np.asarray(d["volts"])[inputs], span_volts=self.settings.vref)
        origin = marks.get("baseline_end", None)
        force = charge.force(self.d33_initial, origin_t=origin if origin is not None else None)
        if origin is None and self.unloaded_correction is None and not self._linear_pretest:
            force = np.full(force.shape, np.nan)  # baseline residuals are not yet a force estimate
        keep = charge.t >= marks.get("capture_start", 0.) if self._linear_pretest and self._unloaded_duration_s is None else np.ones(len(charge.t), dtype=bool)
        return LiveView(phase, prompt, session.device_time_s, charge.t[keep], charge.q_proxy[keep],
                        force[keep], resid, head, [str(p) for p in list(session.assembler.problems)],
                        fc.sampling_rates(samples, last=2000))

    def _live_bias(self, samples, marks) -> Dict[Channel, float]:
        """Live display bias: the mean residual of the baseline so far (display only; the
        analysis estimates and records its own frozen bias)."""
        end = marks.get("baseline_end")
        start = marks.get("baseline_start", 0.0) + BASELINE_SKIP_S
        bias = {}
        if self.unloaded_correction is not None:
            return force_zero.bias_values(self.unloaded_correction)
        if end is None:
            return bias
        for ref, d in samples.items():
            e = fm.residuals(self.models[ref], d["volts"], d["tc"], d["valid"],
                             leakage=self.leakage_models.get(ref, fm.NO_LEAKAGE))
            sel = (d["t"] >= start) & (d["t"] <= end) & np.isfinite(e)
            if sel.sum() >= 8:
                bias[ref] = float(e[sel].mean())
        return bias

    # ------------------------------------------------------------- fit & validation
    def accepted_events(self) -> List[fm.CalEvent]:
        return [e for t in self.trials if t.accepted and t.reference_kind == "known_mass" for e in t.events]

    def fit_gain(self) -> fm.D33Fit:
        if self._analysis_error:
            raise SessionError("reanalysis must succeed before fitting: " + self._analysis_error)
        if self.reference_kind != "known_mass":
            raise SessionError("workflow-only finger tests have no known force reference")
        if self.route_polarity != self.forces.route_polarity(self.group.group_id):
            self.invalidate("relative channel polarity changed")
            raise SessionError(self._stale_reason)
        if self.forces.electrical_hash() != self._electrical_hash:
            self.invalidate("electrical inputs changed")
            raise SessionError(self._stale_reason)
        self.validation = None
        self.fit = fm.fit_d33(self.accepted_events(), self.g)
        self._fit_basis = self._basis()
        self._stale_reason = ""
        self.log.append("fitted d33_eff %.3f pC/N (initial %.3f)"
                        % (self.fit.d33_pc_per_n, self.d33_initial * 1e12))
        return self.fit

    def validate(self, test_repeats: Optional[Sequence[int]] = None,
                 limit_n: Optional[float] = None) -> fm.Validation:
        if (self._analysis_error or self.forces.electrical_hash() != self._electrical_hash
                or self.route_polarity != self.forces.route_polarity(self.group.group_id)):
            self.invalidate("trial reanalysis or electrical context is stale")
            raise SessionError(self._analysis_error or self._stale_reason)
        events = self.accepted_events()
        repeats = sorted({e.repeat for e in events})
        if len(repeats) < 2:
            raise SessionError("held-out validation needs at least two accepted repeats")
        test = list(test_repeats) if test_repeats is not None else [repeats[-1]]
        limit_n = self.limits["max_error_n"] if limit_n is None else limit_n
        self.limits["max_error_n"] = limit_n
        self.validation = fm.validate_holdout(events, test, self.g, limit_n)
        # Freeze the training gain actually used to assess held-out repetitions.
        self.fit = self.validation.fit
        self._fit_basis = self._basis()
        return self.validation

    def evaluate_leakage(self) -> fm.LeakageFit:
        """Fit an effective off-state leakage on the constant-force windows of the first
        intact trial, including a rejected hold/model trial. The result is `not_identified`
        unless the data supports it; nothing
        is applied until :meth:`apply_leakage`."""
        trial = next((t for t in self.trials if t.marks and t.capture_file
                      and not any(r.startswith(("CAPTURE", "CLIPPED", "ADC_HEADROOM_LIMIT", "INVALID")) for r in t.reasons)
                      and {"post_place", "pre_remove"}.issubset(t.windows)), None)
        if trial is None or not trial.capture_file:
            raise SessionError("an intact, unclipped capture with labelled constant-force windows is needed")
        result = capture_module.replay_file(trial.capture_file, keep_routes=self.group.refs,
                                            span_volts=self.settings.vref)
        if digest_bytes(trial.capture_file) != trial.capture_sha256 or not result.valid:
            raise SessionError("leakage evaluation requires the original intact recording")
        marks: fc.Marks = trial.marks
        windows = [fm.ConstantWindow("baseline", marks.baseline[0], marks.baseline[1], "baseline"),
                   fm.ConstantWindow("hold", trial.windows["post_place"][0],
                                     trial.windows["pre_remove"][1], "hold")]
        if "post_remove" in trial.windows:
            windows.append(fm.ConstantWindow("unloaded", trial.windows["post_remove"][0],
                                             trial.windows["post_remove"][1], "unloaded"))
        self.leakage = fm.fit_leakage(self.models, result.samples, windows, self.polarity,
                                    route_polarity=self.route_polarity)
        return self.leakage

    def apply_leakage(self) -> None:
        if self.leakage is None or self.leakage.status != "identified":
            raise SessionError("leakage is not identified: no correction can be applied")
        self._set_correction(self.leakage)

    def rollback_leakage(self):
        self._set_correction(None)

    def _set_correction(self, proposed):
        if self.session is not None:
            raise SessionError("finish or cancel capture before changing its model")
        previous = (self.leakage_models, self._applied_leakage, self._correction_revision,
                    copy.deepcopy(self.trials), self.unloaded_correction)
        self.leakage_models = ({ref: proposed.model for ref in self.group.refs} if proposed else {})
        self._applied_leakage = proposed
        self._correction_revision += 1
        self.unloaded_correction = None  # a different predictor requires a new zero-drift measurement
        try:
            self.reanalyse_trials()
        except Exception as exc:
            self.leakage_models, self._applied_leakage, self._correction_revision, self.trials, self.unloaded_correction = previous
            self._analysis_error = str(exc)
            self.invalidate("reanalysis failed; original model retained")
            raise
        self._analysis_error = ""
        self.invalidate("correction changed; trials replayed, refit and validate")
        if previous[-1] is not None:
            self.log.append("Unloaded correction cleared because the leakage predictor changed; measure unloaded drift again.")
        self.log.append(self._stale_reason)

    def reanalyse_trials(self):
        revised = []
        last_weight = None
        for old in self.trials:
            if old.marks is None or not old.capture_file:
                revised.append(old)
                continue
            if any(r.startswith("CAPTURE:") for r in old.reasons):
                revised.append(old)
                continue
            raw = Path(old.capture_file)
            if digest_bytes(raw) != old.capture_sha256:
                raise SessionError("raw capture hash changed: " + str(raw))
            result = capture_module.replay_file(raw, keep_routes=self.group.refs, span_volts=self.settings.vref)
            if not result.valid:
                raise SessionError("cannot reanalyse damaged capture: " + "; ".join(result.reasons()))
            # An optional annotation may have changed for later trials. Keep the
            # captured position when replaying earlier evidence.
            position = (read_json(old.sidecar).get("contact_position", "") if old.sidecar
                        else old.events[0].position if old.events else "")
            trial = self._analyse(fc.Capture(result.samples, self.settings.vref), old.marks,
                                  old.mass_g, old.repeat, contact_position=position)
            trial.analysis_revision = old.analysis_revision + 1
            for name in ("capture_file", "capture_sha256", "companion", "companion_sha256", "sidecar"):
                setattr(trial, name, getattr(old, name))
            self._mark_vd(trial, result)
            last_weight = result
            revised.append(trial)
        self.analysis_history.extend({"reason": "reanalysis", "trial": t.to_json()} for t in self.trials)
        self.trials = revised
        if last_weight is not None:
            self.last_weight_result = last_weight
        for step in self.wizard.steps:
            matching = [t for t in revised if t.mass_g == step.mass_g and t.repeat == step.repeat]
            if matching and step.status in (fc.DONE_STEP, fc.FAILED_STEP):
                newest = matching[-1]
                step.status = fc.DONE_STEP if newest.accepted else fc.FAILED_STEP
                step.reason = "; ".join(newest.reasons)
        for trial in revised:
            if trial.sidecar:
                body = read_json(trial.sidecar)
                body.setdefault("analyses", []).append({"model_revision": self.model_revision(),
                    "trial": trial.to_json(), "applied_correction":
                    self._applied_leakage.to_json() if self._applied_leakage else None,
                    "limits": self.limits.copy(), "window_s": self.window_s, "guard_s": self.guard_s})
                body["analyses"][-1]["unloaded_correction"] = copy.deepcopy(self.unloaded_correction)
                body["analyses"][-1]["route_polarity"] = self.route_polarity_json()
                body["analyses"][-1]["range_policy"] = fc.RANGE_POLICY
                body["trial"] = trial.to_json()
                write_json(trial.sidecar, body)

    def edit_trial_marks(self, index, marks):
        if self.session is not None:
            raise SessionError("finish capture before reviewing windows")
        previous = copy.deepcopy(self.trials)
        old = self.trials[index]
        self.trials[index] = copy.copy(old)
        self.trials[index].marks = marks
        try:
            self.reanalyse_trials()
        except Exception as exc:
            self.trials = previous
            self._analysis_error = str(exc)
            self.invalidate("window reanalysis failed; original windows retained")
            raise
        self._analysis_error = ""
        self.invalidate("trial windows changed; refit and validate")

    # ---------------------------------------------------------------------- record
    def build_record(self) -> dict:
        if self.fit is not None and self._fit_basis != self._basis():
            self.invalidate("trial/model/limits changed since fit")
        record = fc.build_record(
            self.key, array=self.group.array, pzt=self.group.pzt, d33_initial=self.d33_initial,
            fit=self.fit, validation=self.validation, trials=self.trials, wizard=self.wizard,
            polarity=self.polarity, g=self.g,
            manifest_sha256=self.forces.electrical_hash(),
            leakage=self._applied_leakage, simulated=self.simulated, mounting_notes=self.mounting,
            schedule=self.scan.to_json(), previous=self._previous_record, limits=self.limits)
        record.update(model_revision=self.model_revision(), model_provenance=self._model_provenance,
                      correction_revision=self._correction_revision,
                      proposed_leakage=self.leakage.to_json() if self.leakage else None,
                      correction_applied=self._applied_leakage is not None,
                      analysis_history=self.analysis_history, reference_kind=self.reference_kind)
        record["trial_sidecars"] = list(self._trial_sidecars)
        record["route_polarity"] = self.route_polarity_json()
        record["unloaded_correction"] = copy.deepcopy(self.unloaded_correction)
        record["last_unloaded_measurement"] = copy.deepcopy(self.last_unloaded)
        record["contact_position_notes"] = self.contact_position
        record["electrical_manifest"] = copy.deepcopy(self.forces._body())
        if self.reference_kind != "known_mass":
            record["supported_range"].update(masses_g=[], max_mass_g=None, force_n=None,
                note="Unknown-force workflow only; schedule masses are labels, not known applied loads")
        if self.fit and self.validation and self.validation.limit_n is not None:
            if abs(self.fit.intercept / self.fit.d33_eff) > self.validation.limit_n:
                record["quality_reasons"].append("event intercept exceeds the deployed force-error budget")
        if self._stale_reason and self.fit is None and self.analysis_history:
            record["status"] = "stale"
            record["quality_reasons"].append(self._stale_reason)
        if self._exploratory_reasons:
            record["quality_reasons"].extend(self._exploratory_reasons)
            if record["status"] == "validated":
                record["status"] = "fitted_provisional"
        if self._analysis_error:
            record["quality_reasons"].append("incomplete reanalysis: " + self._analysis_error)
        # Validation scope is only the range independently exercised by held-out trials.
        if self.validation:
            held = [t for t in self.trials if t.accepted and t.repeat in self.validation.test_repeats]
            record["validated_domain"] = {ref: {
                "v_min": min(t.ranges[ref]["v_min"] for t in held if ref in t.ranges),
                "v_max": max(t.ranges[ref]["v_max"] for t in held if ref in t.ranges)}
                for ref in {r for t in held for r in t.ranges}}
            masses = sorted({t.mass_g for t in held})
            positions = sorted({e.position for t in held for e in t.events})
            record["validated_range"] = {"masses_g": masses,
                "force_n": [min(masses) * 1e-3 * self.g, max(masses) * 1e-3 * self.g] if masses else None,
                "voltage_by_route": record["validated_domain"], "contact_positions": positions,
                "position_independent": False, "capture_signature": self.signature(),
                "note": "Force validation in held-out known-mass trials; not an electrical charge calibration"}
        if record["quality_reasons"] and record["status"] == "validated":
            record["status"] = "fitted_provisional"
        if record["status"] != "validated" and "validated_domain" in record:
            record["tested_domain"] = record.pop("validated_domain")
            record["tested_range"] = record.pop("validated_range")
            record["tested_range"]["note"] = "Held-out tested range; force validation has not passed all acceptance checks"
        return record

    def save(self, store: fc.CalibrationStore) -> Path:
        previous = store.load(self.key)
        self._previous_record = previous
        path = store.save(self.build_record())
        return path


def digest_bytes(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
