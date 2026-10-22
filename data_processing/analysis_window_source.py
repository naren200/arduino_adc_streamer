"""
Windowed Analysis sources.

The Analysis tab never holds a whole capture: it pages a bounded window of a
larger source.  A window is a ``WINDOW_VIEW_S`` view plus ``WINDOW_BUFFER_S`` on
each side (at most 100 s), delivered as an ordinary ``AnalysisSourceSnapshot``
so gate replay, derived channels and TouchID keep consuming the same type.

``CsvWindowSource`` indexes an exported CSV once (byte offset and timestamp per
data row) and afterwards reads only the rows of the requested window.
``ArraySnapshotWindowSource`` slices an already-resident snapshot (in-memory and
archive sources).  Snapshot timestamps stay in absolute source time.
"""

from __future__ import annotations

import copy
import csv
import io
import json
from array import array
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import numpy as np

from constants.pzt_blip_filter import (
    PZT_BLIP_FILTER_DEFAULT_ENABLED,
    PZT_BLIP_FILTER_DEFAULT_WINDOW_SAMPLES,
)
from data_processing.analysis_workbench import (
    AnalysisSourceSnapshot,
    _load_filtered_snapshot,
    _metadata_sample_rate_hz,
    csv_signal_columns,
    note_csv_schema_mismatch,
    snapshot_from_csv_rows,
)

WINDOW_VIEW_S = 60.0
WINDOW_BUFFER_S = 20.0
WINDOW_STEP_S = 40.0

_TIMESTAMP_COLUMN = "Timestamp_s"
_COMMENT_PREFIX = "#"

TimeRange = tuple[float, float]


class AnalysisWindowSource(Protocol):
    """A source that hands out bounded windows of itself."""

    source_id: str
    span_s: TimeRange

    def snapshot_for(self, view_start_s: float) -> AnalysisSourceSnapshot: ...

    def clamp_view_start(self, view_start_s: float) -> float: ...


@dataclass(frozen=True, slots=True)
class WindowRanges:
    view: TimeRange
    resident: TimeRange


def clamp_view_start(span_s: TimeRange, view_start_s: float) -> float:
    """Keep ``[start, start + WINDOW_VIEW_S]`` inside the span; a source that fits shows the whole span."""
    first_s, last_s = span_s
    latest_start_s = last_s - WINDOW_VIEW_S
    if latest_start_s <= first_s:
        return float(first_s)
    return float(min(max(view_start_s, first_s), latest_start_s))


def window_ranges(span_s: TimeRange, view_start_s: float) -> WindowRanges:
    first_s, last_s = span_s
    start_s = clamp_view_start(span_s, view_start_s)
    view = (start_s, min(start_s + WINDOW_VIEW_S, last_s))
    resident = (max(first_s, view[0] - WINDOW_BUFFER_S), min(last_s, view[1] + WINDOW_BUFFER_S))
    return WindowRanges(view=view, resident=resident)


def _row_slice(timestamps_s: np.ndarray, time_range_s: TimeRange) -> slice:
    first = int(np.searchsorted(timestamps_s, time_range_s[0], side="left"))
    last = int(np.searchsorted(timestamps_s, time_range_s[1], side="right"))
    return slice(first, last)


@dataclass(frozen=True, slots=True)
class _CsvFileStamp:
    size_bytes: int
    modified_ns: int

    @classmethod
    def of(cls, path: Path) -> "_CsvFileStamp":
        stat = path.stat()
        return cls(size_bytes=stat.st_size, modified_ns=stat.st_mtime_ns)


@dataclass(frozen=True, slots=True)
class _CsvIndex:
    fieldnames: list[str]
    row_offsets: np.ndarray  # byte offset of every data row, plus the end of the last one
    raw_timestamps_s: np.ndarray | None  # None unless every row carries a numeric Timestamp_s

    @property
    def row_count(self) -> int:
        return int(self.row_offsets.size) - 1


def _is_skippable_line(line: bytes) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith(_COMMENT_PREFIX.encode())


def _parse_timestamp_cell(line: bytes, column: int) -> float | None:
    cells = line.split(b",")
    try:
        return float(cells[column])
    except (IndexError, ValueError):
        return None


def _scan_csv(csv_path: Path) -> _CsvIndex:
    """One streaming pass: header, data-row byte offsets and the Timestamp_s column."""
    fieldnames: list[str] | None = None
    timestamp_column: int | None = None
    row_starts = array("q")
    timestamps = array("d")
    is_timestamp_complete = True
    offset = 0
    with csv_path.open("rb") as handle:
        for line in handle:
            line_start, offset = offset, offset + len(line)
            if _is_skippable_line(line):
                continue
            if fieldnames is None:
                fieldnames = [str(name) for name in next(csv.reader([line.decode("utf-8")]))]
                timestamp_column = fieldnames.index(_TIMESTAMP_COLUMN) if _TIMESTAMP_COLUMN in fieldnames else None
                continue
            row_starts.append(line_start)
            row_end = offset
            if timestamp_column is not None:
                value = _parse_timestamp_cell(line, timestamp_column)
                if value is None:
                    is_timestamp_complete = False
                else:
                    timestamps.append(value)
    if fieldnames is None:
        raise ValueError("CSV file is missing a header row.")
    if not row_starts:
        raise ValueError("CSV file contains no data rows.")
    row_starts.append(row_end)
    has_timestamps = timestamp_column is not None and is_timestamp_complete
    return _CsvIndex(
        fieldnames=fieldnames,
        row_offsets=np.frombuffer(row_starts, dtype=np.int64),
        raw_timestamps_s=np.frombuffer(timestamps, dtype=np.float64) if has_timestamps else None,
    )


def _export_timestamps(index: _CsvIndex, metadata: dict) -> np.ndarray:
    """Absolute per-row timestamps; the fallbacks span the whole file, never a window."""
    if index.raw_timestamps_s is not None:
        return index.raw_timestamps_s
    duration = metadata.get("capture_duration_seconds")
    if isinstance(duration, (int, float)) and index.row_count > 1:
        return np.linspace(0.0, float(duration), index.row_count, dtype=np.float64)
    return np.arange(index.row_count, dtype=np.float64)


def _read_export_metadata(csv_path: Path, metadata_path: Path) -> dict:
    if not csv_path.exists():
        raise ValueError(f"CSV file does not exist: {csv_path}")
    if not metadata_path.exists():
        raise ValueError(f"Metadata JSON does not exist: {metadata_path}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Could not read metadata JSON: {exc}") from exc
    if not isinstance(metadata.get("configuration"), dict):
        raise ValueError("Metadata JSON is missing the app export 'configuration' block.")
    return metadata


@dataclass(frozen=True, slots=True)
class CsvWindowSource:
    """Windows of an app-exported CSV, read on demand from a one-time row index."""

    source_id: str
    csv_path: Path
    file_stamp: _CsvFileStamp
    index: _CsvIndex
    data_columns: list[str]
    metadata: dict
    timestamps_s: np.ndarray
    sample_rate_hz: float
    blip_filter_enabled: bool
    blip_filter_window_samples: int

    @classmethod
    def open(
        cls,
        csv_path,
        metadata_path,
        *,
        blip_filter_enabled: bool = PZT_BLIP_FILTER_DEFAULT_ENABLED,
        blip_filter_window_samples: int = PZT_BLIP_FILTER_DEFAULT_WINDOW_SAMPLES,
    ) -> "CsvWindowSource":
        csv_path, metadata_path = Path(csv_path), Path(metadata_path)
        metadata = _read_export_metadata(csv_path, metadata_path)
        file_stamp = _CsvFileStamp.of(csv_path)
        index = _scan_csv(csv_path)
        data_columns = csv_signal_columns(index.fieldnames)
        timestamps_s = _export_timestamps(index, metadata)
        note_csv_schema_mismatch(metadata, len(data_columns))
        signal_shape = np.empty((0, len(data_columns)), dtype=np.float32)
        return cls(
            source_id=f"csv:{csv_path.resolve()}|json:{metadata_path.resolve()}",
            csv_path=csv_path,
            file_stamp=file_stamp,
            index=index,
            data_columns=data_columns,
            metadata=metadata,
            timestamps_s=timestamps_s,
            sample_rate_hz=_metadata_sample_rate_hz(metadata, signal_shape, timestamps_s),
            blip_filter_enabled=blip_filter_enabled,
            blip_filter_window_samples=blip_filter_window_samples,
        )

    @property
    def span_s(self) -> TimeRange:
        return (float(self.timestamps_s[0]), float(self.timestamps_s[-1]))

    def clamp_view_start(self, view_start_s: float) -> float:
        return clamp_view_start(self.span_s, view_start_s)

    def snapshot_for(self, view_start_s: float) -> AnalysisSourceSnapshot:
        return self._snapshot_for_ranges(window_ranges(self.span_s, view_start_s))

    def full_snapshot(self) -> AnalysisSourceSnapshot:
        return self._snapshot_for_ranges(WindowRanges(view=self.span_s, resident=self.span_s))

    def _snapshot_for_ranges(self, ranges: WindowRanges) -> AnalysisSourceSnapshot:
        self._require_unchanged_file()
        rows_slice = _row_slice(self.timestamps_s, ranges.resident)
        snapshot = snapshot_from_csv_rows(
            self._read_rows(rows_slice),
            data_columns=self.data_columns,
            timestamps=self.timestamps_s[rows_slice].copy(),
            metadata=copy.deepcopy(self.metadata),
            source_id=self.source_id,
            sample_rate_hz=self.sample_rate_hz,
        )
        snapshot.span_s = self.span_s
        snapshot.view_range_s = ranges.view
        snapshot.resident_range_s = ranges.resident
        return _load_filtered_snapshot(
            snapshot,
            enabled=self.blip_filter_enabled,
            window=self.blip_filter_window_samples,
        )

    def _require_unchanged_file(self) -> None:
        try:
            current = _CsvFileStamp.of(self.csv_path)
        except OSError as exc:
            raise ValueError(f"CSV file is no longer readable: {self.csv_path} ({exc})") from exc
        if current != self.file_stamp:
            raise ValueError(f"CSV file changed on disk since it was opened; reload the source: {self.csv_path}")

    def _read_rows(self, rows_slice: slice) -> list[dict[str, str]]:
        offsets = self.index.row_offsets
        first_byte, end_byte = int(offsets[rows_slice.start]), int(offsets[rows_slice.stop])
        with self.csv_path.open("rb") as handle:
            handle.seek(first_byte)
            text = handle.read(end_byte - first_byte).decode("utf-8")
        lines = (line for line in io.StringIO(text, newline="") if not line.lstrip().startswith(_COMMENT_PREFIX))
        return list(csv.DictReader(lines, fieldnames=self.index.fieldnames))


class ArraySnapshotWindowSource:
    """Windows of an already-resident snapshot (in-memory or archive); no re-filtering."""

    def __init__(self, snapshot: AnalysisSourceSnapshot):
        if snapshot.timestamps_s.size == 0:
            raise ValueError("Cannot window a source without samples.")
        self._snapshot = snapshot
        self.source_id = snapshot.source_id
        self.span_s: TimeRange = (float(snapshot.timestamps_s[0]), float(snapshot.timestamps_s[-1]))

    def clamp_view_start(self, view_start_s: float) -> float:
        return clamp_view_start(self.span_s, view_start_s)

    def snapshot_for(self, view_start_s: float) -> AnalysisSourceSnapshot:
        ranges = window_ranges(self.span_s, view_start_s)
        source = self._snapshot
        rows_slice = _row_slice(source.timestamps_s, ranges.resident)
        force_times_s = self._force_times_s()
        force_slice = _row_slice(force_times_s, ranges.resident)
        return replace(
            source,
            data=source.data[rows_slice],
            timestamps_s=source.timestamps_s[rows_slice],
            force_timestamps_s=force_times_s[force_slice],
            force_x_n=source.force_x_n[force_slice],
            force_z_n=source.force_z_n[force_slice],
            span_s=self.span_s,
            view_range_s=ranges.view,
            resident_range_s=ranges.resident,
        )

    def _force_times_s(self) -> np.ndarray:
        """Force sample times; when the source has none they are spread over the span, as the force traces do."""
        source = self._snapshot
        count = max(source.force_x_n.size, source.force_z_n.size)
        if source.force_timestamps_s.size >= count:
            return source.force_timestamps_s[:count]
        return np.linspace(self.span_s[0], self.span_s[1], count, dtype=np.float64)
