"""
Idle Baseline + Per-Chunk Activity Test
=========================================
Fits a per-channel mean/std "idle" baseline from a short no-contact capture
of the live sensor, and provides the shared 0.05s-micro-chunk-vs-idle-band
activity test (chunk_is_active) plus the merge-gap threshold
(merge_gap_chunks) that both feed inference/segmentation.py's
ActiveSampleQueue -- the sample-accurate replacement for this module's old
is_window_quality gate (removed; see segmentation.py's module docstring for
why a fixed-hop-grid accept/reject gate was replaced).

Constants validated against texture_piezo's only_idle_v1..v4_202609*.csv
dedicated idle captures (~878s combined, two rig sessions with visibly
different noise floors) and the labeled only_wood_and_idle_v2 capture:
  - Per-sample-in-chunk deviation (not chunk-mean) is required -- chunk-mean
    gating averages transients away and gave 0% false positives even at
    k=3, which is not a real signal.
  - k=8 is the smallest multiplier that reaches 0% false-positive rate
    across all four idle captures (k=5 misfired 8-96% of micro-chunks
    depending on session noise floor; k=6 still had ~7-8% FP on the
    noisier sessions). k=8 still flags ~96% of true-contact samples in the
    labeled wood capture, so it isn't so loose it misses real touches.
  - Gaps between labeled texture events are NOT clean idle (they contain
    settle/creep dynamics -- see clip_windowing_utils_v1.py's own module
    docstring) and were excluded from this validation for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from file_operations.settings_persistence import load_settings_payload, save_settings_payload

MICRO_CHUNK_S = 0.05
DEFAULT_K = 8.0
IDLE_CAPTURE_DURATION_S = 5.0

IDLE_BASELINE_PAYLOAD_KEY = "idle_baseline"


@dataclass
class IdleBaseline:
    pzt_columns: list[str]
    mean: list[float]
    std: list[float]
    fs: float
    k: float = DEFAULT_K
    captured_duration_s: float = 0.0


def fit_idle_baseline(
    samples: np.ndarray, pzt_columns: list[str], fs: float, k: float = DEFAULT_K,
) -> IdleBaseline:
    """samples: (n_samples, len(pzt_columns)) raw ADC counts from a no-contact capture."""
    mean = samples.mean(axis=0, dtype=np.float64)
    std = samples.std(axis=0, dtype=np.float64)
    return IdleBaseline(
        pzt_columns=list(pzt_columns), mean=mean.tolist(), std=std.tolist(),
        fs=fs, k=k, captured_duration_s=len(samples) / fs if fs > 0 else 0.0,
    )


def chunk_is_active(chunk_samples: np.ndarray, baseline: IdleBaseline, k: float | None = None) -> bool:
    """True iff ANY sample within this one micro-chunk has any channel
    outside the [mean - k*std, mean + k*std] idle band. Per-sample (not
    chunk-mean) deviation, per the validation note above -- chunk-mean
    gating averages transients away."""
    if len(chunk_samples) == 0:
        return False
    mean = np.asarray(baseline.mean)
    std = np.asarray(baseline.std)
    kk = baseline.k if k is None else k
    lo, hi = mean - kk * std, mean + kk * std
    out_of_band = (chunk_samples < lo) | (chunk_samples > hi)
    return bool(out_of_band.any())


MAX_WINDOW_IDLE_FRACTION = 0.25


def window_idle_fraction(window_samples: np.ndarray, baseline: IdleBaseline, fs: float, k: float | None = None) -> float:
    """Fraction of this window's own 0.05s micro-chunks that are idle (i.e.
    NOT chunk_is_active), at the same granularity/activity test
    ActiveSampleQueue uses to build spans. A merged span can fuse several
    genuinely separate touch events together when the idle gap between them
    is shorter than merge_gap_chunks(window_size_s) -- this catches a window
    sliced from such a span that straddles one of those gaps, even though the
    span itself (and thus the window) was accepted."""
    if len(window_samples) == 0:
        return 1.0
    chunk_n = max(1, round(MICRO_CHUNK_S * fs))
    n_chunks = 0
    n_idle = 0
    idx = 0
    while idx < len(window_samples):
        end = min(idx + chunk_n, len(window_samples))
        n_chunks += 1
        if not chunk_is_active(window_samples[idx:end], baseline, k):
            n_idle += 1
        idx = end
    return n_idle / n_chunks


def merge_gap_chunks(window_size_s: float) -> int:
    """Idle runs shorter than this many micro-chunks get merge-absorbed into
    the surrounding active span. Threshold is window_size_s/3, rounded to
    the nearest whole micro-chunk."""
    gap_s = window_size_s / 3.0
    return max(1, round(gap_s / MICRO_CHUNK_S))


def _get_idle_baseline_path() -> Path:
    return Path.home() / ".adc_streamer" / "touchid" / "idle_baseline.json"


def save_idle_baseline(baseline: IdleBaseline) -> Path:
    payload = {
        "version": 1,
        IDLE_BASELINE_PAYLOAD_KEY: {
            "pzt_columns": baseline.pzt_columns,
            "mean": baseline.mean,
            "std": baseline.std,
            "fs": baseline.fs,
            "k": baseline.k,
            "captured_duration_s": baseline.captured_duration_s,
        },
    }
    return save_settings_payload(_get_idle_baseline_path(), payload)


def load_idle_baseline(pzt_columns: list[str] | None = None) -> IdleBaseline | None:
    """Returns the persisted baseline, or None if none exists yet, or if it
    was captured for a different set of PZT channels (e.g. after switching
    sensor boards) -- stale-channel baselines must not be silently reused."""
    path = _get_idle_baseline_path()
    if not path.exists():
        return None
    try:
        _path, payload = load_settings_payload(path, payload_key=IDLE_BASELINE_PAYLOAD_KEY)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        baseline = IdleBaseline(
            pzt_columns=list(payload["pzt_columns"]),
            mean=list(payload["mean"]),
            std=list(payload["std"]),
            fs=float(payload["fs"]),
            k=float(payload.get("k", DEFAULT_K)),
            captured_duration_s=float(payload.get("captured_duration_s", 0.0)),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if pzt_columns is not None and baseline.pzt_columns != list(pzt_columns):
        return None
    return baseline
