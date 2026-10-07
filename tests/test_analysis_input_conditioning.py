"""Snapshot loaders stamp which conditioning (median / settle trim) they already applied."""

import os
from pathlib import Path

import numpy as np
import pytest

import data_processing.analysis_workbench as analysis
from core.piezo_engine.baseline import capture_start_settle_sample_count
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
    ({"enabled": True, "window": 3}, {"median_window_samples": 3, "settle_trimmed_samples": 0}),
    ({"enabled": True, "window": 4}, {"median_window_samples": 5, "settle_trimmed_samples": 0}),
    ({"enabled": False}, {"median_window_samples": None, "settle_trimmed_samples": 0}),
])
def test_in_memory_loader_stamps_median_window_and_no_settle(kwargs, expected):
    snapshot = analysis._load_filtered_snapshot(_snapshot(), **kwargs)
    assert snapshot.metadata[INPUT_CONDITIONING_METADATA_KEY] == expected


@pytest.mark.parametrize("kwargs, expected_window", [
    ({}, 3), ({"blip_filter_window_samples": 5}, 5), ({"blip_filter_enabled": False}, None),
])
def test_csv_loader_stamps_median_window_and_settle_trim(kwargs, expected_window):
    csv_path = RAW_DIR / f"{CAPTURE}.csv"
    if not csv_path.exists():
        pytest.skip("training capture not available")
    snapshot = analysis.load_exported_csv_snapshot(csv_path, RAW_DIR / f"{CAPTURE}_metadata.json", **kwargs)
    record = snapshot.metadata[INPUT_CONDITIONING_METADATA_KEY]
    assert record["median_window_samples"] == expected_window
    assert record["settle_trimmed_samples"] == capture_start_settle_sample_count(snapshot.sample_rate_hz)
