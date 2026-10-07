"""Shear/Normal Jerk and Baseline Removed overlays of the Analysis tab, as an
incremental, chunk-invariant stage.

This is NOT the engine's jerk feature (``CausalDerivedChannels``: windowed MEAN
of the raw volts minus the CURRENT median, window 22 by default). The overlay is
the calibration notebook's shear signal: each sample is first baseline-centered
against the running causal median (``volts - median(volts so far)``), the
centered series is summed over a trailing rectangular window (a window SUM, so
it scales with the window), and the five sums go through the shear detector and
normal-force combine. Both are defined by the same window, which is the GUI's
selectable integration window. Like every stage here, one call over a whole capture
equals any chunking of it bit for bit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np

from core.piezo_engine.force_stage import ExpandingMedianCentering, TrailingWindowSum, combine_force_rates
from core.piezo_engine.pipeline import LeadingDropGate
from core.piezo_engine.shear_constants import SHEAR_SENSOR_POSITIONS


@dataclass(frozen=True)
class OverlayTraces:
    """Overlay series of one ``push`` call, with the stream's leading warmup already dropped."""

    baseline_removed: Mapping[str, np.ndarray]
    shear_jerk_lr: np.ndarray
    shear_jerk_tb: np.ndarray
    normal_jerk: np.ndarray
    dropped_leading: int

    @property
    def n_samples(self) -> int:
        return len(self.shear_jerk_lr)


class JerkOverlayStage:
    """``push(volts_by_position)`` -> :class:`OverlayTraces` for the new samples only.

    The first ``window_samples - 1`` samples of the stream (the count a window sum needs
    to be full) are dropped from every series; ``dropped_leading`` says how many of THIS
    call's input samples that was, so a caller can slice its time axis with it.
    """

    def __init__(self, window_samples: int) -> None:
        if int(window_samples) < 1:
            raise ValueError("window_samples must be >= 1")
        self._window = int(window_samples)
        self._warmup = self._window - 1
        self.reset()

    def reset(self) -> None:
        self._centering = ExpandingMedianCentering()
        self._sums = {position: TrailingWindowSum(self._window) for position in SHEAR_SENSOR_POSITIONS}
        self._gate = LeadingDropGate()
        self._gate.arm(self._warmup)

    def push(self, volts_by_position: Mapping[str, np.ndarray]) -> OverlayTraces:
        centered = self._centering.push(volts_by_position)
        sums = {position: self._sums[position].push(centered[position])[0] for position in SHEAR_SENSOR_POSITIONS}
        shear_lr, shear_tb, normal = combine_force_rates(sums, sums)
        dropped = self._gate.take(len(shear_lr))
        return OverlayTraces(
            baseline_removed={position: values[dropped:] for position, values in centered.items()},
            shear_jerk_lr=shear_lr[dropped:],
            shear_jerk_tb=shear_tb[dropped:],
            normal_jerk=normal[dropped:],
            dropped_leading=dropped,
        )
