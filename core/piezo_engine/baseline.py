"""Causal-median baseline and capture-start-settle helpers, ported from
texture_piezo's data.py for arduino_adc_streamer standalone signal processing.
"""

from __future__ import annotations

import math

import numpy as np
from numba import njit


@njit(cache=True)
def _sift_down_min(heap, size, i):
    while True:
        left = 2 * i + 1
        right = 2 * i + 2
        smallest = i
        if left < size and heap[left] < heap[smallest]:
            smallest = left
        if right < size and heap[right] < heap[smallest]:
            smallest = right
        if smallest == i:
            break
        heap[i], heap[smallest] = heap[smallest], heap[i]
        i = smallest


@njit(cache=True)
def _sift_up_min(heap, i):
    while i > 0:
        parent = (i - 1) // 2
        if heap[parent] <= heap[i]:
            break
        heap[parent], heap[i] = heap[i], heap[parent]
        i = parent


@njit(cache=True)
def _heap_push(heap, size, value):
    heap[size] = value
    _sift_up_min(heap, size)
    return size + 1


@njit(cache=True)
def _heap_pop(heap, size):
    top = heap[0]
    size -= 1
    heap[0] = heap[size]
    _sift_down_min(heap, size, 0)
    return top, size


@njit(cache=True)
def incremental_median_batch(values, lower, upper, lower_size, upper_size):
    """Two-heap running median over `values`, resuming from (lower, upper,
    lower_size, upper_size) state passed in and returned updated, so
    repeated calls across chunks match one whole-array call exactly.

    lower = max-heap stored negated (as a min-heap of negatives), upper =
    min-heap. `lower`/`upper` must be pre-sized to hold the full eventual
    history (caller-managed capacity)."""
    n = values.shape[0]
    out = np.empty(n, dtype=np.float64)

    for idx in range(n):
        value = values[idx]
        if lower_size > 0 and value <= -lower[0]:
            lower_size = _heap_push(lower, lower_size, -value)
        else:
            upper_size = _heap_push(upper, upper_size, value)

        if lower_size > upper_size + 1:
            top, lower_size = _heap_pop(lower, lower_size)
            upper_size = _heap_push(upper, upper_size, -top)
        elif upper_size > lower_size:
            top, upper_size = _heap_pop(upper, upper_size)
            lower_size = _heap_push(lower, lower_size, -top)

        if lower_size > upper_size:
            out[idx] = -lower[0]
        else:
            out[idx] = (-lower[0] + upper[0]) / 2.0
    return out, lower_size, upper_size


class IncrementalMedian:
    """Persistent-state two-heap running median.

    `.push_many(values)` advances the series by an arbitrary-length chunk in
    one numba-jitted batch call and returns the causal (past-and-current-
    only) median for every sample in that chunk. State (the two heap arrays
    and their sizes) is stored as numpy arrays and carried across calls, so
    the batch path (below, one call over an already-complete array) and the
    streaming path (causal_derived_channels.CausalDerivedChannels, one call
    per incoming chunk spread across many `process()` calls) share exactly
    one implementation of the two-heap algorithm instead of drifting apart.
    """

    _INITIAL_CAPACITY = 1024

    def __init__(self) -> None:
        self._lower = np.empty(self._INITIAL_CAPACITY, dtype=np.float64)  # max-heap, stored negated
        self._upper = np.empty(self._INITIAL_CAPACITY, dtype=np.float64)  # min-heap
        self._lower_size = 0
        self._upper_size = 0

    def _ensure_capacity(self, n_incoming: int) -> None:
        needed = self._lower_size + self._upper_size + n_incoming
        capacity = self._lower.shape[0]
        if needed <= capacity:
            return
        while capacity < needed:
            capacity *= 2
        lower = np.empty(capacity, dtype=np.float64)
        upper = np.empty(capacity, dtype=np.float64)
        lower[: self._lower_size] = self._lower[: self._lower_size]
        upper[: self._upper_size] = self._upper[: self._upper_size]
        self._lower = lower
        self._upper = upper

    def push_many(self, values: np.ndarray) -> np.ndarray:
        values = np.asarray(values, dtype=np.float64).reshape(-1)
        self._ensure_capacity(values.shape[0])
        out, self._lower_size, self._upper_size = incremental_median_batch(
            values, self._lower, self._upper, self._lower_size, self._upper_size
        )
        return out


class _CausalMedian3:
    """Causal median filter, window = (i-2, i-1, i). One push() call advances
    the series by exactly one sample and returns the filtered value. Removes
    isolated single-sample spikes (the previous/next sample outvote it in the
    median) while only lightly lagging genuine multi-sample transitions.
    """

    def __init__(self) -> None:
        self._history: list[float] = []  # holds up to the 2 most recent raw samples

    def push(self, value: float) -> float:
        value = float(value)
        h = self._history
        n = len(h)
        # Scalar median-of-<=3 by selection, not np.median(): identical
        # output (np.median of an odd count returns the exact middle
        # element with no arithmetic; of an even count it's the mean of
        # the two middle elements, which for n=2 is just their mean) but
        # without paying numpy's general-purpose dispatch overhead on a
        # 1-3 element list 1.4M times -- that dispatch cost, not the
        # median computation itself, was ~90% of this filter's runtime
        # (profiled: 22s of a 45s file build). math.isnan check reproduces
        # np.median's NaN-poisons-the-window behavior, since sorted()
        # would otherwise give NaN a position-dependent, incorrect rank.
        if n == 0:
            filtered = value
        elif n == 1:
            a = h[0]
            filtered = float("nan") if (math.isnan(a) or math.isnan(value)) else (a + value) / 2.0
        else:
            a, b = h
            if math.isnan(a) or math.isnan(b) or math.isnan(value):
                filtered = float("nan")
            else:
                # Compare-swap median-of-3 (selection, no arithmetic) --
                # matches np.median's exact-middle-element output for an
                # odd count bit-for-bit, unlike a+b+c-min-max which
                # recombines via floating-point addition/subtraction and
                # can round differently.
                x, y, z = a, b, value
                if x > y:
                    x, y = y, x
                if y > z:
                    y, z = z, y
                if x > y:
                    x, y = y, x
                filtered = y
        self._history = (h + [value])[-2:]
        return filtered


@njit(cache=True)
def bounded_sum_batch(values, ring, ring_head, ring_count, total):
    """Fixed-window running sum over `values`, resuming from (ring,
    ring_head, ring_count, total) state passed in and returned updated.

    Ported from texture_piezo/src/_incremental_numba.py (shared there with
    `incremental_median_batch` above, split apart again here only to avoid
    duplicating this file's already-ported heap helpers)."""
    n = values.shape[0]
    out = np.empty(n, dtype=np.float64)
    maxlen = ring.shape[0]
    for i in range(n):
        value = values[i]
        if ring_count == maxlen:
            oldest = ring[ring_head]
            total -= oldest
            ring[ring_head] = value
            ring_head = (ring_head + 1) % maxlen
        else:
            ring[(ring_head + ring_count) % maxlen] = value
            ring_count += 1
        total += value
        out[i] = total
    return out, ring_head, ring_count, total


def total_warmup_sample_count(sample_rate_hz: float, integration_window_samples: int = 0) -> int:
    """Single source of truth for how many leading samples of a windowed-sum
    series are not yet valid: a bounded moving-sum window's fill time
    (`integration_window_samples - 1` -- the window has not yet seen enough
    real samples). Both `analysis_workbench.py` (batch) and
    `causal_derived_channels.CausalDerivedChannels` (streaming) call this
    same function so the two paths can never drift apart the way the
    hand-duplicated median implementations already had.

    `sample_rate_hz` is kept as a required, validated parameter even though
    the current formula doesn't use it, so every call site here and in both
    repos' callers doesn't need to change if a future sample-rate-dependent
    warmup source is ever added back.

    A prior version of this function also added a causal-median convergence
    warmup (`CAUSAL_MEDIAN_WARMUP_S`, removed 2026-09-30). `IncrementalMedian`
    is an unbounded expanding median with no fixed "fill" point, so that
    warmup was never a derived quantity -- empirical measurement against the
    `only_*` captures in data/raw/sensor_v12d_7_26/ch5 (true two-heap median
    run from sample 0, deviation-from-steady-state-median vs noise floor)
    found the p90 convergence time was ~0.0001s and the sole outlier
    (PZT5_B, only_idle_v2, 0.358s) was confounded with `CAPTURE_START_SETTLE_S`
    settling rather than genuine median-warmup -- visual inspection of that
    exact file/channel at warmup=0 showed no drift. Removed rather than kept
    conservative.
    """
    if sample_rate_hz <= 0.0:
        raise ValueError("sample_rate_hz must be greater than zero")
    return max(0, int(integration_window_samples) - 1)


CAPTURE_START_SETTLE_S = 0.4
"""Seconds of leading samples to discard from the very start of a capture/
session, before any other processing (integration, calibration, etc.) sees
them. Measured empirically (2026-09-30) against the labeled `baseline`
segments of the `only_*` texture captures in data/raw/sensor_v12d_7_26/ch5
and data/raw/sensor_v12b_v11z4: the mean largest-channel deviation from a
file's own baseline median is ~2.3-2.5x the steady-state noise floor for the
first 30 samples and decays to ~1.3x by sample 100. That covers most
channels, but a per-channel check (`_R` specifically, across all 15 baseline-
labeled files) found several files (cardboard_v1, leather_v1, leather_v2)
still ~1.3-1.5x their floor at sample 300 (~200ms), not converging until
~sample 600 (~1527 Hz -> ~400ms) -- an asymptotic settling transient, not a
fixed-count glitch. A few other files (leather_v3, tile_v3, tiona_v3) stay
elevated on `_R` well past 600 samples, but their 301-600 and 601-1500 bins
are already close to each other -- that's a noisier baseline in that
file/channel, not unfinished settling, and no larger cutoff fixes it. This
is real analog/mux settling at the very start of the series itself, distinct
from `total_warmup_sample_count`'s moving-window fill time on an
already-started series."""


def capture_start_settle_sample_count(sample_rate_hz: float) -> int:
    """Single source of truth for how many leading samples of a fresh
    capture/session to discard before any other processing sees them --
    Analysis (`load_exported_csv_snapshot`), live TouchID ingestion
    (`TouchIdStreamProcessor.push_chunk`, first chunk of a session only),
    and the saved/training-data path (`load_calibration_csv`) all call this
    same function so the cutoff can't drift apart between them. See
    CAPTURE_START_SETTLE_S for how the value was derived."""
    if sample_rate_hz <= 0.0:
        raise ValueError("sample_rate_hz must be greater than zero")
    return math.ceil(CAPTURE_START_SETTLE_S * float(sample_rate_hz))


def preprocess_capture_start(values: np.ndarray, sample_rate_hz: float, *, blip_filter) -> tuple[np.ndarray, int]:
    """Sole authority for capture-start preprocessing ORDER: blip-filter
    first, THEN settle-trim -- as one call, so the sequence can't be pulled
    apart by a future edit the way it already was once (a settle-trim that
    ran before filtering left the filter's own always-unfiltered leading
    edge, normally harmless deep inside a long capture, sitting as the very
    first visible samples instead).

    `blip_filter` is injected (not fixed here) because the three callers
    genuinely use different filters -- windowed `_median_filter_columns`
    with RS-column exclusion (Analysis), plain `causal_median_filter_3`
    (saved/training-data) -- forcing them onto one filter implementation
    would be a bigger, unwanted change than fixing the ordering bug calls
    for. What every caller DOES share, and what actually drifted apart
    before, is the sequence and the trim count -- that's what's centralized
    here. Pass a pass-through filter (`lambda x: x`) for a disabled-filter
    caller; do not skip this call entirely, or the trim silently stops
    happening too.

    `values` has samples on axis 0 (1D or 2D, one call covers a whole
    multi-column block). Returns (processed, trim_count) -- the caller must
    apply the same trim_count to every companion array (timestamps, force
    columns, ...) that isn't itself passed through blip_filter, so they stay
    aligned with the returned array.
    """
    values = np.asarray(values)
    filtered = blip_filter(values)
    trim_count = min(capture_start_settle_sample_count(sample_rate_hz), filtered.shape[0] - 1)
    trim_count = max(trim_count, 0)
    return filtered[trim_count:], trim_count
