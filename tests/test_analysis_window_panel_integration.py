import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyqtgraph as pg
import pytest
from PyQt6.QtWidgets import QApplication

from data_processing.analysis_source_load_worker import AnalysisSourceLoadWorker
from data_processing.analysis_window_source import WINDOW_BUFFER_S, WINDOW_STEP_S, WINDOW_VIEW_S
from gui.analysis_panel import AnalysisPanelMixin
from test_analysis_window_source import _open

SPAN_ROWS = 400  # 200 s
APP = QApplication.instance() or QApplication([])  # module-level so Qt's app is not garbage-collected


class SynchronousLoadWorker:
    """Runs the real AnalysisSourceLoadWorker._process on submit and feeds the panel."""

    def __init__(self, panel):
        self.panel = panel
        self.process = AnalysisSourceLoadWorker()._process

    def submit(self, payload):
        result = self.process(payload)
        self.panel._on_analysis_source_load_result({**result, "generation": payload["generation"]})

    def stop(self):
        pass

    def wait(self, _timeout):
        pass


@pytest.fixture
def panel(tmp_path):
    panel =AnalysisPanelMixin()
    panel._init_analysis_state()
    panel.analysis_source_load_worker.stop()
    panel.analysis_source_load_worker.wait(1500)
    panel.analysis_source_load_worker = SynchronousLoadWorker(panel)
    panel.controls_container = panel._build_analysis_window_controls()
    (panel.analysis_signal_plot, panel.analysis_integration_plot,
     panel.analysis_derived_plot, panel.analysis_force_plot) = [pg.PlotWidget() for _ in range(4)]
    panel._set_analysis_status_text = lambda _text: None
    panel.refresh_analysis_plot = lambda: None
    panel._rebuild_analysis_channel_checks = lambda: None
    panel._load_labels_for_current_source = lambda: None
    panel.save_last_analysis_settings = lambda: None
    yield panel
    panel.shutdown_analysis_worker()


def install_source(panel, tmp_path):
    source = _open(tmp_path, SPAN_ROWS)
    panel.analysis_window_source = source
    panel.analysis_snapshot = source.snapshot_for(0.0)
    panel._finish_analysis_source_loaded()
    return source


def test_right_clicks_slide_the_real_window_and_stay_bounded(panel, tmp_path):
    install_source(panel, tmp_path)

    panel.on_analysis_move_view(1)
    panel.on_analysis_move_view(1)

    snapshot = panel.analysis_snapshot
    assert snapshot.view_range_s == (2 * WINDOW_STEP_S, 2 * WINDOW_STEP_S + WINDOW_VIEW_S)
    assert snapshot.resident_range_s == (2 * WINDOW_STEP_S - WINDOW_BUFFER_S, 2 * WINDOW_STEP_S + WINDOW_VIEW_S + WINDOW_BUFFER_S)
    resident_s = snapshot.timestamps_s[-1] - snapshot.timestamps_s[0]
    assert resident_s <= WINDOW_VIEW_S + 2 * WINDOW_BUFFER_S
    assert panel.analysis_view_right_btn.isEnabled() is True
    assert panel.analysis_view_left_btn.isEnabled() is True


def test_moves_keep_the_source_object_and_clamp_at_the_end(panel, tmp_path):
    source = install_source(panel, tmp_path)

    for _ in range(10):
        panel.on_analysis_move_view(1)

    assert panel.analysis_window_source is source
    assert panel.analysis_snapshot.view_range_s[1] == source.span_s[1]
    assert panel.analysis_view_right_btn.isEnabled() is False
