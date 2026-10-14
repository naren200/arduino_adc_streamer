"""Names of the channels the piezo engine emits: the single definition.

Stdlib only. Every consumer (window layouts, ``LiveWindow``, the texture_piezo
model runtimes) reads names from here, so a channel is never spelled twice.
"""

from __future__ import annotations

PZT_COLUMNS: tuple[str, ...] = ("PZT3_B", "PZT3_L", "PZT3_C", "PZT3_R", "PZT3_T")
CHANNEL_LABELS: tuple[str, ...] = ("B", "L", "C", "R", "T")  # positional, matches PZT_COLUMNS
INTEGRATED_COLUMN_SUFFIX = "_integrated"
INTEGRATED_COLUMNS: tuple[str, ...] = tuple(f"{column}{INTEGRATED_COLUMN_SUFFIX}" for column in PZT_COLUMNS)
SHEAR_NORMAL_COLUMNS: tuple[str, ...] = ("shear_jerk_lr", "shear_jerk_tb", "normal_jerk")
NORMAL_JERK_RESIDUE_PREFIX = "normal_jerk_residue_"
NORMAL_JERK_RESIDUE_COLUMNS: tuple[str, ...] = tuple(
    f"{NORMAL_JERK_RESIDUE_PREFIX}{label}" for label in CHANNEL_LABELS
)  # shear-removed per-sensor residual, positional like PZT_COLUMNS (B, L, C, R, T)
FORCE_COLUMNS: tuple[str, ...] = ("shear_force_lr", "shear_force_tb", "normal_force")

ENGINE_CHANNEL_NAMES: frozenset[str] = frozenset(
    PZT_COLUMNS + INTEGRATED_COLUMNS + SHEAR_NORMAL_COLUMNS + NORMAL_JERK_RESIDUE_COLUMNS + FORCE_COLUMNS
)
