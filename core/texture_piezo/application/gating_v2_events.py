"""
Gating v2 structured events
===========================
The v2 core (good queue, stages, combiner) never logs. V2Gating, the edge, reports what the
queue changed and which windows it emitted through the ``texture_piezo.gating.v2`` logger at
DEBUG. The message is the event name; every field travels in ``extra``, never in a formatted string.
"""

from __future__ import annotations

import logging

from core.texture_piezo.gating.v2.good_queue import GoodRange, QueueChanges

from .gating_strategy import ReadyWindow

logger = logging.getLogger("texture_piezo.gating.v2")

RANGE_OPENED = "range_opened"
RANGE_EXTENDED = "range_extended"
RANGE_MERGED = "range_merged"
RANGE_EXPIRED = "range_expired"
WINDOW_EMITTED = "window_emitted"
GATE_WAITING_FOR_BASELINE = "gate_waiting_for_baseline"


def is_enabled() -> bool:
    return logger.isEnabledFor(logging.DEBUG)


def log_queue_changes(changes: QueueChanges) -> None:
    for event, ranges in (
        (RANGE_OPENED, changes.opened), (RANGE_MERGED, changes.merged),
        (RANGE_EXTENDED, changes.extended), (RANGE_EXPIRED, changes.expired),
    ):
        for good in ranges:
            logger.debug(event, extra=_range_fields(good))


def log_window(window: ReadyWindow, range_ids: tuple[int, ...]) -> None:
    sweep_ts = window.window_ts
    logger.debug(WINDOW_EMITTED, extra={
        "range_ids": range_ids,
        "n_ranges": len(window.ranges),
        "n_seams": len(window.window.seam_idx),
        "good_duration_s": window.window.duration_s,
        "first_sweep_ts": float(sweep_ts[0]),
        "last_sweep_ts": float(sweep_ts[-1]),
    })


def log_waiting_for_baseline() -> None:
    logger.debug(GATE_WAITING_FOR_BASELINE)


def _range_fields(good: GoodRange) -> dict:
    return {
        "range_id": good.range_id, "start_idx": good.start_idx, "end_idx": good.end_idx,
        "start_ts": good.start_ts, "end_ts": good.end_ts,
    }
