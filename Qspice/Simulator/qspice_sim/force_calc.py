"""Force calculation from the ADC values: the ideal charge model of a PZT sensor read as a voltage.

    open-circuit voltage          V = d33 * F / C                  C = C_sensor + C_extra
    change in force               dF[k] = (V[k] - V[k-1]) * C / d33           (consecutive conversions of one sensor)
    total force                   F[k] = sum(dF[0..k])
    sensitivity / resolution      S = d33 / C ,  dF_LSB = lsb * C / d33

The ideal model ignores charge sharing with the multiplexer node, leakage and bias-resistor decay, so F drifts and differs from
the applied force wherever the real circuit deviates; comparing the two is the point.

With leakage compensation (default, when the model supplies the data) the charge the sensor actually generated is recovered:
the voltage of the previous conversion is propagated through the phases between two conversions of one sensor (connected,
floating, connected: exponential decay towards the rest voltage of each phase, and the charge sharing with the mux node at
every re-connection) and only the difference to the next conversion is attributed to new charge, divided by the response h to
the new charge through the same phases (uniform arrival). See ``extract_force_leaky``. Pure numpy, no GUI.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

import numpy as np

from .adc_model import SampleTable

REF_VMID = "VMID"
REF_FIRST = "first sample"
SRC_ADC_OUT = "ADC-Out (quantised, with noise)"
SRC_IDEAL = "ideal ADC input"

FORCE_DEFAULTS: dict = {
    "d33_pC_per_N": None,       # None = follow the component settings of each sensor
    "cp_pF": None,
    "cextra_pF": None,
    "reference": REF_VMID,
    "source": SRC_ADC_OUT,
    "channels": None,           # None = every channel that has conversions
    "min_fin_N": None,          # None = 1 % of the largest |Fin| of the channel in the run
    "leak_comp": True,          # compensate leakage and charge sharing (needs sensor_info()["leak"] and the sample windows)
    "c_side_pF": None,          # sensor-side parasitic capacitance of the leakage model (None = follow the model)
    "r_conn_ohm": None,         # leak resistance of the node while the sensor is connected (None = follow the model)
    "r_float_ohm": None,        # leak resistance of the floating sensor (None = follow the model)
    "i_float_nA": None,         # constant leakage current of the floating sensor (None = follow the model)
}

RATIO_FRACTION = 0.01


@dataclass(frozen=True)
class LeakParams:
    """Leakage and charge-sharing model of one sensor (all SI units)."""

    c_side: float               # F, sensor-side parasitics (trace, off-state pin) added to the sensor capacitance
    c_node: float               # F, shared mux node that the sensor connects to
    r_conn: float               # ohm, leak resistance of the connected node (bias resistor); inf = none
    v_conn: float               # V, rest voltage while connected (VMID)
    i_conn: float               # A, constant current drawn from the connected node
    r_float: float              # ohm, leak resistance of the floating sensor (insulation); inf = none
    v_float: float              # V, rest voltage of the floating sensor
    i_float: float              # A, constant current drawn from the floating sensor (positive = discharging)


@dataclass(frozen=True)
class SensorParams:
    d33: float                  # C/N, includes the force gain of the component
    c_sensor: float            # F
    c_extra: float             # F, capacitance the node adds in parallel to the sensor
    leak: LeakParams | None = None

    @property
    def c_total(self) -> float:
        return self.c_sensor + self.c_extra

    @property
    def c_float(self) -> float:
        """Capacitance of the floating sensor in the leakage model."""
        return self.c_sensor + (self.leak.c_side if self.leak else 0.0)

    @property
    def c_conn(self) -> float:
        """Capacitance of the sensor joined with the mux node in the leakage model."""
        return self.c_float + (self.leak.c_node if self.leak else 0.0)

    @property
    def rho(self) -> float:
        """Share of the sensor charge that stays on the sensor when it connects to the (discharged) mux node."""
        return self.c_float / self.c_conn

    @property
    def sensitivity(self) -> float:
        """Open-circuit sensitivity d33 / C in V/N."""
        return self.d33 / self.c_total

    def force_per_volt(self) -> float:
        """dF per volt of change at the ADC input: C / d33 in N/V (C = connected capacitance with the leakage model)."""
        return (self.c_conn if self.leak else self.c_total) / self.d33


def resolve_params(settings: dict, info: dict) -> SensorParams:
    """Sensor values of the force calculation: the user's override where set, else the model's component value."""
    d33 = info["d33_C_per_N"] if settings.get("d33_pC_per_N") is None else float(settings["d33_pC_per_N"]) * 1e-12
    c_sensor = info["cp_F"] if settings.get("cp_pF") is None else float(settings["cp_pF"]) * 1e-12
    c_extra = info.get("cextra_F", 0.0) if settings.get("cextra_pF") is None else float(settings["cextra_pF"]) * 1e-12
    if c_sensor + c_extra <= 0:
        raise ValueError("the total capacitance (sensor + extra) must be positive")
    if d33 == 0:
        raise ValueError("d33 must not be zero")
    return SensorParams(d33=d33, c_sensor=c_sensor, c_extra=c_extra, leak=_resolve_leak(settings, info, c_sensor))


def _resolve_leak(settings: dict, info: dict, c_sensor: float) -> LeakParams | None:
    """Leakage model of the sensor: the model's values with the user's overrides; None = ideal charge model."""
    raw = info.get("leak")
    if not raw or not settings.get("leak_comp", True):
        return None

    def pick(key: str, raw_key: str, scale: float) -> float:
        return float(raw[raw_key]) if settings.get(key) is None else float(settings[key]) * scale

    c_node = float(raw["c_node_F"]) if settings.get("cextra_pF") is None else float(settings["cextra_pF"]) * 1e-12
    leak = LeakParams(c_side=pick("c_side_pF", "c_side_F", 1e-12), c_node=c_node,
                      r_conn=pick("r_conn_ohm", "r_conn_ohm", 1.0), v_conn=float(raw["v_conn_V"]),
                      i_conn=float(raw.get("i_conn_A", 0.0)),
                      r_float=pick("r_float_ohm", "r_float_ohm", 1.0), v_float=float(raw["v_float_V"]),
                      i_float=pick("i_float_nA", "i_float_A", 1e-9))
    if leak.c_side < 0 or leak.c_node < 0:
        raise ValueError("the sensor-side and node capacitances of the leakage model must not be negative")
    if leak.r_conn <= 0 or leak.r_float <= 0:
        raise ValueError("the leak resistances must be positive (use a very large value for no leakage)")
    if c_sensor + leak.c_side <= 0:
        raise ValueError("the capacitance of the floating sensor must be positive")
    return leak


@dataclass
class ChannelForce:
    address: int
    params: SensorParams
    t: np.ndarray               # conversion instants of this channel
    v: np.ndarray               # voltage used (ADC-Out or ideal input)
    dF: np.ndarray
    F: np.ndarray
    fin: np.ndarray | None      # applied force at the conversion instants (None when the force node was not saved)
    clipped: int = 0
    ratio: np.ndarray | None = None     # F / Fin, NaN where |Fin| is below the threshold
    ratio_threshold: float = 0.0
    leak_used: bool = False             # the leakage / charge-sharing compensation was applied
    leak_missing: str = ""              # why a requested compensation could not be applied
    unreset: int = 0                    # conversions whose mux node was not parked on VMID before the connection
    n_const: int = 0                    # conversions after the first one that follow the previous one on a constant connection
    n_switched: int = 0                 # conversions after the first one that follow a release and a re-connection of the mux

    @property
    def error_final(self) -> float | None:
        return None if self.fin is None or not len(self.F) else float(self.F[-1] - self.fin[-1])


def extract_force(v: np.ndarray, params: SensorParams, v_start: float) -> tuple[np.ndarray, np.ndarray]:
    """(dF, F) of a sequence of voltages of one sensor; v_start is the voltage before the first conversion."""
    v = np.asarray(v, dtype=float)
    previous = np.concatenate([[v_start], v[:-1]]) if len(v) else v
    dF = (v - previous) * params.force_per_volt()
    return dF, np.cumsum(dF)


def _phase(v0, d, c: float, r: float, vr: float, i: float):
    """One first-order phase ``C dV/dt = -(V - vr)/r - i + q``: (V after d, decay factor, dV per unit charge rate q).

    V(d) = V_inf + (V0 - V_inf) exp(-d/rc), V_inf = vr - i r; r = inf is a pure drift V0 - i d / c.
    """
    d = np.maximum(np.asarray(d, dtype=float), 0.0)
    if np.isinf(r):
        return v0 - i * d / c, np.ones_like(d), d / c
    tau = r * c
    x = d / tau
    a = np.exp(-x)
    v_inf = vr - i * r
    return v_inf + (v0 - v_inf) * a, a, tau * (-np.expm1(-x)) / c


def extract_force_leaky(v: np.ndarray, t: np.ndarray, t_on: np.ndarray, t_off: np.ndarray, params: SensorParams, vmid: float,
                        first_is_zero: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """(dF, F) from the charge the sensor generated: leakage and charge sharing between the conversions are compensated.

    ``t`` are the conversion instants of one sensor, ``t_on`` / ``t_off`` the instants the sensor was connected / released
    (the connection that holds the conversion). Between conversions k-1 and k (T apart) the sensor goes through
    connected (d1 = t_off[k-1] - t[k-1]) -> floating (d2 = t_on[k] - t_off[k-1]) -> re-connection with charge sharing
    (V = rho V + (1 - rho) V_conn) -> connected (d3 = t[k] - t_on[k]); a sensor that stays connected is one connected phase.
    V_pred is the voltage at conversion k without new charge, h the voltage per unit charge rate of new charge (uniform arrival),
    so ``dQ[k] = T (V[k] - V_pred) / h`` and ``dF[k] = dQ[k] / d33``.
    """
    L = params.leak
    v = np.asarray(v, dtype=float)
    n = len(v)
    dq = np.zeros(n)
    if n == 0:
        return dq, dq
    cs, ct, rho = params.c_float, params.c_conn, params.rho
    if not first_is_zero:
        # the sensor starts at the reference voltage; its new charge is assumed to arrive just before the connection
        v_pred, a3, _ = _phase(vmid, t[0] - t_on[0], ct, L.r_conn, L.v_conn, L.i_conn)
        dq[0] = (v[0] - float(v_pred)) * ct / float(a3)
    if n > 1:
        t_prev, t_cur = t[:-1], t[1:]
        interval = t_cur - t_prev
        same = t_on[1:] <= t_prev                     # the window of conversion k already holds conversion k-1
        v_same, _, g_same = _phase(v[:-1], interval, ct, L.r_conn, L.v_conn, L.i_conn)
        v1, _, g1 = _phase(v[:-1], t_off[:-1] - t_prev, ct, L.r_conn, L.v_conn, L.i_conn)
        v2, a2, g2 = _phase(v1, t_on[1:] - t_off[:-1], cs, L.r_float, L.v_float, L.i_float)
        v3 = rho * v2 + (1.0 - rho) * L.v_conn
        v4, a3, g3 = _phase(v3, t_cur - t_on[1:], ct, L.r_conn, L.v_conn, L.i_conn)
        g_rc = g1 * a2 * rho * a3 + g2 * rho * a3 + g3
        v_pred = np.where(same, v_same, v4)
        h = np.where(same, g_same, g_rc)
        h = np.where(h > 0, h, interval / ct)          # degenerate windows: ideal response
        dq[1:] = interval * (v[1:] - v_pred) / h
    dF = dq / params.d33
    return dF, np.cumsum(dF)


def force_ratio(F: np.ndarray, fin: np.ndarray, threshold: float) -> np.ndarray:
    """Fcal / Fin: 1 = perfect extraction, below 1 = under-estimated, above 1 = over-estimated. Undefined (NaN) where |Fin| is
    below the threshold; an exact zero is always masked."""
    ok = (np.abs(fin) >= threshold) & (fin != 0)
    out = np.full(len(F), np.nan)
    out[ok] = F[ok] / fin[ok]
    return out


def compute_channel(table: SampleTable, address: int, params: SensorParams, settings: dict, vmid: float,
                    fin_at: Callable[[int, np.ndarray], np.ndarray | None] | None = None,
                    fin_peak: Callable[[int], float | None] | None = None) -> ChannelForce | None:
    """Force of one channel from the conversion table (None when the channel has no conversion in the run).

    ``fin_peak(channel)`` is the largest |applied force| of the channel in the run; it sets the default ratio threshold
    (1 % of it) when the settings give no minimum |Fin|.
    """
    mask = table.channel_mask(address)
    if not mask.any():
        return None
    v = table.v_out[mask] if settings.get("source", SRC_ADC_OUT) == SRC_ADC_OUT else table.v_in[mask]
    first = settings.get("reference", REF_VMID) == REF_FIRST
    v_start = float(v[0]) if first else float(vmid)
    t = table.t[mask]
    leak_used, missing, unreset, n_const, n_switched = False, "", 0, 0, 0
    ideal = replace(params, leak=None)
    if params.leak is None:
        dF, F = extract_force(v, params, v_start)
    else:
        t_on = None if table.t_on is None else table.t_on[mask]
        t_off = None if table.t_off is None else table.t_off[mask]
        if t_on is None or t_off is None or not (np.isfinite(t_on).all() and np.isfinite(t_off).all()):
            missing = "the model gives no connection times for the conversions"
            dF, F = extract_force(v, ideal, v_start)
        else:
            dF, F = extract_force_leaky(v, t, t_on, t_off, params, float(vmid), first)
            leak_used = True
            n_const = int((t_on[1:] <= t[:-1]).sum())
            n_switched = len(t) - 1 - n_const if len(t) else 0
            if table.node_reset is not None:
                unreset = int((~table.node_reset[mask]).sum())
    fin = fin_at(address, t) if fin_at else None
    cf = ChannelForce(address, params, t, v, dF, F, fin, clipped=int(table.clipped[mask].sum()), leak_used=leak_used,
                      leak_missing=missing, unreset=unreset, n_const=n_const, n_switched=n_switched)
    if fin is not None:
        threshold = settings.get("min_fin_N")
        if threshold is None:
            peak = fin_peak(address) if fin_peak else None
            threshold = RATIO_FRACTION * (float(np.max(np.abs(fin))) if peak is None else float(peak))
        cf.ratio_threshold = float(threshold)
        cf.ratio = force_ratio(F, fin, cf.ratio_threshold)
    return cf


def compute_all(table: SampleTable, channels: list[int], infos: dict[int, dict], settings: dict, vmid: float,
                fin_at: Callable[[int, np.ndarray], np.ndarray | None] | None = None,
                fin_peak: Callable[[int], float | None] | None = None) -> dict[int, ChannelForce]:
    out: dict[int, ChannelForce] = {}
    for ch in channels:
        if ch not in infos:
            continue
        cf = compute_channel(table, ch, resolve_params(settings, infos[ch]), settings, vmid, fin_at, fin_peak)
        if cf is not None:
            out[ch] = cf
    return out


def summary_row(cf: ChannelForce) -> list:
    """[channel, conversions, clipped, dF min, dF max, F min, F max, F final, Fin final, error final]"""
    fin_final = None if cf.fin is None else float(cf.fin[-1])
    return [cf.address, len(cf.t), cf.clipped, float(cf.dF.min()), float(cf.dF.max()), float(cf.F.min()), float(cf.F.max()),
            float(cf.F[-1]), fin_final, cf.error_final]


def _eng(value: float, unit: str, scale: float = 1.0) -> str:
    return f"{value * scale:.4g} {unit}"


def _leak_formulas_html(params: SensorParams, live: str, first: str, source: str, cf: ChannelForce | None = None) -> str:
    """Formula panel of the leakage-compensated charge model: the breakdown of the node, the full equation, the mux-switching
    case and the constant-connection case, each with the numbers of the shown channel."""
    L = params.leak
    tau_c = L.r_conn * params.c_conn
    tau_f = L.r_float * params.c_float
    cs, cn, ct = params.c_float, L.c_node, params.c_conn

    breakdown = (
        "<table border='1' cellspacing='0' cellpadding='3'>"
        "<tr><th align='left'>Part</th><th align='left'>Current into the node C dV/dt</th><th align='left'>Value of this channel</th></tr>"
        "<tr><td><b>Charge from the PZT</b></td><td>+q = d33 &middot; dF/dt, the charge rate in A (the unknown that is recovered; dQ = q &middot; T)</td>"
        f"<td>d33 = {_eng(params.d33, 'pC/N', 1e12)}</td></tr>"
        "<tr><td><b>Leakage through the bias resistor</b> (sensor connected)</td>"
        "<td>&minus;(V &minus; V<sub>conn</sub>) / R<sub>conn</sub></td>"
        f"<td>R<sub>conn</sub> = {_eng(L.r_conn, 'ohm')}, V<sub>conn</sub> = {_eng(L.v_conn, 'V')}, "
        f"&tau;<sub>conn</sub> = R<sub>conn</sub>&middot;C<sub>t</sub> = {_eng(tau_c, 'ms', 1e3)}</td></tr>"
        "<tr><td><b>Leakage from the mux drain / ADC pin</b> (sensor connected)</td><td>&minus;I<sub>conn</sub> (constant)</td>"
        f"<td>I<sub>conn</sub> = {_eng(L.i_conn, 'nA', 1e9)}</td></tr>"
        "<tr><td><b>Insulation leakage</b> (sensor floating)</td><td>&minus;(V &minus; V<sub>float</sub>) / R<sub>float</sub></td>"
        f"<td>R<sub>float</sub> = {_eng(L.r_float, 'ohm')}, V<sub>float</sub> = {_eng(L.v_float, 'V')}, "
        f"&tau;<sub>float</sub> = R<sub>float</sub>&middot;C<sub>s</sub> = {_eng(tau_f, 'ms', 1e3)}</td></tr>"
        "<tr><td><b>Mux off-leakage / pin leakage</b> (sensor floating)</td><td>&minus;I<sub>float</sub> (constant)</td>"
        f"<td>I<sub>float</sub> = {_eng(L.i_float, 'nA', 1e9)}</td></tr>"
        "<tr><td><b>Charge sharing</b> at every (re)connection to the mux node</td>"
        "<td>not a current: V &rarr; &rho;&middot;V + (1 &minus; &rho;)&middot;V<sub>conn</sub>, &rho; = C<sub>s</sub> / C<sub>t</sub></td>"
        f"<td>C<sub>s</sub> = {_eng(cs, 'pF', 1e12)} (sensor {_eng(params.c_sensor, 'pF', 1e12)} + side {_eng(L.c_side, 'pF', 1e12)}), "
        f"C<sub>n</sub> = {_eng(cn, 'pF', 1e12)}, C<sub>t</sub> = C<sub>s</sub> + C<sub>n</sub> = {_eng(ct, 'pF', 1e12)}, "
        f"&rho; = {params.rho:.4g}</td></tr>"
        "</table>"
        "<p>The op-amp input bias current is not a separate term (it is in the pA range); the follower bootstraps the op-amp "
        "differential capacitance, so only its common-mode capacitance is part of C<sub>n</sub>.</p>")

    interval = ""
    if cf is not None and len(cf.t) > 1:
        interval = f" For this channel T = {float(np.median(np.diff(cf.t))) * 1e6:.4g} &micro;s (median)."
    const_numbers = ""
    if cf is not None and len(cf.t) > 1:
        T = float(np.median(np.diff(cf.t)))
        a = float(np.exp(-T / tau_c))
        h = L.r_conn * (1.0 - a)
        const_numbers = (f"<br>With T = {T * 1e6:.4g} &micro;s: a = e<sup>&minus;T/&tau;<sub>conn</sub></sup> = {a:.6g}, "
                         f"h = R<sub>conn</sub>&middot;(1 &minus; a) = {h:.4g} &Omega;, T / &tau;<sub>conn</sub> = {T / tau_c:.4g} "
                         "(the small-T form above is valid when this is much smaller than 1)")
    used = ""
    if cf is not None and cf.leak_used:
        used = (f"<p><b>In this run, channel {cf.address}</b>: {cf.n_switched} conversion(s) follow the mux switching (equation 4), "
                f"{cf.n_const} follow on a constant connection (equation 5); the first conversion uses the first-sample rule.</p>")

    return (
        "<p><b>Charge model with leakage and charge-sharing compensation</b></p>"
        "<p><b>1. What is measured and what is wanted.</b> The ADC reads the node voltage <b>V = Q<sub>node</sub> / C</b> at each "
        "conversion, not the charge the PZT generated. A free sensor gives the open-circuit voltage V = d33 &middot; F / C, so a "
        "change in force dF generates the charge <b>dQ = d33 &middot; dF</b>. Between two conversions part of that charge leaks "
        "away and part of it is shared with the mux node. The calculation reverses both effects: it finds dQ[k], the charge "
        f"generated between conversions k&minus;1 and k, and then dF[k] = dQ[k] / d33. V is the {source}.</p>"
        f"<p><b>2. Breakdown of the node.</b></p>{breakdown}"
        "<p><b>3. Equation of one phase</b> (the sensor is either connected or floating during a phase of length d):<br>"
        "<b>C &middot; dV/dt = q &minus; (V &minus; V<sub>rest</sub>) / R &minus; I</b>, with C, R, V<sub>rest</sub>, I of the "
        "phase (table above). Every term is a current (current balance at the node): C&middot;dV/dt is the current that charges "
        "the node capacitance, <b>q</b> is the <b>charge rate of the PZT in amperes</b>, q = d33&middot;dF/dt, not a charge. "
        "Between two conversions q is assumed constant, so <b>q = dQ[k] / T</b> and the charge generated in the interval is "
        "<b>dQ[k] = q &middot; T</b>.<br>"
        "Without new charge (q = 0): <b>V(d) = V<sub>inf</sub> + (V<sub>0</sub> &minus; V<sub>inf</sub>) &middot; a(d)</b>, "
        "a(d) = e<sup>&minus;d/&tau;</sup>, &tau; = R&middot;C, V<sub>inf</sub> = V<sub>rest</sub> &minus; I&middot;R "
        "(no resistor: V(d) = V<sub>0</sub> &minus; I&middot;d / C).<br>"
        "A charge arriving at the rate q during the phase adds <b>g(d) &middot; q</b> to the voltage at its end, "
        "<b>g(d) = &tau;&middot;(1 &minus; a(d)) / C = R&middot;(1 &minus; a(d))</b> (d / C without resistor).</p>"
        "<p><b>4. Mux switching</b> (the sensor is released after conversion k&minus;1 and connected again before conversion k; "
        "T = t[k] &minus; t[k&minus;1]):<br>"
        "&nbsp;&nbsp;connected, d<sub>1</sub> = t<sub>off</sub>[k&minus;1] &minus; t[k&minus;1]: "
        "V<sub>1</sub> = phase(V[k&minus;1]; C<sub>t</sub>, R<sub>conn</sub>, V<sub>conn</sub>, I<sub>conn</sub>), g<sub>1</sub><br>"
        "&nbsp;&nbsp;floating, d<sub>2</sub> = t<sub>on</sub>[k] &minus; t<sub>off</sub>[k&minus;1]: "
        "V<sub>2</sub> = phase(V<sub>1</sub>; C<sub>s</sub>, R<sub>float</sub>, V<sub>float</sub>, I<sub>float</sub>), a<sub>2</sub>, g<sub>2</sub><br>"
        "&nbsp;&nbsp;re-connection (node parked on VMID): V<sub>3</sub> = &rho;&middot;V<sub>2</sub> + (1 &minus; &rho;)&middot;V<sub>conn</sub><br>"
        "&nbsp;&nbsp;connected, d<sub>3</sub> = t[k] &minus; t<sub>on</sub>[k]: "
        "V<sub>pred</sub>[k] = phase(V<sub>3</sub>; C<sub>t</sub>, R<sub>conn</sub>, V<sub>conn</sub>, I<sub>conn</sub>), a<sub>3</sub>, g<sub>3</sub><br>"
        "V<sub>pred</sub>[k] is the voltage the ADC would show if the sensor generated no new charge. Charge that arrives in "
        "phase 1 still has to survive the floating decay, the sharing and the last decay, charge from phase 2 the sharing and the "
        "last decay, charge from phase 4 nothing:<br>"
        "<b>h[k] = g<sub>1</sub>&middot;a<sub>2</sub>&middot;&rho;&middot;a<sub>3</sub> + g<sub>2</sub>&middot;&rho;&middot;a<sub>3</sub> + g<sub>3</sub></b>, "
        "<b>dQ[k] = T &middot; (V[k] &minus; V<sub>pred</sub>[k]) / h[k]</b></p>"
        "<p><b>5. Constant connection</b> (the sensor stays connected to the mux node between the two conversions; no floating "
        "phase, no sharing):<br>"
        "<b>V<sub>pred</sub>[k] = V<sub>inf</sub> + (V[k&minus;1] &minus; V<sub>inf</sub>) &middot; a</b>, "
        "V<sub>inf</sub> = V<sub>conn</sub> &minus; I<sub>conn</sub>&middot;R<sub>conn</sub>, a = e<sup>&minus;T/&tau;<sub>conn</sub></sup>, "
        "&tau;<sub>conn</sub> = R<sub>conn</sub>&middot;C<sub>t</sub><br>"
        "<b>h = R<sub>conn</sub>&middot;(1 &minus; a)</b>, <b>dQ[k] = T &middot; (V[k] &minus; V<sub>pred</sub>[k]) / h</b><br>"
        "For T &lt;&lt; &tau;<sub>conn</sub>: <b>dQ[k] &asymp; C<sub>t</sub>&middot;(V[k] &minus; V[k&minus;1]) + "
        "((V[k&minus;1] &minus; V<sub>conn</sub>) / R<sub>conn</sub> + I<sub>conn</sub>)&middot;T</b>: the charge seen in the ADC "
        f"change plus the charge that leaked through R<sub>conn</sub> and I<sub>conn</sub> during T.{interval}{const_numbers}</p>"
        f"{used}"
        "<p><b>6. Result.</b> <b>dF[k] = dQ[k] / d33</b>, <b>F[k] = &Sigma; dF[0..k]</b>, starting at 0. The first conversion "
        f"starts from V[&minus;1] = {first}: V<sub>pred</sub>[0] is the reference decayed over the connection time before the "
        "conversion and dQ[0] = (V[0] &minus; V<sub>pred</sub>[0]) &middot; C<sub>t</sub> / a (the charge is assumed to arrive just "
        "before the connection); with the reference <i>first sample</i> dF[0] = 0. Without leakage and node capacitance every "
        "a is 1, &rho; is 1 and h = T / C, which is the ideal formula dF = &Delta;V &middot; C / d33.</p>"
        f"{live}"
        "<p style='color:#a05000'>Linear first-order model of the node with one effective node capacitance, new charge arriving "
        "uniformly between two conversions. Not compensated: the charge left on the mux node by a previous channel (no VMID visit "
        "between channels) and the voltage dependence of the leakage (evaluated at VMID).</p>")


def formulas_html(params: SensorParams | None, adc_lsb: float | None, channel: int | None, reference: str, source: str,
                  cf: ChannelForce | None = None) -> str:
    """Formula panel with the live numbers of one channel substituted."""
    live = ""
    if params is not None:
        lsb_txt = ""
        if adc_lsb is not None:
            lsb_txt = (f"<br>Force per LSB: dF<sub>LSB</sub> = lsb &middot; C / d33 = {adc_lsb * 1e3:.4g} mV "
                       f"&middot; {params.force_per_volt():.4g} N/V = <b>{adc_lsb * params.force_per_volt() * 1e3:.4g} mN</b>")
        live = (f"<p><b>Values for channel {channel}</b>: d33 = {params.d33 * 1e12:.4g} pC/N, "
                f"C = {params.c_sensor * 1e12:.4g} pF + {params.c_extra * 1e12:.4g} pF = {params.c_total * 1e12:.4g} pF"
                f"<br>Sensitivity: S = d33 / C = <b>{params.sensitivity:.4g} V/N</b>{lsb_txt}</p>")
    first = "the first sample (dF[0] = 0)" if reference == REF_FIRST else "VMID"
    if params is not None and params.leak is not None:
        return _leak_formulas_html(params, live, first, source, cf)
    return (
        "<p><b>Ideal charge model</b></p>"
        "<p>Open-circuit sensor voltage: <b>V = d33 &middot; F / C</b>, with C = C<sub>sensor</sub> + C<sub>extra</sub>; "
        "a change in force dF generates the charge dQ = d33 &middot; dF, which the node shows as dV = dQ / C.</p>"
        "<p>Change in force between two consecutive conversions of the same sensor:<br>"
        f"<b>dF[k] = (V[k] &minus; V[k&minus;1]) &middot; C / d33</b>, V[&minus;1] = {first}; V is the {source}</p>"
        "<p>Total force (discrete integration): <b>F[k] = &Sigma; dF[0..k]</b>, starting at 0</p>"
        f"{live}"
        "<p style='color:#a05000'>This is the ideal charge model. It ignores the leakage through the bias resistor, the mux and "
        "pin leakage, the insulation leakage and the charge sharing with the multiplexer node, so F drifts and differs from the "
        "applied force when the real circuit deviates; that difference is what the comparison shows. Tick <i>Compensate leakage "
        "and charge sharing</i> to include these effects.</p>")
