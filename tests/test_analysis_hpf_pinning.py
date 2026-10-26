from unittest.mock import MagicMock

from constants.pressure_map import ANALYSIS_HPF_CUTOFF_HZ
from gui.analysis_panel import AnalysisPanelMixin


class AnalysisHarness(AnalysisPanelMixin):
    def __init__(self):
        self.analysis_snapshot = object()
        self.analysis_signal_plot = object()
        self.analysis_channel_checks = {}
        self.analysis_state = {}
        self._analysis_generation = 0
        self.analysis_filter_check = MagicMock(isChecked=lambda: False)
        self.analysis_compute_worker = MagicMock()
        self.signal_integration_window_samples = 5
        self.signal_integration_hpf_cutoff_hz = 10.0
        self._set_analysis_status_text = lambda _text: None
        self._analysis_source_voltage = lambda: 3.3
        self.gating_preview_request = lambda: None


def test_analysis_request_pins_hpf_to_zero_regardless_of_processor_default():
    harness = AnalysisHarness()

    harness.refresh_analysis_plot()

    payload = harness.analysis_compute_worker.submit.call_args.args[0]
    assert payload["hpf_cutoff_hz"] == ANALYSIS_HPF_CUTOFF_HZ == 0.0
