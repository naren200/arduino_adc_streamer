"""Block burst scores for the Analysis tab, using the live gating-v2 strength stage itself.

Nothing here re-implements the gate: scores come from ``block_burst_scores`` and the good
stretches from running ``StrongSignalStage`` over the whole series, so what the plot shows
is what live TouchID gating v2 would decide for the same parameters.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from core.piezo_engine.channel_names import CHANNEL_LABELS
from core.piezo_engine.pipeline import INPUT_CONDITIONING_METADATA_KEY, input_conditioning_from_record
from core.texture_piezo.application.stream_processor import LIVE_ENGINE_CONFIG, DerivedChannelPipeline
from core.texture_piezo.gating.v2.config import GatingV2Config
from core.texture_piezo.gating.v2.strong_signal import (
    StrongSignalStage,
    block_burst_scores,
    block_length_samples,
    is_strong_block,
)
from data_processing.analysis_workbench import (
    AnalysisSourceSnapshot,
    _resolve_shear_position_channels_and_volts,
    build_trace_x_axis,
)

# Starting values (the user's pick, mirrored in texture_piezo configs/config.yaml `gating_v2`); the Analysis settings let the user try others.
DEFAULT_BURST_BLOCK_S = 0.04
DEFAULT_BURST_MIN = 0.3
DEFAULT_BURST_SUSTAIN_MIN = 3
DEFAULT_BURST_SUSTAIN_BLOCKS = 7
_UNUSED_EXPIRY_S = 1.0  # required by GatingV2Config, irrelevant to the strength stage


@dataclass(frozen=True)
class BurstScoreSettings:
    block_s: float = DEFAULT_BURST_BLOCK_S
    burst_min: float = DEFAULT_BURST_MIN
    sustain_min: int = DEFAULT_BURST_SUSTAIN_MIN
    sustain_blocks: int = DEFAULT_BURST_SUSTAIN_BLOCKS


@dataclass(frozen=True)
class BurstScoreSeries:
    """Per-block geometry in x units plus the stretches the gate would call good."""

    block_x0: np.ndarray
    block_x1: np.ndarray
    scores: np.ndarray
    is_strong: np.ndarray
    burst_min: float
    rolling_sum: np.ndarray  # sum of |normal_jerk| over the trailing block_s at every sample, aligned with x
    good_x_ranges: list[tuple[float, float]]


def trailing_abs_sum(normal_jerk: np.ndarray, window_n: int) -> np.ndarray:
    """Sum of |normal_jerk| over the last ``window_n`` samples, at every sample (shorter at the start)."""
    cumulative = np.cumsum(np.abs(np.asarray(normal_jerk, dtype=np.float64)))
    trailing = cumulative.copy()
    trailing[window_n:] -= cumulative[:-window_n]
    return trailing


def compute_burst_score_series(
    normal_jerk: np.ndarray, x: np.ndarray, fs: float, settings: BurstScoreSettings,
) -> BurstScoreSeries:
    """``x`` is the axis of ``normal_jerk`` (same length), in whatever units the plot uses."""
    block_n = block_length_samples(settings.block_s, fs)
    scores = block_burst_scores(np.asarray(normal_jerk, dtype=np.float64), block_n)
    config = GatingV2Config(
        idle_gate_enabled=False, strength_gate_enabled=True, concat_enabled=False,
        burst_min=settings.burst_min, sustain_min=settings.sustain_min, block_s=settings.block_s,
        sustain_blocks=settings.sustain_blocks, expiry_s=_UNUSED_EXPIRY_S,
    )
    good = StrongSignalStage(config).feed(np.asarray(normal_jerk, dtype=np.float64), fs)
    starts = np.arange(len(scores)) * block_n
    ends = np.minimum(starts + block_n, len(x)) - 1
    return BurstScoreSeries(
        block_x0=np.asarray(x)[starts],
        block_x1=np.asarray(x)[ends],
        scores=scores,
        is_strong=is_strong_block(scores, config.burst_min),
        burst_min=config.burst_min,
        rolling_sum=trailing_abs_sum(normal_jerk, block_n),
        good_x_ranges=[(float(x[start]), float(x[min(end, len(x)) - 1])) for start, end in good],
    )


@dataclass(frozen=True)
class EngineView:
    """What the live path derives from a capture, after the engine's warmup drop: the despiked
    raw columns, the engine channels, and the time/x axes aligned with them."""

    columns: tuple[str, ...]
    raw: dict
    derived: dict
    timestamps_s: np.ndarray
    x: np.ndarray

    @property
    def normal_jerk(self) -> np.ndarray:
        return np.asarray(self.derived["normal_jerk"], dtype=np.float64)


def derive_engine_view(snapshot: AnalysisSourceSnapshot, vref_voltage: float, axis_mode: str) -> EngineView | None:
    """Run the live ``DerivedChannelPipeline`` under the live engine config (only the reference
    voltage follows the Analysis setting) over the whole capture; None without C/L/R/T/B.
    The snapshot's loader already applied the median-N despike and says so, so it is not repeated."""
    resolved = _resolve_shear_position_channels_and_volts(
        snapshot, np.asarray(snapshot.data, dtype=np.float32), vref_voltage,
    )
    if resolved is None:
        return None
    position_channels, _volts = resolved
    columns = [position_channels[position] for position in CHANNEL_LABELS]
    names = tuple(label for _index, label in columns)
    counts = {label: np.asarray(snapshot.data[:, index], dtype=np.float64) for index, label in columns}
    pipeline = DerivedChannelPipeline(
        list(names),
        replace(LIVE_ENGINE_CONFIG, vref_voltage=vref_voltage),
        input_conditioning=input_conditioning_from_record(snapshot.metadata.get(INPUT_CONDITIONING_METADATA_KEY)),
    )
    fs = float(snapshot.sample_rate_hz)
    despiked = pipeline.filter_raw(counts)
    raw, derived, timestamps = pipeline.process(despiked, np.asarray(snapshot.timestamps_s, dtype=np.float64), fs)
    x_matrix, _label, _units = build_trace_x_axis(snapshot, axis_mode)
    x_full = x_matrix[:, 0] if x_matrix.size else np.empty(0, dtype=np.float64)
    return EngineView(names, raw, derived, timestamps, x_full[len(x_full) - len(timestamps):])
