"""
Texture Classifier
===================
Loads a trained texture_piezo model once and exposes predict_proba for per-window
inference. Loading is intentionally kept out of the per-hop timer callback --
instantiate one TextureClassifier at panel init (or on model-switch/reload) and reuse
it across windows.

Per-architecture loading lives in architectures.py's ARCH_REGISTRY, which loads
texture_piezo's own runtime -- this class is just "look up config.model_type, load that
runtime, delegate predict_proba to it" so adding a new architecture never touches this
file. The runtime tells the app which engine channels it reads (``required_channels``) and
which EngineConfig it was trained on (``engine_config``).
"""

from __future__ import annotations

from pathlib import Path

from .architectures import ARCH_REGISTRY
from .model_discovery import ModelArtifacts
from core.piezo_engine.live_window import LiveWindow
from core.texture_piezo.application.inference_config import InferenceConfig, model_checkpoint_of, model_version_of


def _optional_path(value: str) -> Path | None:
    return Path(value) if value else None


def artifacts_from_config(config: InferenceConfig, version: str) -> ModelArtifacts:
    """The artifacts ``config`` currently points at (checkpoint plus its sidecars)."""
    return ModelArtifacts(
        model_type=config.model_type,
        version=version,
        checkpoint=model_checkpoint_of(config),
        checkpoint_path=Path(getattr(config, f"{config.model_type}_model_path")),
        scaler_path=_optional_path(config.scaler_path),
        raw_norm_stats_path=_optional_path(config.raw_norm_stats_path),
        feature_names_path=_optional_path(config.feature_names_path),
    )


class TextureClassifier:
    def __init__(self, config: InferenceConfig):
        self.model_type = config.model_type
        self.class_names = tuple(config.class_names)

        spec = ARCH_REGISTRY.get(self.model_type)
        if spec is None:
            raise ValueError(f"Unknown model_type {self.model_type!r} -- known: {sorted(ARCH_REGISTRY)}")

        version = model_version_of(config)
        if version is None:
            raise ValueError(f"Could not determine weight version from config paths for model_type={self.model_type!r}")

        self._runtime = spec.load(artifacts_from_config(config, version), self.class_names)

    @property
    def required_channels(self) -> tuple[str, ...]:
        """Engine channel names the model reads; the live window carries exactly these."""
        return self._runtime.required_channels

    @property
    def engine_config(self) -> dict | None:
        """The model's EngineConfig dict, None for a model whose runtime reports engine_config=None (the app's default runs)."""
        return self._runtime.engine_config

    @property
    def expected_ingest_blip_filter(self) -> bool | None:
        """Whether the model was trained on ingest-blip-filtered input; None when it does not say."""
        return getattr(self._runtime, "expected_ingest_blip_filter", None)

    def predict_proba(self, window: LiveWindow) -> dict[str, float]:
        return self._runtime.predict_proba(window, self.class_names)
