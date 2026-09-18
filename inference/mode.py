"""
TouchID Mode
============
Which non-default activity the TouchID tab is currently doing, beyond plain
live streaming. Deliberately does NOT have a LIVE_STREAMING (or similar)
member -- "are we live streaming" is already owned by the GUI's own
`is_capturing` flag (consulted by should_update_touchid_display()), and
duplicating that fact here would let the two states desync (e.g. is_capturing
True but touchid_mode claiming something else). NORMAL simply means "none of
the special activities below are in progress", live streaming included.

touchid_worker_busy (owned by InferencePanelMixin, not this enum) is a
separate concern: it's the classify worker's own backpressure flag, tracking
whether a submitted window's result is still pending, orthogonal to what
triggered the window in the first place.
"""

from __future__ import annotations

from enum import Enum, auto


class TouchIdMode(Enum):
    NORMAL = auto()
    CAPTURING_BASELINE = auto()
    REPLAYING = auto()
