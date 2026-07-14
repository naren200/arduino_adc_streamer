"""Helpers for the JSON sidecars written by capture and Analysis exports."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np


NO_DATA = "No Data"
_CALIBRATION_PRECISION = {
    "vmid_v": 3,
    "noise_threshold_v": 5,
    "sigma_v": 5,
}


def json_safe_copy(value):
    """Return a deep-copied value that can be written by :mod:`json`."""
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): json_safe_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe_copy(item) for item in value]
    return deepcopy(value)


def format_pzt_channel_calibration(calibration: Mapping[str, object] | None) -> dict:
    """Return JSON-safe PZT calibration results with export-friendly precision."""
    if not isinstance(calibration, Mapping):
        return {}

    formatted = json_safe_copy(calibration)
    for values in formatted.values():
        if not isinstance(values, dict):
            continue
        for field, decimals in _CALIBRATION_PRECISION.items():
            try:
                if field in values:
                    values[field] = round(float(values[field]), decimals)
            except (TypeError, ValueError):
                continue
    return formatted


def build_analysis_export_metadata(
    source_metadata: Mapping[str, object] | None,
    analysis_state: Mapping[str, object] | None,
    *,
    source_id: str,
    csv_path: Path | str,
    x_axis_label: str,
    x_axis_units: str,
    exported_traces: Sequence[str],
) -> dict:
    """Preserve source metadata and append the settings/results of an Analysis export."""
    metadata = json_safe_copy(source_metadata or {})
    settings = json_safe_copy(analysis_state or {})
    pzt_force = settings.get("pzt_force", {}) if isinstance(settings, Mapping) else {}
    if isinstance(pzt_force, dict):
        pzt_force["channel_calibration"] = format_pzt_channel_calibration(
            pzt_force.get("channel_calibration", {})
        )
    metadata["analysis_export"] = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_id": source_id,
        "settings": settings,
        "csv": {
            "path": str(csv_path),
            "x_axis_label": x_axis_label,
            "x_axis_units": x_axis_units,
            "exported_traces": list(exported_traces),
        },
    }
    return metadata


def build_vmid_noise_metadata(
    channel_labels: Sequence[str],
    analysis_state: Mapping[str, object] | None,
    *,
    measured_from_in_memory_capture: bool,
) -> dict[str, dict[str, object]]:
    """Build a complete per-channel Vmid/noise summary for capture metadata."""
    calibration = {}
    if measured_from_in_memory_capture and isinstance(analysis_state, Mapping):
        pzt_force = analysis_state.get("pzt_force", {})
        if isinstance(pzt_force, Mapping):
            candidate = pzt_force.get("channel_calibration", {})
            if isinstance(candidate, Mapping):
                calibration = candidate

    result = {}
    for label in channel_labels:
        measured = calibration.get(label)
        if isinstance(measured, Mapping) and {
            "vmid_v",
            "noise_threshold_v",
        }.issubset(measured):
            formatted = format_pzt_channel_calibration({str(label): measured})[str(label)]
            result[str(label)] = {
                "vmid_v": formatted["vmid_v"],
                "noise_threshold_v": formatted["noise_threshold_v"],
            }
        else:
            result[str(label)] = {
                "vmid_v": NO_DATA,
                "noise_threshold_v": NO_DATA,
            }
    return result
