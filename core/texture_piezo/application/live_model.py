"""
Live model <-> engine binding
==============================
What the live TouchID path takes from a loaded model runtime: the EngineConfig its
processors must run (so live features are computed exactly as the model was trained),
the engine channels it reads, and the warning owed when the GUI's own ingest blip
filter conflicts with what the model expects. GUI-free so each is unit-testable.

``model`` below is anything with the runtime contract's ``engine_config`` (a dict, or
None for a hand-crafted-feature model) and ``required_channels``; ``expected_ingest_blip_filter`` is
optional (hand-crafted-feature runtimes lack it).
"""

from __future__ import annotations

from core.piezo_engine.channel_names import FORCE_COLUMNS
from core.piezo_engine.config import EngineConfig, EngineConfigMismatchError
from core.texture_piezo.application.live_channels import DEFAULT_CHANNELS, needs_force
from core.texture_piezo.application.stream_processor import resolve_live_engine_config


def engine_config_for_model(model) -> EngineConfig:
    """The EngineConfig the live processors run for ``model``.

    A self-describing model supplies its own config dict, which must be rebuildable by
    this engine (else EngineConfigMismatchError, so the model is refused) and must
    compute the force channels the model reads. Without a model -- or for a
    hand-crafted-feature runtime, whose ``engine_config`` is None -- the default force-less config runs."""
    if model is None or model.engine_config is None:
        return resolve_live_engine_config()
    try:
        config = EngineConfig.from_dict(model.engine_config)
    except (KeyError, ValueError, TypeError) as exc:
        raise EngineConfigMismatchError(f"the model's engine config cannot be rebuilt by this engine: {exc}") from exc
    require_engine_supports_model(model, config)
    return config


def required_channels_for_model(model) -> tuple[str, ...]:
    """The engine channels the model reads; every non-force channel when no model is loaded."""
    return DEFAULT_CHANNELS if model is None else tuple(model.required_channels)


def require_engine_supports_model(model, engine_config: EngineConfig) -> None:
    """Refuse a model that reads force channels when ``engine_config`` computes none (the
    window could never be assembled)."""
    if needs_force(model.required_channels) and not engine_config.compute_force:
        raise EngineConfigMismatchError(
            f"model reads {[n for n in model.required_channels if n in FORCE_COLUMNS]} but the "
            "live engine config has compute_force=False"
        )


def ingest_filter_warning(model, ingest_filter_enabled: bool) -> str | None:
    """Warning text when the model was trained WITHOUT an upstream blip filter
    (the engine applies its own median-N) but the GUI ingest blip filter is on, so
    live data would be despiked twice; None when there is nothing to warn about, including
    for a runtime that does not say what it expects (every hand-crafted-feature one)."""
    expected = getattr(model, "expected_ingest_blip_filter", None)
    if expected is None or expected or not ingest_filter_enabled:
        return None
    return (
        "the GUI ingest blip filter is ON but this model was trained without it (the engine already "
        "median-filters); live data is despiked twice and predictions may drift -- turn the PZT blip "
        "filter off"
    )
