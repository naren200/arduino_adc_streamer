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
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np

from constants.force import X_FORCE_SENSOR_TO_NEWTON, Z_FORCE_SENSOR_TO_NEWTON
from constants.plotting import IADC_RESOLUTION_BITS
from constants.pressure_map import DEFAULT_HPF_CUTOFF_HZ, DEFAULT_INTEGRATION_WINDOW_SAMPLES
from constants.pzt_blip_filter import (
    PZT_BLIP_FILTER_DEFAULT_ENABLED,
    PZT_BLIP_FILTER_DEFAULT_WINDOW_SAMPLES,
    normalize_pzt_blip_filter_window,
)
from constants.shear import SHEAR_SENSOR_POSITIONS
from data_processing.adc_filter_engine import ADCFilterEngine
from core.piezo_engine.baseline import preprocess_capture_start, total_warmup_sample_count
from core.piezo_engine.config import EngineConfig, ForceSettings, TimingMode, TimingPolicy
from core.piezo_engine.force_stage import ForceStage
from core.piezo_engine.median import CausalMedianN
from core.piezo_engine.overlay import JerkOverlayStage
from core.piezo_engine.pipeline import INPUT_CONDITIONING_METADATA_KEY, conditioning_record
from core.piezo_engine.timing import (
    TimingEdgeInputs,
    normalize_timing_mode,
    read_block_timing_connected_time_s,
    resolve_timing_policy,
)
from data_processing.signal_integrator import SignalIntegrator


ANALYSIS_TIMESTAMP_COLUMNS = {"timestamp", "timestamp_s"}
ANALYSIS_FORCE_COLUMNS = {"force_x", "force_z", "force_x_n", "force_z_n"}


@dataclass(slots=True)
class AnalysisSourceSnapshot:
    """Immutable-ish source payload used by the Analysis tab."""

    data: np.ndarray
    timestamps_s: np.ndarray
    channel_labels: list[str]
    channel_indices: list[int] | None = None
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


def _load_filtered_snapshot(
    snapshot: AnalysisSourceSnapshot,
    *,
    enabled: bool = PZT_BLIP_FILTER_DEFAULT_ENABLED,
    window: int = PZT_BLIP_FILTER_DEFAULT_WINDOW_SAMPLES,
) -> AnalysisSourceSnapshot:
    """Blip-filter PZT voltage columns once, here, at the I/O edge where every
    snapshot is built (live capture, archive, CSV re-load) rather than in
    every downstream consumer. A filter failure leaves the snapshot's raw
    data intact with a warning, instead of failing the whole load.
    """
    snapshot.metadata[INPUT_CONDITIONING_METADATA_KEY] = conditioning_record(None, 0)
    if not enabled:
        return snapshot
    try:
        normalized_window = normalize_pzt_blip_filter_window(window)
        snapshot.data = _apply_pzt_blip_filter(snapshot, snapshot.data, normalized_window)
        snapshot.metadata[INPUT_CONDITIONING_METADATA_KEY] = conditioning_record(normalized_window, 0)
    except Exception as exc:
        warnings = snapshot.metadata.setdefault("analysis_warnings", [])
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
            snapshot.metadata["analysis_warnings"] = warnings
        warnings.append(f"PZT blip filter skipped: {exc}")
    return snapshot


def build_in_memory_snapshot(owner) -> AnalysisSourceSnapshot:
    """Copy the latest retained in-memory capture from the GUI owner."""
    owner_config = getattr(owner, "config", {}) or {}
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

    channel_labels, channel_indices = _build_in_memory_channel_labels(
        owner,
        data.shape[1],
        fallback_channels=_config_get(owner_config, "channels", []),
        fallback_repeat=int(_config_get(owner_config, "repeat", 1) or 1),
    )
    metadata = {
        "configuration": _config_to_dict(owner_config),
        "source": "in_memory",
        "timing": _owner_analysis_timing_metadata(owner),
    }
    snapshot = AnalysisSourceSnapshot(
        data=data,
        timestamps_s=_normalize_timestamps(timestamps, data.shape[0]),
        channel_labels=channel_labels,
        channel_indices=channel_indices,
        metadata=metadata,
        force_timestamps_s=_force_times(owner),
        force_x_n=_force_values(owner, "x"),
        force_z_n=_force_values(owner, "z"),
        source_id="in_memory",
        sample_rate_hz=_owner_sample_rate_hz(owner, data, timestamps),
    )
    return _load_filtered_snapshot(
        snapshot,
        enabled=bool(getattr(owner, "pzt_blip_filter_enabled", PZT_BLIP_FILTER_DEFAULT_ENABLED)),
        window=getattr(owner, "pzt_blip_filter_window_samples", PZT_BLIP_FILTER_DEFAULT_WINDOW_SAMPLES),
    )


def build_snapshot_from_archive(owner) -> AnalysisSourceSnapshot:
    """Build an analysis snapshot from the persisted archive when the ring buffer has overflowed.

    Falls back to the in-memory ring buffer if the archive is unavailable or empty.
    Caller must ensure capture is stopped before calling.
    """
    if hasattr(owner, '_finalize_archive_if_active'):
        owner._finalize_archive_if_active()

    sweeps = timestamps = None
    if hasattr(owner, 'load_archive_data'):
        sweeps, timestamps = owner.load_archive_data()

    if not sweeps or not timestamps:
        return build_in_memory_snapshot(owner)

    owner_config = getattr(owner, "config", {}) or {}
    data = np.asarray(sweeps, dtype=np.float32)
    ts = np.asarray(timestamps, dtype=np.float64)

    channel_labels, channel_indices = _build_in_memory_channel_labels(
        owner,
        data.shape[1],
        fallback_channels=_config_get(owner_config, "channels", []),
        fallback_repeat=int(_config_get(owner_config, "repeat", 1) or 1),
    )
    metadata = {
        "configuration": _config_to_dict(owner_config),
        "source": "archive",
        "timing": _owner_analysis_timing_metadata(owner),
    }
    snapshot = AnalysisSourceSnapshot(
        data=data,
        timestamps_s=_normalize_timestamps(ts, data.shape[0]),
        channel_labels=channel_labels,
        channel_indices=channel_indices,
        metadata=metadata,
        force_timestamps_s=_force_times(owner),
        force_x_n=_force_values(owner, "x"),
        force_z_n=_force_values(owner, "z"),
        source_id="archive",
        sample_rate_hz=_owner_sample_rate_hz(owner, data, ts),
    )
    return _load_filtered_snapshot(
        snapshot,
        enabled=bool(getattr(owner, "pzt_blip_filter_enabled", PZT_BLIP_FILTER_DEFAULT_ENABLED)),
        window=getattr(owner, "pzt_blip_filter_window_samples", PZT_BLIP_FILTER_DEFAULT_WINDOW_SAMPLES),
    )


def load_exported_csv_snapshot(
    csv_path,
    metadata_path,
    *,
    blip_filter_enabled: bool = PZT_BLIP_FILTER_DEFAULT_ENABLED,
    blip_filter_window_samples: int = PZT_BLIP_FILTER_DEFAULT_WINDOW_SAMPLES,
) -> AnalysisSourceSnapshot:
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

    raw_data_columns = [
        name for name in fieldnames
        if _column_key(name) not in ANALYSIS_TIMESTAMP_COLUMNS | ANALYSIS_FORCE_COLUMNS
    ]
    data_columns = _analysis_signal_columns_from_csv(raw_data_columns)
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
    force_x = _force_column_newtons(rows, axis="x")
    force_z = _force_column_newtons(rows, axis="z")
    channels = config.get("channels", [])
    repeat = int(config.get("repeat_count", config.get("repeat", 1)) or 1)
    expected_columns = int(config.get("buffer_total_samples", 0) or 0)
    if expected_columns <= 0:
        expected_columns = len(channels) * max(1, repeat)
    if expected_columns > 0 and expected_columns != data.shape[1]:
        warnings = metadata.setdefault("analysis_warnings", [])
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
            metadata["analysis_warnings"] = warnings
        warnings.append(
            f"CSV/metadata schema mismatch: metadata expects {expected_columns} signal columns, "
            f"CSV has {data.shape[1]}."
        )

    snapshot = AnalysisSourceSnapshot(
        data=data,
        timestamps_s=timestamps,
        channel_labels=list(data_columns),
        channel_indices=list(range(len(data_columns))),
        metadata=metadata,
        force_timestamps_s=timestamps.copy() if (force_x.size or force_z.size) else np.empty(0, dtype=np.float64),
        force_x_n=force_x,
        force_z_n=force_z,
        source_id=f"csv:{csv_path.resolve()}|json:{metadata_path.resolve()}",
        sample_rate_hz=_metadata_sample_rate_hz(metadata, data, timestamps),
    )
    applied_median_window: list[int] = []

    def _blip_filter(values: np.ndarray) -> np.ndarray:
        # Pass-through when disabled -- preprocess_capture_start must still
        # be called (not skipped) so the settle trim keeps happening.
        if not blip_filter_enabled:
            return values
        try:
            window = normalize_pzt_blip_filter_window(blip_filter_window_samples)
            filtered = _apply_pzt_blip_filter(snapshot, values, window)
            applied_median_window[:] = [window]
            return filtered
        except Exception as exc:
            warnings = snapshot.metadata.setdefault("analysis_warnings", [])
            if not isinstance(warnings, list):
                warnings = [str(warnings)]
                snapshot.metadata["analysis_warnings"] = warnings
            warnings.append(f"PZT blip filter skipped: {exc}")
            return values

    # data_mod.preprocess_capture_start is the sole authority for the
    # blip-filter-then-settle-trim ORDER and the trim count -- shared with
    # drag_detection_utils_v1.load_calibration_csv and
    # TouchIdStreamProcessor.push_chunk, so this can't drift out of sequence
    # again the way it already did once (see its docstring for what broke).
    snapshot.data, settle_count = preprocess_capture_start(
        snapshot.data, snapshot.sample_rate_hz, blip_filter=_blip_filter,
    )
    snapshot.metadata[INPUT_CONDITIONING_METADATA_KEY] = conditioning_record(
        applied_median_window[0] if applied_median_window else None, settle_count,
    )
    if settle_count > 0:
        snapshot.timestamps_s = snapshot.timestamps_s[settle_count:]
        if snapshot.force_x_n.size:
            snapshot.force_x_n = snapshot.force_x_n[settle_count:]
        if snapshot.force_z_n.size:
            snapshot.force_z_n = snapshot.force_z_n[settle_count:]
        if snapshot.force_timestamps_s.size:
            snapshot.force_timestamps_s = snapshot.force_timestamps_s[settle_count:]
    return snapshot


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
    pzt_force_settings: Mapping[str, object] | None = None,
) -> AnalysisPreparedData:
    """Build display traces for the Analysis tab."""
    if snapshot.data.size == 0:
        return AnalysisPreparedData([], [], [], "Time", "ms", "No source data loaded.")

    data = np.asarray(snapshot.data, dtype=np.float32)
    status_parts: list[str] = []
    warnings = snapshot.metadata.get("analysis_warnings", [])
    if isinstance(warnings, list):
        status_parts.extend(str(warning) for warning in warnings if warning)
    elif warnings:
        status_parts.append(str(warnings))
    if filter_enabled and filter_settings and bool(filter_settings.get("enabled", True)):
        try:
            data = filter_offline_data(snapshot, filter_settings)
        except Exception as exc:
            status_parts.append(f"Filter skipped: {exc}")

    visible_set = set(snapshot.channel_labels if visible_labels is None else visible_labels)
    x_base, x_label, x_units = build_trace_x_axis(snapshot, axis_mode)
    time_base_s = build_trace_time_axis_seconds(snapshot)
    traces = []
    voltage_by_label: dict[str, np.ndarray] = {}
    for label, column in iter_analysis_signal_columns(snapshot):
        if label not in visible_set or column >= data.shape[1]:
            continue
        y_values = _signal_display_values(label, data[:, column], vref_voltage)
        voltage_by_label[label] = y_values
        traces.append(AnalysisTrace(label=label, x=x_base[:, column], y=y_values, group="signal"))

    force_traces = build_force_traces(snapshot, axis_mode)
    pzt_timing, timing_status = resolve_analysis_force_timing(snapshot, pzt_force_settings or {})
    if timing_status:
        status_parts.append(timing_status)
    try:
        force_traces.extend(
            build_force_based_shear_normal_traces(
                snapshot,
                data,
                axis_mode=axis_mode,
                overlay_flags=overlay_flags or {},
                vref_voltage=vref_voltage,
                settings=ShearNormalForceSettings(pzt_force_settings=pzt_force_settings or {}, timing=pzt_timing),
            )
        )
    except Exception as exc:
        status_parts.append(f"Shear Force / Normal Force skipped: {exc}")
    overlays = build_overlay_traces(
        snapshot,
        data,
        axis_mode=axis_mode,
        visible_labels=visible_set,
        overlay_flags=overlay_flags or {},
        vref_voltage=vref_voltage,
        integration_window_samples=integration_window_samples,
        hpf_cutoff_hz=hpf_cutoff_hz,
    )
    return AnalysisPreparedData(traces, force_traces, overlays, x_label, x_units, " | ".join(status_parts))


def resolve_analysis_timing(
    snapshot: AnalysisSourceSnapshot, settings: Mapping[str, object],
) -> tuple[TimingPolicy, str]:
    """Resolve MUX leak timing for Shear Force / Normal Force with the engine's resolver.

    This is the Analysis edge of ``core.piezo_engine.timing``: it supplies the two
    things only a snapshot knows -- the block-timing sidecar value (read next to the
    capture CSV named by ``source_id``) and the snapshot's own sample rate -- and
    returns the policy plus the status line shown in the Analysis tab.
    """
    edge = TimingEdgeInputs(
        block_timing_connected_time_s=_block_timing_for_auto_mode(snapshot, settings),
        sample_rate_hz=_snapshot_sample_rate_hz(snapshot) or None,
    )
    return resolve_timing_policy(snapshot.metadata, settings, edge)


def _block_timing_for_auto_mode(snapshot: AnalysisSourceSnapshot, settings: Mapping[str, object]) -> float | None:
    """The sidecar is only consulted by AUTO timing; reading it in other modes would re-read a
    possibly still-growing CSV on every re-render for nothing. An unknown mode raises in the resolver."""
    try:
        mode = normalize_timing_mode(settings.get("mux_timing_mode", TimingMode.AUTO.value))
    except ValueError:
        return None
    if mode is not TimingMode.AUTO:
        return None
    return read_block_timing_connected_time_s(snapshot.metadata, _snapshot_capture_csv_path(snapshot))


def resolve_analysis_force_timing(
    snapshot: AnalysisSourceSnapshot, settings: Mapping[str, object],
) -> tuple[TimingPolicy, str]:
    """:func:`resolve_analysis_timing`, but an unresolvable timing is a status message, not an error.

    The force traces are still built then, with the continuous leak (full trace dt),
    exactly as the tab always did.
    """
    try:
        return resolve_analysis_timing(snapshot, settings)
    except Exception as exc:
        return TimingPolicy(mode=TimingMode.CONTINUOUS), f"PZT force timing skipped: {exc}"


def _snapshot_capture_csv_path(snapshot: AnalysisSourceSnapshot) -> Path | None:
    if not snapshot.source_id.startswith("csv:"):
        return None
    return Path(snapshot.source_id.split("|", 1)[0].removeprefix("csv:"))


def _snapshot_sample_rate_hz(snapshot: AnalysisSourceSnapshot) -> float:
    return float(snapshot.sample_rate_hz or _metadata_sample_rate_hz(snapshot.metadata, snapshot.data, snapshot.timestamps_s))


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
    x = timestamps.reshape(-1, 1) + offsets.reshape(1, -1)
    return x, "Time", "s"


def build_trace_time_axis_seconds(snapshot: AnalysisSourceSnapshot) -> np.ndarray:
    timestamps = _normalize_timestamps(snapshot.timestamps_s, snapshot.sweep_count)
    offsets = _sample_offsets_s(snapshot)
    return timestamps.reshape(-1, 1) + offsets.reshape(1, -1)


def build_force_traces(snapshot: AnalysisSourceSnapshot, axis_mode: str) -> list[AnalysisTrace]:
    if snapshot.force_x_n.size == 0 and snapshot.force_z_n.size == 0:
        return []
    count = max(snapshot.force_x_n.size, snapshot.force_z_n.size)
    if axis_mode == "samples":
        x = np.arange(count, dtype=np.float64)
    elif snapshot.force_timestamps_s.size >= count:
        x = snapshot.force_timestamps_s[:count].astype(np.float64)
    else:
        x = np.linspace(
            float(snapshot.timestamps_s[0] if snapshot.timestamps_s.size else 0.0),
            float(snapshot.timestamps_s[-1] if snapshot.timestamps_s.size else max(count - 1, 0)),
            count,
            dtype=np.float64,
        )

    traces: list[AnalysisTrace] = []
    if snapshot.force_x_n.size:
        traces.append(AnalysisTrace("Force X [N]", x[: snapshot.force_x_n.size], snapshot.force_x_n, "force"))
    if snapshot.force_z_n.size:
        traces.append(AnalysisTrace("Force Z [N]", x[: snapshot.force_z_n.size], snapshot.force_z_n, "force"))
    return traces


def _build_offline_stream_index_map(config: dict, samples_per_sweep: int):
    """Group exported columns into filter streams keyed by their signal name.

    Returns an ordered ``{name: np.ndarray[column indices]}`` map when metadata carries
    ``exported_signal_columns`` matching the data width, otherwise None so the engine
    falls back to grouping by physical channel number.
    """
    names = list(config.get("exported_signal_columns", []) or []) if isinstance(config, dict) else []
    if len(names) != int(samples_per_sweep):
        return None

    stream_map: dict = {}
    for col_idx, name in enumerate(names):
        stream_map.setdefault(str(name), []).append(col_idx)
    if not stream_map:
        return None
    return {name: np.asarray(cols, dtype=np.int32) for name, cols in stream_map.items()}


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

    # Each exported column is one signal. Group columns by their exported name so that
    # array-PZT signals that reuse a physical channel number (e.g. PZT3_B vs PZT6_B)
    # stay independent, while genuine non-array oversampling (repeated identical names)
    # is still grouped into one stream.
    index_map = _build_offline_stream_index_map(config, snapshot.samples_per_sweep)

    engine = ADCFilterEngine()
    channel_rates = engine.estimate_channel_sample_rates(
        total_fs_hz,
        channels,
        repeat,
        sweep_timestamps_sec=snapshot.timestamps_s,
        index_map=index_map,
    )
    runtime = engine.build_runtime_plan(
        settings,
        total_fs_hz,
        channels,
        repeat,
        sweep_timestamps_sec=snapshot.timestamps_s,
        channel_fs_by_channel=channel_rates,
        index_map=index_map,
    )
    engine.reset_runtime_states(runtime)
    return engine.filter_block(runtime, np.asarray(snapshot.data, dtype=np.float32).copy())


def moving_window_warmup_count(integration_window_samples: int) -> int:
    """Leading sample count a ``integration_window_samples`` moving window drops.

    Shared by every moving-window derived trace (Integration overlay,
    Shear/Normal Jerk, Shear/Normal Force) so a caller can drop the same
    number of leading samples from its x/time axis that the underlying
    series functions already dropped from their own output.
    """
    return max(0, int(integration_window_samples) - 1)


def build_overlay_traces(
    snapshot: AnalysisSourceSnapshot,
    data: np.ndarray,
    *,
    axis_mode: str,
    visible_labels: Iterable[str] | None = None,
    overlay_flags: Mapping[str, bool],
    vref_voltage: float,
    integration_window_samples: int,
    hpf_cutoff_hz: float,
) -> list[AnalysisTrace]:
    if not any(
        bool(overlay_flags.get(key, False))
        for key in ("shear", "normal", "integration", "baseline_removed")
    ):
        return []

    visible_set = set(snapshot.channel_labels if visible_labels is None else visible_labels)
    x_matrix, _label, _units = build_trace_x_axis(snapshot, axis_mode)
    x = x_matrix[:, 0] if x_matrix.size else np.empty(0, dtype=np.float64)
    overlays: list[AnalysisTrace] = []

    if overlay_flags.get("integration", False):
        overlays.extend(
            build_integration_traces(
                snapshot,
                data,
                x,
                visible_labels=visible_set,
                vref_voltage=vref_voltage,
                integration_window_samples=integration_window_samples,
                hpf_cutoff_hz=hpf_cutoff_hz,
            )
        )

    if not any(bool(overlay_flags.get(key, False)) for key in ("shear", "normal", "baseline_removed")):
        return overlays

    resolved_positions = _resolve_shear_position_channels_and_volts(snapshot, data, vref_voltage)
    if resolved_positions is None:
        return overlays
    _position_channels, volts_by_position = resolved_positions

    # Shear/normal use a causal median baseline (matching the calibration
    # notebook's shear pipeline) rather than the HPF-based SignalIntegrator
    # used for the generic "Integration" overlay trace -- a Butterworth
    # high-pass filter here washed out the slow shear response.
    overlay = JerkOverlayStage(max(1, int(integration_window_samples))).push(volts_by_position)
    trimmed_x = x[overlay.dropped_leading:]
    if overlay_flags.get("baseline_removed", False):
        for position in SHEAR_SENSOR_POSITIONS:
            overlays.append(
                AnalysisTrace(
                    f"{position} Baseline Removed [V]",
                    trimmed_x,
                    np.asarray(overlay.baseline_removed[position], dtype=np.float64),
                    "derived",
                )
            )
    if overlay_flags.get("shear", False):
        overlays.append(AnalysisTrace("Shear L/R Jerk [V]", trimmed_x, overlay.shear_jerk_lr, "derived"))
        overlays.append(AnalysisTrace("Shear T/B Jerk [V]", trimmed_x, overlay.shear_jerk_tb, "derived"))
    if overlay_flags.get("normal", False):
        overlays.append(AnalysisTrace("Normal Jerk [V]", trimmed_x, overlay.normal_jerk, "derived"))
    return overlays


def _resolve_shear_position_channels_and_volts(
    snapshot: AnalysisSourceSnapshot, data: np.ndarray, vref_voltage: float,
) -> tuple[dict[str, tuple[int, str]], dict[str, np.ndarray]] | None:
    """Resolve C/L/R/T/B channel columns and raw voltage, or None if unavailable."""
    position_channels = _position_channel_map(snapshot)
    if not all(position in position_channels for position in SHEAR_SENSOR_POSITIONS):
        if snapshot.samples_per_sweep < len(SHEAR_SENSOR_POSITIONS):
            return None
        position_channels = {
            position: (
                index,
                snapshot.channel_labels[index] if index < len(snapshot.channel_labels) else position,
            )
            for index, position in enumerate(SHEAR_SENSOR_POSITIONS)
        }

    volts_by_position = {
        position: counts_to_volts(data[:, column], vref_voltage)
        for position, (column, _label) in position_channels.items()
        if column < data.shape[1]
    }
    if not all(position in volts_by_position for position in SHEAR_SENSOR_POSITIONS):
        return None
    return position_channels, volts_by_position


@dataclass(frozen=True, slots=True)
class ShearNormalForceSettings:
    """Bundled user/config settings for the Shear/Normal Force traces: the Analysis
    tab's PZT force settings mapping (resolved by ``ForceSettings.from_mapping``, partial
    mappings resolve against the base profile) and the resolved MUX leak timing."""

    pzt_force_settings: Mapping[str, object]
    timing: TimingPolicy


def _timing_for_labels(timing: TimingPolicy, labels: Iterable[str]) -> TimingPolicy:
    """Keep only the pre-sample decay entries of the five force channels.

    Unknown mappings intentionally receive no correction (a metadata map may carry
    other channels' labels), whereas the engine refuses a map that matches nothing.
    """
    used = set(labels)
    decay = {label: value for label, value in timing.pre_sample_decay_s_by_label.items() if label in used}
    return TimingPolicy(mode=timing.mode, leak_dt_s=timing.leak_dt_s, pre_sample_decay_s_by_label=decay)


def build_force_based_shear_normal_traces(
    snapshot: AnalysisSourceSnapshot,
    data: np.ndarray,
    *,
    axis_mode: str,
    overlay_flags: Mapping[str, bool],
    vref_voltage: float,
    settings: ShearNormalForceSettings,
) -> list[AnalysisTrace]:
    """Shear Force L/R, Shear Force T/B and Normal Force traces from the engine's ``ForceStage``.

    The snapshot columns are already conditioned (median-filtered and settle-trimmed
    by the loader), so they go straight into the force stage with the snapshot's own
    timestamps; the stage is used untrimmed, so the traces span every snapshot row
    (the force path has no warmup trim). Needs all five C/L/R/T/B positions and valid
    numeric PZT force settings; independent of the Jerk display checkboxes.
    """
    if not any(bool(overlay_flags.get(key, False)) for key in ("shear_force", "normal_force")):
        return []

    resolved_positions = _resolve_shear_position_channels_and_volts(snapshot, data, vref_voltage)
    if resolved_positions is None:
        raise ValueError("Shear Force / Normal Force requires all five C/L/R/T/B channels")
    position_channels, volts_by_position = resolved_positions
    labels_by_position = {position: label for position, (_column, label) in position_channels.items()}

    config = EngineConfig(
        timing=_timing_for_labels(settings.timing, labels_by_position.values()),
        force=ForceSettings.from_mapping(settings.pzt_force_settings),
        vref_voltage=vref_voltage,
    )
    time_base_s = build_trace_time_axis_seconds(snapshot)
    row_time_s = time_base_s[:, 0] if time_base_s.size else np.empty(0, dtype=np.float64)
    forces = ForceStage(labels_by_position, config).push(volts_by_position, row_time_s)

    x_matrix, _label, _units = build_trace_x_axis(snapshot, axis_mode)
    x = x_matrix[:, position_channels["C"][0]] if x_matrix.size else np.empty(0, dtype=np.float64)
    traces: list[AnalysisTrace] = []
    if overlay_flags.get("shear_force", False):
        traces.append(AnalysisTrace("Shear Force L/R [N]", x, forces.shear_force_lr, "force"))
        traces.append(AnalysisTrace("Shear Force T/B [N]", x, forces.shear_force_tb, "force"))
    if overlay_flags.get("normal_force", False):
        traces.append(AnalysisTrace("Normal Force [N]", x, forces.normal_force, "force"))
    return traces


def build_integration_traces(
    snapshot: AnalysisSourceSnapshot,
    data: np.ndarray,
    x: np.ndarray,
    *,
    visible_labels: Iterable[str],
    vref_voltage: float,
    integration_window_samples: int,
    hpf_cutoff_hz: float,
) -> list[AnalysisTrace]:
    visible_set = set(visible_labels)
    voltage_by_label = {
        label: _signal_display_values(label, data[:, column], vref_voltage)
        for label, column in iter_analysis_signal_columns(snapshot)
        if label in visible_set and column < data.shape[1] and not _is_resistance_like_label(label)
    }
    if not voltage_by_label:
        return []

    integrated = integrate_voltage_series(
        voltage_by_label,
        sample_rate_hz=_overlay_sample_rate_hz(snapshot),
        integration_window_samples=integration_window_samples,
        hpf_cutoff_hz=hpf_cutoff_hz,
        channel_map=list(voltage_by_label),
    )
    # integrate_voltage_series already dropped its own leading warmup samples
    # (total_warmup_sample_count, the moving-sum window-fill count) -- match
    # it here so the x-axis stays aligned with the y-values.
    sample_rate_hz = _overlay_sample_rate_hz(snapshot)
    warmup = total_warmup_sample_count(sample_rate_hz, integration_window_samples) if sample_rate_hz > 0 \
        else moving_window_warmup_count(integration_window_samples)
    trimmed_x = x[warmup:]
    return [
        AnalysisTrace(
            f"Integrated {label} [V samples]",
            trimmed_x,
            np.asarray(integrated[label], dtype=np.float64),
            "integration",
        )
        for label in voltage_by_label
        if label in integrated
    ]


def integrate_voltage_series(
    voltage_by_key: Mapping,
    *,
    sample_rate_hz: float,
    integration_window_samples: int,
    hpf_cutoff_hz: float,
    channel_map,
) -> dict:
    """This is a single one-shot call over an entire loaded capture (a fresh
    ``SignalIntegrator`` every call, never carried across calls the way live
    streaming usage of ``SignalIntegrator`` is), so -- like the engine's jerk
    overlay -- it drops its own leading warmup outputs before returning,
    rather than emitting partial-window values from a cold start. Uses ``total_warmup_sample_count`` (the same
    warmup the jerk-overlay traces already trim), not just
    ``moving_window_warmup_count`` -- the window-fill count alone left this
    trace's own baseline still visibly converging after the window filled,
    since the underlying HPF/baseline needs the same convergence time as
    every other derived-channel trace, just like the jerk overlays already
    account for.
    """
    keys = list(voltage_by_key)
    integrator = SignalIntegrator(
        channel_count=len(keys),
        hpf_cutoff_hz=float(hpf_cutoff_hz),
        integration_window_samples=int(integration_window_samples),
        sample_rate_hz=sample_rate_hz if sample_rate_hz > 0 else None,
        channel_map=channel_map,
    )
    try:
        result = integrator.process(
            [voltage_by_key[key] for key in keys],
            sample_rate_hz=sample_rate_hz if sample_rate_hz > 0 else None,
        )
    except Exception:
        result = _fallback_integrated(voltage_by_key, int(integration_window_samples))
    warmup = total_warmup_sample_count(sample_rate_hz, integration_window_samples) if sample_rate_hz > 0 \
        else moving_window_warmup_count(integration_window_samples)
    return {key: np.asarray(values, dtype=np.float64)[warmup:] for key, values in result.items()}


def counts_to_volts(values, vref_voltage: float) -> np.ndarray:
    max_adc_value = float((2 ** IADC_RESOLUTION_BITS) - 1)
    return (np.asarray(values, dtype=np.float64) / max_adc_value) * float(vref_voltage)


def _signal_display_values(label: str, values, vref_voltage: float) -> np.ndarray:
    if _is_resistance_like_label(label):
        return np.asarray(values, dtype=np.float64)
    return counts_to_volts(values, vref_voltage)


def _is_resistance_like_label(label: str) -> bool:
    normalized = str(label).strip().upper()
    return "_RS" in normalized or normalized.startswith("RS_") or normalized.startswith("RS ")


def _median_filter_columns(raw_columns: np.ndarray, window: int) -> np.ndarray:
    """Offline causal median-of-N filter, one full-capture pass (no carried state).

    Delegates to the engine's ``CausalMedianN`` (the same stage live and
    training use) so isolated single-sample spikes are rejected identically
    everywhere; the analysis workbench loads a whole capture at once, so one
    call over the whole array is the whole stream.
    """
    window = normalize_pzt_blip_filter_window(window)
    stage = CausalMedianN(window, n_columns=raw_columns.shape[1])
    return stage.process(raw_columns).astype(raw_columns.dtype, copy=False)


def _apply_pzt_blip_filter(
    snapshot: AnalysisSourceSnapshot, data: np.ndarray, window: int,
) -> np.ndarray:
    """Median-of-N filter PZT voltage columns before they feed integration/force calc.

    Excludes resistance-like (RS) columns, matching the live pipeline's
    exclusion in ``pzt_blip_filter.py`` (RS/555 resistance readings are never
    sampled waveforms and must not be median-filtered).
    """
    if data.ndim != 2 or data.shape[0] == 0:
        return data
    columns = [
        column
        for label, column in iter_analysis_signal_columns(snapshot)
        if not _is_resistance_like_label(label)
        and column < data.shape[1]
        and column < snapshot.samples_per_sweep
    ]
    if not columns:
        return data
    column_indices = np.asarray(sorted(set(columns)), dtype=np.int32)
    data = data.copy()
    data[:, column_indices] = _median_filter_columns(data[:, column_indices], window)
    return data


def iter_analysis_signal_columns(snapshot: AnalysisSourceSnapshot):
    indices = snapshot.channel_indices
    if indices is None:
        indices = list(range(len(snapshot.channel_labels)))
    for label, column in zip(snapshot.channel_labels, indices):
        yield label, int(column)


def _position_channel_map(snapshot: AnalysisSourceSnapshot) -> dict[str, tuple[int, str]]:
    result: dict[str, tuple[int, str]] = {}
    for label, column in iter_analysis_signal_columns(snapshot):
        normalized = str(label).strip().upper().replace(" ", "_")
        for position in SHEAR_SENSOR_POSITIONS:
            if normalized == position or normalized.endswith(f"_{position}") or normalized.endswith(f"-{position}"):
                result[position] = (int(column), label)
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


def _build_in_memory_channel_labels(owner, column_count: int, *, fallback_channels: list, fallback_repeat: int) -> tuple[list[str], list[int]]:
    labels_by_column = [None] * int(column_count)
    specs = []
    if hasattr(owner, "get_display_channel_specs"):
        try:
            specs.extend(list(owner.get_display_channel_specs() or []))
        except Exception:
            specs = []
    if hasattr(owner, "get_rosette_display_channel_specs"):
        try:
            specs.extend(list(owner.get_rosette_display_channel_specs() or []))
        except Exception:
            pass

    for spec in specs:
        label = str(spec.get("label", "")).strip()
        sample_indices = list(spec.get("sample_indices", []) or [])
        if not label or not sample_indices:
            continue
        for offset, sample_index in enumerate(sample_indices):
            try:
                column = int(sample_index)
            except (TypeError, ValueError):
                continue
            if column < 0 or column >= column_count:
                continue
            labels_by_column[column] = label if len(sample_indices) == 1 else f"{label}.{offset + 1}"

    labeled_columns = [
        (label, index)
        for index, label in enumerate(labels_by_column)
        if label is not None
    ]
    if labeled_columns:
        return [label for label, _index in labeled_columns], [index for _label, index in labeled_columns]

    fallback = _default_channel_labels(fallback_channels, fallback_repeat, column_count)
    return fallback, list(range(column_count))


def _config_get(config, key: str, default=None):
    if hasattr(config, "get"):
        return config.get(key, default)
    return getattr(config, key, default)


def _config_to_dict(config) -> dict:
    if isinstance(config, dict):
        return dict(config)
    if is_dataclass(config):
        return asdict(config)
    if hasattr(config, "__dict__"):
        return dict(vars(config))
    keys = (
        "channels",
        "repeat",
        "ground_pin",
        "use_ground",
        "osr",
        "gain",
        "reference",
        "sample_rate",
    )
    return {key: getattr(config, key) for key in keys if hasattr(config, key)}


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


def _metadata_sample_rate_hz(metadata: dict, data: np.ndarray, timestamps: np.ndarray) -> float:
    timing = metadata.get("timing", {}) if isinstance(metadata, dict) else {}
    # Per-channel rate first: this is the "sweep rate" TimingDisplayMixin/the filter
    # engine/the spectrum tab all treat as the shared per-signal frequency axis (see
    # tests/test_sample_rate_consistency.py), and it's what a *_metadata.json-driven
    # model's window_size_s/hop_size_s are defined against (texture_piezo's own
    # load_calibration_csv reads this same key). The total/effective mux rate keys
    # below are the whole-ADC-block rate across all channels (+ ground reads) --
    # roughly n_channels x per-channel rate -- and must not be used as a per-channel
    # rate; doing so previously made TouchID slice windows ~n_channels x too long.
    per_channel_rates = timing.get("per_channel_sample_rates_hz")
    if isinstance(per_channel_rates, dict) and per_channel_rates:
        values = [float(v) for v in per_channel_rates.values() if isinstance(v, (int, float)) and float(v) > 0]
        if values:
            return float(np.mean(values))
    for key in ("adc_effective_total_sample_rate_hz", "arduino_sample_rate_hz", "total_rate_hz"):
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


def _owner_analysis_timing_metadata(owner) -> dict:
    timing_state = getattr(owner, "timing_state", None)
    timing_data = getattr(timing_state, "timing_data", {}) if timing_state is not None else {}
    result = dict(timing_data) if isinstance(timing_data, dict) else {}

    # The current run may be analysed before export.  Preserve the same compact
    # calculator payload that is written to capture JSON so Auto timing uses the
    # physical sensor-to-MUX connected interval in either workflow.
    try:
        from data_processing.adc_mux_timing import (
            adc_mux_timing_log,
            calculate_adc_mux_timing_for_acquisition,
        )

        calculated = calculate_adc_mux_timing_for_acquisition(
            getattr(owner, "current_mcu", None),
            getattr(owner, "config", {}),
        )
        if calculated is not None:
            result["adc_mux_timing"] = adc_mux_timing_log(calculated)
            # Keep the full-precision seconds value outside the display-oriented
            # timing JSON. Force reconstruction must never consume rounded JSON.
            result["pzt_mux_connected_time_s"] = calculated.sensor_connected_s
            result["pzt_mux_connected_time_source"] = "adc_mux_timing.t_connected_s"
            result["pzt_pre_sample_decay_s_by_adc_input"] = {
                "1": calculated.decay_before_effective_sample_s(adc_input=1),
                "2": calculated.decay_before_effective_sample_s(adc_input=2),
            }
            result["pzt_pre_sample_decay_s_by_label"] = (
                _owner_pzt_pre_sample_decay_s_by_label(owner, calculated)
            )
            result["pzt_adc_input_by_label"] = _owner_pzt_adc_input_by_label(owner)
    except (AttributeError, TypeError, ValueError):
        # Timing support is optional and must not prevent generic acquisition
        # analysis for unsupported devices or incomplete legacy owners.
        pass

    # Priority order for pzt_mux_connected_time_s/_source: the physical
    # adc_mux_timing.t_connected_s value set above wins; only a device/config
    # unsupported by the calculator falls back to the cached average sample
    # time, and only that falls back to timing_state.arduino_sample_times.
    # Both keys are set-or-skipped together so a value is never paired with a
    # mismatched source string.
    cached_sample_time = _optional_float(getattr(owner, "_cached_avg_sample_time_sec", None))
    if cached_sample_time is not None and cached_sample_time > 0.0:
        result.setdefault("pzt_mux_connected_time_s", cached_sample_time)
        result.setdefault("pzt_mux_connected_time_source", "_cached_avg_sample_time_sec")

    sample_times = getattr(timing_state, "arduino_sample_times", []) if timing_state is not None else []
    if sample_times:
        latest_us = _optional_float(sample_times[-1])
        if latest_us is not None and latest_us > 0.0:
            result.setdefault("arduino_sample_time_us", latest_us)
            result.setdefault("pzt_mux_connected_time_s", latest_us / 1_000_000.0)
            result.setdefault("pzt_mux_connected_time_source", "timing_state.arduino_sample_times")

    block_timing_path = getattr(owner, "_block_timing_path", None)
    if block_timing_path:
        result["block_timing_csv"] = str(block_timing_path)
    return result


def _owner_pzt_adc_input_by_label(owner) -> dict[str, int]:
    """Extract the actual physical ADC input from display-channel MUX keys."""
    if not hasattr(owner, "get_display_channel_specs"):
        return {}
    try:
        specs = owner.get_display_channel_specs() or []
    except Exception:
        return {}
    mapping: dict[str, int] = {}
    for spec in specs:
        if not isinstance(spec, Mapping):
            continue
        label = str(spec.get("label", "")).strip()
        key = spec.get("key")
        if not label or not isinstance(key, tuple):
            continue
        # Array_PZT_PZR1 specs encode the physical MUX number in element 4.
        if len(key) >= 5 and key[0] == "sensor":
            try:
                adc_input = int(key[4])
            except (TypeError, ValueError):
                continue
            if adc_input in (1, 2):
                mapping[label] = adc_input
    return mapping


def _owner_pzt_pre_sample_decay_s_by_label(owner, timing) -> dict[str, float]:
    """Map display labels to exact timing for their physical MUX and repeat."""
    if not hasattr(owner, "get_display_channel_specs"):
        return {}
    try:
        specs = owner.get_display_channel_specs() or []
    except Exception:
        return {}
    result: dict[str, float] = {}
    for spec in specs:
        if not isinstance(spec, Mapping):
            continue
        label = str(spec.get("label", "")).strip()
        key = spec.get("key")
        sample_indices = list(spec.get("sample_indices", []) or [])
        if not label or not sample_indices or not isinstance(key, tuple):
            continue
        if len(key) < 5 or key[0] != "sensor":
            continue
        try:
            adc_input = int(key[4])
        except (TypeError, ValueError):
            continue
        if adc_input not in (1, 2):
            continue
        for repeat_index, _sample_index in enumerate(sample_indices):
            try:
                value = timing.decay_before_effective_sample_s(
                    adc_input=adc_input, repeat_index=repeat_index
                )
            except ValueError:
                break
            mapped_label = label if len(sample_indices) == 1 else f"{label}.{repeat_index + 1}"
            result[mapped_label] = value
    return result


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
    return _force_raw_to_newtons(np.asarray(values, dtype=np.float64), axis)


def _column_key(name: str) -> str:
    return str(name).strip().lower()


def _analysis_signal_columns_from_csv(columns: list[str]) -> list[str]:
    has_named_signal = any(not _is_legacy_placeholder_column(column) for column in columns)
    if not has_named_signal:
        return list(columns)
    return [
        column for column in columns
        if not _is_legacy_placeholder_column(column)
    ]


def _is_legacy_placeholder_column(name: str) -> bool:
    normalized = str(name).strip().lower()
    return normalized.startswith("col") and normalized[3:].isdigit()


def _force_column_newtons(rows: list[dict[str, str]], *, axis: str) -> np.ndarray:
    n_column = "Force_X_N" if axis == "x" else "Force_Z_N"
    raw_column = "Force_X" if axis == "x" else "Force_Z"
    values_n = _optional_numeric_column(rows, n_column)
    if values_n.size:
        return values_n
    values_raw = _optional_numeric_column(rows, raw_column)
    if values_raw.size:
        return _force_raw_to_newtons(values_raw, axis)
    return np.empty(0, dtype=np.float64)


def _force_raw_to_newtons(values, axis: str) -> np.ndarray:
    scale = X_FORCE_SENSOR_TO_NEWTON if axis == "x" else Z_FORCE_SENSOR_TO_NEWTON
    return np.asarray(values, dtype=np.float64) / float(scale)


def _optional_float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
