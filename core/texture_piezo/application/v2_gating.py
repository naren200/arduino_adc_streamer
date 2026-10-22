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

import numpy as np

from core.piezo_engine.live_window import LiveWindow
from core.texture_piezo.gating.v2.combiner import GoodRangeCombiner
from core.texture_piezo.gating.v2.good_queue import GoodQueue, GoodQueueConfig, StoreTimes, WindowSpan
from core.texture_piezo.gating.v2.idle_stage import IdleStage
from core.texture_piezo.gating.v2.ranges import IndexRange, seam_indices
from core.texture_piezo.gating.v2.strong_signal import StrongSignalStage

from . import gating_v2_events as events
from .gating_strategy import DerivedChunk, GatingParams, ReadyWindow
from .gating_trace import COMBINED_LANE, IDLE_LANE, STRENGTH_LANE, GateTick, TraceObserver
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
        self._trace_observer: TraceObserver | None = None
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
        stage_pieces = self._stage_pieces(chunk)
        combined = self._combiner.combine(list(stage_pieces.values()), times)
        pushed = self._queue.push(combined, times)
        if events.is_enabled():
            events.log_queue_changes(pushed.changes)
        ready = self._ready_windows(pushed.windows, chunk.fs)
        self._report_tick(stage_pieces, combined, ready)
        self._trim_store(times)
        return ready

    def set_trace_observer(self, observer: TraceObserver | None) -> None:
        self._trace_observer = observer

    def _report_tick(
        self, stage_pieces: dict[str, list[IndexRange]], combined: list[IndexRange], ready: list[ReadyWindow],
    ) -> None:
        if self._trace_observer is None:
            return
        lane_pieces = {name: tuple(pieces) for name, pieces in {**stage_pieces, COMBINED_LANE: combined}.items()}
        self._trace_observer(GateTick(lane_pieces=lane_pieces, windows=tuple(ready)))

    def describe(self) -> str:
        config = self._config
        lines = [self._describe_final_gate()]
        if config.strength_gate_enabled:
            lines.append(
                f"Strength: block of {config.block_s:g} s is strong when its sum of |{NORMAL_JERK_CHANNEL}| > "
                f"{config.burst_min:g}; good when more than {config.sustain_min} of the last "
                f"{config.sustain_blocks} blocks are strong."
            )
        if config.idle_gate_enabled:
            lines.append("Idle: a micro-chunk is active when a raw sample leaves the idle baseline's k-sigma band.")
        if self.idle_gate_waiting_for_baseline:
            lines.append("Idle gate is enabled but TouchID has no idle baseline yet, so it constrains nothing.")
        return "\n".join(lines)

    def _describe_final_gate(self) -> str:
        stages = [name for name, stage in ((STRENGTH_LANE, self._strength), (IDLE_LANE, self._idle)) if stage is not None]
        gate = " AND ".join(stages) if stages else "no stage (whole stream)"
        params, config = self._params, self._config
        return (
            f"Final gate: {gate} -> drop first {self._onset_skip_s:g} s of every new range"
            f" -> queue: window {params.window_size_s:g} s every {params.hop_size_s:g} s,"
            f" expiry {config.expiry_s:g} s, concat {'on' if config.concat_enabled else 'off'}"
        )

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

    def _stage_pieces(self, chunk: DerivedChunk) -> dict[str, list[IndexRange]]:
        pieces = {}
        if self._strength is not None:
            pieces[STRENGTH_LANE] = self._strength.feed(np.asarray(chunk.derived[NORMAL_JERK_CHANNEL]), chunk.fs)
        if self._idle is not None:
            pieces[IDLE_LANE] = self._idle.feed(self.store, chunk.fs)
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

