"""
Good-Range Combiner
===================
Turns the enabled stages' newly known good pieces into the pieces the queue takes:
intersect the stages (the whole stream when none is enabled), drop the first
``onset_skip_s`` of every new range, and keep warmup samples out.

Each stage only ever reports pieces beyond what it reported before, so everything
new lies at or after ``_cursor``, the end of the last combined piece.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..v1.segmentation import WARMUP_SAMPLES

from .good_queue import StoreTimes
from .ranges import IndexRange, intersect_ranges


class GoodRangeCombiner:
    def __init__(self, stage_count: int, onset_skip_s: float) -> None:
        self.onset_skip_s = onset_skip_s
        self._stage_ranges: list[list[IndexRange]] = [[] for _ in range(stage_count)]
        self._cursor = 0
        self._has_range = False
        self._skip_until_ts = 0.0

    def combine(self, stage_pieces: Sequence[Sequence[IndexRange]], times: StoreTimes) -> list[IndexRange]:
        combined: list[IndexRange] = []
        for start, end in self._unskipped(stage_pieces, times):
            if not (self._has_range and start == self._cursor):
                self._skip_until_ts = times.at(start) + self.onset_skip_s
            self._has_range, self._cursor = True, end
            first_kept = times.first_at_or_after(self._skip_until_ts, start, end)
            first_kept = max(first_kept, start, WARMUP_SAMPLES)
            if first_kept < end:
                combined.append((first_kept, end))
        return combined

    def _unskipped(self, stage_pieces: Sequence[Sequence[IndexRange]], times: StoreTimes) -> list[IndexRange]:
        """The new intersection of all stages, before onset skip and warmup."""
        if not self._stage_ranges:
            stream_end = times.base_abs + len(times.values)
            return [(self._cursor, stream_end)] if self._cursor < stream_end else []
        for ranges, pieces in zip(self._stage_ranges, stage_pieces):
            ranges.extend(pieces)
        overlap = self._stage_ranges[0]
        for ranges in self._stage_ranges[1:]:
            overlap = intersect_ranges(overlap, ranges)
        self._forget_before(times.base_abs)
        return [(max(start, self._cursor), end) for start, end in overlap if end > self._cursor]

    def _forget_before(self, base_abs: int) -> None:
        for ranges in self._stage_ranges:
            ranges[:] = [(start, end) for start, end in ranges if end > base_abs]
