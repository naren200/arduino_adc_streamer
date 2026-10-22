import numpy as np

from data_processing.analysis_workbench import AnalysisSourceSnapshot
from data_processing.burst_score import derive_engine_view
from data_processing.gating_preview import (
    GatePreviewSettings,
    LiveGateParams,
    _label_overlap_line,
    merge_ranges,
    run_gate_preview,
    span_overlap_s,
)

FS = 1000.0
LABELS = ["PZT5_B", "PZT5_L", "PZT5_C", "PZT5_R", "PZT5_T"]
LIVE = LiveGateParams(window_size_s=0.5, hop_size_s=0.1, onset_skip_s=0.0, idle_baseline=None)


def _snapshot(seconds=20.0):
    rng = np.random.default_rng(0)
    n = int(seconds * FS)
    counts = 1600 + rng.normal(0, 2, (n, 5))
    counts[8000:12000] += rng.normal(0, 200, (4000, 5))  # one loud stretch, 8 s .. 12 s
    return AnalysisSourceSnapshot(
        data=counts.astype(np.float32), timestamps_s=np.arange(n) / FS, channel_labels=LABELS,
        metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
        source_id="unit", sample_rate_hz=FS,
    )


def _preview(**overrides):
    settings = GatePreviewSettings(idle_enabled=False, burst_min=2.0, **overrides)
    view = derive_engine_view(_snapshot(), 3.3, "time_ms")
    return run_gate_preview(view, FS, settings, LIVE)


def test_accepted_samples_come_only_from_the_loud_stretch():
    preview = _preview()
    assert preview.error == ""
    assert preview.accepted, preview.logic_text
    assert all(start >= 7.0 and end <= 13.0 for start, end in preview.accepted)  # x is in seconds


def test_accepted_is_inside_the_strength_stage():
    preview = _preview()
    first, last = preview.strength[0][0], preview.strength[-1][1]  # pieces are adjacent, so their span is the stage
    for start, end in preview.accepted:
        assert first <= start and end <= last


def test_logic_text_states_the_stages_and_the_result():
    text = _preview().logic_text
    assert "Final gate: strength" in text and "Result:" in text


def test_expiry_shorter_than_the_window_is_reported_not_raised():
    preview = _preview(expiry_s=0.1)
    assert "expiry_s" in preview.error and preview.accepted == []


def test_merge_ranges_joins_touching_and_overlapping():
    assert merge_ranges([(5, 8), (0, 3), (3, 4), (7, 9)]) == [(0, 4), (5, 9)]


def test_idle_stage_marks_only_the_active_stretch_when_a_baseline_exists():
    from core.texture_piezo.gating.v1.quality_gate import fit_idle_baseline

    quiet = 1600 + np.random.default_rng(5).normal(0, 2, (5000, 5))
    baseline = fit_idle_baseline(quiet, LABELS, FS)
    live = LiveGateParams(window_size_s=0.5, hop_size_s=0.1, onset_skip_s=0.0, idle_baseline=baseline)
    view = derive_engine_view(_snapshot(), 3.3, "time_ms")

    preview = run_gate_preview(view, FS, GatePreviewSettings(strength_enabled=False, idle_enabled=True), live)

    assert preview.idle and preview.strength == []
    assert all(start >= 7.0 and end <= 13.0 for start, end in preview.accepted)


def test_idle_gate_without_a_baseline_is_called_out_and_constrains_nothing():
    view = derive_engine_view(_snapshot(), 3.3, "time_ms")

    preview = run_gate_preview(view, FS, GatePreviewSettings(strength_enabled=False, idle_enabled=True), LIVE)

    assert "no idle baseline" in preview.logic_text
    assert preview.idle == []


def _worker_payload(**extra):
    return {
        "snapshot": _snapshot(), "axis_mode": "time_ms", "visible_labels": LABELS, "filter_enabled": False,
        "filter_settings": {}, "overlay_flags": {}, "vref_voltage": 3.3, "integration_window_samples": 1,
        "hpf_cutoff_hz": 0.0, "pzt_force_settings": {}, **extra,
    }


def test_compute_worker_returns_the_preview_on_the_prepared_data_when_requested():
    from data_processing.analysis_compute_worker import AnalysisComputeWorker

    worker = AnalysisComputeWorker()
    request = {"settings": GatePreviewSettings(idle_enabled=False, burst_min=2.0), "live": LIVE}

    result = worker._process(_worker_payload(gating_request=request))

    assert result["prepared"].gating is not None and result["prepared"].gating.preview.accepted


def test_compute_worker_skips_the_preview_when_it_is_off():
    from data_processing.analysis_compute_worker import AnalysisComputeWorker

    assert AnalysisComputeWorker()._process(_worker_payload(gating_request=None))["prepared"].gating is None


def test_span_overlap_counts_only_shared_seconds():
    assert span_overlap_s([(0.0, 4.0), (6.0, 8.0)], [(3.0, 7.0)]) == 2.0
    assert span_overlap_s([(0.0, 1.0)], [(2.0, 3.0)]) == 0.0


def test_label_overlap_line_reports_labelled_coverage_and_unlabelled_share():
    line = _label_overlap_line([(0.0, 4.0), (6.0, 8.0)], [(3.0, 7.0)])
    assert line == "Hand labels: 4.0 s labelled, 50% of it accepted; 4.0 s (67%) of the accepted signal is unlabelled."
