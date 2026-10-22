import numpy as np

from core.texture_piezo.gating.v2.config import GatingV2Config
from core.texture_piezo.gating.v2.strong_signal import block_burst_scores
from data_processing.burst_score import compute_burst_score_series, trailing_abs_sum

FS = 100.0
CONFIG = GatingV2Config(
    idle_gate_enabled=False, strength_gate_enabled=True, concat_enabled=False,
    burst_min=1.0, sustain_min=0, block_s=0.1, sustain_blocks=2, expiry_s=1.0,
)


def _signal():
    jerk = np.zeros(100)
    jerk[30:50] = -0.2  # two blocks of 10 samples at |0.2| -> score 2.0 each
    return jerk, np.arange(100, dtype=np.float64) / FS


def test_scores_are_the_gates_plain_abs_sums_per_block():
    jerk, timestamps = _signal()
    series = compute_burst_score_series(jerk, timestamps, FS, CONFIG)
    np.testing.assert_array_equal(series.scores, block_burst_scores(jerk, 10))
    assert np.isclose(series.scores[3], 2.0) and np.isclose(series.scores[4], 2.0)


def test_strong_blocks_are_those_strictly_above_burst_min():
    jerk, timestamps = _signal()
    series = compute_burst_score_series(jerk, timestamps, FS, CONFIG)
    assert list(np.flatnonzero(series.is_strong)) == [3, 4]


def test_block_geometry_is_in_capture_seconds():
    jerk, timestamps = _signal()
    series = compute_burst_score_series(jerk, timestamps, FS, CONFIG)
    assert series.block_start_s[3] == timestamps[30] and series.block_end_s[3] == timestamps[39]


def test_trailing_abs_sum_is_the_abs_sum_of_the_past_window_at_every_sample():
    jerk = np.array([1.0, -2.0, 3.0, -4.0, 5.0])

    np.testing.assert_allclose(trailing_abs_sum(jerk, 3), [1, 3, 6, 9, 12])


def test_series_carries_the_rolling_sum_aligned_with_the_timestamps():
    jerk, timestamps = _signal()

    series = compute_burst_score_series(jerk, timestamps, FS, CONFIG)

    assert len(series.rolling_sum) == len(timestamps)
    assert np.isclose(series.rolling_sum[49], 2.0)  # 10 samples of |0.2| ending at sample 49
