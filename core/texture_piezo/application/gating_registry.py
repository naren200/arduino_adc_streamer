"""Gating version registry: maps a model family's gating version to its strategy factory."""

from __future__ import annotations

from .gating_strategy import GatingFactory
from .v1_gating import V1Gating
from .v2_gating import V2Gating

_FACTORIES: dict[str, GatingFactory] = {"v1": V1Gating, "v2": V2Gating}


def get_gating(version: str) -> GatingFactory:
    try:
        return _FACTORIES[version]
    except KeyError:
        raise ValueError(f"unknown gating version {version!r}; available: {sorted(_FACTORIES)}") from None
