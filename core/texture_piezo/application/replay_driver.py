"""
Replay Driver
=============
The data path of a replay: feeds a snapshot to a TouchIdStreamProcessor one hop at a time,
exactly as live streaming would have received it, and keeps EVERY window the processor
emits (in emission order) until the caller has taken it. No Qt, no I/O.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from .gating_strategy import ReadyWindow
from .stream_processor import TouchIdStreamProcessor


@dataclass(frozen=True)
class ReplayStep:
    channel_samples: dict
    timestamps: np.ndarray
    ready_windows: tuple[ReadyWindow, ...]
    is_done: bool


class ReplayDriver:
    def __init__(
        self, processor: TouchIdStreamProcessor, data: np.ndarray, timestamps_s: np.ndarray,
        pzt_columns: tuple[str, ...], channel_indices: list[int], fs: float, hop_size_s: float,
    ) -> None:
        self.processor = processor
        self._data = data
        self._timestamps_s = timestamps_s
        self._columns_and_indices = tuple(zip(pzt_columns, channel_indices))
        self._fs = fs
        self.hop_samples = max(1, round(hop_size_s * fs))
        self._cursor = 0
        self._pending: deque[ReadyWindow] = deque()

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    @property
    def is_exhausted(self) -> bool:
        return self._cursor >= len(self._data)

    def pop_pending(self) -> ReadyWindow:
        return self._pending.popleft()

    def _timestamps(self, start: int, end: int) -> np.ndarray:
        if self._timestamps_s.size:
            return np.asarray(self._timestamps_s[start:end], dtype=np.float64)
        return np.arange(start, end, dtype=np.float64) / self._fs

    def step(self) -> ReplayStep:
        """Feed the next hop. ``now_t`` is the replayed sample time (end / fs), never wall-clock:
        a replay runs as fast as possible, so wall-clock would make every span look younger than
        its stale timeout and silently disable expiry."""
        if self.is_exhausted:
            raise ValueError("replay already consumed the whole snapshot")
        start, end = self._cursor, min(self._cursor + self.hop_samples, len(self._data))
        raw = {col: self._data[start:end, idx].astype(np.float64) for col, idx in self._columns_and_indices}
        channel_samples = self.processor.filter_raw(raw)
        timestamps = self._timestamps(start, end)
        self._cursor = end
        ready = tuple(self.processor.push_chunk(channel_samples, timestamps, self._fs, now_t=end / self._fs))
        self._pending.extend(ready)
        return ReplayStep(channel_samples, timestamps, ready, self.is_exhausted)

    def run_to_end(self) -> list[ReadyWindow]:
        """Every window the rest of the snapshot produces, without classifying anything."""
        while not self.is_exhausted:
            self.step()
        windows = list(self._pending)
        self._pending.clear()
        return windows
