"""Pure scheduling rule for GUI refreshes that can be slower than their target rate."""

from __future__ import annotations


def next_refresh_allowed_at(
    last_finished_at: float | None,
    last_duration: float,
    min_interval: float,
    duty_factor: float,
) -> float:
    """Return the earliest clock time at which the next refresh may start.

    Spacing grows with the cost of the last refresh so that the refresh uses
    at most ``1 / duty_factor`` of the GUI thread; cheap refreshes fall back
    to ``min_interval``. ``None`` means no refresh has finished yet.
    """
    if last_finished_at is None:
        return float("-inf")
    return last_finished_at + max(min_interval, duty_factor * last_duration)
