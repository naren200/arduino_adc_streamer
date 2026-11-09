"""Force calibration: trial analysis, the guided-sequence state machine and per-group records.

Widget-independent (the Qt panel only displays it). One *trial* is one continuous capture of
a whole sequence: unloaded baseline, placement of one known mass, hold, removal, unloaded
settle. The same accumulator runs through all of it; the explicit marks (placement, hand
clear, removal, hand clear) decide the windows, never an automatic reset.

Records are ``captest-force-calibration-1``: one file per (board instance, layout hash, group,
capture signature, mounting), so saving PZT1 never overwrites PZT3, the other array's PZT1,
or the same package at another sampling rate.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import force_model as fm
from .force_export import ForceExportError, ForceInputs, sanitize

SCHEMA = "captest-force-calibration-1"
RANGE_POLICY = "analysis_window_v2"
LEGACY_RANGE_POLICY = "full_capture_v1"
MASSES_G = (20, 50, 100, 200)
DEFAULT_REPEATS = 5
DEFAULT_HOLD_S = 10.0
DEFAULT_WINDOW_S = 0.5          # stable pre/post window length
DEFAULT_GUARD_S = 0.3           # keep windows this far from the mechanical transient

Channel = Tuple[int, ...]


class CalibrationError(ValueError):
    pass


def calibration_masses(values: Sequence[float]) -> Tuple[float, ...]:
    """Positive, distinct masses in ascending order, so range escalation stays meaningful."""
    try:
        masses = tuple(float(m) for m in values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CalibrationError("Enter masses in grams, for example 20, 50 or 12.5, 25.") from exc
    if not masses or any(not math.isfinite(m) or m <= 0 for m in masses):
        raise CalibrationError("Enter at least one finite mass greater than zero, in grams.")
    if len(set(masses)) != len(masses):
        raise CalibrationError("Each mass must appear once; use Repeats per mass for repeated trials.")
    return tuple(sorted(masses))


def parse_masses(text: str) -> Tuple[float, ...]:
    """Comma-, semicolon- or whitespace-separated gram values from the guided tab."""
    return calibration_masses(re.split(r"[,;\s]+", text.strip()) if text.strip() else ())


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------- capture data
@dataclass
class Capture:
    """Continuous samples of the five analysed routes (all visits, in order)."""

    samples: Dict[Channel, dict]                  # ref -> {t, volts, tc, valid(optional)}
    span_volts: float = 2.5
    source: str = ""                              # capture id / raw file this came from

    def routes(self) -> List[Channel]:
        return list(self.samples)

    def duration(self) -> float:
        ends = [float(d["t"][-1]) for d in self.samples.values() if len(d["t"])]
        starts = [float(d["t"][0]) for d in self.samples.values() if len(d["t"])]
        return max(ends) - min(starts) if ends else 0.0

    def save(self, path) -> str:
        """Portable companion file (.npz); returns its SHA-256."""
        arrays = {}
        for ref, d in self.samples.items():
            tag = "_".join(str(int(x)) for x in ref)
            arrays["t_" + tag] = np.asarray(d["t"], dtype=np.float64)
            arrays["v_" + tag] = np.asarray(d["volts"], dtype=np.float64)
            arrays["tc_" + tag] = np.asarray(d["tc"], dtype=np.float64)
            valid = d.get("valid")
            arrays["ok_" + tag] = (np.ones(len(d["t"]), dtype=bool) if valid is None
                                   else np.asarray(valid, dtype=bool))
            for name in ("generation", "vmid_volts"):
                if name in d:
                    arrays[name + "_" + tag] = np.asarray(d[name])
        arrays["meta"] = np.array(json.dumps({"span_volts": self.span_volts, "source": self.source,
                                              "routes": [list(r) for r in self.samples]}))
        path = Path(path)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        os.close(fd)
        try:
            with open(tmp, "wb") as handle:
                np.savez_compressed(handle, **arrays)
            # Never replace another trial's evidence, even if the caller reused a name.
            os.link(tmp, path)
            os.unlink(tmp)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @classmethod
    def load(cls, path, sha256: Optional[str] = None) -> "Capture":
        path = Path(path)
        if sha256 is not None and hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
            raise CalibrationError("capture file %s does not match its recorded hash" % path.name)
        with np.load(path, allow_pickle=False) as z:
            meta = json.loads(str(z["meta"]))
            samples = {}
            for ref in meta["routes"]:
                tag = "_".join(str(int(x)) for x in ref)
                samples[tuple(ref)] = {"t": z["t_" + tag], "volts": z["v_" + tag],
                                       "tc": z["tc_" + tag], "valid": z["ok_" + tag]}
                for name in ("generation", "vmid_volts"):
                    if name + "_" + tag in z:
                        samples[tuple(ref)][name] = z[name + "_" + tag]
        return cls(samples, meta["span_volts"], meta["source"])


@dataclass
class Marks:
    """Explicit user/automatic marks of one trial (capture-clock seconds)."""

    baseline: Tuple[float, float]
    place: float                   # hand arrives / placement action
    place_clear: float             # hand clear after placement
    remove: float
    remove_clear: float
    post_place: Optional[Tuple[float, float]] = None      # user-confirmed windows (optional)
    pre_remove: Optional[Tuple[float, float]] = None
    post_remove: Optional[Tuple[float, float]] = None

    def __post_init__(self):
        times = (*self.baseline, self.place, self.place_clear, self.remove, self.remove_clear)
        if not all(math.isfinite(t) and t >= 0 for t in times) or not (
                self.baseline[0] < self.baseline[1] <= self.place <= self.place_clear < self.remove <= self.remove_clear):
            raise CalibrationError("baseline and event marks must be finite, nonnegative and ordered")
        for name, earliest, latest in (("post_place", self.place_clear, self.remove),
                                       ("pre_remove", self.place_clear, self.remove),
                                       ("post_remove", self.remove_clear, float("inf"))):
            window = getattr(self, name)
            if window is not None and not (all(math.isfinite(t) for t in window)
                                          and earliest <= window[0] < window[1] <= latest):
                raise CalibrationError(name + " must lie in its labelled constant-force interval")


def sampling_rates(samples: Dict[Channel, dict], last: Optional[int] = None) -> Dict[str, dict]:
    """Measured sampling rate of every channel: how often the same sensor is visited.

    Each sweep visits every route once, so the revisit interval is the sampling period of the
    channel (it includes any added lag). ``last`` limits the estimate to the newest samples
    (live display). Keys are "adc:input"."""
    out: Dict[str, dict] = {}
    for ref, d in samples.items():
        t = np.asarray(d["t"], dtype=float)
        t = t[-last:] if last else t
        if len(t) < 3:
            continue
        dt = np.diff(t)
        median = float(np.median(dt))
        out["%d:%d" % ref] = {
            "samples": int(len(t)), "median_interval_us": median * 1e6,
            "rate_hz": 1.0 / median if median > 0 else float("nan"),
            "mean_rate_hz": float((len(t) - 1) / (t[-1] - t[0])) if t[-1] > t[0] else float("nan"),
            "interval_p1_us": float(np.percentile(dt, 1)) * 1e6,
            "interval_p99_us": float(np.percentile(dt, 99)) * 1e6,
            "interval_max_us": float(dt.max()) * 1e6}
    return out


def sampling_summary(trials: Sequence["TrialResult"]) -> Dict[str, dict]:
    """Per channel: median sampling rate over the trials of a calibration."""
    rates: Dict[str, List[float]] = {}
    for trial in trials:
        for key, info in (getattr(trial, "sampling", None) or {}).items():
            rates.setdefault(key, []).append(info["rate_hz"])
    return {key: {"rate_hz": float(np.median(v)), "min_rate_hz": float(min(v)),
                  "max_rate_hz": float(max(v)), "trials": len(v)} for key, v in rates.items()}


@dataclass
class TrialResult:
    mass_g: float
    repeat: int
    accepted: bool
    reasons: List[str]
    flags: List[str]
    events: List[fm.CalEvent]
    windows: Dict[str, Tuple[float, float]]
    bias: Dict[str, dict]
    hold_drift_c_per_s: Optional[float]
    load_removal_ratio: Optional[float]
    ranges: Dict[str, dict]
    event_fits: Dict[str, dict]
    capture_file: str = ""
    capture_sha256: str = ""
    supports_mass: bool = True       # False: stop escalation (clipping / outside range)
    marks: Optional["Marks"] = None  # the marks and windows analysed (re-analysis, review)
    companion: str = ""              # portable .npz of the analysed samples (+ its hash)
    companion_sha256: str = ""
    sampling: Dict[str, dict] = field(default_factory=dict)   # measured rate per channel
    analysis_revision: int = 1
    model_revision: str = ""
    sidecar: str = ""
    reference_kind: str = "known_mass"

    def to_json(self) -> dict:
        return sanitize({
            "mass_g": self.mass_g, "repeat": self.repeat, "accepted": self.accepted,
            "reasons": self.reasons, "flags": self.flags, "windows_s": self.windows,
            "bias": self.bias, "hold_drift_c_per_s": self.hold_drift_c_per_s,
            "load_removal_ratio": self.load_removal_ratio, "ranges": self.ranges,
            "event_fits": self.event_fits,
            "events": [{"id": e.event_id, "mass_g": e.mass_g, "direction": e.direction,
                        "repeat": e.repeat, "delta_q_c": e.delta_q, "drift_c_per_s": e.drift_rate,
                        "accepted": e.accepted, "position": e.position,
                        "plain_delta_q_c": e.plain_delta_q} for e in self.events],
            "capture_file": self.capture_file, "capture_sha256": self.capture_sha256,
            "companion": self.companion, "companion_sha256": self.companion_sha256,
            "sampling": self.sampling,
            "analysis_revision": self.analysis_revision, "model_revision": self.model_revision,
            "sidecar": self.sidecar, "reference_kind": self.reference_kind,
            "marks_s": None if self.marks is None else {
                "baseline": list(self.marks.baseline), "place": self.marks.place,
                "place_clear": self.marks.place_clear, "remove": self.marks.remove,
                "remove_clear": self.marks.remove_clear,
                "post_place": self.marks.post_place, "pre_remove": self.marks.pre_remove,
                "post_remove": self.marks.post_remove},
            "supports_mass": self.supports_mass})


def detect_stable_window(t: np.ndarray, q: np.ndarray, start: float, length: float,
                         noise_scale: float, search_s: float = 5.0, block_s: float = 0.05,
                         k: float = 6.0) -> Optional[Tuple[float, float]]:
    """First window of ``length`` s at or after ``start`` whose block-to-block changes of the
    accumulated charge stay within ``k`` times the baseline scale (``noise_scale``: RMS of the
    baseline block-to-block differences). None when the signal never settles."""
    t = np.asarray(t)
    q = np.asarray(q)
    if len(t) < 16 or not noise_scale > 0:
        return None
    edges = np.arange(start, min(start + search_s, t[-1]) - block_s, block_s)
    if len(edges) < 2:
        return None
    means = []
    for a in edges:
        sel = (t >= a) & (t < a + block_s)
        means.append(float(q[sel].mean()) if sel.any() else float("nan"))
    means = np.array(means)
    steps = np.abs(np.diff(means))
    need = max(int(round(length / block_s)), 2)
    for i in range(0, len(edges) - need):
        window = steps[i:i + need - 1]
        if np.all(np.isfinite(window)) and np.all(window <= k * noise_scale):
            return (float(edges[i]), float(edges[i] + need * block_s))
    return None


def _block_diff_noise(t, q, window: Tuple[float, float], block_s: float = 0.05) -> float:
    """RMS of the block-to-block changes of the accumulated charge in the baseline window: it
    contains both the noise and any residual baseline drift, so a clean (noiseless) record
    still gets a positive scale."""
    t = np.asarray(t)
    sel = (t >= window[0]) & (t <= window[1])
    tw, qw = t[sel], np.asarray(q)[sel]
    if len(tw) < 16:
        return float("nan")
    edges = np.arange(window[0], window[1] - block_s, block_s)
    means = np.array([qw[(tw >= a) & (tw < a + block_s)].mean() for a in edges
                      if ((tw >= a) & (tw < a + block_s)).any()])
    if len(means) < 4:
        return float("nan")
    return float(np.sqrt(np.mean(np.diff(means) ** 2)))


def analyze_trial(capture: Capture, models: Dict[Channel, fm.RouteModel], marks: Marks,
                  mass_g: float, repeat: int, *, polarity: float = 1.0,
                  leakage: Optional[Dict[Channel, fm.LeakageModel]] = None,
                  window_s: float = DEFAULT_WINDOW_S, guard_s: float = DEFAULT_GUARD_S,
                  g: float = fm.STANDARD_GRAVITY, load_removal_tol: float = 0.25,
                  hold_drift_tol: float = 0.15, position: str = "",
                  fixed_bias: Optional[Dict[Channel, float]] = None, bias_source: str = "",
                  fixed_drift: Optional[Dict[Channel, float]] = None,
                  route_polarity: Optional[Dict[Channel, float]] = None,
                  range_policy: str = RANGE_POLICY) -> TrialResult:
    """Placement and removal events of one mass from one continuous capture.

    The raw residuals are bias-corrected with a per-route bias frozen on the unloaded baseline
    window. A trial is rejected for clipping, a voltage outside the fitted range, an unstable
    window, or windows with too few samples; rejected trials keep their reasons.
    """
    reasons: List[str] = []
    flags: List[str] = []
    ranges = {}
    if range_policy not in (RANGE_POLICY, LEGACY_RANGE_POLICY):
        raise CalibrationError("unknown range-check policy")
    range_start = marks.baseline[0] if range_policy == RANGE_POLICY else -float("inf")
    clipped = outside = low = False
    for ref, d in capture.samples.items():
        active, inputs = fm.range_window(d["t"], range_start)
        v = np.asarray(d["volts"])
        rep = fm.range_report(models[ref], v[inputs], span_volts=capture.span_volts)
        if range_policy == RANGE_POLICY:
            rep.update(range_policy=range_policy, analysis_start_s=range_start,
                       checked_sample_count=int(inputs.sum()),
                       capture_v_min=float(np.min(v)) if len(v) else None,
                       capture_v_max=float(np.max(v)) if len(v) else None)
        ranges["%d:%d" % ref] = rep
        clipped |= rep["clipped"]
        outside |= rep["outside_fitted_range"]
        low |= rep["extrapolated_low"]
        if np.any(~np.isfinite(v)) or np.any(~np.isfinite(d["tc"])) or np.any(np.asarray(d["tc"]) <= 0):
            reasons.append("INVALID_INPUT: non-finite voltage or invalid connected time")
        if "valid" in d and not np.all(d["valid"]):
            reasons.append("INVALID_CAPTURE: invalid sensor/reference sample")
        if np.any(np.diff(np.asarray(d["t"])) <= 0) or np.any(~np.isfinite(d["t"])):
            reasons.append("INVALID_TIME: non-finite or unordered timestamps")
        if rep["above_vd_count"]:
            flags.append("ABOVE_VD_EXTRAPOLATION")
        if rep["below_fit_count"]:
            flags.append("BELOW_FIT_EXTRAPOLATION")
        if not rep["coverage_known"]:
            flags.append("UNKNOWN_MODEL_DOMAIN")
        rep["above_vd_duration_s"] = float(np.sum(np.diff(d["t"]) * active[1:] *
                                                    (v[1:] > models[ref].v_d)))
        # Previous measured samples are also inputs to the nonlinear prediction.
        previous = v[:-1][active[1:]]
        rep["predictor_input_v_min"] = float(np.min(previous)) if len(previous) else None
        rep["predictor_input_v_max"] = float(np.max(previous)) if len(previous) else None
    if low:
        flags.append("EXTRAPOLATED_LOW_D")
    if clipped:
        reasons.append("CLIPPED: a channel reached an ADC code limit" if any(r["saturated"] for r in ranges.values())
                       else "ADC_HEADROOM_LIMIT: a channel entered the configured ADC headroom guard")
    if outside:
        reasons.append("OUTSIDE_FITTED_RANGE: voltage fell below the range the electrical fit covers (too far from VD toward 0 V)")
    supports = not (clipped or outside)
    if any(r.startswith(("INVALID_INPUT", "INVALID_CAPTURE", "INVALID_TIME")) for r in reasons):
        return TrialResult(mass_g, repeat, False, list(dict.fromkeys(reasons)), list(dict.fromkeys(flags)),
                           [], {}, {}, None, None, ranges, {}, supports_mass=False, marks=marks)

    leak = leakage or {}
    if fixed_drift is not None and fixed_bias is None:
        fixed_bias = {ref: 0. for ref in models}
    for ref, data in capture.samples.items():
        active, _ = fm.range_window(data["t"], range_start)
        before = leak.get(ref, fm.NO_LEAKAGE).apply(np.asarray(data["volts"])[:-1][active[1:]], models[ref].v_d)
        rep = ranges["%d:%d" % ref]
        if len(before):
            rep["predictor_input_v_min"] = float(np.min(before))
            rep["predictor_input_v_max"] = float(np.max(before))
            state_range = fm.range_report(models[ref], before, span_volts=capture.span_volts)
            rep["predictor_outside_fitted_range"] = state_range["outside_fitted_range"]
            if state_range["outside_fitted_range"] or state_range["above_vd_count"]:
                flags.append("PREDICTOR_EXTRAPOLATION")
    bias: Dict[str, dict] = {}
    bias_v: Dict[Channel, float] = {}
    try:
        for ref, d in capture.samples.items():
            e = fm.residuals(models[ref], d["volts"], d["tc"], d.get("valid"), d.get("generation"),
                             leak.get(ref, fm.NO_LEAKAGE))
            est = fm.estimate_bias(ref, d["t"], e, marks.baseline)
            bias["%d:%d" % tuple(ref)] = est.to_json()
            bias_v[ref] = est.bias_v
            if fixed_bias is not None:
                value = fixed_bias.get(ref)
                if value is None or not math.isfinite(value):
                    raise fm.ModelError("missing/invalid frozen unloaded correction for %s" % (ref,))
                row = bias["%d:%d" % tuple(ref)]
                row.update(baseline_measured_bias_v=est.bias_v, bias_v=value,
                           source="unloaded_drift", source_sha256=bias_source)
                bias_v[ref] = value
                expected = value
                if fixed_drift is not None:
                    dt = np.diff(d["t"])
                    expected += fixed_drift[ref] * float(np.median(dt)) / (polarity * (route_polarity or {}).get(ref, 1) * models[ref].c_total)
                    row["drift_rate_c_per_s"] = fixed_drift[ref]
                if abs(est.bias_v - expected) > max(5 * est.std_v / math.sqrt(est.n), capture.span_volts / 4096 / 20):
                    flags.append("UNLOADED_DRIFT_SHIFT")
    except fm.ModelError as exc:
        return TrialResult(mass_g, repeat, False, reasons + ["BASELINE: %s" % exc], flags, [], {},
                           bias, None, None, ranges, {}, supports_mass=supports,
                           sampling=sampling_rates(capture.samples), marks=marks)
    charge = fm.accumulate(models, capture.samples, bias_v, polarity, leak, fixed_drift, route_polarity)
    noise = _block_diff_noise(charge.t, charge.q_proxy, marks.baseline)
    # Digitised data cannot be quieter than a fraction of one LSB of charge: a floor keeps the
    # stability test meaningful on a noiseless (synthetic) record.
    lsb_charge = (capture.span_volts / float(1 << fm.ADC_BITS_7953)
                  * float(np.mean([m.c_total for m in models.values()])))
    noise = max(noise, 0.05 * lsb_charge) if math.isfinite(noise) else noise

    windows: Dict[str, Tuple[float, float]] = {}
    win = window_s
    windows["pre_place"] = (marks.place - guard_s - win, marks.place - guard_s)
    post = marks.post_place or detect_stable_window(
        charge.t, charge.q_proxy, marks.place_clear + guard_s, win, noise)
    if post is not None and post[1] >= marks.remove - guard_s:
        post = None  # never find a "loaded" window after the removal event
    windows["pre_remove"] = marks.pre_remove or (marks.remove - guard_s - win, marks.remove - guard_s)
    post_r = marks.post_remove or detect_stable_window(
        charge.t, charge.q_proxy, marks.remove_clear + guard_s, win, noise)
    if post is None:
        reasons.append("NO_STABLE_POST_PLACEMENT")
    else:
        windows["post_place"] = post
    if post_r is None:
        reasons.append("NO_STABLE_POST_REMOVAL")
    else:
        windows["post_remove"] = post_r
    events: List[fm.CalEvent] = []
    fits: Dict[str, dict] = {}
    if windows["pre_place"][0] < marks.baseline[0]:
        flags.append("PRE_WINDOW_BEFORE_BASELINE")
    steps = {}
    try:
        if post is not None:
            steps["place"] = fm.event_step(charge.t, charge.q_proxy, windows["pre_place"], post)
        if post_r is not None:
            steps["remove"] = fm.event_step(charge.t, charge.q_proxy, windows["pre_remove"], post_r)
    except fm.ModelError as exc:
        reasons.append("EVENT_WINDOW: %s" % exc)
    for name, sign in (("place", +1), ("remove", -1)):
        s = steps.get(name)
        if s is None:
            continue
        fits[name] = {"delta_q_c": s.delta_q, "drift_c_per_s": s.drift_rate,
                      "plain_delta_q_c": s.plain_delta_q, "rms_c": s.rms,
                      "n_pre": s.n_pre, "n_post": s.n_post}
        pre_window = windows["pre_place"] if name == "place" else windows["pre_remove"]
        post_window = post if name == "place" else post_r
        fits[name]["pre_only_delta_q_c"] = fm.event_step(
            charge.t, charge.q_proxy, pre_window, post_window, mode="pre").delta_q
        fits[name]["drift_charge_over_gap_c"] = s.drift_charge_over_gap
        fits[name]["estimator"] = "joint_step_plus_ramp"
        if abs(s.delta_q) > 0:
            if abs(fits[name]["pre_only_delta_q_c"] - s.delta_q) > load_removal_tol * abs(s.delta_q):
                flags.append("STEP_ESTIMATOR_DISAGREEMENT")
            if abs(s.drift_charge_over_gap) > hold_drift_tol * abs(s.delta_q):
                flags.append("LARGE_DRIFT_CORRECTION")
        events.append(fm.CalEvent("m%g_r%d_%s" % (mass_g, repeat, name), mass_g, sign, repeat,
                                  s.delta_q, s.drift_rate, True, position, s.plain_delta_q))
    hold_drift = ratio = None
    if "place" in steps and "remove" in steps:
        # Compare the post-placement and pre-removal holds: drift must not be hidden.
        qa = charge.q_proxy[(charge.t >= post[0]) & (charge.t <= post[1])].mean()
        qb_sel = (charge.t >= windows["pre_remove"][0]) & (charge.t <= windows["pre_remove"][1])
        qb = charge.q_proxy[qb_sel].mean()
        span = ((windows["pre_remove"][0] + windows["pre_remove"][1]) / 2
                - (post[0] + post[1]) / 2)
        if span > 0:
            hold_drift = float((qb - qa) / span)
        if steps["place"].delta_q != 0:
            ratio = float(-steps["remove"].delta_q / steps["place"].delta_q)
            if abs(ratio - 1.0) > load_removal_tol:
                flags.append("LOAD_REMOVAL_INCONSISTENT")
        step = abs(steps["place"].delta_q)
        if hold_drift is not None and step > 0 and abs(hold_drift * span) > hold_drift_tol * step:
            flags.append("HOLD_DRIFT")
    accepted = not reasons and len(events) == 2
    if not accepted:
        events = [fm.CalEvent(e.event_id, e.mass_g, e.direction, e.repeat, e.delta_q, e.drift_rate,
                              False, e.position, e.plain_delta_q) for e in events]
    return TrialResult(mass_g, repeat, accepted, reasons, list(dict.fromkeys(flags)), events, windows, bias,
                       hold_drift, ratio, ranges, fits, supports_mass=supports, marks=marks,
                       sampling=sampling_rates(capture.samples))


# ----------------------------------------------------------------------------- wizard
PREPARE = "prepare"
BASELINE = "baseline"
PLACE_ARMED = "place_armed"      # capture running, event armed: the placement prompt is shown
HOLD = "hold"
REMOVE_ARMED = "remove_armed"
UNLOADED = "unloaded"
REVIEW = "review"
FIT = "fit"
DONE = "done"

PENDING, DONE_STEP, FAILED_STEP, SKIPPED = "pending", "done", "failed", "skipped"


@dataclass
class Step:
    mass_g: float
    repeat: int
    status: str = PENDING
    reason: str = ""
    attempts: int = 0


class Wizard:
    """Stateful guide driven by explicit actions (no hidden transitions).

    The prompt for a manual action is only available once its event is armed, so the capture
    is already running when the user acts. Failed trials stay recorded; a mass can be retried
    or skipped, and a clipped/out-of-range mass stops escalation to heavier masses.
    """

    def __init__(self, masses: Sequence[float] = MASSES_G, repeats: int = DEFAULT_REPEATS,
                 hold_s: float = DEFAULT_HOLD_S):
        if repeats < 1:
            raise CalibrationError("at least one repeat per mass")
        if not 10.0 <= hold_s <= 30.0:
            raise CalibrationError("hold time must be 10-30 s")
        self.masses = calibration_masses(masses)
        self.hold_s = hold_s
        self.steps = [Step(m, r) for m in self.masses for r in range(1, repeats + 1)]
        self.phase = PREPARE
        self.index = 0
        self.supported_max_g: Optional[float] = None
        self.stopped_at_g: Optional[float] = None
        self.armed = False
        self.marks: Dict[str, float] = {}
        self.log: List[str] = []

    # ------------------------------------------------------------------- queries
    @property
    def current(self) -> Optional[Step]:
        pending = [s for s in self.steps if s.status == PENDING]
        return pending[0] if pending else None

    def prompt(self) -> str:
        step = self.current
        m = "%g g" % step.mass_g if step else ""
        if self.phase == PREPARE:
            return ("Select the Array and PZT package. Keep the load-spreading plate in place "
                    "throughout; all added load must pass through the package.")
        if self.phase == BASELINE:
            return "Capturing the unloaded baseline: keep the sensor undisturbed."
        if self.phase == PLACE_ARMED:
            return ("Place %s now, then take your hand away and press 'Hand clear'." % m
                    if self.armed else "Arming capture...")
        if self.phase == HOLD:
            return "Hold %s for %g s without touching it." % (m, self.hold_s)
        if self.phase == REMOVE_ARMED:
            return ("Remove %s now, then take your hand away and press 'Hand clear'." % m
                    if self.armed else "Arming capture...")
        if self.phase == UNLOADED:
            return "Capturing the unloaded settle: keep the sensor undisturbed."
        if self.phase == REVIEW:
            return "Review the windows of this trial; accept, retry or skip this mass."
        if self.phase == FIT:
            if not any(st.status == DONE_STEP for st in self.steps):
                return ("No trial was accepted. Press 'Retry failed' to repeat the first mass (use a "
                        "lighter load if the reason is OUTSIDE_FITTED_RANGE) or 'Reset trials'.")
            return "All planned trials are finished: fit and validate."
        return "Finished."

    # ------------------------------------------------------------------- actions
    def _need(self, phase: str) -> None:
        if self.phase != phase:
            raise CalibrationError("not allowed in phase %s (expected %s)" % (self.phase, phase))

    def begin(self, t: float) -> None:
        """Start the continuous capture and the unloaded baseline."""
        self._need(PREPARE)
        if self.current is None:
            raise CalibrationError("nothing to do: every step is finished or skipped")
        self.marks = {"capture_start": t, "baseline_start": t}
        self.phase = BASELINE

    def baseline_done(self, t: float) -> None:
        self._need(BASELINE)
        self.marks["baseline_end"] = t
        self.phase, self.armed = PLACE_ARMED, True       # armed before the prompt is shown

    def mark_placement(self, t: float) -> None:
        self._need(PLACE_ARMED)
        if not self.armed:
            raise CalibrationError("event not armed")
        self.marks["place"] = t

    def hand_clear_placement(self, t: float) -> None:
        self._need(PLACE_ARMED)
        if "place" not in self.marks:
            raise CalibrationError("mark the placement first")
        self.marks["place_clear"] = t
        self.phase, self.armed = HOLD, False

    def hold_done(self, t: float) -> None:
        self._need(HOLD)
        if t - self.marks["place_clear"] < self.hold_s - 1e-9:
            raise CalibrationError("hold lasted %.1f s of %.1f s" % (t - self.marks["place_clear"],
                                                                      self.hold_s))
        self.phase, self.armed = REMOVE_ARMED, True

    def mark_removal(self, t: float) -> None:
        self._need(REMOVE_ARMED)
        self.marks["remove"] = t

    def hand_clear_removal(self, t: float) -> None:
        self._need(REMOVE_ARMED)
        if "remove" not in self.marks:
            raise CalibrationError("mark the removal first")
        self.marks["remove_clear"] = t
        self.phase, self.armed = UNLOADED, False

    def unloaded_done(self, t: float) -> None:
        self._need(UNLOADED)
        self.marks["capture_end"] = t
        self.phase = REVIEW

    def trial_marks(self) -> Marks:
        m = self.marks
        return Marks((m["baseline_start"], m["baseline_end"]), m["place"], m["place_clear"],
                     m["remove"], m["remove_clear"])

    def finish_trial(self, result: TrialResult) -> None:
        """Record the analysed trial; accepted ones complete their step."""
        self._need(REVIEW)
        step = self.current
        step.attempts += 1
        if result.accepted:
            step.status = DONE_STEP
        else:
            step.status, step.reason = FAILED_STEP, "; ".join(result.reasons)
            self.log.append("%g g repeat %d failed: %s" % (step.mass_g, step.repeat, step.reason))
        if result.accepted and (self.supported_max_g is None or step.mass_g > self.supported_max_g):
            self.supported_max_g = step.mass_g
        if result.accepted and self.stopped_at_g is not None and step.mass_g == self.stopped_at_g:
            # A successful retry restores only steps skipped by this escalation failure.
            marker = "escalation stopped at %g g (clipping/range)" % self.stopped_at_g
            for pending in self.steps:
                if pending.status == SKIPPED and pending.reason == marker:
                    pending.status, pending.reason = PENDING, ""
            self.stopped_at_g = None
        if not result.supports_mass:
            self._stop_escalation(step.mass_g)
        self._next()

    def abort_trial(self, result: TrialResult) -> None:
        """Record a trial that failed while running (capture problem) and move on."""
        if self.phase in (PREPARE, FIT, DONE):
            raise CalibrationError("no trial is running")
        self.phase = REVIEW
        self.finish_trial(result)

    def retry(self) -> None:
        """Retry the most recent failed step (it becomes pending again)."""
        failed = [s for s in self.steps if s.status == FAILED_STEP]
        if not failed:
            raise CalibrationError("no failed step to retry")
        if self.phase not in (PREPARE, FIT, DONE):
            raise CalibrationError("finish or cancel the running trial first")
        last = failed[-1]
        last.status, last.reason = PENDING, ""
        self.phase = PREPARE

    def skip_mass(self, mass_g: float) -> None:
        """Skip the remaining steps of a mass; validation then does not cover it."""
        n = 0
        for s in self.steps:
            if s.mass_g == mass_g and s.status in (PENDING, FAILED_STEP):
                s.status, s.reason = SKIPPED, "skipped by the user"
                n += 1
        if not n:
            raise CalibrationError("nothing to skip for %g g" % mass_g)
        if self.phase in (PREPARE, REVIEW, FIT, DONE):
            self._next()

    def cancel_trial(self) -> None:
        """Abort the running trial (nothing recorded); the step stays pending."""
        if self.phase in (PREPARE, FIT, DONE):
            return
        self.phase, self.armed, self.marks = PREPARE, False, {}

    def _stop_escalation(self, mass_g: float) -> None:
        self.stopped_at_g = mass_g
        for s in self.steps:
            if s.mass_g >= mass_g and s.status == PENDING:
                s.status, s.reason = SKIPPED, "escalation stopped at %g g (clipping/range)" % mass_g

    def _next(self) -> None:
        self.armed, self.marks = False, {}
        self.phase = FIT if self.current is None else PREPARE

    def covered_masses(self) -> List[float]:
        return sorted({s.mass_g for s in self.steps if s.status == DONE_STEP})

    def skipped_masses(self) -> List[float]:
        return sorted({s.mass_g for s in self.steps if s.status == SKIPPED})


# ------------------------------------------------------------------------- records
@dataclass
class CalibrationKey:
    board_instance: str
    layout_hash: str
    group_id: str
    capture_signature: str
    mounting: str = ""

    def digest(self) -> str:
        text = "|".join([self.board_instance, self.layout_hash, self.group_id,
                         self.capture_signature, self.mounting])
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

    def filename(self) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.board_instance) or "board"
        return "force_cal_%s_%s_%s.json" % (safe, self.group_id, self.digest())

    def to_json(self) -> dict:
        return {"board_instance": self.board_instance, "layout_hash": self.layout_hash,
                "group_id": self.group_id, "capture_signature": self.capture_signature,
                "mounting": self.mounting}


def build_record(key: CalibrationKey, *, array: int, pzt: str, d33_initial: float,
                 fit: Optional[fm.D33Fit], validation: Optional[fm.Validation],
                 trials: Sequence[TrialResult], wizard: Optional[Wizard],
                 polarity: float, g: float, manifest_sha256: str,
                 leakage: Optional[fm.LeakageFit] = None, simulated: bool = False,
                 mounting_notes: str = "", schedule: Optional[dict] = None,
                 previous: Optional[dict] = None, limits: Optional[dict] = None) -> dict:
    """Assemble a record; raises when the data does not support a calibrated sensitivity."""
    if not (math.isfinite(d33_initial) and d33_initial != 0):
        raise CalibrationError("initial d33_eff must be finite and nonzero")
    accepted = [t for t in trials if t.accepted]
    blocking = {"HOLD_DRIFT", "LOAD_REMOVAL_INCONSISTENT", "PRE_WINDOW_BEFORE_BASELINE",
                "STEP_ESTIMATOR_DISAGREEMENT", "LARGE_DRIFT_CORRECTION", "UNKNOWN_MODEL_DOMAIN",
                "UNLOADED_DRIFT_SHIFT"}
    quality_reasons = sorted({flag for t in accepted for flag in t.flags if flag in blocking
                              or flag.startswith(("VD_SHIFT", "VD_NOISY"))})
    if fit and fit.flags:
        quality_reasons.extend(fit.flags)
    if not validation or validation.passed is not True:
        quality_reasons.append("independent validation with an explicit error limit has not passed")
    # Preserve the format name for raw compatibility; schema revision 2 has honest quality states.
    status = "validated" if fit is not None and accepted and not quality_reasons else (
        "fitted_provisional" if fit is not None and accepted else "not_fitted")
    leak_status = (leakage.status if leakage is not None else "not_evaluated")
    revision = (previous["revision"] + 1) if previous else 1
    masses = sorted({t.mass_g for t in accepted})
    record = {
        "format": SCHEMA, "schema_version": 2, "charge_conversion_model": fm.CHARGE_MODEL,
        "key": key.to_json(), "group": {"array": array, "pzt": pzt, "group_id": key.group_id},
        "revision": revision, "created": previous["created"] if previous else _now(),
        "updated": _now(), "origin": "simulated" if simulated else "hardware",
        "status": status, "quality_reasons": quality_reasons, "polarity": polarity, "g_m_per_s2": g,
        "d33_eff_initial_c_per_n": d33_initial,
        "d33_eff_fitted_c_per_n": fit.d33_eff if fit else None,
        "intercept_c": fit.intercept if fit else None,
        "fit": sanitize(_fit_json(fit)) if fit else None,
        "validation": sanitize(_validation_json(validation)) if validation else None,
        "leakage": sanitize(leakage.to_json()) if leakage else {"status": "not_evaluated"},
        "leakage_status": leak_status,
        "supported_range": {"masses_g": masses,
                            "max_mass_g": max(masses) if masses else None,
                            "force_n": [min(masses) * 1e-3 * g, max(masses) * 1e-3 * g] if masses else None,
                            "skipped_masses_g": wizard.skipped_masses() if wizard else [],
                            "stopped_escalation_at_g": wizard.stopped_at_g if wizard else None},
        "limits": limits or {}, "schedule": schedule or {}, "mounting_notes": mounting_notes,
        "sampling_per_channel": sanitize(sampling_summary(accepted or trials)),
        "electrical_manifest_sha256": manifest_sha256,
        "trials": [t.to_json() for t in trials],
        "steps": ([{"mass_g": s.mass_g, "repeat": s.repeat, "status": s.status,
                    "reason": s.reason, "attempts": s.attempts} for s in wizard.steps]
                  if wizard else []),
    }
    return record


def _fit_json(fit: fm.D33Fit) -> dict:
    return {"d33_eff_c_per_n": fit.d33_eff, "d33_eff_pc_per_n": fit.d33_pc_per_n,
            "intercept_c": fit.intercept, "r2": fit.r2, "n": fit.n,
            "rms_force_error_n": fit.rms_force_error_n, "max_force_error_n": fit.max_force_error_n,
            "placement_d33_c_per_n": fit.placement_d33, "removal_d33_c_per_n": fit.removal_d33,
            "repeat_spread": fit.repeat_spread, "flags": list(fit.flags),
            "force_errors_n": list(fit.force_errors_n),
            "regression_force_errors_n": list(fit.regression_force_errors_n),
            "error_estimator": "plain endpoint charge / sensitivity, no event-intercept subtraction"}


def _validation_json(v: fm.Validation) -> dict:
    return {"train_repeats": list(v.train_repeats), "test_repeats": list(v.test_repeats),
            "d33_eff_c_per_n": v.fit.d33_eff, "rms_force_error_n": v.rms_force_error_n,
            "max_force_error_n": v.max_force_error_n, "force_errors_n": list(v.force_errors_n),
            "passed": v.passed, "limit_n": v.limit_n}


class CalibrationStore:
    """Directory of per-group record files (atomic writes, strict JSON, review on import)."""

    def __init__(self, directory):
        self.directory = Path(directory)

    def path_for(self, key: CalibrationKey) -> Path:
        return self.directory / key.filename()

    def load(self, key: CalibrationKey) -> Optional[dict]:
        path = self.path_for(key)
        return read_record(path) if path.exists() else None

    def save(self, record: dict) -> Path:
        key = CalibrationKey(**record["key"])
        path = self.path_for(key)
        existing = self.load(key)
        if existing is not None and record["revision"] <= existing["revision"]:
            raise CalibrationError("a newer or equal revision (%d) is already saved for %s"
                                   % (existing["revision"], key.group_id))
        if existing is not None:
            history = self.directory / "revisions" / (path.stem + "_r%04d.json" % existing["revision"])
            if not history.exists():
                write_record(existing, history, exclusive=True)
        write_record(record, path)
        return path

    def list(self) -> List[dict]:
        out = []
        for p in sorted(self.directory.glob("force_cal_*.json")) if self.directory.exists() else []:
            try:
                out.append(read_record(p))
            except CalibrationError:
                continue
        return out


def write_record(record: dict, path, *, exclusive=False) -> Path:
    path = Path(path)
    body = sanitize(record)
    body.pop("content_sha256", None)
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    body["content_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(body, indent=1, allow_nan=False))
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.link(tmp, path)
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_record(path) -> dict:
    def refuse(constant):
        raise CalibrationError("file contains %s: not strict JSON" % constant)

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=refuse)
    except OSError as exc:
        raise CalibrationError("cannot read %s: %s" % (path, exc)) from exc
    except ValueError as exc:
        if isinstance(exc, CalibrationError):
            raise
        raise CalibrationError("%s is not valid JSON: %s" % (path, exc)) from exc
    if not isinstance(data, dict) or data.get("format") != SCHEMA:
        raise CalibrationError("not a %s file" % SCHEMA)
    if data.get("schema_version", 1) not in (1, 2):
        raise CalibrationError("unsupported calibration schema version")
    stored = data.pop("content_sha256", None)
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if stored != hashlib.sha256(text.encode("utf-8")).hexdigest():
        raise CalibrationError("content hash mismatch: the record was modified or damaged")
    if data.get("charge_conversion_model") != fm.CHARGE_MODEL:
        raise CalibrationError("charge conversion model %r is not supported by this version "
                               "(recalibrate)" % data.get("charge_conversion_model"))
    data["content_sha256"] = stored
    return data


def compatibility(record: dict, *, board_instance: str, layout_hash: str, capture_signature: str,
                  manifest_sha256: Optional[str] = None) -> List[str]:
    """Why a saved record may not be applied as-is (empty = applicable). Stale or unsupported
    records are shown for review and never applied silently."""
    key = record["key"]
    problems = []
    if key["board_instance"] != board_instance:
        problems.append("board instance %r differs from %r" % (key["board_instance"], board_instance))
    if key["layout_hash"] != layout_hash:
        problems.append("sensor-library layout changed since this calibration")
    if key["capture_signature"] != capture_signature:
        problems.append("capture schedule differs from the calibrated one")
    if manifest_sha256 is not None and record["electrical_manifest_sha256"] != manifest_sha256:
        problems.append("electrical inputs were re-measured since this calibration")
    if record["status"] != "validated" or record.get("schema_version", 1) < 2:
        problems.append("record is not independently validated (legacy calibrations are provisional)")
    return problems
