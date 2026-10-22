"""Whether an Analysis snapshot can be replayed through the TouchID gating.

The replay path (``ReplayDriver``, the Analysis gate preview) scales counts with the engine's
default full scale and treats the timestamps as one continuous stream. A TestBoard capture
(other ADC bits / full scale) or a capture with timestamp gaps would be silently mis-scaled or
smeared across the gap, so those are refused up front instead. A legacy capture without board
context keeps replaying as before.
"""

from __future__ import annotations

import math

from constants.plotting import IADC_RESOLUTION_BITS
from core.piezo_engine.config import DEFAULT_VREF_VOLTAGE
from data_processing.acquisition_timing import gap_indices
from data_processing.analysis_workbench import AnalysisSourceSnapshot, source_adc_bits, source_voltage


def replay_unsupported_reason(snapshot: AnalysisSourceSnapshot) -> str | None:
    """A short human reason the snapshot cannot be replayed correctly, or None when it can."""
    return _board_reason(snapshot) or _timestamp_gap_reason(snapshot)


def _board_reason(snapshot: AnalysisSourceSnapshot) -> str | None:
    if snapshot.metadata.get("testboard_acquisition"):
        return "it is a TestBoard capture (replay assumes the default ADC scale)"
    if not snapshot.metadata.get("board_context"):
        return None  # a legacy capture replays on the default scale, as it always did
    is_default_bits = source_adc_bits(snapshot) == IADC_RESOLUTION_BITS
    is_default_scale = math.isclose(source_voltage(snapshot, DEFAULT_VREF_VOLTAGE), DEFAULT_VREF_VOLTAGE)
    if is_default_bits and is_default_scale:
        return None
    return "its ADC resolution or full-scale voltage differs from the default replay scale"


def _timestamp_gap_reason(snapshot: AnalysisSourceSnapshot) -> str | None:
    gap_count = len(gap_indices(snapshot.timestamps_s))
    if not gap_count:
        return None
    return f"its timestamps contain {gap_count} gap(s) (replay needs one continuous stream)"
