"""
Idle Baseline + Per-Chunk Activity Test
=========================================
Ported from texture_piezo/src/touchid_inference/quality_gate.py for
arduino_adc_streamer standalone inference.

Fits a per-channel mean/std "idle" baseline from a short no-contact capture
of the live sensor, and provides the shared 0.05s-micro-chunk-vs-idle-band
activity test (chunk_is_active) plus the idle-strip threshold
(idle_gap_chunks_cap) that both feed inference/segmentation.py's
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

from dataclasses import dataclass

import numpy as np

MICRO_CHUNK_S = 0.05
DEFAULT_K = 8.0
IDLE_CAPTURE_DURATION_S = 5.0


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


def idle_gap_chunks_cap(window_size_s: float) -> int:
    """Idle runs shorter than this many micro-chunks stay inline (absorbed)
    in a fragment; runs at or beyond it are stripped out entirely and close
    the fragment (see segmentation.ActiveSampleQueue). Threshold is
    window_size_s/3, rounded to the nearest whole micro-chunk."""
    gap_s = window_size_s / 3.0
    return max(1, round(gap_s / MICRO_CHUNK_S))
