"""
Windowed-Vote Confidence Smoother
===================================
Replaces the old per-class EMA smoother. Per-class EMA damps each class's
probability independently but does nothing to stop the *predicted label*
itself hopping between classes near a decision boundary -- it has no memory
of how long a class has actually been winning.

Three techniques instead, worked out from first principles (binomial
probability of majority-vote correctness, per-class median vs. EMA, discrete
derivative for transition detection):

  - Majority vote (mode of the last `window_n` windows' top-1 labels) picks
    the displayed class. If each window is independently correct with
    probability p, pooling N windows and taking the majority raises the
    effective correctness above any single window's (the binomial argument:
    P(majority correct) = sum over k > N/2 of C(N,k) p^k (1-p)^(N-k)) -- and
    unlike EMA, a single spiky misclassification is just outvoted rather
    than dragging a running average toward it.
  - Per-class median (over the same window) sets the displayed confidence
    for the majority-vote class -- robust to that same single-window spike
    for the same reason.
  - A discrete first derivative of the majority label's own confidence
    across the window is exposed for transition detection: the median is
    deliberately laggy by design (that's what makes it robust), so a large
    derivative flags "this is changing right now" faster than the smoothed
    value alone would show.

Rolling mean/std of each window's top confidence is also exposed, for
diagnostics/status display rather than to drive the label itself.
"""

from __future__ import annotations

from collections import Counter, deque

import numpy as np

# Guilty-clip filter thresholds -- a window is excluded from the smoother's
# vote (not discarded from classification, just not allowed to influence the
# majority) when its own raw prediction disagrees with the smoother's
# CURRENT (pre-this-window) majority label, or when its raw softmax isn't a
# clean call (a real runner-up above NEAR_TIE_TOP2_MIN even though the top
# class is below DOMINANT_TOP1_MAX). Values fit on touchid replay captures
# (_v1/_v2 files) and validated held-out (_v3/_v4): 83% recall of windows
# that would otherwise corrupt the smoothed output, 11.7% false-positive
# rate on windows that were actually fine (mostly genuine low-confidence
# calls on manual inspection, not filter noise) -- see
# scratchpad/COMBINATION_SEARCH.md and VERIFY_11PCT_NATURE.md from the
# 2026-09-22 investigation for the full methodology.
NEAR_TIE_TOP2_MIN = 0.05
DOMINANT_TOP1_MAX = 0.79

# Internal-only aging cap on the smoother's vote pool: an entry older than
# this (by whatever now_t the caller passes -- wall-clock time.monotonic()
# live, or replay's synthetic end/fs time) is pruned before the vote/median
# is taken, regardless of window_n. Without this, a real-time gap between
# windows (idle-gate skips, the tab not visible, inference manually stopped
# and resumed) leaves stale pre-gap entries sitting in the deque, and the
# first fresh window after the gap gets voted on alongside unrelated old
# history instead of starting clean. Not user-configurable -- window_n
# already exposes the smoothing/responsiveness tradeoff in the GUI.
SMOOTHING_MAX_AGE_S = 7.0


def is_guilty_candidate(probs: dict[str, float], current_majority_label: str | None) -> bool:
    """True if this window's raw prediction should be excluded from the
    smoother's vote. `current_majority_label` is the smoother's
    majority_label() BEFORE this window is appended -- with no history yet
    (None), nothing to disagree with, so it's never excluded."""
    if current_majority_label is None:
        return False
    sorted_probs = sorted(probs.values(), reverse=True)
    top1 = sorted_probs[0] if sorted_probs else 0.0
    top2 = sorted_probs[1] if len(sorted_probs) > 1 else 0.0
    raw_label = max(probs.items(), key=lambda kv: kv[1])[0] if probs else None
    disagrees = raw_label != current_majority_label
    unclean_call = top2 > NEAR_TIE_TOP2_MIN and top1 <= DOMINANT_TOP1_MAX
    return disagrees or unclean_call


class WindowedVoteSmoother:
    def __init__(self, class_names: list[str], window_n: int = 5, max_age_s: float = SMOOTHING_MAX_AGE_S):
        self.class_names = list(class_names)
        self.window_n = max(1, int(window_n))
        self.max_age_s = float(max_age_s) if max_age_s else None
        # Unbounded deque (no maxlen) -- window_n is now enforced in
        # _prune() alongside the age cap, since a plain maxlen deque would
        # silently evict the oldest entry on every append before age-pruning
        # ever got a chance to run.
        self._entries: deque[tuple[float, str, dict[str, float]]] = deque()

    def update(self, probs: dict[str, float], now_t: float = 0.0) -> dict[str, float]:
        top_label = max(probs.items(), key=lambda kv: kv[1])[0]
        self._entries.append((now_t, top_label, dict(probs)))
        self._prune(now_t)
        return self._smoothed_probs()

    def _prune(self, now_t: float) -> None:
        """Drop entries older than max_age_s (if aging is enabled), then cap
        to the window_n most recent survivors."""
        if self.max_age_s is not None:
            while self._entries and (now_t - self._entries[0][0]) > self.max_age_s:
                self._entries.popleft()
        while len(self._entries) > self.window_n:
            self._entries.popleft()

    @property
    def _probs_history(self) -> list[dict[str, float]]:
        return [probs for _ts, _label, probs in self._entries]

    @property
    def _label_history(self) -> list[str]:
        return [label for _ts, label, _probs in self._entries]

    def _smoothed_probs(self) -> dict[str, float]:
        return {
            c: float(np.median([p.get(c, 0.0) for p in self._probs_history]))
            for c in self.class_names
        }

    def majority_label(self) -> str | None:
        """Mode of the last window_n top-1 labels. Ties are broken toward
        the most recently occurring tied label, favoring responsiveness
        over an arbitrary fixed tie rule."""
        if not self._label_history:
            return None
        counts = Counter(self._label_history)
        max_count = max(counts.values())
        candidates = {c for c, n in counts.items() if n == max_count}
        for label in reversed(self._label_history):
            if label in candidates:
                return label
        return None  # unreachable -- every label in _label_history is a candidate for some tie

    def top_class(self, smoothed: dict[str, float]) -> tuple[str, float]:
        """Majority-vote label with its median-smoothed confidence --
        replaces the old argmax-of-EMA'd-probs top_class."""
        label = self.majority_label()
        if label is None:
            label = max(smoothed.items(), key=lambda kv: kv[1])[0]
        return label, smoothed.get(label, 0.0)

    def confidence_derivative(self) -> float:
        """Rate of change (per window-step) of the majority label's own
        confidence across the window -- large magnitude flags a state
        transition in progress."""
        if len(self._probs_history) < 2:
            return 0.0
        label = self.majority_label()
        if label is None:
            return 0.0
        series = [p.get(label, 0.0) for p in self._probs_history]
        steps = len(series) - 1
        return (series[-1] - series[0]) / steps if steps else 0.0

    def rolling_stats(self) -> tuple[float, float]:
        """(mean, std) of each window's top confidence, over the smoothing
        window -- diagnostic only, does not drive the displayed label."""
        if not self._probs_history:
            return 0.0, 0.0
        tops = np.asarray([max(p.values()) for p in self._probs_history], dtype=np.float64)
        return float(tops.mean()), float(tops.std())

    def set_window_n(self, window_n: int):
        """Resize the smoothing window, keeping whatever history still fits
        (most recent first) rather than resetting -- so nudging the GUI
        spinbox doesn't blank the live display."""
        window_n = max(1, int(window_n))
        self.window_n = window_n
        while len(self._entries) > window_n:
            self._entries.popleft()

    def reset(self):
        self._entries.clear()
