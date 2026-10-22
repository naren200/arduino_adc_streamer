"""Gating v2 preview for the Analysis tab: runs live's own ``V2Gating`` over a capture.

Nothing here re-implements a gate stage or the queue. The capture is fed to ``V2Gating`` in
hop-sized chunks the way replay does, and every number shown is read back from it: each
stage's good pieces and the combined pieces through its ``stage_observer`` hook, and the
accepted samples from the windows it emitted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

import numpy as np

from core.texture_piezo.application.gating_strategy import DerivedChunk, GatingParams
from core.texture_piezo.application.v2_gating import NORMAL_JERK_CHANNEL, V2Gating
from core.texture_piezo.gating.v1.quality_gate import IdleBaseline
from core.texture_piezo.gating.v2.config import GatingV2Config
from data_processing.analysis_workbench import AnalysisSourceSnapshot
from data_processing.burst_score import (
    DEFAULT_BURST_BLOCK_S,
    DEFAULT_BURST_MIN,
    DEFAULT_BURST_SUSTAIN_BLOCKS,
    DEFAULT_BURST_SUSTAIN_MIN,
    BurstScoreSeries,
    BurstScoreSettings,
    EngineView,
    compute_burst_score_series,
    derive_engine_view,
)

# Provisional starting value; expiry_s must be >= the live window size.
DEFAULT_EXPIRY_S = 3.0

XRange = tuple[float, float]


@dataclass(frozen=True)
class GatePreviewSettings(BurstScoreSettings):
    """The tunable ``GatingV2Config`` fields; window size, hop and onset skip stay the live TouchID values."""

    strength_enabled: bool = True
    idle_enabled: bool = True
    concat_enabled: bool = True
    expiry_s: float = DEFAULT_EXPIRY_S

    def to_config(self) -> GatingV2Config:
        return GatingV2Config(
            idle_gate_enabled=self.idle_enabled, strength_gate_enabled=self.strength_enabled,
            concat_enabled=self.concat_enabled, burst_min=self.burst_min, sustain_min=self.sustain_min,
            block_s=self.block_s, sustain_blocks=self.sustain_blocks, expiry_s=self.expiry_s,
        )


DEFAULT_GATE_PREVIEW_SETTINGS = asdict(GatePreviewSettings())
assert DEFAULT_GATE_PREVIEW_SETTINGS["block_s"] == DEFAULT_BURST_BLOCK_S
assert DEFAULT_GATE_PREVIEW_SETTINGS["burst_min"] == DEFAULT_BURST_MIN
assert DEFAULT_GATE_PREVIEW_SETTINGS["sustain_min"] == DEFAULT_BURST_SUSTAIN_MIN
assert DEFAULT_GATE_PREVIEW_SETTINGS["sustain_blocks"] == DEFAULT_BURST_SUSTAIN_BLOCKS


@dataclass(frozen=True)
class LiveGateParams:
    """Values that stay the live TouchID tab's: not editable in the preview."""

    window_size_s: float
    hop_size_s: float
    onset_skip_s: float
    idle_baseline: IdleBaseline | None


@dataclass(frozen=True)
class GatePreview:
    """Good ranges in plot x units, lane by lane, plus the plain-language logic and result."""

    strength: list[XRange]
    idle: list[XRange]
    combined: list[XRange]
    accepted: list[XRange]
    logic_text: str
    error: str = ""
    accepted_seconds: tuple[XRange, ...] = ()  # accepted ranges in capture seconds, for the label overlap


@dataclass(frozen=True)
class GatingPreviewData:
    """Everything the Analysis tab draws for the preview; computed off the GUI thread."""

    burst: BurstScoreSeries | None  # None when the strength gate is off
    preview: GatePreview
    x: np.ndarray


def compute_gating_preview(
    snapshot: AnalysisSourceSnapshot, *, vref_voltage: float, axis_mode: str,
    settings: GatePreviewSettings, live: LiveGateParams, label_spans: tuple[XRange, ...] = (),
) -> GatingPreviewData:
    view = derive_engine_view(snapshot, vref_voltage, axis_mode)
    if view is None:
        missing = GatePreview([], [], [], [], "", error="Needs the five C/L/R/T/B channels.")
        return GatingPreviewData(None, missing, np.empty(0))
    fs = float(snapshot.sample_rate_hz)
    burst = compute_burst_score_series(view.normal_jerk, view.x, fs, settings) if settings.strength_enabled else None
    preview = run_gate_preview(view, fs, settings, live)
    return GatingPreviewData(burst, _with_label_overlap(preview, snapshot, label_spans), view.x)


def run_gate_preview(
    view: EngineView, fs: float, settings: GatePreviewSettings, live: LiveGateParams,
) -> GatePreview:
    config = settings.to_config()
    logic = describe_gate_logic(settings, live)
    params = GatingParams(
        pzt_columns=view.columns, required_channels=(NORMAL_JERK_CHANNEL,), window_size_s=live.window_size_s,
        hop_size_s=live.hop_size_s, idle_baseline=live.idle_baseline, onset_skip_s=live.onset_skip_s,
        v2_config=config,
    )
    try:
        gating = V2Gating(params)
    except ValueError as exc:
        return GatePreview([], [], [], [], logic, error=str(exc))
    stage_names = [name for name, is_on in (("strength", config.strength_gate_enabled), ("idle", gating._idle is not None)) if is_on]
    stage_ranges: dict[str, list[tuple[int, int]]] = {name: [] for name in stage_names}
    combined: list[tuple[int, int]] = []

    def observe(pieces, combined_pieces):
        for name, stage_pieces in zip(stage_names, pieces):
            stage_ranges[name].extend(stage_pieces)
        combined.extend(combined_pieces)

    gating.stage_observer = observe
    accepted, window_count = _feed_capture(gating, view, fs, live)
    to_x = _index_to_x(view.x)
    total_s = float(view.timestamps_s[-1] - view.timestamps_s[0]) if len(view.timestamps_s) > 1 else 0.0
    accepted_s = sum(float(view.timestamps_s[min(end, len(view.timestamps_s)) - 1] - view.timestamps_s[start])
                     for start, end in accepted)
    result = _result_line(accepted_s, total_s, window_count, gating.idle_gate_waiting_for_baseline)
    return GatePreview(
        strength=[to_x(r) for r in stage_ranges.get("strength", [])],
        idle=[to_x(r) for r in stage_ranges.get("idle", [])],
        combined=[to_x(r) for r in combined],
        accepted=[to_x(r) for r in accepted],
        logic_text=logic + "\n" + result,
        accepted_seconds=tuple(
            (float(view.timestamps_s[start]), float(view.timestamps_s[min(end, len(view.timestamps_s)) - 1]))
            for start, end in accepted),
    )


def _feed_capture(gating: V2Gating, view: EngineView, fs: float, live: LiveGateParams):
    """Hop-sized chunks, like replay; returns the merged accepted index ranges and the window count."""
    hop_n = max(1, round(live.hop_size_s * fs))
    normal_jerk = view.normal_jerk
    used: list[tuple[int, int]] = []
    window_count = 0
    for start in range(0, len(normal_jerk), hop_n):
        end = min(start + hop_n, len(normal_jerk))
        chunk = DerivedChunk(
            channel_samples={name: np.asarray(view.raw[name])[start:end] for name in view.columns},
            derived=_slice_derived(view.derived, start, end),
            timestamps=view.timestamps_s[start:end], fs=fs, now_t=float(view.timestamps_s[end - 1]),
        )
        for window in gating.push(chunk):
            used.extend(window.ranges)
            window_count += 1
    return merge_ranges(used), window_count


def _slice_derived(derived: dict, start: int, end: int) -> dict:
    """One chunk of the engine's derived dict (``integrated`` is nested by sensor column)."""
    return {
        name: {column: values[start:end] for column, values in channel.items()} if isinstance(channel, dict)
        else np.asarray(channel)[start:end]
        for name, channel in derived.items()
    }


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _index_to_x(x: np.ndarray):
    def convert(index_range: tuple[int, int]) -> XRange:
        start, end = index_range
        return float(x[start]), float(x[min(end, len(x)) - 1])

    return convert


def _with_label_overlap(preview: GatePreview, snapshot, label_spans: tuple[XRange, ...]) -> GatePreview:
    if not label_spans or preview.error:
        return preview
    origin_s = float(snapshot.timestamps_s[0])
    accepted = [(start - origin_s, end - origin_s) for start, end in preview.accepted_seconds]
    return replace(preview, logic_text=preview.logic_text + "\n" + _label_overlap_line(accepted, list(label_spans)))


def span_overlap_s(first: list[XRange], second: list[XRange]) -> float:
    """Seconds covered by both span lists (each non-overlapping within itself)."""
    first, second = sorted(first), sorted(second)
    total, i, j = 0.0, 0, 0
    while i < len(first) and j < len(second):
        total += max(0.0, min(first[i][1], second[j][1]) - max(first[i][0], second[j][0]))
        if first[i][1] < second[j][1]:
            i += 1
        else:
            j += 1
    return total


def _label_overlap_line(accepted: list[XRange], labels: list[XRange]) -> str:
    accepted_s = sum(end - start for start, end in accepted)
    labelled_s = sum(end - start for start, end in labels)
    overlap_s = span_overlap_s(accepted, labels)
    unlabelled_s = accepted_s - overlap_s
    labelled_share = 100.0 * overlap_s / labelled_s if labelled_s > 0 else 0.0
    unlabelled_share = 100.0 * unlabelled_s / accepted_s if accepted_s > 0 else 0.0
    return (f"Hand labels: {labelled_s:.1f} s labelled, {labelled_share:.0f}% of it accepted; "
            f"{unlabelled_s:.1f} s ({unlabelled_share:.0f}%) of the accepted signal is unlabelled.")

def _result_line(accepted_s: float, total_s: float, window_count: int, is_waiting_for_baseline: bool) -> str:
    share = 100.0 * accepted_s / total_s if total_s > 0 else 0.0
    line = f"Result: {accepted_s:.1f} s of {total_s:.1f} s accepted ({share:.0f}%) in {window_count} windows."
    if is_waiting_for_baseline:
        line += "\nIdle gate is enabled but TouchID has no idle baseline yet, so it constrains nothing."
    return line


def describe_gate_logic(settings: GatePreviewSettings, live: LiveGateParams) -> str:
    stages = []
    if settings.strength_enabled:
        stages.append("strength")
    if settings.idle_enabled:
        stages.append("idle")
    lines = [
        "Final gate: " + (" AND ".join(stages) if stages else "no stage (whole stream)")
        + f" -> drop first {live.onset_skip_s:g} s of every new range"
        + f" -> queue: window {live.window_size_s:g} s every {live.hop_size_s:g} s,"
        + f" expiry {settings.expiry_s:g} s, concat {'on' if settings.concat_enabled else 'off'}",
    ]
    if settings.strength_enabled:
        lines.append(
            f"Strength: block of {settings.block_s:g} s is strong when its sum of |normal_jerk| > {settings.burst_min:g}; "
            f"good when more than {settings.sustain_min} of the last {settings.sustain_blocks} blocks are strong."
        )
    if settings.idle_enabled:
        lines.append("Idle: a micro-chunk is active when a raw sample leaves the idle baseline's k-sigma band.")
    return "\n".join(lines)
