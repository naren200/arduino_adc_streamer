"""Gate replay for the Analysis tab: exactly what TouchID "Run on Analysis Source" would window.

The capture goes through the same ``ReplayDriver`` and the processor built by the same
``ReplaySetup`` the TouchID tab uses; nothing is gated or derived a second time. A trace
recorder on the gating strategy and an observer on the processor's derived chunks capture
what the Analysis plots draw. Qt-free; meant to run on the Analysis worker thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from core.texture_piezo.application.gating_strategy import DerivedChunk
from core.texture_piezo.application.gating_trace import (
    Describable, GateTrace, TraceObservable, TraceRecorder, merge_ranges,
)
from core.texture_piezo.application.replay_driver import ReplayDriver
from core.texture_piezo.application.replay_setup import ReplaySetup
from core.texture_piezo.application.stream_processor import TouchIdStreamProcessor
from data_processing.analysis_workbench import AnalysisSourceSnapshot
from data_processing.burst_score import BurstScoreSeries, compute_burst_score_series

NORMAL_JERK_CHANNEL = "normal_jerk"

Span = tuple[float, float]


@dataclass(frozen=True, eq=False)
class GateReplayResult:
    """What one replay decided. Index ranges in ``trace`` are absolute positions in
    ``timestamps_s`` / ``normal_jerk`` (both after the engine's warmup drop)."""

    setup: ReplaySetup
    trace: GateTrace
    timestamps_s: np.ndarray
    logic_text: str
    normal_jerk: np.ndarray
    burst: BurstScoreSeries | None  # None when the setup has no strength gate

    def spans_s(self, index_ranges: Sequence[tuple[int, int]]) -> tuple[Span, ...]:
        count = len(self.timestamps_s)
        return tuple(
            (float(self.timestamps_s[start]), float(self.timestamps_s[min(end, count) - 1]))
            for start, end in index_ranges
        )

    def lane_spans_s(self, lane_name: str) -> tuple[Span, ...]:
        for lane in self.trace.lanes:
            if lane.name == lane_name:
                return self.spans_s(lane.ranges)
        return ()

    def accepted_spans_s(self) -> tuple[Span, ...]:
        """The union of every emitted window's ranges: the signal that reached the classifier."""
        used = [index_range for window in self.trace.windows for index_range in window.ranges]
        return self.spans_s(merge_ranges(used))


class _ReplayCapture:
    """Collects the normal_jerk and timestamps the replay's own engine pass derives."""

    def __init__(self) -> None:
        self._normal_jerk: list[np.ndarray] = []
        self._timestamps: list[np.ndarray] = []

    def observe(self, chunk: DerivedChunk) -> None:
        self._normal_jerk.append(np.asarray(chunk.derived[NORMAL_JERK_CHANNEL], dtype=np.float64))
        self._timestamps.append(np.asarray(chunk.timestamps, dtype=np.float64))

    def normal_jerk(self) -> np.ndarray:
        return np.concatenate(self._normal_jerk) if self._normal_jerk else np.empty(0)

    def timestamps_s(self) -> np.ndarray:
        return np.concatenate(self._timestamps) if self._timestamps else np.empty(0)


def _channel_indices(snapshot: AnalysisSourceSnapshot, pzt_columns: Sequence[str]) -> list[int]:
    missing = [column for column in pzt_columns if column not in snapshot.channel_labels]
    if missing:
        raise ValueError(f"the source has no {missing} channel(s)")
    return [snapshot.channel_labels.index(column) for column in pzt_columns]


def _attach_observers(processor: TouchIdStreamProcessor, capture: _ReplayCapture) -> TraceRecorder:
    recorder = TraceRecorder()
    if isinstance(processor.gating, TraceObservable):
        processor.gating.set_trace_observer(recorder.observe)
    processor.set_derived_observer(capture.observe)
    return recorder


def _has_strength_gate(setup: ReplaySetup) -> bool:
    return setup.gating_v2_config is not None and setup.gating_v2_config.strength_gate_enabled


def compute_gate_replay(snapshot: AnalysisSourceSnapshot, setup: ReplaySetup) -> GateReplayResult:
    fs = float(snapshot.sample_rate_hz)
    processor = setup.new_processor()
    capture = _ReplayCapture()
    recorder = _attach_observers(processor, capture)
    ReplayDriver(
        processor, snapshot.data, snapshot.timestamps_s, setup.pzt_columns,
        _channel_indices(snapshot, setup.pzt_columns), fs, setup.hop_size_s,
    ).run_to_end()
    normal_jerk, timestamps_s = capture.normal_jerk(), capture.timestamps_s()
    burst = None
    if _has_strength_gate(setup):
        burst = compute_burst_score_series(normal_jerk, timestamps_s, fs, setup.gating_v2_config)
    description = processor.gating.describe() if isinstance(processor.gating, Describable) else ""
    return GateReplayResult(setup, recorder.trace(), timestamps_s, description, normal_jerk, burst)


def result_line(result: GateReplayResult) -> str:
    accepted_s = sum(end - start for start, end in result.accepted_spans_s())
    total_s = float(result.timestamps_s[-1] - result.timestamps_s[0]) if len(result.timestamps_s) > 1 else 0.0
    share = 100.0 * accepted_s / total_s if total_s > 0 else 0.0
    return f"Result: {accepted_s:.1f} s of {total_s:.1f} s accepted ({share:.0f}%) in {len(result.trace.windows)} windows."


def span_overlap_s(first: Sequence[Span], second: Sequence[Span]) -> float:
    """Seconds covered by both span lists (each non-overlapping within itself)."""
    first, second = sorted(first), sorted(second)
    total, i, j = 0.0, 0, 0
    while i < len(first) and j < len(second):
        total += max(0.0, min(first[i][1], second[j][1]) - max(first[i][0], second[j][0]))
        if first[i][1] < second[j][1]:
            i += 1
        else:
            j += 1
    return total


def label_overlap_line(accepted: Sequence[Span], labels: Sequence[Span]) -> str:
    accepted_s = sum(end - start for start, end in accepted)
    labelled_s = sum(end - start for start, end in labels)
    overlap_s = span_overlap_s(accepted, labels)
    unlabelled_s = accepted_s - overlap_s
    labelled_share = 100.0 * overlap_s / labelled_s if labelled_s > 0 else 0.0
    unlabelled_share = 100.0 * unlabelled_s / accepted_s if accepted_s > 0 else 0.0
    return (f"Hand labels: {labelled_s:.1f} s labelled, {labelled_share:.0f}% of it accepted; "
            f"{unlabelled_s:.1f} s ({unlabelled_share:.0f}%) of the accepted signal is unlabelled.")
