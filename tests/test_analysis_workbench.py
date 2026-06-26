import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from data_processing.analysis_workbench import (
    AnalysisSourceSnapshot,
    build_overlay_traces,
    load_exported_csv_snapshot,
    prepare_analysis_data,
    reorder_circular_capture,
)


class AnalysisWorkbenchTests(unittest.TestCase):
    def test_reorder_circular_capture_returns_oldest_to_newest(self):
        data = np.asarray(
            [
                [40, 41],
                [50, 51],
                [10, 11],
                [20, 21],
                [30, 31],
            ],
            dtype=np.float32,
        )
        timestamps = np.asarray([4, 5, 1, 2, 3], dtype=np.float64)

        ordered, ordered_timestamps = reorder_circular_capture(
            data,
            timestamps,
            sweep_count=7,
            write_index=2,
            max_sweeps=5,
        )

        np.testing.assert_array_equal(
            ordered,
            np.asarray([[10, 11], [20, 21], [30, 31], [40, 41], [50, 51]], dtype=np.float32),
        )
        np.testing.assert_array_equal(ordered_timestamps, np.asarray([1, 2, 3, 4, 5], dtype=np.float64))

    def test_load_exported_csv_snapshot_validates_metadata_column_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            csv_path = temp_path / "capture.csv"
            metadata_path = temp_path / "capture_metadata.json"
            csv_path.write_text(
                "Timestamp,CH1,CH2,Force_X_N,Force_Z_N\n"
                "00:00:00.000000,1,2,0.5,1.5\n"
                "00:00:00.010000,3,4,0.6,1.6\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                json.dumps(
                    {
                        "configuration": {"channels": [1, 2], "repeat_count": 1},
                        "capture_duration_seconds": 0.01,
                        "timing": {"arduino_sample_rate_hz": 200.0},
                    }
                ),
                encoding="utf-8",
            )

            snapshot = load_exported_csv_snapshot(csv_path, metadata_path)

            self.assertEqual(snapshot.channel_labels, ["CH1", "CH2"])
            self.assertEqual(snapshot.data.shape, (2, 2))
            np.testing.assert_allclose(snapshot.timestamps_s, [0.0, 0.01])
            np.testing.assert_allclose(snapshot.force_x_n, [0.5, 0.6])

    def test_load_exported_csv_snapshot_accepts_array_export_force_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            csv_path = temp_path / "array.csv"
            metadata_path = temp_path / "array_metadata.json"
            csv_path.write_text(
                "PZT6_B,PZT6_L,PZT6_C,PZT6_R,PZT6_T,PZT6_RS1,PZT6_RS2,Force_X,Force_Z\n"
                "2046,2052,2039,2049,2044,474.6,455.42,0,0\n"
                "2044,2052,2038,2050,2044,474.6,455.42,0,0\n",
                encoding="utf-8",
            )
            metadata_path.write_text(
                json.dumps(
                    {
                        "configuration": {
                            "channels": [10, 11, 12, 13, 14],
                            "repeat_count": 1,
                            "buffer_total_samples": 7,
                        },
                        "capture_duration_seconds": 0.01,
                        "timing": {"arduino_sample_rate_hz": 20833.333333333332},
                    }
                ),
                encoding="utf-8",
            )

            snapshot = load_exported_csv_snapshot(csv_path, metadata_path)

            self.assertEqual(snapshot.channel_labels, [
                "PZT6_B",
                "PZT6_L",
                "PZT6_C",
                "PZT6_R",
                "PZT6_T",
                "PZT6_RS1",
                "PZT6_RS2",
            ])
            self.assertEqual(snapshot.data.shape, (2, 7))
            np.testing.assert_allclose(snapshot.force_x_n, [0.0, 0.0])

    def test_prepare_analysis_data_builds_requested_overlays(self):
        snapshot = AnalysisSourceSnapshot(
            data=np.asarray(
                [
                    [100, -100, 80, 40, -40],
                    [120, -120, 100, 50, -50],
                    [140, -140, 120, 60, -60],
                ],
                dtype=np.float32,
            ),
            timestamps_s=np.asarray([0.0, 0.01, 0.02], dtype=np.float64),
            channel_labels=["C", "L", "R", "T", "B"],
            metadata={"configuration": {"channels": [1, 2, 3, 4, 5], "repeat_count": 1}},
            source_id="unit",
            sample_rate_hz=500.0,
        )

        prepared = prepare_analysis_data(
            snapshot,
            axis_mode="time_ms",
            overlay_flags={"shear": True, "normal": True, "integration": True},
            vref_voltage=3.3,
            integration_window_samples=1,
            hpf_cutoff_hz=0.0,
        )

        overlay_labels = {trace.label for trace in prepared.overlay_traces}
        self.assertIn("Shear L/R [V]", overlay_labels)
        self.assertIn("Shear T/B [V]", overlay_labels)
        self.assertIn("Normal Pressure [V]", overlay_labels)
        self.assertIn("Integrated C [V samples]", overlay_labels)

        direct_overlays = build_overlay_traces(
            snapshot,
            snapshot.data,
            axis_mode="samples",
            overlay_flags={"shear": True},
            vref_voltage=3.3,
            integration_window_samples=1,
            hpf_cutoff_hz=0.0,
        )
        self.assertEqual([trace.label for trace in direct_overlays], ["Shear L/R [V]", "Shear T/B [V]"])


if __name__ == "__main__":
    unittest.main()
