"""PZT force reconstruction defaults and unit constants."""

PZT_FORCE_DEFAULT_SETTINGS = {
    "enabled": False,
    "capacitance_value": 150.0,
    "capacitance_unit": "pF",
    "rleak_ohm": 1_000_000.0,
    "d33_pc_per_n": 600.0,
    "noise_threshold_v": 0.01,
    "quiet_duration_s": 2.0,
    "noise_sigma_multiplier": 5.0,
    "channel_calibration": {},
}

PZT_FORCE_CAPACITANCE_UNITS = ("pF", "nF", "F")
PZT_FORCE_PIC_COULOMB_TO_COULOMB = 1e-12
PZT_FORCE_MAD_TO_SIGMA = 1.4826
PZT_FORCE_NOISE_PERCENTILE = 95.0
