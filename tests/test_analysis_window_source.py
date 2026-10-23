import json

import numpy as np
import pytest

from core.piezo_engine.pipeline import INPUT_CONDITIONING_METADATA_KEY
from data_processing.analysis_source_load_worker import AnalysisSourceLoadWorker
from data_processing.analysis_window_source import (
    WINDOW_BUFFER_S,
    WINDOW_VIEW_S,
    ArraySnapshotWindowSource,
    CsvWindowSource,
)
from data_processing.analysis_workbench import AnalysisSourceSnapshot, load_exported_csv_snapshot

SAMPLE_PERIOD_S = 0.5
LONG_ROWS = 600  # 300 s
MAX_RESIDENT_S = WINDOW_VIEW_S + 2 * WINDOW_BUFFER_S


def _write_metadata(tmp_path, **extra):
    path = tmp_path / "capture_metadata.json"
    payload = {"configuration": {"channels": [1, 2], "repeat_count": 1}, "capture_duration_seconds": 10.0, **extra}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_csv(tmp_path, row_count, *, with_timestamp=True, header=None, with_force=True):
    columns = header or (["Timestamp_s"] if with_timestamp else []) + ["CH1", "CH2"] + (
        ["Force_X_N", "Force_Z_N"] if with_force else []
    )
    lines = ["# exported by test", ",".join(columns)]
    for row in range(row_count):
        cells = []
        for column in columns:
            if column == "Timestamp_s":
                cells.append(f"{row * SAMPLE_PERIOD_S:.3f}")
            elif column.startswith("Force"):
                cells.append(f"{row * 0.01:.4f}")
            else:
                cells.append(str(2000 + (row * 7) % 50))
        lines.append(",".join(cells))
        if row == 3:
            lines.append("# comment between rows")
    path = tmp_path / "capture.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _open(tmp_path, row_count=LONG_ROWS, *, blip=False, **csv_kwargs):
    csv_path = _write_csv(tmp_path, row_count, **csv_kwargs)
    return CsvWindowSource.open(
        csv_path, _write_metadata(tmp_path), blip_filter_enabled=blip, blip_filter_window_samples=3
    )


def test_range_read_equals_slice_of_full_read(tmp_path):
    source = _open(tmp_path)
    full = load_exported_csv_snapshot(
        tmp_path / "capture.csv", tmp_path / "capture_metadata.json", blip_filter_enabled=False
    )
    window = source.snapshot_for(100.0)
    low, high = window.resident_range_s
    mask = (full.timestamps_s >= low) & (full.timestamps_s <= high)
    np.testing.assert_array_equal(window.data, full.data[mask])
    np.testing.assert_array_equal(window.timestamps_s, full.timestamps_s[mask])
    np.testing.assert_array_equal(window.force_x_n, full.force_x_n[mask])
    np.testing.assert_array_equal(window.force_z_n, full.force_z_n[mask])
    np.testing.assert_array_equal(window.force_timestamps_s, full.timestamps_s[mask])


def test_resident_range_is_view_plus_buffer_clamped_to_span(tmp_path):
    source = _open(tmp_path)
    first, last = source.span_s
    middle = source.snapshot_for(100.0)
    assert middle.view_range_s == (100.0, 100.0 + WINDOW_VIEW_S)
    assert middle.resident_range_s == (100.0 - WINDOW_BUFFER_S, 160.0 + WINDOW_BUFFER_S)
    assert middle.span_s == (first, last)
    start = source.snapshot_for(first)
    assert start.resident_range_s == (first, first + WINDOW_VIEW_S + WINDOW_BUFFER_S)
    end = source.snapshot_for(last)
    assert end.view_range_s == (last - WINDOW_VIEW_S, last)
    assert end.resident_range_s == (last - WINDOW_VIEW_S - WINDOW_BUFFER_S, last)


def test_clamp_view_start(tmp_path):
    source = _open(tmp_path)
    first, last = source.span_s
    assert source.clamp_view_start(-50.0) == first
    assert source.clamp_view_start(last + 50.0) == last - WINDOW_VIEW_S
    assert source.clamp_view_start(80.0) == 80.0


def test_source_shorter_than_view_shows_whole_span(tmp_path):
    source = _open(tmp_path, row_count=40)
    first, last = source.span_s
    assert source.clamp_view_start(10.0) == first
    window = source.snapshot_for(10.0)
    assert window.view_range_s == (first, last)
    assert window.sweep_count == 40


def test_resident_snapshot_is_bounded(tmp_path):
    source = _open(tmp_path)
    window = source.snapshot_for(120.0)
    assert window.sweep_count <= MAX_RESIDENT_S / SAMPLE_PERIOD_S + 1
    assert window.sweep_count < LONG_ROWS


def test_sample_rate_is_identical_across_windows(tmp_path):
    source = _open(tmp_path)
    rates = {source.snapshot_for(start).sample_rate_hz for start in (0.0, 90.0, 240.0)}
    assert rates == {source.sample_rate_hz}
    assert source.sample_rate_hz > 0.0


def test_window_key_and_fingerprint_change_between_windows(tmp_path):
    source = _open(tmp_path)
    first, second = source.snapshot_for(0.0), source.snapshot_for(100.0)
    assert first.window_key() != second.window_key()
    assert first.window_key() == source.snapshot_for(0.0).window_key()
    assert first.fingerprint() != second.fingerprint()


def test_blip_filter_is_applied_per_window(tmp_path):
    source = _open(tmp_path, blip=True)
    window = source.snapshot_for(100.0)
    assert INPUT_CONDITIONING_METADATA_KEY in window.metadata
    assert window.sweep_count > 0


def test_array_snapshot_window_source_slices_by_time():
    count = 400
    times = np.arange(count) * SAMPLE_PERIOD_S
    snapshot = AnalysisSourceSnapshot(
        data=np.arange(count * 2, dtype=np.float32).reshape(count, 2),
        timestamps_s=times,
        channel_labels=["A", "B"],
        force_timestamps_s=times.copy(),
        force_x_n=np.arange(count, dtype=np.float64),
        force_z_n=np.arange(count, dtype=np.float64) * 2,
        source_id="in_memory",
        sample_rate_hz=10.0,
    )
    source = ArraySnapshotWindowSource(snapshot)
    window = source.snapshot_for(50.0)
    low, high = window.resident_range_s
    assert (low, high) == (30.0, 130.0)
    assert window.timestamps_s[0] == low and window.timestamps_s[-1] == high
    assert window.data.shape[0] == window.force_x_n.size == window.force_z_n.size
    np.testing.assert_array_equal(window.force_timestamps_s, window.timestamps_s)
    assert window.metadata is snapshot.metadata
    assert window.sample_rate_hz == 10.0
    assert source.clamp_view_start(1e6) == times[-1] - WINDOW_VIEW_S


def test_legacy_placeholder_and_force_columns(tmp_path):
    header = ["Timestamp_s", "PZT3_B", "Col1", "PZT3_L", "Col3", "Force_X", "Force_Z"]
    csv_path = _write_csv(tmp_path, 20, header=header)
    source = CsvWindowSource.open(csv_path, _write_metadata(tmp_path), blip_filter_enabled=False)
    window = source.snapshot_for(0.0)
    assert window.channel_labels == ["PZT3_B", "PZT3_L"]
    assert window.force_x_n.size == 20
    assert "metadata expects" not in " ".join(window.metadata.get("analysis_warnings", []))


def test_file_without_timestamp_column_uses_absolute_row_time(tmp_path):
    source = _open(tmp_path, row_count=300, with_timestamp=False)
    assert source.span_s == (0.0, 10.0)
    late = source.snapshot_for(0.0)
    assert late.timestamps_s[0] == 0.0
    expected = np.linspace(0.0, 10.0, 300)
    np.testing.assert_allclose(late.timestamps_s, expected)


def test_long_file_without_timestamp_column_still_windows_to_100_s(tmp_path):
    csv_path = _write_csv(tmp_path, LONG_ROWS, with_timestamp=False)
    metadata_path = _write_metadata(tmp_path, capture_duration_seconds=300.0)
    source = CsvWindowSource.open(csv_path, metadata_path, blip_filter_enabled=False)
    window = source.snapshot_for(100.0)
    assert window.channel_labels == ["CH1", "CH2"]
    assert window.view_range_s == (100.0, 100.0 + WINDOW_VIEW_S)
    assert window.timestamps_s[-1] - window.timestamps_s[0] <= MAX_RESIDENT_S


@pytest.mark.parametrize("timestamp_header", ["timestamp_s", "Timestamp"])
def test_timestamp_column_is_used_as_time_and_never_as_a_signal(tmp_path, timestamp_header):
    rows = [f"{row * SAMPLE_PERIOD_S:.3f},{2000 + row},{2100 + row}" for row in range(40)]
    csv_path = tmp_path / "capture.csv"
    lines = [f"{timestamp_header},CH1,CH2", *rows]
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    source = CsvWindowSource.open(csv_path, _write_metadata(tmp_path), blip_filter_enabled=False)
    window = source.snapshot_for(0.0)
    assert window.channel_labels == ["CH1", "CH2"]
    assert source.span_s == (0.0, 39 * SAMPLE_PERIOD_S)


def test_file_changed_on_disk_is_rejected(tmp_path):
    source = _open(tmp_path, row_count=40)
    with source.csv_path.open("a", encoding="utf-8") as handle:
        handle.write("999.000,1,2,0,0\n")
    with pytest.raises(ValueError, match="changed on disk"):
        source.snapshot_for(0.0)


def test_non_numeric_row_only_fails_its_window(tmp_path):
    csv_path = _write_csv(tmp_path, LONG_ROWS)
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    lines[-2] = "299.000,abc,2,0,0"
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    source = CsvWindowSource.open(csv_path, _write_metadata(tmp_path), blip_filter_enabled=False)
    assert source.snapshot_for(0.0).sweep_count > 0
    with pytest.raises(ValueError, match="numeric"):
        source.snapshot_for(source.span_s[1])


def test_worker_opens_and_pages_window_source(tmp_path):
    csv_path = _write_csv(tmp_path, LONG_ROWS)
    metadata_path = _write_metadata(tmp_path)
    worker = AnalysisSourceLoadWorker()
    opened = worker._process({
        "csv_path": csv_path, "metadata_path": metadata_path,
        "blip_filter_enabled": False, "blip_filter_window_samples": 3,
    })
    paged = worker._process({"window_source": opened["window_source"], "view_start_s": 150.0})
    assert opened["snapshot"].view_range_s[0] == 0.0
    assert paged["snapshot"].view_range_s[0] == 150.0
    assert paged["window_source"] is opened["window_source"]
