"""
Offline Analysis tab source loading and derived-signal helpers.

This module is deliberately GUI independent.  The Analysis tab calls these
functions to load a stable snapshot, apply optional Spectrum-compatible
filtering, and compute Pressure-map-style derived overlays without mutating the
live acquisition buffers or filter runtime.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np

from constants.plotting import IADC_RESOLUTION_BITS
from constants.pressure_map import DEFAULT_HPF_CUTOFF_HZ, DEFAULT_INTEGRATION_WINDOW_SAMPLES
from constants.shear import SHEAR_SENSOR_POSITIONS
from data_processing.adc_filter_engine import ADCFilterEngine
from data_processing.normal_force_calculator import NormalForceCalculator
from data_processing.shear_detector import ShearDetector
from data_processing.signal_integrator import SignalIntegrator


ANALYSIS_TIMESTAMP_COLUMNS = {"timestamp", "timestamp_s"}
ANALYSIS_FORCE_COLUMNS = {"force_x", "force_z", "force_x_n", "force_z_n"}


@dataclass(slots=True)
class AnalysisSourceSnapshot:
    """Immutable-ish source payload used by the Analysis tab."""

    data: np.ndarray
    timestamps_s: np.ndarray
    channel_labels: list[str]
    metadata: dict = field(default_factory=dict)
    force_timestamps_s: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.float64))
    force_x_n: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.float64))
    force_z_n: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.float64))
    source_id: str = ""
    sample_rate_hz: float = 0.0

    @property
    def sweep_count(self) -> int:
        return int(self.data.shape[0]) if self.data.ndim == 2 else 0

    @property
    def samples_per_sweep(self) -> int:
        return int(self.data.shape[1]) if self.data.ndim == 2 else 0

    def fingerprint(self) -> tuple:
        if self.sweep_count <= 0:
            return (self.source_id, 0, 0)
        return (
            self.source_id,
            self.sweep_count,
            self.samples_per_sweep,
            float(self.timestamps_s[0]) if self.timestamps_s.size else 0.0,
            float(self.timestamps_s[-1]) if self.timestamps_s.size else 0.0,
        )


@dataclass(slots=True)
class AnalysisTrace:
    label: str
    x: np.ndarray
    y: np.ndarray
    group: str = "signal"


@dataclass(slots=True)
class AnalysisPreparedData:
    traces: list[AnalysisTrace]
    force_traces: list[AnalysisTrace]
    overlay_traces: list[AnalysisTrace]
    x_label: str
    x_units: str
    status: str = ""


def reorder_circular_capture(
    data_buffer,
    timestamps_buffer,
    sweep_count: int,
    write_index: int,
    max_sweeps: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Return oldest-to-newest data from the app's circular capture buffer."""
    if data_buffer is None or timestamps_buffer is None:
        return np.empty((0, 0), dtype=np.float32), np.empty(0, dtype=np.float64)

    data = np.asarray(data_buffer, dtype=np.float32)
    timestamps = np.asarray(timestamps_buffer, dtype=np.float64)
    actual_sweeps = min(max(0, int(sweep_count)), max(0, int(max_sweeps)))
    if actual_sweeps <= 0 or data.ndim != 2:
        return np.empty((0, 0), dtype=np.float32), np.empty(0, dtype=np.float64)

    if actual_sweeps < int(max_sweeps):
        return data[:actual_sweeps].copy(), timestamps[:actual_sweeps].copy()

    write_pos = int(write_index) % int(max_sweeps)
    return (
        np.concatenate([data[write_pos:], data[:write_pos]]).astype(np.float32, copy=False),
        np.concatenate([timestamps[write_pos:], timestamps[:write_pos]]).astype(np.float64, copy=False),
    )


def build_in_memory_snapshot(owner) -> AnalysisSourceSnapshot:
    """Copy the latest retained in-memory capture from the GUI owner."""
    with owner.buffer_lock:
        data, timestamps = reorder_circular_capture(
            getattr(owner, "raw_data_buffer", None),
            getattr(owner, "sweep_timestamps_buffer", None),
            int(getattr(owner, "sweep_count", 0) or 0),
            int(getattr(owner, "buffer_write_index", 0) or 0),
            int(getattr(owner, "MAX_SWEEPS_BUFFER", 0) or 0),
        )

    if data.size == 0:
        raise ValueError("No in-memory capture is available yet.")

    channel_labels = _default_channel_labels(
        getattr(owner, "config", {}).get("channels", []),
        int(getattr(owner, "config", {}).get("repeat", 1) or 1),
        data.shape[1],
    )
    metadata = {
        "configuration": dict(getattr(owner, "config", {}) or {}),
        "source": "in_memory",
    }
    return AnalysisSourceSnapshot(
        data=data,
        timestamps_s=_normalize_timestamps(timestamps, data.shape[0]),
        channel_labels=channel_labels,
        metadata=metadata,
        force_timestamps_s=_force_times(owner),
        force_x_n=_force_values(owner, "x"),
        force_z_n=_force_values(owner, "z"),
        source_id="in_memory",
        sample_rate_hz=_owner_sample_rate_hz(owner, data, timestamps),
    )


def load_exported_csv_snapshot(csv_path, metadata_path) -> AnalysisSourceSnapshot:
    """Load an app-exported CSV plus its JSON metadata sidecar."""
    csv_path = Path(csv_path)
    metadata_path = Path(metadata_path)
    if not csv_path.exists():
        raise ValueError(f"CSV file does not exist: {csv_path}")
    if not metadata_path.exists():
        raise ValueError(f"Metadata JSON does not exist: {metadata_path}")

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Could not read metadata JSON: {exc}") from exc

    config = metadata.get("configuration")
    if not isinstance(config, dict):
        raise ValueError("Metadata JSON is missing the app export 'configuration' block.")

    rows: list[dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(line for line in handle if not line.lstrip().startswith("#"))
        if not reader.fieldnames:
            raise ValueError("CSV file is missing a header row.")
        fieldnames = [str(name) for name in reader.fieldnames]
        for row in reader:
            rows.append(row)

    if not rows:
        raise ValueError("CSV file contains no data rows.")

    data_columns = [
        name for name in fieldnames
        if _column_key(name) not in ANALYSIS_TIMESTAMP_COLUMNS | ANALYSIS_FORCE_COLUMNS
    ]
    if not data_columns:
        raise ValueError("CSV file has no signal columns after timestamp and force columns.")

    try:
        data = np.asarray(
            [[float(row.get(column, "nan")) for column in data_columns] for row in rows],
            dtype=np.float32,
        )
    except ValueError as exc:
        raise ValueError(f"CSV signal columns must be numeric: {exc}") from exc

    if np.isnan(data).any():
        raise ValueError("CSV signal columns contain missing or non-numeric values.")

    timestamps = _timestamps_from_export_rows(rows, metadata)
    force_x = _optional_numeric_column_any(rows, ("Force_X_N", "Force_X"))
    force_z = _optional_numeric_column_any(rows, ("Force_Z_N", "Force_Z"))
    channels = config.get("channels", [])
    repeat = int(config.get("repeat_count", config.get("repeat", 1)) or 1)
    expected_columns = int(config.get("buffer_total_samples", 0) or 0)
    if expected_columns <= 0:
        expected_columns = len(channels) * max(1, repeat)
    if expected_columns > 0 and expected_columns != data.shape[1]:
        raise ValueError(
            f"CSV/metadata schema mismatch: metadata expects {expected_columns} signal columns, "
            f"CSV has {data.shape[1]}."
        )

    return AnalysisSourceSnapshot(
        data=data,
        timestamps_s=timestamps,
        channel_labels=list(data_columns),
        metadata=metadata,
        force_timestamps_s=timestamps.copy() if (force_x.size or force_z.size) else np.empty(0, dtype=np.float64),
        force_x_n=force_x,
        force_z_n=force_z,
        source_id=f"csv:{csv_path.resolve()}|json:{metadata_path.resolve()}",
        sample_rate_hz=_metadata_sample_rate_hz(metadata, data, timestamps),
    )


def prepare_analysis_data(
    snapshot: AnalysisSourceSnapshot,
    *,
    axis_mode: str = "time_ms",
    visible_labels: Iterable[str] | None = None,
    filter_enabled: bool = False,
    filter_settings: dict | None = None,
    overlay_flags: Mapping[str, bool] | None = None,
    vref_voltage: float = 3.3,
    integration_window_samples: int = DEFAULT_INTEGRATION_WINDOW_SAMPLES,
    hpf_cutoff_hz: float = DEFAULT_HPF_CUTOFF_HZ,
) -> AnalysisPreparedData:
    """Build display traces for the Analysis tab."""
    if snapshot.data.size == 0:
        return AnalysisPreparedData([], [], [], "Time", "ms", "No source data loaded.")

    data = np.asarray(snapshot.data, dtype=np.float32)
    status_parts: list[str] = []
    if filter_enabled and filter_settings and bool(filter_settings.get("enabled", True)):
        try:
            data = filter_offline_data(snapshot, filter_settings)
        except Exception as exc:
            status_parts.append(f"Filter skipped: {exc}")

    visible_set = set(visible_labels or snapshot.channel_labels)
    x_base, x_label, x_units = build_trace_x_axis(snapshot, axis_mode)
    traces = [
        AnalysisTrace(label=label, x=x_base[:, column], y=data[:, column].astype(np.float64), group="signal")
        for column, label in enumerate(snapshot.channel_labels)
        if label in visible_set and column < data.shape[1]
    ]

    force_traces = build_force_traces(snapshot, axis_mode)
    overlays = build_overlay_traces(
        snapshot,
        data,
        axis_mode=axis_mode,
        overlay_flags=overlay_flags or {},
        vref_voltage=vref_voltage,
        integration_window_samples=integration_window_samples,
        hpf_cutoff_hz=hpf_cutoff_hz,
    )
    return AnalysisPreparedData(traces, force_traces, overlays, x_label, x_units, " | ".join(status_parts))


def build_trace_x_axis(snapshot: AnalysisSourceSnapshot, axis_mode: str) -> tuple[np.ndarray, str, str]:
    sweeps = snapshot.sweep_count
    samples = snapshot.samples_per_sweep
    if axis_mode == "samples":
        return (
            np.arange(sweeps * samples, dtype=np.float64).reshape(sweeps, samples),
            "Sample Index",
            "",
        )

    timestamps = _normalize_timestamps(snapshot.timestamps_s, sweeps)
    offsets = _sample_offsets_s(snapshot)
    x = (timestamps.reshape(-1, 1) + offsets.reshape(1, -1)) * 1000.0
    return x, "Time", "ms"


def build_force_traces(snapshot: AnalysisSourceSnapshot, axis_mode: str) -> list[AnalysisTrace]:
    if snapshot.force_x_n.size == 0 and snapshot.force_z_n.size == 0:
        return []
    count = max(snapshot.force_x_n.size, snapshot.force_z_n.size)
    if axis_mode == "samples":
        x = np.arange(count, dtype=np.float64)
    elif snapshot.force_timestamps_s.size >= count:
        x = snapshot.force_timestamps_s[:count].astype(np.float64) * 1000.0
    else:
        x = np.linspace(
            float(snapshot.timestamps_s[0] if snapshot.timestamps_s.size else 0.0),
            float(snapshot.timestamps_s[-1] if snapshot.timestamps_s.size else max(count - 1, 0)),
            count,
            dtype=np.float64,
        ) * 1000.0

    traces: list[AnalysisTrace] = []
    if snapshot.force_x_n.size:
        traces.append(AnalysisTrace("Force X [N]", x[: snapshot.force_x_n.size], snapshot.force_x_n, "force"))
    if snapshot.force_z_n.size:
        traces.append(AnalysisTrace("Force Z [N]", x[: snapshot.force_z_n.size], snapshot.force_z_n, "force"))
    return traces


def filter_offline_data(snapshot: AnalysisSourceSnapshot, filter_settings: dict) -> np.ndarray:
    settings = {**filter_settings, "notches": [dict(n) for n in filter_settings.get("notches", [])]}
    total_fs_hz = float(snapshot.sample_rate_hz or _metadata_sample_rate_hz(snapshot.metadata, snapshot.data, snapshot.timestamps_s))
    if total_fs_hz <= 0.0:
        raise ValueError("sample rate unavailable")

    config = snapshot.metadata.get("configuration", {}) if isinstance(snapshot.metadata, dict) else {}
    channels = list(config.get("channels", []))
    repeat = int(config.get("repeat_count", config.get("repeat", 1)) or 1)
    if not channels or len(channels) * max(1, repeat) != snapshot.samples_per_sweep:
        channels = list(range(snapshot.samples_per_sweep))
        repeat = 1

    engine = ADCFilterEngine()
    channel_rates = engine.estimate_channel_sample_rates(
        total_fs_hz,
        channels,
        repeat,
        sweep_timestamps_sec=snapshot.timestamps_s,
    )
    runtime = engine.build_runtime_plan(
        settings,
        total_fs_hz,
        channels,
        repeat,
        sweep_timestamps_sec=snapshot.timestamps_s,
        channel_fs_by_channel=channel_rates,
    )
    engine.reset_runtime_states(runtime)
    return engine.filter_block(runtime, np.asarray(snapshot.data, dtype=np.float32).copy())


def build_overlay_traces(
    snapshot: AnalysisSourceSnapshot,
    data: np.ndarray,
    *,
    axis_mode: str,
    overlay_flags: Mapping[str, bool],
    vref_voltage: float,
    integration_window_samples: int,
    hpf_cutoff_hz: float,
) -> list[AnalysisTrace]:
    if not any(bool(overlay_flags.get(key, False)) for key in ("shear", "normal", "integration")):
        return []

    position_columns = _position_column_map(snapshot.channel_labels)
    if not all(position in position_columns for position in SHEAR_SENSOR_POSITIONS):
        if snapshot.samples_per_sweep < len(SHEAR_SENSOR_POSITIONS):
            return []
        position_columns = {position: index for index, position in enumerate(SHEAR_SENSOR_POSITIONS)}

    volts_by_position = {
        position: counts_to_volts(data[:, column], vref_voltage)
        for position, column in position_columns.items()
        if column < data.shape[1]
    }
    if not all(position in volts_by_position for position in SHEAR_SENSOR_POSITIONS):
        return []

    sample_rate_hz = _overlay_sample_rate_hz(snapshot)
    integrator = SignalIntegrator(
        channel_count=len(SHEAR_SENSOR_POSITIONS),
        hpf_cutoff_hz=float(hpf_cutoff_hz),
        integration_window_samples=int(integration_window_samples),
        sample_rate_hz=sample_rate_hz if sample_rate_hz > 0 else None,
        channel_map=list(SHEAR_SENSOR_POSITIONS),
    )
    try:
        integrated = integrator.process(
            [volts_by_position[position] for position in SHEAR_SENSOR_POSITIONS],
            sample_rate_hz=sample_rate_hz if sample_rate_hz > 0 else None,
        )
    except Exception:
        integrated = _fallback_integrated(volts_by_position, int(integration_window_samples))

    x_matrix, _label, _units = build_trace_x_axis(snapshot, axis_mode)
    x = x_matrix[:, 0] if x_matrix.size else np.empty(0, dtype=np.float64)
    overlays: list[AnalysisTrace] = []
    shear_detector = ShearDetector()
    normal_calculator = NormalForceCalculator()
    shear_lr: list[float] = []
    shear_tb: list[float] = []
    normal: list[float] = []

    for row_index in range(snapshot.sweep_count):
        values = {
            position: float(np.asarray(integrated[position], dtype=np.float64)[row_index])
            for position in SHEAR_SENSOR_POSITIONS
        }
        shear = shear_detector.detect(values)
        normal_result = normal_calculator.compute(shear.residual)
        shear_lr.append(float(shear.b_lr))
        shear_tb.append(float(shear.b_tb))
        normal.append(float(normal_result.total_force))

    if overlay_flags.get("shear", False):
        overlays.append(AnalysisTrace("Shear L/R [V]", x, np.asarray(shear_lr, dtype=np.float64), "overlay"))
        overlays.append(AnalysisTrace("Shear T/B [V]", x, np.asarray(shear_tb, dtype=np.float64), "overlay"))
    if overlay_flags.get("normal", False):
        overlays.append(AnalysisTrace("Normal Pressure [V]", x, np.asarray(normal, dtype=np.float64), "overlay"))
    if overlay_flags.get("integration", False):
        for position in SHEAR_SENSOR_POSITIONS:
            overlays.append(
                AnalysisTrace(
                    f"Integrated {position} [V samples]",
                    x,
                    np.asarray(integrated[position], dtype=np.float64),
                    "overlay",
                )
            )
    return overlays


def counts_to_volts(values, vref_voltage: float) -> np.ndarray:
    max_adc_value = float((2 ** IADC_RESOLUTION_BITS) - 1)
    return (np.asarray(values, dtype=np.float64) / max_adc_value) * float(vref_voltage)


def _position_column_map(labels: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, label in enumerate(labels):
        normalized = str(label).strip().upper().replace(" ", "_")
        for position in SHEAR_SENSOR_POSITIONS:
            if normalized == position or normalized.endswith(f"_{position}") or normalized.endswith(f"-{position}"):
                result[position] = index
    return result


def _fallback_integrated(values_by_position: Mapping[str, np.ndarray], window_samples: int) -> dict[str, np.ndarray]:
    window = max(1, int(window_samples))
    result: dict[str, np.ndarray] = {}
    for position, values in values_by_position.items():
        samples = np.asarray(values, dtype=np.float64)
        cumulative = np.cumsum(samples, dtype=np.float64)
        integrated = cumulative.copy()
        if samples.size > window:
            integrated[window:] = cumulative[window:] - cumulative[:-window]
        result[position] = integrated
    return result


def _default_channel_labels(channels: list, repeat: int, column_count: int) -> list[str]:
    labels: list[str] = []
    repeat = max(1, int(repeat or 1))
    for channel in channels:
        for rep in range(repeat):
            labels.append(f"CH{channel}" if repeat == 1 else f"CH{channel}.{rep + 1}")
    if len(labels) != column_count:
        labels = [f"Col{index}" for index in range(column_count)]
    return labels


def _normalize_timestamps(timestamps, count: int) -> np.ndarray:
    ts = np.asarray(timestamps, dtype=np.float64).reshape(-1)
    if ts.size >= count:
        return ts[:count].copy()
    if ts.size > 1:
        step = float(np.median(np.diff(ts)))
    else:
        step = 1.0
    start = float(ts[0]) if ts.size else 0.0
    return start + np.arange(count, dtype=np.float64) * step


def _sample_offsets_s(snapshot: AnalysisSourceSnapshot) -> np.ndarray:
    samples = max(1, snapshot.samples_per_sweep)
    fs = float(snapshot.sample_rate_hz or _metadata_sample_rate_hz(snapshot.metadata, snapshot.data, snapshot.timestamps_s))
    if fs > 0.0:
        return np.arange(samples, dtype=np.float64) / fs
    return np.zeros(samples, dtype=np.float64)


def _timestamps_from_export_rows(rows: list[dict[str, str]], metadata: dict) -> np.ndarray:
    if rows and "Timestamp_s" in rows[0]:
        values = _optional_numeric_column(rows, "Timestamp_s")
        if values.size == len(rows):
            return values

    duration = metadata.get("capture_duration_seconds")
    if isinstance(duration, (int, float)) and len(rows) > 1:
        return np.linspace(0.0, float(duration), len(rows), dtype=np.float64)
    return np.arange(len(rows), dtype=np.float64)


def _optional_numeric_column(rows: list[dict[str, str]], column_name: str) -> np.ndarray:
    values: list[float] = []
    for row in rows:
        raw = row.get(column_name)
        if raw in (None, ""):
            continue
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return np.asarray(values, dtype=np.float64)


def _optional_numeric_column_any(rows: list[dict[str, str]], column_names: tuple[str, ...]) -> np.ndarray:
    if not rows:
        return np.empty(0, dtype=np.float64)
    available = set(rows[0].keys())
    for column_name in column_names:
        if column_name in available:
            return _optional_numeric_column(rows, column_name)
    return np.empty(0, dtype=np.float64)


def _metadata_sample_rate_hz(metadata: dict, data: np.ndarray, timestamps: np.ndarray) -> float:
    timing = metadata.get("timing", {}) if isinstance(metadata, dict) else {}
    for key in ("arduino_sample_rate_hz", "total_rate_hz"):
        value = timing.get(key)
        if isinstance(value, (int, float)) and float(value) > 0:
            return float(value)
    if timestamps.size > 1:
        diffs = np.diff(timestamps)
        diffs = diffs[diffs > 0]
        if diffs.size:
            return float(data.shape[1]) / float(np.median(diffs))
    return 0.0


def _owner_sample_rate_hz(owner, data: np.ndarray, timestamps: np.ndarray) -> float:
    if hasattr(owner, "_get_filter_total_sample_rate_hz"):
        try:
            rate = float(owner._get_filter_total_sample_rate_hz())
            if rate > 0.0:
                return rate
        except Exception:
            pass
    return _metadata_sample_rate_hz({}, data, timestamps)


def _overlay_sample_rate_hz(snapshot: AnalysisSourceSnapshot) -> float:
    if snapshot.timestamps_s.size > 1:
        diffs = np.diff(snapshot.timestamps_s)
        diffs = diffs[diffs > 0]
        if diffs.size:
            return float(1.0 / np.median(diffs))
    return 0.0


def _force_times(owner) -> np.ndarray:
    state = getattr(owner, "force_state", None)
    samples = list(getattr(state, "data", []) if state is not None else [])
    times = [float(sample[0]) for sample in samples if len(sample) >= 3]
    return np.asarray(times, dtype=np.float64)


def _force_values(owner, axis: str) -> np.ndarray:
    state = getattr(owner, "force_state", None)
    samples = list(getattr(state, "data", []) if state is not None else [])
    offset = 1 if axis == "x" else 2
    values = [float(sample[offset]) for sample in samples if len(sample) > offset]
    return np.asarray(values, dtype=np.float64)


def _column_key(name: str) -> str:
    return str(name).strip().lower()
