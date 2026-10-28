"""Pressure Map PZT3 normal force: recording, alignment and CSV export."""
import csv
import json
import threading
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from constants.force import PRESSURE_MAP_NORMAL_FORCE_COLUMN, PRESSURE_MAP_NORMAL_FORCE_SENSOR
from constants.pzt_force import PZT_FORCE_DEFAULT_SETTINGS
from constants.shear import SHEAR_SENSOR_POSITIONS
from data_processing.acquisition_worker import AcquisitionWorker
from data_processing.analysis_workbench import load_exported_csv_snapshot
from data_processing.capture_lifecycle import CaptureLifecycleMixin
from data_processing.force_block_worker import ForceBlockWorker
from data_processing.normal_force_calculator import NormalForceCalculator
from data_processing.normal_force_recorder import (
    NormalForceRecorder,
    NormalForceSeries,
    ensure_normal_force_recorder,
)
from data_processing.pressure_force_display import PressureForceDisplayEngine
from data_processing.pressure_map_array_generator import PressureMapArrayGenerator
from data_processing.pressure_map_geometry import PressureMapGeometry
from data_processing.shear_detector import ShearDetector
from file_operations.force_export_alignment import get_nearest_normal_force
from test_force_export_alignment import ForceExportHarness, workspace_tempdir
from test_signal_integration_panel import SignalIntegrationPanelHarness
from test_testboard_acquisition_pipeline import descriptor

ENGINE_SETTINGS = {"noise_threshold_v": 0.01, "quiet_hold_clear_s": 0.05}
SWEEP_COUNT = 160
LOAD_SWEEPS = range(60, 120)
LOAD_COUNTS = 800.0
BASELINE_COUNTS = 2048.0


def test_recorder_matches_suffix_and_ignores_other_packages():
    recorder = NormalForceRecorder(PRESSURE_MAP_NORMAL_FORCE_SENSOR)
    recorder.record("A1_PZT3", 0.1, 1.0)
    recorder.record("A1_PZT6", 0.1, 9.0)
    recorder.record("a1_pzt3", 0.2, 2.0)

    series = recorder.series(["A1_PZT3"])

    np.testing.assert_array_equal(series.timestamps_s, [0.1, 0.2])
    np.testing.assert_array_equal(series.normal_force_n, [1.0, 2.0])


def test_recorder_prefers_requested_array_and_falls_back_to_sole_series():
    recorder = NormalForceRecorder("PZT3")
    recorder.record("A1_PZT3", 0.0, 1.0)
    recorder.record("A2_PZT3", 0.0, 2.0)

    assert recorder.series(["A2_PZT3", "PZT3"]).normal_force_n.tolist() == [2.0]
    assert recorder.series(["A3_PZT3"]) is None

    sole = NormalForceRecorder("PZT3")
    sole.record("A1_PZT3", 0.0, 5.0)
    assert sole.series(["A2_PZT3"]).normal_force_n.tolist() == [5.0]
    assert NormalForceRecorder("PZT3").series(["A1_PZT3"]) is None


def test_nearest_normal_force_is_blank_outside_the_recording():
    series = NormalForceSeries(np.array([1.0, 2.0, 3.0]), np.array([10.0, 20.0, 30.0]))

    assert get_nearest_normal_force(series, 0.5) is None
    assert get_nearest_normal_force(series, 3.5) is None
    assert get_nearest_normal_force(series, None) is None
    assert get_nearest_normal_force(None, 1.0) is None
    assert get_nearest_normal_force(series, 1.0) == 10.0
    assert get_nearest_normal_force(series, 2.4) == 20.0
    assert get_nearest_normal_force(series, 2.6) == 30.0
    assert get_nearest_normal_force(series, 3.0) == 30.0


def test_engine_normal_forces_match_package_results():
    engine = PressureForceDisplayEngine(geometry=PressureMapGeometry(), settings=ENGINE_SETTINGS)
    voltages = {"A1_PZT3": {position: 0.3 for position in SHEAR_SENSOR_POSITIONS}}
    engine.process_sample(1, {"A1_PZT3": {p: 0.0 for p in SHEAR_SENSOR_POSITIONS}}, 0.0)
    engine.process_sample(2, voltages, 0.01)

    expected = {result.sensor_id: result.normal_force_n for result in engine.package_results()}

    assert engine.normal_forces_n() == expected
    assert expected["A1_PZT3"] != 0.0


def _package_specs(worker_descriptor):
    packages = {}
    for spec in worker_descriptor["channel_specs"]:
        packages.setdefault(spec["key"][1], {})[spec["placement"]] = spec
    return packages


def _force_options(worker_descriptor):
    packages = _package_specs(worker_descriptor)
    return dict(
        revision="test",
        geometry=PressureMapGeometry(),
        settings=dict(ENGINE_SETTINGS),
        packages=packages,
        baselines={tuple(spec["key"]): BASELINE_COUNTS for pkg in packages.values() for spec in pkg.values()},
        voltage_scale=2.5 / 4095.0,
        polarity=1.0,
        grid_positions={package_id: divmod(index, 3) for index, package_id in enumerate(packages)},
        calibration={package_id: {position: 1.0 for position in SHEAR_SENSOR_POSITIONS} for package_id in packages},
    )


def _loaded_sweeps(worker_descriptor):
    """Samples that load only the PZT3 packages for LOAD_SWEEPS, then release."""
    samples = np.full((SWEEP_COUNT, len(worker_descriptor["ordered_routes"])), BASELINE_COUNTS, dtype=np.uint16)
    for spec in worker_descriptor["channel_specs"]:
        if spec["sensor_id"] == PRESSURE_MAP_NORMAL_FORCE_SENSOR:
            column = spec["sample_indices"][0]
            samples[LOAD_SWEEPS.start:LOAD_SWEEPS.stop, column] = BASELINE_COUNTS + LOAD_COUNTS
    return samples


def _run_worker(recorder):
    worker_descriptor = descriptor(50)
    worker = AcquisitionWorker(worker_descriptor, 1, threading.Lock(), 1000, None, normal_force_recorder=recorder)
    worker.set_options({"filter": None, "force": _force_options(worker_descriptor)})
    samples = _loaded_sweeps(worker_descriptor)
    starts = 1000 + np.arange(SWEEP_COUNT) * 1000
    worker.process_batch(dict(
        generation=1, samples=samples, starts=starts, ends=starts + 49, avg_us=np.ones(SWEEP_COUNT),
        arrival=time.perf_counter(), decoded=time.perf_counter(),
    ))
    worker.finish_pending()
    worker._publish_force_result(force=True)
    return worker


def test_worker_records_one_displayed_pzt3_normal_force_per_sweep():
    recorder = NormalForceRecorder(PRESSURE_MAP_NORMAL_FORCE_SENSOR)
    worker = _run_worker(recorder)
    assert worker.error is None

    series = recorder.series(["A1_PZT3"])

    assert len(series.timestamps_s) == SWEEP_COUNT
    np.testing.assert_allclose(series.timestamps_s, worker.times[:SWEEP_COUNT], rtol=0, atol=1e-12)
    assert np.count_nonzero(series.normal_force_n) > 0, "synthetic load must produce a nonzero Normal force"
    assert np.all(series.normal_force_n[:LOAD_SWEEPS.start] == 0.0)
    displayed = {p.sensor_id: p.normal_force_n for p in worker.latest_force_result.package_results}
    assert series.normal_force_n[-1] == displayed["A1_PZT3"]


def test_worker_without_recorder_still_runs_force_path():
    worker = _run_worker(None)

    assert worker.error is None
    assert worker.latest_force_result is not None


def _export_csv(harness):
    with workspace_tempdir("normal_force_export") as tmpdir:
        harness.dir_input._value = str(tmpdir)
        with patch("file_operations.data_exporter.QMessageBox.information"), patch(
            "file_operations.data_exporter.QMessageBox.warning"
        ), patch("file_operations.data_exporter.QMessageBox.critical"):
            harness.save_data()
        csv_path = next(Path(tmpdir).glob("capture_*.csv"))
        metadata_path = next(Path(tmpdir).glob("capture_*_metadata.json"))
        rows = list(csv.reader(csv_path.open("r", encoding="utf-8", newline="")))
        return rows, json.loads(metadata_path.read_text(encoding="utf-8"))


def _harness_with_recording(timestamps, values):
    harness = ForceExportHarness(Path("."))
    recorder = NormalForceRecorder(PRESSURE_MAP_NORMAL_FORCE_SENSOR)
    for timestamp_s, value in zip(timestamps, values):
        recorder.record("A1_PZT3", timestamp_s, value)
    harness.normal_force_recorder = recorder
    return harness


def test_save_data_appends_one_normal_force_column_aligned_by_sweep_time():
    harness = _harness_with_recording([0.05, 0.28, 0.52], [0.5, 1.5, 2.5])

    rows, metadata = _export_csv(harness)

    assert rows[0] == ["Timestamp", "CH0", "Force_X_N", "Force_Z_N", PRESSURE_MAP_NORMAL_FORCE_COLUMN]
    assert [row[-1] for row in rows[1:]] == ["0.5", "1.5", "2.5"]
    assert metadata["pressure_map_normal_force"]["included_in_csv"] is True
    assert metadata["pressure_map_normal_force"]["recorded_sweeps"] == 3
    assert PRESSURE_MAP_NORMAL_FORCE_COLUMN not in metadata["configuration"]["exported_signal_columns"]


def test_save_data_leaves_rows_outside_the_recording_blank():
    harness = _harness_with_recording([0.28, 0.52], [1.5, 2.5])

    rows, _metadata = _export_csv(harness)

    assert [row[-1] for row in rows[1:]] == ["", "1.5", "2.5"]


def test_save_data_without_recording_keeps_original_columns():
    harness = ForceExportHarness(Path("."))

    rows, metadata = _export_csv(harness)

    assert rows[0] == ["Timestamp", "CH0", "Force_X_N", "Force_Z_N"]
    assert metadata["pressure_map_normal_force"]["included_in_csv"] is False


def test_analysis_loader_does_not_treat_normal_force_as_a_signal(tmp_path):
    csv_path = tmp_path / "capture.csv"
    metadata_path = tmp_path / "capture_metadata.json"
    csv_path.write_text(
        f"Timestamp,CH1,Force_X_N,Force_Z_N,{PRESSURE_MAP_NORMAL_FORCE_COLUMN}\n"
        "00:00:00.000000,1,0.5,1.5,\n"
        "00:00:00.010000,3,0.6,1.6,0.25\n",
        encoding="utf-8",
    )
    metadata_path.write_text(
        json.dumps({
            "configuration": {"channels": [1], "repeat_count": 1},
            "capture_duration_seconds": 0.01,
            "timing": {"arduino_sample_rate_hz": 200.0},
        }),
        encoding="utf-8",
    )

    snapshot = load_exported_csv_snapshot(csv_path, metadata_path)

    assert snapshot.channel_labels == ["CH1"]
    assert snapshot.data.shape == (2, 1)


# --- Legacy block path (binary_processor -> process_pressure_force_block) ---------------------

LEGACY_SWEEPS = 40


def _legacy_path_harness():
    harness = SignalIntegrationPanelHarness()
    harness.pressure_map_geometry = PressureMapGeometry()
    engine = PressureForceDisplayEngine(geometry=harness.pressure_map_geometry, settings=ENGINE_SETTINGS)
    engine.configure_layout({"PZT3": (0, 0)})
    harness.pressure_force_engine = engine
    harness._force_worker = ForceBlockWorker(engine, ensure_normal_force_recorder(harness))
    harness.get_display_channel_specs = lambda: [
        {"key": ("sensor", "PZT3", position), "label": f"PZT3_{position}", "sample_indices": [index]}
        for index, position in enumerate(SHEAR_SENSOR_POSITIONS)
    ]
    harness._is_pressure_map_force_display_visible = lambda: True
    harness.is_pzt_ghost_block_net_centered = lambda: True
    harness.plot_baselines = {("sensor", "PZT3", position): 0.0 for position in SHEAR_SENSOR_POSITIONS}
    return harness, engine


def test_legacy_block_path_records_pzt3_normal_force_per_sweep():
    harness, engine = _legacy_path_harness()
    block = np.zeros((LEGACY_SWEEPS, len(SHEAR_SENSOR_POSITIONS)))
    block[LEGACY_SWEEPS // 2:, :] = LOAD_COUNTS
    times = np.arange(LEGACY_SWEEPS, dtype=np.float64) * 1e-3

    harness.process_pressure_force_block(block, times, first_sweep_id=0, avg_sample_time_us=1.0)
    harness._force_worker.drain_synchronous()

    series = harness.normal_force_recorder.series(["PZT3"])
    np.testing.assert_allclose(series.timestamps_s, times, rtol=0, atol=1e-12)
    assert np.all(series.normal_force_n[: LEGACY_SWEEPS // 2] == 0.0)
    assert np.count_nonzero(series.normal_force_n) > 0
    assert series.normal_force_n[-1] == engine.normal_forces_n()["PZT3"]


def test_rebuilding_the_force_engine_gives_the_worker_the_apps_recorder():
    harness = SignalIntegrationPanelHarness()
    harness.pressure_map_geometry = PressureMapGeometry()
    harness.normal_force_calculator = NormalForceCalculator()
    harness.shear_detector = ShearDetector()
    harness.pressure_map_array_generator = PressureMapArrayGenerator(geometry=harness.pressure_map_geometry)
    harness._pzt_force_settings = lambda: dict(PZT_FORCE_DEFAULT_SETTINGS)

    with patch("gui.signal_integration_panel.ForceBlockWorker", MagicMock()) as worker_class:
        harness._rebuild_pressure_force_engine()

    worker_class.assert_called_once_with(harness.pressure_force_engine, harness.normal_force_recorder)


def test_capture_start_reset_clears_the_recording():
    recorder = NormalForceRecorder("PZT3")
    recorder.record("PZT3", 0.0, 1.0)
    owner = MagicMock(normal_force_recorder=recorder)

    with patch("data_processing.capture_lifecycle.get_force_runtime_state", return_value=MagicMock()):
        CaptureLifecycleMixin._reset_force_capture_state(owner)

    assert recorder.series(["PZT3"]) is None
