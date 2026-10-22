"""Unit tests for the pure gating-v2 pieces: strength stage, idle stage, combiner, good queue.

Streams are synthetic at FS = 1000 Hz so a 0.04 s block is 40 samples and indices read as
milliseconds.
"""

import numpy as np
import pytest

from core.texture_piezo.application.sample_store import ContinuousSampleStore
from core.texture_piezo.gating.v1.quality_gate import IdleBaseline
from core.texture_piezo.gating.v1.segmentation import WARMUP_SAMPLES, ActiveSampleQueue
from core.texture_piezo.gating.v2.combiner import GoodRangeCombiner
from core.texture_piezo.gating.v2.config import GatingV2Config
from gating_v2_support import make_v2_config
from core.texture_piezo.gating.v2.good_queue import GoodQueue, GoodQueueConfig, StoreTimes, WindowSpan
from core.texture_piezo.gating.v2.idle_stage import IdleStage
from core.texture_piezo.gating.v2.ranges import intersect_ranges, seam_indices
from core.texture_piezo.gating.v2.strong_signal import StrongSignalStage, block_burst_scores

FS = 1000.0
BLOCK_N = 40
BURST_MIN = 10.0
PZT_COLUMNS = [f"PZT3_{c}" for c in "BLCRT"]
WINDOW_S = 0.5
HOP_S = 0.1


def _config(**overrides) -> GatingV2Config:
    return make_v2_config(**{"burst_min": BURST_MIN, **overrides})


def _jerk_from_strong_blocks(pattern: list[int]) -> np.ndarray:
    """Per-sample |normal_jerk| of 1.0 in blocks flagged 1 (score 40 > BURST_MIN), else 0."""
    return np.repeat(np.asarray(pattern, dtype=float), BLOCK_N)


def _feed_in_ticks(stage, values: np.ndarray, tick: int) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    for start in range(0, len(values), tick):
        pieces.extend(stage.feed(values[start:start + tick], FS))
    return pieces


def _times(n_samples: int, base_abs: int = 0) -> StoreTimes:
    return StoreTimes((base_abs + np.arange(n_samples - base_abs)) / FS, base_abs, 1.0 / FS)


# --- strength stage ---------------------------------------------------------------------------


def test_burst_score_is_the_plain_sum_of_absolute_jerk():
    jerk = np.array([-1.0, 2.0, -3.0, 4.0, 10.0, -10.0])
    assert block_burst_scores(jerk, 2).tolist() == [3.0, 7.0, 20.0]
    assert block_burst_scores(jerk, 4).tolist() == [10.0]


def test_a_block_is_strong_only_above_burst_min():
    stage = StrongSignalStage(_config(burst_min=40.0, sustain_min=0))
    at_threshold = np.ones(BLOCK_N)  # score exactly 40 -> not strong
    assert stage.feed(at_threshold, FS) == []
    assert stage.feed(at_threshold * 1.01, FS) == [(BLOCK_N, 2 * BLOCK_N)]


def test_sustain_trips_on_the_second_strong_block_and_backdates_to_the_first():
    jerk = _jerk_from_strong_blocks([0, 0, 1, 1, 1, 0, 0, 0, 1, 0, 1, 1])
    pieces = StrongSignalStage(_config()).feed(jerk, FS)
    # b3 trips (b2,b3 strong) back-dated to b2's start; b4 extends; b5 drops (1 of 2);
    # a lone b8 / b10 never trips; b11 trips with b10.
    assert pieces == [(80, 160), (160, 200), (400, 480)]


def test_with_sustain_min_zero_one_strong_block_trips_and_the_range_ends_on_it():
    jerk = _jerk_from_strong_blocks([0, 0, 1, 0, 0, 0])
    assert StrongSignalStage(_config(sustain_min=0)).feed(jerk, FS) == [(80, 120)]


def test_backdating_reaches_across_quiet_blocks_inside_the_lookback():
    jerk = _jerk_from_strong_blocks([0, 1, 0, 1])
    pieces = StrongSignalStage(_config(sustain_blocks=3)).feed(jerk, FS)
    assert pieces == [(40, 160)]


def test_a_range_never_ends_on_a_trailing_quiet_block():
    jerk = _jerk_from_strong_blocks([1, 1, 1, 0, 0, 0])
    assert StrongSignalStage(_config()).feed(jerk, FS) == [(0, 80), (80, 120)]


def test_strength_pieces_do_not_depend_on_the_tick_size():
    rng = np.random.default_rng(7)
    pattern = (rng.random(300) < 0.5).astype(int).tolist()
    jerk = _jerk_from_strong_blocks(pattern) * rng.uniform(0.5, 1.5, size=300 * BLOCK_N)
    expected = _feed_in_ticks(StrongSignalStage(_config()), jerk, len(jerk))
    assert len(expected) > 5
    for tick in (1, 77, 500, 1538):
        assert _feed_in_ticks(StrongSignalStage(_config()), jerk, tick) == expected


# --- idle stage -------------------------------------------------------------------------------


def _baseline() -> IdleBaseline:
    return IdleBaseline(
        pzt_columns=list(PZT_COLUMNS), mean=[0.0] * 5, std=[1.0] * 5, fs=FS, k=8.0, captured_duration_s=5.0,
    )


def _store_from_activity(activity: list[int], micro_n: int = 50) -> ContinuousSampleStore:
    """One store sample block per entry: 100.0 (active, far outside 8 sigma) or 0.0 (idle)."""
    levels = np.repeat(np.asarray(activity, dtype=float) * 100.0, micro_n)
    store = ContinuousSampleStore(PZT_COLUMNS, channel_names=("PZT3_B",))
    no_integration = {"integrated": {col: np.zeros(len(levels)) for col in PZT_COLUMNS}}
    store.append({col: levels for col in PZT_COLUMNS}, no_integration, np.arange(len(levels)) / FS)
    return store


# idle_gap_chunks_cap(0.5) == 3: the single idle chunk is absorbed, the run of 3 is stripped
ACTIVITY = [0, 1, 0, 1, 0, 0, 0, 1, 1, 0, 0, 0, 0]


def test_idle_stage_absorbs_short_gaps_and_strips_runs_at_the_cap():
    pieces = IdleStage(_baseline(), WINDOW_S).feed(_store_from_activity(ACTIVITY), FS)
    assert pieces == [(50, 100), (100, 200), (350, 400), (400, 450)]


def test_idle_stage_reports_an_absorbed_gap_only_once_activity_resumes():
    stage = IdleStage(_baseline(), WINDOW_S)
    assert stage.feed(_store_from_activity([0, 1, 0]), FS) == [(50, 100)]


def test_idle_stage_ranges_equal_v1_fragments():
    gap = [0] * 12  # beyond v1's strip cap plus merge grace, which v2 deliberately lacks
    store = _store_from_activity([0, 1, 0, 1] + gap + [1, 1] + gap + [1, 1, 0, 1] + gap)
    ranges = _merged(IdleStage(_baseline(), WINDOW_S).feed(store, FS))
    queue = ActiveSampleQueue(window_size_s=WINDOW_S, hop_size_s=HOP_S, baseline=_baseline(), onset_skip_s=0.0)
    for start in range(0, store.next_abs, 50):
        queue.push_micro_chunk((start, start + 50), store.raw(start, start + 50), start / FS, now_t=start / FS)
    v1_fragments = sorted((f.start_idx, f.end_idx) for f in queue._fragments)
    assert ranges == v1_fragments


def test_idle_stage_k_scales_the_band():
    store = _store_from_activity([1, 1])  # |100| sigma
    stage = IdleStage(_baseline(), WINDOW_S)
    stage.k = 1000.0
    assert stage.feed(store, FS) == []


def _merged(pieces: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in pieces:
        if merged and merged[-1][1] == start:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


# --- range helpers and combiner ---------------------------------------------------------------


def test_intersect_ranges_keeps_only_the_overlaps():
    assert intersect_ranges([(0, 10), (20, 40)], [(5, 25), (30, 35), (50, 60)]) == [(5, 10), (20, 25), (30, 35)]


def test_seam_indices_are_the_concatenated_start_of_every_later_range():
    assert seam_indices([(10, 20), (50, 55), (100, 130)]) == (10, 15)
    assert seam_indices([(10, 20)]) == ()


def test_combiner_intersects_the_stages():
    combiner = GoodRangeCombiner(stage_count=2, onset_skip_s=0.0)
    times = _times(2000)
    combined = combiner.combine([[(1000, 1320), (1600, 1920)], [(1100, 2100)]], times)
    assert combined == [(1100, 1320), (1600, 1920)]


def test_combiner_emits_a_late_stage_overlap_once_the_other_stage_catches_up():
    combiner = GoodRangeCombiner(stage_count=2, onset_skip_s=0.0)
    assert combiner.combine([[(400, 600)], []], _times(700)) == []
    assert combiner.combine([[], [(400, 500)]], _times(800)) == [(400, 500)]
    assert combiner.combine([[], [(500, 650)]], _times(900)) == [(500, 600)]


def test_combiner_without_stages_passes_the_whole_stream_after_warmup():
    combiner = GoodRangeCombiner(stage_count=0, onset_skip_s=0.0)
    assert combiner.combine([], _times(150)) == []
    assert combiner.combine([], _times(500)) == [(WARMUP_SAMPLES, 500)]
    assert combiner.combine([], _times(900)) == [(500, 900)]


def test_combiner_drops_the_onset_of_every_new_range_but_not_of_extensions():
    combiner = GoodRangeCombiner(stage_count=1, onset_skip_s=0.03)
    times = _times(3000)
    assert combiner.combine([[(1000, 1020)]], times) == []  # entirely inside the onset
    assert combiner.combine([[(1020, 1100)]], times) == [(1030, 1100)]  # same range: skip continues
    assert combiner.combine([[(1100, 1200)]], times) == [(1100, 1200)]  # extension: no new skip
    assert combiner.combine([[(2000, 2100)]], times) == [(2030, 2100)]  # new range


def test_combiner_clips_warmup_samples():
    combiner = GoodRangeCombiner(stage_count=1, onset_skip_s=0.0)
    assert combiner.combine([[(100, 400)]], _times(500)) == [(WARMUP_SAMPLES, 400)]


# --- good queue -------------------------------------------------------------------------------


def _queue(concat_enabled: bool = True, window_s: float = WINDOW_S, expiry_s: float = 3.0) -> GoodQueue:
    return GoodQueue(GoodQueueConfig(window_s, HOP_S, expiry_s, concat_enabled))


def _push(queue: GoodQueue, ranges, times) -> list[WindowSpan]:
    return list(queue.push(ranges, times).windows)


def _bounds(queue: GoodQueue) -> list[tuple[int, int, int]]:
    return [(r.range_id, r.start_idx, r.end_idx) for r in queue.ranges]


def test_touching_ranges_merge_and_keep_the_older_id():
    queue = _queue(window_s=10.0)
    _push(queue, [(0, 100)], _times(500))
    _push(queue, [(100, 200), (300, 400)], _times(500))
    assert _bounds(queue) == [(0, 0, 200), (1, 300, 400)]
    assert queue.ranges[0].end_ts == pytest.approx(0.199)


def test_range_ids_only_increase():
    queue = _queue(window_s=10.0)
    for start in (0, 200, 400):
        _push(queue, [(start, start + 50)], _times(600))
    assert [r.range_id for r in queue.ranges] == [0, 1, 2]


def test_expiry_deletes_old_ranges_and_advances_a_straddling_head_only():
    queue = _queue(window_s=10.0)
    _push(queue, [(0, 200), (1500, 2500), (3000, 3100)], _times(5000))
    # horizon = 4.999 - 3.0 = 1.999 s: first range gone, second cut at index 1999, third untouched
    assert _bounds(queue) == [(1, 1999, 2500), (2, 3000, 3100)]
    assert queue.ranges[0].start_ts == pytest.approx(1.999)


def test_a_single_range_yields_a_window_per_hop():
    queue = _queue()
    spans = _push(queue, [(0, 1000)], _times(1000))
    assert [span.ranges for span in spans] == [((s, s + 500),) for s in range(0, 600, 100)]
    assert _bounds(queue) == [(0, 600, 1000)]


def test_a_window_spans_ranges_and_the_hop_trim_crosses_the_boundary():
    queue = _queue()
    spans = _push(queue, [(0, 40), (600, 1100)], _times(1100))
    assert spans[0].ranges == ((0, 40), (600, 1060))
    assert spans[0].range_id == 0
    # the 0.1 s hop removes the whole 0.04 s head range and 0.06 s of the next one
    assert queue.ranges[0].range_id == 1
    assert _bounds(queue)[0][1:] == (660, 1100)
    assert len(spans) == 1


def test_good_signal_shorter_than_a_window_waits_and_is_only_dropped_by_expiry():
    queue = _queue()
    assert _push(queue, [(0, 300)], _times(400)) == []
    assert _bounds(queue) == [(0, 0, 300)]
    assert _push(queue, [], _times(3600)) == []
    assert _bounds(queue) == []


def test_several_windows_can_be_emitted_in_one_push():
    queue = _queue()
    spans = _push(queue, [(0, 300), (400, 800), (900, 1200)], _times(1200))
    assert len(spans) >= 3
    assert all(sum(end - start for start, end in span.ranges) == 500 for span in spans)


def test_without_concat_a_window_comes_from_one_range_only():
    queue = _queue(concat_enabled=False)
    assert _push(queue, [(0, 300), (600, 1000)], _times(1000)) == []
    spans = _push(queue, [(1000, 1200)], _times(1200))  # touches the second range -> 600 samples long
    assert [span.ranges for span in spans] == [((600, 1100),), ((700, 1200),)]
    assert spans[0].range_id == 1
    assert _bounds(queue)[0] == (0, 0, 300)


def test_push_reports_what_changed():
    queue = _queue(window_s=10.0)
    first = queue.push([(0, 100)], _times(500)).changes
    assert [(r.range_id, r.start_idx, r.end_idx) for r in first.opened] == [(0, 0, 100)]
    assert first.merged == first.extended == first.expired == ()

    second = queue.push([(100, 150), (150, 200), (300, 400)], _times(500)).changes
    assert [r.range_id for r in second.opened] == [1]
    assert [(r.range_id, r.end_idx) for r in second.merged] == [(0, 150), (0, 200)]
    assert [(r.range_id, r.end_idx) for r in second.extended] == [(0, 200)]


def test_a_range_opened_and_merged_in_one_push_is_not_reported_as_extended():
    changes = _queue(window_s=10.0).push([(0, 50), (50, 100)], _times(500)).changes
    assert len(changes.opened) == 1 and len(changes.merged) == 1
    assert changes.extended == ()


def test_push_reports_expired_ranges_but_not_a_head_cut_short():
    queue = _queue(window_s=10.0)
    changes = queue.push([(0, 200), (1500, 2500), (3000, 3100)], _times(5000)).changes
    assert [r.range_id for r in changes.expired] == [0]


# --- LiveWindow.seam_idx ----------------------------------------------------------------------


def test_live_window_seam_idx_defaults_to_contiguous_and_validates():
    from core.piezo_engine.live_window import LiveWindow

    channels = {"PZT3_B": np.arange(10.0)}
    assert LiveWindow(channels, FS).seam_idx == ()
    assert LiveWindow(channels, FS, seam_idx=[3, 7]).seam_idx == (3, 7)
    for bad in ((0,), (10,), (7, 3), (3, 3)):
        with pytest.raises(ValueError, match="seam_idx"):
            LiveWindow(channels, FS, seam_idx=bad)
