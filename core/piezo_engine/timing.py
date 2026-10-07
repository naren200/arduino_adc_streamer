"""Capture-metadata timing resolver: metadata dict -> TimingPolicy + sample rate.

The single resolver for Analysis, training and live (it replaced the Analysis
tab's own ``resolve_analysis_pzt_*`` / ``_auto_pzt_*`` / ``_normalize_pzt_*``
functions, P2b), so all three resolve the same numbers from the same metadata.
Input is a plain dict (no GUI snapshot).

Deliberate differences from the Analysis originals:
* AUTO without any resolvable connected time raises (the Analysis path does
  too); an unrecognised mode string raises instead of silently becoming auto.
* No array fallbacks: a missing sample rate raises (the Analysis path falls
  back to timestamp medians, which need arrays the resolver does not take).
* The ``block_timing_csv`` sidecar is not read inside the resolver (file I/O
  belongs at the edge): the caller reads it with
  :func:`read_block_timing_connected_time_s` and passes the value in, where it
  sits at the same priority as in Analysis.
* CONTINUOUS returns an empty pre-sample-decay map (decay only applies to a
  MUX-connected leak); the pre-P2b Analysis still applied the metadata map there.

The Analysis tab is a caller of this module (it no longer carries its own
resolver): it passes the two things only it knows through
:class:`TimingEdgeInputs` -- the block-timing sidecar value and the snapshot's
own sample rate (an in-memory snapshot may carry no per-channel rates in its
metadata).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import numpy as np

from core.piezo_engine.config import TimingMode, TimingPolicy

_MICROSECONDS_PER_SECOND = 1_000_000.0
_MILLISECONDS_PER_SECOND = 1000.0
_RATE_FALLBACK_KEYS = ("adc_effective_total_sample_rate_hz", "arduino_sample_rate_hz", "total_rate_hz")
_INFER = TimingMode.INFER_FROM_TOTAL_SAMPLE_RATE.value
_MODE_ALIASES = {
    "infer": _INFER,
    "infer_from_rate": _INFER,
    "infer_from_sample_rate": _INFER,
    "total_sample_rate": _INFER,
    "continuous_leak": TimingMode.CONTINUOUS.value,
}


@dataclass(frozen=True)
class TimingEdgeInputs:
    """Caller-side facts the metadata alone may not hold.

    ``block_timing_connected_time_s`` is the sidecar value from
    :func:`read_block_timing_connected_time_s`; ``sample_rate_hz`` overrides the
    metadata rate when resolving INFER_FROM_TOTAL_SAMPLE_RATE.
    """

    block_timing_connected_time_s: float | None = None
    sample_rate_hz: float | None = None


NO_EDGE_INPUTS = TimingEdgeInputs()
_INFER_RATE_UNAVAILABLE = "sample rate unavailable for inferred PZT MUX connected time"


@dataclass(frozen=True)
class ResolvedCaptureTiming:
    policy: TimingPolicy
    sample_rate_hz: float
    status: str


def _optional_float(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value) -> float | None:
    parsed = _optional_float(value)
    return parsed if parsed is not None and parsed > 0.0 else None


def normalize_timing_mode(value) -> TimingMode:
    normalized = str(value or TimingMode.AUTO.value).strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return TimingMode(_MODE_ALIASES.get(normalized, normalized))
    except ValueError:
        raise ValueError(f"unknown PZT MUX timing mode: {value!r}") from None


def _timing_block(metadata: Mapping) -> Mapping:
    timing = metadata.get("timing", {}) if isinstance(metadata, Mapping) else {}
    return timing if isinstance(timing, Mapping) else {}


def _metadata_rate_or_none(metadata: Mapping) -> float | None:
    timing = _timing_block(metadata)
    rates = timing.get("per_channel_sample_rates_hz")
    if isinstance(rates, Mapping):
        positive = [float(v) for v in rates.values() if isinstance(v, (int, float)) and float(v) > 0]
        if positive:
            return float(np.mean(positive))
    for key in _RATE_FALLBACK_KEYS:
        value = timing.get(key)
        if isinstance(value, (int, float)) and float(value) > 0:
            return float(value)
    return None


def metadata_sample_rate_hz(metadata: Mapping) -> float:
    """Per-channel sample rate (mean of ``timing.per_channel_sample_rates_hz``)."""
    rate = _metadata_rate_or_none(metadata)
    if rate is None:
        raise ValueError("capture metadata carries no usable sample rate")
    return rate


def timestamps_for_capture(n_samples: int, fs: float) -> np.ndarray:
    """Uniform grid ``i / fs``.

    CSV timestamps are not trusted per sample (they are wall-clock strings with
    jitter and block-level quantisation); ``fs`` is the metadata per-channel
    average from :func:`metadata_sample_rate_hz`, which is what live derives
    from its cumulative average too.
    """
    if not fs > 0.0:
        raise ValueError("fs must be > 0")
    return np.arange(int(n_samples), dtype=np.float64) / float(fs)


def _calculated_connected_time_s(metadata: Mapping) -> float | None:
    candidates = [metadata.get("adc_mux_timing"), _timing_block(metadata).get("adc_mux_timing")]
    for candidate in candidates:
        calculated = candidate.get("calculated_timing") if isinstance(candidate, Mapping) else None
        value_us = _positive_float(calculated.get("t_connected_us")) if isinstance(calculated, Mapping) else None
        if value_us is not None:
            return value_us / _MICROSECONDS_PER_SECOND
    return None


_BLOCK_TIMING_COLUMN = "avg_dt_us"
_BLOCK_TIMING_POSITIONAL_INDEX = 3


def _block_timing_values_us(sidecar_path: Path) -> list[float]:
    with sidecar_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames and _BLOCK_TIMING_COLUMN in reader.fieldnames:
            raw_values = [row.get(_BLOCK_TIMING_COLUMN) for row in reader]
        else:
            handle.seek(0)
            positional = csv.reader(handle)
            next(positional, None)
            raw_values = [row[_BLOCK_TIMING_POSITIONAL_INDEX] for row in positional if len(row) > _BLOCK_TIMING_POSITIONAL_INDEX]
    return [value for value in (_positive_float(raw) for raw in raw_values) if value is not None]


def read_block_timing_connected_time_s(metadata: Mapping, capture_csv_path: Path | None = None) -> float | None:
    """Edge helper: median ``avg_dt_us`` of the ``block_timing_csv`` sidecar, in seconds.

    A relative sidecar path is resolved against the capture CSV's folder.
    Returns None when the metadata names no sidecar or it cannot be read.
    """
    timing = _timing_block(metadata)
    candidate = metadata.get("block_timing_csv") or timing.get("block_timing_csv")
    if not candidate:
        return None
    sidecar_path = Path(str(candidate)).expanduser()
    if not sidecar_path.is_absolute() and capture_csv_path is not None:
        sidecar_path = Path(capture_csv_path).parent / sidecar_path
    try:
        values_us = _block_timing_values_us(sidecar_path)
    except (OSError, ValueError, csv.Error):
        return None
    return float(np.median(np.asarray(values_us, dtype=np.float64))) / _MICROSECONDS_PER_SECOND if values_us else None


def auto_mux_connected_time_s(
    metadata: Mapping, block_timing_connected_time_s: float | None = None,
) -> tuple[float | None, str]:
    timing = _timing_block(metadata)
    for holder in (metadata, timing):
        value = _positive_float(holder.get("pzt_mux_connected_time_s"))
        if value is not None:
            return value, str(holder.get("pzt_mux_connected_time_source") or "metadata timing")
    calculated = _calculated_connected_time_s(metadata)
    if calculated is not None:
        return calculated, "adc_mux_timing.calculated_timing.t_connected_us"
    if block_timing_connected_time_s is not None and block_timing_connected_time_s > 0.0:
        return float(block_timing_connected_time_s), "block_timing_csv avg_dt_us"
    for key in ("adc_active_sample_interval_us", "arduino_sample_time_us"):
        value_us = _positive_float(timing.get(key))
        if value_us is not None:
            return value_us / _MICROSECONDS_PER_SECOND, f"metadata timing.{key}"
    return None, ""


def resolve_mux_leak_dt_s(
    metadata: Mapping, settings: Mapping, edge: TimingEdgeInputs = NO_EDGE_INPUTS,
) -> tuple[float | None, str]:
    """Resolve the MUX-connected leak exposure; None means continuous leak."""
    mode = normalize_timing_mode(settings.get("mux_timing_mode", TimingMode.AUTO.value))
    if mode is TimingMode.CONTINUOUS:
        return None, "PZT MUX timing: Continuous leak uses full trace dt."
    if mode is TimingMode.MANUAL:
        value = _positive_float(settings.get("mux_connected_time_s"))
        if value is None:
            raise ValueError("manual PZT MUX connected time must be greater than zero")
        return value, f"PZT MUX timing: Manual {value * _MILLISECONDS_PER_SECOND:.3f} ms."
    if mode is TimingMode.INFER_FROM_TOTAL_SAMPLE_RATE:
        rate = edge.sample_rate_hz if edge.sample_rate_hz else _metadata_rate_or_none(metadata)
        if rate is None or not rate > 0.0:
            raise ValueError(_INFER_RATE_UNAVAILABLE)
        value = 1.0 / float(rate)
        return value, f"PZT MUX timing: Inferred {value * _MILLISECONDS_PER_SECOND:.3f} ms from total sample rate."
    value, source = auto_mux_connected_time_s(metadata, edge.block_timing_connected_time_s)
    if value is None:
        raise ValueError("PZT MUX connected time unavailable; choose Manual or Infer from total sample rate")
    return value, f"PZT MUX timing: Auto {value * _MILLISECONDS_PER_SECOND:.3f} ms from {source}."


def _decay_from_exact_labels(timing: Mapping) -> dict[str, float]:
    exact = timing.get("pzt_pre_sample_decay_s_by_label", {})
    result: dict[str, float] = {}
    for label, raw in (exact.items() if isinstance(exact, Mapping) else ()):
        parsed = _optional_float(raw)
        if parsed is not None and parsed >= 0.0:
            result[str(label)] = parsed
    return result


def _decay_from_adc_inputs(timing: Mapping) -> dict[str, float]:
    by_input = timing.get("pzt_pre_sample_decay_s_by_adc_input", {})
    by_label = timing.get("pzt_adc_input_by_label", {})
    if not isinstance(by_input, Mapping) or not isinstance(by_label, Mapping):
        return {}
    result: dict[str, float] = {}
    for label, adc_input in by_label.items():
        try:
            value = float(by_input[str(int(adc_input))])
        except (KeyError, TypeError, ValueError):
            continue
        if value >= 0.0:
            result[str(label)] = value
    return result


def resolve_pre_sample_decay_by_label(metadata: Mapping) -> dict[str, float]:
    """Exact per-label pre-sample decay from the physical MUX map; unknown mappings get none."""
    timing = _timing_block(metadata)
    return _decay_from_exact_labels(timing) or _decay_from_adc_inputs(timing)


def resolve_timing_policy(
    metadata: Mapping, settings: Mapping, edge: TimingEdgeInputs = NO_EDGE_INPUTS,
) -> tuple[TimingPolicy, str]:
    """Resolve only the :class:`TimingPolicy` (and its status line); needs no sample rate
    unless the mode infers the leak from it.

    CONTINUOUS gets no pre-sample decay: the decay is the settling between the MUX
    switch and the sample, which only exists for a MUX-connected leak.
    """
    mode = normalize_timing_mode(settings.get("mux_timing_mode", TimingMode.AUTO.value))
    leak_dt_s, status = resolve_mux_leak_dt_s(metadata, settings, edge)
    decay = {} if mode is TimingMode.CONTINUOUS else resolve_pre_sample_decay_by_label(metadata)
    return TimingPolicy(mode=mode, leak_dt_s=leak_dt_s, pre_sample_decay_s_by_label=decay), status


def resolve_capture_timing(
    metadata: Mapping, settings: Mapping, block_timing_connected_time_s: float | None = None,
) -> ResolvedCaptureTiming:
    """Resolve TimingPolicy and the per-channel sample rate from capture metadata.

    ``settings`` carries ``mux_timing_mode`` (default auto) and, for manual
    mode, ``mux_connected_time_s``. ``block_timing_connected_time_s`` is the
    optional sidecar value from :func:`read_block_timing_connected_time_s`.
    """
    edge = TimingEdgeInputs(
        block_timing_connected_time_s=block_timing_connected_time_s, sample_rate_hz=_metadata_rate_or_none(metadata)
    )
    policy, status = resolve_timing_policy(metadata, settings, edge)
    return ResolvedCaptureTiming(policy=policy, sample_rate_hz=metadata_sample_rate_hz(metadata), status=status)
