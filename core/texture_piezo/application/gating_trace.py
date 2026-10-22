"""
Gating Trace
============
A version-agnostic record of what a gating strategy decided, so a viewer can draw it
without naming a gating version or re-implementing a stage. A strategy that implements
``TraceObservable`` reports one ``GateTick`` per push; ``TraceRecorder`` folds the ticks
into an immutable ``GateTrace``. Reporting never feeds back into a gating decision.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from core.texture_piezo.gating.v2.ranges import IndexRange

from .gating_strategy import ReadyWindow


STRENGTH_LANE = "strength"
IDLE_LANE = "idle"
COMBINED_LANE = "combined"


@dataclass(frozen=True)
class GateTick:
    """What one push decided: the good ranges each named lane newly reported (absolute
    post-warmup sample indices) and the windows the push emitted."""

    lane_pieces: Mapping[str, tuple[IndexRange, ...]]
    windows: tuple[ReadyWindow, ...]


@dataclass(frozen=True)
class GateLane:
    """One named good-signal lane: merged, ordered, half-open index ranges."""

    name: str
    ranges: tuple[IndexRange, ...]


@dataclass(frozen=True)
class WindowRecord:
    """An emitted window; ``window_id`` is its emission sequence number."""

    window_id: int
    frag_id: int | None
    ranges: tuple[IndexRange, ...]
    seam_idx: tuple[int, ...]


@dataclass(frozen=True)
class GateTrace:
    lanes: tuple[GateLane, ...]
    windows: tuple[WindowRecord, ...]


TraceObserver = Callable[[GateTick], None]


@runtime_checkable
class TraceObservable(Protocol):
    def set_trace_observer(self, observer: TraceObserver | None) -> None:
        """Report a GateTick on every push to ``observer``; None detaches it."""
        ...


@runtime_checkable
class Describable(Protocol):
    def describe(self) -> str:
        """The strategy's gating logic in plain language, from its own configuration."""
        ...


def merge_ranges(ranges: Sequence[IndexRange]) -> tuple[IndexRange, ...]:
    merged: list[IndexRange] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


class TraceRecorder:
    def __init__(self) -> None:
        self._lane_ranges: dict[str, list[IndexRange]] = {}
        self._windows: list[WindowRecord] = []

    def observe(self, tick: GateTick) -> None:
        for name, pieces in tick.lane_pieces.items():
            self._lane_ranges.setdefault(name, []).extend(pieces)
        for window in tick.windows:
            self._windows.append(WindowRecord(
                window_id=len(self._windows), frag_id=window.frag_id,
                ranges=tuple(window.ranges), seam_idx=window.window.seam_idx,
            ))

    def trace(self) -> GateTrace:
        lanes = tuple(GateLane(name, merge_ranges(ranges)) for name, ranges in self._lane_ranges.items())
        return GateTrace(lanes=lanes, windows=tuple(self._windows))
