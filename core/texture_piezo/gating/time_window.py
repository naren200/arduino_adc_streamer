"""
Timestamp-Range Window Selection
================================
Pure helpers that pick window/hop boundaries out of a per-sample timestamp
array by real elapsed time, instead of converting window_size_s/hop_size_s
into sample counts with a sample-rate estimate. The sample rate of a live
stream drifts, so a count derived from it silently stops spanning the
intended duration; timestamps are the single source of truth for "how long".

Every function assumes `timestamps` is non-decreasing.
"""

from __future__ import annotations

import numpy as np

# Float-noise slack for boundary comparisons, so a uniform-rate window does
# not flip between N and N+1 samples on rounding error. Far below any real
# sample interval.
TIMESTAMP_EPSILON_S = 1e-9


def window_end_index(timestamps: np.ndarray, start_i: int, window_size_s: float) -> int | None:
    """Exclusive end index of the half-open window [t0, t0 + window_size_s)
    starting at start_i, or None until a sample at/after the boundary has
    arrived (only then is the window known to be complete)."""
    boundary = timestamps[start_i] + window_size_s - TIMESTAMP_EPSILON_S
    end_i = int(np.searchsorted(timestamps, boundary, side="left"))
    return end_i if end_i < len(timestamps) else None


def hop_start_index(timestamps: np.ndarray, start_i: int, hop_size_s: float) -> int:
    """Start index of the next window, hop_size_s after start_i. Always at
    least start_i + 1 so duplicate timestamps can never stall a sliding loop."""
    boundary = timestamps[start_i] + hop_size_s - TIMESTAMP_EPSILON_S
    return max(start_i + 1, int(np.searchsorted(timestamps, boundary, side="left")))


def first_index_at_or_after(timestamps: np.ndarray, target_ts: float) -> int:
    """Index of the first sample whose timestamp is >= target_ts (len(timestamps)
    if none has arrived yet)."""
    return int(np.searchsorted(timestamps, target_ts - TIMESTAMP_EPSILON_S, side="left"))


def padded_start_index(timestamps: np.ndarray, end_i: int, window_size_s: float) -> int | None:
    """Start index of the window_size_s-long span ending at sample end_i - 1,
    or None if the retained history does not reach back that far. Spans
    (t_last - window_size_s, t_last], so a uniform-rate window holds exactly
    the samples a window_size_s * rate count would."""
    t_last = timestamps[end_i - 1]
    first_i = int(np.searchsorted(timestamps, t_last - window_size_s + TIMESTAMP_EPSILON_S, side="left"))
    if first_i > 0:
        return first_i
    # History starts inside the span; it reaches back a full window only if
    # the retained span plus one sample interval covers window_size_s.
    span = t_last - timestamps[0]
    sample_interval = span / max(1, end_i - 1)
    return 0 if span + sample_interval >= window_size_s - TIMESTAMP_EPSILON_S else None
