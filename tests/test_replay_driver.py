"""ReplayDriver / ReplaySetup / resolve_replay_baseline: the Qt-free replay pieces the TouchID tab drives."""

from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from core.piezo_engine.pipeline import ALREADY_CONDITIONED_INPUT, RAW_INPUT
from core.texture_piezo.application.live_channels import DEFAULT_CHANNELS
from core.texture_piezo.application.replay_driver import ReplayDriver
from core.texture_piezo.application.replay_setup import (
    BASELINE_FROM_FILE_LABEL, BASELINE_FROM_GLOBAL, BASELINE_FROM_NONE, ReplaySetup, resolve_replay_baseline,
)
from core.texture_piezo.application.stream_processor import LIVE_ENGINE_CONFIG
from core.texture_piezo.application.v2_gating import V2Gating
from core.texture_piezo.gating.v1.quality_gate import IdleBaseline
from core.texture_piezo.gating.v2.config import GatingV2Config

PZT_COLUMNS = tuple(f"PZT3_{c}" for c in "BLCRT")
FS = 1000.0
WINDOW_SIZE_S = 0.1
HOP_SIZE_S = 0.05
BURST_COUNT = 12
BURST_SAMPLES = 250
GAP_SAMPLES = 150
BURST_AMPLITUDE = 500.0
BURST_FREQUENCY_HZ = 50.0
SIDE_OFFSETS = (-4.0, -2.0, 0.0, 2.0, 4.0)

STRENGTH_ONLY = GatingV2Config(
    idle_gate_enabled=False, strength_gate_enabled=True, concat_enabled=True,
    burst_min=1.0, sustain_min=3, block_s=0.04, sustain_blocks=7, expiry_s=1.0,
)


def make_setup(**overrides) -> ReplaySetup:
    fields = dict(
        gating_version="v2", gating_v2_config=STRENGTH_ONLY, span_stale_timeout_s=1.0,
        engine_config=LIVE_ENGINE_CONFIG, idle_baseline=None, window_size_s=WINDOW_SIZE_S,
        hop_size_s=HOP_SIZE_S, onset_skip_s=0.0, required_channels=tuple(DEFAULT_CHANNELS),
        pzt_columns=PZT_COLUMNS, input_conditioning=RAW_INPUT,
    )
    return ReplaySetup(**{**fields, **overrides})


def make_baseline(**overrides) -> IdleBaseline:
    fields = dict(pzt_columns=list(PZT_COLUMNS), mean=[0.0] * 5, std=[0.1] * 5, fs=FS, k=8.0, captured_duration_s=5.0)
    return IdleBaseline(**{**fields, **overrides})


def make_snapshot(seed: int = 0, start_s: float = 0.0) -> SimpleNamespace:
    """Quiet noise with strong common-mode oscillation bursts (what normal_jerk measures): the strength
    gate emits several windows per burst."""
    rng = np.random.default_rng(seed)
    period = BURST_SAMPLES + GAP_SAMPLES
    total = BURST_COUNT * period
    data = rng.normal(0.0, 0.5, size=(total, 5)) + np.asarray(SIDE_OFFSETS)
    for burst in range(BURST_COUNT):
        start = burst * period + GAP_SAMPLES
        phase = 2 * np.pi * BURST_FREQUENCY_HZ * np.arange(BURST_SAMPLES) / FS
        data[start:start + BURST_SAMPLES] += (BURST_AMPLITUDE * np.sin(phase))[:, None]
    timestamps_s = start_s + np.arange(total) / FS
    resident_range_s = (float(timestamps_s[0]), float(timestamps_s[-1]))
    return SimpleNamespace(
        data=data, timestamps_s=timestamps_s, channel_labels=list(PZT_COLUMNS),
        sample_rate_hz=FS, sweep_count=total, metadata={}, resident_range_s=resident_range_s,
        window_key=lambda: ("test-source", resident_range_s),
    )


def make_driver(snapshot, setup=None) -> ReplayDriver:
    setup = setup or make_setup()
    return ReplayDriver(
        setup.new_processor(), snapshot.data, snapshot.timestamps_s, setup.pzt_columns,
        list(range(5)), FS, setup.hop_size_s,
    )


def emitted_by_plain_loop(snapshot, setup) -> list:
    processor = setup.new_processor()
    hop = round(setup.hop_size_s * FS)
    windows = []
    for start in range(0, snapshot.sweep_count, hop):
        end = min(start + hop, snapshot.sweep_count)
        raw = {col: snapshot.data[start:end, i].astype(np.float64) for i, col in enumerate(PZT_COLUMNS)}
        windows += processor.push_chunk(processor.filter_raw(raw), snapshot.timestamps_s[start:end], FS, now_t=end / FS)
    return windows


def test_the_gating_under_test_is_v2_and_emits_several_windows_in_one_step():
    snapshot = make_snapshot()
    driver = make_driver(snapshot)
    assert isinstance(driver.processor.gating, V2Gating)
    steps = []
    while not driver.is_exhausted:
        steps.append(driver.step())
    assert max(len(step.ready_windows) for step in steps) > 1


def test_driver_keeps_every_emitted_window_in_emission_order():
    snapshot = make_snapshot()
    reference = emitted_by_plain_loop(snapshot, make_setup())
    driver = make_driver(snapshot)
    popped = []
    while not driver.is_exhausted:
        driver.step()
        while driver.has_pending:
            popped.append(driver.pop_pending())
    assert len(popped) == len(reference) > 0
    for got, expected in zip(popped, reference):
        np.testing.assert_array_equal(got.window_ts, expected.window_ts)


def test_a_window_starting_at_a_nonzero_time_yields_windows_at_absolute_times():
    start_s = 100.0
    shifted = make_driver(make_snapshot(start_s=start_s)).run_to_end()
    reference = make_driver(make_snapshot()).run_to_end()
    assert len(shifted) == len(reference) > 0
    for got, expected in zip(shifted, reference):
        np.testing.assert_allclose(got.window_ts, expected.window_ts + start_s)


def test_run_to_end_collects_all_windows():
    snapshot = make_snapshot()
    reference = emitted_by_plain_loop(snapshot, make_setup())
    windows = make_driver(snapshot).run_to_end()
    assert [w.window_ts[0] for w in windows] == [w.window_ts[0] for w in reference]


def test_step_reports_done_on_the_last_hop_and_refuses_afterwards():
    driver = make_driver(make_snapshot())
    last = None
    while not driver.is_exhausted:
        last = driver.step()
    assert last.is_done
    with pytest.raises(ValueError):
        driver.step()


def test_timestamps_fall_back_to_the_sample_index_when_the_snapshot_has_none():
    snapshot = make_snapshot()
    snapshot.timestamps_s = np.empty(0)
    step = make_driver(snapshot).step()
    np.testing.assert_allclose(step.timestamps, np.arange(round(HOP_SIZE_S * FS)) / FS)


def test_equal_setups_compare_equal_and_any_difference_breaks_it():
    assert make_setup() == make_setup()
    assert make_setup() != make_setup(window_size_s=0.2)
    assert make_setup() != make_setup(input_conditioning=ALREADY_CONDITIONED_INPUT)
    assert make_setup() != make_setup(gating_v2_config=None, gating_version="v1")


def test_baselines_compare_by_value_even_when_they_hold_arrays():
    as_lists = make_setup(idle_baseline=make_baseline())
    as_arrays = make_setup(idle_baseline=make_baseline(mean=np.zeros(5), std=np.full(5, 0.1)))
    assert as_lists == as_arrays
    assert as_lists != make_setup(idle_baseline=make_baseline(std=[0.2] * 5))
    assert as_lists != make_setup(idle_baseline=None)


def test_setup_equality_follows_a_baseline_mutated_in_place():
    baseline = make_baseline()
    before = make_setup(idle_baseline=baseline)
    baseline.k = 3.0
    assert before != make_setup(idle_baseline=make_baseline())


def test_new_processor_builds_what_the_setup_describes():
    setup = make_setup(idle_baseline=make_baseline(), onset_skip_s=0.25)
    processor = setup.new_processor()
    assert (processor.gating_version, processor.gating_v2_config) == ("v2", STRENGTH_ONLY)
    assert processor.idle_baseline == make_baseline()
    assert processor.pzt_columns == list(PZT_COLUMNS)
    assert (processor.window_size_s, processor.hop_size_s, processor.onset_skip_s) == (WINDOW_SIZE_S, HOP_SIZE_S, 0.25)
    assert processor.engine_config == LIVE_ENGINE_CONFIG


def test_new_processor_adopts_engine_state_only_for_the_same_engine_config():
    previous = make_setup().new_processor()
    with patch("core.texture_piezo.application.stream_processor.TouchIdStreamProcessor.adopt_engine_state_from") as adopt:
        make_setup().new_processor(continue_from=previous)
        assert adopt.call_count == 1
        other_engine = SimpleNamespace(engine_config=object())
        make_setup().new_processor(continue_from=other_engine)
        assert adopt.call_count == 1


def label_segments(*spans):
    return [{"class": "baseline", "start_s": start, "end_s": end} for start, end in spans]


def resolve(segments, global_baseline, snapshot=None):
    snapshot = snapshot or make_snapshot()
    return resolve_replay_baseline(segments, snapshot, list(range(5)), PZT_COLUMNS, FS, 6.0, global_baseline)


def test_file_label_baseline_is_fit_from_the_labeled_region_with_the_gate_k():
    resolved = resolve(label_segments((0.0, 0.1)), make_baseline())
    assert resolved.source == BASELINE_FROM_FILE_LABEL
    assert resolved.baseline.k == 6.0
    assert resolved.baseline.captured_duration_s == pytest.approx(0.1)


def test_labels_partly_outside_the_window_fit_from_the_part_inside():
    snapshot = make_snapshot(start_s=100.0)
    resolved = resolve(label_segments((99.0, 100.1), (500.0, 501.0)), None, snapshot)
    assert resolved.source == BASELINE_FROM_FILE_LABEL
    assert resolved.baseline.captured_duration_s == pytest.approx(0.1)


def test_other_labels_do_not_make_a_baseline():
    segments = [{"class": "wood", "start_s": 0.0, "end_s": 0.1}]
    assert resolve(segments, None).source == BASELINE_FROM_NONE


def test_a_label_outside_the_recording_falls_back_to_the_global_baseline():
    global_baseline = make_baseline()
    resolved = resolve(label_segments((900.0, 901.0)), global_baseline)
    assert (resolved.source, resolved.baseline) == (BASELINE_FROM_GLOBAL, global_baseline)


def test_without_labels_the_global_baseline_then_none():
    global_baseline = make_baseline()
    assert resolve([], global_baseline).baseline is global_baseline
    assert resolve([], None).baseline is None
    assert resolve([], None).source == BASELINE_FROM_NONE
