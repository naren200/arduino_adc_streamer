"""Widget-independent force-calibration math (live updates and replay share it).

Units are SI inside: volts, farads, seconds, coulombs, newtons. UI/exports may show pF,
pC/N, us and grams.

Model (capacitance-proxy mode, ``charge_conversion_model = capacitance_proxy_v1``)::

    d_prev = VD - V_previous            (previous sample of the same route)
    E      = exp(-Tc / (Rb * Ctotal))
    V_pred = VD - E * ((1 - c0 - c1*d_prev) * d_prev + qv)
    e      = V_measured - V_pred
    dQ_proxy = Ctotal * e               (a proxy: not established as generated charge)
    Q_group(t) = sum over the five routes and visits of polarity * dQ_proxy

Nothing here resets, decays, thresholds or re-zeroes the accumulator on its own. A bias
estimate is a separate, explicit, recorded stage fitted on a labelled quiet window and then
frozen. Increments are summed directly: no extra multiplication by sample time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

CHARGE_MODEL = "capacitance_proxy_v1"
STANDARD_GRAVITY = 9.80665
ADC_BITS_7953 = 12
G_DEFAULT = STANDARD_GRAVITY

Channel = Tuple[int, ...]


class ModelError(ValueError):
    pass


def code_to_volts(code, span_volts: float, bits: int = ADC_BITS_7953):
    """ADC code -> volts, span/2**bits per code (span/4096 for the 12-bit 7953). Do not
    apply it to data that is already in volts."""
    return np.asarray(code, dtype=np.float64) * (float(span_volts) / float(1 << bits))


# --------------------------------------------------------------------------- route model
@dataclass(frozen=True)
class RouteModel:
    """Electrical inputs of one sensor route (all SI)."""

    ref: Channel
    r_bias: float            # ohm
    c_total: float           # F, loaded (not Cs, C_net or a board blank)
    v_d: float               # V, measured Vmid reference of this ADC
    c0: float = 0.0          # Cshare(VD) / Ctotal
    c1: float = 0.0          # 1/V
    qv: float = 0.0          # V
    d_min: float = float("nan")      # fitted distance-below-VD range (V); NaN = unknown
    d_max: float = float("nan")
    signature: str = ""      # acquisition signature of the fit (see signature_of)

    def __post_init__(self) -> None:
        for name in ("r_bias", "c_total"):
            value = getattr(self, name)
            if not (math.isfinite(value) and value > 0):
                raise ModelError("%s of route %s must be finite and positive" % (name, self.ref))
        for name in ("v_d", "c0", "c1", "qv"):
            if not math.isfinite(getattr(self, name)):
                raise ModelError("%s of route %s must be finite" % (name, self.ref))

    @property
    def tau(self) -> float:
        return self.r_bias * self.c_total

    @classmethod
    def from_summary(cls, ref: Channel, *, r_bias: float, c_total: float, v_d: float,
                     c_share_ratio: float, c_share_1v_pf: float, q_cycle_pc: float,
                     **extra) -> "RouteModel":
        """Recover c0, c1, qv from exported summary fields (legacy/row-only data)::

            c0 = Cshare_at_VD / Ctotal
            c1 = (Cshare_1V_below_VD - Cshare_at_VD) / (Ctotal * 1 V)
            qv = q_cycle_pc * 1e-12 / Ctotal
        """
        c_share_vd = c_share_ratio * c_total
        c0 = c_share_vd / c_total
        c1 = (c_share_1v_pf * 1e-12 - c_share_vd) / (c_total * 1.0)
        qv = q_cycle_pc * 1e-12 / c_total
        return cls(ref, r_bias, c_total, v_d, c0, c1, qv, **extra)


def signature_of(**parts) -> str:
    """Canonical text of the acquisition signature (sorted key=value pairs)."""
    return ";".join("%s=%s" % (k, parts[k]) for k in sorted(parts))


def route_signs_from_json(values):
    if not values:
        return None
    signs = {tuple(map(int, key.split(":"))): value for key, value in values.items()}
    if any(v not in (-1, 1) for v in signs.values()):
        raise ModelError("relative channel polarity must be +1 or -1")
    return signs


# ----------------------------------------------------------------------- leakage model
@dataclass(frozen=True)
class LeakageModel:
    """Effective off-state transition ``V_before = Vleak + (V_prev - Vleak) * lam``.

    ``lam = exp(-Toff / (Roff*Coff))`` for a verified linear leakage path (``from_physical``);
    otherwise it is a clearly labelled *effective observation correction* valid only over the
    tested voltage/timing domain. ``lam == 1`` means no correction.
    """

    lam: float = 1.0
    v_leak: Optional[float] = None     # None: the route's VD
    kind: str = "none"                 # none | effective | physical
    note: str = ""

    def __post_init__(self) -> None:
        if not (math.isfinite(self.lam) and 0.0 < self.lam <= 1.0):
            raise ModelError("leakage lam must be in (0, 1]")

    @classmethod
    def from_physical(cls, t_off: float, r_off: float, c_off: float,
                      v_leak: Optional[float] = None) -> "LeakageModel":
        if not all(math.isfinite(x) and x > 0 for x in (t_off, r_off, c_off)):
            raise ModelError("Toff, Roff and Coff must be finite and positive")
        return cls(math.exp(-t_off / (r_off * c_off)), v_leak, "physical",
                   "Coff must be independently known and the read map verified")

    @property
    def active(self) -> bool:
        return self.lam < 1.0

    def apply(self, v_prev, v_d: float):
        """Voltage of the node just before the next read, given the previous reading."""
        if not self.active:
            return np.asarray(v_prev, dtype=np.float64)
        v_leak = v_d if self.v_leak is None else self.v_leak
        return v_leak + (np.asarray(v_prev, dtype=np.float64) - v_leak) * self.lam


NO_LEAKAGE = LeakageModel()


# ------------------------------------------------------------------------ residuals
def predict(model: RouteModel, v_prev, tc, leakage: LeakageModel = NO_LEAKAGE):
    """Predicted voltage of the next visit given the previous reading ``v_prev``
    (vectorised); ``tc`` is the connected time of that next visit (s)."""
    d_prev = model.v_d - leakage.apply(v_prev, model.v_d)
    decay = np.exp(-np.asarray(tc, dtype=np.float64) / model.tau)
    return model.v_d - decay * ((1.0 - model.c0 - model.c1 * d_prev) * d_prev + model.qv)


def residuals(model: RouteModel, volts, tc, valid=None, generation=None,
              leakage: LeakageModel = NO_LEAKAGE) -> np.ndarray:
    """Raw residual e = V_measured - V_pred per visit of one route (volts).

    The first valid sample of each route and generation has no predecessor: its residual is
    NaN and it contributes no dQ. A sample after an invalid one starts a new generation.
    ``generation`` (optional int array) forces a new generation where its value changes.
    """
    v = np.asarray(volts, dtype=np.float64)
    tc = np.asarray(tc, dtype=np.float64)
    if v.shape != tc.shape:
        raise ModelError("volts and tc must have the same length")
    ok = np.ones(v.shape, dtype=bool) if valid is None else np.asarray(valid, dtype=bool)
    ok = ok & np.isfinite(v) & np.isfinite(tc) & (tc > 0)
    e = np.full(v.shape, np.nan)
    if len(v) < 2:
        return e
    linked = ok[1:] & ok[:-1]
    if generation is not None:
        gen = np.asarray(generation)
        linked &= gen[1:] == gen[:-1]
    pred = predict(model, v[:-1], tc[1:], leakage)
    if np.any(linked & ~np.isfinite(pred)):
        raise ModelError("non-finite circuit prediction on a valid sample transition")
    e[1:] = np.where(linked, v[1:] - pred, np.nan)
    return e


def apply_bias(e: np.ndarray, bias: float) -> np.ndarray:
    """Corrected residual: the raw one is kept by the caller, the bias is explicit."""
    return np.asarray(e, dtype=np.float64) - float(bias)


@dataclass(frozen=True)
class BiasEstimate:
    ref: Channel
    bias_v: float            # mean raw residual over the window (V)
    std_v: float
    n: int
    window: Tuple[float, float]       # (t_start, t_end) of the labelled quiet window
    frozen: bool = True

    def to_json(self) -> dict:
        return {"ref": list(self.ref), "bias_v": self.bias_v, "std_v": self.std_v, "n": self.n,
                "window_s": list(self.window), "frozen": self.frozen}


def estimate_bias(ref: Channel, t, e, window: Tuple[float, float]) -> BiasEstimate:
    """Mean raw residual of the labelled unloaded window; fitted once, then frozen."""
    t = np.asarray(t, dtype=np.float64)
    e = np.asarray(e, dtype=np.float64)
    sel = (t >= window[0]) & (t <= window[1]) & np.isfinite(e)
    n = int(sel.sum())
    if n < 8:
        raise ModelError("route %s: only %d residuals in the baseline window" % (ref, n))
    return BiasEstimate(tuple(ref), float(e[sel].mean()), float(e[sel].std(ddof=1)), n,
                        (float(window[0]), float(window[1])))


# ---------------------------------------------------------------------- group charge
@dataclass
class GroupCharge:
    """Merged, time-ordered accumulation of one package. Immutable once built."""

    t: np.ndarray                    # s, sorted
    q_proxy: np.ndarray              # C, cumulative; starts at 0
    dq: np.ndarray                   # C, increment of each visit (raw routes summed in time order)
    route: np.ndarray                # index of the route per increment
    per_route_q: Dict[Channel, Tuple[np.ndarray, np.ndarray]]    # ref -> (t, cumulative Q)

    def force(self, d33_eff: float, origin_t: Optional[float] = None) -> np.ndarray:
        """Relative force F(t) - F(t0) in N with t0 = origin_t (default: first sample)."""
        if not (math.isfinite(d33_eff) and d33_eff != 0.0):
            raise ModelError("d33_eff must be finite and nonzero")
        q0 = self.q_at(origin_t) if origin_t is not None else self.q_proxy[0]
        return (self.q_proxy - q0) / d33_eff

    def q_at(self, t0: float) -> float:
        if len(self.t) == 0:
            return 0.0
        i = int(np.searchsorted(self.t, t0, side="right")) - 1
        return float(self.q_proxy[max(i, 0)])


def accumulate(models: Dict[Channel, RouteModel], samples: Dict[Channel, dict],
               bias: Optional[Dict[Channel, float]] = None, polarity: float = 1.0,
               leakage: Optional[Dict[Channel, LeakageModel]] = None,
               drift_rate: Optional[Dict[Channel, float]] = None,
               route_polarity: Optional[Dict[Channel, float]] = None) -> GroupCharge:
    """Charge proxy of the routes in ``samples``.

    ``samples[ref]`` holds arrays ``t`` (s), ``volts``, ``tc`` and optionally ``valid`` and
    ``generation``. ``bias[ref]`` is the frozen per-route residual bias (V), default 0.
    ``polarity`` (+1/-1) is the documented output polarity, applied exactly once here.
    ``route_polarity`` is a fixed relative sign per channel, applied before the sum.
    ``drift_rate`` is a frozen per-route charge rate in C/s after polarity. Its
    actual inter-visit interval is subtracted on valid linked samples only.
    """
    if polarity not in (1, -1, 1.0, -1.0):
        raise ModelError("polarity must be +1 or -1")
    bias = bias or {}
    leakage = leakage or {}
    times: List[np.ndarray] = []
    incs: List[np.ndarray] = []
    idx: List[np.ndarray] = []
    per_route: Dict[Channel, Tuple[np.ndarray, np.ndarray]] = {}
    for k, (ref, data) in enumerate(samples.items()):
        model = models[ref]
        t = np.asarray(data["t"], dtype=np.float64)
        if len(t) != len(data["volts"]) or not np.all(np.isfinite(t)) or np.any(np.diff(t) <= 0):
            raise ModelError("route %s timestamps must be finite, ordered and match samples" % (ref,))
        e = residuals(model, data["volts"], data["tc"], data.get("valid"), data.get("generation"),
                      leakage.get(ref, NO_LEAKAGE))
        corrected = apply_bias(e, bias.get(ref, 0.0))
        if np.any(np.isinf(corrected)):
            raise ModelError("non-finite circuit prediction")
        sign = (route_polarity or {}).get(ref, 1.)
        if sign not in (-1, 1):
            raise ModelError("relative polarity must be +1 or -1 for %s" % (ref,))
        dq = float(polarity) * sign * model.c_total * np.nan_to_num(corrected, nan=0.0)
        if drift_rate is not None:
            rate = drift_rate.get(ref)
            if rate is None or not math.isfinite(rate):
                raise ModelError("missing/invalid charge drift rate for %s" % (ref,))
            dt = np.diff(t, prepend=t[0]) if len(t) else t
            dq -= np.where(np.isfinite(e), rate * dt, 0.)
        times.append(t)
        incs.append(dq)
        idx.append(np.full(len(t), k))
        per_route[ref] = (t, np.cumsum(dq))
    if not times:
        z = np.zeros(0)
        return GroupCharge(z, z, z, np.zeros(0, dtype=int), per_route)
    t_all = np.concatenate(times)
    order = np.argsort(t_all, kind="stable")
    dq_all = np.concatenate(incs)[order]
    return GroupCharge(t_all[order], np.cumsum(dq_all), dq_all, np.concatenate(idx)[order], per_route)


# ---------------------------------------------------------------------- event fitting
@dataclass(frozen=True)
class EventStep:
    delta_q: float                   # C, step with the drift ramp removed
    drift_rate: float                # C/s
    drift_charge_over_gap: float     # drift * (post_start - pre_end), the part a plain after-before would add
    plain_delta_q: float             # C, mean(post) - mean(pre)
    rms: float                       # C, residual of the fit
    n_pre: int
    n_post: int
    mode: str


def event_step(t, q, pre: Tuple[float, float], post: Tuple[float, float],
               mode: str = "joint") -> EventStep:
    """Step of the charge proxy across an event from stable pre/post windows (s).

    ``mode``: ``joint`` fits ``Q = a + r*(t-t0) + s*[t >= post_start]`` over both windows
    (one drift rate); ``pre`` takes the drift rate from the pre window only; ``none`` ignores
    drift (plain difference of means, for comparison). The step is the jump at the gap
    between the windows, so it excludes drift times the elapsed time.
    """
    t = np.asarray(t, dtype=np.float64)
    q = np.asarray(q, dtype=np.float64)
    if not (pre[0] < pre[1] <= post[0] < post[1]):
        raise ModelError("pre/post windows must be ordered and not overlap")
    sp = (t >= pre[0]) & (t <= pre[1])
    so = (t >= post[0]) & (t <= post[1])
    n_pre, n_post = int(sp.sum()), int(so.sum())
    if n_pre < 8 or n_post < 8:
        raise ModelError("event windows hold too few samples (%d pre, %d post)" % (n_pre, n_post))
    plain = float(q[so].mean() - q[sp].mean())
    t0 = pre[1]
    gap = post[0] - pre[1]
    if mode == "none":
        rate = 0.0
        step = plain
        fit_res = np.concatenate([q[sp] - q[sp].mean(), q[so] - q[so].mean()])
    elif mode == "pre":
        x = t[sp] - t0
        slope, icpt = np.polyfit(x, q[sp], 1)
        rate = float(slope)
        resid_post = q[so] - (icpt + slope * (t[so] - t0))
        step = float(resid_post.mean())
        fit_res = np.concatenate([q[sp] - (icpt + slope * x), resid_post - resid_post.mean()])
    elif mode == "joint":
        tt = np.concatenate([t[sp], t[so]]) - t0
        qq = np.concatenate([q[sp], q[so]])
        h = np.concatenate([np.zeros(n_pre), np.ones(n_post)])
        design = np.column_stack([np.ones_like(tt), tt, h])
        coef, *_ = np.linalg.lstsq(design, qq, rcond=None)
        rate, step = float(coef[1]), float(coef[2])
        fit_res = qq - design @ coef
    else:
        raise ModelError("unknown event mode %r" % mode)
    rms = float(np.sqrt(np.mean(fit_res ** 2)))
    # The "step" of mode 'pre'/'joint' is the jump at the window boundary (drift extrapolated
    # to the boundary of the post window); express it at the same place for all modes.
    return EventStep(step, rate, rate * gap, plain, rms, n_pre, n_post, mode)


# -------------------------------------------------------------------- d33_eff fitting
@dataclass(frozen=True)
class CalEvent:
    event_id: str
    mass_g: float
    direction: int                   # +1 placement, -1 removal
    repeat: int
    delta_q: float                   # C (drift-corrected step)
    drift_rate: float = 0.0
    accepted: bool = True
    position: str = ""               # contact-position tag, optional
    plain_delta_q: Optional[float] = None  # deployed accumulator endpoint difference

    def delta_f(self, g: float = STANDARD_GRAVITY) -> float:
        return self.direction * self.mass_g * 1e-3 * g


@dataclass(frozen=True)
class D33Fit:
    d33_eff: float                   # C/N (signed)
    intercept: float                 # C
    r2: float
    n: int
    rms_force_error_n: float
    max_force_error_n: float
    placement_d33: Optional[float]
    removal_d33: Optional[float]
    repeat_spread: float             # relative std of per-repeat slopes (NaN with one repeat)
    force_errors_n: Tuple[float, ...]
    flags: Tuple[str, ...]
    regression_force_errors_n: Tuple[float, ...] = ()

    @property
    def d33_pc_per_n(self) -> float:
        return self.d33_eff * 1e12


def _line(x: np.ndarray, y: np.ndarray, through_origin: bool = False):
    if through_origin:
        slope = float(x @ y / (x @ x))
        return slope, 0.0
    design = np.column_stack([x, np.ones_like(x)])
    (slope, icpt), *_ = np.linalg.lstsq(design, y, rcond=None)
    return float(slope), float(icpt)


def fit_d33(events: Sequence[CalEvent], g: float = STANDARD_GRAVITY,
            intercept_limit_fraction: float = 0.25, through_origin: bool = False) -> D33Fit:
    """Common signed slope delta_Q = d33_eff * delta_F + intercept over accepted events."""
    use = [e for e in events if e.accepted and math.isfinite(e.delta_q)]
    if len(use) < 3:
        raise ModelError("at least 3 accepted events are needed (have %d)" % len(use))
    x = np.array([e.delta_f(g) for e in use])
    y = np.array([e.delta_q for e in use])
    if len(set(np.round(np.abs(x), 12))) < 2 and len({e.direction for e in use}) < 2:
        raise ModelError("events cover a single force step: gain and intercept cannot be separated")
    slope, icpt = _line(x, y, through_origin)
    if slope == 0.0 or not math.isfinite(slope):
        raise ModelError("fit gave a zero/non-finite slope")
    fitted = slope * x + icpt
    ss_res = float(((y - fitted) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    # Force error of each event when the fitted gain is used (intercept reported separately).
    # The deployed accumulator uses Q/slope; an event intercept is diagnostic.
    deployed = np.array([e.delta_q if e.plain_delta_q is None else e.plain_delta_q for e in use])
    err = deployed / slope - x
    flags: List[str] = []
    span = float(np.ptp(y)) if len(y) else 0.0
    if span > 0 and abs(icpt) > intercept_limit_fraction * span:
        flags.append("LARGE_INTERCEPT")
    drift = [abs(e.drift_rate) for e in use]
    if np.any(np.array(drift) > 0):
        smallest = float(np.min(np.abs(y)))
        if max(drift) * 1.0 > 0.5 * smallest:
            flags.append("DRIFT_RATE_LARGE_VS_STEP")
    plus = [i for i, e in enumerate(use) if e.direction > 0]
    minus = [i for i, e in enumerate(use) if e.direction < 0]
    pd = _line(x[plus], y[plus], through_origin)[0] if len(plus) >= 2 else None
    md = _line(x[minus], y[minus], through_origin)[0] if len(minus) >= 2 else None
    if pd is not None and md is not None and abs(pd - md) > 0.15 * abs(slope):
        flags.append("DIRECTION_MISMATCH")
    per_repeat: List[float] = []
    for r in sorted({e.repeat for e in use}):
        sel = [i for i, e in enumerate(use) if e.repeat == r]
        if len(sel) >= 2 and len(set(np.round(x[sel], 12))) >= 2:
            per_repeat.append(_line(x[sel], y[sel], through_origin)[0])
    spread = (float(np.std(per_repeat, ddof=1) / abs(np.mean(per_repeat)))
              if len(per_repeat) >= 2 and np.mean(per_repeat) != 0 else float("nan"))
    positions = {}
    for i, e in enumerate(use):
        if e.position and x[i] != 0:
            positions.setdefault(e.position, []).append(y[i] / x[i])
    if len(positions) >= 2:
        means = [float(np.mean(v)) for v in positions.values()]
        if abs(np.mean(means)) > 0 and np.ptp(means) > 0.2 * abs(np.mean(means)):
            flags.append("POSITION_DEPENDENT_GAIN")
    return D33Fit(slope, icpt, r2, len(use), float(np.sqrt(np.mean(err ** 2))),
                  float(np.max(np.abs(err))), pd, md, spread,
                  tuple(float(v) for v in err), tuple(flags),
                  tuple(float(v) for v in ((y - icpt) / slope - x)))


@dataclass(frozen=True)
class Validation:
    train_repeats: Tuple[int, ...]
    test_repeats: Tuple[int, ...]
    fit: D33Fit
    rms_force_error_n: float
    max_force_error_n: float
    force_errors_n: Tuple[float, ...]
    passed: Optional[bool]           # None until a limit is given
    limit_n: Optional[float]


def validate_holdout(events: Sequence[CalEvent], test_repeats: Iterable[int],
                     g: float = STANDARD_GRAVITY, limit_n: Optional[float] = None,
                     through_origin: bool = False) -> Validation:
    """Fit on the other repeats and evaluate the held-out ones with that fit."""
    test = set(test_repeats)
    train_events = [e for e in events if e.repeat not in test]
    held = [e for e in events if e.repeat in test and e.accepted and math.isfinite(e.delta_q)]
    if not held:
        raise ModelError("no accepted events in the held-out repeats")
    if len([e for e in train_events if e.accepted and math.isfinite(e.delta_q)]) < 3:
        raise ModelError("insufficient training evidence: at least 3 events must remain after holding out repeats")
    if limit_n is not None and (not math.isfinite(limit_n) or limit_n <= 0):
        raise ModelError("validation error limit must be finite and positive")
    fit = fit_d33(train_events, g, through_origin=through_origin)
    errs = np.array([(e.delta_q if e.plain_delta_q is None else e.plain_delta_q) / fit.d33_eff
                     - e.delta_f(g) for e in held])
    rms = float(np.sqrt(np.mean(errs ** 2)))
    mx = float(np.max(np.abs(errs)))
    passed = None if limit_n is None else bool(mx <= limit_n)
    return Validation(tuple(sorted({e.repeat for e in train_events if e.accepted})),
                      tuple(sorted(test)), fit, rms, mx, tuple(float(v) for v in errs),
                      passed, limit_n)


# ------------------------------------------------------------------- leakage fitting
@dataclass(frozen=True)
class ConstantWindow:
    """A labelled constant-force interval (unloaded baseline, loaded hold, post-removal)."""

    name: str
    t0: float
    t1: float
    role: str = "hold"               # baseline | hold | unloaded


@dataclass(frozen=True)
class LeakageFit:
    status: str                      # identified | not_identified
    model: LeakageModel
    reason: str
    chi2_none: float
    chi2_best: float
    window_slopes_none: Dict[str, float]
    window_slopes_best: Dict[str, float]

    def to_json(self) -> dict:
        return {"status": self.status, "lam": self.model.lam, "kind": self.model.kind,
                "v_leak_v": self.model.v_leak, "model_note": self.model.note,
                "reason": self.reason, "chi2_none": self.chi2_none, "chi2_best": self.chi2_best,
                "window_slopes_none": self.window_slopes_none,
                "window_slopes_best": self.window_slopes_best}


def _slope_and_se(t: np.ndarray, q: np.ndarray) -> Tuple[float, float]:
    """OLS slope of the accumulated charge over a window and its standard error.

    The accumulator is a random walk, so the usual regression error is far too small. With
    white increments of std sigma sampled every dt, the OLS slope of an n-point walk has
    variance 1.2*sigma^2/(n*dt^2). Coloured noise makes the true error larger; this is the
    white-noise approximation used to judge identifiability.
    """
    n = len(t)
    if n < 8:
        return float("nan"), float("nan")
    x = t - t.mean()
    slope = float(x @ (q - q.mean()) / float(x @ x))
    sigma = float(np.std(np.diff(q), ddof=1))
    dt = float(np.mean(np.diff(t)))
    if not (dt > 0 and math.isfinite(sigma)):
        return slope, float("nan")
    return slope, math.sqrt(1.2 * max(sigma, 1e-300) ** 2 / (n * dt * dt))


def fit_leakage(models: Dict[Channel, RouteModel], samples: Dict[Channel, dict],
                windows: Sequence[ConstantWindow], polarity: float = 1.0,
                lam_bounds: Tuple[float, float] = (0.90, 1.0), grid: int = 101,
                min_voltage_contrast_v: float = 0.005, min_chi2_gain: float = 9.0,
                v_leak: Optional[float] = None, route_polarity=None) -> LeakageFit:
    """Fit an effective off-state relaxation factor ``lam`` on constant-force windows.

    For each candidate lam the per-route bias is re-estimated on the ``baseline`` window and
    frozen; the objective is the sum of squared (slope / error) of the accumulated charge
    proxy over the other constant-force windows (it must stay constant). The result is
    ``not_identified`` (no correction, lam = 1) when the data cannot separate leakage from a
    bias, reference drift, qv error or creep: too little voltage contrast between windows,
    an optimum at the search bound, or no significant improvement over no correction.
    Slope-only fitting never claims to measure Roff.
    """
    base = [w for w in windows if w.role == "baseline"]
    others = [w for w in windows if w.role != "baseline"]
    none = LeakageModel()
    if len(base) != 1 or not others:
        return LeakageFit("not_identified", none, "needs one baseline window and at least one "
                          "other constant-force window", float("nan"), float("nan"), {}, {})

    def evaluate(lam: float):
        leak = LeakageModel(lam, v_leak, "effective" if lam < 1 else "none")
        leaks = {ref: leak for ref in samples}
        bias = {}
        for ref, d in samples.items():
            e = residuals(models[ref], d["volts"], d["tc"], d.get("valid"), d.get("generation"), leak)
            bias[ref] = estimate_bias(ref, d["t"], e, (base[0].t0, base[0].t1)).bias_v
        charge = accumulate(models, samples, bias, polarity, leaks, route_polarity=route_polarity)
        slopes, chi2 = {}, 0.0
        for w in others:
            sel = (charge.t >= w.t0) & (charge.t <= w.t1)
            slope, se = _slope_and_se(charge.t[sel], charge.q_proxy[sel])
            slopes[w.name] = slope
            if math.isfinite(slope) and math.isfinite(se) and se > 0:
                chi2 += (slope / se) ** 2
        return chi2, slopes

    # Voltage contrast: mean distance below VD per window relative to the baseline window.
    def mean_d(w: ConstantWindow) -> float:
        vals = []
        for ref, d in samples.items():
            t = np.asarray(d["t"])
            sel = (t >= w.t0) & (t <= w.t1)
            if sel.any():
                vals.append(models[ref].v_d - float(np.mean(np.asarray(d["volts"])[sel])))
        return float(np.mean(vals)) if vals else float("nan")

    d_base = mean_d(base[0])
    contrast = max(abs(mean_d(w) - d_base) for w in others)
    lams = np.linspace(lam_bounds[0], lam_bounds[1], grid)
    chis = []
    all_slopes = []
    for lam in lams:
        c, s = evaluate(float(lam))
        chis.append(c)
        all_slopes.append(s)
    chis = np.array(chis)
    chi_none, slopes_none = chis[-1], all_slopes[-1]            # lam == 1
    best = int(np.nanargmin(chis))
    lam_best = float(lams[best])
    reason = ""
    if not (contrast >= min_voltage_contrast_v):
        reason = ("windows differ by only %.1f mV from the baseline: leakage cannot be separated "
                  "from bias" % (contrast * 1e3 if math.isfinite(contrast) else float("nan")))
    elif best in (0, grid - 1) and best != grid - 1:
        reason = "optimum at the lower search bound lam=%.3f" % lam_bounds[0]
    elif best == grid - 1:
        reason = "no leakage correction improves the constant-force windows"
    elif chi_none - chis[best] < min_chi2_gain:
        reason = ("improvement over no correction (chi2 %.1f -> %.1f) is not significant"
                  % (chi_none, chis[best]))
    if reason:
        return LeakageFit("not_identified", none, reason, float(chi_none), float(chis[best]),
                          slopes_none, all_slopes[best])
    model = LeakageModel(lam_best, v_leak, "effective",
                         "effective observation correction over the tested voltage/timing domain; "
                         "not a measurement of Roff")
    return LeakageFit("identified", model, "", float(chi_none), float(chis[best]),
                      slopes_none, all_slopes[best])


# ------------------------------------------------------------------------ range checks
def range_window(t, start: float, end: float = float("inf")):
    """Select measured outputs in an analysis window and their predictor inputs.

    The sample just before the window is required by its first residual. Earlier
    settling samples contribute only an accumulator offset, which cancels in steps.
    """
    active = (np.asarray(t) >= start) & (np.asarray(t) <= end)
    inputs = active.copy()
    inputs[:-1] |= active[1:]
    return active, inputs


def range_report(model: RouteModel, volts, headroom_codes: float = 8.0,
                 span_volts: float = 2.5, bits: int = ADC_BITS_7953,
                 high_margin: float = 0.05, neg_margin_v: Optional[float] = None) -> dict:
    """Clipping and calibrated-range status of one route over a capture.

    ``outside_fitted_range`` (reject): the distance below VD exceeds the fitted maximum by
    more than ``high_margin``, i.e. the voltage fell below anything the transition fit saw.
    A voltage ABOVE VD is retained for provisional analysis (``extrapolated_low`` reports it).
    Smoothness of the polynomial does not establish accuracy there. ``neg_margin_v`` is an
    optional extra limit on the excursion above VD (off by default).
    ``extrapolated_low`` (informational): some samples lie nearer to VD than the fit's data
    floor (``d_min``, a noise cut-off) or beyond it; a force step moves the sensor there by
    design.
    """
    v = np.asarray(volts, dtype=np.float64)
    invalid = bool(np.any(~np.isfinite(v)))
    v = v[np.isfinite(v)]
    lsb = span_volts / (1 << bits)
    top = span_volts - headroom_codes * lsb
    d = model.v_d - v
    clipped = bool(np.any(v >= top) or np.any(v <= headroom_codes * lsb))
    saturated = bool(np.any(v >= span_volts - lsb) or np.any(v <= 0))
    outside = low = False
    if math.isfinite(model.d_max) and len(d):
        outside = bool(np.any(d > model.d_max * (1.0 + high_margin) + 1e-9))
    if neg_margin_v is not None:
        if not math.isfinite(neg_margin_v) or neg_margin_v <= 0:
            raise ModelError("optional excursion guard must be finite and positive")
        outside = outside or bool(np.any(d < -neg_margin_v))
    if math.isfinite(model.d_min) and len(d):
        low = bool(np.any(d < model.d_min - 1e-9))
    above = d < 0
    below = d > model.d_max if math.isfinite(model.d_max) else np.zeros(len(d), dtype=bool)
    return {"clipped": clipped, "outside_fitted_range": outside, "extrapolated_low": low,
            "saturated": saturated, "headroom_low": clipped and not saturated,
            "invalid_input": invalid, "coverage_known": math.isfinite(model.d_min) and math.isfinite(model.d_max),
            "above_vd_count": int(above.sum()), "below_fit_count": int(below.sum()),
            "max_above_vd_v": float(max(0., -d.min())) if len(d) else 0.,
            "adc_span_v": span_volts, "headroom_codes": headroom_codes,
            "d_min_v": float(d.min()) if len(d) else float("nan"),
            "d_max_v": float(d.max()) if len(d) else float("nan"),
            "v_min": float(v.min()) if len(v) else float("nan"),
            "v_max": float(v.max()) if len(v) else float("nan")}
