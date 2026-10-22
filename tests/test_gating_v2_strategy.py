"""V2Gating end to end on synthetic streams (FS = 1000 Hz, so indices read as milliseconds)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from core.texture_piezo.application.gating_strategy import DerivedChunk, GatingParams
from core.texture_piezo.application.stream_processor import TouchIdStreamProcessor
from core.texture_piezo.application.v2_gating import V2Gating
from core.texture_piezo.gating.v1.quality_gate import IdleBaseline
from core.texture_piezo.gating.v2.config import GatingV2Config
from gating_v2_support import make_v2_config

FS = 1000.0
PZT_COLUMNS = tuple(f"PZT3_{c}" for c in "BLCRT")
ENGINE_CHANNEL = "PZT3_B"
BURST_MIN = 10.0
ACTIVE_LEVEL = 100.0
STREAM_SAMPLES = 2500
# strong |normal_jerk| over these sample ranges (block aligned: 40-sample blocks)
BURSTS = ((1000, 1320), (1600, 1920))


def _config(**overrides) -> GatingV2Config:
    return make_v2_config(**{"burst_min": BURST_MIN, **overrides})


def _baseline() -> IdleBaseline:
    return IdleBaseline(
        pzt_columns=list(PZT_COLUMNS), mean=[0.0] * 5, std=[1.0] * 5, fs=FS, k=8.0, captured_duration_s=5.0,
    )


def _gating(config: GatingV2Config, baseline: IdleBaseline | None = None, onset_skip_s: float = 0.0) -> V2Gating:
    return V2Gating(GatingParams(
        pzt_columns=PZT_COLUMNS, required_channels=(ENGINE_CHANNEL,), window_size_s=0.5, hop_size_s=0.1,
        idle_baseline=baseline, onset_skip_s=onset_skip_s, v2_config=config,
    ))


def _signals(bursts=BURSTS, active=None, n_samples=STREAM_SAMPLES):
    """(raw level per sample, |normal_jerk| per sample, timestamps). The raw level is the sample
    index, so a window's content shows exactly which samples it was cut from. With ``active``
    ranges it is instead idle (0) except far outside the idle band (ACTIVE_LEVEL) there."""
    raw = np.arange(n_samples, dtype=float) if active is None else np.zeros(n_samples)
    for start, end in active or ():
        raw[start:end] = ACTIVE_LEVEL
    jerk = np.zeros(n_samples)
    for start, end in bursts:
        jerk[start:end] = 1.0
    return raw, jerk, np.arange(n_samples) / FS


def _chunk(raw, jerk, timestamps, start: int, stop: int) -> DerivedChunk:
    level = raw[start:stop]
    return DerivedChunk(
        channel_samples={col: level for col in PZT_COLUMNS},
        derived={"integrated": {col: np.zeros(stop - start) for col in PZT_COLUMNS}, "normal_jerk": jerk[start:stop]},
        timestamps=timestamps[start:stop], fs=FS, now_t=0.0,
    )


def _run(gating: V2Gating, signals, tick: int):
    raw, jerk, timestamps = signals
    windows = []
    for start in range(0, len(raw), tick):
        windows.extend(gating.push(_chunk(raw, jerk, timestamps, start, min(start + tick, len(raw))))) 
    return windows


def test_a_window_spans_two_ranges_and_is_their_exact_concatenation():
    windows = _run(_gating(_config()), _signals(), tick=100)
    assert [w.ranges for w in windows] == [
        ((1000, 1320), (1600, 1780)),
        ((1100, 1320), (1600, 1880)),
    ]
    first = windows[0]
    expected = np.concatenate([np.arange(1000, 1320), np.arange(1600, 1780)])
    assert np.array_equal(first.window.channels[ENGINE_CHANNEL], expected)
    assert np.array_equal(first.window_ts, expected / FS)
    assert first.window.seam_idx == (320,)
    assert windows[1].window.seam_idx == (220,)
    assert first.frag_id == windows[1].frag_id == 0


def test_windows_do_not_depend_on_the_tick_size():
    expected = [w.ranges for w in _run(_gating(_config()), _signals(), tick=100)]
    for tick in (1, 77, 500, 1538):
        assert [w.ranges for w in _run(_gating(_config()), _signals(), tick=tick)] == expected


def test_without_concat_neither_short_burst_makes_a_window():
    assert _run(_gating(_config(concat_enabled=False)), _signals(), tick=100) == []


def test_without_concat_a_long_enough_burst_yields_single_range_windows():
    signals = _signals(bursts=((1000, 1800),))
    windows = _run(_gating(_config(concat_enabled=False)), signals, tick=100)
    assert [w.ranges for w in windows] == [((1000, 1500),), ((1100, 1600),), ((1200, 1700),), ((1300, 1800),)]
    assert all(w.window.seam_idx == () for w in windows)


def test_good_signal_shorter_than_a_window_is_never_emitted():
    signals = _signals(bursts=((1000, 1400),), n_samples=6000)
    assert _run(_gating(_config()), signals, tick=100) == []


def test_both_gates_off_makes_the_whole_stream_one_range_after_warmup_and_onset():
    gating = _gating(_config(strength_gate_enabled=False), onset_skip_s=0.3)
    windows = _run(gating, _signals(), tick=100)
    assert [w.ranges[0][0] for w in windows[:3]] == [300, 400, 500]
    assert all(len(w.ranges) == 1 for w in windows)
    assert windows[-1].ranges == ((2000, 2500),)


def test_warmup_keeps_the_first_samples_out_even_without_onset_skip():
    windows = _run(_gating(_config(strength_gate_enabled=False)), _signals(), tick=100)
    assert windows[0].ranges == ((200, 700),)


def test_one_tick_can_emit_several_windows():
    gating = _gating(_config(strength_gate_enabled=False))
    windows = gating.push(_chunk(*_signals(), 0, STREAM_SAMPLES))
    assert [w.ranges[0][0] for w in windows] == list(range(200, 2001, 100))


def test_both_gates_intersect():
    signals = _signals(active=[(1100, 2100)])
    windows = _run(_gating(_config(idle_gate_enabled=True), baseline=_baseline()), signals, tick=100)
    assert windows[0].ranges == ((1100, 1320), (1600, 1880))


def test_the_idle_gate_alone_constrains_the_stream():
    signals = _signals(active=[(1000, 1700)])
    config = _config(strength_gate_enabled=False, idle_gate_enabled=True)
    windows = _run(_gating(config, baseline=_baseline()), signals, tick=100)
    assert [w.ranges for w in windows] == [((1000, 1500),), ((1100, 1600),), ((1200, 1700),)]


def test_idle_gate_without_a_baseline_waits_and_constrains_nothing():
    config = _config(strength_gate_enabled=False, idle_gate_enabled=True)
    gating = _gating(config, baseline=None)
    assert gating.idle_gate_waiting_for_baseline
    assert len(_run(gating, _signals(), tick=100)) > 0
    assert not _gating(config, baseline=_baseline()).idle_gate_waiting_for_baseline


def test_a_missing_normal_jerk_channel_fails_fast_when_the_strength_gate_is_on():
    raw, jerk, timestamps = _signals()
    chunk = _chunk(raw, jerk, timestamps, 0, 100)
    chunk.derived.pop("normal_jerk")
    with pytest.raises(ValueError, match="normal_jerk"):
        _gating(_config()).push(chunk)
    _gating(_config(strength_gate_enabled=False)).push(chunk)


def test_reset_forgets_the_stream():
    gating = _gating(_config(strength_gate_enabled=False))
    _run(gating, _signals(), tick=500)
    gating.reset()
    assert gating.store.next_abs == 0
    assert _run(gating, _signals(), tick=500)[0].ranges == ((200, 700),)


def test_the_store_is_trimmed_to_the_expiry_horizon():
    gating = _gating(_config(strength_gate_enabled=False, expiry_s=0.8))
    _run(gating, _signals(n_samples=5000), tick=100)
    assert 700 <= len(gating.store.timestamps) <= 900


def test_expiry_shorter_than_a_window_is_rejected():
    with pytest.raises(ValueError, match="expiry_s"):
        _gating(_config(expiry_s=0.4))


def test_set_onset_skip_applies_from_the_next_range():
    gating = _gating(_config())
    gating.set_onset_skip_s(0.1)
    windows = _run(gating, _signals(bursts=((1000, 1800),)), tick=100)
    assert windows[0].ranges == ((1100, 1600),)


def test_set_gate_k_widens_the_idle_band():
    config = _config(strength_gate_enabled=False, idle_gate_enabled=True)
    gating = _gating(config, baseline=_baseline())
    gating.set_gate_k(1000.0)
    assert _run(gating, _signals(active=[(1000, 1700)]), tick=100) == []


# --- through the stream processor -------------------------------------------------------------

PROCESSOR_FS = 1538.2


def _processor(config: GatingV2Config) -> TouchIdStreamProcessor:
    return TouchIdStreamProcessor(
        pzt_columns=list(PZT_COLUMNS), window_size_s=0.5, hop_size_s=0.1, span_stale_timeout_s=1.0,
        idle_baseline=None, onset_skip_s=0.0, gating_version="v2", gating_v2_config=config,
    )


def _feed_processor(processor, noise: np.ndarray, start_ts: float = 0.0, tick: int = 154):
    windows = []
    for start in range(0, len(noise), tick):
        chunk = noise[start:start + tick]
        samples = processor.filter_raw({col: chunk[:, i] for i, col in enumerate(PZT_COLUMNS)})
        timestamps = start_ts + (start + np.arange(len(chunk))) / PROCESSOR_FS
        windows.extend(processor.push_chunk(samples, timestamps, PROCESSOR_FS, now_t=timestamps[-1]))
    return windows


def _stream_with_burst(seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    idle = 2048.0 + rng.normal(0.0, 1.0, size=(int(1.5 * PROCESSOR_FS), 5))
    burst = 2048.0 + rng.normal(0.0, 60.0, size=(int(1.2 * PROCESSOR_FS), 5))
    return np.concatenate([idle, burst, idle])


def test_processor_runs_v2_end_to_end_and_survives_a_timeline_break():
    config = _config(burst_min=1.0, sustain_min=1)
    processor = _processor(config)
    stream = _stream_with_burst()
    windows = _feed_processor(processor, stream)
    assert windows
    assert all(w.window.n_samples == sum(end - start for start, end in w.ranges) for w in windows)
    after_break = _feed_processor(processor, stream, start_ts=0.0)  # timestamps restart -> gating resets
    assert after_break
    assert after_break[0].ranges[0][0] < windows[-1].ranges[-1][1]  # indices restarted from the new store
    processor.set_onset_skip_s(0.05)
    processor.set_gate_k(5.0)


def test_processor_requires_the_v2_config():
    with pytest.raises(ValueError, match="v2_config"):
        TouchIdStreamProcessor(
            pzt_columns=list(PZT_COLUMNS), window_size_s=0.5, hop_size_s=0.1, span_stale_timeout_s=1.0,
            idle_baseline=None, gating_version="v2",
        )
