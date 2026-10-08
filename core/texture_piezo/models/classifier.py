"""
Texture Classifier
===================
Loads a trained texture_piezo model once and exposes predict_proba for per-window
inference. Loading is intentionally kept out of the per-hop timer callback --
instantiate one TextureClassifier at panel init (or on model-switch/reload) and reuse
it across windows.

Per-architecture loading is texture_piezo's own runtime, reached through the adapter --
this class is just "load config.model_type's runtime, delegate predict_proba to it" so
adding a new architecture never touches this file. The runtime tells the app which engine channels it reads (``required_channels``) and
which EngineConfig it was trained on (``engine_config``).
"""

from __future__ import annotations

import logging

from . import model_discovery
from .model_discovery import ModelArtifacts
from core.piezo_engine.live_window import LiveWindow
from core.texture_piezo.application.inference_config import InferenceConfig, model_checkpoint_of, model_version_of


logger = logging.getLogger(__name__)

MODEL_LOAD_LOG_EVENT = "model_load"
LOAD_OK = "ok"
LOAD_FAILED = "failed"


def artifacts_from_config(config: InferenceConfig) -> ModelArtifacts:
    """The discovered bundle the config's selection names for its active architecture."""
    version, checkpoint = model_version_of(config), model_checkpoint_of(config)
    artifacts = model_discovery.artifacts_for(config.model_type, version, checkpoint) if version else None
    if artifacts is None:
        raise ValueError(
            f"No discovered checkpoint for model_type={config.model_type!r} version={version!r} checkpoint={checkpoint!r}")
    return artifacts


class TextureClassifier:
    def __init__(self, config: InferenceConfig):
        self.model_type = config.model_type
        artifacts = artifacts_from_config(config)
        self.class_names = artifacts.class_names
        self._runtime = self._load(artifacts)

    def _load(self, artifacts: ModelArtifacts):
        try:
            runtime = model_discovery.load_artifacts_runtime(artifacts)
        except Exception:
            self._log_load(artifacts, LOAD_FAILED)
            raise
        self._log_load(artifacts, LOAD_OK)
        return runtime

    @staticmethod
    def _log_load(artifacts: ModelArtifacts, outcome: str) -> None:
        logger.info(MODEL_LOAD_LOG_EVENT, extra={
            "family": artifacts.model_type, "version": artifacts.version,
            "checkpoint": artifacts.checkpoint, "outcome": outcome,
        })

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
