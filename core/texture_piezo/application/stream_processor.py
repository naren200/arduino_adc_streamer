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
  - The GatingStrategy (gating_strategy.py) selected by ``gating_version`` via
    gating_registry.get_gating: it owns the sample store, windowing state and
    trim policy that turn derived samples into ReadyWindows.

Does NOT own: classification (the caller submits ReadyWindows to whatever
worker/synchronous path it likes), the "which of this tick's windows to
submit" policy (live submits only the newest and drops the rest under load;
replay must submit and wait for every one -- both are caller policy, not
processor policy), or any GUI/plotting state.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np

from .gating_registry import get_gating
from .gating_strategy import (
    DerivedChunk, GateKTunable, GatingParams, GatingStrategy, OnsetSkipTunable, ReadyWindow,
)
from .v1_gating import V1GatingOptions
from .live_channels import DEFAULT_CHANNELS, needs_force

from core.piezo_engine.channel_names import FORCE_COLUMNS
from core.piezo_engine.config import EngineConfig, EngineConfigMismatchError, TimingMode, TimingPolicy
from core.piezo_engine.pipeline import PiezoEnginePipeline, InputConditioning, RAW_INPUT
from core.texture_piezo.gating.v1.window_config import ONSET_SKIP_S
from core.texture_piezo.gating.v1.quality_gate import IdleBaseline
from core.texture_piezo.gating.v2.config import GatingV2Config

# Engine for the live path when the loaded model declares no EngineConfig of its own
# (every hand-crafted-feature runtime): continuous leak timing, no force stage -- those models read none.
# A self-describing model supplies its own config.
LIVE_ENGINE_CONFIG = EngineConfig(timing=TimingPolicy(mode=TimingMode.CONTINUOUS), compute_force=False)


def resolve_live_engine_config(bundle_config: EngineConfig | None = None) -> EngineConfig:
    """Loader hook: the EngineConfig the live path must run for a model bundle -- the
    bundle's own config, or the placeholder ``LIVE_ENGINE_CONFIG`` without one."""
    return LIVE_ENGINE_CONFIG if bundle_config is None else bundle_config


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
            **result.normal_jerk_residue,
        }
        if self.engine_config.compute_force:
            derived["shear_force_lr"] = result.shear_force_lr
            derived["shear_force_tb"] = result.shear_force_tb
            derived["normal_force"] = result.normal_force
        timestamps = np.asarray(timestamps).reshape(-1)
        return dict(result.raw), derived, timestamps[result.dropped_leading.total:]


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
        gating_version: str = "v1",
        gating_v2_config: GatingV2Config | None = None,
    ) -> None:
        self.pzt_columns = list(pzt_columns)
        self.required_channels = tuple(required_channels)
        self.window_size_s = float(window_size_s)
        self.hop_size_s = float(hop_size_s)
        self.span_stale_timeout_s = float(span_stale_timeout_s)
        self.idle_baseline = idle_baseline
        self.onset_skip_s = float(onset_skip_s)
        self.gating_version = gating_version
        self.gating_v2_config = gating_v2_config

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
        self._derived_observer: Callable[[DerivedChunk], None] | None = None
        self.gating: GatingStrategy = get_gating(gating_version)(GatingParams(
            pzt_columns=tuple(self.pzt_columns),
            required_channels=self.required_channels,
            window_size_s=self.window_size_s,
            hop_size_s=self.hop_size_s,
            idle_baseline=self.idle_baseline,
            onset_skip_s=self.onset_skip_s,
            v1_options=self._v1_options(),
            v2_config=gating_v2_config,
        ))

    def set_derived_observer(self, observer: Callable[[DerivedChunk], None] | None) -> None:
        """Hand every non-empty derived chunk to ``observer`` just before gating sees it, so a viewer
        can read the engine channels the replay itself derived; None detaches it."""
        self._derived_observer = observer

    def _v1_options(self) -> V1GatingOptions | None:
        if self.gating_version != "v1":
            return None
        return V1GatingOptions(span_stale_timeout_s=self.span_stale_timeout_s)

    def _restart_windowing_if_timeline_broke(self, timestamps: np.ndarray) -> None:
        """Timestamp-range windowing is only valid on non-decreasing
        timestamps. They restart near zero on a capture restart and wrap
        every ~71.6 min (32-bit microsecond MCU clock), so start the
        windowing state over instead of mis-slicing across the break."""
        went_backwards = self._last_sweep_ts is not None and timestamps[0] < self._last_sweep_ts
        if went_backwards or np.any(np.diff(timestamps) < 0):
            self.gating.reset()
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

    def set_onset_skip_s(self, onset_skip_s: float) -> None:
        """Apply a new onset skip now: to this processor and to live gating state."""
        self.onset_skip_s = float(onset_skip_s)
        if isinstance(self.gating, OnsetSkipTunable):
            self.gating.set_onset_skip_s(self.onset_skip_s)

    def set_gate_k(self, k: float) -> None:
        """Apply a new idle-gate multiplier to live gating state."""
        if isinstance(self.gating, GateKTunable):
            self.gating.set_gate_k(float(k))

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

        chunk = DerivedChunk(channel_samples, derived, timestamps, fs, now_t)
        if self._derived_observer is not None:
            self._derived_observer(chunk)
        return self.gating.push(chunk)
