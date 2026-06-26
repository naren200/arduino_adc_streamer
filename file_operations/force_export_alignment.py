"""
Force Export Alignment Helpers
==============================
Helpers for aligning captured force samples to exported ADC sweep rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from constants.force import X_FORCE_SENSOR_TO_NEWTON, Z_FORCE_SENSOR_TO_NEWTON


@dataclass(frozen=True, slots=True)
class ForceExportSeries:
    """Sorted force samples ready for nearest-timestamp lookup."""

    timestamps_s: np.ndarray
    x_force: np.ndarray
    z_force: np.ndarray


def build_force_export_series(force_samples) -> ForceExportSeries | None:
    """Return sorted, Newton-converted force samples for export alignment."""
    if not force_samples:
        return None

    force_array = np.asarray(force_samples, dtype=np.float64)
    if force_array.ndim != 2 or force_array.shape[1] < 3 or len(force_array) == 0:
        return None

    sort_order = np.argsort(force_array[:, 0], kind="stable")
    force_array = force_array[sort_order]
    return ForceExportSeries(
        timestamps_s=force_array[:, 0],
        x_force=force_array[:, 1] / X_FORCE_SENSOR_TO_NEWTON,
        z_force=force_array[:, 2] / Z_FORCE_SENSOR_TO_NEWTON,
    )


def build_export_row_timestamps(
    *,
    selected_timestamps,
    saved_total: int,
    capture_duration_s: float | None,
):
    """Return row timestamps for export using measured sweep times when available."""
    if selected_timestamps is not None and len(selected_timestamps) >= saved_total:
        return np.asarray(selected_timestamps[:saved_total], dtype=np.float64)

    if capture_duration_s is None or saved_total <= 1:
        return None

    return np.linspace(0.0, float(capture_duration_s), num=saved_total, dtype=np.float64)


def resolve_export_start_datetime(
    *,
    capture_start_time_s: float | None = None,
    archive_start_time_iso: str | None = None,
) -> datetime | None:
    """Resolve the absolute wall-clock start time for a CSV export."""
    if archive_start_time_iso:
        try:
            return datetime.fromisoformat(archive_start_time_iso.replace("Z", "+00:00"))
        except ValueError:
            pass

    if capture_start_time_s is None:
        return None

    try:
        return datetime.fromtimestamp(float(capture_start_time_s))
    except (TypeError, ValueError, OSError):
        return None


def format_export_clock_time(
    export_start_datetime: datetime | None,
    row_offset_s: float | None,
) -> str:
    """Format one export row's wall-clock time as ``HH:MM:SS.ffffff``."""
    if export_start_datetime is None:
        return ""

    offset_s = 0.0 if row_offset_s is None else float(row_offset_s)
    row_datetime = export_start_datetime + timedelta(seconds=offset_s)
    return row_datetime.strftime("%H:%M:%S.%f")


def get_nearest_force_values(
    force_series: ForceExportSeries | None,
    sweep_time_s: float | None,
) -> tuple[float, float]:
    """Return the nearest calibrated force sample in Newtons for one export row."""
    if force_series is None or sweep_time_s is None or len(force_series.timestamps_s) == 0:
        return (0.0, 0.0)

    timestamps = force_series.timestamps_s
    insert_at = int(np.searchsorted(timestamps, float(sweep_time_s), side="left"))

    if insert_at <= 0:
        closest_index = 0
    elif insert_at >= len(timestamps):
        closest_index = len(timestamps) - 1
    else:
        prev_index = insert_at - 1
        next_index = insert_at
        prev_diff = abs(float(sweep_time_s) - float(timestamps[prev_index]))
        next_diff = abs(float(timestamps[next_index]) - float(sweep_time_s))
        closest_index = prev_index if prev_diff <= (next_diff + 1e-12) else next_index

    return (
        float(force_series.x_force[closest_index]),
        float(force_series.z_force[closest_index]),
    )
