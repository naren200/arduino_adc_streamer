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

from ._paths import TEXTURE_PIEZO_MODELS, TEXTURE_PIEZO_SRC
from file_operations.settings_persistence import load_settings_payload, save_settings_payload

sys.path.insert(0, str(TEXTURE_PIEZO_SRC))
from clip_windowing_utils_v1 import CHANNEL_LABELS  # noqa: E402
from clip_windowing_utils_v1 import PZT_COLUMNS as _DEFAULT_PZT_COLUMNS  # noqa: E402

# texture_piezo's channel-suffix order (B/L/C/R/T) for one PZT sensor board,
# independent of which physical sensor number it's wired up as -- see
# pzt_columns_for_sensor/pzt_sensor_number_of below. Different capture rigs
# label the same 5 physical channels with different board numbers (e.g.
# PZT3_*, PZT4_*, PZT5_*); texture_piezo's own CSV loader canonicalizes
# those onto PZT3_* at ingestion (drag_detection_utils_v1._canonicalize_pzt_columns),
# but the live/offline GUI pipeline works with whichever board is actually
# streaming, so it needs to build the right column names itself.
DEFAULT_PZT_SENSOR_NUMBER = "3"

TOUCHID_SETTINGS_PAYLOAD_KEY = "touchid_settings"

# model_type -> checkpoint filename stem prefix, e.g. "quad" -> "texture_quadbranch".
# Drives both the generic `{model_type}_model_path` config field and version
# discovery/selection below. Keep in sync with architectures.ARCH_REGISTRY's
# version_regex for each key (this prefix is that regex without its capture group).
_CHECKPOINT_STEM_PREFIX = {
    "ann": "texture_ann",
    "cnn": "texture_cnn",
    "quad": "texture_quadbranch",
    "penta": "texture_pentabranch",
}


@dataclass
class InferenceConfig:
    window_size_s: float = 0.5      # independent literal, NOT imported from training config
    hop_size_s: float = 0.5         # user-adjustable via GUI spinbox
    min_span_fill_ratio: float = 0.08  # ActiveSampleQueue.evict_stale: min length (as a
                                        # fraction of window_size_s) a still-growing OPEN span
                                        # must reach before it's kept alive past
                                        # span_stale_timeout_s. Short FINALIZED spans (done
                                        # growing) are instead padded out to a full window using
                                        # history -- see inference/window_padding.py.
    span_stale_timeout_s: float = 1.0  # ActiveSampleQueue: drop an unfinished span once
                                        # its oldest sample is this old, rather than
                                        # waiting indefinitely for it to fill or close out
    pzt_columns: list[str] = field(default_factory=lambda: list(_DEFAULT_PZT_COLUMNS))
    smoothing_alpha: float = 0.9    # EMA alpha for ConfidenceSmoother, user-adjustable
    confidence_threshold: float = 0.6  # bar-chart gray/red cutoff + latched "last detected" gate, user-adjustable
    model_type: str = "ann"         # "ann" | "cnn" | "quad" | "penta" — which architecture is active
    ann_model_path: str = str(TEXTURE_PIEZO_MODELS / "texture_ann_v2.pt")
    cnn_model_path: str = str(TEXTURE_PIEZO_MODELS / "texture_cnn_v2.pt")
    quad_model_path: str = str(TEXTURE_PIEZO_MODELS / "texture_quadbranch_v4.pt")
    penta_model_path: str = str(TEXTURE_PIEZO_MODELS / "texture_pentabranch_v1.pt")
    scaler_path: str = str(TEXTURE_PIEZO_MODELS / "scaler_v2.pkl")
    raw_norm_stats_path: str = str(TEXTURE_PIEZO_MODELS / "raw_norm_stats_v2.npz")
    class_names: list[str] = field(default_factory=lambda: [
        "bumpy_wood", "cardboard", "leather", "tile", "tiona", "idle",
    ])


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
    """Scan TEXTURE_PIEZO_MODELS for weight versions available for `model_type`.

    ANN/CNN (ArchSpec.requires_sidecars=True) list a version only if its model
    .pt file plus a matching scaler_<version>.pkl and raw_norm_stats_<version>.npz
    sidecar are all present, so switching to it can't fail on a missing artifact.
    Quad/Penta (requires_sidecars=False) need only the .pt file -- their
    normalization artifacts are either baked into the checkpoint (fusion-feature
    BatchNorm1d) or shared/global (see architectures.py's module docstring).

    A version qualifies if ANY of its checkpoint tags (see discover_checkpoints)
    satisfies the sidecar requirement -- sidecars are keyed by version only, not
    by checkpoint tag, so they either cover the whole version or none of it.
    """
    from .architectures import ARCH_REGISTRY

    spec = ARCH_REGISTRY.get(model_type)
    if spec is None:
        return []
    versions = set()
    for path in TEXTURE_PIEZO_MODELS.glob("*.pt"):
        match = spec.version_regex.match(path.stem)
        if not match:
            continue
        version = match.group(1)
        if spec.requires_sidecars:
            if not ((TEXTURE_PIEZO_MODELS / f"scaler_{version}.pkl").exists() and
                    (TEXTURE_PIEZO_MODELS / f"raw_norm_stats_{version}.npz").exists()):
                continue
        versions.add(version)
    return sorted(versions)


def discover_checkpoints(model_type: str, version: str) -> list[str]:
    """Scan TEXTURE_PIEZO_MODELS for checkpoint tags available for `model_type`'s
    `version` -- tags are free-form (e.g. "best_working_09_17_2026"), not a
    fixed enum, since a checkpoint filename's suffix can be anything after the
    version. [CHECKPOINT_DEFAULT] alone means an untagged single-file version.
    Sorted with CHECKPOINT_DEFAULT first, then CHECKPOINT_TAGS priority order,
    then any other tags alphabetically."""
    from .architectures import ARCH_REGISTRY, CHECKPOINT_DEFAULT, CHECKPOINT_TAGS

    spec = ARCH_REGISTRY.get(model_type)
    if spec is None:
        return []
    checkpoints = set()
    for path in TEXTURE_PIEZO_MODELS.glob("*.pt"):
        match = spec.version_regex.match(path.stem)
        if not match or match.group(1) != version:
            continue
        checkpoints.add(match.group(2) or CHECKPOINT_DEFAULT)
    priority = (CHECKPOINT_DEFAULT,) + CHECKPOINT_TAGS
    return sorted(checkpoints, key=lambda c: (priority.index(c) if c in priority else len(priority), c))


def model_version_of(config: InferenceConfig) -> str | None:
    """Best-effort extraction of the version suffix (e.g. "v2b") config is currently set to."""
    match = _current_checkpoint_match(config)
    return match.group(1) if match else None


def model_checkpoint_of(config: InferenceConfig) -> str:
    """Best-effort extraction of the checkpoint tag (e.g. "best") config is
    currently set to, or CHECKPOINT_DEFAULT for an untagged/unrecognized path."""
    from .architectures import CHECKPOINT_DEFAULT

    match = _current_checkpoint_match(config)
    if not match:
        return CHECKPOINT_DEFAULT
    return match.group(2) or CHECKPOINT_DEFAULT


def _current_checkpoint_match(config: InferenceConfig):
    from .architectures import ARCH_REGISTRY

    spec = ARCH_REGISTRY.get(config.model_type)
    if spec is None:
        return None
    path = getattr(config, _model_path_attr(config.model_type))
    return spec.version_regex.match(Path(path).stem)


def set_model_version(config: InferenceConfig, version: str, checkpoint: str = "default") -> None:
    """Point config at `version`'s weight file (optionally a specific `checkpoint`
    tag, e.g. "best") for the currently active architecture (config.model_type),
    plus scaler/norm-stats for architectures that use them (keyed by version
    only -- see architectures.py's module docstring). Leaves other
    architectures' paths untouched so switching back to one doesn't lose its
    own version selection."""
    from .architectures import CHECKPOINT_DEFAULT

    prefix = _CHECKPOINT_STEM_PREFIX[config.model_type]
    stem = f"{prefix}_{version}" if checkpoint == CHECKPOINT_DEFAULT else f"{prefix}_{version}_{checkpoint}"
    checkpoint_path = TEXTURE_PIEZO_MODELS / f"{stem}.pt"
    if config.model_type == "cnn" and not checkpoint_path.exists():
        stem = f"texture_cnn2d_{version}" if checkpoint == CHECKPOINT_DEFAULT else f"texture_cnn2d_{version}_{checkpoint}"
        checkpoint_path = TEXTURE_PIEZO_MODELS / f"{stem}.pt"
    setattr(config, _model_path_attr(config.model_type), str(checkpoint_path))

    from .architectures import ARCH_REGISTRY
    if ARCH_REGISTRY[config.model_type].requires_sidecars:
        config.scaler_path = str(TEXTURE_PIEZO_MODELS / f"scaler_{version}.pkl")
        config.raw_norm_stats_path = str(TEXTURE_PIEZO_MODELS / f"raw_norm_stats_{version}.npz")


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
            "smoothing_alpha": config.smoothing_alpha,
            "confidence_threshold": config.confidence_threshold,
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
        if "smoothing_alpha" in payload:
            config.smoothing_alpha = payload["smoothing_alpha"]
        if "confidence_threshold" in payload:
            config.confidence_threshold = payload["confidence_threshold"]
        if "model_type" in payload and payload["model_type"] in _CHECKPOINT_STEM_PREFIX:
            config.model_type = payload["model_type"]
        version = payload.get("model_version")
        if version and version in discover_model_versions(config.model_type):
            checkpoint = payload.get("model_checkpoint") or "default"
            if checkpoint not in discover_checkpoints(config.model_type, version):
                checkpoint = "default"
            set_model_version(config, version, checkpoint)
    return config
