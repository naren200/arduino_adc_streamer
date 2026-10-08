"""The live TouchID path runs the loaded model runtime's EngineConfig, refuses a model whose
config cannot be rebuilt by this engine, and warns when the GUI ingest blip filter conflicts
with what the model was trained on."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from core.piezo_engine.config import EngineConfig, EngineConfigMismatchError, TimingMode, TimingPolicy
from core.texture_piezo.application.live_channels import DEFAULT_CHANNELS
from core.texture_piezo.application.stream_processor import LIVE_ENGINE_CONFIG
from test_inference_panel import TouchIdHarness


def runtime_with_blip_window(window_samples, expects_ingest_filter=False):
    """A loaded model as the app sees it: engine_config dict, required channels, ingest expectation."""
    config = EngineConfig(timing=TimingPolicy(mode=TimingMode.CONTINUOUS), blip_window_samples=window_samples,
                          compute_force=False)
    return SimpleNamespace(
        engine_config=config.to_dict(), required_channels=DEFAULT_CHANNELS,
        expected_ingest_blip_filter=expects_ingest_filter,
    )


def engine_config_of(runtime):
    return EngineConfig.from_dict(runtime.engine_config)


class TouchIdModelBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_harness(self, runtime=None):
        harness = TouchIdHarness()
        harness.log = []
        harness.log_status = harness.log.append
        harness.touchid_classifier = runtime
        harness.touchid_classifier_error = None
        harness._touchid_ingest_warning = None
        return harness

    def test_processors_are_built_with_the_model_config(self):
        runtime = runtime_with_blip_window(5)
        harness = self.make_harness(runtime)
        self.assertEqual(harness._touchid_new_processor().engine_config, engine_config_of(runtime))

    def test_replay_processor_uses_the_model_config_too(self):
        runtime = runtime_with_blip_window(5)
        harness = self.make_harness(runtime)
        replay = harness._touchid_new_processor(idle_baseline=None)
        self.assertEqual(replay.engine_config.blip_window_samples, 5)

    def test_without_a_model_the_default_force_less_config_runs(self):
        harness = self.make_harness(None)
        self.assertIs(harness._touchid_new_processor().engine_config, LIVE_ENGINE_CONFIG)

    def test_resize_continues_engine_state_while_the_config_is_unchanged(self):
        harness = self.make_harness(runtime_with_blip_window(5))
        harness.touchid_processor = harness._touchid_new_processor()
        with patch.object(type(harness.touchid_processor), "adopt_engine_state_from") as adopt:
            harness._rebuild_touchid_buffers()
        adopt.assert_called_once()

    def test_resize_starts_cold_when_the_config_differs_from_the_running_processor(self):
        harness = self.make_harness(runtime_with_blip_window(5))
        old_processor = harness._touchid_new_processor(continue_from=None)
        harness.touchid_classifier = runtime_with_blip_window(7)
        with patch.object(type(old_processor), "adopt_engine_state_from") as adopt:
            new_processor = harness._touchid_new_processor(continue_from=old_processor)
        adopt.assert_not_called()
        self.assertEqual(new_processor.engine_config.blip_window_samples, 7)

    def test_reload_with_a_different_engine_config_rebuilds_the_processor(self):
        harness = self.make_harness(runtime_with_blip_window(5))
        harness.touchid_processor = harness._touchid_new_processor()
        replacement = runtime_with_blip_window(7)
        with patch("gui.inference_panel.TextureClassifier", return_value=replacement):
            harness._touchid_reload_model()
        self.assertEqual(harness.touchid_processor.engine_config.blip_window_samples, 7)
        self.assertIsNone(harness.touchid_classifier_error)

    def test_reload_with_the_same_engine_config_keeps_the_running_processor(self):
        harness = self.make_harness(runtime_with_blip_window(5))
        harness.touchid_processor = harness._touchid_new_processor()
        running = harness.touchid_processor
        with patch("gui.inference_panel.TextureClassifier", return_value=runtime_with_blip_window(5)):
            harness._touchid_reload_model()
        self.assertIs(harness.touchid_processor, running)

    def test_mismatching_model_is_refused_with_a_clear_status_and_no_classifier(self):
        harness = self.make_harness(None)
        error = EngineConfigMismatchError("cannot be rebuilt")
        with patch("gui.inference_panel.TextureClassifier", side_effect=error):
            harness._touchid_reload_model()
        self.assertIsNone(harness.touchid_classifier)
        self.assertIn("model refused", harness.touchid_classifier_error)
        self.assertIn("engine config mismatch", harness.touchid_status_label.text())
        self.assertIn("reload failed", harness.log[-1])

    def test_a_model_whose_engine_config_cannot_be_rebuilt_is_refused_at_load(self):
        harness = self.make_harness(None)
        broken = SimpleNamespace(engine_config={"unknown_key": 1}, required_channels=DEFAULT_CHANNELS)
        with patch("gui.inference_panel.TextureClassifier", return_value=broken):
            harness._touchid_reload_model()
        self.assertIsNone(harness.touchid_classifier)
        self.assertIn("model refused", harness.touchid_classifier_error)

    def test_ingest_warning_is_logged_once_per_state_change_and_shown_on_the_status_label(self):
        harness = self.make_harness(runtime_with_blip_window(5, expects_ingest_filter=False))
        harness.pzt_blip_filter_enabled = True
        harness._touchid_check_ingest_filter_warning()
        harness._touchid_check_ingest_filter_warning()
        self.assertEqual(len(harness.log), 1)
        self.assertIn("WARNING", harness.log[0])
        self.assertIn("WARNING", harness.touchid_status_label.text())

        harness.pzt_blip_filter_enabled = False
        harness._touchid_check_ingest_filter_warning()
        self.assertNotIn("WARNING", harness.touchid_status_label.text())
        harness.pzt_blip_filter_enabled = True
        harness._touchid_check_ingest_filter_warning()
        self.assertEqual(len(harness.log), 2)

    def test_no_warning_without_the_mixin_flag_or_for_a_model_that_expects_the_ingest_filter(self):
        harness = self.make_harness(runtime_with_blip_window(5, expects_ingest_filter=False))
        harness._touchid_check_ingest_filter_warning()
        self.assertEqual(harness.log, [])
        harness = self.make_harness(runtime_with_blip_window(5, expects_ingest_filter=True))
        harness.pzt_blip_filter_enabled = True
        harness._touchid_check_ingest_filter_warning()
        self.assertEqual(harness.log, [])


if __name__ == "__main__":
    unittest.main()
