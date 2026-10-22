"""
Strength Stage
==============
Good signal = stretches where ``|normal_jerk|`` bursts repeatedly. The stream is cut
into fixed blocks; a block is STRONG when its burst score (the plain sum of
``|normal_jerk|`` over its samples) exceeds ``burst_min``, and GOOD when more than
``sustain_min`` of the last ``sustain_blocks`` blocks are strong. A good stretch
starts at the earliest strong block of the lookback that tripped it and ends at the
end of its last strong block, so a range never ends on a trailing quiet block.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from .config import GatingV2Config
from .ranges import IndexRange


def block_length_samples(block_s: float, fs: float) -> int:
    return max(1, round(block_s * fs))


def block_burst_scores(normal_jerk: np.ndarray, block_n: int) -> np.ndarray:
    """Plain sum (no mean, no normalisation) of ``|normal_jerk|`` per whole block."""
    whole = len(normal_jerk) // block_n * block_n
    return np.abs(normal_jerk[:whole]).reshape(-1, block_n).sum(axis=1)


def is_strong_block(scores, burst_min: float):
    """Strictly above ``burst_min`` (works on one score or an array of them)."""
    return scores > burst_min


@dataclass(frozen=True)
class _Block:
    start_idx: int
    end_idx: int
    is_strong: bool


class StrongSignalStage:
    def __init__(self, config: GatingV2Config) -> None:
        self._config = config
        self._recent_blocks: deque[_Block] = deque(maxlen=config.sustain_blocks)
        self._carry = np.empty(0)  # samples of the block still being filled, kept across ticks
        self._next_block_start = 0
        self._emitted_end = 0
        self._is_sustained = False

    def feed(self, normal_jerk: np.ndarray, fs: float) -> list[IndexRange]:
        """Consume one tick's ``normal_jerk`` and return the newly known good pieces:
        each is a stretch not reported before, in stream order."""
        block_n = block_length_samples(self._config.block_s, fs)
        values = np.concatenate([self._carry, normal_jerk])
        scores = block_burst_scores(values, block_n)
        self._carry = values[len(scores) * block_n:]
        pieces = (self._advance(float(score), block_n) for score in scores)
        return [piece for piece in pieces if piece is not None]

    def _advance(self, score: float, block_n: int) -> IndexRange | None:
        start = self._next_block_start
        self._next_block_start = start + block_n
        self._recent_blocks.append(_Block(start, start + block_n, bool(is_strong_block(score, self._config.burst_min))))
        strong = [block for block in self._recent_blocks if block.is_strong]
        if len(strong) <= self._config.sustain_min:
            self._is_sustained = False
            return None
        piece_start = self._emitted_end
        if not self._is_sustained:
            self._is_sustained = True
            piece_start = max(strong[0].start_idx, self._emitted_end)
        piece_end = strong[-1].end_idx
        if piece_end <= piece_start:
            return None
        self._emitted_end = piece_end
        return piece_start, piece_end
