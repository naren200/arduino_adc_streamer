"""
V1 Gating
=========
Single-range windows. With an idle baseline, sample-accurate ActiveSampleQueue
segmentation over a continuous sample store; without one, the plain fixed
window/hop grid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.piezo_engine.live_window import LiveWindow
from core.texture_piezo.gating.v1.quality_gate import MICRO_CHUNK_S
from core.texture_piezo.gating.v1.segmentation import ActiveSampleQueue

from .buffer import RollingBuffer
from .gating_strategy import DerivedChunk, GatingParams, ReadyWindow
from .live_channels import named_engine_channels
from .sample_store import ContinuousSampleStore


logger = logging.getLogger("texture_piezo.gating.v1")


@dataclass(frozen=True)
class V1GatingOptions:
    """Construction options only the v1 strategy reads."""

    span_stale_timeout_s: float


class V1Gating:
    def __init__(self, params: GatingParams) -> None:
        if params.v1_options is None:
            raise ValueError("gating version 'v1' needs GatingParams.v1_options")
        if params.v2_config is not None:
            raise ValueError("gating version 'v1' has no GatingParams.v2_config; it would be silently ignored")
        self._params = params
        self._onset_skip_s = params.onset_skip_s
        self.reset()

    def reset(self) -> None:
        """(Re)initialize both windowing branches. The fixed-grid fallback (used only
        while there is no idle baseline) is one RollingBuffer over the requested engine
        channels, so a window's channels are aligned by construction. The active-queue
        branch's store is dropped with its ActiveSampleQueue, which is rebuilt lazily
        (see _ensure_active_queue) once samples arrive again."""
        params = self._params
        self.buffer = RollingBuffer(
            n_channels=len(params.required_channels),
            window_size_s=params.window_size_s,
            hop_size_s=params.hop_size_s,
        )
        self.store = ContinuousSampleStore(pzt_columns=params.pzt_columns, channel_names=params.required_channels)
        self.active_queue: ActiveSampleQueue | None = None

    def set_onset_skip_s(self, onset_skip_s: float) -> None:
        self._onset_skip_s = onset_skip_s
        if self.active_queue is not None:
            self.active_queue.onset_skip_s = onset_skip_s

    def set_gate_k(self, k: float) -> None:
        if self.active_queue is not None:
            self.active_queue.k = k

    def push(self, chunk: DerivedChunk) -> list[ReadyWindow]:
        if self._params.idle_baseline is None:
            ready = self._push_fixed_grid(chunk)
        else:
            ready = self._push_active_queue(chunk)
        self._log_windows(ready)
        return ready

    @staticmethod
    def _log_windows(ready: list[ReadyWindow]) -> None:
        if not logger.isEnabledFor(logging.DEBUG):
            return
        for window in ready:
            logger.debug("window_emitted", extra={"frag_id": window.frag_id, "n_samples": window.window.n_samples})

    def _push_fixed_grid(self, chunk: DerivedChunk) -> list[ReadyWindow]:
        """No baseline captured yet -- classify every window on the plain
        fixed window_size_s/hop_size_s grid unconditionally (matches
        is_window_quality's old no-op-without-a-baseline behavior)."""
        params = self._params
        named = named_engine_channels(
            params.pzt_columns, chunk.channel_samples, chunk.derived, params.required_channels,
        )
        self.buffer.push(named, chunk.timestamps)
        start_abs = self.buffer.dropped_count
        window = self.buffer.get_window()
        if window is None:
            return []
        stacked, window_ts = window
        live_window = LiveWindow.from_stacked(stacked, params.required_channels, chunk.fs)
        ranges = ((start_abs, start_abs + len(window_ts)),)
        return [ReadyWindow(window=live_window, window_ts=window_ts, frag_id=None, ranges=ranges)]

    def _ensure_active_queue(self) -> ActiveSampleQueue:
        """Lazily build the ActiveSampleQueue on the first tick samples
        arrive after a baseline exists (it is rebuilt after reset)."""
        if self.active_queue is None:
            params = self._params
            self.active_queue = ActiveSampleQueue(
                window_size_s=params.window_size_s,
                hop_size_s=params.hop_size_s,
                baseline=params.idle_baseline,
                onset_skip_s=self._onset_skip_s,
            )
            self.store._chunk_cursor_abs = self.store._store_next_abs
        return self.active_queue

    def _push_active_queue(self, chunk: DerivedChunk) -> list[ReadyWindow]:
        """Baseline present: sample-accurate ActiveSampleQueue segmentation
        (gating/v1/segmentation.py) -- feed this tick's newly-derived samples
        into the continuous store, then chunk/segment off of it instead of a
        fixed hop grid.

        Chunks any newly-appended continuous-store samples into 0.05s
        micro-chunks, feeds them into the queue, drains whatever windows it
        yields, and expires fragments too old to still matter. Idle is now
        stripped out of a fragment before windowing (not rejected after), so
        a returned window cannot straddle a genuine idle gap by
        construction -- there is no post-hoc idle-fraction rejection step
        anymore."""
        store = self.store
        store.append(chunk.channel_samples, chunk.derived, chunk.timestamps)
        queue = self._ensure_active_queue()

        self._feed_micro_chunks(queue, max(1, round(MICRO_CHUNK_S * chunk.fs)), chunk.now_t)
        windows = queue.ready_windows(store._store_ts, store._store_base_abs, pad_short_spans=True)
        queue.expire(chunk.now_t)

        ready = self._slice_ready_windows(windows, chunk.fs)
        store.trim(queue, self._params.window_size_s, self._params.v1_options.span_stale_timeout_s)
        return ready

    def _feed_micro_chunks(self, queue: ActiveSampleQueue, chunk_n: int, now_t: float) -> None:
        store = self.store
        while store._chunk_cursor_abs + chunk_n <= store._store_next_abs:
            start_abs = store._chunk_cursor_abs
            end_abs = start_abs + chunk_n
            start_i = start_abs - store._store_base_abs
            end_i = end_abs - store._store_base_abs
            queue.push_micro_chunk(
                (start_abs, end_abs), store._store_raw[start_i:end_i], store._store_ts[start_i], now_t,
            )
            store._chunk_cursor_abs = end_abs

    def _slice_ready_windows(self, windows: list[tuple[int, int, int]], fs: float) -> list[ReadyWindow]:
        ready: list[ReadyWindow] = []
        for start_abs, end_abs, frag_id in windows:
            sliced = self.store.slice(start_abs, end_abs)
            if sliced is None:
                continue
            channels, window_ts = sliced
            ready.append(ReadyWindow(
                window=LiveWindow(channels, fs), window_ts=window_ts, frag_id=frag_id,
                ranges=((start_abs, end_abs),),
            ))
        return ready
