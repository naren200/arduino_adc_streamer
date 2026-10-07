"""Composed engine pipeline: median-N -> derived channels + force stage ->
common leading warmup drop.

One implementation serves training (a whole file in one ``process`` call),
offline replay and live streaming (one call per arriving chunk): the output is
bit-identical however the input is chunked. Each stage is a small stateful
class; this module only wires them.

The common warmup (``config.leading_warmup_samples``, the longest window fill)
is dropped from the outputs of EVERY channel so they stay aligned; the force
stage itself integrates from its first sample and is only sliced, never gated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

import numpy as np

from core.piezo_engine.config import EngineConfig
from core.piezo_engine.force_stage import ForceStage, ForceTraces
from core.piezo_engine.median import DEFAULT_MEDIAN_WINDOW_SAMPLES, CausalMedianN
from core.piezo_engine.streaming import CausalDerivedChannels, counts_to_volts


INPUT_CONDITIONING_METADATA_KEY = "analysis_input_conditioning"
_MEDIAN_WINDOW_RECORD_KEY = "median_window_samples"

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InputConditioning:
    """What an upstream loader already did to the samples fed to the pipeline.

    Default is raw ADC counts: the engine applies the median itself. A source
    that was already median-filtered (with window ``median_window_samples``, an
    Analysis snapshot) must say so, or the median would run twice.
    """

    median_window_samples: int | None = None

    @property
    def median_applied(self) -> bool:
        return self.median_window_samples is not None


RAW_INPUT = InputConditioning()
ALREADY_CONDITIONED_INPUT = InputConditioning(median_window_samples=DEFAULT_MEDIAN_WINDOW_SAMPLES)


def conditioning_record(median_window_samples: int | None) -> dict:
    """The plain-dict form a loader stamps into snapshot metadata."""
    return {_MEDIAN_WINDOW_RECORD_KEY: None if median_window_samples is None else int(median_window_samples)}


def input_conditioning_from_record(record: Mapping | None) -> InputConditioning:
    """Build an InputConditioning from a loader's stamp; no stamp means raw."""
    if not isinstance(record, Mapping):
        return RAW_INPUT
    window = record.get(_MEDIAN_WINDOW_RECORD_KEY)
    return InputConditioning(median_window_samples=None if window is None else int(window))


@dataclass(frozen=True)
class DroppedLeading:
    """Leading samples removed from THIS call's input (align timestamps with ``total``)."""

    warmup: int = 0

    @property
    def total(self) -> int:
        return self.warmup


@dataclass(frozen=True)
class PipelineResult:
    raw: Mapping[str, np.ndarray]
    integrated: Mapping[str, np.ndarray]
    shear_jerk_lr: np.ndarray
    shear_jerk_tb: np.ndarray
    normal_jerk: np.ndarray
    normal_force: np.ndarray | None
    shear_force_lr: np.ndarray | None
    shear_force_tb: np.ndarray | None
    dropped_leading: DroppedLeading

    @property
    def n_samples(self) -> int:
        return len(self.shear_jerk_lr)


class MedianStage:
    """Per-column causal median-N over a dict of 1-D chunks."""

    def __init__(self, pzt_columns: list[str], window_samples: int) -> None:
        self._columns = list(pzt_columns)
        self._median = CausalMedianN(window_samples, n_columns=len(self._columns))

    def process(self, chunk_by_column: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
        block = np.stack([np.asarray(chunk_by_column[c], dtype=np.float64).reshape(-1) for c in self._columns], axis=1)
        filtered = self._median.process(block)
        return {col: filtered[:, i] for i, col in enumerate(self._columns)}


class LeadingDropGate:
    """Drops the first ``total`` samples of a stream, across any chunking."""

    def __init__(self) -> None:
        self._remaining: int | None = None

    def arm(self, total: int) -> None:
        """Latch the stream-start drop count; later calls are ignored."""
        if self._remaining is None:
            self._remaining = max(0, int(total))

    def take(self, n_incoming: int) -> int:
        """How many samples to drop from the front of a chunk of ``n_incoming``."""
        dropped = min(int(n_incoming), self._remaining or 0)
        self._remaining = (self._remaining or 0) - dropped
        return dropped


class SampleTimeline:
    """Timestamps of the input stream: explicit ones, or a uniform ``i / fs`` grid.

    The grid index is the GLOBAL input sample count and ``fs`` is latched
    at the first call (a live caller's per-tick rate drifts and would otherwise
    make the grid depend on chunking), so any chunking yields identical times.
    """

    def __init__(self) -> None:
        self._sample_rate_hz: float | None = None
        self._count = 0

    def latch_sample_rate(self, sample_rate_hz: float) -> None:
        """Called on every call: only the first value sticks."""
        if self._sample_rate_hz is None:
            self._sample_rate_hz = float(sample_rate_hz)

    def take(self, n_samples: int, explicit_s: np.ndarray | None) -> np.ndarray:
        first = self._count
        self._count += n_samples
        if explicit_s is not None:
            return explicit_s
        return np.arange(first, first + n_samples, dtype=np.float64) / self._sample_rate_hz


def _slice_chunk(chunk_by_column: Mapping[str, np.ndarray], start: int) -> dict[str, np.ndarray]:
    return {col: np.asarray(values).reshape(-1)[start:] for col, values in chunk_by_column.items()}


class PiezoEnginePipeline:
    """``pipeline.process({col: samples}, sample_rate_hz=fs)`` -> :class:`PipelineResult`.

    With ``config.compute_force`` False no force stage exists and the three force
    outputs of every result are None (never zeros, which would read as real force).
    """

    def __init__(
        self,
        pzt_columns: list[str],
        config: EngineConfig,
        *,
        input_conditioning: InputConditioning = RAW_INPUT,
    ) -> None:
        self.pzt_columns = list(pzt_columns)
        self.config = config
        self.input_conditioning = input_conditioning
        self._warn_if_upstream_median_differs(input_conditioning, config)
        self._median = MedianStage(self.pzt_columns, config.blip_window_samples)
        self.derived_channels = self._new_derived_channels()
        self._force = ForceStage(self.derived_channels.column_map, config) if config.compute_force else None
        self._timeline = SampleTimeline()

    @staticmethod
    def _warn_if_upstream_median_differs(conditioning: InputConditioning, config: EngineConfig) -> None:
        window = conditioning.median_window_samples
        if window is not None and window != config.blip_window_samples:
            _logger.warning(
                "input was median-%d filtered upstream but the engine config expects median-%d",
                window, config.blip_window_samples,
            )

    def _new_derived_channels(self) -> CausalDerivedChannels:
        return CausalDerivedChannels(
            pzt_columns=self.pzt_columns,
            integration_window_samples=self.config.integration_window_samples,
            jerk_window_samples=self.config.jerk_window_samples,
            vref_voltage=self.config.vref_voltage,
        )

    def adopt_state_from(self, other: "PiezoEnginePipeline") -> None:
        """Continue ``other``'s stream: take over EVERY stateful stage (median window,
        derived sums/medians, force stage, force timeline) so nothing restarts cold.

        Only valid between pipelines running the same engine (columns and config);
        ``other`` must not be used afterwards, its stage objects are now shared.
        """
        if other.pzt_columns != self.pzt_columns or other.config != self.config:
            raise ValueError("cannot adopt the state of a pipeline with different columns or engine config")
        self.input_conditioning = other.input_conditioning
        self._median = other._median
        self.derived_channels = other.derived_channels
        self._force = other._force
        self._timeline = other._timeline

    def filter_raw(self, chunk_by_column: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Median stage only (full length, untrimmed): for callers that also plot or
        accumulate the despiked stream. Identity when the source was already filtered."""
        if self.input_conditioning.median_applied:
            return {col: np.asarray(chunk_by_column[col], dtype=np.float64).reshape(-1) for col in self.pzt_columns}
        return self._median.process(chunk_by_column)

    def process(
        self,
        chunk_by_column: Mapping[str, np.ndarray],
        *,
        sample_rate_hz: float,
        timestamps_s: np.ndarray | None = None,
    ) -> PipelineResult:
        """``timestamps_s``, when given, has one strictly increasing entry per INPUT sample;
        default is the uniform grid ``i / fs``."""
        filtered = self.filter_raw(chunk_by_column)
        return self.process_filtered(filtered, sample_rate_hz=sample_rate_hz, timestamps_s=timestamps_s)

    def process_filtered(
        self,
        filtered_by_column: Mapping[str, np.ndarray],
        *,
        sample_rate_hz: float,
        timestamps_s: np.ndarray | None = None,
    ) -> PipelineResult:
        """Derived + force -> warmup on samples that already went through ``filter_raw``."""
        n_input = len(np.asarray(filtered_by_column[self.pzt_columns[0]]).reshape(-1))
        explicit = self._validated_timestamps(timestamps_s, n_input)
        self._timeline.latch_sample_rate(sample_rate_hz)
        kept = _slice_chunk(filtered_by_column, 0)
        kept_times = self._timeline.take(n_input, explicit)
        return self._derive_and_trim(kept, kept_times, sample_rate_hz)

    @staticmethod
    def _validated_timestamps(timestamps_s: np.ndarray | None, n_input: int) -> np.ndarray | None:
        if timestamps_s is None:
            return None
        timestamps = np.asarray(timestamps_s, dtype=np.float64).reshape(-1)
        if timestamps.size != n_input:
            raise ValueError(f"timestamps_s has {timestamps.size} entries for {n_input} input samples")
        return timestamps

    def _force_traces(self, kept: dict, kept_times: np.ndarray) -> ForceTraces | None:
        if self._force is None:
            return None
        volts_by_position = {
            position: counts_to_volts(kept[column], self.config.vref_voltage)
            for position, column in self.derived_channels.column_map.items()
        }
        return self._force.push(volts_by_position, kept_times)

    def _derive_and_trim(self, kept: dict, kept_times: np.ndarray, sample_rate_hz: float) -> PipelineResult:
        derived_channels = self.derived_channels
        seen_before = derived_channels.samples_seen
        derived = derived_channels.process(kept, sample_rate_hz=sample_rate_hz)
        force = self._force_traces(kept, kept_times)
        n_kept = len(derived["shear_jerk_lr"])
        warmup_dropped = min(n_kept, max(0, self.config.leading_warmup_samples - seen_before))
        dropped = DroppedLeading(warmup=warmup_dropped)
        return PipelineResult(
            raw=MappingProxyType(_slice_chunk(kept, warmup_dropped)),
            integrated=MappingProxyType(_slice_chunk(derived["integrated"], warmup_dropped)),
            shear_jerk_lr=derived["shear_jerk_lr"][warmup_dropped:],
            shear_jerk_tb=derived["shear_jerk_tb"][warmup_dropped:],
            normal_jerk=derived["normal_jerk"][warmup_dropped:],
            normal_force=None if force is None else force.normal_force[warmup_dropped:],
            shear_force_lr=None if force is None else force.shear_force_lr[warmup_dropped:],
            shear_force_tb=None if force is None else force.shear_force_tb[warmup_dropped:],
            dropped_leading=dropped,
        )
