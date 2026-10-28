"""Per-capture record of the Pressure Map normal force for one sensor package."""

from __future__ import annotations

import threading
from array import array
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from constants.force import PRESSURE_MAP_NORMAL_FORCE_SENSOR


@dataclass(frozen=True, slots=True)
class NormalForceSeries:
    """Recorded normal force samples, ordered by sweep timestamp."""

    timestamps_s: np.ndarray
    normal_force_n: np.ndarray


def ensure_normal_force_recorder(owner) -> "NormalForceRecorder":
    """Return the owner's single recorder, creating it on first use.

    Both force-feeding paths (legacy block path and batched acquisition worker)
    write into this one recorder so the exporter never has to know which ran.
    """
    recorder = getattr(owner, "normal_force_recorder", None)
    if recorder is None:
        recorder = NormalForceRecorder(PRESSURE_MAP_NORMAL_FORCE_SENSOR)
        owner.normal_force_recorder = recorder
    return recorder


class NormalForceRecorder:
    """Append-only, thread-safe log of one package's displayed normal force.

    The acquisition worker thread appends while the GUI thread reads at export
    time. Packages are matched by suffix so ``PZT3`` also finds ``A1_PZT3`` and
    ``A2_PZT3``; each array keeps its own series.
    """

    def __init__(self, sensor_suffix: str) -> None:
        self._sensor_suffix = sensor_suffix.strip().upper()
        self._lock = threading.Lock()
        self._timestamps_s: dict[str, array] = {}
        self._normal_force_n: dict[str, array] = {}

    def record(self, sensor_id: str, timestamp_s: float, normal_force_n: float) -> None:
        key = str(sensor_id).strip().upper()
        if not self._matches(key):
            return
        with self._lock:
            if key not in self._timestamps_s:
                self._timestamps_s[key] = array("d")
                self._normal_force_n[key] = array("d")
            self._timestamps_s[key].append(float(timestamp_s))
            self._normal_force_n[key].append(float(normal_force_n))

    def clear(self) -> None:
        with self._lock:
            self._timestamps_s.clear()
            self._normal_force_n.clear()

    def series(self, preferred_sensor_ids: Iterable[str]) -> NormalForceSeries | None:
        """Return the first preferred package's series, or the only one recorded."""
        with self._lock:
            key = self._resolve_key(preferred_sensor_ids)
            if key is None:
                return None
            return NormalForceSeries(
                timestamps_s=np.array(self._timestamps_s[key], dtype=np.float64),
                normal_force_n=np.array(self._normal_force_n[key], dtype=np.float64),
            )

    def _matches(self, key: str) -> bool:
        return key == self._sensor_suffix or key.endswith(f"_{self._sensor_suffix}")

    def _resolve_key(self, preferred_sensor_ids: Iterable[str]) -> str | None:
        for sensor_id in preferred_sensor_ids:
            key = str(sensor_id).strip().upper()
            if key in self._timestamps_s:
                return key
        if len(self._timestamps_s) == 1:
            return next(iter(self._timestamps_s))
        return None
