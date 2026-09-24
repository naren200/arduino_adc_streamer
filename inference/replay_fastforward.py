"""Fast-forward toggle for the 'Run on Analysis Source' replay.

A tiny standalone class (rather than a bare bool on InferencePanelMixin) so
the on/off bookkeeping for skipping replay's per-tick animation lives in one
place and can be reused by whatever triggers a replay -- the toolbar
checkbox today, potentially a scripted/batch run later.
"""


class ReplayFastForward:
    """Tracks whether an in-progress or about-to-start replay should skip
    its per-tick animation (stream plot redraw, inference region highlight)
    and run at full speed instead."""

    def __init__(self, enabled: bool = False):
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def should_render_tick(self) -> bool:
        """Whether a replay tick should push its animation frame. False
        while fast-forward is enabled, so callers skip the expensive plot
        redraw and just keep classifying as fast as the worker allows."""
        return not self._enabled
