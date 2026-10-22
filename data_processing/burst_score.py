"""Block burst scores for the Analysis burst plots, from the replay's own normal_jerk.

Nothing here re-implements the gate: scores come from the strength stage's own
``block_burst_scores`` / ``is_strong_block``, with the block length and threshold of the
replay's ``GatingV2Config``. Which stretches the gate then called good is read from the
replay's gate trace, not recomputed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from core.texture_piezo.gating.v2.config import GatingV2Config
from core.texture_piezo.gating.v2.strong_signal import block_burst_scores, block_length_samples, is_strong_block


@dataclass(frozen=True)
class BurstScoreSeries:
    """Per-block geometry in capture seconds, plus the trailing sum at every replayed sample."""

    block_start_s: np.ndarray
    block_end_s: np.ndarray
    scores: np.ndarray
    is_strong: np.ndarray
    burst_min: float
    rolling_sum: np.ndarray  # sum of |normal_jerk| over the trailing block_s, aligned with the replay timestamps


def trailing_abs_sum(normal_jerk: np.ndarray, window_n: int) -> np.ndarray:
    """Sum of |normal_jerk| over the last ``window_n`` samples, at every sample (shorter at the start)."""
    cumulative = np.cumsum(np.abs(np.asarray(normal_jerk, dtype=np.float64)))
    trailing = cumulative.copy()
    trailing[window_n:] -= cumulative[:-window_n]
    return trailing


def compute_burst_score_series(
    normal_jerk: np.ndarray, timestamps_s: np.ndarray, fs: float, config: GatingV2Config,
) -> BurstScoreSeries:
    """``timestamps_s`` is the time axis of ``normal_jerk`` (same length)."""
    normal_jerk = np.asarray(normal_jerk, dtype=np.float64)
    block_n = block_length_samples(config.block_s, fs)
    scores = block_burst_scores(normal_jerk, block_n)
    starts = np.arange(len(scores)) * block_n
    ends = np.minimum(starts + block_n, len(timestamps_s)) - 1
    return BurstScoreSeries(
        block_start_s=timestamps_s[starts],
        block_end_s=timestamps_s[ends],
        scores=scores,
        is_strong=is_strong_block(scores, config.burst_min),
        burst_min=config.burst_min,
        rolling_sum=trailing_abs_sum(normal_jerk, block_n),
    )
