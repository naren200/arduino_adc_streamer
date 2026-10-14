"""normal_jerk_residue_<B|L|C|R|T>: the shear-removed per-sensor residuals the engine
computes on the way to normal_jerk, exposed as named channels."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from core.piezo_engine.batch import compute_shear_normal_batch, compute_shear_residual_batch
from core.piezo_engine.channel_names import (
    CHANNEL_LABELS, ENGINE_CHANNEL_NAMES, NORMAL_JERK_RESIDUE_COLUMNS,
)
from core.piezo_engine.config import EngineConfig, TimingMode, TimingPolicy
from core.piezo_engine.pipeline import PiezoEnginePipeline
from core.piezo_engine.streaming import CausalDerivedChannels
from core.texture_piezo.application.live_channels import DEFAULT_CHANNELS, named_engine_channels
from core.texture_piezo.application.stream_processor import TouchIdStreamProcessor

SUM_TOLERANCE = 1e-9
SENSOR_COLUMNS = [f"PZT5_{label}" for label in CHANNEL_LABELS]
FS = 1000.0
CONFIG = EngineConfig(timing=TimingPolicy(mode=TimingMode.CONTINUOUS), compute_force=False)
WITH_RESIDUE = DEFAULT_CHANNELS + NORMAL_JERK_RESIDUE_COLUMNS


def _centered(c, l, r, t, b):
    return {name: np.asarray(values, dtype=np.float64) for name, values in zip("CLRTB", (c, l, r, t, b))}


def _adversarial_centered():
    rng = np.random.default_rng(3)
    random = rng.normal(0.0, 1.0, size=(5, 500))
    ties = np.array([[0.0, 1.0, -1.0, 2.0, -2.0, 0.0, -0.0, 1e-12, -1e-12, 3.0]] * 5)
    ties[1], ties[2] = ties[2], ties[1] * -1.0
    ties[3] = np.roll(ties[3], 3)
    stacked = np.concatenate([random, ties], axis=1)
    return _centered(*stacked)


def test_residue_sums_to_normal_jerk_on_random_and_adversarial_data():
    centered = _adversarial_centered()
    _, _, normal_jerk = compute_shear_normal_batch(centered)
    residue = compute_shear_residual_batch(centered)
    total = sum(residue[label] for label in "CLRTB")
    np.testing.assert_allclose(total, normal_jerk, rtol=0.0, atol=SUM_TOLERANCE)


def test_shear_pair_example_from_shear_detector_docstring():
    centered = _centered(c=[5.0], l=[-3.0], r=[4.0], t=[0.0], b=[0.0])
    residue = compute_shear_residual_batch(centered)
    assert residue["L"][0] == 0.0
    assert residue["R"][0] == 1.0
    assert residue["C"][0] == 5.0 and residue["T"][0] == 0.0 and residue["B"][0] == 0.0


def test_channel_names_follow_pzt_order_and_are_engine_channels():
    assert NORMAL_JERK_RESIDUE_COLUMNS == tuple(f"normal_jerk_residue_{label}" for label in "BLCRT")
    assert set(NORMAL_JERK_RESIDUE_COLUMNS) <= ENGINE_CHANNEL_NAMES


def _raw_counts(n_samples, seed=0):
    rng = np.random.default_rng(seed)
    return {col: 2048 + rng.normal(0.0, 60.0, n_samples) for col in SENSOR_COLUMNS}


def _process_in_chunks(raw, sizes):
    channels = CausalDerivedChannels(pzt_columns=SENSOR_COLUMNS)
    parts = {name: [] for name in NORMAL_JERK_RESIDUE_COLUMNS}
    start = 0
    for size in sizes:
        chunk = {col: samples[start:start + size] for col, samples in raw.items()}
        residue = channels.process(chunk, sample_rate_hz=FS)["normal_jerk_residue"]
        for name in NORMAL_JERK_RESIDUE_COLUMNS:
            parts[name].append(residue[name])
        start += size
    return {name: np.concatenate(chunks) for name, chunks in parts.items()}


@pytest.mark.parametrize("sizes", [[1] * 40, [3, 1, 6, 1, 29], [40]])
def test_chunked_streaming_equals_one_shot(sizes):
    raw = _raw_counts(sum(sizes), seed=7)
    one_shot = _process_in_chunks(raw, [sum(sizes)])
    chunked = _process_in_chunks(raw, sizes)
    for name in NORMAL_JERK_RESIDUE_COLUMNS:
        np.testing.assert_array_equal(one_shot[name], chunked[name])


def test_pipeline_result_exposes_trimmed_residue_by_name():
    raw = _raw_counts(600, seed=1)
    result = PiezoEnginePipeline(SENSOR_COLUMNS, CONFIG).process(raw, sample_rate_hz=FS)
    assert tuple(result.normal_jerk_residue) == NORMAL_JERK_RESIDUE_COLUMNS
    for name in NORMAL_JERK_RESIDUE_COLUMNS:
        assert len(result.normal_jerk_residue[name]) == result.n_samples
    total = sum(result.normal_jerk_residue.values())
    np.testing.assert_allclose(total, result.normal_jerk, rtol=0.0, atol=SUM_TOLERANCE)


def test_live_window_carries_residue_channels_equal_to_engine_slice():
    n_samples, chunk_n, window_n = 2500, 50, 100
    raw = _raw_counts(n_samples, seed=2)
    processor = TouchIdStreamProcessor(
        pzt_columns=SENSOR_COLUMNS, window_size_s=0.1, hop_size_s=0.05, span_stale_timeout_s=1.0,
        idle_baseline=None, engine_config=CONFIG, required_channels=WITH_RESIDUE,
    )
    ready = []
    for start in range(0, n_samples, chunk_n):
        chunk = {col: samples[start:start + chunk_n] for col, samples in raw.items()}
        timestamps = (start + np.arange(chunk_n)) / FS
        ready += processor.push_chunk(processor.filter_raw(chunk), timestamps, FS, now_t=timestamps[0])
    assert ready

    result = PiezoEnginePipeline(SENSOR_COLUMNS, CONFIG).process(raw, sample_rate_hz=FS)
    derived = {"integrated": dict(result.integrated), "shear_jerk_lr": result.shear_jerk_lr,
               "shear_jerk_tb": result.shear_jerk_tb, "normal_jerk": result.normal_jerk,
               **result.normal_jerk_residue}
    reference = named_engine_channels(SENSOR_COLUMNS, result.raw, derived, WITH_RESIDUE)
    trimmed_ts = (np.arange(n_samples) / FS)[result.dropped_leading.total:]
    for ready_window in ready:
        assert set(ready_window.window.channels) == set(WITH_RESIDUE)
        start = int(np.searchsorted(trimmed_ts, ready_window.window_ts[0]))
        for name in NORMAL_JERK_RESIDUE_COLUMNS:
            np.testing.assert_array_equal(
                ready_window.window.channels[name], reference[name][start:start + window_n]
            )
