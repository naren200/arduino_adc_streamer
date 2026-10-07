"""PZT force reconstruction constants.

Force defaults and unit constants are defined in the piezo engine
(``core/piezo_engine/force_settings.py``) and re-exported here; only
GUI-only constants are defined in this module.
"""

from core.piezo_engine.force_settings import (  # noqa: F401  (re-exports)
    ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS,
    PZT_FORCE_DEFAULT_SETTINGS,
    PZT_FORCE_MAD_TO_SIGMA,
    PZT_FORCE_NOISE_PERCENTILE,
    PZT_FORCE_PIC_COULOMB_TO_COULOMB,
)

PZT_FORCE_CAPACITANCE_UNITS = ("pF", "nF", "F")
PZT_FORCE_MUX_TIMING_MODES = ("Auto", "Manual", "Infer from total sample rate", "Continuous")
PZT_FORCE_DEFAULT_MUX_CONNECTED_TIME_S = 0.030
