"""
Gating v2 live settings
=======================
A v2 model bundle carries the gating parameters it was trained behind. The live tab
starts from those values and lets the user override single fields; only the overrides
are persisted, so a later model keeps its own training values for every field the user
never touched. Pure data in, data out: no GUI, no texture_piezo import.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, replace

from core.texture_piezo.gating.v2.config import GatingV2Config

GATING_V2_VERSION = "v2"
GATING_V2_FIELD_NAMES = tuple(field.name for field in fields(GatingV2Config))


def config_from_bundle_params(params: Mapping) -> GatingV2Config:
    """The GatingV2Config of a bundle's ``gating_params`` dict (its ``version`` key is not a field)."""
    return GatingV2Config(**{name: params[name] for name in GATING_V2_FIELD_NAMES})


def apply_overrides(trained: GatingV2Config, overrides: Mapping) -> GatingV2Config:
    return replace(trained, **overrides)


def differing_fields(live: GatingV2Config, trained: GatingV2Config) -> list[str]:
    return [name for name in GATING_V2_FIELD_NAMES if getattr(live, name) != getattr(trained, name)]


def overrides_of(live: GatingV2Config, trained: GatingV2Config) -> dict:
    return {name: getattr(live, name) for name in differing_fields(live, trained)}
