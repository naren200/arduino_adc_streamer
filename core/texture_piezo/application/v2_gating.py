"""
V2 Gating
=========
Windows built by concatenating non-adjacent stretches of "good signal". Independent
stages (strength: sustained |normal_jerk| bursts; idle: v1's per-chunk idle test)
each report good ranges; their intersection feeds a GoodQueue that cuts a window
whenever ``window_size_s`` of good signal has accumulated, possibly across several
ranges. Everything is measured on sweep timestamps, so ``DerivedChunk.now_t`` is not read.
Queue changes and emitted windows are reported as DEBUG events (gating_v2_events.py).
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from core.piezo_engine.live_window import LiveWindow
from core.texture_piezo.gating.v2.combiner import GoodRangeCombiner
from core.texture_piezo.gating.v2.good_queue import GoodQueue, GoodQueueConfig, StoreTimes, WindowSpan
from core.texture_piezo.gating.v2.idle_stage import IdleStage
from core.texture_piezo.gating.v2.ranges import IndexRange, seam_indices
from core.texture_piezo.gating.v2.strong_signal import StrongSignalStage

from . import gating_v2_events as events
from .gating_strategy import DerivedChunk, GatingParams, ReadyWindow
from .sample_store import ContinuousSampleStore

NORMAL_JERK_CHANNEL = "normal_jerk"


class V2Gating:
    def __init__(self, params: GatingParams) -> None:
        if params.v2_config is None:
            raise ValueError("gating version 'v2' needs GatingParams.v2_config")
        if params.v2_config.expiry_s < params.window_size_s:
            raise ValueError(
                f"expiry_s ({params.v2_config.expiry_s}) must be >= window_size_s ({params.window_size_s}), "
                "or no window could ever accumulate"
            )
        self._params = params
        self._config = params.v2_config
        self._onset_skip_s = params.onset_skip_s
        # Optional inspection hook: called with (per-stage good pieces, combined pieces) on every push.
        self.stage_observer: Callable[[list[list[IndexRange]], list[IndexRange]], None] | None = None
        self.reset()

    @property
    def idle_gate_waiting_for_baseline(self) -> bool:
        """The idle gate is enabled but has no baseline yet, so it constrains nothing."""
        return self._config.idle_gate_enabled and self._params.idle_baseline is None

    def reset(self) -> None:
        params, config = self._params, self._config
        self.store = ContinuousSampleStore(pzt_columns=params.pzt_columns, channel_names=params.required_channels)
        self._strength = StrongSignalStage(config) if config.strength_gate_enabled else None
        self._idle = None
        if config.idle_gate_enabled and params.idle_baseline is not None:
            self._idle = IdleStage(params.idle_baseline, params.window_size_s)
        stage_count = sum(stage is not None for stage in (self._strength, self._idle))
        self._combiner = GoodRangeCombiner(stage_count, self._onset_skip_s)
        self._queue = GoodQueue(
            GoodQueueConfig(params.window_size_s, params.hop_size_s, config.expiry_s, config.concat_enabled)
        )
        self._was_waiting_for_baseline = False

    def set_onset_skip_s(self, onset_skip_s: float) -> None:
        self._onset_skip_s = onset_skip_s
        self._combiner.onset_skip_s = onset_skip_s

    def set_gate_k(self, k: float) -> None:
        if self._idle is not None:
            self._idle.k = k

    def push(self, chunk: DerivedChunk) -> list[ReadyWindow]:
        self._require_normal_jerk(chunk)
        self._announce_waiting_for_baseline()
        self.store.append(chunk.channel_samples, chunk.derived, chunk.timestamps)
        times = StoreTimes(self.store.timestamps, self.store.base_abs, 1.0 / chunk.fs)
        pieces = self._stage_pieces(chunk)
        combined = self._combiner.combine(pieces, times)
        if self.stage_observer is not None:
            self.stage_observer(pieces, combined)
        pushed = self._queue.push(combined, times)
        if events.is_enabled():
            events.log_queue_changes(pushed.changes)
        ready = self._ready_windows(pushed.windows, chunk.fs)
        self._trim_store(times)
        return ready

    def _announce_waiting_for_baseline(self) -> None:
        is_waiting = self.idle_gate_waiting_for_baseline
        if is_waiting and not self._was_waiting_for_baseline and events.is_enabled():
            events.log_waiting_for_baseline()
        self._was_waiting_for_baseline = is_waiting

    def _require_normal_jerk(self, chunk: DerivedChunk) -> None:
        if self._strength is not None and NORMAL_JERK_CHANNEL not in chunk.derived:
            raise ValueError(
                f"the strength gate reads the engine channel {NORMAL_JERK_CHANNEL!r}, which this chunk's "
                f"derived channels lack (has {sorted(chunk.derived)})"
            )

    def _stage_pieces(self, chunk: DerivedChunk) -> list[list[IndexRange]]:
        pieces = []
        if self._strength is not None:
            pieces.append(self._strength.feed(np.asarray(chunk.derived[NORMAL_JERK_CHANNEL]), chunk.fs))
        if self._idle is not None:
            pieces.append(self._idle.feed(self.store, chunk.fs))
        return pieces

    def _ready_windows(self, spans: tuple[WindowSpan, ...], fs: float) -> list[ReadyWindow]:
        ready: list[ReadyWindow] = []
        for span in spans:
            sliced = self.store.slice_ranges(span.ranges)
            if sliced is None:
                continue
            channels, window_ts = sliced
            window = LiveWindow(channels, fs, seam_idx=seam_indices(span.ranges))
            ready.append(ReadyWindow(window=window, window_ts=window_ts, frag_id=span.range_id, ranges=span.ranges))
            if events.is_enabled():
                events.log_window(ready[-1], span.range_ids)
        return ready

    def _trim_store(self, times: StoreTimes) -> None:
        """The queue expires everything older than ``expiry_s`` and the stages look back
        well under that, so nothing older is ever read again."""
        horizon = times.latest - self._config.expiry_s
        keep_from = times.first_at_or_after(horizon, times.base_abs, times.base_abs + len(times.values))
        self.store.trim_to(keep_from)
