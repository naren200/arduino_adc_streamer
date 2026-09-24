"""
Idle Baseline Settings Persistence
=====================================
GUI-app settings persistence for the idle baseline: save/load an IdleBaseline
to/from ~/.adc_streamer/touchid/idle_baseline.json.

The actual baseline-fitting/activity-test logic (IdleBaseline dataclass,
fit_idle_baseline, chunk_is_active, idle_gap_chunks_cap) moved to
texture_piezo's src/touchid_inference/quality_gate.py -- it's pure
array-in/array-out inference logic with no dependency on this app's settings
layer, so it belongs there alongside segmentation.py and window_padding.py.
This module keeps only the settings I/O that's genuinely specific to the
live GUI tool (this app's ~/.adc_streamer/ settings directory and
file_operations.settings_persistence helpers), and imports IdleBaseline from
texture_piezo rather than duplicating the dataclass.
"""

from __future__ import annotations

from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: puts texture_piezo/src on sys.path)
from touchid_inference.quality_gate import DEFAULT_K, IdleBaseline

from file_operations.settings_persistence import load_settings_payload, save_settings_payload

IDLE_BASELINE_PAYLOAD_KEY = "idle_baseline"


def _get_idle_baseline_path() -> Path:
    return Path.home() / ".adc_streamer" / "touchid" / "idle_baseline.json"


def save_idle_baseline(baseline: IdleBaseline) -> Path:
    payload = {
        "version": 1,
        IDLE_BASELINE_PAYLOAD_KEY: {
            "pzt_columns": baseline.pzt_columns,
            "mean": baseline.mean,
            "std": baseline.std,
            "fs": baseline.fs,
            "k": baseline.k,
            "captured_duration_s": baseline.captured_duration_s,
        },
    }
    return save_settings_payload(_get_idle_baseline_path(), payload)


def load_idle_baseline(pzt_columns: list[str] | None = None) -> IdleBaseline | None:
    """Returns the persisted baseline, or None if none exists yet, or if it
    was captured for a different set of PZT channels (e.g. after switching
    sensor boards) -- stale-channel baselines must not be silently reused."""
    path = _get_idle_baseline_path()
    if not path.exists():
        return None
    try:
        _path, payload = load_settings_payload(path, payload_key=IDLE_BASELINE_PAYLOAD_KEY)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        baseline = IdleBaseline(
            pzt_columns=list(payload["pzt_columns"]),
            mean=list(payload["mean"]),
            std=list(payload["std"]),
            fs=float(payload["fs"]),
            k=float(payload.get("k", DEFAULT_K)),
            captured_duration_s=float(payload.get("captured_duration_s", 0.0)),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if pzt_columns is not None and baseline.pzt_columns != list(pzt_columns):
        return None
    return baseline
