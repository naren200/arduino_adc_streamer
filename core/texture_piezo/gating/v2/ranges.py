"""Index-range helpers shared by the v2 gating stages. A range is a half-open
``(start_idx, end_idx)`` pair of absolute sample indices."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import accumulate

IndexRange = tuple[int, int]


def intersect_ranges(first: Sequence[IndexRange], second: Sequence[IndexRange]) -> list[IndexRange]:
    """Intersection of two start-ordered lists of non-overlapping ranges."""
    overlaps: list[IndexRange] = []
    i = j = 0
    while i < len(first) and j < len(second):
        start = max(first[i][0], second[j][0])
        end = min(first[i][1], second[j][1])
        if start < end:
            overlaps.append((start, end))
        if first[i][1] <= second[j][1]:
            i += 1
        else:
            j += 1
    return overlaps


def seam_indices(ranges: Sequence[IndexRange]) -> tuple[int, ...]:
    """Where each range after the first begins once the ranges are concatenated."""
    return tuple(accumulate(end - start for start, end in ranges[:-1]))
