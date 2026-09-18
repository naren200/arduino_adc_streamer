"""
Active-Sample Segmentation
===========================
Replaces the old fixed-hop-grid quality gate (formerly quality_gate.is_window_quality)
with a sample-accurate scheme: instead of slicing a rigid window_size_s/hop_size_s
grid first and then accepting/rejecting the whole pre-cut block, this tracks
*where the real active spans of signal are* as 0.05s micro-chunks arrive, and
only ever emits window_size_s-length windows that slide within one contiguous
active span -- so a window can never straddle a genuine return-to-idle, and a
real touch event that doesn't happen to align to the fixed hop grid is no
longer silently chopped across a window boundary and discarded.

ActiveSampleQueue holds only index bookkeeping (start_idx/end_idx pairs into
the caller's own continuous raw/derived buffers), never a copy of the
underlying sample data -- see the class docstring below.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from . import window_padding
from .quality_gate import IdleBaseline, chunk_is_active, merge_gap_chunks

# CausalDerivedChannels' bounded windowed sums (integration, shear/normal) and
# chunk_is_active's own idle-band check are both unreliable on the very first
# samples of a session/capture -- the integration windows haven't filled and
# early ADC samples can carry settling transients, so windows drawn from here
# are not trustworthy inference input regardless of how "active" they look.
# Skip them outright rather than feeding false detections into the classifier.
WARMUP_SAMPLES = 200


@dataclass
class _Span:
    """Index-only bookkeeping for one finalized active span. start_idx/end_idx
    are references into the caller's continuous raw/derived buffers (end_idx
    exclusive), never a copy of the sample data itself."""

    start_idx: int
    end_idx: int
    enqueued_at: float
    span_id: int


class ActiveSampleQueue:
    """Consumes 0.05s micro-chunks one at a time (push_micro_chunk) and tracks
    finalized contiguous active spans, plus one "open" span still being built.

    A short idle run (shorter than merge_gap_chunks(window_size_s) micro-chunks)
    is absorbed into the surrounding active span, matching the old
    is_window_quality's merge behavior. A genuine, longer return-to-idle
    truncates the idle tail off the open span and finalizes what's left.

    ready_windows() then slides window_size_s windows at hop_size_s WITHIN
    each finalized span only -- a hop can never cross a span boundary, which
    is the core fix over the old fixed-grid gate.
    """

    def __init__(
        self,
        fs: float,
        window_size_s: float,
        hop_size_s: float,
        baseline: IdleBaseline,
        k: float | None = None,
    ):
        self.fs = fs
        self.window_size_s = window_size_s
        self.hop_size_s = hop_size_s
        self.baseline = baseline
        self.k = baseline.k if k is None else k
        self._merge_gap_chunks_n = merge_gap_chunks(window_size_s)

        self._finalized: deque[_Span] = deque()

        # Monotonically increasing id identifying one continuous active span
        # (one real touch event) across however many micro-chunks/windows it
        # spans -- lets a caller (the GUI's inference-region highlighting)
        # tell "still the same span" from "a new one started" without having
        # to compare index ranges itself. Assigned once per span, when it
        # first opens; carried through finalization and through ready_windows'
        # partial-consumption bookkeeping.
        self._next_span_id = 0

        # Open span currently being built -- index bookkeeping only.
        self._open_start: int | None = None
        self._open_end: int | None = None
        self._open_started_at: float | None = None
        self._open_span_id: int | None = None
        self._idle_run_chunks = 0
        # Index where the current tentative idle run began -- tracked
        # directly (not reconstructed from idle_run_chunks * a chunk width)
        # since micro-chunks pushed in are not guaranteed uniform width (the
        # last chunk of a capture, or a live tick's sweep count, can be
        # shorter than MICRO_CHUNK_S * fs).
        self._idle_run_start_idx: int | None = None
        # Timestamp of the most recent push_micro_chunk call -- used by
        # ready_windows() to refresh _open_started_at after advancing
        # _open_start, so evict_stale's age check tracks the age of the
        # oldest UNCONSUMED sample in the open span, not the age of the
        # touch's original onset (which would otherwise make evict_stale
        # drop a still-active, still-emitting open span mid-touch once the
        # touch runs longer than span_stale_timeout_s).
        self._last_now_t: float | None = None

    def push_micro_chunk(
        self, chunk_idx_range: tuple[int, int], chunk_samples: np.ndarray, now_t: float,
    ) -> None:
        """Feed one 0.05s micro-chunk's worth of raw ADC samples (for the
        activity test) plus its index range into the caller's continuous
        buffers. Call once per micro-chunk tick, in order."""
        start_idx, end_idx = chunk_idx_range
        chunk_samples = np.asarray(chunk_samples)
        active = chunk_is_active(chunk_samples, self.baseline, self.k)
        self._last_now_t = now_t

        if self._open_start is None:
            self._open_start = start_idx
            self._open_started_at = now_t
            self._open_span_id = self._next_span_id
            self._next_span_id += 1
        self._open_end = end_idx

        if active:
            self._idle_run_chunks = 0
            self._idle_run_start_idx = None
            return

        self._idle_run_chunks += 1
        if self._idle_run_start_idx is None:
            self._idle_run_start_idx = start_idx
        if self._idle_run_chunks < self._merge_gap_chunks_n:
            # Tentative idle run, still short of the merge-absorb threshold --
            # stays folded into the open span, nothing finalized yet.
            return

        # Genuine return to idle: truncate the idle tail back off the open
        # span (using the tracked start of this idle run, not a
        # chunk-count*width reconstruction, since chunk width isn't
        # guaranteed uniform) and finalize whatever real active signal is left.
        finalized_end = self._idle_run_start_idx
        if finalized_end > self._open_start:
            self._finalized.append(_Span(self._open_start, finalized_end, now_t, self._open_span_id))

        self._open_start = None
        self._open_end = None
        self._open_started_at = None
        self._open_span_id = None
        self._idle_run_chunks = 0
        self._idle_run_start_idx = None

    def ready_windows(
        self, window_size_s: float | None = None, hop_size_s: float | None = None,
        store_base_abs: int | None = None,
    ) -> list[tuple[int, int, int]]:
        """Slide window_size_s-length windows at hop_size_s hop within each
        finalized span that has reached window_size_s worth of real samples,
        never crossing a span boundary. Consumed portions advance so the same
        samples aren't re-yielded on a later call (mirrors
        RollingBuffer.get_window's advance-after-return pattern).

        A finalized span is, by construction, done growing -- it was closed
        off by a genuine return to idle in push_micro_chunk, so waiting any
        longer can never make its leftover remainder (shorter than window_n)
        reach window_n on its own. Rather than discard that remainder (or
        classify it alone on too little signal -- observed to misclassify,
        see window_padding.py's module docstring), pad it out to window_n
        using its own immediately-preceding history via
        window_padding.classify_padding -- see that module for the exact
        accept/reject rules (PaddingStatus). store_base_abs is the caller's
        continuous store's earliest still-retained absolute index, needed to
        know how far back padding can actually reach; store_base_abs=None
        (the default) skips padding entirely and preserves the older
        wait-for-evict_stale behavior, e.g. for a caller that hasn't opted in.

        Also slides within the OPEN span's confirmed-active prefix (up to
        the start of any current tentative idle run, so a still-tentative
        idle tail is never included) -- a sustained touch would otherwise
        emit nothing until it ends and the span finalizes, which starves the
        live "Prediction" readout during long drags/holds. The open span has
        had no genuine idle return yet, so this can't straddle one.

        Each yielded tuple is (start_idx, end_idx, span_id) -- span_id is
        stable across every window drawn from the same continuous active
        span (see _next_span_id), so a caller can tell "still the same real
        touch event" from "a new one started" without comparing indices."""
        window_size_s = self.window_size_s if window_size_s is None else window_size_s
        hop_size_s = self.hop_size_s if hop_size_s is None else hop_size_s
        window_n = round(window_size_s * self.fs)
        hop_n = round(hop_size_s * self.fs)
        if window_n <= 0 or hop_n <= 0:
            return []

        windows: list[tuple[int, int, int]] = []
        remaining: deque[_Span] = deque()
        for span in self._finalized:
            start, end = span.start_idx, span.end_idx
            while end - start >= window_n:
                if start >= WARMUP_SAMPLES:
                    windows.append((start, start + window_n, span.span_id))
                start += hop_n
            if end > start:
                if store_base_abs is not None and self._last_now_t is not None:
                    age_s = self._last_now_t - span.enqueued_at
                    decision = window_padding.classify_padding(
                        start, end, window_n, store_base_abs, age_s,
                    )
                    if decision.status is window_padding.PaddingStatus.READY and decision.padded_start >= WARMUP_SAMPLES:
                        windows.append((decision.padded_start, end, span.span_id))
                        continue
                    if decision.status in (
                        window_padding.PaddingStatus.EXPIRED,
                        window_padding.PaddingStatus.INSUFFICIENT_REAL_DATA,
                    ):
                        continue  # never going to become usable -- don't keep carrying it
                # INSUFFICIENT_HISTORY (more history may still be retained
                # later, though unlikely as the store grows forward), or
                # store_base_abs=None (padding not opted in): keep waiting.
                remaining.append(_Span(start, end, span.enqueued_at, span.span_id))
        self._finalized = remaining

        if self._open_start is not None:
            confirmed_end = (
                self._idle_run_start_idx if self._idle_run_start_idx is not None else self._open_end
            )
            start = self._open_start
            while confirmed_end - start >= window_n:
                if start >= WARMUP_SAMPLES:
                    windows.append((start, start + window_n, self._open_span_id))
                start += hop_n
            if start != self._open_start and self._last_now_t is not None:
                # Consumed part of the open span -- its oldest remaining
                # sample is now recent, so restart the staleness clock from
                # the last time we actually pushed data (see _last_now_t's
                # docstring in __init__).
                self._open_started_at = self._last_now_t
            self._open_start = start

        return windows

    def evict_stale(
        self, now_t: float, min_span_fill_ratio: float, span_stale_timeout_s: float,
    ) -> None:
        """Drop stale spans that will never produce a window.

        A FINALIZED span never grows (it's closed off by a genuine return to
        idle), so ready_windows() has already stripped every full window_n
        out of it -- anything still sitting in self._finalized is,  by
        construction, shorter than window_n and can never reach it. Once
        such a remainder is older than span_stale_timeout_s it's dead
        weight; drop it outright rather than carrying it forever (the
        min_span_fill_ratio check does not apply here -- it exists to give a
        still-growing span more time, and a finalized remainder isn't
        growing).

        The OPEN span, by contrast, IS still growing -- it only gets dropped
        if it's both stale AND still short of min_span_fill_ratio *
        window_size_s, so a legitimately-in-progress touch isn't discarded
        just because it hasn't hit the timeout-check cadence yet."""
        window_n = round(self.window_size_s * self.fs)
        min_n = min_span_fill_ratio * window_n

        kept: deque[_Span] = deque()
        for span in self._finalized:
            age = now_t - span.enqueued_at
            if age >= span_stale_timeout_s:
                continue  # drop: finalized spans never grow past what ready_windows already took
            kept.append(span)
        self._finalized = kept

        if self._open_start is not None:
            length = self._open_end - self._open_start
            age = now_t - self._open_started_at
            if age >= span_stale_timeout_s and length < min_n:
                self._open_start = None
                self._open_end = None
                self._open_started_at = None
                self._open_span_id = None
                self._idle_run_chunks = 0
                self._idle_run_start_idx = None

    def oldest_referenced_idx(self) -> int | None:
        """Smallest start_idx still referenced by any live span (finalized or
        open), or None if the queue is currently empty. A caller managing its
        own continuous backing buffers (raw/derived sample arrays) can safely
        trim anything before this index (minus its own safety margin), since
        nothing left in the queue points at it anymore."""
        candidates = [span.start_idx for span in self._finalized]
        if self._open_start is not None:
            candidates.append(self._open_start)
        return min(candidates) if candidates else None
