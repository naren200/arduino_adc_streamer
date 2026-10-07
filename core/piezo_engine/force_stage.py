"""Shear/Normal Force stage: the incremental, chunk-invariant form of the
Analysis tab's force path.

Per position (C/L/R/T/B): causal expanding-median centering -> RC-charge force
RATE (:func:`compute_pzt_force_rate_series` semantics, per-position
capacitance, MUX leak timing) -> shear/normal combine on the five rates (shear
detected from a trailing average of the outer rates, normal = raw rate minus
that shear) -> three independent :class:`PztForceChannelIntegrator` (center ->
normal, L -> shear L/R, T -> shear T/B; ``accumulate_raw=True``).

Every stage carries its state across ``push`` calls, so one call over a whole
file equals any chunking of it bit for bit. The stage integrates from the first
sample it receives: leading-warmup trimming is the pipeline's business.

The numeric recipe is the Analysis tab's former batch force path (removed in
P2b, frozen as reference arrays under tests/fixtures/golden); the Analysis tab now
calls this stage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np

from core.piezo_engine.baseline import IncrementalMedian
from core.piezo_engine.config import ROLE_NORMAL, ROLE_SHEAR, EngineConfig, ForceSettings
from core.piezo_engine.force_integrator import (
    PztChannelPhysicalParams,
    PztForceChannelIntegrator,
    compute_pzt_force_rate_series,
)
from core.piezo_engine.shear_constants import (
    NORMAL_FORCE_SENSOR_COUNT,
    SHEAR_OUTER_SENSOR_POSITIONS,
    SHEAR_POSITION_BOTTOM,
    SHEAR_POSITION_CENTER,
    SHEAR_POSITION_LEFT,
    SHEAR_POSITION_RIGHT,
    SHEAR_POSITION_TOP,
    SHEAR_SENSOR_POSITIONS,
    SHEAR_ZERO_VALUE,
)

_SHEAR_LR_INTEGRATOR_POSITION = SHEAR_POSITION_LEFT
_SHEAR_TB_INTEGRATOR_POSITION = SHEAR_POSITION_TOP


@dataclass(frozen=True)
class ForceTraces:
    normal_force: np.ndarray
    shear_force_lr: np.ndarray
    shear_force_tb: np.ndarray

    @property
    def n_samples(self) -> int:
        return len(self.normal_force)


def _as_series(values) -> np.ndarray:
    return np.asarray(values, dtype=np.float64).reshape(-1)


class ExpandingMedianCentering:
    """Per-position ``volts - causal expanding median(volts)``."""

    def __init__(self, positions: tuple[str, ...] = SHEAR_SENSOR_POSITIONS) -> None:
        self._medians = {position: IncrementalMedian() for position in positions}

    def push(self, volts_by_position: Mapping[str, np.ndarray]) -> dict[str, np.ndarray]:
        centered = {}
        for position, median in self._medians.items():
            volts = _as_series(volts_by_position[position])
            centered[position] = volts - median.push_many(volts)
        return centered


class ForceRateStage:
    """Incremental :func:`compute_pzt_force_rate_series` for all positions.

    Carries the previous centered voltage per position and the previous
    timestamp, so the first sample of the stream has rate 0.0 and every later
    sample (also the first of a chunk) sees its true predecessor.
    """

    def __init__(
        self,
        params_by_position: Mapping[str, PztChannelPhysicalParams],
        leak_dt_s: float | None,
        pre_sample_decay_s_by_position: Mapping[str, float],
    ) -> None:
        self._params = dict(params_by_position)
        self._leak_dt_s = leak_dt_s
        self._decay = dict(pre_sample_decay_s_by_position)
        self._previous_voltage: dict[str, float] | None = None
        self._previous_time: float | None = None

    def push(self, centered_by_position: Mapping[str, np.ndarray], times_s: np.ndarray) -> dict[str, np.ndarray]:
        times = _as_series(times_s)
        rates = {
            position: self._rate(position, _as_series(centered_by_position[position]), times)
            for position in self._params
        }
        self._previous_voltage = {p: float(_as_series(centered_by_position[p])[-1]) for p in self._params}
        self._previous_time = float(times[-1])
        return rates

    def _rate(self, position: str, centered: np.ndarray, times: np.ndarray) -> np.ndarray:
        has_previous = self._previous_voltage is not None
        if has_previous:
            centered = np.concatenate([[self._previous_voltage[position]], centered])
            times = np.concatenate([[self._previous_time], times])
        rate = compute_pzt_force_rate_series(
            centered,
            times,
            self._params[position],
            leak_dt_s=self._leak_dt_s,
            pre_sample_decay_dt_s=self._decay.get(position),
        )
        return rate[1:] if has_previous else rate


class TrailingWindowSum:
    """Incremental moving rectangular sum over up to ``window`` past samples: a partial
    window at the stream start, so the output has one value per input sample.

    The sum is a difference of GLOBAL prefix sums, so this carries the last ``window``
    prefix values and extends them with a sequential ``np.cumsum`` seeded by the carried
    prefix -- a ring-buffer running total would round differently, and the Analysis tab's
    plotted values (frozen goldens) are defined by this form.
    """

    def __init__(self, window_samples: int) -> None:
        self._window = int(window_samples)
        self._prefix_tail = np.zeros(1, dtype=np.float64)
        self._sample_count = 0

    def push(self, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(window sums, number of samples each sum covers) for the new samples."""
        values = _as_series(values)
        first = self._sample_count
        new_prefix = np.cumsum(np.concatenate([self._prefix_tail[-1:], values]))
        prefix = np.concatenate([self._prefix_tail[:-1], new_prefix])
        prefix_base = first - (self._prefix_tail.size - 1)
        end_index = np.arange(first, first + values.size)
        start_index = np.maximum(0, end_index - self._window + 1)
        window_sum = prefix[end_index + 1 - prefix_base] - prefix[start_index - prefix_base]
        self._prefix_tail = prefix[-self._window:]
        self._sample_count += values.size
        return window_sum, (end_index - start_index + 1).astype(np.float64)


class TrailingMeanStage:
    """Causal trailing average over up to ``window`` past samples (partial window at the start)."""

    def __init__(self, window_samples: int) -> None:
        self._sums = TrailingWindowSum(window_samples)

    def push(self, values: np.ndarray) -> np.ndarray:
        window_sum, counts = self._sums.push(values)
        return window_sum / counts


def _opposite_sign_pair_component(first: np.ndarray, second: np.ndarray, sign_source: np.ndarray) -> np.ndarray:
    """``copysign(min(|first|, |second|), sign_source)`` where the pair has opposite signs, else 0."""
    is_pair = (first != SHEAR_ZERO_VALUE) & (second != SHEAR_ZERO_VALUE) & (np.signbit(first) != np.signbit(second))
    magnitude = np.copysign(np.minimum(np.abs(first), np.abs(second)), sign_source)
    return np.where(is_pair, magnitude, SHEAR_ZERO_VALUE)


def _normal_force_total(residual: Mapping[str, np.ndarray]) -> np.ndarray:
    """Vectorised ``NormalForceCalculator.compute(residual).total_force``."""
    outer = [residual[position] for position in SHEAR_OUTER_SENSOR_POSITIONS]
    is_compression, is_tension = _force_type_masks(residual[SHEAR_POSITION_CENTER], outer)
    outer_min = np.minimum(np.minimum(outer[0], outer[1]), np.minimum(outer[2], outer[3]))
    outer_max = np.maximum(np.maximum(outer[0], outer[1]), np.maximum(outer[2], outer[3]))
    offset = np.where(
        is_compression, np.maximum(SHEAR_ZERO_VALUE, outer_min),
        np.where(is_tension, np.minimum(SHEAR_ZERO_VALUE, outer_max), SHEAR_ZERO_VALUE),
    )
    normalized = [residual[position] - offset for position in SHEAR_SENSOR_POSITIONS]
    return _compensated_sum(normalized) + float(NORMAL_FORCE_SENSOR_COUNT) * offset


def _force_type_masks(center: np.ndarray, outer: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """(is_compression, is_tension): center sign, else outer majority, else outer magnitude."""
    positive_count = sum((values > SHEAR_ZERO_VALUE).astype(np.int64) for values in outer)
    negative_count = sum((values < SHEAR_ZERO_VALUE).astype(np.int64) for values in outer)
    positive_magnitude = _compensated_sum([np.where(v > SHEAR_ZERO_VALUE, v, SHEAR_ZERO_VALUE) for v in outer])
    negative_magnitude = _compensated_sum([np.where(v < SHEAR_ZERO_VALUE, -v, SHEAR_ZERO_VALUE) for v in outer])
    tie = positive_count == negative_count
    inferred_compression = (positive_count > negative_count) | (tie & (positive_magnitude > negative_magnitude))
    inferred_tension = (negative_count > positive_count) | (tie & (negative_magnitude > positive_magnitude))
    center_is_zero = ~(center > SHEAR_ZERO_VALUE) & ~(center < SHEAR_ZERO_VALUE)
    is_compression = (center > SHEAR_ZERO_VALUE) | (center_is_zero & inferred_compression)
    is_tension = (center < SHEAR_ZERO_VALUE) | (center_is_zero & inferred_tension)
    return is_compression, is_tension


def _compensated_sum(terms: list[np.ndarray]) -> np.ndarray:
    """Elementwise Neumaier-compensated left-to-right sum, as ``sum()`` of floats
    does since CPython 3.12 (the Analysis reference code uses ``sum``). Spelled out
    so the engine's numbers do not depend on the interpreter version; ``np.sum``
    would reorder and round differently.
    """
    total = np.zeros_like(terms[0])
    compensation = np.zeros_like(total)
    for term in terms:
        running = total + term
        compensation += np.where(np.abs(total) >= np.abs(term), (total - running) + term, (term - running) + total)
        total = running
    return np.where((compensation != 0.0) & np.isfinite(compensation), total + compensation, total)


def combine_force_rates(
    raw_rate_by_position: Mapping[str, np.ndarray], smoothed_rate_by_position: Mapping[str, np.ndarray],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorised per-sample shear detect + normal total: (shear_lr, shear_tb, normal) rate series.

    Shear is detected on the SMOOTHED outer rates; the normal total is computed on
    the RAW rates minus that shear, so normal stays instantaneous.
    """
    smoothed = smoothed_rate_by_position
    b_lr = _opposite_sign_pair_component(
        smoothed[SHEAR_POSITION_LEFT], smoothed[SHEAR_POSITION_RIGHT], smoothed[SHEAR_POSITION_RIGHT])
    b_tb = _opposite_sign_pair_component(
        smoothed[SHEAR_POSITION_TOP], smoothed[SHEAR_POSITION_BOTTOM], smoothed[SHEAR_POSITION_TOP])
    raw = raw_rate_by_position
    residual = {
        SHEAR_POSITION_CENTER: raw[SHEAR_POSITION_CENTER] - SHEAR_ZERO_VALUE,
        SHEAR_POSITION_LEFT: raw[SHEAR_POSITION_LEFT] - (-b_lr),
        SHEAR_POSITION_RIGHT: raw[SHEAR_POSITION_RIGHT] - b_lr,
        SHEAR_POSITION_TOP: raw[SHEAR_POSITION_TOP] - b_tb,
        SHEAR_POSITION_BOTTOM: raw[SHEAR_POSITION_BOTTOM] - (-b_tb),
    }
    return b_lr, b_tb, _normal_force_total(residual)


def new_role_integrator(settings: ForceSettings, sensor_position: str) -> PztForceChannelIntegrator:
    """One independent accumulate-raw integrator; role (and capacitance) follow the position."""
    role = ROLE_NORMAL if sensor_position == SHEAR_POSITION_CENTER else ROLE_SHEAR
    thresholds = settings.role_thresholds(role)
    params = settings.physical_params(sensor_position)
    return PztForceChannelIntegrator(
        capacitance_f=params.capacitance_f,
        rleak_ohm=params.rleak_ohm,
        d33_c_per_n=params.d33_c_per_n,
        noise_threshold_v=thresholds.noise_threshold_n,
        off_mux_rleak_ohm=params.off_mux_rleak_ohm,
        accumulate_raw=True,
        force_zero_band_fraction=settings.force_zero_band_fraction,
        force_zero_band_min_n=thresholds.zero_band_min_n,
        force_zero_min_event_peak_n=thresholds.zero_min_event_peak_n,
        quiet_hold_release_fraction=settings.quiet_hold_release_fraction,
        quiet_hold_clear_s=settings.quiet_hold_clear_s,
        stuck_force_failsafe_enabled=settings.stuck_force_failsafe_enabled,
        stuck_force_quiet_hold_s=settings.stuck_force_quiet_hold_s,
        stuck_force_decay_tau_s=settings.stuck_force_decay_tau_s,
    )


def integrate_causal_series(
    integrator: PztForceChannelIntegrator, values: np.ndarray, times_s: np.ndarray,
) -> np.ndarray:
    """Run an already-centered scalar series through one integrator, sample by sample."""
    values, times = _as_series(values), _as_series(times_s)
    out = np.zeros(values.size, dtype=np.float64)
    for index in range(values.size):
        out[index] = integrator.process_centered_sample(
            float(values[index]), float(times[index])).accumulated_force_n
    return out


def _decay_by_position(config: EngineConfig, column_map: Mapping[str, str]) -> dict[str, float]:
    """Pre-sample decay (seconds) per position, from the policy's per-label map.

    Labels are matched against the engine's column names; a non-empty map that
    matches none of them is a naming mismatch, not a no-op.
    """
    by_label = config.timing.pre_sample_decay_s_by_label
    decay = {position: by_label[column] for position, column in column_map.items() if column in by_label}
    if by_label and not decay:
        raise ValueError(
            f"pre-sample decay labels {sorted(by_label)} match none of the engine columns {sorted(column_map.values())}"
        )
    return decay


class ForceStage:
    """``push(volts_by_position, timestamps_s)`` -> :class:`ForceTraces` for the new samples only."""

    def __init__(self, column_map: Mapping[str, str], config: EngineConfig) -> None:
        self._config = config
        self._decay = _decay_by_position(config, column_map)
        self.reset()

    def reset(self) -> None:
        """Clear every carried state (medians, previous sample, averages, integrators)."""
        force = self._config.force
        self._centering = ExpandingMedianCentering()
        self._rates = ForceRateStage(
            {position: force.physical_params(position) for position in SHEAR_SENSOR_POSITIONS},
            self._config.timing.leak_dt_s,
            self._decay,
        )
        window = self._config.smoothing_window_samples
        self._smoothing = {position: TrailingMeanStage(window) for position in SHEAR_OUTER_SENSOR_POSITIONS}
        self._normal = new_role_integrator(force, SHEAR_POSITION_CENTER)
        self._shear_lr = new_role_integrator(force, _SHEAR_LR_INTEGRATOR_POSITION)
        self._shear_tb = new_role_integrator(force, _SHEAR_TB_INTEGRATOR_POSITION)

    def push(self, volts_by_position: Mapping[str, np.ndarray], timestamps_s: np.ndarray) -> ForceTraces:
        times = _as_series(timestamps_s)
        if times.size == 0:
            return ForceTraces(times, times, times)
        rates = self._rates.push(self._centering.push(volts_by_position), times)
        smoothed = {position: stage.push(rates[position]) for position, stage in self._smoothing.items()}
        shear_lr, shear_tb, normal = combine_force_rates(rates, smoothed)
        return ForceTraces(
            normal_force=integrate_causal_series(self._normal, normal, times),
            shear_force_lr=integrate_causal_series(self._shear_lr, shear_lr, times),
            shear_force_tb=integrate_causal_series(self._shear_tb, shear_tb, times),
        )
