"""
Short-Span History Padding
=============================
When a real active span (see segmentation.ActiveSampleQueue) is shorter than
window_size_s, the classifier previously either got nothing (the span sat
until evict_stale dropped it unclassified) or -- briefly, before this module
existed -- got just the short span alone, which starved feature extraction of
enough signal and produced wrong classifications (observed: a ~0.15s touch
tail misclassified as bumpy_wood instead of the touch's own tiona class,
2026-09-17).

This module pads a short span out to a full window_n using the continuous
sample store's own immediately-preceding samples -- whatever they are, real
signal or idle -- instead of discarding it or classifying it alone. Every bit
of real active signal can now eventually reach the classifier, at the cost of
some sample overlap with whatever window(s) came right before it.

classify_padding() is the single decision point: it always returns a
PaddingStatus, so a caller can tell exactly why a span was or wasn't padded
rather than silently guessing. Two callers use this identically: the live GUI
panel (gui/inference_panel.py) and offline CSV inference (inference/offline.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# A padded window's own real (un-padded) portion must be at least this
# fraction of window_n -- below it, padding would dilute a sliver of real
# activity into a window that's mostly borrowed history, which isn't a
# meaningful clip of the actual event. Equivalently, padding drawn from
# history never exceeds (1 - MIN_REAL_FRACTION) of the window.
MIN_REAL_FRACTION = 0.2

# How long a short span may sit waiting to be padded before it's considered
# too stale to bother with -- same spirit as ActiveSampleQueue's
# span_stale_timeout_s (which governs whether the QUEUE keeps carrying a span
# forward), but this is the separate question of whether it's still worth
# padding and classifying at all once we do get around to it.
PADDING_MAX_AGE_S = 1.0


class PaddingStatus(Enum):
    """Why a short (< window_n) active span was or wasn't padded into a
    classifiable window."""

    READY = "ready"
    # A padded window was built: [padded_start, end) is exactly window_n
    # samples -- the span's own real active samples, preceded by enough
    # immediately-prior history (real or idle) to fill out the rest.

    INSUFFICIENT_REAL_DATA = "insufficient_real_data"
    # The span's own active portion is below MIN_REAL_FRACTION * window_n --
    # not enough real signal for padding to be meaningful.

    INSUFFICIENT_HISTORY = "insufficient_history"
    # Not enough preceding samples are still retained (e.g. very early in a
    # capture/session, or already trimmed out of the continuous store) to
    # supply the padding this span needs.

    EXPIRED = "expired"
    # Enough history exists, but this span has been waiting to be padded for
    # longer than PADDING_MAX_AGE_S -- too stale to still be worth building.


@dataclass(frozen=True)
class PaddingDecision:
    status: PaddingStatus
    padded_start: int | None = None  # set only when status is READY


def classify_padding(
    start: int, end: int, window_n: int, store_base_abs: int, age_s: float,
) -> PaddingDecision:
    """Decide whether/how to pad one short active span [start, end) up to
    window_n samples using its own immediately-preceding history.

    store_base_abs: earliest absolute sample index still retained by the
        caller's continuous store -- padding can't reach before this.
    age_s: how long this span has existed without reaching window_n on its
        own (caller-computed, typically now_t - span.enqueued_at)."""
    real_n = end - start
    if real_n < MIN_REAL_FRACTION * window_n:
        return PaddingDecision(PaddingStatus.INSUFFICIENT_REAL_DATA)

    pad_n = window_n - real_n
    padded_start = start - pad_n
    if padded_start < store_base_abs:
        return PaddingDecision(PaddingStatus.INSUFFICIENT_HISTORY)

    if age_s > PADDING_MAX_AGE_S:
        return PaddingDecision(PaddingStatus.EXPIRED)

    return PaddingDecision(PaddingStatus.READY, padded_start=padded_start)
