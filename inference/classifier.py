"""
Texture Classifier
===================
Loads a trained texture_piezo model + its normalization artifacts once and
exposes predict_proba for per-window inference. Loading is intentionally
kept out of the per-hop timer callback (see task 09) -- instantiate one
TextureClassifier at panel init (or on model-switch/reload) and reuse it
across windows.

Per-architecture loading/input-construction lives in architectures.py's
ARCH_REGISTRY -- this class is just "look up config.model_type, load that
architecture's runtime, delegate predict_proba to it" so adding a new
architecture never touches this file.
"""

from __future__ import annotations

import numpy as np

from .architectures import ARCH_REGISTRY
from .config import InferenceConfig, model_version_of


class TextureClassifier:
    def __init__(self, config: InferenceConfig):
        self.model_type = config.model_type
        self.class_names = config.class_names

        spec = ARCH_REGISTRY.get(self.model_type)
        if spec is None:
            raise ValueError(f"Unknown model_type {self.model_type!r} -- known: {sorted(ARCH_REGISTRY)}")

        version = model_version_of(config)
        if version is None:
            raise ValueError(f"Could not determine weight version from config paths for model_type={self.model_type!r}")

        self._runtime = spec.load(config, version)

    def predict_proba(
        self, feature_vector: np.ndarray, window_channels: np.ndarray | None = None,
        window_integrated: np.ndarray | None = None,
    ) -> dict[str, float]:
        """
        feature_vector: (n_feat,) hand-crafted features, same for every model type.
        window_channels: (n_samples, n_pzt + 3) raw [pzt(5), shear_lr, shear_tb, normal]
            window -- required for every architecture except "ann", ignored otherwise.
        window_integrated: optional (n_samples, n_pzt) whole-file-integrated ADC
            slice -- see pipeline.classify_window. Only quad/penta use it.
        """
        return self._runtime.predict_proba(
            feature_vector, window_channels, self.class_names, window_integrated=window_integrated)
