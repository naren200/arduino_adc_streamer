"""Per-ADC-input PZT sampling-timing context derived from the MUX timing model.

This module contains no Qt or serial code.  The Force Display block worker uses
it to place each repeat of a burst on the physical acquisition timeline.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

import numpy as np

from data_processing.adc_mux_timing import AdcMuxTiming, adc_mux_timing_log


@dataclass(frozen=True)
class PztDecayTimingContext:
    timing_source: str
    physical_adc_input: int
    sensor_connected_s: float
    pre_sample_decay_s: float
    post_sample_connected_s: float
    repeat_count: int = 1
    pair_loop_interval_s: float = 0.0
    # From sweep/sequence start to the centre of the selected IADC observation.
    effective_sample_offset_s: float = 0.0
    mean_wall_sample_interval_s: float = 0.0
    median_wall_sample_interval_s: float = 0.0
    wall_sample_interval_std_s: float = 0.0
    mean_disconnected_s: float = 0.0
    median_disconnected_s: float = 0.0
    timing_consistency_valid: bool = True
    timing_consistency_errors: tuple[str, ...] = ()
    calculated_signal_sequence_s: float = 0.0
    calculated_ground_phase_s: float = 0.0
    calculated_complete_sequence_s: float = 0.0
    adc_mux_timing: AdcMuxTiming | None = None

    @classmethod
    def from_adc_mux_timing(cls, timing: AdcMuxTiming, physical_adc_input: int) -> "PztDecayTimingContext":
        if physical_adc_input == 1:
            pre, post = timing.t_decay_before_effective_sample_ch1_s, timing.t_connected_after_effective_sample_ch1_s
            effective_offset = timing.first_sample_effective_from_sequence_us * 1e-6
        elif physical_adc_input == 2:
            pre, post = timing.t_decay_before_effective_sample_ch2_s, timing.t_connected_after_effective_sample_ch2_s
            effective_offset = timing.second_sample_effective_from_sequence_us * 1e-6
        else:
            raise ValueError("physical_adc_input must be 1 or 2")
        return cls(
            timing_source="calculated_adc_mux_plus_measured_timestamps",
            physical_adc_input=physical_adc_input,
            sensor_connected_s=timing.sensor_connected_s,
            pre_sample_decay_s=pre,
            post_sample_connected_s=post,
            repeat_count=timing.repeat_count,
            pair_loop_interval_s=timing.t_pair_loop_total_us * 1e-6,
            effective_sample_offset_s=effective_offset,
            calculated_signal_sequence_s=timing.signal_sequence_us * 1e-6,
            calculated_ground_phase_s=timing.ground_phase_us * 1e-6,
            calculated_complete_sequence_s=timing.complete_sequence_us * 1e-6,
            adc_mux_timing=timing,
        )

    def with_sample_intervals(
        self, wall_intervals: Sequence[float], connected_intervals: Sequence[float]
    ) -> "PztDecayTimingContext":
        """Attach measured wall and interval-specific connected exposure data."""
        if len(wall_intervals) != len(connected_intervals):
            raise ValueError("wall and connected interval counts must match")
        walls = np.asarray(wall_intervals, dtype=float)
        connected = np.asarray(connected_intervals, dtype=float)
        finite = np.isfinite(walls) & np.isfinite(connected) & (walls >= 0.0) & (connected >= 0.0)
        values, connected = walls[finite], connected[finite]
        errors: list[str] = []
        if np.any(connected > values + 1e-9):
            errors.append("connected exposure exceeds measured wall interval")
        disconnected = np.maximum(0.0, values - connected)
        return PztDecayTimingContext(
            **{**asdict(self), "adc_mux_timing": self.adc_mux_timing,
               "mean_wall_sample_interval_s": float(np.mean(values)) if len(values) else 0.0,
               "median_wall_sample_interval_s": float(np.median(values)) if len(values) else 0.0,
               "wall_sample_interval_std_s": float(np.std(values)) if len(values) else 0.0,
               "mean_disconnected_s": float(np.mean(disconnected)) if len(disconnected) else 0.0,
               "median_disconnected_s": float(np.median(disconnected)) if len(disconnected) else 0.0,
               "timing_consistency_valid": self.timing_consistency_valid and not errors,
               "timing_consistency_errors": tuple(dict.fromkeys((*self.timing_consistency_errors, *errors)))})

    def export_dict(self) -> dict:
        payload = asdict(self)
        payload["adc_mux_timing"] = adc_mux_timing_log(self.adc_mux_timing)
        return payload

    def observation_offset_s(self, repeat_index: int) -> float:
        """Time from the sweep start to one CH1/CH2 effective sample."""
        if repeat_index < 0 or repeat_index >= self.repeat_count:
            raise ValueError("repeat index is outside the configured burst")
        return self.effective_sample_offset_s + repeat_index * self.pair_loop_interval_s

    def connected_exposure_between(self, previous_repeat: int | None, current_repeat: int) -> float:
        """Connected exposure between two effective observations of this input."""
        if previous_repeat is None:
            return 0.0
        if current_repeat == previous_repeat + 1:
            return self.pair_loop_interval_s
        if self.adc_mux_timing is not None:
            return (
                self.adc_mux_timing.connected_after_effective_sample_s(
                    adc_input=self.physical_adc_input, repeat_index=previous_repeat
                )
                + self.adc_mux_timing.decay_before_effective_sample_s(
                    adc_input=self.physical_adc_input, repeat_index=current_repeat
                )
            )
        return self.sensor_connected_s

    def connected_exposure_for_transition(
        self, previous: Any | None, *, current_burst_index: int,
        current_repeat_index: int,
    ) -> float | None:
        """Return the physical exposure between two valid retained samples.

        ``previous`` is any object exposing ``burst_index`` and ``repeat_index``.
        ``None`` deliberately represents an invalid/dropped/reordered
        transition; callers must not invent a duration for it.
        """
        if previous is None:
            return 0.0
        if current_burst_index == previous.burst_index:
            if current_repeat_index == previous.repeat_index + 1:
                return self.pair_loop_interval_s
            return None
        if current_burst_index == previous.burst_index + 1:
            if previous.repeat_index == self.repeat_count - 1 and current_repeat_index == 0:
                return self.connected_exposure_between(previous.repeat_index, current_repeat_index)
            return None
        return None
