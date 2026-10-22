"""The TestBoard behaviours, kept on top of the engine-based Analysis pipeline:
per-capture voltage scaling, AUTO timing without a physical connection time, gap-aware blip
filtering and the replay guard."""

from dataclasses import replace

import numpy as np
import pytest

from core.piezo_engine.config import TimingMode
from core.piezo_engine.overlay import JerkOverlayStage
from gui.analysis_panel import AnalysisPanelMixin
from data_processing.analysis_workbench import (
    AnalysisSourceSnapshot,
    _apply_pzt_blip_filter,
    build_overlay_traces,
    counts_to_volts,
    prepare_analysis_data,
    resolve_analysis_force_timing,
    resolve_analysis_timing,
)
from data_processing.replay_support import replay_unsupported_reason

POSITIONS = ("B", "L", "C", "R", "T")
SAMPLE_COUNT = 400
SAMPLE_PERIOD_S = 1e-3
WIDE_BITS = 16
WIDE_FULL_SCALE_V = 5.0
DEFAULT_BITS = 12
DEFAULT_FULL_SCALE_V = 3.3
JERK_FLAGS = {"shear": True, "normal": True}


def wide_descriptor():
    return {"adc_resolution_bits": WIDE_BITS, "full_scale_volts": WIDE_FULL_SCALE_V, "reference": WIDE_FULL_SCALE_V}


def wide_board_context():
    return {"hardware": {"adc_resolution_bits": WIDE_BITS}, "full_scale_volts": WIDE_FULL_SCALE_V}


def make_snapshot(metadata=None, counts=None, timestamps=None, prefix="PKG"):
    rng = np.random.default_rng(1)
    data = counts if counts is not None else rng.integers(0, 2 ** DEFAULT_BITS, size=(SAMPLE_COUNT, 5)).astype(np.float64)
    return AnalysisSourceSnapshot(
        data=data,
        timestamps_s=timestamps if timestamps is not None else np.arange(len(data)) * SAMPLE_PERIOD_S,
        channel_labels=[f"{prefix}_{position}" for position in POSITIONS],
        channel_indices=list(range(5)),
        metadata=metadata or {},
        sample_rate_hz=1.0 / SAMPLE_PERIOD_S,
    )


def counts_as_default_scale(counts):
    """Counts that read, on the default 12-bit 3.3 V scale, the same volts as `counts` on the wide scale."""
    volts = counts / (2 ** WIDE_BITS - 1) * WIDE_FULL_SCALE_V
    return volts / DEFAULT_FULL_SCALE_V * (2 ** DEFAULT_BITS - 1)


def wide_counts():
    return np.random.default_rng(2).integers(0, 2 ** WIDE_BITS, size=(SAMPLE_COUNT, 5)).astype(np.float64)


WIDE_METADATA = [
    {"testboard_acquisition": wide_descriptor()},
    {"board_context": wide_board_context()},
]


@pytest.mark.parametrize("metadata", WIDE_METADATA)
def test_signal_traces_use_the_capture_full_scale_and_bits(metadata):
    counts = wide_counts()
    prepared = prepare_analysis_data(make_snapshot(metadata, counts), vref_voltage=DEFAULT_FULL_SCALE_V)
    expected = counts[:, 0] / (2 ** WIDE_BITS - 1) * WIDE_FULL_SCALE_V
    np.testing.assert_allclose(prepared.traces[0].y, expected)


def test_default_capture_still_scales_with_the_given_vref():
    counts = np.full((SAMPLE_COUNT, 5), 4095.0)
    prepared = prepare_analysis_data(make_snapshot({}, counts), vref_voltage=DEFAULT_FULL_SCALE_V)
    np.testing.assert_allclose(prepared.traces[0].y, DEFAULT_FULL_SCALE_V)


@pytest.mark.parametrize("metadata", WIDE_METADATA)
def test_jerk_overlays_use_the_capture_scale(metadata):
    counts = wide_counts()
    overlay_kwargs = dict(axis_mode="samples", overlay_flags=JERK_FLAGS, vref_voltage=DEFAULT_FULL_SCALE_V,
                          integration_window_samples=5, hpf_cutoff_hz=1.0)
    wide = make_snapshot(metadata, counts)
    reference = make_snapshot({}, counts_as_default_scale(counts))

    actual = {t.label.removeprefix("PKG "): t.y for t in build_overlay_traces(wide, wide.data, **overlay_kwargs)}
    expected = {t.label: t.y for t in build_overlay_traces(reference, reference.data, **overlay_kwargs)}

    assert set(actual) == set(expected) == {"Shear L/R Jerk [V]", "Shear T/B Jerk [V]", "Normal Jerk [V]"}
    for label in expected:
        np.testing.assert_allclose(actual[label], expected[label], rtol=1e-6, atol=1e-9)


def test_integration_overlay_uses_the_capture_scale():
    counts = wide_counts()
    kwargs = dict(axis_mode="samples", overlay_flags={"integration": True}, vref_voltage=DEFAULT_FULL_SCALE_V,
                  integration_window_samples=5, hpf_cutoff_hz=1.0)
    wide = make_snapshot({"board_context": wide_board_context()}, counts)
    reference = make_snapshot({}, counts_as_default_scale(counts))
    actual = build_overlay_traces(wide, wide.data, **kwargs)
    expected = build_overlay_traces(reference, reference.data, **kwargs)
    np.testing.assert_allclose(actual[0].y, expected[0].y, rtol=1e-6, atol=1e-9)


# ---- AUTO timing -------------------------------------------------------------------------------

AUTO = {"mux_timing_mode": "auto"}
UNAVAILABLE = "PZT MUX connected time unavailable; choose Manual or Infer from total sample rate"
ADS7953_MARKER = {"timing": {"physical_adc_connection_timing": "unavailable for ADS7953"}}


@pytest.mark.parametrize("metadata", [{"testboard_acquisition": wide_descriptor()}, ADS7953_MARKER])
def test_auto_timing_is_unavailable_without_a_physical_connection_time(metadata):
    with pytest.raises(ValueError, match=UNAVAILABLE) as raised:
        resolve_analysis_timing(make_snapshot(metadata), AUTO)
    assert "ADS7953 physical connection timing unavailable" in str(raised.value)


def test_the_testboard_auto_failure_becomes_a_status_and_continuous_policy():
    policy, status = resolve_analysis_force_timing(make_snapshot({"testboard_acquisition": wide_descriptor()}), AUTO)
    assert policy.mode is TimingMode.CONTINUOUS
    assert UNAVAILABLE in status


@pytest.mark.parametrize("settings, expected_mode", [
    ({"mux_timing_mode": "manual", "mux_connected_time_s": 1e-4}, TimingMode.MANUAL),
    ({"mux_timing_mode": "infer_from_total_sample_rate"}, TimingMode.INFER_FROM_TOTAL_SAMPLE_RATE),
    ({"mux_timing_mode": "continuous"}, TimingMode.CONTINUOUS),
])
def test_other_timing_modes_still_work_on_a_testboard_capture(settings, expected_mode):
    policy, _status = resolve_analysis_timing(make_snapshot({"testboard_acquisition": wide_descriptor()}), settings)
    assert policy.mode is expected_mode


def test_auto_timing_is_untouched_for_captures_with_a_physical_connection_time():
    metadata = {"timing": {"adc_active_sample_interval_us": 100.0}}
    policy, status = resolve_analysis_timing(make_snapshot(metadata), AUTO)
    assert policy.leak_dt_s == pytest.approx(1e-4)
    assert "Auto" in status


# ---- gap-aware blip filter ---------------------------------------------------------------------

GAP_AT = 50
GAP_SECONDS = 0.5
BLIP_WINDOW = 3
STEP_COUNTS = 100.0
SPIKE_COUNTS = 1000.0


def stepped_counts():
    counts = np.zeros((SAMPLE_COUNT, 5))
    counts[GAP_AT:] = STEP_COUNTS
    return counts


def timestamps_with_gap():
    timestamps = np.arange(SAMPLE_COUNT) * SAMPLE_PERIOD_S
    timestamps[GAP_AT:] += GAP_SECONDS
    return timestamps


def test_blip_filter_does_not_blend_samples_across_a_timestamp_gap():
    snapshot = make_snapshot({}, stepped_counts(), timestamps_with_gap())
    filtered = _apply_pzt_blip_filter(snapshot, snapshot.data, BLIP_WINDOW)
    np.testing.assert_array_equal(filtered, snapshot.data)


def test_blip_filter_still_smooths_a_continuous_step_edge():
    snapshot = make_snapshot({}, stepped_counts())
    filtered = _apply_pzt_blip_filter(snapshot, snapshot.data, BLIP_WINDOW)
    assert filtered[GAP_AT, 0] == 0.0


def test_blip_filter_removes_a_single_spike_inside_a_segment_after_a_gap():
    counts = np.zeros((SAMPLE_COUNT, 5))
    counts[GAP_AT + 10] = SPIKE_COUNTS
    snapshot = make_snapshot({}, counts, timestamps_with_gap())
    filtered = _apply_pzt_blip_filter(snapshot, snapshot.data, BLIP_WINDOW)
    assert filtered[GAP_AT + 10, 0] == 0.0


# ---- replay guard ------------------------------------------------------------------------------

def test_a_plain_continuous_capture_can_be_replayed():
    assert replay_unsupported_reason(make_snapshot({})) is None


@pytest.mark.parametrize("metadata", [
    {"testboard_acquisition": {"adc_resolution_bits": DEFAULT_BITS, "full_scale_volts": DEFAULT_FULL_SCALE_V,
                               "reference": DEFAULT_FULL_SCALE_V}},
    {"board_context": wide_board_context()},
    {"board_context": {"hardware": {"adc_resolution_bits": DEFAULT_BITS}, "full_scale_volts": WIDE_FULL_SCALE_V}},
])
def test_testboard_and_non_default_scale_captures_cannot_be_replayed(metadata):
    assert replay_unsupported_reason(make_snapshot(metadata))


def test_a_default_scale_board_context_can_be_replayed():
    metadata = {"board_context": {"hardware": {"adc_resolution_bits": DEFAULT_BITS},
                                  "full_scale_volts": DEFAULT_FULL_SCALE_V}}
    assert replay_unsupported_reason(make_snapshot(metadata)) is None


def test_a_capture_with_a_timestamp_gap_cannot_be_replayed():
    reason = replay_unsupported_reason(make_snapshot({}, timestamps=timestamps_with_gap()))
    assert "gap" in reason


def test_a_legacy_capture_with_its_own_vref_setting_keeps_replaying():
    assert replay_unsupported_reason(make_snapshot({"configuration": {"vref_voltage": WIDE_FULL_SCALE_V}})) is None


# ---- per-package force / baseline removed, and gap-free stage segments --------------------------

PACKAGE_IDS = ("A1_PZT3", "A2_PZT6")
PACKAGE_WINDOW = 5
SAMPLES_PER_ROW = 5  # the "samples" x axis counts every channel of a row
FORCE_FLAGS = {"shear_force": True, "normal_force": True}
FORCE_LABELS = ("Shear Force L/R [N]", "Shear Force T/B [N]", "Normal Force [N]")
TESTBOARD_DEFAULT_SCALE = {"testboard_acquisition": {"adc_resolution_bits": DEFAULT_BITS,
                                                      "full_scale_volts": DEFAULT_FULL_SCALE_V,
                                                      "reference": DEFAULT_FULL_SCALE_V}}


def two_package_snapshot(timestamps=None, counts=None):
    rng = np.random.default_rng(3)
    data = counts if counts is not None else rng.integers(0, 2 ** DEFAULT_BITS, size=(SAMPLE_COUNT, 10)).astype(np.float64)
    labels = [f"{package}_{position}" for package in PACKAGE_IDS for position in POSITIONS]
    return AnalysisSourceSnapshot(
        data=data,
        timestamps_s=timestamps if timestamps is not None else np.arange(len(data)) * SAMPLE_PERIOD_S,
        channel_labels=labels, channel_indices=list(range(10)),
        metadata=TESTBOARD_DEFAULT_SCALE, sample_rate_hz=1.0 / SAMPLE_PERIOD_S,
    )


def package_reference(snapshot, package_index):
    """The same five columns as a plain single-package capture (legacy, unprefixed path)."""
    columns = slice(5 * package_index, 5 * package_index + 5)
    return make_snapshot({}, snapshot.data[:, columns], snapshot.timestamps_s)


def force_by_label(prepared):
    return {trace.label: trace.y for trace in prepared.force_traces}


def prepare_forces(snapshot, flags=FORCE_FLAGS):
    return prepare_analysis_data(snapshot, overlay_flags=flags, integration_window_samples=PACKAGE_WINDOW)


def test_force_traces_exist_for_every_package_with_prefixed_labels():
    snapshot = two_package_snapshot()
    actual = force_by_label(prepare_forces(snapshot))
    assert set(actual) == {f"{package} {label}" for package in PACKAGE_IDS for label in FORCE_LABELS}
    for index, package in enumerate(PACKAGE_IDS):
        expected = force_by_label(prepare_forces(package_reference(snapshot, index)))
        for label in FORCE_LABELS:
            np.testing.assert_allclose(actual[f"{package} {label}"], expected[label])


def test_baseline_removed_replaces_the_signals_of_every_package():
    snapshot = two_package_snapshot()
    flags = {"baseline_removed": True}
    actual = {t.label: t for t in prepare_forces(snapshot, flags).traces}
    for index, package in enumerate(PACKAGE_IDS):
        expected = {t.label.removeprefix("PKG_"): t for t in prepare_forces(package_reference(snapshot, index), flags).traces}
        for position in POSITIONS:
            trace = actual[f"{package}_{position}"]
            assert len(trace.y) == SAMPLE_COUNT - (PACKAGE_WINDOW - 1)
            np.testing.assert_allclose(trace.y, expected[position].y)


def test_a_package_with_fewer_than_five_positions_is_skipped():
    snapshot = two_package_snapshot()
    snapshot = replace(snapshot, channel_labels=snapshot.channel_labels[:9], channel_indices=list(range(9)))
    labels = set(force_by_label(prepare_forces(snapshot)))
    assert labels == {f"{PACKAGE_IDS[0]} {label}" for label in FORCE_LABELS}


class ArrayFilterPanel(AnalysisPanelMixin):
    def __init__(self, array):
        self.analysis_array_combo = type("Combo", (), {"currentText": lambda self: array})()
        self.analysis_snapshot = two_package_snapshot()


@pytest.mark.parametrize("array, label, shown", [
    ("1", "A1_PZT3 Shear Force L/R [N]", True),
    ("1", "A2_PZT6 Normal Force [N]", False),
    ("2", "A2_PZT6 Normal Force [N]", True),
    ("2", "A1_PZT3 Shear Force T/B [N]", False),
    ("2", "Force X [N]", True),
])
def test_the_array_selector_filters_force_traces(array, label, shown):
    assert ArrayFilterPanel(array)._analysis_force_trace_matches_array(label) is shown


def gap_split_references(snapshot):
    return [make_snapshot({}, snapshot.data[:GAP_AT, :5], snapshot.timestamps_s[:GAP_AT]),
            make_snapshot({}, snapshot.data[GAP_AT:, :5], snapshot.timestamps_s[GAP_AT:])]


def single_package_gapped(counts):
    return make_snapshot({}, counts, timestamps_with_gap())


def test_force_stage_state_does_not_cross_a_timestamp_gap():
    counts = np.random.default_rng(4).integers(0, 2 ** DEFAULT_BITS, size=(SAMPLE_COUNT, 5)).astype(np.float64)
    gapped = single_package_gapped(counts)
    before, after = gap_split_references(gapped)
    actual = prepare_forces(gapped).force_traces
    expected = [prepare_forces(part).force_traces for part in (before, after)]
    for index, trace in enumerate(actual):
        np.testing.assert_allclose(trace.y, np.concatenate([expected[0][index].y, expected[1][index].y]))


def test_jerk_and_baseline_stages_restart_after_a_timestamp_gap():
    counts = np.random.default_rng(5).integers(0, 2 ** DEFAULT_BITS, size=(SAMPLE_COUNT, 5)).astype(np.float64)
    gapped = single_package_gapped(counts)
    kwargs = dict(axis_mode="samples", overlay_flags=JERK_FLAGS, vref_voltage=DEFAULT_FULL_SCALE_V,
                  integration_window_samples=PACKAGE_WINDOW, hpf_cutoff_hz=1.0)
    actual = build_overlay_traces(gapped, gapped.data, **kwargs)
    parts = [build_overlay_traces(part, part.data, **kwargs) for part in gap_split_references(gapped)]
    warmup = PACKAGE_WINDOW - 1
    for index, trace in enumerate(actual):
        np.testing.assert_allclose(trace.y, np.concatenate([parts[0][index].y, parts[1][index].y]))
        assert len(trace.y) == SAMPLE_COUNT - 2 * warmup
        assert trace.x[GAP_AT - warmup] == SAMPLES_PER_ROW * (GAP_AT + warmup)

    baseline = prepare_forces(gapped, {"baseline_removed": True}).traces[0]
    assert len(baseline.y) == SAMPLE_COUNT - 2 * warmup


def test_a_gap_free_capture_matches_a_single_engine_pass():
    counts = np.random.default_rng(6).integers(0, 2 ** DEFAULT_BITS, size=(SAMPLE_COUNT, 5)).astype(np.float64)
    snapshot = make_snapshot({}, counts)
    volts = {p: counts_to_volts(counts[:, i], DEFAULT_FULL_SCALE_V) for i, p in enumerate(POSITIONS)}
    reference = JerkOverlayStage(PACKAGE_WINDOW).push(volts)
    traces = build_overlay_traces(snapshot, snapshot.data, axis_mode="samples", overlay_flags=JERK_FLAGS,
                                  vref_voltage=DEFAULT_FULL_SCALE_V, integration_window_samples=PACKAGE_WINDOW,
                                  hpf_cutoff_hz=1.0)
    by_label = {t.label.removeprefix("PKG "): t for t in traces}
    np.testing.assert_allclose(by_label["Shear L/R Jerk [V]"].y, reference.shear_jerk_lr, rtol=1e-6, atol=1e-9)
    np.testing.assert_array_equal(by_label["Normal Jerk [V]"].x, SAMPLES_PER_ROW * np.arange(PACKAGE_WINDOW - 1, SAMPLE_COUNT))
