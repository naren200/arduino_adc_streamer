"""The Analysis tab runs on the engine (P2b): ``prepare_analysis_data`` status messages for an inferred
sample rate and invalid force settings, and when the block-timing sidecar is read."""

import numpy as np
import pytest

import data_processing.analysis_workbench as analysis
from constants.pzt_force import ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS
from test_piezo_engine_force_stage import synthetic_counts

FS = 1024.0
COLUMNS = [f"PZT3_{position}" for position in "BLCRT"]
SETTINGS = dict(ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS)
LEAK_DT_S = 2.1583e-05
MANUAL = {"mux_timing_mode": "manual", "mux_connected_time_s": LEAK_DT_S}
CONTINUOUS = {"mux_timing_mode": "continuous"}
FORCE_FLAGS = {"shear_force": True, "normal_force": True}
BASE_METADATA = {"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}}


def _snapshot(counts: np.ndarray, metadata: dict | None = None, sample_rate_hz: float = FS):
    return analysis.AnalysisSourceSnapshot(
        data=counts.astype(np.float32), timestamps_s=np.arange(len(counts)) / FS, channel_labels=list(COLUMNS),
        metadata=BASE_METADATA if metadata is None else metadata, source_id="unit", sample_rate_hz=sample_rate_hz,
    )


def _decay_metadata(decay: dict) -> dict:
    return {**BASE_METADATA, "timing": {
        "per_channel_sample_rates_hz": {label: FS for label in COLUMNS}, "pzt_pre_sample_decay_s_by_label": decay}}


def _prepare(snapshot, settings: dict, flags: dict = FORCE_FLAGS):
    return analysis.prepare_analysis_data(
        snapshot, axis_mode="samples", overlay_flags=flags, pzt_force_settings={**SETTINGS, **settings})


def test_infer_mode_uses_the_snapshot_sample_rate_when_the_metadata_has_none():
    prepared = _prepare(_snapshot(synthetic_counts(300), dict(BASE_METADATA)), {"mux_timing_mode": "infer_from_total_sample_rate"})
    assert prepared.status == f"PZT MUX timing: Inferred {1000.0 / FS:.3f} ms from total sample rate."


def test_invalid_force_settings_are_reported_as_skipped_and_produce_no_force_traces():
    prepared = _prepare(_snapshot(synthetic_counts(300)[:50]), {**MANUAL, "center_capacitance_value": 0.0})
    assert "Shear Force / Normal Force skipped" in prepared.status
    assert not prepared.force_traces


@pytest.mark.parametrize(("settings", "is_read"), [
    ({"mux_timing_mode": "auto"}, True), ({}, True),
    (MANUAL, False), (CONTINUOUS, False), ({"mux_timing_mode": "infer_from_total_sample_rate"}, False),
], ids=["auto", "default", "manual", "continuous", "infer"])
def test_the_block_timing_sidecar_is_only_read_in_auto_mode(monkeypatch, settings, is_read):
    reads = []
    monkeypatch.setattr(analysis, "read_block_timing_connected_time_s", lambda *args: reads.append(args) or None)
    metadata = {**_decay_metadata({}), "block_timing_csv": "blocks.csv", "pzt_mux_connected_time_s": LEAK_DT_S}
    analysis.resolve_analysis_timing(_snapshot(synthetic_counts(300)[:10], metadata), settings)
    assert bool(reads) is is_read
