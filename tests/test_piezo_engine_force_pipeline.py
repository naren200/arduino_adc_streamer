"""PiezoEnginePipeline with the force stage: chunk invariance on all outputs, timestamps, warmup slicing and speed."""

import time

import numpy as np
import pytest

from core.piezo_engine.config import EngineConfig, TimingMode, TimingPolicy
from core.piezo_engine.pipeline import ALREADY_CONDITIONED_INPUT, PiezoEnginePipeline
from test_piezo_engine_force_stage import assert_bit_equal, random_sizes, synthetic_counts

SYNTHETIC_FS = 1024.0
SYNTHETIC_COLUMNS = [f"PZT3_{position}" for position in "BLCRT"]
CONTINUOUS_CONFIG = EngineConfig(timing=TimingPolicy(TimingMode.CONTINUOUS))
ALL_OUTPUTS = (
    "shear_jerk_lr", "shear_jerk_tb", "normal_jerk", "normal_force", "shear_force_lr", "shear_force_tb",
)
SPEED_SANITY_BOUND_S_PER_100K = 30.0


def _synthetic_stream(n_samples: int, seed: int = 0) -> dict:
    counts = synthetic_counts(n_samples, seed)
    return {column: counts[:, i] for i, column in enumerate(SYNTHETIC_COLUMNS)}


def _slice(stream: dict, start: int, stop: int) -> dict:
    return {column: values[start:stop] for column, values in stream.items()}


def _run(pipeline, stream, sizes, explicit_times=None) -> dict:
    parts = {name: [] for name in ALL_OUTPUTS + ("integrated_C", "raw_C")}
    start = 0
    for size in sizes:
        times = None if explicit_times is None else explicit_times[start:start + size]
        result = pipeline.process(_slice(stream, start, start + size), sample_rate_hz=SYNTHETIC_FS, timestamps_s=times)
        for name in ALL_OUTPUTS:
            parts[name].append(getattr(result, name))
        parts["integrated_C"].append(result.integrated["PZT3_C"])
        parts["raw_C"].append(result.raw["PZT3_C"])
        start += size
    return {name: np.concatenate(chunks) for name, chunks in parts.items()}


# ---------------------------------------------------------------- synthetic pipeline behaviour

def test_all_outputs_are_bit_identical_across_chunkings_including_single_samples_and_the_warmup_boundary():
    n_samples = 4200
    stream = _synthetic_stream(n_samples, 1)
    whole = _run(PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONTINUOUS_CONFIG), stream, [n_samples])
    boundary_sizes = [1] * 450 + [n_samples - 450]
    assert CONTINUOUS_CONFIG.leading_warmup_samples < 450
    for sizes in (boundary_sizes, random_sizes(n_samples, 21), random_sizes(n_samples, 22)):
        chunked = _run(PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONTINUOUS_CONFIG), stream, sizes)
        for name in whole:
            assert_bit_equal(chunked[name], whole[name], name)
    assert np.abs(whole["normal_force"]).max() > 0.1 and np.abs(whole["shear_force_tb"]).max() > 0.05


def test_explicit_timestamps_equal_the_default_grid_when_uniform():
    n_samples = 2500
    stream = _synthetic_stream(n_samples, 2)
    default = _run(PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONTINUOUS_CONFIG), stream, [n_samples])
    explicit_times = np.arange(n_samples) / SYNTHETIC_FS
    explicit = _run(PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONTINUOUS_CONFIG), stream, random_sizes(n_samples, 3), explicit_times)
    for name in default:
        assert_bit_equal(explicit[name], default[name], name)


def test_default_grid_is_global_and_latches_the_first_sample_rate():
    stream = _synthetic_stream(2500, 3)
    reference = _run(PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONTINUOUS_CONFIG), stream, [2500])
    drifting = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONTINUOUS_CONFIG)
    parts, start = [], 0
    for index, size in enumerate(random_sizes(2500, 6)):
        result = drifting.process(_slice(stream, start, start + size), sample_rate_hz=SYNTHETIC_FS * (1 + 1e-3 * (index % 3)))
        parts.append(result.normal_force)
        start += size
    assert_bit_equal(np.concatenate(parts), reference["normal_force"])


def test_timestamp_length_must_match_the_input_chunk():
    pipeline = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONTINUOUS_CONFIG)
    with pytest.raises(ValueError, match="timestamps_s has 5 entries for 10 input samples"):
        pipeline.process(_slice(_synthetic_stream(10), 0, 10), sample_rate_hz=SYNTHETIC_FS, timestamps_s=np.arange(5.0))


def test_force_is_not_gated_by_the_warmup_only_sliced_by_it():
    stream = _synthetic_stream(1500, 4)
    config = EngineConfig(timing=CONTINUOUS_CONFIG.timing, integration_window_samples=30, jerk_window_samples=22)
    pipeline = PiezoEnginePipeline(SYNTHETIC_COLUMNS, config, input_conditioning=ALREADY_CONDITIONED_INPUT)
    result = pipeline.process(stream, sample_rate_hz=SYNTHETIC_FS)
    assert result.dropped_leading.total == 29
    assert len(result.normal_force) == len(result.shear_jerk_lr) == len(result.raw["PZT3_B"]) == 1500 - 29
    ungated = PiezoEnginePipeline(SYNTHETIC_COLUMNS, EngineConfig(timing=CONTINUOUS_CONFIG.timing, integration_window_samples=2, jerk_window_samples=2),
                                  input_conditioning=ALREADY_CONDITIONED_INPUT).process(stream, sample_rate_hz=SYNTHETIC_FS)
    assert_bit_equal(result.normal_force, ungated.normal_force[28:])


def test_speed_of_the_full_pipeline_including_force_is_reported(capsys):
    n_samples = 100_000
    stream = _synthetic_stream(n_samples, 5)
    pipeline = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONTINUOUS_CONFIG)
    started = time.perf_counter()
    pipeline.process(stream, sample_rate_hz=SYNTHETIC_FS)
    seconds_per_100k = time.perf_counter() - started
    with capsys.disabled():
        print(f"\nfull pipeline incl. force: {seconds_per_100k:.2f} s per 100k samples")
    assert seconds_per_100k < SPEED_SANITY_BOUND_S_PER_100K
