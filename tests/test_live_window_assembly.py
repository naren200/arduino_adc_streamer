"""Live window assembly by NAME: every ready window is a LiveWindow carrying exactly the engine
channels the model asked for (raw, integrated, jerk, force), aligned with one whole-stream
engine call, trimmed/padded like every other derived channel."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from core.piezo_engine.channel_names import (
    FORCE_COLUMNS, INTEGRATED_COLUMN_SUFFIX, INTEGRATED_COLUMNS, PZT_COLUMNS as ENGINE_PZT, SHEAR_NORMAL_COLUMNS,
)
from core.piezo_engine.config import EngineConfig, EngineConfigMismatchError, TimingMode, TimingPolicy
from core.piezo_engine.pipeline import PiezoEnginePipeline
from core.texture_piezo.application.live_channels import (
    DEFAULT_CHANNELS, engine_names_by_sensor_column, named_engine_channels,
)
from core.texture_piezo.application.live_model import (
    engine_config_for_model, ingest_filter_warning, required_channels_for_model,
)
from core.texture_piezo.application.stream_processor import (
    LIVE_ENGINE_CONFIG, ContinuousSampleStore, TouchIdStreamProcessor,
)
from core.texture_piezo.gating.quality_gate import IdleBaseline

SENSOR_COLUMNS = [f"PZT5_{c}" for c in "BLCRT"]
FS = 1000.0
CHUNK_N = 50
FORCE_CONFIG = EngineConfig(timing=TimingPolicy(mode=TimingMode.CONTINUOUS), compute_force=True)
NO_FORCE_CONFIG = EngineConfig(timing=TimingPolicy(mode=TimingMode.CONTINUOUS), compute_force=False)
WINDOW_S, HOP_S = 0.1, 0.05
WINDOW_N = 100
FORCE_CHANNELS = DEFAULT_CHANNELS + FORCE_COLUMNS


def baseline():
    return IdleBaseline(pzt_columns=list(SENSOR_COLUMNS), mean=[0.0] * 5, std=[0.1] * 5, fs=FS, k=8.0,
                        captured_duration_s=5.0)


def noisy_stream(n_samples, seed=0, burst=None):
    rng = np.random.default_rng(seed)
    stream = {col: rng.normal(0.0, 30.0, n_samples) for col in SENSOR_COLUMNS}
    if burst is not None:
        for samples in stream.values():
            samples[: burst[0]] = 0.0
            samples[burst[1]:] = 0.0
    return stream


def make_processor(config, with_baseline, required_channels=DEFAULT_CHANNELS):
    return TouchIdStreamProcessor(
        pzt_columns=SENSOR_COLUMNS, window_size_s=WINDOW_S, hop_size_s=HOP_S, span_stale_timeout_s=1.0,
        idle_baseline=baseline() if with_baseline else None, engine_config=config,
        required_channels=required_channels,
    )


def run_processor(processor, stream):
    n = len(stream[SENSOR_COLUMNS[0]])
    ready = []
    for start in range(0, n, CHUNK_N):
        chunk = {col: samples[start:start + CHUNK_N] for col, samples in stream.items()}
        timestamps = (start + np.arange(len(chunk[SENSOR_COLUMNS[0]]))) / FS
        ready += processor.push_chunk(processor.filter_raw(chunk), timestamps, FS, now_t=timestamps[0])
    return ready


def reference_channels(config, stream):
    """{engine name: array} and trimmed timestamps from ONE whole-stream engine call."""
    result = PiezoEnginePipeline(SENSOR_COLUMNS, config).process(stream, sample_rate_hz=FS)
    derived = {"integrated": dict(result.integrated), "shear_jerk_lr": result.shear_jerk_lr,
               "shear_jerk_tb": result.shear_jerk_tb, "normal_jerk": result.normal_jerk}
    if config.compute_force:
        derived.update(shear_force_lr=result.shear_force_lr, shear_force_tb=result.shear_force_tb,
                       normal_force=result.normal_force)
    names = FORCE_CHANNELS if config.compute_force else DEFAULT_CHANNELS
    channels = named_engine_channels(SENSOR_COLUMNS, result.raw, derived, names)
    n = len(stream[SENSOR_COLUMNS[0]])
    return channels, (np.arange(n) / FS)[result.dropped_leading.total:]


def assert_windows_equal_engine_slices(ready, channels, trimmed_ts, names):
    assert ready
    for ready_window in ready:
        window = ready_window.window
        assert set(window.channels) == set(names)
        assert window.n_samples == WINDOW_N == len(ready_window.window_ts)
        assert window.sample_rate_hz == FS
        start = int(np.searchsorted(trimmed_ts, ready_window.window_ts[0]))
        assert np.isclose(trimmed_ts[start], ready_window.window_ts[0])
        for name in names:
            np.testing.assert_array_equal(window.channels[name], channels[name][start:start + WINDOW_N])


def test_fixed_grid_windows_equal_the_engine_output_by_name():
    stream = noisy_stream(2500, seed=1)
    ready = run_processor(make_processor(FORCE_CONFIG, False, FORCE_CHANNELS), stream)
    channels, trimmed_ts = reference_channels(FORCE_CONFIG, stream)
    assert np.abs(channels["normal_force"]).max() > 0.0
    assert_windows_equal_engine_slices(ready, channels, trimmed_ts, FORCE_CHANNELS)


def test_active_queue_windows_equal_the_engine_output_by_name():
    stream = noisy_stream(3000, seed=2, burst=(1200, 2400))
    ready = run_processor(make_processor(FORCE_CONFIG, True, FORCE_CHANNELS), stream)
    channels, trimmed_ts = reference_channels(FORCE_CONFIG, stream)
    assert all(window.frag_id is not None for window in ready)
    assert_windows_equal_engine_slices(ready, channels, trimmed_ts, FORCE_CHANNELS)


def test_short_padded_span_borrows_every_channel_from_its_preceding_history():
    stream = noisy_stream(2600, seed=3, burst=(1500, 1530))
    ready = run_processor(make_processor(FORCE_CONFIG, True, FORCE_CHANNELS), stream)
    channels, trimmed_ts = reference_channels(FORCE_CONFIG, stream)
    assert_windows_equal_engine_slices(ready, channels, trimmed_ts, FORCE_CHANNELS)
    padded = [w for w in ready if w.window_ts[-1] >= 1.5 and w.window_ts[0] < 1.5]
    assert padded, "the short burst produced no padded window"
    assert 0 < np.count_nonzero(padded[0].window.channels[ENGINE_PZT[0]]) <= 30


def test_only_the_requested_channels_are_carried():
    stream = noisy_stream(2500, seed=4, burst=(1000, 2000))
    names = (ENGINE_PZT[0], "shear_jerk_lr")
    for with_baseline in (False, True):
        ready = run_processor(make_processor(NO_FORCE_CONFIG, with_baseline, names), stream)
        assert ready and all(set(w.window.channels) == set(names) for w in ready)


def test_windows_hold_float64_arrays_even_for_integer_adc_input():
    stream = {col: (samples * 10).astype(np.int64) for col, samples in noisy_stream(1500, seed=7).items()}
    window = run_processor(make_processor(NO_FORCE_CONFIG, False), stream)[-1].window
    assert all(array.dtype == np.float64 for array in window.channels.values())


def test_a_force_model_with_a_force_less_engine_is_refused():
    with pytest.raises(EngineConfigMismatchError, match="compute_force=False"):
        make_processor(NO_FORCE_CONFIG, False, FORCE_CHANNELS)


def test_store_slices_and_trims_every_channel_in_lockstep():
    names = (ENGINE_PZT[0], FORCE_COLUMNS[0], FORCE_COLUMNS[2])
    store = ContinuousSampleStore(SENSOR_COLUMNS, names)
    n = 10
    derived = {
        "integrated": {col: np.zeros(n) for col in SENSOR_COLUMNS},
        "shear_jerk_lr": np.zeros(n), "shear_jerk_tb": np.zeros(n), "normal_jerk": np.zeros(n),
        "shear_force_lr": np.arange(n, dtype=float), "shear_force_tb": 10.0 + np.arange(n),
        "normal_force": 100.0 + np.arange(n),
    }
    store.append({col: np.zeros(n) for col in SENSOR_COLUMNS}, derived, np.arange(n) / FS)
    channels, window_ts = store.slice(2, 5)
    np.testing.assert_array_equal(channels[FORCE_COLUMNS[0]], [2.0, 3.0, 4.0])
    np.testing.assert_array_equal(channels[FORCE_COLUMNS[2]], [102.0, 103.0, 104.0])
    assert len(window_ts) == 3


def test_sensor_columns_map_onto_engine_names_by_suffix_not_position():
    shuffled = ["PZT5_T", "PZT5_B", "PZT5_L", "PZT5_C", "PZT5_R"]
    assert engine_names_by_sensor_column(shuffled)["PZT5_T"] == "PZT3_T"
    with pytest.raises(ValueError, match="does not end"):
        engine_names_by_sensor_column(["PZT5_X"])


def test_a_missing_engine_channel_is_named_in_the_error():
    derived = {"integrated": {c: np.zeros(3) for c in SENSOR_COLUMNS}, "shear_jerk_lr": np.zeros(3),
               "shear_jerk_tb": np.zeros(3), "normal_jerk": np.zeros(3)}
    with pytest.raises(KeyError, match="normal_force"):
        named_engine_channels(SENSOR_COLUMNS, {c: np.zeros(3) for c in SENSOR_COLUMNS}, derived, ["normal_force"])


class FakeRuntime:
    def __init__(self, engine_config, required_channels, expected_ingest_blip_filter=None):
        self.engine_config = engine_config
        self.required_channels = required_channels
        if expected_ingest_blip_filter is not None:
            self.expected_ingest_blip_filter = expected_ingest_blip_filter


def test_engine_config_follows_the_runtime():
    assert engine_config_for_model(None) is LIVE_ENGINE_CONFIG
    assert engine_config_for_model(FakeRuntime(None, DEFAULT_CHANNELS)) is LIVE_ENGINE_CONFIG
    assert not LIVE_ENGINE_CONFIG.compute_force
    own = FakeRuntime(FORCE_CONFIG.to_dict(), FORCE_CHANNELS)
    assert engine_config_for_model(own) == FORCE_CONFIG


def test_a_force_runtime_whose_config_computes_no_force_is_refused():
    with pytest.raises(EngineConfigMismatchError, match="compute_force=False"):
        engine_config_for_model(FakeRuntime(NO_FORCE_CONFIG.to_dict(), FORCE_CHANNELS))


def test_a_config_this_engine_cannot_rebuild_is_refused():
    with pytest.raises(EngineConfigMismatchError, match="cannot be rebuilt"):
        engine_config_for_model(FakeRuntime({"unknown_key": 1}, DEFAULT_CHANNELS))


def test_required_channels_default_to_the_non_force_engine_channels():
    assert required_channels_for_model(None) == DEFAULT_CHANNELS
    assert required_channels_for_model(FakeRuntime(None, INTEGRATED_COLUMNS)) == INTEGRATED_COLUMNS
    assert DEFAULT_CHANNELS == ENGINE_PZT + INTEGRATED_COLUMNS + SHEAR_NORMAL_COLUMNS
    assert INTEGRATED_COLUMNS[0] == ENGINE_PZT[0] + INTEGRATED_COLUMN_SUFFIX


def test_ingest_warning_needs_a_runtime_that_expects_no_filter_and_the_filter_on():
    no_filter = FakeRuntime(None, DEFAULT_CHANNELS, expected_ingest_blip_filter=False)
    filtered = FakeRuntime(None, DEFAULT_CHANNELS, expected_ingest_blip_filter=True)
    silent = FakeRuntime(None, DEFAULT_CHANNELS)
    assert "ingest blip filter" in ingest_filter_warning(no_filter, True)
    assert ingest_filter_warning(no_filter, False) is None
    assert ingest_filter_warning(filtered, True) is None
    assert ingest_filter_warning(silent, True) is None
    assert ingest_filter_warning(None, True) is None
