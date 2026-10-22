"""Min/max envelope decimation so tens of millions of points can be shown and zoomed interactively."""
from __future__ import annotations

import numpy as np


def envelope(t: np.ndarray, v: np.ndarray, x0: float, x1: float, n_buckets: int = 1500) -> tuple[np.ndarray, np.ndarray]:
    """Return (x, y) for plotting the visible window [x0, x1].

    Small windows return the raw points. Large windows return, per pixel bucket, the minimum and the maximum so spikes
    survive decimation (x repeated twice, y interleaved min/max).
    """
    if len(t) == 0:
        return np.zeros(0), np.zeros(0)
    i0 = int(np.searchsorted(t, x0, side="left"))
    i1 = int(np.searchsorted(t, x1, side="right"))
    i0, i1 = max(0, i0 - 1), min(len(t), i1 + 1)
    if i1 - i0 <= 4 * n_buckets:
        return t[i0:i1], v[i0:i1]
    seg_t = t[i0:i1]
    edges = np.linspace(seg_t[0], seg_t[-1], n_buckets + 1)
    idx = np.searchsorted(seg_t, edges[:-1], side="left")
    idx = np.unique(idx[idx < len(seg_t)])
    seg = v[i0:i1]
    lo = np.minimum.reduceat(seg, idx)
    hi = np.maximum.reduceat(seg, idx)
    x = np.repeat(seg_t[idx], 2)
    y = np.empty(2 * len(idx))
    y[0::2], y[1::2] = lo, hi
    return x, y
