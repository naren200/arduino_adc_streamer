"""TouchID's adapter over the engine: the bundle-config hook, the compute_force switch, full engine
state hand-over across a processor rebuild, and the deliberate absence of live timestamps in the force stage."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from core.piezo_engine.config import EngineConfig
from core.texture_piezo.application.stream_processor import (
    LIVE_ENGINE_CONFIG,
    DerivedChannelPipeline,
    TouchIdStreamProcessor,
    resolve_live_engine_config,
)

COLUMNS = [f"PZT3_{c}" for c in "BLCRT"]
FS = 1000.0
DERIVED_KEYS = ("shear_jerk_lr", "shear_jerk_tb", "normal_jerk")
OTHER_CONFIG = EngineConfig(timing=LIVE_ENGINE_CONFIG.timing, smoothing_window_samples=3)


def _chunk(n_samples: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    base = rng.integers(1500, 2500, size=(n_samples, len(COLUMNS)))
    return {col: base[:, i].astype(np.float64) for i, col in enumerate(COLUMNS)}


def _stamps(n_samples: int, start: int = 0) -> np.ndarray:
    return (np.arange(n_samples) + start) / FS


def _process(pipeline: DerivedChannelPipeline, chunk: dict, start: int = 0):
    return pipeline.process(pipeline.filter_raw(chunk), _stamps(len(chunk[COLUMNS[0]]), start), FS)


# ---------------------------------------------------------------- bundle-config hook

def test_the_placeholder_runs_without_a_bundle_and_an_injected_bundle_config_is_used_as_is():
    assert resolve_live_engine_config() is LIVE_ENGINE_CONFIG
    assert resolve_live_engine_config(bundle_config=OTHER_CONFIG) is OTHER_CONFIG
    assert LIVE_ENGINE_CONFIG.compute_force is False  # the default for runtimes that declare no engine_config and read no force


# ---------------------------------------------------------------- compute_force switch

def test_a_config_without_force_makes_the_adapter_skip_the_force_stage_only():
    on = DerivedChannelPipeline(COLUMNS, config=EngineConfig(timing=LIVE_ENGINE_CONFIG.timing, compute_force=True))
    off = DerivedChannelPipeline(COLUMNS, config=EngineConfig(timing=LIVE_ENGINE_CONFIG.timing, compute_force=False))
    assert on._engine._force is not None and off._engine._force is None
    chunk = _chunk(1500, 1)
    _raw_on, derived_on, _ = _process(on, chunk)
    _raw_off, derived_off, _ = _process(off, chunk)
    for key in DERIVED_KEYS:
        assert np.array_equal(derived_on[key], derived_off[key]), key
    for column in COLUMNS:
        assert np.array_equal(derived_on["integrated"][column], derived_off["integrated"][column])


def test_the_processor_accepts_an_injected_engine_config():
    config = EngineConfig(timing=LIVE_ENGINE_CONFIG.timing, compute_force=False)
    processor = TouchIdStreamProcessor(
        pzt_columns=COLUMNS, window_size_s=0.1, hop_size_s=0.05, span_stale_timeout_s=1.0, idle_baseline=None,
        engine_config=config)
    assert processor._derived_pipeline._engine.config is config
    assert processor._derived_pipeline._engine._force is None


# ---------------------------------------------------------------- live timestamps are not forwarded

def test_constant_live_timestamps_neither_break_the_force_stage_nor_change_the_derived_outputs():
    """Live clocks jitter and can go backwards; force integrates against time, so the adapter hands the
    engine no timestamps (its default grid is the global sample index / fs, as in training). Constant
    timestamps would make the force rate stage raise if they were forwarded; the equality assertions
    are on the jerk/integrated outputs, which never read timestamps (force is not exposed by the adapter)."""
    chunk = _chunk(1400, 2)
    clean = DerivedChannelPipeline(COLUMNS)
    wild = DerivedChannelPipeline(COLUMNS)
    _raw, clean_derived, clean_ts = _process(clean, chunk)
    _raw, wild_derived, wild_ts = wild.process(wild.filter_raw(chunk), np.zeros(1400), FS)  # constant timestamps
    for key in DERIVED_KEYS:
        assert np.array_equal(clean_derived[key], wild_derived[key]), key
    assert len(clean_ts) == len(wild_ts) == len(clean_derived["normal_jerk"])


# ---------------------------------------------------------------- state hand-over

def test_a_pipeline_rebuild_with_state_hand_over_continues_every_stage_bit_exactly():
    total = _chunk(2600, 3)
    split = 900
    whole = DerivedChannelPipeline(COLUMNS)
    _raw, expected, _ts = _process(whole, total)
    first = DerivedChannelPipeline(COLUMNS)
    _raw, head, _ts = _process(first, {c: v[:split] for c, v in total.items()})
    second = DerivedChannelPipeline(COLUMNS)
    second.adopt_state_from(first)
    _raw, tail, _ts = _process(second, {c: v[split:] for c, v in total.items()}, split)
    for key in DERIVED_KEYS:
        assert np.array_equal(np.concatenate([head[key], tail[key]]), expected[key]), key
    # the force stage continued as well (the adapter does not expose force; compare the engines' state)
    assert second._engine._force is first._engine._force


def test_the_processor_hand_over_takes_the_engine_state_but_not_the_windowing_state():
    kwargs = dict(pzt_columns=COLUMNS, window_size_s=0.1, hop_size_s=0.05, span_stale_timeout_s=1.0, idle_baseline=None)
    old = TouchIdStreamProcessor(**kwargs)
    n_samples = 800
    old.push_chunk(old.filter_raw(_chunk(n_samples, 4)), _stamps(n_samples), FS, now_t=0.0)
    resized = TouchIdStreamProcessor(**{**kwargs, "window_size_s": 0.2})
    resized.adopt_engine_state_from(old)
    assert resized._derived_pipeline._engine.derived_channels is old._derived_pipeline._engine.derived_channels
    assert resized._derived_pipeline._engine._force is old._derived_pipeline._engine._force
    assert resized._buffer is not old._buffer
    follow_up = resized.push_chunk(resized.filter_raw(_chunk(100, 5)), _stamps(100, n_samples), FS, now_t=0.8)
    assert follow_up == [] or all(window.window_adc.shape[0] > 0 for window in follow_up)


def test_the_processor_exposes_its_engine_config_so_a_rebuild_can_continue_on_the_same_engine():
    config = EngineConfig(timing=LIVE_ENGINE_CONFIG.timing, smoothing_window_samples=3)
    kwargs = dict(pzt_columns=COLUMNS, window_size_s=0.1, hop_size_s=0.05, span_stale_timeout_s=1.0, idle_baseline=None)
    old = TouchIdStreamProcessor(**kwargs, engine_config=config)
    rebuilt = TouchIdStreamProcessor(**kwargs, engine_config=old.engine_config)
    rebuilt.adopt_engine_state_from(old)  # would raise with the default config
    assert rebuilt.engine_config is config
    with pytest.raises(ValueError, match="different columns or engine config"):
        TouchIdStreamProcessor(**kwargs).adopt_engine_state_from(old)


def test_a_hand_over_between_different_engine_configs_is_refused():
    first = DerivedChannelPipeline(COLUMNS)
    other = DerivedChannelPipeline(COLUMNS, config=OTHER_CONFIG)
    with pytest.raises(ValueError, match="different columns or engine config"):
        other.adopt_state_from(first)
