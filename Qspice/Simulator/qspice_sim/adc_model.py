"""Derived ADC output: quantise the ADC input value at every conversion instant (no re-simulation needed).

The conversion instants and channel addresses come from the model adapter (``samples``), the ADC input value is the
simulated sample node at those instants. Pure numpy, no GUI.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class AdcParams:
    """ADC resolution and range. The range is 0 .. vref when VMID is above 0 and -vref .. +vref (twice the span) when VMID is 0."""

    bits: int
    vref: float
    vmid: float

    def __post_init__(self) -> None:
        if int(self.bits) != self.bits or self.bits < 2:
            raise ValueError("the ADC resolution must be an integer of at least 2 bits")
        if not self.vref > 0:
            raise ValueError("the ADC reference voltage must be positive")

    @property
    def bipolar(self) -> bool:
        return self.vmid == 0

    @property
    def v_low(self) -> float:
        return -self.vref if self.bipolar else 0.0

    @property
    def v_high(self) -> float:
        return self.vref

    @property
    def lsb(self) -> float:
        return (self.v_high - self.v_low) / 2 ** int(self.bits)

    @property
    def max_code(self) -> int:
        return 2 ** int(self.bits) - 1

    def describe(self) -> str:
        kind = "bipolar, VMID = 0" if self.bipolar else "unipolar"
        return f"{self.v_low:g} .. {self.v_high:g} V ({kind}), LSB {self.lsb * 1e3:.4g} mV at {int(self.bits)} bit"


def quantize(v_in: np.ndarray, adc: AdcParams) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(code, v_out, clipped) of the input voltages: round to the nearest code (half up), clip to 0 .. 2^bits - 1.

    ``clipped`` marks inputs outside v_low .. v_high.
    """
    v = np.asarray(v_in, dtype=float)
    raw = np.floor((v - adc.v_low) / adc.lsb + 0.5)
    code = np.clip(raw, 0, adc.max_code).astype(np.int64)
    v_out = adc.v_low + code * adc.lsb
    clipped = (v < adc.v_low) | (v > adc.v_high)
    return code, v_out, clipped


@dataclass
class SampleTable:
    """One row per conversion, in time order."""

    t: np.ndarray
    address: np.ndarray            # channel of the conversion (-1 when the adapter gives none)
    label: list[str]
    v_in: np.ndarray               # ideal ADC input value at the conversion instant
    noise_v: np.ndarray            # white noise added to the input (zero without noise)
    code: np.ndarray
    v_out: np.ndarray              # quantised value, converted back to volts
    clipped: np.ndarray
    adc: AdcParams = field(default=None)  # type: ignore[assignment]

    def __len__(self) -> int:
        return len(self.t)

    @property
    def v_noisy(self) -> np.ndarray:
        return self.v_in + self.noise_v

    def channel_mask(self, ch: int) -> np.ndarray:
        return self.address == ch

    def clipped_count(self, channels: list[int] | None = None) -> int:
        if channels is None:
            return int(self.clipped.sum())
        return int(self.clipped[np.isin(self.address, channels)].sum())


def build_sample_table(samples: list[dict], v_ideal: np.ndarray, adc: AdcParams, noise_mv: float = 0.0,
                       seed: int = 1) -> SampleTable:
    """Quantise the ideal input values of the adapter's conversion list.

    ``noise_mv`` is the RMS of independent Gaussian noise added at every conversion before quantisation
    (``v_out = quant(v_in + N(0, sigma))``); the same seed gives the same noise for the same list of conversions.
    """
    if noise_mv < 0:
        raise ValueError("the noise must not be negative")
    t = np.array([float(s["t"]) for s in samples])
    order = np.argsort(t, kind="stable")
    t = t[order]
    address = np.array([int(samples[i].get("address", -1)) for i in order], dtype=np.int64)
    label = [str(samples[i].get("label", "")) for i in order]
    v_in = np.asarray(v_ideal, dtype=float)[order]
    noise = np.random.default_rng(int(seed)).normal(0.0, noise_mv * 1e-3, len(t)) if len(t) else np.zeros(0)
    code, v_out, clipped = quantize(v_in + noise, adc)
    return SampleTable(t, address, label, v_in, noise, code, v_out, clipped, adc)


def stair(t: np.ndarray, v: np.ndarray, t_end: float) -> tuple[np.ndarray, np.ndarray]:
    """Step trace: every value is held until the next conversion (the last one until ``t_end``)."""
    n = len(t)
    if n == 0:
        return np.zeros(0), np.zeros(0)
    x = np.empty(2 * n)
    y = np.empty(2 * n)
    x[0::2], y[0::2] = t, v
    x[1:-1:2] = t[1:]
    x[-1] = max(float(t[-1]), t_end)
    y[1::2] = v
    return x, y


def stair_at(times: np.ndarray, t: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Value of the step trace at the given times (NaN before the first conversion)."""
    out = np.full(len(times), np.nan)
    if len(t):
        idx = np.searchsorted(t, times, side="right") - 1
        ok = idx >= 0
        out[ok] = v[idx[ok]]
    return out
