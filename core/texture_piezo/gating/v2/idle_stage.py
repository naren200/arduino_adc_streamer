"""
Idle Stage
==========
v1's per-micro-chunk idle test re-expressed as good ranges: a micro-chunk is active
when any raw sample leaves the idle baseline's k-sigma band; idle runs shorter than
``idle_gap_chunks_cap`` stay inside the surrounding good range, longer ones are cut
out. There is no merge-grace hold -- v2 relies on window concatenation instead.
"""

from __future__ import annotations

from ..v1.quality_gate import (
    MICRO_CHUNK_S,
    IdleBaseline,
    chunk_is_active,
    idle_gap_chunks_cap,
)

from .ranges import IndexRange


def micro_chunk_samples(fs: float) -> int:
    return max(1, round(MICRO_CHUNK_S * fs))


class IdleStage:
    def __init__(self, baseline: IdleBaseline, window_size_s: float) -> None:
        self._baseline = baseline
        self.k = baseline.k
        self._gap_cap = idle_gap_chunks_cap(window_size_s)
        self._cursor = 0  # first sample not yet judged
        self._is_in_range = False
        self._idle_run_chunks = 0
        self._emitted_end = 0

    def feed(self, store, fs: float) -> list[IndexRange]:
        """Judge every whole micro-chunk the store gained and return the newly known
        good pieces. An absorbed idle gap is reported only once active signal resumes."""
        chunk_n = micro_chunk_samples(fs)
        pieces: list[IndexRange] = []
        while self._cursor + chunk_n <= store.next_abs:
            start, end = self._cursor, self._cursor + chunk_n
            self._cursor = end
            if chunk_is_active(store.raw(start, end), self._baseline, self.k):
                pieces.append(self._active_piece(start, end))
            else:
                self._note_idle_chunk()
        return pieces

    def _active_piece(self, start: int, end: int) -> IndexRange:
        piece_start = self._emitted_end if self._is_in_range else start
        self._is_in_range = True
        self._idle_run_chunks = 0
        self._emitted_end = end
        return piece_start, end

    def _note_idle_chunk(self) -> None:
        if not self._is_in_range:
            return
        self._idle_run_chunks += 1
        if self._idle_run_chunks >= self._gap_cap:
            self._is_in_range = False
            self._idle_run_chunks = 0
