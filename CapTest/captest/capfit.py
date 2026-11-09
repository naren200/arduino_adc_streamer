"""Numerical analysis of a capacitance-test burst (no GUI, no serial I/O).

Model: after the sensor is connected at t_c the shared MXO node, the sensor
node and the bias resistor form one RC to Vmid::

    V(t) = Vinf - (Vinf - V0) * exp(-(t - t_c) / tau)      (t >= t_c)
    tau      = R_bias * C_total
    V0       = V_s0 + (V_D - V_s0) * C_D / C_total          (charge sharing)

so the step ratio is C_D / C_total = (V0 - V_s0) / (V_D - V_s0).

Real PZT sensors are not a single RC: their charge-up has a fast and a slow
component (see docs). The optional two-exponential model

    V(t) = Vinf - A_fast*exp(-t/tau_fast) - A_slow*exp(-t/tau_slow),
    V0   = Vinf - A_fast - A_slow

is fitted next to the single exponential. The user selects which one drives
tau, C_total, V0 and the ratio (AnalysisSettings.fit_model); the other one is
always kept so its effect on C_total can be reported.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

try:  # optional
    from scipy.optimize import curve_fit as _curve_fit
    from scipy.optimize import least_squares as _least_squares
except Exception:  # pragma: no cover - exercised only without scipy
    _curve_fit = None
    _least_squares = None

from . import protocol

# Quality flag names (strings keep the exported tables readable).
LOW_AMPLITUDE = "LOW_AMPLITUDE"
TAG_MISMATCH = "TAG_MISMATCH"
HIGH_RESIDUAL = "HIGH_RESIDUAL"
PERIODIC_RESIDUAL = "PERIODIC_RESIDUAL"
VS0_SUSPECT = "VS0_SUSPECT"
PERIOD_SHORT = "FRAME_PERIOD_SHORT"
ACQUISITION_SHORT = "ACQUISITION_SHORT"
INCOMPLETE = "INCOMPLETE_BURST"
FIT_FAILED = "FIT_FAILED"
FEW_SAMPLES = "FEW_SAMPLES"
NO_VMID = "NO_VMID_READING"
VINF_OFFSET = "VINF_NOT_VMID"
V0_INCONSISTENT = "V0_INCONSISTENT"
TOO_SOON = "TOO_SOON"
VS0_HIGH = "VS0_HIGH"
DATA_ERRORS = "DATA_ERRORS"                # ADC124: responses with non-zero upper bits
SWITCH_JITTER = "ENABLE_EDGE_JITTER"       # ADC124: the GPIO mux switch took longer than expected
STAMP_GAP = "STAMP_GAP"                    # ADC124: a stalled frame (interrupt) in the time stamps
CLOCK_OUT_OF_SPEC = "CLOCK_OUT_OF_SPEC"    # SPI clock outside the ADC's specified range
SHORT_WINDOW = "SHORT_WINDOW"              # the burst is shorter than 4 tau: raise the sample count
V0_OUT_OF_RANGE = "V0_OUT_OF_RANGE"        # fitted V0 outside 0..VREF: not physically possible
FRAME_DEPENDENT = "FRAME_DEPENDENT"        # tau/R_bias depends on the frame period: a relative reading, not a capacitance
MIXED_METHODS = "MIXED_METHODS"            # bursts taken by different methods: only one method is averaged
VMID_RESET_FAILED = "VMID_RESET_FAILED"    # Vmid between samples: the node did not return to V_D after a cycle
VINF_NOT_SETTLED = "VINF_NOT_SETTLED"      # Vinf mode: the held sensor was still moving at the end of the burst
VINF_ALT_NOT_SETTLED = "VINF_ALT_NOT_SETTLED"  # Vinf mode: the sensor alternating with Vmid was still moving at the end
UNSTABLE_TAU = "UNSTABLE_TAU"              # tau differs between the repeated bursts of one channel

LOW_AMPLITUDE_LSB = 50.0
HIGH_RESIDUAL_LSB = 2.0
MIN_FRAME_PERIOD_NS = 1000.0
MIN_ACQUISITION_NS = 325.0
# Pin leakage (about 16 inputs x 3-7 nA at 25 C, datasheet Figure 43) pulls the node
# below Vmid by I*R_bias; only a much larger implied current is suspicious.
MAX_LEAK_NA = 250.0
FRAME_PATTERN_MAX_PERIOD = 6.5     # frames; the band searched for frame-rate patterns

FIT_ONE = "1exp"
FIT_TWO = "2exp"
FIT_MODELS = (FIT_ONE, FIT_TWO)
FIT_ALT = "alt"                    # Vmid between samples (capalt), see analyze_alternate

ALT_MIN_DISTANCE_V = 0.006         # alternate fit: cycles still this far below V_D (about 10 LSB)
ALT_MIN_PAIRS = 6                  # ... at least this many cycle-to-cycle pairs
ALT_MIN_CONTRAST = 1.2             # longest / shortest connected time needed for tau
ALT_RESET_TOLERANCE_V = 0.005      # the Vmid reading closing a cycle may differ this much from V_D (board: < 1 mV)
ALT_MIN_START_V = 0.3              # first sample at least this far below V_D (sensor rested)
ALT_SMALL_RANGE = "ALT_SMALL_RANGE"  # alternate burst started too close to V_D: excluded
# How C_total was obtained (exported as c_total_method).
C_TOTAL_TAU_OVER_R = "tau_over_r"  # tau / R_bias of one connection: a lower bound on the 7953 (frame effect)
C_TOTAL_VMID_CYCLES = "vmid_cycles"  # tau of the cycle-to-cycle fit with Vmid between samples
UNSTABLE_TAU_FRACTION = 0.05
VINF_TAIL_FRACTION = 0.25          # Vinf mode: the median is taken over the last quarter of the held burst ...
VINF_SETTLE_LSB = 3.0              # ... and must agree with the quarter before it to within this many LSB       # sd / mean of tau over the bursts of a channel
# The second exponential is only used when it is clearly needed.
MIN_TAU_RATIO = 1.4                # tau_slow / tau_fast
MIN_RMS_IMPROVEMENT = 0.85         # rms(2 exp) must be below this fraction of rms(1 exp) ...
MIN_RMS_GAIN_LSB = 0.3             # ... and at least this much lower (in LSB)
# Starting voltage check (see start_voltage_check).
VS0_MIN_CHANNELS = 5
VS0_THRESHOLD_V = 0.05


@dataclass
class AnalysisSettings:
    vref: float = 2.5
    discard_k: int = 2
    v_s0: float = 0.0
    c_parasitic_pf: float = 5.0
    r_bias_tolerance: float = 0.01
    first_clock_delay_ns: Optional[float] = None   # None: use the value reported by the device
    fit_model: str = FIT_TWO                        # "1exp" or "2exp" (2exp falls back to 1exp when not needed)


@dataclass
class FitResult:
    vinf: float
    v0: float
    tau: float
    rms_residual_v: float
    residuals_v: np.ndarray
    ok: bool = True
    method: str = "varpro"


def code_to_volts(code, vref: float = 2.5):
    return np.asarray(code, dtype=np.float64) / 4096.0 * vref


def _linear_solution(t: np.ndarray, v: np.ndarray, tau: float):
    """For fixed tau the model v = vinf + b*exp(-t/tau) is linear in (vinf, b)."""
    basis = np.exp(-t / tau)
    design = np.column_stack([np.ones_like(t), basis])
    coef, *_ = np.linalg.lstsq(design, v, rcond=None)
    resid = v - design @ coef
    return coef, float(resid @ resid)


def fit_exponential(t: np.ndarray, v: np.ndarray) -> FitResult:
    """Fit Vinf, V0, tau to (t - t_c, v); t is seconds since the connection.

    Numpy-only variable projection (1-D search over tau with an exact linear
    solve for Vinf and the amplitude); polished with scipy's curve_fit when
    scipy is installed.
    """
    t = np.asarray(t, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)
    if len(t) < 5 or not np.all(np.isfinite(v)):
        return FitResult(np.nan, np.nan, np.nan, np.nan, np.zeros(0), ok=False)
    span = max(t[-1] - t[0], 1e-9)
    step = max(span / max(len(t) - 1, 1), 1e-9)

    # Coarse log grid over tau, then golden-section refinement.
    lo, hi = np.log(step * 0.2), np.log(span * 50.0)
    grid = np.linspace(lo, hi, 80)
    costs = np.array([_linear_solution(t, v, np.exp(g))[1] for g in grid])
    best = int(np.argmin(costs))
    a = grid[max(best - 1, 0)]
    b = grid[min(best + 1, len(grid) - 1)]
    inv_phi = (np.sqrt(5.0) - 1.0) / 2.0
    c, d = b - inv_phi * (b - a), a + inv_phi * (b - a)
    fc, fd = _linear_solution(t, v, np.exp(c))[1], _linear_solution(t, v, np.exp(d))[1]
    for _ in range(60):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - inv_phi * (b - a)
            fc = _linear_solution(t, v, np.exp(c))[1]
        else:
            a, c, fc = c, d, fd
            d = a + inv_phi * (b - a)
            fd = _linear_solution(t, v, np.exp(d))[1]
    tau = float(np.exp((a + b) / 2.0))
    (vinf, amp), _ = _linear_solution(t, v, tau)
    method = "varpro"

    if _curve_fit is not None:
        try:
            def model(tt, vinf_, v0_, tau_):
                return vinf_ - (vinf_ - v0_) * np.exp(-tt / tau_)
            popt, _ = _curve_fit(model, t, v, p0=[vinf, vinf + amp, tau],
                                 bounds=([-np.inf, -np.inf, step * 0.05],
                                         [np.inf, np.inf, span * 1e3]),
                                 maxfev=2000)
            trial = v - model(t, *popt)
            if float(trial @ trial) <= float(((v - (vinf + amp * np.exp(-t / tau))) ** 2).sum()) * (1 + 1e-9):
                vinf, amp, tau = float(popt[0]), float(popt[1] - popt[0]), float(popt[2])
                method = "scipy"
        except Exception:
            pass

    v0 = vinf + amp
    fitted = vinf + amp * np.exp(-t / tau)
    residuals = v - fitted
    at_edge = tau <= step * 0.21 or tau >= span * 49.0
    return FitResult(float(vinf), float(v0), float(tau),
                     float(np.sqrt(np.mean(residuals ** 2))), residuals,
                     ok=not at_edge, method=method)


@dataclass
class TwoExpFit:
    vinf: float
    v0: float
    tau_fast: float
    tau_slow: float
    a_fast: float            # amplitudes (V), same sign convention as vinf + a*exp(-t/tau)
    a_slow: float
    rms_residual_v: float
    residuals_v: np.ndarray
    ok: bool = True
    method: str = "varpro"

    @property
    def fast_fraction(self) -> float:
        total = self.a_fast + self.a_slow
        return float(self.a_fast / total) if total else float("nan")


def _lin3(t: np.ndarray, v: np.ndarray, tau1: float, tau2: float):
    """For fixed taus the model is linear in (vinf, a1, a2)."""
    design = np.column_stack([np.ones_like(t), np.exp(-t / tau1), np.exp(-t / tau2)])
    coef, *_ = np.linalg.lstsq(design, v, rcond=None)
    resid = v - design @ coef
    return coef, float(resid @ resid)


def fit_two_exponentials(t: np.ndarray, v: np.ndarray,
                         tau_hint: Optional[float] = None) -> TwoExpFit:
    """Fit Vinf and two decaying exponentials to (t - t_c, v).

    Numpy-only variable projection: a log grid over (tau_fast, tau_slow) around
    the single-exponential `tau_hint`, an exact linear solve for Vinf and both
    amplitudes at every grid point, then a shrinking pattern search on the two
    taus; polished with scipy's least_squares when scipy is installed."""
    t = np.asarray(t, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)
    failed = TwoExpFit(np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, np.zeros(0), ok=False)
    if len(t) < 12 or not np.all(np.isfinite(v)):
        return failed
    span = max(t[-1] - t[0], 1e-9)
    step = max(span / max(len(t) - 1, 1), 1e-9)
    centre = tau_hint if tau_hint and np.isfinite(tau_hint) else span / 6.0
    lo = np.log(max(step * 0.3, centre / 10.0))
    hi = np.log(min(span * 3.0, centre * 10.0))
    if hi <= lo:
        return failed
    grid = np.linspace(lo, hi, 26)
    best = (np.inf, 0, 1)
    for i in range(len(grid)):
        for j in range(i + 1, len(grid)):
            if grid[j] - grid[i] < np.log(1.3):
                continue
            cost = _lin3(t, v, np.exp(grid[i]), np.exp(grid[j]))[1]
            if cost < best[0]:
                best = (cost, i, j)
    if not np.isfinite(best[0]):
        return failed
    x = np.array([grid[best[1]], grid[best[2]]])
    cost = best[0]
    delta = 0.2
    for _ in range(120):
        improved = False
        for dx in ((delta, 0), (-delta, 0), (0, delta), (0, -delta),
                   (delta, delta), (-delta, -delta), (delta, -delta), (-delta, delta)):
            trial = x + np.array(dx)
            if trial[1] - trial[0] < np.log(1.05) or not (np.log(step * 0.05) < trial[0] < trial[1] < np.log(span * 50)):
                continue
            c = _lin3(t, v, np.exp(trial[0]), np.exp(trial[1]))[1]
            if c < cost:
                x, cost, improved = trial, c, True
        if not improved:
            delta *= 0.5
            if delta < 1e-3:
                break
    tau1, tau2 = float(np.exp(x[0])), float(np.exp(x[1]))
    (vinf, a1, a2), _ = _lin3(t, v, tau1, tau2)
    method = "varpro"

    if _least_squares is not None:
        try:
            def model(p):
                return p[0] + p[1] * np.exp(-t / p[2]) + p[3] * np.exp(-t / p[4])
            r = _least_squares(lambda p: model(p) - v, [vinf, a1, tau1, a2, tau2],
                               x_scale=[0.05, 0.05, tau1, 0.05, tau2],
                               bounds=([-5, -10, step * 0.05, -10, step * 0.05],
                                       [5, 10, span * 50, 10, span * 50]))
            trial = float(np.sum(r.fun ** 2))
            if trial <= cost * (1 + 1e-9) and r.x[4] > r.x[2] * 1.02:
                vinf, a1, tau1, a2, tau2 = (float(q) for q in r.x)
                cost, method = trial, "scipy"
        except Exception:
            pass

    if tau1 > tau2:
        tau1, tau2, a1, a2 = tau2, tau1, a2, a1
    fitted = vinf + a1 * np.exp(-t / tau1) + a2 * np.exp(-t / tau2)
    residuals = v - fitted
    at_edge = tau1 <= step * 0.06 or tau2 >= span * 45.0
    return TwoExpFit(float(vinf), float(vinf + a1 + a2), tau1, tau2, float(a1), float(a2),
                     float(np.sqrt(np.mean(residuals ** 2))), residuals, ok=not at_edge,
                     method=method)


def periodic_residual(residuals_lsb: np.ndarray, min_amplitude_lsb: float = 1.0,
                      peak_to_floor: float = 20.0, floor_bins: int = 17) -> Optional[float]:
    """Return the period (frames) of a narrow line at 2..FRAME_PATTERN_MAX_PERIOD
    frames in the residuals, or None.

    Such a short period is the signature of something tied to the frame rate
    (e.g. a mux that re-closes every other frame). The residual noise of the real
    board is coloured (neighbouring samples correlate and the spectrum is red),
    so longer periods are not judged: slow structure there is indistinguishable
    from model misfit and noise. A line has to stand out against the local median
    of the spectrum, not against its mean."""
    r = np.asarray(residuals_lsb, dtype=np.float64)
    n = len(r)
    if n < 64:
        return None
    r = r - r.mean()
    spectrum = np.abs(np.fft.rfft(r)) ** 2
    body = spectrum[1:]                         # body[k-1] belongs to bin k
    half = floor_bins // 2
    padded = np.pad(body, half, mode="reflect")
    floor = np.median(np.lib.stride_tricks.sliding_window_view(padded, floor_bins), axis=1)
    ratio = body / np.maximum(floor, 1e-12)
    bins = np.arange(1, len(body) + 1)
    in_band = (bins >= n / FRAME_PATTERN_MAX_PERIOD) & (bins <= n / 2.0)
    if not in_band.any():
        return None
    candidate = np.where(in_band, ratio, 0.0)
    peak = int(np.argmax(candidate))
    if candidate[peak] < peak_to_floor:
        return None
    amplitude = 2.0 * np.sqrt(body[peak]) / n
    if amplitude < min_amplitude_lsb:
        return None
    return float(n / bins[peak])


@dataclass
class Measurement:
    """Analysis of one burst."""

    adc: int
    channel: int
    r_bias: float
    tau: float = np.nan
    c_total: float = np.nan
    ratio: float = np.nan
    c_d: float = np.nan
    c_sensor_side: float = np.nan
    c_pzt: float = np.nan
    v0: float = np.nan
    vinf: float = np.nan
    v_d: float = np.nan
    residual_rms_lsb: float = np.nan
    amplitude_lsb: float = np.nan
    n_used: int = 0
    flags: List[str] = field(default_factory=list)
    t: np.ndarray = field(default_factory=lambda: np.zeros(0))       # seconds after t_c
    v: np.ndarray = field(default_factory=lambda: np.zeros(0))       # volts
    fitted: np.ndarray = field(default_factory=lambda: np.zeros(0))
    residuals_lsb: np.ndarray = field(default_factory=lambda: np.zeros(0))
    period_ns: Dict[str, float] = field(default_factory=dict)
    periodic_frames: Optional[float] = None
    leak_na: float = np.nan             # (V_D - Vinf) / R_bias: net leakage pulling the node down
    # Fit model bookkeeping (tau/V0/c_total above come from fit_model_used).
    fit_model_requested: str = FIT_TWO
    fit_model_used: str = FIT_ONE
    tau_fast: float = np.nan            # 2 exp only
    tau_slow: float = np.nan            # = tau for both models
    fast_fraction: float = np.nan       # share of the step carried by the fast component
    tau_1exp: float = np.nan
    v0_1exp: float = np.nan
    rms_1exp: float = np.nan
    c_total_1exp: float = np.nan
    v0_2exp: float = np.nan
    rms_2exp: float = np.nan
    c_total_shift_pct: float = np.nan   # C_total of the used model versus the single exponential
    tau_2exp: float = np.nan            # slow time constant of the two-exponential fit (if it converged)
    c_total_2exp: float = np.nan
    ratio_1exp: float = np.nan          # C_D / C_total with each model's V0, whichever one is in use
    ratio_2exp: float = np.nan
    source: object = None               # the raw burst this was analysed from (set by the session)
    fitted_other: np.ndarray = field(default_factory=lambda: np.zeros(0))   # the model not used
    other_label: str = ""
    vs0_est_v: float = np.nan           # set by start_voltage_check across the channels of an ADC
    ref: tuple = ()                     # board channel id, e.g. (adc, channel) or (array, mux, input)
    c_total_method: str = C_TOTAL_TAU_OVER_R
    # Vmid between samples: the effective connection capacitance at V_D (c0 * C_total) and its
    # fraction of C_total. It is not the node capacitance C_D, so ratio and c_d stay empty there.
    c_share_ratio: float = np.nan
    c_share: float = np.nan
    # Vmid between samples, one entry per cycle: the connected time accumulated up to the cycle's
    # last sensor sample (s), the distance below V_D there (V, NaN where the cycle is not usable)
    # and the fitted model run freely from the first cycle (V).
    alt_connected_s: np.ndarray = field(default_factory=lambda: np.zeros(0))
    alt_d_v: np.ndarray = field(default_factory=lambda: np.zeros(0))
    alt_d_model_v: np.ndarray = field(default_factory=lambda: np.zeros(0))
    # False: V0, the step ratio and what derives from it (C_D, C sensor, C_PZT) have no valid
    # basis for this burst. The values stay on the Measurement; aggregate() leaves them out.
    step_valid: bool = True
    label: str = ""
    pre_t: np.ndarray = field(default_factory=lambda: np.zeros(0))   # pre-connect baseline samples (s, V)
    pre_v: np.ndarray = field(default_factory=lambda: np.zeros(0))
    connect_info: Dict[str, float] = field(default_factory=dict)      # board-specific timing facts

    @property
    def ok(self) -> bool:
        return not any(f in (FIT_FAILED, INCOMPLETE, FEW_SAMPLES, ALT_SMALL_RANGE) for f in self.flags)


def analyze_burst(burst: protocol.BurstData, r_bias: float,
                  settings: Optional[AnalysisSettings] = None,
                  since_last_ms: Optional[float] = None,
                  discharge_ms: float = 0.0) -> Measurement:
    settings = settings or AnalysisSettings()
    m = Measurement(burst.adc, burst.channel, float(r_bias))
    lsb = settings.vref / 4096.0
    _header_checks(m, burst, settings, since_last_ms, discharge_ms)
    # On the 7953 tau depends on the frame period (a charge per frame), so tau / R_bias of a
    # single connection reads low, and V0 does not follow the charge-sharing model.
    m.step_valid = False
    m.flags.append(FRAME_DEPENDENT)

    # Samples: frame 2+i, discard the first K and any with a wrong tag.
    words = burst.words[protocol.LEAD_FRAMES:]
    stamps = burst.timestamps
    if len(words) < 5 + settings.discard_k or len(stamps) < len(words) + protocol.LEAD_FRAMES:
        m.flags.append(FEW_SAMPLES)
        if INCOMPLETE not in m.flags:
            m.flags.append(INCOMPLETE)
        return m
    tags = (words >> 12).astype(np.int64)
    keep = np.ones(len(words), dtype=bool)
    keep[:settings.discard_k] = False
    wrong = tags != burst.channel
    if np.any(wrong[settings.discard_k:]):
        m.flags.append(TAG_MISMATCH)
    keep &= ~wrong

    # Times relative to the connection instant t_c = CS-fall(k+1) + delta.
    fcd = (settings.first_clock_delay_ns if settings.first_clock_delay_ns is not None
           else burst.first_clock_delay_ns) * 1e-9
    delta = fcd + 1.5 / burst.spi_clock_hz
    t_all = protocol.timestamps_to_seconds(stamps, burst.cpu_hz, origin_index=1)
    t_samples = t_all[protocol.LEAD_FRAMES:protocol.LEAD_FRAMES + len(words)] - delta
    volts = code_to_volts(words & 0x0FFF, settings.vref)
    t = t_samples[keep]
    v = volts[keep]
    m.n_used = int(keep.sum())
    if m.n_used < 16:
        m.flags.append(FEW_SAMPLES)
        return m
    return _fit_and_derive(m, t, v, r_bias, settings, lsb)


def _header_checks(m: Measurement, burst: protocol.BurstData, settings: AnalysisSettings,
                   since_last_ms: Optional[float], discharge_ms: float) -> None:
    """Flags from the packet header and the rest time, and V_D from the Vmid words."""
    m.ref = (burst.adc, burst.channel)
    m.period_ns = burst.period_ns()
    if burst.flags & protocol.FLAG_INCOMPLETE or burst.n_frames < burst.n_samples + protocol.LEAD_FRAMES:
        m.flags.append(INCOMPLETE)
    if burst.flags & protocol.FLAG_PERIOD_TOO_SHORT or (m.period_ns["min"] and m.period_ns["min"] < MIN_FRAME_PERIOD_NS):
        m.flags.append(PERIOD_SHORT)
    if burst.flags & protocol.FLAG_ACQUISITION_SHORT:
        m.flags.append(ACQUISITION_SHORT)
    if since_last_ms is not None and since_last_ms < discharge_ms - 0.01:
        m.flags.append(TOO_SOON)
        m.flags.append(VS0_SUSPECT)

    # V_D: Vmid readings (tag 15) from the end of the park and frames 0 and 1.
    candidates = list(burst.vmid_words[-8:]) + list(burst.words[:protocol.LEAD_FRAMES])
    vmid_codes = [w & 0x0FFF for w in candidates if (w >> 12) == protocol.VMID_CHANNEL]
    if vmid_codes:
        m.v_d = float(code_to_volts(np.mean(vmid_codes), settings.vref))
    else:
        m.flags.append(NO_VMID)


def _alternate_equilibrium(keep: float, curve: float, q: float, tc: np.ndarray, tau: float):
    """Distances below V_D (d_short, d_long) at which the sensor settles after a short and after
    a long cycle, from iterating the fitted model d' = exp(-Tc / tau) * (keep d + curve d^2 + q)
    through the alternating sequence (Tc: mean of the shorter and of the longer half of the
    connected times). It is the nonlinear model's own fixed cycle, not a constant-Tc formula.
    A fit whose map has no stable cycle (a positive curvature with a large offset, from noisy
    data) diverges: the result is (nan, nan) instead of a made-up level."""
    keep, curve, q, tau = float(keep), float(curve), float(q), float(tau)
    middle = 0.5 * (tc.min() + tc.max())
    t_short, t_long = float(tc[tc <= middle].mean()), float(tc[tc > middle].mean())
    decay_short, decay_long = math.exp(-t_short / tau), math.exp(-t_long / tau)
    d_short = d_long = 0.0
    for _ in range(500):
        d_short_new = decay_short * (keep * d_long + curve * d_long * d_long + q)
        d_long_new = decay_long * (keep * d_short_new + curve * d_short_new * d_short_new + q)
        if not (abs(d_short_new) < 100.0 and abs(d_long_new) < 100.0):
            return float("nan"), float("nan")
        done = abs(d_short_new - d_short) + abs(d_long_new - d_long) < 1e-9
        d_short, d_long = d_short_new, d_long_new
        if done:
            break
    else:
        return float("nan"), float("nan")       # still moving after 500 cycles: no settling level
    return d_short, d_long


def analyze_alternate(burst: protocol.BurstData, r_bias: float,
                      settings: Optional[AnalysisSettings] = None,
                      since_last_ms: Optional[float] = None,
                      discharge_ms: float = 0.0,
                      fixed_c_total_f: Optional[float] = None) -> Measurement:
    """Analysis of a capalt burst: S sensor frames, then V Vmid frames, repeated.

    Each Vmid frame resets the shared node to V_D, so per cycle the sensor's distance
    below V_D, d = V_D - U (U: last sensor sample of the cycle), follows
        d_(k+1) = exp(-Tc / tau) * ((1 - c(d_k)) * d_k + q),   c(d) = c0 + c1 * d
    with Tc the connected time of the next cycle (from the time stamps), c = C_share /
    C_total the charge taken per connection as a capacitance reset to V_D, and q a
    fixed offset per connection. c grows with d because the once-per-frame charge does
    (on the board C_share was about 6.6 pF at V_D on four channels of 70-260 pF, and a
    constant c biased C_total by up to 6 %). One connected time cannot separate c from
    tau, so the burst must contain two (firmware 1.3.0 stretches every second cycle).
    For a given tau the model is linear in (1 - c0), c1 and q; tau is the one that
    minimises the squared error. C_total = tau / R_bias; c_share reports C_share at V_D (not the
    node capacitance C_D).
    The sensor start voltage does not enter, but it must leave a range: a sensor that
    starts within ALT_MIN_START_V of V_D (not rested) leaves tau undetermined."""
    settings = settings or AnalysisSettings()
    m = Measurement(burst.adc, burst.channel, float(r_bias))
    m.fit_model_requested = m.fit_model_used = FIT_ALT
    m.c_total_method = C_TOTAL_VMID_CYCLES
    lsb = settings.vref / 4096.0
    _header_checks(m, burst, settings, since_last_ms, discharge_ms)
    pattern = protocol.alternate_pattern(burst)
    n = min(burst.n_samples, burst.n_frames - protocol.LEAD_FRAMES)
    if pattern is None or n < 4:
        m.flags.append(INCOMPLETE)
        return m
    sensor_run, vmid_run = pattern
    length = sensor_run + vmid_run
    words = burst.words[protocol.LEAD_FRAMES:protocol.LEAD_FRAMES + n]
    expected = np.where(protocol.alternate_commands_sensor(n, sensor_run, vmid_run),
                        burst.channel, protocol.VMID_CHANNEL)
    tags = (words >> 12).astype(np.int64)
    if np.any(tags != expected):
        m.flags.append(TAG_MISMATCH)
    volts = code_to_volts(words & 0x0FFF, settings.vref)
    t_all = protocol.timestamps_to_seconds(burst.timestamps, burst.cpu_hz, origin_index=1)

    # Cycle j: commands j*L .. j*L+S-1 select the sensor; the mux connects it in frame
    # j*L+1 and releases it in frame j*L+S+1, whose CS fall samples U_j just before.
    cycles = n // length
    starts = np.arange(cycles) * length
    last = starts + sensor_run - 1
    u = volts[last]
    t_u = t_all[last + protocol.LEAD_FRAMES]
    connected = t_all[starts + sensor_run + 1] - t_all[starts + 1]
    valid = tags[last] == burst.channel
    m.t, m.v = t_u[valid], u[valid]
    # The model starts every cycle from V_D, so the reading that closes a cycle (its last Vmid
    # frame, tag 15) has to show the node back at V_D. A cycle that did not reset leaves its
    # successor starting from another level: that pair is left out.
    reset_index = starts + length - 1
    reset_ok = tags[reset_index] == protocol.VMID_CHANNEL

    m.v0 = float(u[0])
    m.connect_info = {"sensor_frames": float(sensor_run), "vmid_frames": float(vmid_run)}
    if not np.isfinite(m.v_d):
        m.flags.append(FIT_FAILED)
        return m

    reset_ok &= np.abs(volts[reset_index] - m.v_d) <= ALT_RESET_TOLERANCE_V
    if not reset_ok.all():
        m.flags.append(VMID_RESET_FAILED)
        m.connect_info["resets_failed"] = float((~reset_ok).sum())
    d = m.v_d - u
    k = np.flatnonzero(valid[:-1] & valid[1:] & reset_ok[:-1] & (d[:-1] > ALT_MIN_DISTANCE_V))
    m.n_used = int(len(k))
    m.amplitude_lsb = float(d[0] / lsb)
    if d[0] < ALT_MIN_START_V:
        m.flags.append(ALT_SMALL_RANGE)
    if len(k) < ALT_MIN_PAIRS:
        m.flags.append(FEW_SAMPLES)
        return m
    tc = connected[k + 1]
    if tc.max() < ALT_MIN_CONTRAST * tc.min():
        m.flags.append(FIT_FAILED)       # one connected time: tau and c are not separable
        return m
    d_now, d_next = d[k], d[k + 1]

    def solve(tau):
        decay = np.exp(-tc / tau)
        design = np.column_stack([decay * d_now, decay * d_now ** 2, decay])
        coef, _, _, _ = np.linalg.lstsq(design, d_next, rcond=None)
        error = d_next - design @ coef
        return float(error @ error), coef

    if fixed_c_total_f is not None:
        if not np.isfinite(fixed_c_total_f) or fixed_c_total_f <= 0:
            raise ValueError("fixed loaded capacitance must be finite and positive")
        tau = float(r_bias * fixed_c_total_f)
        m.connect_info["fixed_c_total_f"] = float(fixed_c_total_f)
    else:
        grid = np.geomspace(tc.min() * 0.2, tc.max() * 1e4, 400)
        errors = [solve(tau)[0] for tau in grid]
        best = int(np.argmin(errors))
        if best in (0, len(grid) - 1):
            m.flags.append(FIT_FAILED)
            return m
        low, high = np.log(grid[best - 1]), np.log(grid[best + 1])
        golden = (np.sqrt(5.0) - 1.0) / 2.0
        for _ in range(60):
            a, b = high - golden * (high - low), low + golden * (high - low)
            if solve(np.exp(a))[0] < solve(np.exp(b))[0]:
                high = b
            else:
                low = a
        tau = float(np.exp(0.5 * (low + high)))
    _, (keep, curve, q) = solve(tau)
    c = 1.0 - float(keep)                # at V_D; c1 = -curve

    m.tau = m.tau_slow = tau
    m.c_total = tau / r_bias
    # C_share is an effective connection capacitance (node plus the connected frame's
    # per-frame charge), not the node capacitance C_D and not a capacitance to subtract: it
    # has its own fields, and ratio, C_D, C sensor and C_PZT stay empty until C_n and the
    # sensor-side parasitics are measured (report step 5).
    m.c_share_ratio = c
    m.c_share = c * m.c_total
    d_short, d_long = _alternate_equilibrium(keep, curve, q, tc, tau)
    m.vinf = float(m.v_d - d_short)      # where the sensor settles after a short cycle
    predicted = m.v_d - np.exp(-connected[1:] / tau) * (keep * d[:-1] + curve * d[:-1] ** 2 + q)
    m.fitted = np.concatenate([[u[0]], predicted])[valid]
    # For the ln(d) view: d against the connected time only, and the model run from cycle 0
    # with the measured connected times (errors accumulate instead of restarting at each dot).
    free = np.empty(len(d))
    free[0] = d[0]
    with np.errstate(all="ignore"):
        for j in range(1, len(d)):
            free[j] = np.exp(-connected[j] / tau) * (keep * free[j - 1] + curve * free[j - 1] ** 2 + q)
    m.alt_connected_s = np.cumsum(connected)
    m.alt_d_v = np.where(valid & (d > 0), d, np.nan)
    m.alt_d_model_v = np.where(np.isfinite(free) & (free > 0) & (free < 100.0), free, np.nan)
    residuals = (u[k + 1] - predicted[k]) / lsb
    m.residuals_lsb = residuals
    m.residual_rms_lsb = float(np.sqrt(np.mean(residuals ** 2)))
    m.connect_info.update({"q_cycle_pc": float(q * m.c_total * 1e12),
                           "c_share_1v_pf": float((c - curve * 1.0) * m.c_total * 1e12),
                           "vinf_long_v": float(m.v_d - d_long),
                           "t_short_us": float(tc.min() * 1e6),
                           "t_long_us": float(tc.max() * 1e6), "pairs": float(len(k))})
    if m.amplitude_lsb < LOW_AMPLITUDE_LSB:
        m.flags.append(LOW_AMPLITUDE)
    # Each point is predicted from the previous, equally noisy, sample.
    if m.residual_rms_lsb > HIGH_RESIDUAL_LSB * np.sqrt(1.0 + keep ** 2):
        m.flags.append(HIGH_RESIDUAL)
    return m


def _fit_and_derive(m: Measurement, t: np.ndarray, v: np.ndarray, r_bias: float,
                    settings: AnalysisSettings, lsb: float) -> Measurement:
    """Fit t (s after the connection) / v (V) and derive tau, C_total, the step ratio and
    the quality flags into `m` (m.v_d, m.flags and m.n_used are set by the caller)."""
    # Fit relative to t_c (t is already measured from the connection instant).
    fit = fit_exponential(t, v)
    m.t, m.v = t, v
    m.fit_model_requested = settings.fit_model
    if not fit.ok or not np.isfinite(fit.tau) or fit.tau <= 0:
        m.flags.append(FIT_FAILED)
        return m
    fitted1 = fit.vinf - (fit.vinf - fit.v0) * np.exp(-t / fit.tau)
    rms1 = float(np.sqrt(np.mean(((v - fitted1) / lsb) ** 2)))
    m.tau_1exp, m.v0_1exp, m.rms_1exp = fit.tau, fit.v0, rms1
    m.c_total_1exp = fit.tau / r_bias

    fit2 = fit_two_exponentials(t, v, tau_hint=fit.tau)
    use_two = False
    fitted2 = np.zeros(0)
    if fit2.ok:
        fitted2 = v - fit2.residuals_v
        m.v0_2exp = fit2.v0
        m.tau_2exp = fit2.tau_slow
        m.c_total_2exp = fit2.tau_slow / r_bias
        m.rms_2exp = fit2.rms_residual_v / lsb
        needed = (fit2.tau_slow / fit2.tau_fast >= MIN_TAU_RATIO
                  and m.rms_2exp < MIN_RMS_IMPROVEMENT * rms1
                  and rms1 - m.rms_2exp > MIN_RMS_GAIN_LSB)
        use_two = settings.fit_model == FIT_TWO and needed

    if use_two:
        m.fit_model_used = FIT_TWO
        m.tau, m.v0, m.vinf = fit2.tau_slow, fit2.v0, fit2.vinf
        m.tau_slow, m.tau_fast, m.fast_fraction = fit2.tau_slow, fit2.tau_fast, fit2.fast_fraction
        m.fitted, m.residuals_lsb = fitted2, fit2.residuals_v / lsb
        m.fitted_other, m.other_label = fitted1, "single exponential"
    else:
        m.fit_model_used = FIT_ONE
        m.tau, m.v0, m.vinf = fit.tau, fit.v0, fit.vinf
        m.tau_slow = fit.tau
        m.fitted, m.residuals_lsb = fitted1, fit.residuals_v / lsb
        if fit2.ok:
            m.fitted_other, m.other_label = fitted2, "two exponentials"
    m.residual_rms_lsb = float(np.sqrt(np.mean(m.residuals_lsb ** 2)))
    m.amplitude_lsb = abs(m.vinf - m.v0) / lsb

    m.c_total = m.tau / r_bias
    m.c_total_shift_pct = (m.c_total / m.c_total_1exp - 1.0) * 100.0
    if np.isfinite(m.v_d) and abs(m.v_d - settings.v_s0) > 1e-9:
        m.ratio = (m.v0 - settings.v_s0) / (m.v_d - settings.v_s0)
        m.ratio_1exp = (m.v0_1exp - settings.v_s0) / (m.v_d - settings.v_s0)
        m.ratio_2exp = (m.v0_2exp - settings.v_s0) / (m.v_d - settings.v_s0)
        m.c_d = m.ratio * m.c_total
        m.c_sensor_side = m.c_total - m.c_d
        m.c_pzt = m.c_sensor_side - settings.c_parasitic_pf * 1e-12
        m.leak_na = (m.v_d - m.vinf) / r_bias * 1e9
        if abs(m.leak_na) > MAX_LEAK_NA:
            m.flags.append(VINF_OFFSET)

    if m.amplitude_lsb < LOW_AMPLITUDE_LSB:
        m.flags.append(LOW_AMPLITUDE)
    if m.residual_rms_lsb > HIGH_RESIDUAL_LSB:
        m.flags.append(HIGH_RESIDUAL)
    period = periodic_residual(m.residuals_lsb)
    if period is not None:
        m.periodic_frames = period
        m.flags.append(PERIODIC_RESIDUAL)
    return m



def analyze_record(record: "BurstRecord", r_bias: float,
                   settings: Optional[AnalysisSettings] = None,
                   since_last_ms: Optional[float] = None,
                   discharge_ms: float = 0.0) -> Measurement:
    """Analysis of one channel of one burst for any board (see BurstRecord)."""
    settings = settings or AnalysisSettings()
    m = Measurement(record.ref[0], record.ref[-1], float(r_bias))
    m.ref, m.label = tuple(record.ref), record.label
    lsb = settings.vref / 4096.0
    m.period_ns = dict(record.period_ns)
    m.flags.extend(record.flags)
    m.pre_t, m.pre_v = record.pre_t, record.pre_v
    m.connect_info = dict(record.connect_info)
    if since_last_ms is not None and since_last_ms < discharge_ms - 0.01:
        m.flags.append(TOO_SOON)
        m.flags.append(VS0_SUSPECT)
    if np.isfinite(record.v_d):
        m.v_d = float(record.v_d)
    else:
        m.flags.append(NO_VMID)
    m.n_used = int(len(record.t))
    if m.n_used < 16:
        m.flags.append(FEW_SAMPLES)
        if INCOMPLETE not in m.flags and m.n_used < 5:
            m.flags.append(INCOMPLETE)
        m.t, m.v = record.t, record.v
        return m
    return _fit_and_derive(m, np.asarray(record.t, dtype=np.float64),
                           np.asarray(record.v, dtype=np.float64), r_bias, settings, lsb)


@dataclass
class VinfReading:
    """Settling voltage of one sensor in Vinf mode, three ways (volts): measured with the sensor
    alternating with Vmid as in production (`v_alt`: median of the settled sensor samples),
    measured with the sensor held connected (`v_held`), and calculated by the
    Vmid-between-samples fit (`v_calc`)."""

    ref: tuple
    r_bias: float
    v_d: float = np.nan             # the Vmid level read before the connection
    v_alt: float = np.nan           # alternating with Vmid: median of the settled tail (mean over the bursts)
    v_alt_sd: float = np.nan        # sd of the per-burst medians (needs two bursts)
    alt_spread_lsb: float = np.nan  # robust spread of the tail samples
    alt_bursts: int = 0
    alt_cycles: int = 0             # sensor samples of the last alternating burst
    v_held: float = np.nan          # held connected: median of the settled tail (mean over the bursts)
    v_held_sd: float = np.nan
    held_spread_lsb: float = np.nan
    v_calc: float = np.nan          # Vinf of the alternating sequence's fitted model
    tau_calc: float = np.nan
    bursts: int = 0
    flags: List[str] = field(default_factory=list)
    label: str = ""
    t: np.ndarray = field(default_factory=lambda: np.zeros(0))     # last held burst, s after connection
    v: np.ndarray = field(default_factory=lambda: np.zeros(0))
    tail_start_s: float = np.nan
    alt_t: np.ndarray = field(default_factory=lambda: np.zeros(0))  # last alternating burst: the sensor samples
    alt_v: np.ndarray = field(default_factory=lambda: np.zeros(0))
    alt_tail_start_s: float = np.nan

    @property
    def offset_alt_mv(self) -> float:
        return (self.v_alt - self.v_d) * 1e3

    @property
    def alt_minus_calc_mv(self) -> float:
        return (self.v_alt - self.v_calc) * 1e3

    @property
    def offset_held_mv(self) -> float:
        return (self.v_held - self.v_d) * 1e3

    @property
    def offset_calc_mv(self) -> float:
        return (self.v_calc - self.v_d) * 1e3

    @property
    def held_minus_calc_mv(self) -> float:
        return (self.v_held - self.v_calc) * 1e3


def analyze_vinf(burst: protocol.BurstData, r_bias: float,
                 settings: Optional[AnalysisSettings] = None) -> VinfReading:
    """Settling voltage of a sensor held connected for the whole burst (a capburst long
    enough for it to charge): the median of the last VINF_TAIL_FRACTION of the samples. It
    is only a settling level if the quarter before it agrees (VINF_SETTLE_LSB), otherwise the
    reading is flagged VINF_NOT_SETTLED. No model is fitted."""
    settings = settings or AnalysisSettings()
    reading = VinfReading((burst.adc, burst.channel), float(r_bias))
    probe = Measurement(burst.adc, burst.channel, float(r_bias))
    _header_checks(probe, burst, settings, None, 0.0)       # V_D and the header flags
    reading.v_d, reading.flags = probe.v_d, [f for f in probe.flags if f != TOO_SOON]
    words = burst.words[protocol.LEAD_FRAMES:]
    stamps = burst.timestamps
    if len(words) < 32 or len(stamps) < len(words) + protocol.LEAD_FRAMES:
        reading.flags += [FEW_SAMPLES, INCOMPLETE]
        return reading
    tags = (words >> 12).astype(np.int64)
    wrong = tags != burst.channel
    if np.any(wrong[settings.discard_k:]):
        reading.flags.append(TAG_MISMATCH)
    fcd = (settings.first_clock_delay_ns if settings.first_clock_delay_ns is not None
           else burst.first_clock_delay_ns) * 1e-9
    delta = fcd + 1.5 / burst.spi_clock_hz
    t_all = protocol.timestamps_to_seconds(stamps, burst.cpu_hz, origin_index=1)
    t = t_all[protocol.LEAD_FRAMES:protocol.LEAD_FRAMES + len(words)] - delta
    volts = code_to_volts(words & 0x0FFF, settings.vref)
    keep = ~wrong
    keep[:settings.discard_k] = False
    t, volts = t[keep], volts[keep]
    reading.t, reading.v = t, volts
    quarter = int(len(volts) * VINF_TAIL_FRACTION)
    if quarter < 8:
        reading.flags += [FEW_SAMPLES]
        return reading
    tail, before = volts[-quarter:], volts[-2 * quarter:-quarter]
    lsb = settings.vref / 4096.0
    reading.v_held = float(np.median(tail))
    reading.held_spread_lsb = float(1.4826 * np.median(np.abs(tail - reading.v_held)) / lsb)
    reading.tail_start_s = float(t[-quarter])
    reading.bursts = 1
    if abs(np.median(before) - reading.v_held) > VINF_SETTLE_LSB * lsb:
        reading.flags.append(VINF_NOT_SETTLED)
    return reading


def analyze_vinf_alternating(burst: protocol.BurstData, r_bias: float,
                             settings: Optional[AnalysisSettings] = None) -> VinfReading:
    """Settling voltage of a sensor that alternates with Vmid (capalt with one connected time, as
    production runs it): the median of the sensor samples in the last VINF_TAIL_FRACTION of the
    burst, the quiet period after the sensor has settled. Flags: VINF_ALT_NOT_SETTLED when the
    quarter before it differs by more than VINF_SETTLE_LSB, VMID_RESET_FAILED when a Vmid reading
    in the tail is more than ALT_RESET_TOLERANCE_V from V_D. No model is fitted."""
    settings = settings or AnalysisSettings()
    reading = VinfReading((burst.adc, burst.channel), float(r_bias))
    probe = Measurement(burst.adc, burst.channel, float(r_bias))
    _header_checks(probe, burst, settings, None, 0.0)
    reading.v_d, reading.flags = probe.v_d, list(probe.flags)
    pattern = protocol.alternate_pattern(burst)
    n = min(burst.n_samples, burst.n_frames - protocol.LEAD_FRAMES)
    if pattern is None or n < 32:
        reading.flags += [FEW_SAMPLES, INCOMPLETE]
        return reading
    sensor_run, vmid_run = pattern
    words = burst.words[protocol.LEAD_FRAMES:protocol.LEAD_FRAMES + n]
    is_sensor = protocol.alternate_commands_sensor(n, sensor_run, vmid_run)
    tags = (words >> 12).astype(np.int64)
    if np.any(tags != np.where(is_sensor, burst.channel, protocol.VMID_CHANNEL)):
        reading.flags.append(TAG_MISMATCH)
    volts = code_to_volts(words & 0x0FFF, settings.vref)
    t_all = protocol.timestamps_to_seconds(burst.timestamps, burst.cpu_hz, origin_index=1)
    t = t_all[protocol.LEAD_FRAMES:protocol.LEAD_FRAMES + n]
    sensor = is_sensor & (tags == burst.channel)
    reading.alt_t, reading.alt_v = t[sensor], volts[sensor]
    reading.alt_cycles = int(sensor.sum())
    quarter = int(reading.alt_cycles * VINF_TAIL_FRACTION)
    if quarter < 8:
        reading.flags.append(FEW_SAMPLES)
        return reading
    tail, before = reading.alt_v[-quarter:], reading.alt_v[-2 * quarter:-quarter]
    lsb = settings.vref / 4096.0
    reading.v_alt = float(np.median(tail))
    reading.alt_spread_lsb = float(1.4826 * np.median(np.abs(tail - reading.v_alt)) / lsb)
    reading.alt_tail_start_s = float(reading.alt_t[-quarter])
    reading.alt_bursts = 1
    if abs(np.median(before) - reading.v_alt) > VINF_SETTLE_LSB * lsb:
        reading.flags.append(VINF_ALT_NOT_SETTLED)
    in_tail = (t >= reading.alt_tail_start_s) & ~is_sensor & (tags == protocol.VMID_CHANNEL)
    if np.any(np.abs(volts[in_tail] - reading.v_d) > ALT_RESET_TOLERANCE_V):
        reading.flags.append(VMID_RESET_FAILED)
    return reading


@dataclass
class BurstRecord:
    """Board-neutral burst data of ONE measured channel, input of analyze_record.

    t: sample times in seconds after the connection instant (samples already
    filtered: discarded first K, data errors removed); v: volts after conversion;
    v_d: the pre-connect baseline (V) or NaN; pre_t/pre_v: the baseline samples
    (time relative to the connection, negative) for plotting; flags: quality flags
    the board driver already knows (INCOMPLETE, FRAME_PERIOD_SHORT, ...)."""

    ref: tuple
    t: np.ndarray
    v: np.ndarray
    v_d: float = float("nan")
    pre_t: np.ndarray = field(default_factory=lambda: np.zeros(0))
    pre_v: np.ndarray = field(default_factory=lambda: np.zeros(0))
    flags: List[str] = field(default_factory=list)
    period_ns: Dict[str, float] = field(default_factory=dict)
    label: str = ""
    connect_info: Dict[str, float] = field(default_factory=dict)


@dataclass
class Aggregate:
    """Mean/std over the M bursts of one channel."""

    adc: int
    channel: int
    count: int
    mean: Dict[str, float]
    std: Dict[str, float]
    flags: List[str]
    method: str = C_TOTAL_TAU_OVER_R          # c_total_method of the bursts that were averaged
    methods_seen: Dict[str, int] = field(default_factory=dict)   # usable bursts per method


# Derived from V0 and the step model (see Measurement.step_valid).
_STEP_FIELDS = ("v0", "ratio", "c_d", "c_sensor_side", "c_pzt", "ratio_1exp", "ratio_2exp",
                "v0_1exp", "v0_2exp")

_AGG_FIELDS = ("leak_na", "tau", "c_total", "ratio", "c_d", "c_sensor_side", "c_pzt", "v0",
               "vinf", "residual_rms_lsb", "amplitude_lsb", "tau_fast", "tau_slow",
               "fast_fraction", "tau_1exp", "c_total_1exp", "c_total_shift_pct", "v0_1exp",
               "tau_2exp", "c_total_2exp", "ratio_1exp", "ratio_2exp",
               "v0_2exp", "rms_1exp", "rms_2exp", "c_share_ratio", "c_share")


def preferred_method(counts: Dict[str, int]) -> str:
    """The method whose bursts are averaged when a channel has several: the most usable bursts,
    then Vmid between samples (the one that gives a capacitance)."""
    if not counts:
        return C_TOTAL_TAU_OVER_R
    return max(counts, key=lambda name: (counts[name], name == C_TOTAL_VMID_CYCLES))


def aggregate(measurements: Sequence[Measurement], v0_rel_tolerance: float = 0.03,
              method: Optional[str] = None) -> Aggregate:
    """Mean and sd over the usable bursts of ONE c_total_method. Bursts taken by different
    methods (or conditions) measure different things, so their mean has no meaning: with
    `method` None the preferred method is averaged and the rest are left out, with the flag
    MIXED_METHODS; pass `method` to average one of them."""
    usable = [m for m in measurements if m.ok and np.isfinite(m.tau)]
    seen: Dict[str, int] = {}
    for m in usable:
        seen[m.c_total_method] = seen.get(m.c_total_method, 0) + 1
    explicit = method is not None
    method = method or preferred_method(seen)
    good = [m for m in usable if m.c_total_method == method]
    counted = {id(m) for m in good} | {id(m) for m in measurements} - {id(m) for m in usable}
    flags: List[str] = sorted({f for m in measurements if id(m) in counted for f in m.flags})
    if len(seen) > 1 and not explicit:
        flags.append(MIXED_METHODS)
    mean: Dict[str, float] = {}
    std: Dict[str, float] = {}
    for name in _AGG_FIELDS:
        values = np.array([getattr(m, name) for m in good], dtype=np.float64)
        values = values[np.isfinite(values)]
        mean[name] = float(values.mean()) if len(values) else float("nan")
        std[name] = float(values.std(ddof=1)) if len(values) > 1 else 0.0
    # Where the step values have no valid basis they are left out, not shown as numbers that
    # look plausible.
    if any(not m.step_valid for m in good):
        for name in _STEP_FIELDS:
            mean[name], std[name] = float("nan"), float("nan")
    # With Vmid between samples V0 is only the first sample: it follows the sensor's start
    # voltage, which that method does not need.
    alternating = bool(good) and all(m.fit_model_used == FIT_ALT for m in good)
    if len(good) > 1 and np.isfinite(mean["tau"]) and mean["tau"] > 0:
        if std["tau"] / mean["tau"] > UNSTABLE_TAU_FRACTION and UNSTABLE_TAU not in flags:
            flags.append(UNSTABLE_TAU)
    if len(good) > 1 and not alternating and np.isfinite(mean["v0"]) and mean["v0"] != 0:
        if std["v0"] / abs(mean["v0"]) > v0_rel_tolerance and V0_INCONSISTENT not in flags:
            flags.append(V0_INCONSISTENT)
            if VS0_SUSPECT not in flags:
                flags.append(VS0_SUSPECT)
    first = measurements[0]
    return Aggregate(first.adc, first.channel, len(good), mean, std, flags, method, seen)


def start_voltage_check(points: Dict[object, Sequence[float]],
                        v_d: float = 1.25) -> Dict[object, float]:
    """Estimate which sensors of one ADC started above 0 V.

    `points` maps a channel key to (V0, tau) of the fit in use. All channels of
    an ADC share the same C_D, so V0 = V_D * C_D / C_total is proportional to
    1/tau when every sensor starts at 0 V (C_total is proportional to tau for a
    given resistor). The robust constant K = median(V0 * tau) gives the expected
    V0 of every channel, size independent and without needing R_bias. The excess
    over it is a sensor starting charged: V0 = V_s0 + (V_D - V_s0) * r, so
    V_s0 = (V0 - r * V_D) / (1 - r) with r the expected V0 / V_D.

    Returns {key: estimated V_s0} (NaN when there are too few channels). A
    channel counts as high above VS0_THRESHOLD_V."""
    keys = [k for k, (v0, tau) in points.items()
            if np.isfinite(v0) and np.isfinite(tau) and tau > 0]
    result = {k: float("nan") for k in points}
    if len(keys) < VS0_MIN_CHANNELS:
        return result
    k_const = float(np.median([points[k][0] * points[k][1] for k in keys]))
    for key in keys:
        v0, tau = points[key]
        expected = k_const / tau
        r = min(max(expected / v_d, 0.0), 0.95)
        result[key] = float((v0 - r * v_d) / (1.0 - r))
    return result


def recommended_samples(r_bias: float, c_guess: float, period_s: float,
                        tau_multiples: float = 6.0, cap: int = 1024, floor: int = 32) -> int:
    """Samples covering `tau_multiples` * tau of the guessed C, capped."""
    n = int(np.ceil(tau_multiples * r_bias * c_guess / period_s)) + 1
    return max(floor, min(cap, n))


def plateau_check(waits_ms: Sequence[float], v0_values: Sequence[float],
                  rel_tolerance: float = 0.05, abs_tolerance_lsb: float = 3.0,
                  vref: float = 2.5, reference: Optional[float] = None) -> Dict[str, object]:
    """Does V0 versus wait time plateau? (Sensors discharge between bursts.)

    A value agrees with the final one when it is within max(`abs_tolerance_lsb`
    LSB, `rel_tolerance` of the scale). The scale is the final V0, or, when a
    `reference` voltage (the pre-connect level V_D) is given, the step
    |V0_final - reference|: a sensor that stays near V_D has a tiny step, and 5 % of
    the absolute level would hide a drift of tens of millivolts. The plateau needs the
    last two points to agree; `sufficient_wait_ms` is the shortest wait that already agrees."""
    waits = list(waits_ms)
    values = np.array(list(v0_values), dtype=np.float64)
    final = values[-1]
    scale = abs(final) if reference is None else abs(final - reference)
    tolerance = max(abs_tolerance_lsb * vref / 4096.0, rel_tolerance * scale)
    plateau = bool(len(values) >= 2 and abs(values[-1] - values[-2]) <= tolerance)
    sufficient = None
    for wait, value in zip(waits, values):
        if abs(value - final) <= tolerance:
            sufficient = wait
            break
    return {"plateau": plateau, "sufficient_wait_ms": sufficient,
            "v0_final": float(final), "tolerance_v": float(tolerance)}
