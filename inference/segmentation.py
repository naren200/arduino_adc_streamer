"""
Active-Sample Segmentation
===========================
Replaces the old fixed-hop-grid quality gate (formerly quality_gate.is_window_quality)
with a sample-accurate scheme: instead of slicing a rigid window_size_s/hop_size_s
grid first and then accepting/rejecting the whole pre-cut block, this tracks
*where the real active samples are* as 0.05s micro-chunks arrive, and only
ever emits window_size_s-length windows that slide within one contiguous
active fragment -- so a window can never straddle a genuine return-to-idle,
and a real touch event that doesn't happen to align to the fixed hop grid is
no longer silently chopped across a window boundary and discarded.

There is no separate "finalized vs. open" split -- every active run is one
uniform _Fragment, tracked in a single ordered queue. An idle run shorter
than quality_gate.idle_gap_chunks_cap(window_size_s) stays inline (absorbed)
inside the open fragment; an idle run at or beyond that threshold is
stripped out entirely and closes the fragment off. expire() bounds how long
an unconsumed fragment is carried before being dropped, independently of
window_padding.PADDING_MAX_AGE_S (which governs whether a short leftover
remainder is still worth padding, a different question -- see its own
docstring).

NOTE on scope: a window is still drawn from a SINGLE fragment, same as the
prior span model. Stitching two fragments separated by a STRIPPED (not
absorbed) idle gap into one classifier window is not implemented here --
doing so would require a window to reference multiple, non-contiguous
index ranges into the caller's raw store, which the current
(start_idx, end_idx, frag_id) return contract cannot express. That's a
stream_processor.py/ReadyWindow-level change (gathering multiple ranges
into one array), not a segmentation.py one, and is out of scope for this
pass -- flagged explicitly rather than silently faked.

ActiveSampleQueue holds only index bookkeeping (start_idx/end_idx pairs into
the caller's own continuous raw/derived buffers, plus each fragment's real
start/end timestamps for visualization/analysis), never a copy of the
underlying sample data.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import window_padding
from .quality_gate import IdleBaseline, chunk_is_active, idle_gap_chunks_cap

# CausalDerivedChannels' bounded windowed sums (integration, shear/normal) and
# chunk_is_active's own idle-band check are both unreliable on the very first
# samples of a session/capture -- the integration windows haven't filled and
# early ADC samples can carry settling transients, so windows drawn from here
# are not trustworthy inference input regardless of how "active" they look.
# Skip them outright rather than feeding false detections into the classifier.
WARMUP_SAMPLES = 200

# How long a fragment may sit in the queue, unconsumed by a full window,
# before it's dropped as dead weight -- queue-residency staleness, a
# DIFFERENT question from window_padding.PADDING_MAX_AGE_S (whether a
# leftover remainder is still worth padding once we do get to it). Two
# distinct timers, two distinct jobs -- do not collapse them.
FRAGMENT_MAX_AGE_S = 3.0


@dataclass
class _Fragment:
    """Index-only bookkeeping for one contiguous run of active samples.
    start_idx/end_idx are references into the caller's continuous
    raw/derived buffers (end_idx exclusive), never a copy of the sample data
    itself. start_ts/end_ts are the real (caller-supplied now_t-derived)
    timestamps this fragment's samples were pushed at, carried alongside the
    index range for visualization/analysis of the queue."""

    start_idx: int
    end_idx: int
    start_ts: float
    end_ts: float
    frag_id: int


class ActiveSampleQueue:
    """Consumes 0.05s micro-chunks one at a time (push_micro_chunk) and
    maintains an ordered queue of active _Fragments. An idle run shorter
    than idle_gap_chunks_cap(window_size_s) stays inline inside the
    currently-open fragment (absorbed); an idle run at or beyond that
    threshold is stripped out entirely and closes the fragment off, so a
    fragment is always genuinely, contiguously active.

    ready_windows() slides window_size_s windows at hop_size_s hop within
    each fragment (never crossing a fragment boundary -- see the module
    docstring's NOTE on scope for why cross-fragment stitching isn't done
    here). Fragments keep accumulating in the queue for as long as real
    activity (with only short absorbed gaps) keeps arriving -- there is no
    separate cap on how many fragments may exist or how long the queue may
    grow; expire() is what actually bounds how long a fragment is carried
    before being dropped as dead weight.
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
        self._idle_gap_chunks_cap = idle_gap_chunks_cap(window_size_s)

        self._fragments: list[_Fragment] = []

        # Monotonically increasing id identifying one continuous active
        # fragment (one real touch event) -- lets a caller (the GUI's
        # inference-region highlighting) tell "still the same fragment" from
        # "a new one started" without comparing index ranges itself.
        # Assigned once per fragment, when it first opens.
        self._next_frag_id = 0

        # Open fragment currently being built -- index + timestamp
        # bookkeeping only.
        self._open_start: int | None = None
        self._open_end: int | None = None
        self._open_start_ts: float | None = None
        self._open_frag_id: int | None = None
        self._idle_run_chunks = 0
        # Index where the current tentative idle run began -- tracked
        # directly (not reconstructed from idle_run_chunks * a chunk width)
        # since micro-chunks pushed in are not guaranteed uniform width (the
        # last chunk of a capture, or a live tick's sweep count, can be
        # shorter than MICRO_CHUNK_S * fs).
        self._idle_run_start_idx: int | None = None
        # Timestamp of the most recent push_micro_chunk call -- used by
        # ready_windows() to refresh the open fragment's start_ts after
        # advancing past consumed samples, and by expire() to judge the
        # open fragment's own staleness.
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
            self._open_start_ts = now_t
            self._open_frag_id = self._next_frag_id
            self._next_frag_id += 1
        self._open_end = end_idx

        if active:
            self._idle_run_chunks = 0
            self._idle_run_start_idx = None
            return

        self._idle_run_chunks += 1
        if self._idle_run_start_idx is None:
            self._idle_run_start_idx = start_idx
        if self._idle_run_chunks < self._idle_gap_chunks_cap:
            # Tentative idle run, still short of the strip threshold -- stays
            # inline inside the open fragment, nothing closed off yet.
            return

        # Genuine return to idle: strip the idle tail back off the open
        # fragment (using the tracked start of this idle run, not a
        # chunk-count*width reconstruction, since chunk width isn't
        # guaranteed uniform) and close whatever real active signal is left.
        fragment_end = self._idle_run_start_idx
        if fragment_end > self._open_start:
            self._fragments.append(
                _Fragment(self._open_start, fragment_end, self._open_start_ts, now_t, self._open_frag_id)
            )

        self._open_start = None
        self._open_end = None
        self._open_start_ts = None
        self._open_frag_id = None
        self._idle_run_chunks = 0
        self._idle_run_start_idx = None

    @staticmethod
    def _windows_for_fragment(
        fragment: _Fragment, window_n: int, hop_n: int,
    ) -> tuple[list[tuple[int, int, int]], int]:
        """Pure: slide window_n windows at hop_n hop within one fragment.
        Returns (windows, new_start) where new_start is the fragment's
        updated start_idx after consuming whatever full windows fit."""
        windows: list[tuple[int, int, int]] = []
        start = fragment.start_idx
        end = fragment.end_idx
        while end - start >= window_n:
            if start >= WARMUP_SAMPLES:
                windows.append((start, start + window_n, fragment.frag_id))
            start += hop_n
        return windows, start

    def ready_windows(
        self, window_size_s: float | None = None, hop_size_s: float | None = None,
        store_base_abs: int | None = None,
    ) -> list[tuple[int, int, int]]:
        """Slide window_size_s-length windows at hop_size_s hop within each
        fragment that has reached window_size_s worth of real samples, never
        crossing a fragment boundary. Consumed portions advance so the same
        samples aren't re-yielded on a later call (mirrors
        RollingBuffer.get_window's advance-after-return pattern).

        A CLOSED fragment is, by construction, done growing -- so waiting
        any longer can never make its leftover remainder (shorter than
        window_n) reach window_n on its own. Rather than discard that
        remainder (or classify it alone on too little signal -- observed to
        misclassify, see window_padding.py's module docstring), pad it out
        to window_n using its own immediately-preceding history via
        window_padding.classify_padding -- see that module for the exact
        accept/reject rules (PaddingStatus). store_base_abs is the caller's
        continuous store's earliest still-retained absolute index, needed to
        know how far back padding can actually reach; store_base_abs=None
        (the default) skips padding entirely.

        Also slides within the OPEN fragment's confirmed-active prefix (up
        to the start of any current tentative idle run, so a still-tentative
        idle tail is never included) -- a sustained touch would otherwise
        emit nothing until it closes, which starves the live "Prediction"
        readout during long drags/holds. The open fragment has had no
        genuine idle return yet, so this can't straddle one.

        Each yielded tuple is (start_idx, end_idx, frag_id) -- frag_id is
        stable across every window drawn from the same fragment, so a
        caller can tell "still the same real touch event" from "a new one
        started" without comparing indices."""
        window_size_s = self.window_size_s if window_size_s is None else window_size_s
        hop_size_s = self.hop_size_s if hop_size_s is None else hop_size_s
        window_n = round(window_size_s * self.fs)
        hop_n = round(hop_size_s * self.fs)
        if window_n <= 0 or hop_n <= 0:
            return []

        windows: list[tuple[int, int, int]] = []
        remaining: list[_Fragment] = []
        for fragment in self._fragments:
            frag_windows, new_start = self._windows_for_fragment(fragment, window_n, hop_n)
            windows.extend(frag_windows)
            start, end = new_start, fragment.end_idx
            if end > start:
                if store_base_abs is not None and self._last_now_t is not None:
                    age_s = self._last_now_t - fragment.end_ts
                    decision = window_padding.classify_padding(start, end, window_n, store_base_abs, age_s)
                    if decision.status is window_padding.PaddingStatus.READY and decision.padded_start >= WARMUP_SAMPLES:
                        windows.append((decision.padded_start, end, fragment.frag_id))
                        continue
                    if decision.status in (
                        window_padding.PaddingStatus.EXPIRED,
                        window_padding.PaddingStatus.INSUFFICIENT_REAL_DATA,
                    ):
                        continue  # never going to become usable -- don't keep carrying it
                # INSUFFICIENT_HISTORY (more history may still be retained
                # later), or store_base_abs=None (padding not opted in):
                # keep waiting.
                remaining.append(_Fragment(start, end, fragment.start_ts, fragment.end_ts, fragment.frag_id))
        self._fragments = remaining

        if self._open_start is not None:
            confirmed_end = (
                self._idle_run_start_idx if self._idle_run_start_idx is not None else self._open_end
            )
            open_fragment = _Fragment(self._open_start, confirmed_end, self._open_start_ts, confirmed_end, self._open_frag_id)
            frag_windows, new_start = self._windows_for_fragment(open_fragment, window_n, hop_n)
            windows.extend(frag_windows)
            if new_start != self._open_start and self._last_now_t is not None:
                # Consumed part of the open fragment -- its oldest remaining
                # sample is now recent, so restart the staleness clock from
                # the last time we actually pushed data.
                self._open_start_ts = self._last_now_t
            self._open_start = new_start

        return windows

    def expire(self, now_t: float) -> None:
        """Drop stale fragments that will never produce a window.

        A CLOSED fragment never grows again, so ready_windows() has already
        stripped every full window_n out of it -- anything still sitting in
        self._fragments is, by construction, shorter than window_n and can
        never reach it on its own. Once such a remainder's most recent
        sample is older than FRAGMENT_MAX_AGE_S it's dead weight; drop it
        outright rather than carrying it forever.

        The OPEN fragment, by contrast, IS still growing -- it only gets
        dropped once its most recently pushed data is older than
        FRAGMENT_MAX_AGE_S, since a legitimately-in-progress touch keeps
        refreshing its own age on every push_micro_chunk call.

        This is also what bounds how far apart in real time two fragments
        can still end up windowed close together -- there is no separate
        merge-distance cap beyond this."""
        self._fragments = [f for f in self._fragments if now_t - f.end_ts <= FRAGMENT_MAX_AGE_S]

        if self._open_start is not None and self._last_now_t is not None:
            if now_t - self._last_now_t > FRAGMENT_MAX_AGE_S:
                self._open_start = None
                self._open_end = None
                self._open_start_ts = None
                self._open_frag_id = None
                self._idle_run_chunks = 0
                self._idle_run_start_idx = None

    def oldest_referenced_idx(self) -> int | None:
        """Smallest start_idx still referenced by any live fragment (closed
        or open), or None if the queue is currently empty. A caller managing
        its own continuous backing buffers (raw/derived sample arrays) can
        safely trim anything before this index (minus its own safety
        margin), since nothing left in the queue points at it anymore."""
        candidates = [f.start_idx for f in self._fragments]
        if self._open_start is not None:
            candidates.append(self._open_start)
        return min(candidates) if candidates else None
