"""Snapshot loaders stamp which conditioning (median) they already applied."""

import os
from pathlib import Path

import numpy as np
import pytest

import data_processing.analysis_workbench as analysis
from core.piezo_engine.pipeline import INPUT_CONDITIONING_METADATA_KEY

RAW_DIR = Path(os.environ.get(
    "TP_RAW_DIR", r"C:\Users\sense\Documents\Github\texture_piezo\data\raw\sensor_v12d_7_26\ch5",
))
CAPTURE = "only_cardboard_and_idle_v1_20260914_1635"
SYNTHETIC_COLUMNS = [f"PZT5_{c}" for c in "BLCRT"]


def _snapshot() -> analysis.AnalysisSourceSnapshot:
    rng = np.random.default_rng(0)
    return analysis.AnalysisSourceSnapshot(
        data=rng.integers(0, 4096, size=(100, 5)).astype(np.float32),
        timestamps_s=np.arange(100, dtype=np.float64) / 1500.0,
        channel_labels=list(SYNTHETIC_COLUMNS), channel_indices=list(range(5)),
        metadata={}, sample_rate_hz=1500.0,
    )


@pytest.mark.parametrize("kwargs, expected", [
    ({"enabled": True, "window": 3}, {"median_window_samples": 3}),
    ({"enabled": True, "window": 4}, {"median_window_samples": 5}),
    ({"enabled": False}, {"median_window_samples": None}),
])
def test_in_memory_loader_stamps_median_window(kwargs, expected):
    snapshot = analysis._load_filtered_snapshot(_snapshot(), **kwargs)
    assert snapshot.metadata[INPUT_CONDITIONING_METADATA_KEY] == expected


@pytest.mark.parametrize("kwargs, expected_window", [
    ({}, 3), ({"blip_filter_window_samples": 5}, 5), ({"blip_filter_enabled": False}, None),
])
def test_csv_loader_stamps_median_window_and_keeps_every_row(kwargs, expected_window):
    csv_path = RAW_DIR / f"{CAPTURE}.csv"
    if not csv_path.exists():
        pytest.skip("training capture not available")
    snapshot = analysis.load_exported_csv_snapshot(csv_path, RAW_DIR / f"{CAPTURE}_metadata.json", **kwargs)
    assert snapshot.metadata[INPUT_CONDITIONING_METADATA_KEY] == {"median_window_samples": expected_window}
    assert len(snapshot.timestamps_s) == len(snapshot.data)
