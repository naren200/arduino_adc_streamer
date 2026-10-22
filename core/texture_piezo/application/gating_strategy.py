"""
Gating Strategy Contract
========================
The seam between ``TouchIdStreamProcessor`` (engine derivation, clock handling)
and a gating implementation (which newly appended samples form a classifier
window). A strategy owns its windowing state, its sample store and the store's
trim policy; the processor only hands it each tick's derived samples. Live-tuning hooks
are optional capabilities (OnsetSkipTunable, GateKTunable): callers apply one only to a
strategy that implements it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable

import numpy as np

from core.piezo_engine.live_window import LiveWindow
from core.texture_piezo.gating.v1.quality_gate import IdleBaseline
from core.texture_piezo.gating.v2.config import GatingV2Config

if TYPE_CHECKING:
    from .v1_gating import V1GatingOptions


@dataclass
class ReadyWindow:
    """One classifier-ready window, plus the bookkeeping the caller needs to
    paint it and submit it. ``window`` carries exactly the engine channels the
    loaded model asked for, by name, at native length and the window's sample rate.
    ``ranges`` are the absolute (start, end) sample-index ranges, end exclusive, the
    window is made of in stream order; ``window`` and ``window_ts`` are those ranges
    concatenated. v1 emits exactly one range, other strategies may emit several
    non-adjacent ones. frag_id is None in the fixed-grid fallback branch (no fragment
    concept there -- see _touchid_region_color_for_span's docstring in
    gui/inference_panel.py for how callers use this)."""

    window: LiveWindow
    window_ts: np.ndarray
    frag_id: int | None
    ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class GatingParams:
    """Everything a strategy is built from; fixed for its lifetime except the
    live-tunable values behind ``set_onset_skip_s`` / ``set_gate_k``."""

    pzt_columns: tuple[str, ...]
    required_channels: tuple[str, ...]
    window_size_s: float
    hop_size_s: float
    idle_baseline: IdleBaseline | None
    onset_skip_s: float
    v1_options: V1GatingOptions | None = None  # required by the "v1" strategy
    v2_config: GatingV2Config | None = None  # required by the "v2" strategy, refused by "v1"


@dataclass(frozen=True)
class DerivedChunk:
    """One tick's newly appended samples after the engine's warmup drop:
    ``channel_samples`` (despiked raw by pzt column), ``derived`` (engine output),
    and their parallel ``timestamps``, sampled at ``fs``. ``now_t`` is the caller's
    staleness clock (see TouchIdStreamProcessor.push_chunk); strategies measuring on
    sweep timestamps ignore it."""

    channel_samples: dict
    derived: dict
    timestamps: np.ndarray
    fs: float
    now_t: float


class GatingStrategy(Protocol):
    def push(self, chunk: DerivedChunk) -> list[ReadyWindow]:
        """Take in a non-empty chunk and return every window that became ready.
        Appending to the strategy's own store and trimming it afterwards are the
        strategy's job."""
        ...

    def reset(self) -> None:
        """Drop all windowing state and stored samples: the timeline broke, so
        previously stored indices and timestamps no longer relate to new ones."""
        ...


@runtime_checkable
class OnsetSkipTunable(Protocol):
    def set_onset_skip_s(self, onset_skip_s: float) -> None:
        """Apply a new onset-skip to live state; takes effect from the next touch."""
        ...


@runtime_checkable
class GateKTunable(Protocol):
    def set_gate_k(self, k: float) -> None:
        """Apply a new idle-gate multiplier to live state."""
        ...


GatingFactory = Callable[[GatingParams], GatingStrategy]
