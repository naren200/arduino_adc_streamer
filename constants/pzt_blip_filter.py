"""Defaults for the PZT ADC single-sample blip filter.

The window bounds and normalisation are defined by the piezo engine
(``core/piezo_engine/median.py``) and re-exported here.
"""

from core.piezo_engine.median import (
    DEFAULT_MEDIAN_WINDOW_SAMPLES as PZT_BLIP_FILTER_DEFAULT_WINDOW_SAMPLES,
    MEDIAN_WINDOW_MAX_SAMPLES as PZT_BLIP_FILTER_MAX_WINDOW_SAMPLES,
    MEDIAN_WINDOW_MIN_SAMPLES as PZT_BLIP_FILTER_MIN_WINDOW_SAMPLES,
    normalize_median_window as normalize_pzt_blip_filter_window,
)

PZT_BLIP_FILTER_DEFAULT_ENABLED = True
