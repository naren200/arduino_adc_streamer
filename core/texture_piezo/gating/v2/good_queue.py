"""
Good-Range Queue
================
Pure bookkeeping of the good-signal ranges awaiting a window. Ranges are absolute
sample-index spans in stream order; every tick runs, in this order: append new
ranges (touching ones merge into the older id), expire the old head, then emit
windows -- each the first ``window_size_s`` of good signal across ranges -- deleting
``hop_size_s`` of good signal from the front after each. Time is measured only on
the sweep timestamps, never on a wall clock, so replay and live agree. The queue never
logs: each mutating call returns what changed (``QueueChanges``) for the caller to report.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np

from ..v1.time_window import TIMESTAMP_EPSILON_S, first_index_at_or_after

from .ranges import IndexRange


@dataclass(frozen=True)
class StoreTimes:
    """Read-only view of the sample store's sweep timestamps."""

    values: np.ndarray
    base_abs: int
    sample_interval_s: float

    @property
    def latest(self) -> float:
        return float(self.values[-1])

    def at(self, abs_idx: int) -> float:
        return float(self.values[abs_idx - self.base_abs])

    def first_at_or_after(self, target_ts: float, start_abs: int, end_abs: int) -> int:
        """First absolute index in [start_abs, end_abs) stamped at/after target_ts, else end_abs."""
        span = self.values[start_abs - self.base_abs:end_abs - self.base_abs]
        return start_abs + first_index_at_or_after(span, target_ts)

    def extent_s(self, start_abs: int, end_abs: int) -> float:
        """Time the samples [start_abs, end_abs) cover: first stamp to last stamp plus one sample interval."""
        return self.at(end_abs - 1) - self.at(start_abs) + self.sample_interval_s

    def elapsed_since_start(self, start_abs: int, end_abs: int) -> np.ndarray:
        span = self.values[start_abs - self.base_abs:end_abs - self.base_abs]
        return span - span[0]


@dataclass(frozen=True)
class GoodRange:
    """``end_ts`` is the stamp of the last included sample (``end_idx`` itself is exclusive)."""

    range_id: int
    start_idx: int
    end_idx: int
    start_ts: float
    end_ts: float


@dataclass(frozen=True)
class WindowSpan:
    """One window to cut: the id of its first range, the ids of every range it draws on, and
    the absolute ranges it concatenates."""

    range_id: int
    ranges: tuple[IndexRange, ...]
    range_ids: tuple[int, ...]


@dataclass(frozen=True)
class GoodQueueConfig:
    window_size_s: float
    hop_size_s: float
    expiry_s: float
    concat_enabled: bool


@dataclass(frozen=True)
class QueueChanges:
    """What one push did to the ranges, each as the range's state right after the change.
    ``opened``: new ids. ``merged``: a touching new piece absorbed into the tail (one entry per
    piece). ``extended``: ranges that existed before this push and now reach further.
    ``expired``: ranges dropped whole off the head (a head range merely cut shorter is not listed)."""

    opened: tuple[GoodRange, ...] = ()
    merged: tuple[GoodRange, ...] = ()
    extended: tuple[GoodRange, ...] = ()
    expired: tuple[GoodRange, ...] = ()


@dataclass(frozen=True)
class QueuePush:
    windows: tuple[WindowSpan, ...]
    changes: QueueChanges


class GoodQueue:
    def __init__(self, config: GoodQueueConfig) -> None:
        self._window_s = config.window_size_s - TIMESTAMP_EPSILON_S
        self._hop_s = config.hop_size_s - TIMESTAMP_EPSILON_S
        self._expiry_s = config.expiry_s
        self._concat_enabled = config.concat_enabled
        self._ranges: list[GoodRange] = []
        self._next_range_id = 0

    @property
    def ranges(self) -> tuple[GoodRange, ...]:
        return tuple(self._ranges)

    def push(self, new_ranges: Sequence[IndexRange], times: StoreTimes) -> QueuePush:
        existing_ids = {good.range_id for good in self._ranges}
        opened, merged = self._append(new_ranges, times)
        extended = tuple({good.range_id: good for good in merged if good.range_id in existing_ids}.values())
        expired = self._expire(times)
        windows = self._drain_windows(times)
        return QueuePush(tuple(windows), QueueChanges(opened, merged, extended, expired))

    def _append(
        self, new_ranges: Sequence[IndexRange], times: StoreTimes,
    ) -> tuple[tuple[GoodRange, ...], tuple[GoodRange, ...]]:
        opened: list[GoodRange] = []
        merged: list[GoodRange] = []
        for start, end in new_ranges:
            tail = self._ranges[-1] if self._ranges else None
            if tail is not None and tail.end_idx == start:
                self._ranges[-1] = replace(tail, end_idx=end, end_ts=times.at(end - 1))
                merged.append(self._ranges[-1])
            else:
                self._ranges.append(GoodRange(self._next_range_id, start, end, times.at(start), times.at(end - 1)))
                self._next_range_id += 1
                opened.append(self._ranges[-1])
        return tuple(opened), tuple(merged)

    def _expire(self, times: StoreTimes) -> tuple[GoodRange, ...]:
        """Cut the head only: whole ranges older than the horizon go, a straddling one
        just has its start advanced."""
        horizon = times.latest - self._expiry_s
        dropped: list[GoodRange] = []
        while self._ranges:
            head = self._ranges[0]
            new_start = times.first_at_or_after(horizon, head.start_idx, head.end_idx)
            if new_start >= head.end_idx:
                dropped.append(self._ranges.pop(0))
                continue
            if new_start > head.start_idx:
                self._ranges[0] = replace(head, start_idx=new_start, start_ts=times.at(new_start))
            break
        return tuple(dropped)

    def _drain_windows(self, times: StoreTimes) -> list[WindowSpan]:
        spans: list[WindowSpan] = []
        while (first := self._first_window_source(times)) is not None:
            sources = self._sources_from(first)
            window = _take_front(sources, self._window_s, times)
            range_ids = tuple(good.range_id for good in sources[:len(window)])
            spans.append(WindowSpan(self._ranges[first].range_id, tuple(window), range_ids))
            self._delete_front(first, _take_front(sources, self._hop_s, times), times)
        return spans

    def _sources_from(self, first: int) -> list[GoodRange]:
        return self._ranges[first:] if self._concat_enabled else self._ranges[first:first + 1]

    def _first_window_source(self, times: StoreTimes) -> int | None:
        """Index of the range a window starts in, or None while not enough good signal waits."""
        if self._concat_enabled:
            total_s = sum(times.extent_s(r.start_idx, r.end_idx) for r in self._ranges)
            return 0 if self._ranges and total_s >= self._window_s else None
        for index, good in enumerate(self._ranges):
            if times.extent_s(good.start_idx, good.end_idx) >= self._window_s:
                return index
        return None

    def _delete_front(self, first: int, deleted: list[IndexRange], times: StoreTimes) -> None:
        """Only the last deleted range can be cut partway; it keeps its id and gets a later start."""
        survivors = [
            replace(good, start_idx=deleted_end, start_ts=times.at(deleted_end))
            for good, (_, deleted_end) in zip(self._ranges[first:], deleted)
            if deleted_end < good.end_idx
        ]
        self._ranges[first:first + len(deleted)] = survivors


def _take_front(sources: Sequence[GoodRange], amount_s: float, times: StoreTimes) -> list[IndexRange]:
    """The leading ``amount_s`` of good signal over ``sources`` as index ranges. Each seam
    between sources counts as one sample interval, so time never jumps across a gap."""
    remaining_s = amount_s
    taken: list[IndexRange] = []
    for good in sources:
        elapsed = times.elapsed_since_start(good.start_idx, good.end_idx)
        sample_count = int(np.searchsorted(elapsed, remaining_s, side="left"))
        if sample_count < len(elapsed):
            taken.append((good.start_idx, good.start_idx + sample_count))
            break
        taken.append((good.start_idx, good.end_idx))
        remaining_s -= elapsed[-1] + times.sample_interval_s
        if remaining_s <= 0:
            break
    return taken
