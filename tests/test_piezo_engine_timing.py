"""Core timing resolver: mode normalization, manual/infer/continuous policies, decay carry-over, the analysis edge,
uniform timestamps and the block-timing sidecar priority."""

import numpy as np
import pytest

import data_processing.analysis_workbench as analysis
from core.piezo_engine.config import TimingMode
from core.piezo_engine.timing import (
    TimingEdgeInputs,
    auto_mux_connected_time_s,
    metadata_sample_rate_hz,
    normalize_timing_mode,
    resolve_capture_timing,
    resolve_mux_leak_dt_s,
    resolve_timing_policy,
    timestamps_for_capture,
)

MANUAL_CONNECTED_TIME_S = 3.0e-5
MODE_SETTINGS = [
    {},
    {"mux_timing_mode": "auto"},
    {"mux_timing_mode": "Auto"},
    {"mux_timing_mode": "continuous"},
    {"mux_timing_mode": "Continuous"},
    {"mux_timing_mode": "manual", "mux_connected_time_s": MANUAL_CONNECTED_TIME_S},
    {"mux_timing_mode": "infer_from_total_sample_rate"},
    {"mux_timing_mode": "Infer from total sample rate"},
    {"mux_timing_mode": "infer"},
]
SYNTHETIC_RATE = 1500.0


def _snapshot(metadata: dict) -> analysis.AnalysisSourceSnapshot:
    data = np.zeros((4, 5))
    timestamps = np.arange(4, dtype=np.float64) / SYNTHETIC_RATE
    has_timing_block = isinstance(metadata.get("timing"), dict)
    return analysis.AnalysisSourceSnapshot(
        data=data, timestamps_s=timestamps, channel_labels=["a", "b", "c", "d", "e"], metadata=metadata,
        sample_rate_hz=analysis._metadata_sample_rate_hz(metadata, data, timestamps) if has_timing_block else SYNTHETIC_RATE,
    )


@pytest.mark.parametrize("settings", [{"mux_timing_mode": "manual"}, {"mux_timing_mode": "manual", "mux_connected_time_s": 0}])
def test_manual_without_positive_time_raises(settings):
    with pytest.raises(ValueError):
        resolve_mux_leak_dt_s({}, settings)


DECAY_WITH_RATE_METADATA = {
    "timing": {"pzt_pre_sample_decay_s_by_label": {"PZT5_B": 1e-6, "PZT5_C": 2e-6}, "per_channel_sample_rates_hz": {"a": 1500.0}},
    "pzt_mux_connected_time_s": 2e-5,
}
MANUAL_SETTINGS = {"mux_timing_mode": "manual", "mux_connected_time_s": 1e-5}
CONTINUOUS_SETTINGS = {"mux_timing_mode": "continuous"}


def test_continuous_policy_carries_no_decay_although_the_metadata_has_one():
    """The one approved P2b behaviour change: the pre-P2b Analysis resolver returned the metadata decay map
    in continuous mode too; decay only exists for a MUX-connected leak."""
    for resolve in (
        lambda: resolve_capture_timing(DECAY_WITH_RATE_METADATA, CONTINUOUS_SETTINGS).policy,
        lambda: resolve_timing_policy(DECAY_WITH_RATE_METADATA, CONTINUOUS_SETTINGS)[0],
        lambda: analysis.resolve_analysis_timing(_snapshot(DECAY_WITH_RATE_METADATA), CONTINUOUS_SETTINGS)[0],
    ):
        policy = resolve()
        assert policy.mode is TimingMode.CONTINUOUS and policy.leak_dt_s is None
        assert not policy.pre_sample_decay_s_by_label


@pytest.mark.parametrize("settings", [{}, MANUAL_SETTINGS], ids=["auto", "manual"])
def test_mux_connected_modes_keep_the_metadata_decay(settings):
    expected = {"PZT5_B": 1e-6, "PZT5_C": 2e-6}
    assert dict(resolve_capture_timing(DECAY_WITH_RATE_METADATA, settings).policy.pre_sample_decay_s_by_label) == expected
    policy, _status = analysis.resolve_analysis_timing(_snapshot(DECAY_WITH_RATE_METADATA), settings)
    assert dict(policy.pre_sample_decay_s_by_label) == expected


def test_unknown_mode_raises_instead_of_silently_becoming_auto():
    with pytest.raises(ValueError):
        normalize_timing_mode("bogus")
    assert normalize_timing_mode(None) is TimingMode.AUTO


def test_infer_mode_prefers_the_edge_sample_rate_and_keeps_the_analysis_error_text():
    settings = {"mux_timing_mode": "infer_from_total_sample_rate"}
    metadata = {"timing": {"per_channel_sample_rates_hz": {"a": 1000.0}}}
    assert resolve_mux_leak_dt_s(metadata, settings, TimingEdgeInputs(sample_rate_hz=2000.0))[0] == 1.0 / 2000.0
    assert resolve_mux_leak_dt_s(metadata, settings)[0] == 1.0 / 1000.0
    with pytest.raises(ValueError, match="sample rate unavailable for inferred PZT MUX connected time"):
        resolve_mux_leak_dt_s({}, settings)


def test_sample_rate_unavailable_raises():
    with pytest.raises(ValueError):
        metadata_sample_rate_hz({"timing": {}})


def test_timestamps_for_capture_is_the_uniform_grid():
    stamps = timestamps_for_capture(5, 1000.0)
    assert np.array_equal(stamps, np.arange(5) / 1000.0)
    with pytest.raises(ValueError):
        timestamps_for_capture(5, 0.0)


def test_sidecar_ranks_below_direct_and_calculated_times(tmp_path):
    metadata = {"timing": {"pzt_mux_connected_time_s": 2e-5}}
    assert auto_mux_connected_time_s(metadata, 9e-5)[0] == 2e-5
