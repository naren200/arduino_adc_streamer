import numpy as np

from core.texture_piezo.gating.v2.strong_signal import block_burst_scores
from core.texture_piezo.application.stream_processor import DerivedChannelPipeline
from data_processing.analysis_workbench import AnalysisSourceSnapshot
from data_processing.burst_score import (
    BurstScoreSettings,
    compute_burst_score_series,
    derive_engine_view,
    trailing_abs_sum,
)

FS = 100.0
SETTINGS = BurstScoreSettings(block_s=0.1, burst_min=1.0, sustain_min=0, sustain_blocks=2)


def _signal():
    jerk = np.zeros(100)
    jerk[30:50] = -0.2  # two blocks of 10 samples at |0.2| -> score 2.0 each
    return jerk, np.arange(100, dtype=np.float64) / FS


def test_scores_are_the_gates_plain_abs_sums_per_block():
    jerk, x = _signal()
    series = compute_burst_score_series(jerk, x, FS, SETTINGS)
    np.testing.assert_array_equal(series.scores, block_burst_scores(jerk, 10))
    assert series.scores[3] == series.scores[4] == np.float64(2.0) or np.isclose(series.scores[3], 2.0)


def test_strong_blocks_are_those_strictly_above_burst_min():
    jerk, x = _signal()
    series = compute_burst_score_series(jerk, x, FS, SETTINGS)
    assert list(np.flatnonzero(series.is_strong)) == [3, 4]


def test_good_ranges_follow_the_live_stage_and_end_on_a_strong_block():
    jerk, x = _signal()
    series = compute_burst_score_series(jerk, x, FS, SETTINGS)
    assert series.good_x_ranges[0][0] == x[30]
    assert series.good_x_ranges[-1][1] == x[49]


def test_sustain_min_rejects_a_lone_strong_block():
    jerk = np.zeros(100)
    jerk[30:40] = 1.0
    x = np.arange(100, dtype=np.float64) / FS
    strict = BurstScoreSettings(block_s=0.1, burst_min=1.0, sustain_min=1, sustain_blocks=3)
    assert compute_burst_score_series(jerk, x, FS, strict).good_x_ranges == []


def test_analysis_normal_jerk_equals_the_live_pipelines():
    rng = np.random.default_rng(1)
    labels = ["PZT5_B", "PZT5_L", "PZT5_C", "PZT5_R", "PZT5_T"]
    counts = (1600 + rng.normal(0, 30, (600, 5))).astype(np.float32)
    snapshot = AnalysisSourceSnapshot(
        data=counts, timestamps_s=np.arange(600) / 1000.0, channel_labels=labels,
        metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
        source_id="unit", sample_rate_hz=1000.0,
    )
    from core.texture_piezo.application.stream_processor import LIVE_ENGINE_CONFIG

    live = DerivedChannelPipeline(labels, LIVE_ENGINE_CONFIG)
    columns = {label: counts[:, index].astype(np.float64) for index, label in enumerate(labels)}
    filtered = live.filter_raw(columns)
    _raw, derived, timestamps = live.process(filtered, snapshot.timestamps_s, 1000.0)

    view = derive_engine_view(snapshot, LIVE_ENGINE_CONFIG.vref_voltage, "time_ms")

    np.testing.assert_array_equal(view.normal_jerk, derived["normal_jerk"])
    assert len(view.x) == len(view.normal_jerk) == len(timestamps)


def test_trailing_abs_sum_is_the_abs_sum_of_the_past_window_at_every_sample():
    jerk = np.array([1.0, -2.0, 3.0, -4.0, 5.0])

    np.testing.assert_allclose(trailing_abs_sum(jerk, 3), [1, 3, 6, 9, 12])


def test_series_carries_the_rolling_sum_aligned_with_x():
    jerk, x = _signal()

    series = compute_burst_score_series(jerk, x, FS, SETTINGS)

    assert len(series.rolling_sum) == len(x)
    assert np.isclose(series.rolling_sum[49], 2.0)  # 10 samples of |0.2| ending at sample 49
