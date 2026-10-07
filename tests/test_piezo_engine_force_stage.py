"""ForceStage and its sub-stages: rate stage vs the batch series, median centering vs a brute-force running
median, chunk invariance, reset, timestamps and settings sensitivity."""

import dataclasses

import numpy as np
import pytest

from core.piezo_engine.config import ANALYSIS_FORCE_SETTINGS, EngineConfig, TimingMode, TimingPolicy
from core.piezo_engine.force_integrator import compute_pzt_force_rate_series
from core.piezo_engine.force_settings import ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS
from core.piezo_engine.force_stage import (
    ExpandingMedianCentering,
    ForceRateStage,
    ForceStage,
)
from core.piezo_engine.shear_constants import SHEAR_SENSOR_POSITIONS
from core.piezo_engine.streaming import counts_to_volts

FS = 1024.0  # dyadic: sample times and per-channel offsets are exact in binary floating point
COLUMNS = [f"PZT3_{position}" for position in "BLCRT"]
COLUMN_MAP = dict(zip("BLCRT", COLUMNS))
POSITIONS_BY_COLUMN_INDEX = ("B", "L", "C", "R", "T")
ANALYSIS_SETTINGS = dict(ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS)
LEAK_DT_S = 2.1583e-05
VREF = 3.3
CONTINUOUS = TimingPolicy(TimingMode.CONTINUOUS)
MANUAL = TimingPolicy(TimingMode.MANUAL, LEAK_DT_S)
MANUAL_WITH_DECAY = TimingPolicy(
    TimingMode.MANUAL, LEAK_DT_S, {"PZT3_B": 4e-6, "PZT3_L": 9e-6, "PZT3_C": 6e-6, "PZT3_R": 2e-6, "PZT3_T": 7e-6})
FORCE_TRACE_LABELS = {
    "normal_force": "Normal Force [N]", "shear_force_lr": "Shear Force L/R [N]", "shear_force_tb": "Shear Force T/B [N]",
}


def _bits(values: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(values, dtype=np.float64).view(np.int64)


def assert_bit_equal(actual, expected, message: str = "") -> None:
    actual, expected = np.asarray(actual, dtype=np.float64), np.asarray(expected, dtype=np.float64)
    assert actual.shape == expected.shape, message
    assert np.array_equal(_bits(actual), _bits(expected)), f"{message} max abs diff {np.abs(actual - expected).max()}"


def synthetic_counts(n_samples: int, seed: int = 0) -> np.ndarray:
    """(n, 5) counts in B/L/C/R/T order: noisy 2048 baseline plus decaying press/shear events, then silence."""
    rng = np.random.default_rng(seed)
    counts = 2048.0 + rng.integers(-2, 3, size=(n_samples, 5)).astype(np.float64)
    active_end = n_samples // 3
    for start in range(40, active_end, 260):
        shear_lr, shear_tb, press = rng.uniform(-300, 300), rng.uniform(-300, 300), rng.uniform(-400, 400)
        amplitudes = np.array([-shear_tb, -shear_lr, press, shear_lr, shear_tb]) + 0.3 * press
        length = min(80, n_samples - start)
        counts[start:start + length] += amplitudes * np.exp(-np.arange(length) / 20.0)[:, None]
    return np.round(counts)


def volts_by_position(counts: np.ndarray) -> dict:
    return {position: counts_to_volts(counts[:, i], VREF) for i, position in enumerate(POSITIONS_BY_COLUMN_INDEX)}


def times_for(n_samples: int, start: int = 0) -> np.ndarray:
    return (np.arange(n_samples) + start) / FS


def _slice_volts(volts: dict, start: int, stop: int) -> dict:
    return {position: values[start:stop] for position, values in volts.items()}


def push_in_chunks(stage: ForceStage, volts: dict, times: np.ndarray, sizes: list[int]) -> dict:
    parts = {name: [] for name in FORCE_TRACE_LABELS}
    start = 0
    for size in sizes:
        traces = stage.push(_slice_volts(volts, start, start + size), times[start:start + size])
        for name in parts:
            parts[name].append(getattr(traces, name))
        start += size
    return {name: np.concatenate(chunks) for name, chunks in parts.items()}


def random_sizes(n_samples: int, seed: int) -> list[int]:
    rng = np.random.default_rng(seed)
    sizes, total = [], 0
    while total < n_samples:
        size = min(int(rng.choice([1, 1, 2, 5, 31, 200, 900])), n_samples - total)
        sizes.append(size)
        total += size
    return sizes


def _engine_config(policy: TimingPolicy) -> EngineConfig:
    return EngineConfig(timing=policy, force=ANALYSIS_FORCE_SETTINGS)


# ---------------------------------------------------------------- sub-stages

@pytest.mark.parametrize("policy", [CONTINUOUS, MANUAL, MANUAL_WITH_DECAY], ids=["continuous", "manual", "decay"])
def test_rate_stage_equals_the_batch_rate_series_for_any_chunking(policy):
    n_samples = 3000
    centered = {p: v - np.median(v) for p, v in volts_by_position(synthetic_counts(n_samples, 4)).items()}
    jitter = np.cumsum(np.random.default_rng(8).uniform(0.5e-3, 1.5e-3, n_samples))
    for sizes in ([n_samples], [1] * 400 + [n_samples - 400], random_sizes(n_samples, 2)):
        decay = {p: policy.pre_sample_decay_s_by_label.get(COLUMN_MAP[p]) for p in SHEAR_SENSOR_POSITIONS}
        stage = ForceRateStage(
            {p: ANALYSIS_FORCE_SETTINGS.physical_params(p) for p in SHEAR_SENSOR_POSITIONS},
            policy.leak_dt_s, {p: v for p, v in decay.items() if v is not None},
        )
        parts, start = {p: [] for p in SHEAR_SENSOR_POSITIONS}, 0
        for size in sizes:
            rates = stage.push(_slice_volts(centered, start, start + size), jitter[start:start + size])
            for p in parts:
                parts[p].append(rates[p])
            start += size
        for p in SHEAR_SENSOR_POSITIONS:
            reference = compute_pzt_force_rate_series(
                centered[p], jitter, ANALYSIS_FORCE_SETTINGS.physical_params(p),
                leak_dt_s=policy.leak_dt_s, pre_sample_decay_dt_s=decay[p],
            )
            assert_bit_equal(np.concatenate(parts[p]), reference, f"{p} sizes[:3]={sizes[:3]}")


BRUTE_FORCE_MEDIAN_SAMPLES = 700


def test_median_centering_equals_volts_minus_a_brute_force_running_median():
    volts = volts_by_position(synthetic_counts(BRUTE_FORCE_MEDIAN_SAMPLES, 11))
    stage, parts, start = ExpandingMedianCentering(), {p: [] for p in volts}, 0
    for size in random_sizes(BRUTE_FORCE_MEDIAN_SAMPLES, 3):
        centered = stage.push(_slice_volts(volts, start, start + size))
        for p in parts:
            parts[p].append(centered[p])
        start += size
    for p, values in volts.items():
        running_median = np.array([np.median(values[:i + 1]) for i in range(len(values))])
        assert_bit_equal(np.concatenate(parts[p]), values - running_median, p)


# ---------------------------------------------------------------- ForceStage

@pytest.mark.parametrize("policy", [CONTINUOUS, MANUAL_WITH_DECAY], ids=["continuous", "decay"])
def test_stage_is_bit_identical_for_whole_vs_many_random_chunkings_including_single_samples(policy):
    counts = synthetic_counts(9000, 8)
    volts, times = volts_by_position(counts), times_for(len(counts))
    whole = push_in_chunks(ForceStage(COLUMN_MAP, _engine_config(policy)), volts, times, [len(counts)])
    for seed, sizes in enumerate([[1] * 700 + [len(counts) - 700], random_sizes(len(counts), 4), random_sizes(len(counts), 5)]):
        chunked = push_in_chunks(ForceStage(COLUMN_MAP, _engine_config(policy)), volts, times, sizes)
        for name in whole:
            assert_bit_equal(chunked[name], whole[name], f"{name} chunking {seed}")


def test_reset_restores_the_initial_state():
    counts = synthetic_counts(3000, 9)
    volts, times = volts_by_position(counts), times_for(len(counts))
    stage = ForceStage(COLUMN_MAP, _engine_config(MANUAL))
    first = push_in_chunks(stage, volts, times, [1500, 1500])
    stage.reset()
    second = push_in_chunks(stage, volts, times, [1500, 1500])
    for name in first:
        assert_bit_equal(second[name], first[name], name)


def test_first_sample_has_zero_rate_so_zero_force_and_later_chunks_see_their_predecessor():
    counts = synthetic_counts(500, 10)
    volts, times = volts_by_position(counts), times_for(len(counts))
    stage = ForceStage(COLUMN_MAP, _engine_config(CONTINUOUS))
    first = stage.push(_slice_volts(volts, 0, 1), times[:1])
    assert first.normal_force[0] == first.shear_force_lr[0] == first.shear_force_tb[0] == 0.0
    rest = stage.push(_slice_volts(volts, 1, 500), times[1:])
    whole = push_in_chunks(ForceStage(COLUMN_MAP, _engine_config(CONTINUOUS)), volts, times, [500])
    assert_bit_equal(np.concatenate([first.normal_force, rest.normal_force]), whole["normal_force"])


def test_empty_push_returns_empty_traces_and_keeps_state():
    counts = synthetic_counts(300, 12)
    volts, times = volts_by_position(counts), times_for(len(counts))
    stage = ForceStage(COLUMN_MAP, _engine_config(MANUAL))
    empty = stage.push(_slice_volts(volts, 0, 0), times[:0])
    assert empty.n_samples == 0
    assert_bit_equal(stage.push(volts, times).normal_force,
                     ForceStage(COLUMN_MAP, _engine_config(MANUAL)).push(volts, times).normal_force)


def test_non_increasing_timestamps_raise():
    volts = volts_by_position(synthetic_counts(50, 13))
    stage = ForceStage(COLUMN_MAP, _engine_config(CONTINUOUS))
    stage.push(_slice_volts(volts, 0, 25), times_for(25))
    with pytest.raises(ValueError, match="strictly increasing"):
        stage.push(_slice_volts(volts, 25, 50), times_for(25, start=24))


def test_decay_labels_matching_no_engine_column_fail_fast():
    policy = TimingPolicy(TimingMode.MANUAL, LEAK_DT_S, {"PZT5_C": 1e-5})
    with pytest.raises(ValueError, match="match none of the engine columns"):
        ForceStage(COLUMN_MAP, _engine_config(policy))


def test_force_settings_change_the_numbers():
    counts = synthetic_counts(4000, 14)
    volts, times = volts_by_position(counts), times_for(len(counts))
    baseline = push_in_chunks(ForceStage(COLUMN_MAP, _engine_config(MANUAL)), volts, times, [4000])
    changed_force = dataclasses.replace(ANALYSIS_FORCE_SETTINGS, d33_pc_per_n=240.0)
    changed = push_in_chunks(
        ForceStage(COLUMN_MAP, EngineConfig(timing=MANUAL, force=changed_force)), volts, times, [4000])
    assert not np.array_equal(baseline["normal_force"], changed["normal_force"])
    smoothing = push_in_chunks(
        ForceStage(COLUMN_MAP, EngineConfig(timing=MANUAL, smoothing_window_samples=1)), volts, times, [4000])
    assert not np.array_equal(baseline["shear_force_lr"], smoothing["shear_force_lr"])
