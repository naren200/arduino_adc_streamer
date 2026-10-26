"""Force calculation from the ADC values: the ideal charge model of a PZT sensor read as a voltage.

    open-circuit voltage          V = d33 * F / C                  C = C_sensor + C_extra
    change in force               dF[k] = (V[k] - V[k-1]) * C / (d33 * G)     (consecutive conversions of one sensor)
    total force                   F[k] = sum(dF[0..k])
    sensitivity / resolution      S = d33 / C ,  dF_LSB = lsb * C / (d33 * G)

It ignores charge sharing with the multiplexer node, leakage and bias-resistor decay, so F drifts and differs from the applied
force wherever the real circuit deviates; comparing the two is the point. Pure numpy, no GUI.
"""
from __future__ import annotations

from dataclasses import dataclass
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
    "gain": 1.0,
    "reference": REF_VMID,
    "source": SRC_ADC_OUT,
    "channels": None,           # None = every channel that has conversions
}


@dataclass(frozen=True)
class SensorParams:
    d33: float                  # C/N, includes the force gain of the component
    c_sensor: float            # F
    c_extra: float             # F, capacitance the node adds in parallel to the sensor
    gain: float                # front-end gain G

    @property
    def c_total(self) -> float:
        return self.c_sensor + self.c_extra

    @property
    def sensitivity(self) -> float:
        """Open-circuit sensitivity d33 / C in V/N."""
        return self.d33 / self.c_total

    def force_per_volt(self) -> float:
        """dF per volt of change at the ADC input: C / (d33 * G) in N/V."""
        return self.c_total / (self.d33 * self.gain)


def resolve_params(settings: dict, info: dict) -> SensorParams:
    """Sensor values of the force calculation: the user's override where set, else the model's component value."""
    d33 = info["d33_C_per_N"] if settings.get("d33_pC_per_N") is None else float(settings["d33_pC_per_N"]) * 1e-12
    c_sensor = info["cp_F"] if settings.get("cp_pF") is None else float(settings["cp_pF"]) * 1e-12
    c_extra = info.get("cextra_F", 0.0) if settings.get("cextra_pF") is None else float(settings["cextra_pF"]) * 1e-12
    gain = float(settings.get("gain", 1.0))
    if c_sensor + c_extra <= 0:
        raise ValueError("the total capacitance (sensor + extra) must be positive")
    if d33 * gain == 0:
        raise ValueError("d33 x gain must not be zero")
    return SensorParams(d33=d33, c_sensor=c_sensor, c_extra=c_extra, gain=gain)


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

    @property
    def error_final(self) -> float | None:
        return None if self.fin is None or not len(self.F) else float(self.F[-1] - self.fin[-1])


def extract_force(v: np.ndarray, params: SensorParams, v_start: float) -> tuple[np.ndarray, np.ndarray]:
    """(dF, F) of a sequence of voltages of one sensor; v_start is the voltage before the first conversion."""
    v = np.asarray(v, dtype=float)
    previous = np.concatenate([[v_start], v[:-1]]) if len(v) else v
    dF = (v - previous) * params.force_per_volt()
    return dF, np.cumsum(dF)


def compute_channel(table: SampleTable, address: int, params: SensorParams, settings: dict, vmid: float,
                    fin_at: Callable[[int, np.ndarray], np.ndarray | None] | None = None) -> ChannelForce | None:
    """Force of one channel from the conversion table (None when the channel has no conversion in the run)."""
    mask = table.channel_mask(address)
    if not mask.any():
        return None
    v = table.v_out[mask] if settings.get("source", SRC_ADC_OUT) == SRC_ADC_OUT else table.v_in[mask]
    v_start = float(v[0]) if settings.get("reference", REF_VMID) == REF_FIRST else float(vmid)
    dF, F = extract_force(v, params, v_start)
    t = table.t[mask]
    fin = fin_at(address, t) if fin_at else None
    return ChannelForce(address, params, t, v, dF, F, fin, clipped=int(table.clipped[mask].sum()))


def compute_all(table: SampleTable, channels: list[int], infos: dict[int, dict], settings: dict, vmid: float,
                fin_at: Callable[[int, np.ndarray], np.ndarray | None] | None = None) -> dict[int, ChannelForce]:
    out: dict[int, ChannelForce] = {}
    for ch in channels:
        if ch not in infos:
            continue
        cf = compute_channel(table, ch, resolve_params(settings, infos[ch]), settings, vmid, fin_at)
        if cf is not None:
            out[ch] = cf
    return out


def summary_row(cf: ChannelForce) -> list:
    """[channel, conversions, clipped, dF min, dF max, F min, F max, F final, Fin final, error final]"""
    fin_final = None if cf.fin is None else float(cf.fin[-1])
    return [cf.address, len(cf.t), cf.clipped, float(cf.dF.min()), float(cf.dF.max()), float(cf.F.min()), float(cf.F.max()),
            float(cf.F[-1]), fin_final, cf.error_final]


def formulas_html(params: SensorParams | None, adc_lsb: float | None, channel: int | None, reference: str, source: str) -> str:
    """Formula panel with the live numbers of one channel substituted."""
    live = ""
    if params is not None:
        lsb_txt = ""
        if adc_lsb is not None:
            lsb_txt = (f"<br>Force per LSB: dF<sub>LSB</sub> = lsb &middot; C / (d33 &middot; G) = {adc_lsb * 1e3:.4g} mV "
                       f"&middot; {params.force_per_volt():.4g} N/V = <b>{adc_lsb * params.force_per_volt() * 1e3:.4g} mN</b>")
        live = (f"<p><b>Values for channel {channel}</b>: d33 = {params.d33 * 1e12:.4g} pC/N, "
                f"C = {params.c_sensor * 1e12:.4g} pF + {params.c_extra * 1e12:.4g} pF = {params.c_total * 1e12:.4g} pF, "
                f"G = {params.gain:g}<br>Sensitivity: S = d33 / C = <b>{params.sensitivity:.4g} V/N</b>{lsb_txt}</p>")
    first = "the first sample (dF[0] = 0)" if reference == REF_FIRST else "VMID"
    return (
        "<p><b>Ideal charge model</b></p>"
        "<p>Open-circuit sensor voltage: <b>V = d33 &middot; F / C</b>, with C = C<sub>sensor</sub> + C<sub>extra</sub></p>"
        "<p>Change in force between two consecutive conversions of the same sensor:<br>"
        f"<b>dF[k] = (V[k] &minus; V[k&minus;1]) &middot; C / (d33 &middot; G)</b>, V[&minus;1] = {first}; V is the {source}</p>"
        "<p>Total force (discrete integration): <b>F[k] = &Sigma; dF[0..k]</b>, starting at 0</p>"
        f"{live}"
        "<p style='color:#a05000'>This is the ideal charge model. It ignores the charge sharing with the multiplexer node, the "
        "leakage and the bias-resistor decay, so F drifts and differs from the applied force when the real circuit deviates; "
        "that difference is what the comparison shows.</p>")
