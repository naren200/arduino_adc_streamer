"""Analysis-tab gate preview: a read-only replica of the TouchID tab's replay, with banners instead of dialogs."""

import os
from unittest.mock import patch
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyqtgraph as pg
import pytest

from data_processing.gate_replay import compute_gate_replay
from gui.analysis_panel import AnalysisPanelMixin
from gui.gating_preview_panel import EDIT_ON_TOUCHID_TEXT, STALE_PREDICTIONS_TEXT
from test_replay_driver import make_baseline, make_snapshot
from test_touchid_gating_v2_panel import TRAINED_PARAMS, fake_model, harness, load, qt_app  # noqa: F401


@pytest.fixture
def host(harness):
    # The TouchID host plus the Analysis mixin, with only the widgets the preview touches.
    harness.__class__ = type("PreviewHost", (AnalysisPanelMixin, type(harness)), {})
    harness._init_analysis_state()
    harness.preview_group = harness.create_gating_preview_group()  # keep it alive: nothing parents it here
    harness.analysis_signal_plot = pg.PlotWidget()
    harness.create_gating_preview_plots(harness.analysis_signal_plot)
    harness.analysis_prepared = SimpleNamespace(x_label="Time", x_units="s")
    harness.refreshes = []
    harness.refresh_analysis_plot = lambda: harness.refreshes.append(True)
    harness.touchid_config.window_size_s, harness.touchid_config.hop_size_s = 0.1, 0.05
    snapshot = make_snapshot()
    snapshot.channel_labels = list(harness.touchid_config.pzt_columns)
    harness.analysis_snapshot, harness.analysis_label_segments = snapshot, []
    yield harness
    harness.shutdown_analysis_worker()


def enable_preview(host):
    host.analysis_burst_check.setChecked(True)


def gate_plot_item_count(host):
    return len(host.analysis_gate_plot.listDataItems()) + len(host.analysis_gate_plot.getPlotItem().items)


def test_the_preview_request_is_nothing_while_the_preview_is_off(host):
    assert host.gating_preview_request() is None
    assert host.analysis_gating_baseline_banner.isHidden()


def test_a_required_baseline_that_is_missing_shows_a_banner_and_requests_no_overlay(host):
    load(host, fake_model("v1"))
    enable_preview(host)

    assert host.gating_preview_request() is None

    assert not host.analysis_gating_baseline_banner.isHidden()
    assert host.analysis_gating_baseline_banner.text().startswith("Idle baseline missing")
    assert host.render_gating_preview(None) == {}
    assert gate_plot_item_count(host) == 0


def test_the_request_is_exactly_the_setup_a_touchid_replay_would_use(host):
    load(host, fake_model("v1"))
    host.touchid_idle_baseline = make_baseline()
    enable_preview(host)

    request = host.gating_preview_request()

    assert request == host._touchid_replay_setup_for(host.analysis_snapshot)
    assert request.idle_baseline is not host.touchid_idle_baseline
    assert host.analysis_gating_baseline_banner.isHidden()
    assert "Gating v1" in host.analysis_gating_info_label.text()
    assert "TouchID tab's baseline" in host.analysis_gating_info_label.text()


def test_the_read_only_block_shows_the_strategys_own_description(host):
    load(host, fake_model(params={**TRAINED_PARAMS, "burst_min": 1.0, "sustain_min": 3, "sustain_blocks": 7}))
    enable_preview(host)
    setup = host.gating_preview_request()

    plots = host.render_gating_preview(compute_gate_replay(host.analysis_snapshot, setup))

    text = host.analysis_gating_info_label.text()
    assert "Strength: block of" in text and EDIT_ON_TOUCHID_TEXT in text and "Result:" in text
    assert plots == {"burst": True, "rolling": True, "gate": True}


def test_a_v1_replay_renders_its_windows_only(host):
    load(host, fake_model("v1"))
    host.touchid_idle_baseline = make_baseline()
    enable_preview(host)
    setup = host.gating_preview_request()

    plots = host.render_gating_preview(compute_gate_replay(host.analysis_snapshot, setup))

    assert plots == {"burst": False, "rolling": False, "gate": True}
    assert [name for _row, name in host.analysis_gate_plot.getAxis("left")._tickLevels[0]] == ["accepted"]


def test_predictions_from_other_settings_than_the_overlay_show_a_stale_banner(host):
    load(host, fake_model("v1"))
    host.touchid_idle_baseline = make_baseline()
    enable_preview(host)
    host.analysis_predicted_labels = [{"start_s": 0.0, "end_s": 0.1}]
    host.analysis_predicted_setup = host._touchid_replay_setup_for(host.analysis_snapshot).with_detached_baseline()

    host.refresh_gating_preview_banners()
    assert host.analysis_gating_stale_banner.isHidden()

    host.touchid_config.hop_size_s = 0.1
    host.refresh_gating_preview_banners()
    assert not host.analysis_gating_stale_banner.isHidden()
    assert host.analysis_gating_stale_banner.text() == STALE_PREDICTIONS_TEXT


def test_a_live_edit_of_the_baselines_k_does_not_change_a_stored_setup(host):
    load(host, fake_model("v1"))
    host.touchid_idle_baseline = make_baseline()
    stored = host._touchid_replay_setup_for(host.analysis_snapshot).with_detached_baseline()

    host.touchid_idle_baseline.k = 99.0

    assert stored.idle_baseline.k != 99.0
    assert stored != host._touchid_replay_setup_for(host.analysis_snapshot)


def test_load_last_inference_restores_the_setup_with_the_predictions_and_the_banner_follows(host):
    load(host, fake_model("v1"))
    host.touchid_idle_baseline = make_baseline()
    enable_preview(host)
    host._touchid_show_predicted_labels_exclusively = lambda: None
    host._touchid_last_inference_window_key = host.analysis_snapshot.window_key()
    host._touchid_last_inference_predictions = [{"start_s": 0.0, "end_s": 0.1}]
    host._touchid_last_inference_setup = host._touchid_replay_setup_for(host.analysis_snapshot).with_detached_baseline()
    host.touchid_config.hop_size_s = 0.1  # settings moved on since that replay

    host.on_touchid_load_last_inference_clicked()

    assert host.analysis_predicted_setup is host._touchid_last_inference_setup
    assert not host.analysis_gating_stale_banner.isHidden()


def test_a_touchid_replay_records_the_setup_it_runs_under(host):
    load(host, fake_model("v1"))
    host.touchid_idle_baseline = make_baseline()

    host.on_touchid_run_on_source_clicked()
    host._touchid_replay_timer.stop()

    assert host._touchid_replay_setup_used == host._touchid_replay_setup_for(host.analysis_snapshot)


def test_load_last_inference_is_refused_for_a_different_window(host):
    load(host, fake_model("v1"))
    host._touchid_last_inference_window_key = host.analysis_snapshot.window_key()
    host._touchid_last_inference_predictions = [{"start_s": 0.0, "end_s": 0.1}]
    host.analysis_snapshot = make_snapshot(start_s=100.0)

    with patch("gui.inference_panel.QMessageBox.warning") as warning:
        host.on_touchid_load_last_inference_clicked()

    warning.assert_called_once()
    assert host.analysis_predicted_labels == []


def test_a_changed_window_clears_predictions_and_disables_reload(host):
    load(host, fake_model("v1"))
    host.touchid_load_last_inference_btn.setEnabled(True)
    host._touchid_last_inference_window_key = host.analysis_snapshot.window_key()
    host._touchid_last_inference_predictions = [{"start_s": 0.0, "end_s": 0.1}]
    host.analysis_predicted_labels = host._touchid_last_inference_predictions

    host.touchid_on_analysis_window_changed()
    assert host.analysis_predicted_labels and host.touchid_load_last_inference_btn.isEnabled()

    host.analysis_snapshot = make_snapshot(start_s=100.0)
    host.touchid_on_analysis_window_changed()
    assert host.analysis_predicted_labels == [] and not host.touchid_load_last_inference_btn.isEnabled()


def test_gate_label_overlap_is_clipped_to_the_replayed_window(host):
    host.analysis_label_segments = [{"class": "wood", "start_s": 90.0, "end_s": 100.5}]
    assert host._gating_label_spans(100.0, 101.0) == [(100.0, 100.5)]
    assert host._gating_label_spans(200.0, 201.0) == []
