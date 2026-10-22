"""
Replay Setup
============
Everything that defines how a stream of raw sweeps is gated and windowed, as one value.
Live streaming and "Run on Analysis Source" replay both build their TouchIdStreamProcessor
from a ReplaySetup, so they cannot disagree on the construction. Two setups compare equal
exactly when they would build processors that window identically.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Sequence

import numpy as np

from core.piezo_engine.config import EngineConfig
from core.piezo_engine.pipeline import InputConditioning
from core.texture_piezo.gating.v1.quality_gate import IdleBaseline, fit_idle_baseline
from core.texture_piezo.gating.v2.config import GatingV2Config

from .stream_processor import TouchIdStreamProcessor

BASELINE_FROM_FILE_LABEL = "file-label"
BASELINE_FROM_GLOBAL = "global"
BASELINE_FROM_NONE = "none"
FILE_BASELINE_CLASS = "baseline"

BaselineSource = Literal["file-label", "global", "none"]


def _baseline_fingerprint(baseline: IdleBaseline | None) -> tuple | None:
    """Value identity of a baseline. IdleBaseline is mutable (the gate k is edited live) and its
    mean/std may be lists or arrays, so compare plain tuples of what it holds, not the object."""
    if baseline is None:
        return None
    return (
        tuple(baseline.pzt_columns),
        tuple(np.asarray(baseline.mean, dtype=np.float64).tolist()),
        tuple(np.asarray(baseline.std, dtype=np.float64).tolist()),
        float(baseline.fs), float(baseline.k), float(baseline.captured_duration_s),
    )


@dataclass(frozen=True, eq=False)
class ReplaySetup:
    gating_version: str
    gating_v2_config: GatingV2Config | None
    span_stale_timeout_s: float
    engine_config: EngineConfig
    idle_baseline: IdleBaseline | None
    window_size_s: float
    hop_size_s: float
    onset_skip_s: float
    required_channels: tuple[str, ...]
    pzt_columns: tuple[str, ...]
    input_conditioning: InputConditioning

    def _identity(self) -> tuple:
        return (
            self.gating_version, self.gating_v2_config, self.span_stale_timeout_s, self.engine_config,
            _baseline_fingerprint(self.idle_baseline), self.window_size_s, self.hop_size_s, self.onset_skip_s,
            self.required_channels, self.pzt_columns, self.input_conditioning,
        )

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ReplaySetup) and self._identity() == other._identity()

    __hash__ = None

    @property
    def is_missing_required_baseline(self) -> bool:
        """The gate needs an idle baseline and has none: v1 then windows on a plain fixed grid, v2 waits
        with its idle gate unconstrained."""
        if self.idle_baseline is not None:
            return False
        return self.gating_v2_config is None or self.gating_v2_config.idle_gate_enabled

    def with_detached_baseline(self) -> ReplaySetup:
        """The same setup holding its own copy of the baseline: a live baseline's gate k is edited in
        place, which must not change a setup kept for a worker thread or for later comparison."""
        if self.idle_baseline is None:
            return self
        return replace(self, idle_baseline=replace(self.idle_baseline))

    def new_processor(self, continue_from: TouchIdStreamProcessor | None = None) -> TouchIdStreamProcessor:
        """A fresh processor under this setup. ``continue_from`` carries the previous processor's engine
        state forward when its engine config is the same (the raw stream is unbroken)."""
        processor = TouchIdStreamProcessor(
            pzt_columns=list(self.pzt_columns),
            window_size_s=self.window_size_s,
            hop_size_s=self.hop_size_s,
            span_stale_timeout_s=self.span_stale_timeout_s,
            idle_baseline=self.idle_baseline,
            onset_skip_s=self.onset_skip_s,
            input_conditioning=self.input_conditioning,
            engine_config=self.engine_config,
            required_channels=self.required_channels,
            gating_version=self.gating_version,
            gating_v2_config=self.gating_v2_config,
        )
        if continue_from is not None and continue_from.engine_config == self.engine_config:
            processor.adopt_engine_state_from(continue_from)
        return processor


@dataclass(frozen=True)
class ResolvedBaseline:
    baseline: IdleBaseline | None
    source: BaselineSource


def _label_segment_samples(label_segments: Sequence[dict], snapshot, channel_indices: Sequence[int]) -> np.ndarray | None:
    """The raw samples of the snapshot inside its "baseline"-labeled regions, concatenated; None if there are none."""
    timestamps = snapshot.timestamps_s
    chunks = []
    for segment in label_segments:
        if segment.get("class") != FILE_BASELINE_CLASS:
            continue
        inside = (timestamps >= segment["start_s"]) & (timestamps < segment["end_s"])
        if inside.any():
            chunks.append(snapshot.data[inside][:, list(channel_indices)])
    return np.concatenate(chunks, axis=0) if chunks else None


def resolve_replay_baseline(
    label_segments: Sequence[dict], snapshot, channel_indices: Sequence[int],
    pzt_columns: Sequence[str], fs: float, idle_gate_k: float, global_baseline: IdleBaseline | None,
) -> ResolvedBaseline:
    """The idle baseline a replay of ``snapshot`` runs under: one fit from the file's own
    "baseline"-labeled regions when it has any (its noise floor is the most representative),
    else the live/persisted ``global_baseline``, else none."""
    samples = _label_segment_samples(label_segments, snapshot, channel_indices)
    if samples is not None:
        baseline = fit_idle_baseline(samples, list(pzt_columns), fs, k=idle_gate_k)
        return ResolvedBaseline(baseline, BASELINE_FROM_FILE_LABEL)
    if global_baseline is not None:
        return ResolvedBaseline(global_baseline, BASELINE_FROM_GLOBAL)
    return ResolvedBaseline(None, BASELINE_FROM_NONE)
