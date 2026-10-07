"""PiezoEnginePipeline: chunk invariance (synthetic and real capture), stage order, state hand-over, config."""

import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core.piezo_engine.config import EngineConfig, TimingMode, TimingPolicy
from core.piezo_engine.median import CausalMedianN
from core.piezo_engine.pipeline import (
    ALREADY_CONDITIONED_INPUT,
    INPUT_CONDITIONING_METADATA_KEY,
    RAW_INPUT,
    InputConditioning,
    PiezoEnginePipeline,
    conditioning_record,
    input_conditioning_from_record,
)
from core.piezo_engine.streaming import CausalDerivedChannels
from core.piezo_engine.timing import resolve_capture_timing

RAW_DIR = Path(os.environ.get(
    "TP_RAW_DIR", r"C:\Users\sense\Documents\Github\texture_piezo\data\raw\sensor_v12d_7_26\ch5",
))
CAPTURES = (
    "only_cardboard_and_idle_v1_20260914_1635",
    "only_cardboard_and_idle_v2_20260915_1800",
    "only_cardboard_and_idle_v3_20260916_1022",
)
SYNTHETIC_FS = 1000.0
SYNTHETIC_COLUMNS = [f"PZT3_{c}" for c in "BLCRT"]
CONFIG = EngineConfig(timing=TimingPolicy(TimingMode.CONTINUOUS))


def _synthetic_stream(n_samples: int, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    base = rng.integers(1500, 2500, size=(n_samples, len(SYNTHETIC_COLUMNS)))
    return {col: base[:, i].astype(np.float64) for i, col in enumerate(SYNTHETIC_COLUMNS)}


def _run_chunks(pipeline: PiezoEnginePipeline, stream: dict, sizes: list[int]) -> list:
    results, start = [], 0
    for size in sizes:
        chunk = {col: values[start:start + size] for col, values in stream.items()}
        results.append(pipeline.process(chunk, sample_rate_hz=SYNTHETIC_FS))
        start += size
    return results


def _concat(results: list) -> dict:
    out = {"raw": {}, "integrated": {}}
    for col in results[0].raw:
        out["raw"][col] = np.concatenate([r.raw[col] for r in results])
        out["integrated"][col] = np.concatenate([r.integrated[col] for r in results])
    for key in ("shear_jerk_lr", "shear_jerk_tb", "normal_jerk"):
        out[key] = np.concatenate([getattr(r, key) for r in results])
    return out


def _assert_equal(a: dict, b: dict) -> None:
    for col in a["raw"]:
        assert np.array_equal(a["raw"][col], b["raw"][col])
        assert np.array_equal(a["integrated"][col], b["integrated"][col])
    for key in ("shear_jerk_lr", "shear_jerk_tb", "normal_jerk"):
        assert np.array_equal(a[key], b[key])


def _load_capture(capture: str):
    csv_path = RAW_DIR / f"{capture}.csv"
    if not csv_path.exists():
        pytest.skip("training capture not available")
    metadata = json.loads((RAW_DIR / f"{capture}_metadata.json").read_text(encoding="utf-8"))
    frame = pd.read_csv(csv_path)
    columns = list(SYNTHETIC_COLUMNS)
    stream = {col: frame[col.replace("PZT3", "PZT5")].to_numpy(np.float64) for col in columns}
    return metadata, columns, stream


def test_many_chunks_are_bit_identical_to_one_call_on_a_real_capture():
    metadata, columns, stream = _load_capture(CAPTURES[0])
    resolved = resolve_capture_timing(metadata, {})
    config = EngineConfig(timing=resolved.policy)
    whole = PiezoEnginePipeline(columns, config).process(stream, sample_rate_hz=resolved.sample_rate_hz)
    chunked = PiezoEnginePipeline(columns, config)
    rng = np.random.default_rng(5)
    results, start, n_total = [], 0, len(stream[columns[0]])
    while start < n_total:
        size = int(rng.choice([1, 7, 150, 611, 700, 2500]))
        chunk = {col: values[start:start + size] for col, values in stream.items()}
        results.append(chunked.process(chunk, sample_rate_hz=resolved.sample_rate_hz))
        start += size
    merged = _concat(results)
    assert np.array_equal(whole.shear_jerk_lr, merged["shear_jerk_lr"])
    assert np.array_equal(whole.normal_jerk, merged["normal_jerk"])
    for col in columns:
        assert np.array_equal(whole.integrated[col], merged["integrated"][col])


def test_chunked_equals_one_call_across_the_warmup_boundary():
    n_samples = CONFIG.leading_warmup_samples + 300
    stream = _synthetic_stream(n_samples)
    whole = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG).process(stream, sample_rate_hz=SYNTHETIC_FS)
    sizes = [1, 1, 20, 7, 140, 10, 11, 1, 5]
    sizes.append(n_samples - sum(sizes))
    chunked = _run_chunks(PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG), stream, sizes)
    _assert_equal(_concat([whole]), _concat(chunked))
    assert sum(r.dropped_leading.total for r in chunked) == whole.dropped_leading.total


def test_stream_is_not_settle_trimmed_and_only_the_warmup_is_dropped():
    stream = _synthetic_stream(1000)
    result = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG).process(stream, sample_rate_hz=SYNTHETIC_FS)
    warmup = CONFIG.leading_warmup_samples
    assert result.dropped_leading.warmup == warmup and result.dropped_leading.total == warmup
    assert result.n_samples == 1000 - warmup


def test_derived_channels_see_the_whole_median_filtered_stream_from_sample_zero():
    stream = _synthetic_stream(900)
    filtered = CausalMedianN(CONFIG.blip_window_samples, len(SYNTHETIC_COLUMNS)).process(
        np.stack([stream[c] for c in SYNTHETIC_COLUMNS], axis=1))
    whole = {col: filtered[:, i] for i, col in enumerate(SYNTHETIC_COLUMNS)}
    reference = CausalDerivedChannels(SYNTHETIC_COLUMNS).process(whole, sample_rate_hz=SYNTHETIC_FS)
    result = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG).process(stream, sample_rate_hz=SYNTHETIC_FS)
    warmup = CONFIG.leading_warmup_samples
    assert np.array_equal(result.shear_jerk_lr, reference["shear_jerk_lr"][warmup:])
    assert np.array_equal(result.integrated[SYNTHETIC_COLUMNS[2]], reference["integrated"][SYNTHETIC_COLUMNS[2]][warmup:])
    assert np.array_equal(result.raw[SYNTHETIC_COLUMNS[0]], whole[SYNTHETIC_COLUMNS[0]][warmup:])


def test_already_conditioned_input_does_not_run_the_median_again():
    stream = _synthetic_stream(200)
    pipeline = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG, input_conditioning=ALREADY_CONDITIONED_INPUT)
    result = pipeline.process(stream, sample_rate_hz=SYNTHETIC_FS)
    warmup = CONFIG.leading_warmup_samples
    assert result.dropped_leading.warmup == warmup
    assert np.array_equal(result.raw[SYNTHETIC_COLUMNS[1]], stream[SYNTHETIC_COLUMNS[1]][warmup:])


def test_filter_raw_then_process_filtered_equals_process():
    stream = _synthetic_stream(800)
    split = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG)
    two_step = split.process_filtered(split.filter_raw(stream), sample_rate_hz=SYNTHETIC_FS)
    one_step = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG).process(stream, sample_rate_hz=SYNTHETIC_FS)
    _assert_equal(_concat([two_step]), _concat([one_step]))


ALL_SERIES = ("shear_jerk_lr", "shear_jerk_tb", "normal_jerk", "normal_force", "shear_force_lr", "shear_force_tb")


def _series(results: list) -> dict:
    out = {name: np.concatenate([getattr(r, name) for r in results]) for name in ALL_SERIES}
    out.update({f"raw_{c}": np.concatenate([r.raw[c] for r in results]) for c in SYNTHETIC_COLUMNS})
    out.update({f"integrated_{c}": np.concatenate([r.integrated[c] for r in results]) for c in SYNTHETIC_COLUMNS})
    return out


def test_adopting_a_warm_pipeline_continues_the_stream_bit_exactly_in_every_output():
    """The hand-over carries EVERY stateful stage (median window, derived sums/medians,
    force stage, force timeline): chunk 1 on one pipeline + chunk 2 on its successor == one pipeline."""
    n_samples, split = 2600, 900
    stream = _synthetic_stream(n_samples)
    whole = _series(_run_chunks(PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG), stream, [n_samples]))
    first = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG)
    head = _run_chunks(first, {c: v[:split] for c, v in stream.items()}, [split])
    successor = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG)
    successor.adopt_state_from(first)
    tail = _run_chunks(successor, {c: v[split:] for c, v in stream.items()}, [n_samples - split])
    assert tail[0].dropped_leading.total == 0
    handed_over = _series(head + tail)
    for name, expected in whole.items():
        assert np.array_equal(handed_over[name], expected), name


def test_a_cold_successor_would_differ_which_is_what_the_hand_over_prevents():
    n_samples, split = 2600, 900
    stream = _synthetic_stream(n_samples)
    whole = _series(_run_chunks(PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG), stream, [n_samples]))
    head = _run_chunks(PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG), {c: v[:split] for c, v in stream.items()}, [split])
    cold = _run_chunks(PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG), {c: v[split:] for c, v in stream.items()}, [n_samples - split])
    assert not np.array_equal(_series(head + cold)["normal_force"], whole["normal_force"])


def test_adopting_state_requires_the_same_columns_and_engine_config():
    first = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG)
    other_config = EngineConfig(timing=CONFIG.timing, smoothing_window_samples=3)
    with pytest.raises(ValueError, match="different columns or engine config"):
        PiezoEnginePipeline(SYNTHETIC_COLUMNS, other_config).adopt_state_from(first)
    with pytest.raises(ValueError, match="different columns or engine config"):
        PiezoEnginePipeline(list(reversed(SYNTHETIC_COLUMNS)), CONFIG).adopt_state_from(first)


def test_adopting_state_also_takes_over_the_input_conditioning():
    first = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG, input_conditioning=ALREADY_CONDITIONED_INPUT)
    successor = PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG)
    successor.adopt_state_from(first)
    assert successor.input_conditioning == ALREADY_CONDITIONED_INPUT


# ---------------------------------------------------------------- compute_force switch

def test_compute_force_false_skips_the_force_stage_and_returns_none_forces():
    config = EngineConfig(timing=CONFIG.timing, compute_force=False)
    pipeline = PiezoEnginePipeline(SYNTHETIC_COLUMNS, config)
    assert pipeline._force is None
    result = pipeline.process(_synthetic_stream(900), sample_rate_hz=SYNTHETIC_FS)
    assert result.normal_force is None and result.shear_force_lr is None and result.shear_force_tb is None
    assert result.n_samples == 900 - config.leading_warmup_samples


def test_compute_force_false_leaves_every_other_output_bit_identical():
    stream = _synthetic_stream(1500)
    with_force = _series(_run_chunks(PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG), stream, [1500]))
    without = PiezoEnginePipeline(SYNTHETIC_COLUMNS, EngineConfig(timing=CONFIG.timing, compute_force=False))
    results = _run_chunks(without, stream, [700, 800])
    for name in ("shear_jerk_lr", "shear_jerk_tb", "normal_jerk"):
        assert np.array_equal(np.concatenate([getattr(r, name) for r in results]), with_force[name]), name
    for column in SYNTHETIC_COLUMNS:
        assert np.array_equal(np.concatenate([r.integrated[column] for r in results]), with_force[f"integrated_{column}"])


def test_compute_force_false_does_not_need_matching_decay_labels():
    policy = TimingPolicy(TimingMode.MANUAL, 2e-5, {"NOT_A_COLUMN": 1e-6})
    with pytest.raises(ValueError, match="match none of the engine columns"):
        PiezoEnginePipeline(SYNTHETIC_COLUMNS, EngineConfig(timing=policy))
    PiezoEnginePipeline(SYNTHETIC_COLUMNS, EngineConfig(timing=policy, compute_force=False))


def test_compute_force_is_part_of_equality_and_the_canonical_dict():
    on, off = EngineConfig(timing=CONFIG.timing), EngineConfig(timing=CONFIG.timing, compute_force=False)
    assert on.to_dict()["compute_force"] is True and off.to_dict()["compute_force"] is False
    assert on != off
    assert on.to_dict()["schema_version"] == 4


def test_engine_config_round_trips_through_its_canonical_dict_and_json():
    policy = TimingPolicy(TimingMode.MANUAL, 2.5e-5, {"PZT3_C": 4e-6})
    config = EngineConfig(timing=policy, integration_window_samples=40, compute_force=False)
    assert EngineConfig.from_dict(config.to_dict()) == config
    assert EngineConfig.from_dict(json.loads(json.dumps(config.to_dict()))) == config


def test_engine_config_from_dict_refuses_a_foreign_schema():
    record = EngineConfig(timing=CONFIG.timing).to_dict()
    with pytest.raises(ValueError, match="schema"):
        EngineConfig.from_dict({**record, "schema_version": record["schema_version"] - 1})


def test_conditioning_record_round_trips_and_missing_record_means_raw():
    assert input_conditioning_from_record(None) == RAW_INPUT
    assert input_conditioning_from_record({}) == RAW_INPUT
    assert input_conditioning_from_record(conditioning_record(None)) == RAW_INPUT
    assert input_conditioning_from_record(conditioning_record(5)) == InputConditioning(5)


def test_conditioning_record_reader_ignores_the_legacy_settle_trimmed_key():
    legacy = {"median_window_samples": 3, "settle_trimmed_samples": 611}
    assert input_conditioning_from_record(legacy) == InputConditioning(3)


def test_upstream_median_window_mismatch_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="core.piezo_engine.pipeline"):
        PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG, input_conditioning=InputConditioning(5))
    assert "median-5" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="core.piezo_engine.pipeline"):
        PiezoEnginePipeline(SYNTHETIC_COLUMNS, CONFIG, input_conditioning=ALREADY_CONDITIONED_INPUT)
    assert not caplog.text
