import unittest

from gui.analysis_panel import AnalysisPanelMixin


class FakePlot:
    def __init__(self):
        self.visible = None

    def setVisible(self, visible):
        self.visible = bool(visible)


class FakeSplitter:
    def __init__(self):
        self.minimum_height = None
        self.sizes = None

    def setMinimumHeight(self, minimum_height):
        self.minimum_height = minimum_height

    def setSizes(self, sizes):
        self.sizes = list(sizes)


class AnalysisPlotVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.harness = AnalysisPanelMixin()
        self.harness.analysis_signal_plot = FakePlot()
        self.harness.analysis_integration_plot = FakePlot()
        self.harness.analysis_derived_plot = FakePlot()
        self.harness.analysis_force_plot = FakePlot()
        self.harness.analysis_plot_splitter = FakeSplitter()

    def test_hides_unrequested_empty_plots_and_collapses_their_space(self):
        self.harness._update_analysis_plot_visibility(
            show_signal=True,
            show_integration=False,
            show_derived=False,
            show_force=False,
        )

        self.assertTrue(self.harness.analysis_signal_plot.visible)
        self.assertFalse(self.harness.analysis_integration_plot.visible)
        self.assertFalse(self.harness.analysis_derived_plot.visible)
        self.assertFalse(self.harness.analysis_force_plot.visible)
        self.assertEqual(self.harness.analysis_plot_splitter.minimum_height, 360)
        self.assertEqual(self.harness.analysis_plot_splitter.sizes, [360, 0, 0, 0])

    def test_shows_each_available_requested_plot(self):
        self.harness._update_analysis_plot_visibility(
            show_signal=True,
            show_integration=True,
            show_derived=True,
            show_force=True,
        )

        self.assertTrue(self.harness.analysis_integration_plot.visible)
        self.assertTrue(self.harness.analysis_derived_plot.visible)
        self.assertTrue(self.harness.analysis_force_plot.visible)
        self.assertEqual(self.harness.analysis_plot_splitter.minimum_height, 1160)
        self.assertEqual(self.harness.analysis_plot_splitter.sizes, [360, 260, 240, 300])


if __name__ == "__main__":
    unittest.main()
