"""
Inference Config
=================
Configuration for the TouchID inference pipeline. Sizing constants here are
intentionally independent literals, decoupled from texture_piezo's training
window constants so that
inference sizing can be tuned without affecting training.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from core.texture_piezo.models import model_discovery
from file_operations.settings_persistence import load_settings_payload, save_settings_payload

from core.texture_piezo.gating.quality_gate import DEFAULT_K
from core.texture_piezo.gating.window_config import ONSET_SKIP_S

from core.piezo_engine.channel_names import CHANNEL_LABELS

# texture_piezo's channel-suffix order (B/L/C/R/T) for one PZT sensor board,
# independent of which physical sensor number it's wired up as -- see
# pzt_columns_for_sensor/pzt_sensor_number_of below. Different capture rigs
# label the same 5 physical channels with different board numbers (e.g.
# PZT3_*, PZT4_*, PZT5_*); texture_piezo's own CSV loader canonicalizes
# those onto PZT3_* at ingestion (drag_detection_utils_v1._canonicalize_pzt_columns),
# but the live/offline GUI pipeline works with whichever board is actually
# streaming, so it needs to build the right column names itself.
DEFAULT_PZT_SENSOR_NUMBER = "5"

TOUCHID_SETTINGS_PAYLOAD_KEY = "touchid_settings"
MODEL_FALLBACK_LOG_EVENT = "touchid_saved_model_fallback"

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class ModelSelection:
    """Which discovered checkpoint of one architecture is selected. Paths are never stored:
    discovery resolves the checkpoint and its sidecars from this at load time."""

    version: str
    checkpoint: str


def _default_selections() -> dict[str, ModelSelection]:
    """The default checkpoint of every architecture that has a loadable one (see model_discovery.default_for_family)."""
    defaults = {key: model_discovery.default_for_family(key) for key in model_discovery.family_keys()}
    return {key: _selection_of(artifacts) for key, artifacts in defaults.items() if artifacts}


def _selection_of(artifacts) -> ModelSelection:
    return ModelSelection(artifacts.version, artifacts.checkpoint)


def default_model_type() -> str:
    """The family of the default model (model_discovery.default_model); the empty string when nothing is loadable."""
    default = model_discovery.default_model().artifacts
    return default.model_type if default else ""


@dataclass
class InferenceConfig:
    window_size_s: float = 0.5      # independent literal, NOT imported from training config
    hop_size_s: float = 0.1         # user-adjustable via GUI spinbox
    span_stale_timeout_s: float = 1.0  # TouchIdStreamProcessor._trim_store's safety
                                        # margin only -- fragment expiry itself is now
                                        # governed by segmentation.FRAGMENT_MAX_AGE_S,
                                        # not this value. Short (leftover-remainder)
                                        # fragments are instead padded out to a full
                                        # window using history -- see
                                        # inference/window_padding.py.
    pzt_columns: list[str] = field(default_factory=lambda: pzt_columns_for_sensor(DEFAULT_PZT_SENSOR_NUMBER))
    smoothing_window_n: int = 5     # WindowedVoteSmoother majority-vote/median window size, user-adjustable
    guilty_clip_filter_enabled: bool = True  # smoothing.is_guilty_candidate gate on the smoother's vote, user-toggleable
    confidence_threshold: float = 0.6  # bar-chart gray/red cutoff + latched "last detected" gate, user-adjustable
    idle_gate_k: float = DEFAULT_K  # quality_gate.chunk_is_active band-width multiplier, user-adjustable --
                                     # higher = only stronger-than-idle-noise signals get inferenced
    onset_skip_s: float = ONSET_SKIP_S  # ActiveSampleQueue's fresh-onset settling-transient skip, user-adjustable --
                                         # NOT re-applied when a touch merely resumes after a brief dip
    model_type: str = field(default_factory=default_model_type)  # a catalog family key — which architecture is active
    # Selected checkpoint per architecture, discovered at instantiation, never hardcoded to a
    # filename: each defaults to model_discovery.default_for_family (the explicit default pointer for its
    # family, else the first loadable bundle in stable order). An architecture with nothing loadable has
    # no entry. Switching `model_type` needs nothing else: the bundle is resolved from the selection by
    # discovery on each load.
    selections: dict[str, ModelSelection] = field(default_factory=_default_selections)
    # Set by load_touchid_settings when the saved model selection could not be
    # restored; empty when the selection was restored or nothing was saved.
    model_fallback_message: str = ""

    @property
    def class_names(self) -> list[str]:
        """The class order of the selected model's bundle; empty when the active architecture has no loadable model."""
        selection = self.selections.get(self.model_type)
        artifacts = model_discovery.artifacts_for(self.model_type, selection.version, selection.checkpoint) if selection else None
        return list(artifacts.class_names) if artifacts else []


def pzt_columns_for_sensor(sensor_number: str) -> list[str]:
    """Build pzt_columns for physical PZT sensor board `sensor_number`
    (e.g. "4", "5"), e.g. ["PZT4_B", "PZT4_L", "PZT4_C", "PZT4_R", "PZT4_T"]."""
    return [f"PZT{sensor_number}_{suffix}" for suffix in CHANNEL_LABELS]


def pzt_sensor_number_of(pzt_columns: list[str]) -> str:
    """Best-effort extraction of the sensor board number pzt_columns was built for."""
    if pzt_columns:
        prefix = pzt_columns[0].split('_', 1)[0]
        if prefix.startswith('PZT'):
            return prefix[len('PZT'):]
    return DEFAULT_PZT_SENSOR_NUMBER


def discover_model_versions(model_type: str) -> list[str]:
    """Weights versions of `model_type` that are actually usable, in natural order.

    "Usable" means the bundle loads -- see model_discovery, which probes each
    candidate instead of trusting that its header parses.
    """
    seen = {found.artifacts.version for found in model_discovery.loadable(model_type)}
    return sorted(seen, key=model_discovery.version_sort_key)


def discover_checkpoints(model_type: str, version: str) -> list[str]:
    """Usable checkpoint tags for `model_type`'s `version` -- tags are free-form
    (e.g. "best_working_09_17_2026"), not a fixed enum. [CHECKPOINT_DEFAULT]
    alone means a single-checkpoint version. Ordered CHECKPOINT_DEFAULT
    first, then the other tags in natural order."""
    return [
        found.artifacts.checkpoint
        for found in model_discovery.loadable(model_type)
        if found.artifacts.version == version
    ]


def model_version_of(config: InferenceConfig) -> str | None:
    """The version (e.g. "v2b") selected for the active architecture, or None when it has no loadable model."""
    selection = config.selections.get(config.model_type)
    return selection.version if selection else None


def model_checkpoint_of(config: InferenceConfig) -> str:
    """The checkpoint tag (e.g. "best") selected for the active architecture, CHECKPOINT_DEFAULT when untagged or unselected."""
    selection = config.selections.get(config.model_type)
    return selection.checkpoint if selection else model_discovery.CHECKPOINT_DEFAULT


def set_model_version(config: InferenceConfig, version: str, checkpoint: str = "default") -> None:
    """Select `version`/`checkpoint` of the currently active architecture (config.model_type).
    Leaves other architectures' selections untouched so switching back to one doesn't lose it.

    The selection is validated against discovery (the identity in each bundle's header).
    """
    artifacts = model_discovery.artifacts_for(config.model_type, version, checkpoint)
    if artifacts is None:
        # A version need not have an untagged checkpoint -- ann v3b exists only
        # as the tagged "best". Callers that just pick a version (the GUI's
        # Version combo) pass the "default" tag, so fall back to that version's
        # highest-priority available tag rather than rejecting the selection.
        available = discover_checkpoints(config.model_type, version)
        if not available:
            raise ValueError(
                f"No usable checkpoint for model_type={config.model_type!r} version={version!r}"
            )
        artifacts = model_discovery.artifacts_for(config.model_type, version, available[0])
    _apply_artifacts(config, artifacts)


def _apply_artifacts(config: InferenceConfig, artifacts) -> None:
    config.selections[artifacts.model_type] = _selection_of(artifacts)


def _get_last_touchid_settings_path() -> Path:
    return Path.home() / ".adc_streamer" / "touchid" / "last_used_touchid_settings.json"


def save_touchid_settings(config: InferenceConfig) -> Path:
    """Persist user-overridable TouchID inference settings to disk."""
    payload = {
        "version": 1,
        TOUCHID_SETTINGS_PAYLOAD_KEY: {
            "window_size_s": config.window_size_s,
            "hop_size_s": config.hop_size_s,
            "span_stale_timeout_s": config.span_stale_timeout_s,
            "pzt_columns": list(config.pzt_columns),
            "smoothing_window_n": config.smoothing_window_n,
            "guilty_clip_filter_enabled": config.guilty_clip_filter_enabled,
            "confidence_threshold": config.confidence_threshold,
            "idle_gate_k": config.idle_gate_k,
            "onset_skip_s": config.onset_skip_s,
            "model_type": config.model_type,
            "model_version": model_version_of(config),
            "model_checkpoint": model_checkpoint_of(config),
        },
    }
    return save_settings_payload(_get_last_touchid_settings_path(), payload)


@dataclass(frozen=True)
class SavedModelSelection:
    model_type: str | None
    version: str | None
    checkpoint: str | None

    @classmethod
    def from_payload(cls, payload: dict) -> "SavedModelSelection":
        return cls(payload.get("model_type"), payload.get("model_version"), payload.get("model_checkpoint"))


def _restore_saved_model_selection(config: InferenceConfig, saved: SavedModelSelection) -> None:
    """Apply the saved model type/version/checkpoint by name. A selection that no
    longer resolves keeps the default model instead of raising, and is reported
    through a WARNING log event and `config.model_fallback_message`."""
    if saved.model_type is not None and saved.model_type not in model_discovery.family_keys():
        _use_default_model(config, saved)
        return
    if saved.model_type is not None:
        config.model_type = saved.model_type
    if not saved.version:
        return
    available = discover_checkpoints(config.model_type, saved.version)
    # "default" is tolerated even when the version has only tagged checkpoints:
    # set_model_version then picks that version's highest-priority tag.
    is_checkpoint_missing = saved.checkpoint not in (None, model_discovery.CHECKPOINT_DEFAULT, *available)
    if not available or is_checkpoint_missing:
        _use_default_model(config, saved)
        return
    set_model_version(config, saved.version, saved.checkpoint or model_discovery.CHECKPOINT_DEFAULT)


def _use_default_model(config: InferenceConfig, saved: SavedModelSelection) -> None:
    default = model_discovery.default_for_family(config.model_type)
    if default is not None:
        _apply_artifacts(config, default)
    fallback_version = default.version if default else None
    fallback_description = f"default {config.model_type} {fallback_version}" if default else f"no loadable {config.model_type} model"
    logger.warning(
        MODEL_FALLBACK_LOG_EVENT,
        extra={
            "saved_type": saved.model_type,
            "saved_version": saved.version,
            "saved_checkpoint": saved.checkpoint,
            "fallback_version": fallback_version,
        },
    )
    config.model_fallback_message = (
        f"Saved model {saved.model_type}/{saved.version}/{saved.checkpoint} is unavailable; "
        f"using {fallback_description}"
    )


def load_touchid_settings(config: InferenceConfig | None = None) -> InferenceConfig:
    """Load user-overridable TouchID inference settings, applying them onto `config`.

    If no saved settings exist, returns `config` unchanged (or a fresh default
    InferenceConfig if `config` was not provided).
    """
    if config is None:
        config = InferenceConfig()

    path = _get_last_touchid_settings_path()
    if not path.exists():
        return config

    _path, payload = load_settings_payload(path, payload_key=TOUCHID_SETTINGS_PAYLOAD_KEY)
    if isinstance(payload, dict):
        if "window_size_s" in payload:
            config.window_size_s = payload["window_size_s"]
        if "hop_size_s" in payload:
            config.hop_size_s = payload["hop_size_s"]
        if "span_stale_timeout_s" in payload:
            config.span_stale_timeout_s = payload["span_stale_timeout_s"]
        if "pzt_columns" in payload:
            config.pzt_columns = list(payload["pzt_columns"])
        if "smoothing_window_n" in payload:
            config.smoothing_window_n = payload["smoothing_window_n"]
        if "guilty_clip_filter_enabled" in payload:
            config.guilty_clip_filter_enabled = bool(payload["guilty_clip_filter_enabled"])
        if "confidence_threshold" in payload:
            config.confidence_threshold = payload["confidence_threshold"]
        if "idle_gate_k" in payload:
            config.idle_gate_k = payload["idle_gate_k"]
        if "onset_skip_s" in payload:
            config.onset_skip_s = payload["onset_skip_s"]
        _restore_saved_model_selection(config, SavedModelSelection.from_payload(payload))
    return config
