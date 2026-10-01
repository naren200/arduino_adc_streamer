"""Chunk-invariance regression test for core.piezo_engine.streaming.CausalDerivedChannels.

Ported from texture_piezo/tests/test_causal_derived_channels.py as the golden
reference for the core/piezo_engine port -- behavior must be unchanged.

The whole point of CausalDerivedChannels is that feeding it the complete
history in one process() call must produce byte-identical output to feeding
the same total samples through many small sequential process() calls (as a
live stream would) -- training, offline inference, and live inference all
rely on this. The shear/normal path's unbounded causal median
(core.piezo_engine.baseline.IncrementalMedian) is the part most likely to
have a subtle carried-state bug, since it must be correct incrementally
across chunk boundaries, not just correct when computed once over a complete
array.
"""

import numpy as np
import pytest

from core.piezo_engine import baseline as data_mod
from core.piezo_engine.streaming import CausalDerivedChannels

PZT_COLUMNS = ["PZT3_B", "PZT3_L", "PZT3_C", "PZT3_R", "PZT3_T"]
N_SAMPLES = 200
SAMPLE_RATE_HZ = 1000.0


def _make_synthetic_series(n_samples: int, n_channels: int, seed: int = 0) -> np.ndarray:
    """Realistic-ish raw ADC counts: slowly varying signal + noise per
    channel, with different phases/offsets per channel so shear/normal's
    opposite-sign detection actually has something to detect."""
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 4 * np.pi, n_samples)
    out = np.empty((n_samples, n_channels), dtype=np.float64)
    for ch in range(n_channels):
        phase = ch * 0.7
        base = 2048 + 300 * np.sin(t + phase)
        out[:, ch] = base + rng.normal(0, 40, size=n_samples)
    return out


def _run_whole(raw: np.ndarray) -> dict:
    channels = CausalDerivedChannels(pzt_columns=PZT_COLUMNS)
    chunk_by_column = {col: raw[:, i] for i, col in enumerate(PZT_COLUMNS)}
    return channels.process(chunk_by_column, sample_rate_hz=SAMPLE_RATE_HZ)


def _run_chunked(raw: np.ndarray, chunk_sizes: list[int]) -> dict:
    assert sum(chunk_sizes) == len(raw)
    channels = CausalDerivedChannels(pzt_columns=PZT_COLUMNS)
    integrated_parts = {col: [] for col in PZT_COLUMNS}
    shear_jerk_lr_parts, shear_jerk_tb_parts, normal_jerk_parts = [], [], []

    start = 0
    for size in chunk_sizes:
        end = start + size
        chunk_by_column = {col: raw[start:end, i] for i, col in enumerate(PZT_COLUMNS)}
        result = channels.process(chunk_by_column, sample_rate_hz=SAMPLE_RATE_HZ)
        for col in PZT_COLUMNS:
            integrated_parts[col].append(result["integrated"][col])
        shear_jerk_lr_parts.append(result["shear_jerk_lr"])
        shear_jerk_tb_parts.append(result["shear_jerk_tb"])
        normal_jerk_parts.append(result["normal_jerk"])
        start = end

    return {
        "integrated": {col: np.concatenate(parts) for col, parts in integrated_parts.items()},
        "shear_jerk_lr": np.concatenate(shear_jerk_lr_parts),
        "shear_jerk_tb": np.concatenate(shear_jerk_tb_parts),
        "normal_jerk": np.concatenate(normal_jerk_parts),
    }


def _chunk_sizes_with_boundary_and_singleton() -> list[int]:
    """Chunk plan covering: a chunk boundary inside the first 10 samples,
    and one chunk of length 1, summing to N_SAMPLES."""
    sizes = [3, 1, 6, 1, 40, 25, 60, 64]
    assert sum(sizes) == N_SAMPLES
    return sizes


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_chunk_invariance(seed):
    raw = _make_synthetic_series(N_SAMPLES, len(PZT_COLUMNS), seed=seed)

    whole = _run_whole(raw)
    chunked = _run_chunked(raw, _chunk_sizes_with_boundary_and_singleton())

    for col in PZT_COLUMNS:
        np.testing.assert_array_equal(
            whole["integrated"][col], chunked["integrated"][col],
            err_msg=f"integrated[{col}] diverged between whole-array and chunked processing",
        )
    np.testing.assert_array_equal(whole["shear_jerk_lr"], chunked["shear_jerk_lr"])
    np.testing.assert_array_equal(whole["shear_jerk_tb"], chunked["shear_jerk_tb"])
    np.testing.assert_array_equal(whole["normal_jerk"], chunked["normal_jerk"])


def test_chunk_invariance_all_singleton_chunks():
    """Extreme case: every chunk has length 1 (worst case for any
    off-by-one in how state carries across process() calls)."""
    raw = _make_synthetic_series(50, len(PZT_COLUMNS), seed=42)
    whole = _run_whole(raw)
    chunked = _run_chunked(raw, [1] * 50)

    for col in PZT_COLUMNS:
        np.testing.assert_array_equal(whole["integrated"][col], chunked["integrated"][col])
    np.testing.assert_array_equal(whole["shear_jerk_lr"], chunked["shear_jerk_lr"])
    np.testing.assert_array_equal(whole["shear_jerk_tb"], chunked["shear_jerk_tb"])
    np.testing.assert_array_equal(whole["normal_jerk"], chunked["normal_jerk"])


def test_reset_clears_state():
    """After reset(), processing the same series again from scratch must
    reproduce the first run's output exactly -- proves reset() clears BOTH
    the bounded rings and the unbounded median heaps, not just one."""
    raw = _make_synthetic_series(80, len(PZT_COLUMNS), seed=7)
    channels = CausalDerivedChannels(pzt_columns=PZT_COLUMNS)
    chunk_by_column = {col: raw[:, i] for i, col in enumerate(PZT_COLUMNS)}

    first = channels.process(chunk_by_column, sample_rate_hz=SAMPLE_RATE_HZ)
    channels.reset()
    second = channels.process(chunk_by_column, sample_rate_hz=SAMPLE_RATE_HZ)

    for col in PZT_COLUMNS:
        np.testing.assert_array_equal(first["integrated"][col], second["integrated"][col])
    np.testing.assert_array_equal(first["shear_jerk_lr"], second["shear_jerk_lr"])
    np.testing.assert_array_equal(first["shear_jerk_tb"], second["shear_jerk_tb"])
    np.testing.assert_array_equal(first["normal_jerk"], second["normal_jerk"])


def test_warmup_sample_count_matches_shared_helper():
    channels = CausalDerivedChannels(pzt_columns=PZT_COLUMNS)
    raw = _make_synthetic_series(10, len(PZT_COLUMNS), seed=1)
    chunk_by_column = {col: raw[:, i] for i, col in enumerate(PZT_COLUMNS)}

    channels.process(chunk_by_column, sample_rate_hz=SAMPLE_RATE_HZ)

    assert channels.warmup_sample_count == data_mod.total_warmup_sample_count(
        SAMPLE_RATE_HZ, channels.jerk_window_samples
    )
    assert channels.samples_seen == 10


def test_samples_seen_accumulates_across_chunks():
    channels = CausalDerivedChannels(pzt_columns=PZT_COLUMNS)
    raw = _make_synthetic_series(N_SAMPLES, len(PZT_COLUMNS), seed=2)

    seen_after_each_chunk = []
    start = 0
    for size in _chunk_sizes_with_boundary_and_singleton():
        end = start + size
        chunk_by_column = {col: raw[start:end, i] for i, col in enumerate(PZT_COLUMNS)}
        channels.process(chunk_by_column, sample_rate_hz=SAMPLE_RATE_HZ)
        seen_after_each_chunk.append(channels.samples_seen)
        start = end

    assert seen_after_each_chunk[-1] == N_SAMPLES
    assert seen_after_each_chunk == sorted(seen_after_each_chunk)


def test_warmup_sample_count_before_first_process_raises():
    channels = CausalDerivedChannels(pzt_columns=PZT_COLUMNS)
    with pytest.raises(ValueError):
        _ = channels.warmup_sample_count


def test_sample_rate_change_mid_stream_raises():
    channels = CausalDerivedChannels(pzt_columns=PZT_COLUMNS)
    raw = _make_synthetic_series(10, len(PZT_COLUMNS), seed=3)
    chunk_by_column = {col: raw[:, i] for i, col in enumerate(PZT_COLUMNS)}

    channels.process(chunk_by_column, sample_rate_hz=SAMPLE_RATE_HZ)
    with pytest.raises(ValueError):
        channels.process(chunk_by_column, sample_rate_hz=SAMPLE_RATE_HZ * 2)
