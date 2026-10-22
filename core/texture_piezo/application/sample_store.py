"""
Continuous Sample Store
=======================
The growing raw+derived sample arrays a gating strategy cuts windows from,
with the absolute-index bookkeeping that survives front-trimming.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from core.texture_piezo.gating.v1.time_window import first_index_at_or_after

from .live_channels import DEFAULT_CHANNELS, named_engine_channels


class ContinuousSampleStore:
    """Owns the continuous raw+derived sample store used once an idle
    baseline exists (the ActiveSampleQueue path): the raw sweeps the idle gate
    reads, one array per requested ENGINE channel name (what a model's window is
    cut from), and the timestamps -- all parallel -- plus the 3 index trackers
    (_store_base_abs/_store_next_abs/_chunk_cursor_abs) needed to translate
    ActiveSampleQueue's absolute indices into slices of the (periodically
    trimmed) arrays.

    ``channel_names`` are engine channel names (``channel_names.py``); every one of them
    is trimmed and sliced in lockstep, so padding/short-window logic, which only moves
    absolute indices, treats force like every other channel."""

    def __init__(self, pzt_columns: list[str], channel_names: Sequence[str] = DEFAULT_CHANNELS) -> None:
        self.pzt_columns = list(pzt_columns)
        self.channel_names = tuple(channel_names)
        self.reset()

    def reset(self) -> None:
        """(Re)initialize the store to empty -- called on construction and
        whenever the caller drops its ActiveSampleQueue (see
        V1Gating.reset)."""
        self._store_raw = np.empty((0, len(self.pzt_columns)))  # the idle gate's input, sensor-column order
        self._store_channels = {name: np.empty(0) for name in self.channel_names}
        self._store_ts = np.empty(0)
        self._store_base_abs = 0  # abs index of store[0]
        self._store_next_abs = 0  # abs index just past the last appended sample
        self._chunk_cursor_abs = 0  # abs index up to which micro-chunks have been pushed

    @property
    def base_abs(self) -> int:
        return self._store_base_abs

    @property
    def next_abs(self) -> int:
        return self._store_next_abs

    @property
    def timestamps(self) -> np.ndarray:
        return self._store_ts

    def raw(self, start_abs: int, end_abs: int) -> np.ndarray:
        """The raw sensor-column samples [start_abs, end_abs), as the idle gate reads them."""
        return self._store_raw[start_abs - self._store_base_abs:end_abs - self._store_base_abs]

    def append(self, channel_samples: dict, derived: dict, timestamps: np.ndarray) -> None:
        """Append this tick's newly-pushed raw+derived samples to the
        continuous store, in lockstep, at the running absolute index
        ActiveSampleQueue's yielded (start_idx, end_idx) pairs reference."""
        raw = np.stack([channel_samples[col] for col in self.pzt_columns], axis=1)
        named = named_engine_channels(self.pzt_columns, channel_samples, derived, self.channel_names)
        self._store_raw = np.concatenate([self._store_raw, raw], axis=0)
        for name, values in named.items():
            self._store_channels[name] = np.concatenate([self._store_channels[name], values])
        self._store_ts = np.concatenate([self._store_ts, np.asarray(timestamps, dtype=np.float64)])
        self._store_next_abs += len(raw)

    def trim(self, queue, window_size_s: float, span_stale_timeout_s: float) -> None:
        """Drop the front of the continuous store once no live span
        (finalized or open, per queue.oldest_referenced_idx) references it
        anymore, keeping a window_size_s + span_stale_timeout_s safety
        margin so a still-growing open span never has its start index
        trimmed out from under it."""
        if queue is None:
            return
        if len(self._store_ts) == 0:
            return
        margin_s = window_size_s + span_stale_timeout_s
        oldest_referenced = queue.oldest_referenced_idx()
        safe_abs = self._chunk_cursor_abs if oldest_referenced is None else min(
            oldest_referenced, self._chunk_cursor_abs
        )
        safe_i = min(safe_abs - self._store_base_abs, len(self._store_ts) - 1)
        if safe_i < 0:
            return
        trim_n = first_index_at_or_after(self._store_ts, self._store_ts[safe_i] - margin_s)
        if trim_n <= 0:
            return
        trim_to_abs = self._store_base_abs + trim_n
        self._store_raw = self._store_raw[trim_n:]
        self._store_channels = {name: values[trim_n:] for name, values in self._store_channels.items()}
        self._store_ts = self._store_ts[trim_n:]
        self._store_base_abs = trim_to_abs

    def trim_to(self, keep_from_abs: int) -> None:
        """Drop every sample before ``keep_from_abs`` (no-op if already trimmed past it)."""
        trim_n = keep_from_abs - self._store_base_abs
        if trim_n <= 0:
            return
        self._store_raw = self._store_raw[trim_n:]
        self._store_channels = {name: values[trim_n:] for name, values in self._store_channels.items()}
        self._store_ts = self._store_ts[trim_n:]
        self._store_base_abs = keep_from_abs

    def slice(self, start_abs: int, end_abs: int):
        """Slice the continuous store at an absolute (start_idx, end_idx)
        pair from ActiveSampleQueue -- returns ``(channels, window_ts)`` where
        ``channels`` maps each requested engine channel name to its samples -- or
        None if the range has already been trimmed out (shouldn't happen given
        trim()'s safety margin, but guarded rather than slicing garbage)."""
        if start_abs < self._store_base_abs:
            return None
        start_i = start_abs - self._store_base_abs
        end_i = end_abs - self._store_base_abs
        channels = {name: values[start_i:end_i] for name, values in self._store_channels.items()}
        return channels, self._store_ts[start_i:end_i]

    def slice_ranges(self, ranges: Sequence[tuple[int, int]]):
        """Like ``slice`` over several absolute ranges: every channel and the timestamps
        concatenated in the order given. None if any range has been trimmed out."""
        slices = [self.slice(start, end) for start, end in ranges]
        if any(sliced is None for sliced in slices):
            return None
        channels = {
            name: np.concatenate([part[0][name] for part in slices]) for name in self.channel_names
        }
        return channels, np.concatenate([part[1] for part in slices])
