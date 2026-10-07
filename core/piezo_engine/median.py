"""Streaming causal median-of-N blip filter (the engine's single despike stage).

Semantics are those of the GUI ingest filter (``PztBlipFilterMixin``): each
output is the median of the sample and its ``N - 1`` predecessors, and the
first ``N - 1`` samples of a stream pass through RAW (there is no full window
yet). Only ``N - 1`` samples per column are carried across calls, so one call
over a whole capture and many small calls give bit-identical output.

The median of an odd count is an exact element of the window (no arithmetic),
so computing in float64 returns the same values the GUI mixin computes in
float32 whenever the input is float32-representable.
"""

from __future__ import annotations

import numpy as np

MEDIAN_WINDOW_MIN_SAMPLES = 3
MEDIAN_WINDOW_MAX_SAMPLES = 15
DEFAULT_MEDIAN_WINDOW_SAMPLES = 3
_WINDOW_PARITY_DIVISOR = 2


def normalize_median_window(window_samples: int) -> int:
    """Clamp to the supported range and force odd (a median needs a middle element)."""
    window = int(window_samples)
    if window % _WINDOW_PARITY_DIVISOR == 0:
        window += 1
    return min(MEDIAN_WINDOW_MAX_SAMPLES, max(MEDIAN_WINDOW_MIN_SAMPLES, window))


def validate_median_window(window_samples: int) -> int:
    """Fail fast on a window ``normalize_median_window`` would silently alter."""
    window = int(window_samples)
    if normalize_median_window(window) != window:
        raise ValueError(
            f"median window must be odd and within "
            f"[{MEDIAN_WINDOW_MIN_SAMPLES}, {MEDIAN_WINDOW_MAX_SAMPLES}], got {window_samples}"
        )
    return window


class CausalMedianN:
    """Stateful causal median-of-N over one or more columns (samples on axis 0)."""

    def __init__(self, window_samples: int = DEFAULT_MEDIAN_WINDOW_SAMPLES, n_columns: int = 1) -> None:
        self.window_samples = validate_median_window(window_samples)
        self.n_columns = int(n_columns)
        self._carry = self.window_samples - 1
        self.reset()

    def reset(self) -> None:
        self._history = np.empty((0, self.n_columns), dtype=np.float64)

    def process(self, values) -> np.ndarray:
        """Filter the next chunk; returns an array shaped like ``values`` (float64)."""
        block = np.asarray(values, dtype=np.float64)
        is_single_column = block.ndim == 1
        block = block.reshape(len(block), -1) if is_single_column else block
        if block.shape[1] != self.n_columns:
            raise ValueError(f"expected {self.n_columns} columns, got {block.shape[1]}")
        extended = np.concatenate([self._history, block], axis=0)
        filtered = self._filter_block(block, extended)
        self._history = extended[len(extended) - min(self._carry, len(extended)):].copy()
        return filtered[:, 0] if is_single_column else filtered

    def _filter_block(self, block: np.ndarray, extended: np.ndarray) -> np.ndarray:
        filtered = block.copy()
        if extended.shape[0] < self.window_samples:
            return filtered
        windows = np.lib.stride_tricks.sliding_window_view(extended, self.window_samples, axis=0)
        first_filtered_row = self._carry - len(self._history)
        filtered[first_filtered_row:] = np.median(windows, axis=-1)
        return filtered
