"""Causal median-of-N blip filtering for PZT ADC voltage columns during binary ingest.

Rejects isolated single-sample spikes the same way the 555/PZR firmware's own
``pzr_median3`` rejects isolated one-pair spikes on its resistance readings:
for each window of ``N`` (odd, default 3) consecutive raw samples the
middle-ranked value is kept, so a lone outlier is always out-voted by the
rest of the window. Runs after PZT ghost removal, on the same PZT voltage
columns identified by ``PztGhostRemovalMixin._get_pzt_ghost_groups`` (RS/555
resistance columns are never sampled waveforms and must not be
median-filtered).

Each column's filtered value only ever depends on its own ``N - 1``
immediately preceding raw samples, so the state carried across blocks is
``N - 1`` rows per filtered column, not a growing history.
"""

from __future__ import annotations

import numpy as np

from constants.pzt_blip_filter import (
    PZT_BLIP_FILTER_DEFAULT_ENABLED,
    PZT_BLIP_FILTER_DEFAULT_WINDOW_SAMPLES,
    normalize_pzt_blip_filter_window,
)


class PztBlipFilterMixin:
    """Owns per-column causal median-of-N state for PZT ADC voltage columns."""

    def _init_pzt_blip_filter_state(self) -> None:
        self.pzt_blip_filter_enabled = PZT_BLIP_FILTER_DEFAULT_ENABLED
        self.pzt_blip_filter_window_samples = PZT_BLIP_FILTER_DEFAULT_WINDOW_SAMPLES
        self._pzt_blip_filter_history: np.ndarray | None = None  # shape (window - 1, len(columns))
        self._pzt_blip_filter_history_len = 0

    def set_pzt_blip_filter_enabled(self, enabled: bool) -> None:
        self.pzt_blip_filter_enabled = bool(enabled)

    def set_pzt_blip_filter_window_samples(self, window_samples: int) -> None:
        window = normalize_pzt_blip_filter_window(window_samples)
        if window != getattr(self, "pzt_blip_filter_window_samples", None):
            self.pzt_blip_filter_window_samples = window
            # History is sized to the old window; a size change can never be
            # blended in, so drop it rather than reinterpret stale rows.
            self._pzt_blip_filter_history = None
            self._pzt_blip_filter_history_len = 0

    def begin_pzt_blip_filter_capture(self) -> None:
        """Drop carried-over history so a new capture never blends across the boundary."""
        self._pzt_blip_filter_history = None
        self._pzt_blip_filter_history_len = 0

    def prepare_pzt_blip_filter_blocks(
        self, block_data: np.ndarray, archive_data: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Filter PZT voltage columns once and mirror the result into both outputs.

        ``block_data`` and ``archive_data`` carry identical values in the PZT
        voltage columns (only RS/other slots may differ between the display
        and archive representations), so the stateful filter must run exactly
        once per block on those columns; running it twice would consume the
        carry-over history twice for what is the same signal.
        """
        block = np.asarray(block_data, dtype=np.float32).copy()
        archive = np.asarray(archive_data, dtype=np.float32).copy()
        if not getattr(self, "pzt_blip_filter_enabled", PZT_BLIP_FILTER_DEFAULT_ENABLED):
            return block, archive
        if block.ndim != 2 or block.shape[0] == 0:
            return block, archive

        width = block.shape[1]
        columns = sorted({
            column for group in self._get_pzt_ghost_groups(width) for column in group
        })
        if not columns:
            return block, archive

        column_indices = np.asarray(columns, dtype=np.int32)
        filtered = self._pzt_blip_filtered_columns(block[:, column_indices])
        block[:, column_indices] = filtered
        if archive.ndim == 2 and archive.shape[1] == width:
            archive[:, column_indices] = filtered
        return block, archive

    def _pzt_blip_filtered_columns(self, raw_columns: np.ndarray) -> np.ndarray:
        """Causal median-of-N filter every column of ``raw_columns`` (rows = samples)."""
        window = normalize_pzt_blip_filter_window(
            getattr(self, "pzt_blip_filter_window_samples", PZT_BLIP_FILTER_DEFAULT_WINDOW_SAMPLES)
        )
        carry = window - 1
        column_count = raw_columns.shape[1]
        history = self._pzt_blip_filter_history
        history_len = self._pzt_blip_filter_history_len
        if history is None or history.shape != (carry, column_count):
            history = np.zeros((carry, column_count), dtype=np.float32)
            history_len = 0

        prefix = history[carry - history_len:] if history_len else history[:0]
        extended = np.concatenate([prefix, raw_columns], axis=0)

        if extended.shape[0] >= window:
            windows = np.lib.stride_tricks.sliding_window_view(extended, window, axis=0)
            median = np.median(windows, axis=-1)
            filtered = raw_columns.copy()
            offset = carry - history_len
            filtered[offset:] = median
        else:
            # Not enough samples (across this block + carried history) for a
            # single full window yet; pass raw values through unfiltered.
            filtered = raw_columns.copy()

        tail = extended[-carry:] if carry else extended[:0]
        if tail.shape[0] < carry:
            padded = np.zeros((carry, column_count), dtype=np.float32)
            if tail.shape[0]:
                padded[carry - tail.shape[0]:] = tail
            self._pzt_blip_filter_history = padded
        else:
            self._pzt_blip_filter_history = tail.copy()
        self._pzt_blip_filter_history_len = min(carry, extended.shape[0])

        return filtered
