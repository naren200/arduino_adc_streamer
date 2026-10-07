"""
TouchID Stream Processor
========================
GUI-free extraction of the buffering/derivation/segmentation logic that
turns newly-arrived raw PZT sweeps into classifier-ready windows -- the
single source of truth for BOTH live streaming (gui/inference_panel.py,
fed one hop's worth of sweeps per QTimer tick, now_t=time.monotonic()) and
offline replay (fed the same size chunks as fast as possible, now_t derived
from the snapshot's own sample clock -- see push_chunk's docstring for why
the two need different clocks).

Owns:
  - The engine pipeline (core/piezo_engine/pipeline.py: median-N despike,
    "integrated"/shear-jerk/normal-jerk causal
    derivation, warmup drop). Feeding it in small incremental chunks or
    fewer/larger ones produces IDENTICAL numbers, which is what makes
    sharing it between live, replay and training valid in the first place.
  - The RollingBuffer pair (fixed-grid fallback path, used only when no idle
    baseline has been captured yet -- see push_chunk).
  - The continuous raw+derived sample store + its trim/slice logic, feeding
    ActiveSampleQueue's index-only bookkeeping (used once a baseline exists).
  - The ActiveSampleQueue lifecycle itself (lazy construction, micro-chunking,
    fragment expiry).

Does NOT own: classification (the caller submits ReadyWindows to whatever
worker/synchronous path it likes), the "which of this tick's windows to
submit" policy (live submits only the newest and drops the rest under load;
replay must submit and wait for every one -- both are caller policy, not
processor policy), or any GUI/plotting state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .buffer import RollingBuffer

from .live_channels import DEFAULT_CHANNELS, named_engine_channels, needs_force

from core.piezo_engine.channel_names import FORCE_COLUMNS
from core.piezo_engine.config import EngineConfig, EngineConfigMismatchError, TimingMode, TimingPolicy
from core.piezo_engine.live_window import LiveWindow
from core.piezo_engine.pipeline import PiezoEnginePipeline, InputConditioning, RAW_INPUT
from core.texture_piezo.gating.window_config import ONSET_SKIP_S
from core.texture_piezo.gating.quality_gate import IdleBaseline, MICRO_CHUNK_S
from core.texture_piezo.gating.segmentation import ActiveSampleQueue
from core.texture_piezo.gating.time_window import first_index_at_or_after

# Engine for the live path when the loaded model declares no EngineConfig of its own
# (every hand-crafted-feature runtime): continuous leak timing, no force stage -- those models read none.
# A self-describing model supplies its own config.
LIVE_ENGINE_CONFIG = EngineConfig(timing=TimingPolicy(mode=TimingMode.CONTINUOUS), compute_force=False)


def resolve_live_engine_config(bundle_config: EngineConfig | None = None) -> EngineConfig:
    """Loader hook: the EngineConfig the live path must run for a model bundle -- the
    bundle's own config, or the placeholder ``LIVE_ENGINE_CONFIG`` without one."""
    return LIVE_ENGINE_CONFIG if bundle_config is None else bundle_config


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
        TouchIdStreamProcessor._store_reset)."""
        self._store_raw = np.empty((0, len(self.pzt_columns)))  # the idle gate's input, sensor-column order
        self._store_channels = {name: np.empty(0) for name in self.channel_names}
        self._store_ts = np.empty(0)
        self._store_base_abs = 0  # abs index of store[0]
        self._store_next_abs = 0  # abs index just past the last appended sample
        self._chunk_cursor_abs = 0  # abs index up to which micro-chunks have been pushed

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


class DerivedChannelPipeline:
    """TouchID's adapter over ``PiezoEnginePipeline`` (median-N -> derived
    channels -> warmup drop), keeping the two-call tick
    contract the GUI relies on: ``filter_raw`` (despiked, full-length, also
    feeds the Signal Stream plot and idle-baseline accumulation) then
    ``process`` (derived + warmup drop, outputs aligned with
    timestamps).

    Live timing: the engine's force stage runs on the uniform ``i / fs`` grid of
    the GLOBAL sample index (what training uses), so live timestamps
    are deliberately NOT forwarded to it -- they are wall-clock based, can jitter or
    go backwards, and force integrates against them. A model whose features read no
    force sets ``EngineConfig.compute_force=False`` and the stage is skipped."""

    def __init__(
        self,
        pzt_columns: list[str],
        config: EngineConfig = LIVE_ENGINE_CONFIG,
        input_conditioning: InputConditioning = RAW_INPUT,
    ) -> None:
        self.pzt_columns = list(pzt_columns)
        self._engine = PiezoEnginePipeline(self.pzt_columns, config, input_conditioning=input_conditioning)
        # Fail-fast marker: process() assumes filter_raw() already ran on this
        # tick's channel_samples -- catch a caller that forgets or reorders
        # the call, instead of silently letting an unfiltered blip slip in.
        self._raw_filtered_this_tick = False

    @property
    def engine_config(self) -> EngineConfig:
        return self._engine.config

    def adopt_state_from(self, other: "DerivedChannelPipeline") -> None:
        """Continue ``other``'s stream (median window, derived sums and
        medians, force stage) instead of starting every stage cold."""
        self._engine.adopt_state_from(other._engine)
        self._raw_filtered_this_tick = other._raw_filtered_this_tick

    def filter_raw(self, channel_samples: dict) -> dict:
        """Causal median-N despike (engine median stage). Must be called ONCE
        per tick, before channel_samples is used for anything else -- callers
        must not filter twice."""
        filtered = self._engine.filter_raw(channel_samples)
        self._raw_filtered_this_tick = True
        return filtered

    def process(self, channel_samples: dict, timestamps: np.ndarray, fs: float) -> tuple[dict, dict, np.ndarray]:
        """Derived channels and warmup drop for this tick's
        already-``filter_raw``ed chunk; returns (raw, derived, timestamps)
        trimmed together. Raises RuntimeError if filter_raw() was not called
        this tick first."""
        if not self._raw_filtered_this_tick:
            raise RuntimeError(
                "push_chunk called without a matching filter_raw() call this tick -- "
                "process assumes raw was already blip-filtered"
            )
        self._raw_filtered_this_tick = False
        result = self._engine.process_filtered(channel_samples, sample_rate_hz=fs)
        derived = {
            "integrated": dict(result.integrated),
            "shear_jerk_lr": result.shear_jerk_lr,
            "shear_jerk_tb": result.shear_jerk_tb,
            "normal_jerk": result.normal_jerk,
        }
        if self.engine_config.compute_force:
            derived["shear_force_lr"] = result.shear_force_lr
            derived["shear_force_tb"] = result.shear_force_tb
            derived["normal_force"] = result.normal_force
        timestamps = np.asarray(timestamps).reshape(-1)
        return dict(result.raw), derived, timestamps[result.dropped_leading.total:]


@dataclass
class ReadyWindow:
    """One classifier-ready window, plus the bookkeeping the caller needs to
    paint it and submit it. ``window`` carries exactly the engine channels the
    loaded model asked for, by name, at native length and the window's sample rate.
    frag_id is None in the fixed-grid fallback branch (no fragment concept there --
    see _touchid_region_color_for_span's docstring in gui/inference_panel.py for how
    callers use this)."""

    window: LiveWindow
    window_ts: np.ndarray
    frag_id: int | None


class TouchIdStreamProcessor:
    """Feed it successive, causally-ordered chunks of raw PZT sweeps via
    push_chunk(); it returns whatever ReadyWindows became available this
    tick (zero, one, or several)."""

    def __init__(
        self,
        pzt_columns: list[str],
        window_size_s: float,
        hop_size_s: float,
        span_stale_timeout_s: float,
        idle_baseline: IdleBaseline | None,
        onset_skip_s: float = ONSET_SKIP_S,
        input_conditioning: InputConditioning = RAW_INPUT,
        engine_config: EngineConfig = LIVE_ENGINE_CONFIG,
        required_channels: Sequence[str] = DEFAULT_CHANNELS,
    ) -> None:
        self.pzt_columns = list(pzt_columns)
        self.required_channels = tuple(required_channels)
        self.window_size_s = float(window_size_s)
        self.hop_size_s = float(hop_size_s)
        self.span_stale_timeout_s = float(span_stale_timeout_s)
        self.idle_baseline = idle_baseline
        self.onset_skip_s = float(onset_skip_s)

        # Causal derivation + raw despike + warmup-drop logic (see DerivedChannelPipeline).
        self._derived_pipeline = DerivedChannelPipeline(
            pzt_columns=self.pzt_columns, config=engine_config, input_conditioning=input_conditioning,
        )

        if needs_force(self.required_channels) and not self.engine_config.compute_force:
            raise EngineConfigMismatchError(
                f"the model reads force channels {[n for n in self.required_channels if n in FORCE_COLUMNS]} but the "
                "engine config has compute_force=False"
            )

        self._last_sweep_ts: float | None = None
        self._reset_fixed_grid_buffers()
        self._store_reset()

    def _reset_fixed_grid_buffers(self) -> None:
        """Fixed-grid fallback path (used only while idle_baseline is None): one
        RollingBuffer over the requested engine channels, so a window's channels are
        aligned by construction."""
        self._buffer = RollingBuffer(
            n_channels=len(self.required_channels), window_size_s=self.window_size_s, hop_size_s=self.hop_size_s,
        )

    def _restart_windowing_if_timeline_broke(self, timestamps: np.ndarray) -> None:
        """Timestamp-range windowing is only valid on non-decreasing
        timestamps. They restart near zero on a capture restart and wrap
        every ~71.6 min (32-bit microsecond MCU clock), so start the
        windowing state over instead of mis-slicing across the break."""
        went_backwards = self._last_sweep_ts is not None and timestamps[0] < self._last_sweep_ts
        if went_backwards or np.any(np.diff(timestamps) < 0):
            self._reset_fixed_grid_buffers()
            self._store_reset()
        self._last_sweep_ts = float(timestamps[-1])

    @property
    def engine_config(self) -> EngineConfig:
        return self._derived_pipeline.engine_config

    def adopt_engine_state_from(self, other: "TouchIdStreamProcessor") -> None:
        """Continue ``other``'s sample stream: its whole engine state (median window,
        bounded sums, causal medians, force stage) stays valid across a
        window/hop resize because the raw stream is unbroken (gui/inference_panel.py's
        _rebuild_touchid_buffers). The windowing/queue state is NOT taken over."""
        self._derived_pipeline.adopt_state_from(other._derived_pipeline)

    def _store_reset(self) -> None:
        """(Re)initialize the continuous raw+derived sample store (used only
        once an idle baseline exists) and drop the ActiveSampleQueue built
        on top of it -- it's rebuilt lazily (see _ensure_active_queue) once a
        measured fs is available again."""
        self._store = ContinuousSampleStore(pzt_columns=self.pzt_columns, channel_names=self.required_channels)
        self.active_queue: ActiveSampleQueue | None = None

    def _ensure_active_queue(self) -> None:
        """Lazily build the ActiveSampleQueue on the first tick samples
        arrive after a baseline exists (it is rebuilt after _store_reset)."""
        if self.active_queue is not None or self.idle_baseline is None:
            return
        self.active_queue = ActiveSampleQueue(
            window_size_s=self.window_size_s,
            hop_size_s=self.hop_size_s,
            baseline=self.idle_baseline,
            onset_skip_s=self.onset_skip_s,
        )
        self._store._chunk_cursor_abs = self._store._store_next_abs

    def filter_raw(self, channel_samples: dict) -> dict:
        """Causal median-N despike (the engine's CausalMedianN, the same stage
        training uses), so live and offline raw are despiked identically.
        Must be called ONCE per tick,
        before channel_samples is used for anything else (the Signal Stream
        plot, idle-baseline accumulation, AND push_chunk) -- callers must not
        filter twice."""
        return self._derived_pipeline.filter_raw(channel_samples)

    def push_chunk(
        self, channel_samples: dict, timestamps: np.ndarray, fs: float, now_t: float,
    ) -> list[ReadyWindow]:
        """Advance the derived-channel causal state by exactly this newly-
        pushed chunk, then feed it into whichever windowing branch is active,
        returning every ReadyWindow that became available this call.

        now_t is caller-supplied, never read from the wall clock in here:
        live streaming passes time.monotonic() (hops really do arrive
        ~hop_size_s apart in real time, so ActiveSampleQueue.expire's
        staleness timeout means what it says). Replay must NOT pass
        wall-clock time -- it runs the whole capture as fast as possible, so
        wall-clock would barely advance between chunks and every span would
        look far younger than span_stale_timeout_s regardless of how much
        (sample-time) history it actually spans, silently disabling eviction.
        Replay instead passes a sample-derived clock (e.g.
        end_sample_index / fs, matching the snapshot's own timestamps_s) so
        expire sees the same real-time deltas the algorithm would have
        seen live for that exact recording, reproducing identical
        segmentation decisions.

        Leading samples are dropped by the engine pipeline: the engine's common
        window-fill warmup (config.leading_warmup_samples,
        max(integration, jerk window) - 1 = 29) is dropped from its outputs, the
        same as training. Sources already median-filtered upstream (Analysis
        snapshot replay) declare it via ``input_conditioning`` so the median
        does not run twice.
        """
        channel_samples, derived, timestamps = self._derived_pipeline.process(
            channel_samples, timestamps, fs,
        )
        if timestamps.size == 0:
            return []
        self._restart_windowing_if_timeline_broke(timestamps)

        if self.idle_baseline is None:
            return self._push_chunk_fixed_grid(channel_samples, derived, timestamps, fs)
        return self._push_chunk_active_queue(channel_samples, derived, timestamps, fs, now_t)

    def _push_chunk_fixed_grid(
        self, channel_samples: dict, derived: dict, timestamps: np.ndarray, fs: float,
    ) -> list[ReadyWindow]:
        """No baseline captured yet -- classify every window on the plain
        fixed window_size_s/hop_size_s grid unconditionally (matches
        is_window_quality's old no-op-without-a-baseline behavior)."""
        named = named_engine_channels(self.pzt_columns, channel_samples, derived, self.required_channels)
        self._buffer.push(named, timestamps)
        window = self._buffer.get_window()
        if window is None:
            return []
        stacked, window_ts = window
        live_window = LiveWindow.from_stacked(stacked, self.required_channels, fs)
        return [ReadyWindow(window=live_window, window_ts=window_ts, frag_id=None)]

    def _push_chunk_active_queue(
        self, channel_samples: dict, derived: dict, timestamps: np.ndarray, fs: float, now_t: float,
    ) -> list[ReadyWindow]:
        """Baseline present: sample-accurate ActiveSampleQueue segmentation
        (inference/segmentation.py) -- feed this tick's newly-derived samples
        into the continuous store, then chunk/segment off of it instead of a
        fixed hop grid.

        Chunks any newly-appended continuous-store samples into 0.05s
        micro-chunks, feeds them into the queue, drains whatever windows it
        yields, and expires fragments too old to still matter. Idle is now
        stripped out of a fragment before windowing (not rejected after), so
        a returned window cannot straddle a genuine idle gap by
        construction -- there is no post-hoc idle-fraction rejection step
        anymore."""
        store = self._store
        store.append(channel_samples, derived, timestamps)
        self._ensure_active_queue()
        queue = self.active_queue
        if queue is None:
            return []

        chunk_n = max(1, round(MICRO_CHUNK_S * fs))

        while store._chunk_cursor_abs + chunk_n <= store._store_next_abs:
            start_abs = store._chunk_cursor_abs
            end_abs = start_abs + chunk_n
            start_i = start_abs - store._store_base_abs
            end_i = end_abs - store._store_base_abs
            queue.push_micro_chunk(
                (start_abs, end_abs), store._store_raw[start_i:end_i], store._store_ts[start_i], now_t,
            )
            store._chunk_cursor_abs = end_abs

        windows = queue.ready_windows(
            store._store_ts, store._store_base_abs, pad_short_spans=True,
        )
        queue.expire(now_t)

        ready: list[ReadyWindow] = []
        for start_abs, end_abs, frag_id in windows:
            sliced = store.slice(start_abs, end_abs)
            if sliced is None:
                continue
            channels, window_ts = sliced
            ready.append(ReadyWindow(window=LiveWindow(channels, fs), window_ts=window_ts, frag_id=frag_id))

        store.trim(queue, self.window_size_s, self.span_stale_timeout_s)
        return ready
