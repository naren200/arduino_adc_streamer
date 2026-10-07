"""Causal-median baseline and warmup helpers, ported from
texture_piezo's data.py for arduino_adc_streamer standalone signal processing.
"""

from __future__ import annotations

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
    """
    if sample_rate_hz <= 0.0:
        raise ValueError("sample_rate_hz must be greater than zero")
    return max(0, int(integration_window_samples) - 1)
