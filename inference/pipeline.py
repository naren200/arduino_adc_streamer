"""
Preprocessing / Featurization Pipeline
========================================
Glue between raw ADC windows pulled off the ring buffer and the feature
vector the classifier expects. Kept as a thin wrapper around texture_piezo's
own feature function so the GUI panel has one call site, and upstream
signature changes only need updating here.

Shear/normal and integrated-ADC channels are no longer computed here: both
offline CSV inference (inference/offline.py) and live TouchID streaming
(gui/inference_panel.py) derive them via a shared, stateful
causal_derived_channels.CausalDerivedChannels instance (one per whole
snapshot, or one persisted across the live session) and pass the already-
sliced-to-this-window arrays in. Recomputing shear/normal from scratch per
window (the previous behavior here) restarted the underlying causal state at
every window boundary, producing an artificial startup ramp not present in
training data -- see texture_piezo/src/drag_detection_utils_v1.py's
load_and_integrate_calibration_csv docstring (08_analysis_v1.ipynb finding,
2026-09-10; CausalDerivedChannels fix, 2026-09-16).
"""

from __future__ import annotations

import sys

import numpy as np

from ._paths import TEXTURE_PIEZO_SRC

sys.path.insert(0, str(TEXTURE_PIEZO_SRC))
from clip_windowing_utils_v1 import extract_window_features  # noqa: E402


def classify_window(
    window_adc: np.ndarray,
    window_integrated: np.ndarray,
    window_shear_lr: np.ndarray,
    window_shear_tb: np.ndarray,
    window_normal: np.ndarray,
    fs: float,
    classifier,
) -> dict[str, float]:
    """
    Single source of truth for turning one already-derived window into class
    probabilities: featurize -> predict. Shared by the live TouchID
    rolling-buffer path (inference_panel.py) and offline CSV inference
    (inference/offline.py) so both always run identical
    featurization/model logic -- a change here updates both.

    window_adc: (n_samples, n_channels) raw ADC counts, columns ordered per
        the caller's pzt_columns.
    window_integrated: (n_samples, n_pzt) -- slice of a whole-file/whole-
        session continuous CausalDerivedChannels "integrated" output.
    window_shear_lr/window_shear_tb/window_normal: (n_samples,) -- slices of
        the same CausalDerivedChannels output's shear/normal channels.
    classifier: a TextureClassifier (its model_type selects ANN/CNN/Quad/Penta input).
    """
    features = extract_window_features(
        window_adc, window_integrated, window_shear_lr, window_shear_tb, window_normal, fs,
    )

    # Built for every architecture except "ann" (which ignores it) -- cheap
    # column_stack, not worth gating per model_type here when classifier.py's
    # architecture registry is what actually decides who needs it.
    window_channels = np.column_stack([window_adc, window_shear_lr, window_shear_tb, window_normal])

    return classifier.predict_proba(
        features, window_channels=window_channels, window_integrated=window_integrated)
