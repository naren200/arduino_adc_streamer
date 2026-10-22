"""Gating traces: what a strategy reports per push, recorded without altering any decision.
FS = 1000 Hz, so sample indices read as milliseconds."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from core.texture_piezo.application.gating_strategy import DerivedChunk, GatingParams
from core.texture_piezo.application.gating_trace import Describable, GateTrace, TraceObservable, TraceRecorder
from core.texture_piezo.application.v1_gating import V1Gating, V1GatingOptions
from core.texture_piezo.application.v2_gating import V2Gating
from core.texture_piezo.gating.v1.quality_gate import IdleBaseline
from gating_v2_support import make_v2_config

FS = 1000.0
COLUMNS = tuple(f"PZT3_{c}" for c in "BLCRT")
CHANNEL = "PZT3_B"
WINDOW_S = 0.5
HOP_S = 0.1
STALE_TIMEOUT_S = 5.0
TICK = 100
STREAM_SAMPLES = 2500
BURSTS = ((1000, 1320), (1600, 1920))
ACTIVE_LEVEL = 100.0


def _baseline() -> IdleBaseline:
    return IdleBaseline(pzt_columns=list(COLUMNS), mean=[0.0] * 5, std=[1.0] * 5, fs=FS, k=8.0, captured_duration_s=5.0)


def _params(**overrides) -> GatingParams:
    values = dict(
        pzt_columns=COLUMNS, required_channels=(CHANNEL,), window_size_s=WINDOW_S, hop_size_s=HOP_S,
        idle_baseline=None, onset_skip_s=0.0, v1_options=None, v2_config=None,
    )
    return GatingParams(**{**values, **overrides})


def _v2(**config_overrides) -> V2Gating:
    return V2Gating(_params(v2_config=make_v2_config(burst_min=10.0, **config_overrides)))


def _v1(baseline: IdleBaseline | None) -> V1Gating:
    return V1Gating(_params(idle_baseline=baseline, v1_options=V1GatingOptions(span_stale_timeout_s=STALE_TIMEOUT_S)))


def _chunk(level: np.ndarray, jerk: np.ndarray, start: int, stop: int) -> DerivedChunk:
    return DerivedChunk(
        channel_samples={col: level[start:stop] for col in COLUMNS},
        derived={"integrated": {col: np.zeros(stop - start) for col in COLUMNS}, "normal_jerk": jerk[start:stop]},
        timestamps=np.arange(start, stop) / FS, fs=FS, now_t=stop / FS,
    )


def _stream(active: tuple[tuple[int, int], ...] = ()):
    level = np.arange(STREAM_SAMPLES, dtype=float)
    jerk = np.zeros(STREAM_SAMPLES)
    for start, end in BURSTS:
        jerk[start:end] = 1.0
    if active:
        level = np.zeros(STREAM_SAMPLES)
        for start, end in active:
            level[start:end] = ACTIVE_LEVEL
    return level, jerk


def _run(gating, level, jerk):
    windows = []
    for start in range(0, STREAM_SAMPLES, TICK):
        windows.extend(gating.push(_chunk(level, jerk, start, min(start + TICK, STREAM_SAMPLES))))
    return windows


def _traced_run(gating, level, jerk) -> tuple[list, GateTrace]:
    recorder = TraceRecorder()
    gating.set_trace_observer(recorder.observe)
    return _run(gating, level, jerk), recorder.trace()


def test_v2_trace_matches_the_hand_computed_case():
    windows, trace = _traced_run(_v2(), *_stream())
    lanes = {lane.name: lane.ranges for lane in trace.lanes}
    assert [lane.name for lane in trace.lanes] == ["strength", "combined"]
    assert lanes["strength"] == lanes["combined"]
    (first_start, _), (_, last_end) = lanes["combined"][0], lanes["combined"][-1]
    assert (first_start, last_end) == (1000, 1920)
    assert [(w.window_id, w.frag_id, w.ranges, w.seam_idx) for w in trace.windows] == [
        (0, 0, ((1000, 1320), (1600, 1780)), (320,)),
        (1, 0, ((1100, 1320), (1600, 1880)), (220,)),
    ]
    assert [w.ranges for w in windows] == [w.ranges for w in trace.windows]


def test_v2_reports_idle_lane_only_when_the_idle_stage_is_active():
    _, trace = _traced_run(_v2(idle_gate_enabled=True), *_stream())
    assert [lane.name for lane in trace.lanes] == ["strength", "combined"]
    gating = V2Gating(_params(
        idle_baseline=_baseline(), v2_config=make_v2_config(burst_min=10.0, idle_gate_enabled=True),
    ))
    _, trace = _traced_run(gating, *_stream(active=[(1000, 2000)]))
    assert [lane.name for lane in trace.lanes] == ["strength", "idle", "combined"]


def test_combined_lane_applies_onset_skip_and_warmup():
    gating = V2Gating(_params(onset_skip_s=0.3, v2_config=make_v2_config(strength_gate_enabled=False)))
    _, trace = _traced_run(gating, *_stream())
    assert [lane.name for lane in trace.lanes] == ["combined"]
    assert trace.lanes[0].ranges[0][0] == 300


def test_v1_active_queue_branch_records_windows_with_fragment_ids():
    windows, trace = _traced_run(_v1(_baseline()), *_stream(active=[(300, 1800)]))
    assert windows
    assert trace.lanes == ()
    assert [w.ranges for w in trace.windows] == [w.ranges for w in windows]
    assert all(w.frag_id is not None and len(w.ranges) == 1 for w in trace.windows)
    assert [w.window_id for w in trace.windows] == list(range(len(windows)))


def test_v1_fixed_grid_branch_records_windows_without_fragment_ids():
    windows, trace = _traced_run(_v1(None), *_stream())
    assert windows
    assert [w.ranges for w in trace.windows] == [w.ranges for w in windows]
    assert all(w.frag_id is None and w.seam_idx == () for w in trace.windows)


def test_tracing_does_not_change_any_window():
    for make, stream in ((_v2, _stream()), (lambda: _v1(_baseline()), _stream(active=[(300, 1800)])),
                         (lambda: _v1(None), _stream())):
        plain = _run(make(), *stream)
        traced, _ = _traced_run(make(), *stream)
        assert [(w.frag_id, w.ranges) for w in plain] == [(w.frag_id, w.ranges) for w in traced]
        assert all(np.array_equal(a.window_ts, b.window_ts) for a, b in zip(plain, traced))


def test_a_detached_observer_receives_nothing_more():
    gating = _v2()
    recorder = TraceRecorder()
    gating.set_trace_observer(recorder.observe)
    level, jerk = _stream()
    gating.push(_chunk(level, jerk, 0, 1500))
    seen = recorder.trace()
    gating.set_trace_observer(None)
    gating.push(_chunk(level, jerk, 1500, STREAM_SAMPLES))
    assert recorder.trace() == seen


def test_both_strategies_expose_the_optional_capabilities():
    for gating in (_v2(), _v1(None)):
        assert isinstance(gating, TraceObservable)
        assert isinstance(gating, Describable)


def test_v2_describe_states_the_configured_numbers():
    text = V2Gating(_params(onset_skip_s=0.25, v2_config=make_v2_config(burst_min=7.5, expiry_s=4.0))).describe()
    for expected in ("window 0.5 s every 0.1 s", "drop first 0.25 s", "expiry 4 s", "concat on", "> 7.5", "0.04 s"):
        assert expected in text


def test_v2_describe_says_when_the_idle_gate_waits_for_a_baseline():
    waiting = _v2(idle_gate_enabled=True)
    assert "no idle baseline yet" in waiting.describe()
    assert "Final gate: strength ->" in waiting.describe()
    assert "no idle baseline yet" not in _v2().describe()


def test_v1_describe_distinguishes_the_baseline_branches():
    without = _v1(None).describe()
    assert "fixed grid" in without and "window 0.5 s every 0.1 s" in without
    with_baseline = _v1(_baseline()).describe()
    assert "idle is stripped" in with_baseline and "5 s" in with_baseline
