"""
Inference Config
=================
Configuration for the TouchID inference pipeline. Sizing constants here are
intentionally independent literals, decoupled from texture_piezo's training
config (WINDOW_SIZE_S / HOP_SIZE_S in clip_windowing_utils_v1.py) so that
inference sizing can be tuned without affecting training.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import model_discovery
from ._paths import TEXTURE_PIEZO_MODELS, TEXTURE_PIEZO_SRC
from .model_discovery import DEFAULT_CLASS_NAMES
from file_operations.settings_persistence import load_settings_payload, save_settings_payload

sys.path.insert(0, str(TEXTURE_PIEZO_SRC))
from clip_windowing_utils_v1 import CHANNEL_LABELS  # noqa: E402
from touchid_inference.quality_gate import DEFAULT_K  # noqa: E402

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

def _default_path(model_type: str) -> str:
    """Newest loadable checkpoint for `model_type`, or "" when none is on disk.

    Empty rather than a guessed filename: a path that doesn't exist fails at
    load time with a confusing FileNotFoundError, while "" makes it obvious
    that discovery found nothing for this architecture.
    """
    artifacts = model_discovery.newest_artifacts(model_type)
    return str(artifacts.checkpoint_path) if artifacts else ""


def _default_sidecar(attr: str) -> str:
    """The named sidecar belonging to the default architecture's own default
    checkpoint, or "" when that architecture doesn't use one.

    Deliberately does not fall back to another architecture's sidecar: these
    fields are only read by the architecture they belong to, and borrowing
    ANN's scaler while penta is active would put a misleading path in the
    saved settings file. Switching architectures calls set_model_version,
    which fills them in from the newly selected checkpoint.
    """
    artifacts = model_discovery.newest_artifacts(DEFAULT_MODEL_TYPE)
    resolved = getattr(artifacts, attr, None) if artifacts else None
    return str(resolved) if resolved is not None else ""


DEFAULT_MODEL_TYPE = "penta"


@dataclass
class InferenceConfig:
    window_size_s: float = 0.5      # independent literal, NOT imported from training config
    hop_size_s: float = 0.1         # user-adjustable via GUI spinbox
    min_span_fill_ratio: float = 0.08  # UNUSED as of the fragment-stitching rewrite --
                                        # ActiveSampleQueue.expire() uses a single
                                        # FRAGMENT_MAX_AGE_S rule with no separate
                                        # fill-ratio grace period. Kept as a
                                        # TouchIdStreamProcessor constructor param for
                                        # now to avoid a wider call-site cleanup.
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
    model_type: str = DEFAULT_MODEL_TYPE  # "ann" | "cnn" | "quad" | "penta" — which architecture is active
    # Every model path below is discovered from TEXTURE_PIEZO_MODELS at
    # instantiation, never hardcoded to a filename: each defaults to the
    # newest loadable version's default checkpoint for that architecture
    # (see model_discovery.newest_artifacts). Drop a new checkpoint plus its
    # sidecars into the models folder and it becomes the default with no code
    # change; a checkpoint whose artifacts don't actually load is not offered.
    ann_model_path: str = field(default_factory=lambda: _default_path("ann"))
    cnn_model_path: str = field(default_factory=lambda: _default_path("cnn"))
    quad_model_path: str = field(default_factory=lambda: _default_path("quad"))
    penta_model_path: str = field(default_factory=lambda: _default_path("penta"))
    # Sidecars are resolved per version alongside the checkpoint they belong
    # to, so they track whichever architecture's default is sidecar-backed.
    scaler_path: str = field(default_factory=lambda: _default_sidecar("scaler_path"))
    raw_norm_stats_path: str = field(default_factory=lambda: _default_sidecar("raw_norm_stats_path"))
    # Frozen, ordered feature-name manifest the active checkpoint was trained
    # on. Resolved per version by discovery because texture_piezo has written
    # these under several naming conventions over time.
    feature_names_path: str = field(default_factory=lambda: _default_sidecar("feature_names_path"))
    class_names: list[str] = field(default_factory=lambda: list(DEFAULT_CLASS_NAMES))


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


def _model_path_attr(model_type: str) -> str:
    return f"{model_type}_model_path"


def discover_model_versions(model_type: str) -> list[str]:
    """Versions of `model_type` that are actually usable, newest last.

    "Usable" means the checkpoint and its resolved sidecars load -- see
    model_discovery, which probes each candidate instead of checking that
    files with the expected names exist.
    """
    seen = {found.artifacts.version for found in model_discovery.loadable(model_type)}
    return sorted(seen, key=model_discovery.version_sort_key)


def discover_checkpoints(model_type: str, version: str) -> list[str]:
    """Usable checkpoint tags for `model_type`'s `version` -- tags are free-form
    (e.g. "best_working_09_17_2026"), not a fixed enum. [CHECKPOINT_DEFAULT]
    alone means an untagged single-file version. Ordered CHECKPOINT_DEFAULT
    first, then CHECKPOINT_TAGS priority, then any other tag alphabetically."""
    return [
        found.artifacts.checkpoint
        for found in model_discovery.loadable(model_type)
        if found.artifacts.version == version
    ]


def unusable_models(model_type: str) -> list[tuple[str, str, str]]:
    """(version, checkpoint, error) for every checkpoint on disk that parses as
    `model_type` but does not load -- the reason it is absent from the
    dropdown. Surfaced so a checkpoint that silently fails to appear is
    diagnosable without reading discovery internals."""
    return [
        (found.artifacts.version, found.artifacts.checkpoint, found.error)
        for found in model_discovery.discover(model_type)
        if not found.is_loadable
    ]


def model_version_of(config: InferenceConfig) -> str | None:
    """Best-effort extraction of the version suffix (e.g. "v2b") config is currently set to."""
    artifacts = _current_artifacts(config)
    return artifacts.version if artifacts else None


def model_checkpoint_of(config: InferenceConfig) -> str:
    """Best-effort extraction of the checkpoint tag (e.g. "best") config is
    currently set to, or CHECKPOINT_DEFAULT for an untagged/unrecognized path."""
    artifacts = _current_artifacts(config)
    return artifacts.checkpoint if artifacts else model_discovery.CHECKPOINT_DEFAULT


def _current_artifacts(config: InferenceConfig):
    current = Path(getattr(config, _model_path_attr(config.model_type), ""))
    for found in model_discovery.discover(config.model_type):
        if found.artifacts.checkpoint_path == current:
            return found.artifacts
    return None


def set_model_version(config: InferenceConfig, version: str, checkpoint: str = "default") -> None:
    """Point config at the discovered artifacts for `version`/`checkpoint` of the
    currently active architecture (config.model_type), including whichever
    sidecars that version resolved to. Leaves other architectures' paths
    untouched so switching back to one doesn't lose its own version selection.

    Paths come from discovery rather than being rebuilt from a naming
    convention: the same architecture's checkpoints are not all named alike
    (texture_ann_v3.pt vs ann_v3b_best.pt), and rebuilding silently produced
    paths that did not exist.
    """
    artifacts = model_discovery.artifacts_for(config.model_type, version, checkpoint)
    if artifacts is None:
        # A version need not have an untagged checkpoint -- ann v3b exists only
        # as ann_v3b_best.pt. Callers that just pick a version (the GUI's
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
    setattr(config, _model_path_attr(artifacts.model_type), str(artifacts.checkpoint_path))
    for attr in ("scaler_path", "raw_norm_stats_path", "feature_names_path"):
        resolved = getattr(artifacts, attr)
        if resolved is not None:
            setattr(config, attr, str(resolved))


def _get_last_touchid_settings_path() -> Path:
    return Path.home() / ".adc_streamer" / "touchid" / "last_used_touchid_settings.json"


def save_touchid_settings(config: InferenceConfig) -> Path:
    """Persist user-overridable TouchID inference settings to disk."""
    payload = {
        "version": 1,
        TOUCHID_SETTINGS_PAYLOAD_KEY: {
            "window_size_s": config.window_size_s,
            "hop_size_s": config.hop_size_s,
            "min_span_fill_ratio": config.min_span_fill_ratio,
            "span_stale_timeout_s": config.span_stale_timeout_s,
            "pzt_columns": list(config.pzt_columns),
            "smoothing_window_n": config.smoothing_window_n,
            "guilty_clip_filter_enabled": config.guilty_clip_filter_enabled,
            "confidence_threshold": config.confidence_threshold,
            "idle_gate_k": config.idle_gate_k,
            "model_type": config.model_type,
            "model_version": model_version_of(config),
            "model_checkpoint": model_checkpoint_of(config),
        },
    }
    return save_settings_payload(_get_last_touchid_settings_path(), payload)


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
        if "min_span_fill_ratio" in payload:
            config.min_span_fill_ratio = payload["min_span_fill_ratio"]
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
        if "model_type" in payload and payload["model_type"] in model_discovery.ARCH_STEM_PREFIXES:
            config.model_type = payload["model_type"]
        version = payload.get("model_version")
        if version and version in discover_model_versions(config.model_type):
            checkpoint = payload.get("model_checkpoint") or "default"
            if checkpoint not in discover_checkpoints(config.model_type, version):
                checkpoint = "default"
            set_model_version(config, version, checkpoint)
    return config
