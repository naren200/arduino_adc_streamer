import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from gui.inference_panel import (
    _TOUCHID_ONSET_SKIP_MAX_S,
    _TOUCHID_WINDOW_MIN_S,
    InferencePanelMixin,
)

ONSET_SKIP_S = 0.025


def _stub_panel(active_queue):
    # The panel is too heavy to construct headlessly, so exercise the
    # unbound handler against a lightweight stand-in.
    return SimpleNamespace(
        touchid_config=SimpleNamespace(onset_skip_s=0.0),
        touchid_processor=SimpleNamespace(onset_skip_s=0.0, active_queue=active_queue),
        save_last_touchid_settings=MagicMock(),
    )


class TouchIdOnsetControlTests(unittest.TestCase):
    def test_handler_updates_config_processor_and_live_queue(self):
        panel = _stub_panel(SimpleNamespace(onset_skip_s=0.0))

        InferencePanelMixin.on_touchid_onset_skip_changed(panel, ONSET_SKIP_S)

        expected_s = ONSET_SKIP_S
        self.assertEqual(panel.touchid_config.onset_skip_s, expected_s)
        self.assertEqual(panel.touchid_processor.onset_skip_s, expected_s)
        self.assertEqual(panel.touchid_processor.active_queue.onset_skip_s, expected_s)
        panel.save_last_touchid_settings.assert_called_once()

    def test_handler_tolerates_missing_queue(self):
        panel = _stub_panel(None)

        InferencePanelMixin.on_touchid_onset_skip_changed(panel, ONSET_SKIP_S)

        self.assertEqual(panel.touchid_config.onset_skip_s, ONSET_SKIP_S)

    def test_spin_max_is_effectively_unbounded(self):
        self.assertGreater(_TOUCHID_ONSET_SKIP_MAX_S, 100 * _TOUCHID_WINDOW_MIN_S)


if __name__ == '__main__':
    unittest.main()
