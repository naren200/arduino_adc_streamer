"""Single source of truth for turning raw PZT ADC counts into the
"integrated" and shear/normal derived channels, processed strictly causally
(past-only) and incrementally, so training (whole file, one `process()`
call), offline GUI inference (whole snapshot, one `process()` call), and
live streaming inference (one `process()` call per newly arrived chunk) all
produce IDENTICAL numbers from identical input, by construction -- there is
no separate "live" vs "offline" algorithm, only how much input each caller
has accumulated so far.

Ported math (unchanged): data.integrate_voltage_series_causal_median's
bounded windowed sum (PZT-integration channels) and
shear_normal_utils_v1.compute_shear_normal_series's windowed-mean-minus-
causal-median centering + ShearDetector/NormalForceCalculator (shear/
normal channels). Both are re-expressed here as O(1)-amortized-per-sample
streaming state (bounded ring buffers + running sums, plus
data.IncrementalMedian for the unbounded causal median) instead of
recomputing over ever-growing history on every call.

`process()` never trims its own output -- every call returns exactly
`len(input)` samples per channel, so batch and streaming stay bit-exact by
construction. Warmup (see `data.total_warmup_sample_count`) is the caller's
job: check `.warmup_sample_count` and `.samples_seen` (both public after the
first `process()` call) and discard/withhold accordingly. This keeps the
one thing this class promises -- identical numbers from identical input,
regardless of chunking -- from ever depending on how a particular caller
happens to want its warmup handled.

Ported from texture_piezo/src/causal_derived_channels.py for
arduino_adc_streamer standalone signal processing; uses this repo's own
ShearDetector/NormalForceCalculator (core/piezo_engine) in place of
texture_piezo's shear_normal_utils_v1 copy of those two classes.
"""

from __future__ import annotations

import numpy as np

from core.piezo_engine.baseline import (
    IncrementalMedian,
    bounded_sum_batch,
    total_warmup_sample_count,
)
from core.piezo_engine.batch import compute_shear_normal_batch

# Rewired per task: these two classes now come from this repo's own
# core/piezo_engine modules instead of texture_piezo's shear_normal_utils_v1.
from core.piezo_engine.shear_detector import ShearDetector
from core.piezo_engine.normal_force_calculator import NormalForceCalculator

from constants.shear import SHEAR_SENSOR_POSITIONS as SENSOR_POSITIONS

# Ported from texture_piezo/src/data.py (DEFAULT_INTEGRATION_WINDOW_SAMPLES)
# and shear_normal_utils_v1.py (DEFAULT_JERK_INTEGRATION_WINDOW_SAMPLES,
# DEFAULT_VREF_VOLTAGE, IADC_RESOLUTION_BITS) as plain literals -- this repo
# has its own `constants/pressure_map.py` copy of the first and
# `constants/plotting.py` copy of the third, but they are duplicated here
# rather than imported so this module's defaults can never drift if either
# of those unrelated-purpose constants modules changes for its own reasons.
DEFAULT_INTEGRATION_WINDOW_SAMPLES = 30
DEFAULT_JERK_INTEGRATION_WINDOW_SAMPLES = 22
DEFAULT_VREF_VOLTAGE = 3.3
_IADC_RESOLUTION_BITS = 12


def counts_to_volts(values, vref_voltage: float = DEFAULT_VREF_VOLTAGE) -> np.ndarray:
    """ADC counts -> volts. Ported standalone from texture_piezo's
    shear_normal_utils_v1.counts_to_volts (itself ported from this repo's
    own data_processing/analysis_workbench.py:counts_to_volts) rather than
    imported from analysis_workbench.py directly: that module already
    imports core.piezo_engine.{shear_detector,normal_force_calculator,...},
    so importing back from it here would create a core <-> data_processing
    import cycle."""
    max_adc_value = float((2 ** _IADC_RESOLUTION_BITS) - 1)
    return (np.asarray(values, dtype=np.float64) / max_adc_value) * float(vref_voltage)

# Positional channel-suffix order for one PZT sensor board. Must match
# clip_windowing_utils_v1.CHANNEL_LABELS / PZT_COLUMNS ordering -- duplicated
# here as a plain literal (not imported) to avoid a circular import, since
# clip_windowing_utils_v1 imports drag_detection_utils_v1, which imports this
# module.
_CHANNEL_LABELS = ["B", "L", "C", "R", "T"]


class _BoundedSum:
    """Bounded moving-window rectangular sum: an O(1)-amortized-per-push
    ring buffer + running total, matching integrate_voltage_series_causal_median's
    "cumsum over the trailing `window` raw samples" semantics exactly
    (including its behavior in the first `window - 1` samples, where the sum
    only covers however many samples have arrived so far).

    State (ring buffer, head, count, total) is stored as numpy
    arrays/scalars and advanced a whole chunk at a time via `push_many`, one
    numba-jitted batch call per call instead of one Python call per sample."""

    def __init__(self, window: int) -> None:
        self._window = max(1, int(window))
        self._ring = np.zeros(self._window, dtype=np.float64)
        self._ring_head = 0
        self._ring_count = 0
        self.total = 0.0

    def push_many(self, values: np.ndarray) -> np.ndarray:
        values = np.asarray(values, dtype=np.float64).reshape(-1)
        out, self._ring_head, self._ring_count, self.total = bounded_sum_batch(
            values, self._ring, self._ring_head, self._ring_count, self.total
        )
        return out


class CausalDerivedChannels:
    """Streaming, stateful re-implementation of this project's "integrated"
    PZT-ADC channels and shear/normal proxy channels.

    Call `.process(chunk_by_column)` repeatedly with successive, non-
    overlapping, causally-ordered chunks of raw ADC counts (one call for the
    whole file/snapshot, or many small calls as a live stream arrives) --
    the returned per-sample outputs are identical either way.
    """

    def __init__(
        self,
        pzt_columns: list[str],
        column_map: dict[str, str] | None = None,
        integration_window_samples: int = DEFAULT_INTEGRATION_WINDOW_SAMPLES,
        jerk_window_samples: int = DEFAULT_JERK_INTEGRATION_WINDOW_SAMPLES,
        vref_voltage: float = DEFAULT_VREF_VOLTAGE,
    ) -> None:
        self.pzt_columns = list(pzt_columns)
        if column_map is None:
            if len(self.pzt_columns) != len(_CHANNEL_LABELS):
                raise ValueError(
                    "column_map must be given explicitly when pzt_columns does not "
                    "have exactly 5 entries in canonical B/L/C/R/T order"
                )
            column_map = dict(zip(_CHANNEL_LABELS, self.pzt_columns))
        self.column_map = dict(column_map)
        self.integration_window_samples = int(integration_window_samples)
        self.jerk_window_samples = int(jerk_window_samples)
        self.vref_voltage = float(vref_voltage)

        self._shear_detector = ShearDetector()
        self._normal_calculator = NormalForceCalculator()

        self._sample_rate_hz: float | None = None
        self.samples_seen = 0

        self.reset()

    def reset(self) -> None:
        """Clear all state: both bounded rings AND the unbounded median heaps."""
        self._pzt_sum = {col: _BoundedSum(self.integration_window_samples) for col in self.pzt_columns}
        self._shear_sum = {pos: _BoundedSum(self.jerk_window_samples) for pos in SENSOR_POSITIONS}
        self._shear_median = {pos: IncrementalMedian() for pos in SENSOR_POSITIONS}
        self._sample_rate_hz = None
        self.samples_seen = 0

    @property
    def warmup_sample_count(self) -> int:
        """Leading samples (of this stream's total history) still invalid.

        Only meaningful once `process()` has been called at least once (needs
        a known sample rate); raises if called earlier rather than guessing.
        """
        if self._sample_rate_hz is None:
            raise ValueError("warmup_sample_count is undefined before the first process() call")
        return total_warmup_sample_count(self._sample_rate_hz, self.jerk_window_samples)

    def process(self, chunk_by_column: dict[str, np.ndarray], *, sample_rate_hz: float) -> dict:
        """chunk_by_column: {pzt_column_name: 1-D array of new raw ADC-count
        samples in this chunk}, same channels/order every call.

        ``sample_rate_hz`` is required (not defaulted) so training, offline
        GUI inference, and live streaming can never silently disagree on what
        the FIRST call's rate was -- a wrong default is exactly how the
        warmup count would drift between callers. Only that first-call value
        is ever used (see `warmup_sample_count`); later calls may pass a
        different value without affecting anything; see `reset()`.

        Returns {"integrated": {pzt_column_name: array}, "shear_jerk_lr": array,
        "shear_jerk_tb": array, "normal_jerk": array} -- one output value per
        input sample in THIS chunk (not the whole history), untrimmed; see
        `warmup_sample_count`/`samples_seen` for what the caller should still
        discard.
        """
        sample_rate_hz = float(sample_rate_hz)
        if sample_rate_hz <= 0.0:
            raise ValueError("sample_rate_hz must be greater than zero")
        if self._sample_rate_hz is None:
            self._sample_rate_hz = sample_rate_hz
        # A later call's sample_rate_hz is intentionally never compared
        # against the latched value: it only ever feeds warmup_sample_count
        # (consumed once, at stream start -- see push_chunk's warmup trim),
        # never the per-sample integration/shear/normal math, which is sized
        # entirely by integration_window_samples/jerk_window_samples (sample
        # counts, not Hz). Live callers recompute fs every tick from a
        # cumulative elapsed-time average that drifts slightly forever and is
        # never bit-identical call-to-call -- rejecting that drift here would
        # crash an otherwise-healthy stream to protect a value nothing past
        # warmup still reads. Call reset() to actually start a new stream.

        n = 0
        for col in self.pzt_columns:
            n = len(np.asarray(chunk_by_column[col]).reshape(-1))
            break

        integrated_out = {}
        for col in self.pzt_columns:
            raw = np.asarray(chunk_by_column[col], dtype=np.float64).reshape(-1)
            integrated_out[col] = self._pzt_sum[col].push_many(raw)

        volts_by_position = {
            position: counts_to_volts(
                np.asarray(chunk_by_column[column], dtype=np.float64).reshape(-1), self.vref_voltage
            )
            for position, column in self.column_map.items()
        }

        centered = {}
        for position in SENSOR_POSITIONS:
            volts = volts_by_position[position]
            # bounded_sum and median don't depend on each other's output, so
            # each can be batched independently and combined afterward --
            # bit-identical to the interleaved per-sample push/push/combine.
            sum_out = self._shear_sum[position].push_many(volts)
            median_out = self._shear_median[position].push_many(volts)
            windowed_mean = sum_out / self.jerk_window_samples
            centered[position] = windowed_mean - median_out

        # detect()/compute() are pure per-sample functions (no cross-sample
        # state), unlike the bounded-sum/causal-median steps above, so this
        # replaces the per-sample Python loop with one vectorized numpy pass
        # -- see compute_shear_normal_batch's docstring for the bit-
        # exactness argument and its verification.
        shear_jerk_lr, shear_jerk_tb, normal_jerk = compute_shear_normal_batch(centered)

        self.samples_seen += n
        return {
            "integrated": integrated_out,
            "shear_jerk_lr": shear_jerk_lr,
            "shear_jerk_tb": shear_jerk_tb,
            "normal_jerk": normal_jerk,
        }
