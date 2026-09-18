"""
Rolling Buffer
==============
Plain, PyQt-independent accumulator for per-channel ADC samples arriving in
irregular pushes. Feeds fixed-size, overlapping windows to the inference
pipeline once enough data has accumulated.

Deliberately has no signals/slots so it can be unit-tested without a running
GUI event loop.
"""

from __future__ import annotations

import numpy as np


class RollingBuffer:
    def __init__(self, n_channels: int, window_size_s: float, hop_size_s: float):
        self.n_channels = n_channels
        self.window_size_s = window_size_s
        self.hop_size_s = hop_size_s

        self._channel_order: list[str] | None = None
        self._samples: dict[str, np.ndarray] = {}
        self._timestamps: np.ndarray = np.empty(0)

    def push(self, channel_samples: dict[str, np.ndarray], timestamps: np.ndarray) -> None:
        """Append newly arrived per-channel samples + their timestamps."""
        if self._channel_order is None:
            self._channel_order = list(channel_samples.keys())
            if len(self._channel_order) != self.n_channels:
                raise ValueError(
                    f"Expected {self.n_channels} channels, got {len(self._channel_order)}"
                )
            self._samples = {name: np.empty(0) for name in self._channel_order}

        for name in self._channel_order:
            new_samples = np.asarray(channel_samples[name])
            self._samples[name] = np.concatenate([self._samples[name], new_samples])

        self._timestamps = np.concatenate([self._timestamps, np.asarray(timestamps)])

    def get_window(self, fs: float) -> tuple[np.ndarray, np.ndarray] | None:
        """
        Return (window_adc: (n_window_samples, n_channels), timestamps) once
        window_size_s worth of data is buffered, else None.
        Internally advances the buffer start by hop_size_s worth of samples
        after a window is successfully returned (so the next call yields the
        next hop, not the same window again).
        """
        if self._channel_order is None:
            return None

        window_n = round(self.window_size_s * fs)
        hop_n = round(self.hop_size_s * fs)

        if len(self._timestamps) < window_n:
            return None

        window_adc = np.stack(
            [self._samples[name][:window_n] for name in self._channel_order], axis=1
        )
        window_timestamps = self._timestamps[:window_n]

        advance_n = min(hop_n, len(self._timestamps))
        for name in self._channel_order:
            self._samples[name] = self._samples[name][advance_n:]
        self._timestamps = self._timestamps[advance_n:]

        return window_adc, window_timestamps
