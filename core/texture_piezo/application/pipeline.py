"""
Window classification
======================
Glue between one ready window and the loaded model. The window is a ``LiveWindow`` of
named engine channels (live_channels.py builds it, stream_processor.py cuts it); the
model runtime -- texture_piezo's model-runtime package -- computes its own
features and resizing from it. Nothing here knows any feature, layout or model.

Shared by the live TouchID path (gui/inference_panel.py) and offline replay so both
always run identical logic.
"""

from __future__ import annotations

from core.piezo_engine.live_window import LiveWindow


def classify_window(window: LiveWindow, classifier) -> dict[str, float]:
    """Class probabilities for one ready window from a TextureClassifier."""
    return classifier.predict_proba(window)
