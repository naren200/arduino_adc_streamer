"""
Architecture Registry
======================
Maps a `model_type` key ("ann", "cnn", "quad", "penta", "chunk") to the runtime that
runs it. Every runtime -- model definition, feature engineering, resizing, normalisation
-- lives in texture_piezo's model-runtime package and is reached only through
inference/texture_piezo_adapter.py; nothing here knows a model.

model_discovery decides whether a checkpoint is offerable by loading it through this
registry, so an architecture that needs no sidecars simply never fails for want of one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from inference import texture_piezo_adapter

from .model_discovery import ModelArtifacts

ARCH_KEYS = ("ann", "cnn", "quad", "penta", "chunk")


@dataclass(frozen=True)
class ArchSpec:
    key: str

    def load(self, artifacts: ModelArtifacts, class_names: Sequence[str]):
        """The loaded texture_piezo runtime (``required_channels``, ``engine_config``,
        ``predict_proba(window, class_names)``) for one discovered checkpoint."""
        return texture_piezo_adapter.load_runtime(
            self.key, texture_piezo_adapter.runtime_artifacts(artifacts), tuple(class_names),
        )


ARCH_REGISTRY: dict[str, ArchSpec] = {key: ArchSpec(key) for key in ARCH_KEYS}
