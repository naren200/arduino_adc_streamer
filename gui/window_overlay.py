"""Geometry for the Analysis-tab predicted-window overlay (pure, no Qt)."""
from __future__ import annotations

import numpy as np

Span = tuple[float, float]


def window_stretches(window_ts, seam_idx) -> list[Span]:
    """Every contiguous stretch of a window, split at its seams (full extent)."""
    parts = np.split(np.asarray(window_ts, dtype=np.float64), list(seam_idx))
    return [(float(part[0]), float(part[-1])) for part in parts if part.size]


def first_hop_tiles(window_ts, seam_idx, hop_samples: int) -> list[Span]:
    """The stretches covering a window's first ``hop_samples`` samples.

    Overlapping windows (hop << window) share all but their first hop, so that first hop
    is the only part unique to the window: drawing only it tiles the timeline without
    overlap. A tile ends where the next sample starts so consecutive tiles abut; it may
    cross a seam, giving one span per stretch it touches.
    """
    remaining = int(hop_samples)
    tiles: list[Span] = []
    for part in np.split(np.asarray(window_ts, dtype=np.float64), list(seam_idx)):
        if remaining <= 0:
            break
        if not part.size:
            continue
        take = min(remaining, part.size)
        end = part[take] if take < part.size else part[-1]
        tiles.append((float(part[0]), float(end)))
        remaining -= take
    return tiles


def window_id_at(predictions: list[dict], seconds: float) -> int | None:
    """Id of the window whose drawn tile contains ``seconds`` (None if over idle)."""
    for prediction in predictions:
        if prediction["start_s"] <= seconds <= prediction["end_s"]:
            return prediction["window_id"]
    return None
