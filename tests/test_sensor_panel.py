import json
import tempfile
import unittest
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QWidget

from config.sensor_config import SensorConfigStore
from gui.sensor_panel import SensorPanelMixin


class SensorPanelHarness(QWidget, SensorPanelMixin):
    def __init__(self, store: SensorConfigStore):
        super().__init__()
        self.sensor_config_store = store
        self.sensor_configurations = []
        self.active_sensor_config_name = ""
        self._sensor_config_ui_loading = False
        self.logged_messages: list[str] = []

        configs, selected_name = self.sensor_config_store.load()
        self.sensor_configurations = configs
        self.active_sensor_config_name = selected_name

        self.sensor_tab = self.create_sensor_tab()
        self._refresh_sensor_tab_ui()

    def log_status(self, message: str):
        self.logged_messages.append(str(message))


class SensorPanelSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_sync_active_sensor_config_from_editor_persists_pending_rs_mapping(self):
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            bundled_file = tmp_dir / "bundled_sensor_configurations.json"
            local_file = tmp_dir / "user_sensor_configurations.json"
            payload = {
                "selected_name": "Array_PCB1.7",
                "configurations": [
                    {
                        "name": "Array_PCB1.7",
                        "type": "array_layout",
                        "channel_sensor_map": ["B", "L", "C", "R", "T"],
                        "reverse_polarity": False,
                        "array_layout": {
                            "cells": [
                                [None, None, None],
                                [None, "PZT3", None],
                                [None, None, None],
                            ]
                        },
                        "mux_mapping": {
                            "PZT3": {
                                "mux": 1,
                                "channels": [5, 6, 7, 8, 9],
                                "rs_channels": [7, 6],
                            }
                        },
                        "channel_layout": {"channels_per_sensor": 5},
                    }
                ],
            }
            bundled_file.write_text(json.dumps({"configurations": []}), encoding="utf-8")
            local_file.write_text(json.dumps(payload), encoding="utf-8")

            harness = SensorPanelHarness(
                SensorConfigStore(file_path=local_file, bundled_file_path=bundled_file)
            )

            self.assertEqual(
                harness.get_active_sensor_configuration()["mux_mapping"]["PZT3"]["rs_channels"],
                [7, 6],
            )

            rs_item = harness.array_mux_table.item(0, 3)
            self.assertIsNotNone(rs_item)
            rs_item.setText("14,15")

            changed = harness.sync_active_sensor_config_from_editor()

            self.assertTrue(changed)
            self.assertEqual(
                harness.get_active_sensor_configuration()["mux_mapping"]["PZT3"]["rs_channels"],
                [14, 15],
            )

            saved_payload = json.loads(local_file.read_text(encoding="utf-8"))
            saved_by_name = {
                str(config["name"]): config
                for config in saved_payload["configurations"]
            }
            self.assertEqual(
                saved_by_name["Array_PCB1.7"]["mux_mapping"]["PZT3"]["rs_channels"],
                [14, 15],
            )

    def test_sync_active_sensor_config_from_editor_allows_duplicate_rs_channels(self):
        with tempfile.TemporaryDirectory() as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            bundled_file = tmp_dir / "bundled_sensor_configurations.json"
            local_file = tmp_dir / "user_sensor_configurations.json"
            payload = {
                "selected_name": "Array_PCB1.7",
                "configurations": [
                    {
                        "name": "Array_PCB1.7",
                        "type": "array_layout",
                        "channel_sensor_map": ["B", "L", "C", "R", "T"],
                        "reverse_polarity": False,
                        "array_layout": {
                            "cells": [
                                [None, None, None],
                                [None, "PZT3", None],
                                [None, None, None],
                            ]
                        },
                        "mux_mapping": {
                            "PZT3": {
                                "mux": 1,
                                "channels": [5, 6, 7, 8, 9],
                                "rs_channels": [14, 15],
                            }
                        },
                        "channel_layout": {"channels_per_sensor": 5},
                    }
                ],
            }
            bundled_file.write_text(json.dumps({"configurations": []}), encoding="utf-8")
            local_file.write_text(json.dumps(payload), encoding="utf-8")

            harness = SensorPanelHarness(
                SensorConfigStore(file_path=local_file, bundled_file_path=bundled_file)
            )

            rs_item = harness.array_mux_table.item(0, 3)
            self.assertIsNotNone(rs_item)
            rs_item.setText("14,14")

            changed = harness.sync_active_sensor_config_from_editor()

            self.assertTrue(changed)
            self.assertEqual(
                harness.get_active_sensor_configuration()["mux_mapping"]["PZT3"]["rs_channels"],
                [14, 14],
            )


if __name__ == "__main__":
    unittest.main()
