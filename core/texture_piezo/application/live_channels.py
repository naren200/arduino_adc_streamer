"""Engine outputs -> the named channels of a ``LiveWindow``.

The engine emits its derived channels under structural keys (``derived["integrated"][column]``,
``derived["shear_jerk_lr"]``, ...) and the raw channels under whichever sensor board is
streaming (``PZT5_B``). A model asks for engine channel NAMES (``PZT3_B``,
``PZT3_B_integrated``, ``shear_jerk_lr``, ``normal_force``); this module is the one place
that maps between the two, so no positional layout ever reaches a model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

from core.piezo_engine.channel_names import (
    CHANNEL_LABELS,
    FORCE_COLUMNS,
    INTEGRATED_COLUMN_SUFFIX,
    INTEGRATED_COLUMNS,
    PZT_COLUMNS,
    SHEAR_NORMAL_COLUMNS,
)

# What a model that declares nothing more is handed: every non-force engine channel.
DEFAULT_CHANNELS: tuple[str, ...] = PZT_COLUMNS + INTEGRATED_COLUMNS + SHEAR_NORMAL_COLUMNS
_BOARD_SEPARATOR = "_"


def needs_force(channel_names: Sequence[str]) -> bool:
    return any(name in FORCE_COLUMNS for name in channel_names)


def engine_names_by_sensor_column(pzt_columns: Sequence[str]) -> dict[str, str]:
    """``{"PZT5_B": "PZT3_B", ...}``: each streamed column onto the engine's canonical raw
    name, matched by its B/L/C/R/T suffix (never by position)."""
    mapping = {}
    for column in pzt_columns:
        suffix = column.rsplit(_BOARD_SEPARATOR, 1)[-1]
        if suffix not in CHANNEL_LABELS:
            raise ValueError(f"PZT column {column!r} does not end in one of {list(CHANNEL_LABELS)}")
        mapping[column] = PZT_COLUMNS[CHANNEL_LABELS.index(suffix)]
    if len(set(mapping.values())) != len(mapping):
        raise ValueError(f"PZT columns {list(pzt_columns)} map several columns onto one engine channel")
    return mapping


def named_engine_channels(
    pzt_columns: Sequence[str], raw: Mapping[str, np.ndarray], derived: Mapping, names: Sequence[str],
) -> dict[str, np.ndarray]:
    """The engine channels called ``names`` as float64 arrays (integer ADC counts are converted).

    ``raw`` is keyed by the streamed sensor columns, ``derived`` is the dict
    ``DerivedChannelPipeline.process`` returns. KeyError when a requested channel was not
    produced (force asked for from an engine running with ``compute_force=False``)."""
    engine_by_column = engine_names_by_sensor_column(pzt_columns)
    available = {engine: raw[column] for column, engine in engine_by_column.items()}
    available.update({
        engine + INTEGRATED_COLUMN_SUFFIX: derived["integrated"][column] for column, engine in engine_by_column.items()
    })
    available.update({name: derived[name] for name in SHEAR_NORMAL_COLUMNS + FORCE_COLUMNS if name in derived})
    missing = [name for name in names if name not in available]
    if missing:
        raise KeyError(
            f"the engine produced no channel(s) {missing}; a model that reads force needs an engine "
            "running with compute_force=True"
        )
    return {name: np.asarray(available[name], dtype=np.float64) for name in names}

