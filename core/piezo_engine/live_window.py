"""``LiveWindow``: one window of named engine channels, the AA <-> model-runtime contract.

No positional layout crosses the boundary: a consumer asks
for channels BY NAME and assembles whatever array layout it needs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

import numpy as np

from core.piezo_engine.channel_names import ENGINE_CHANNEL_NAMES


def _frozen_channel(name: str, values: object) -> np.ndarray:
    array = np.array(values, copy=True)
    if array.ndim != 1:
        raise ValueError(f"channel {name!r} must be 1-D, got shape {array.shape}")
    if not np.issubdtype(array.dtype, np.floating):
        raise ValueError(f"channel {name!r} must be a float array, got dtype {array.dtype}")
    array.setflags(write=False)
    return array


@dataclass(frozen=True, eq=False)
class LiveWindow:
    """Named, equal-length, native-rate 1-D float channels plus their sample rate.

    ``channels`` is copied into read-only arrays behind a read-only mapping, so a
    window can be shared between the app and a runtime without defensive copies.
    Channel names must be engine output names (``channel_names``); unknown names raise.
    """

    channels: Mapping[str, np.ndarray]
    sample_rate_hz: float

    def __post_init__(self) -> None:
        if not self.channels:
            raise ValueError("LiveWindow needs at least one channel")
        unknown = sorted(set(self.channels) - ENGINE_CHANNEL_NAMES)
        if unknown:
            raise ValueError(f"unknown engine channel names {unknown}; known: {sorted(ENGINE_CHANNEL_NAMES)}")
        frozen = {name: _frozen_channel(name, values) for name, values in self.channels.items()}
        lengths = {name: array.shape[0] for name, array in frozen.items()}
        if len(set(lengths.values())) != 1:
            raise ValueError(f"channels must have equal lengths, got {lengths}")
        if next(iter(lengths.values())) < 1:
            raise ValueError("channels must hold at least one sample")
        rate = float(self.sample_rate_hz)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"sample_rate_hz must be finite and > 0, got {self.sample_rate_hz!r}")
        object.__setattr__(self, "channels", MappingProxyType(frozen))
        object.__setattr__(self, "sample_rate_hz", rate)

    @property
    def n_samples(self) -> int:
        return next(iter(self.channels.values())).shape[0]

    @property
    def duration_s(self) -> float:
        return self.n_samples / self.sample_rate_hz

    def require(self, names: Sequence[str]) -> None:
        """Raise KeyError listing every requested channel this window does not carry."""
        missing = [name for name in names if name not in self.channels]
        if missing:
            raise KeyError(f"LiveWindow is missing channels {missing}; has {list(self.channels)}")

    def stack(self, names: Sequence[str], dtype: type = np.float64) -> np.ndarray:
        """``(n_samples, len(names))`` array, columns in the order of ``names``."""
        if dtype not in (np.float64, np.float32):
            raise ValueError(f"dtype must be np.float64 or np.float32, got {dtype!r}")
        self.require(names)
        return np.stack([self.channels[name] for name in names], axis=1).astype(dtype, copy=False)

    @classmethod
    def from_stacked(cls, array: np.ndarray, names: Sequence[str], sample_rate_hz: float) -> "LiveWindow":
        """Inverse of ``stack``: column ``i`` of ``array`` becomes channel ``names[i]``."""
        matrix = np.asarray(array)
        if matrix.ndim != 2 or matrix.shape[1] != len(names):
            raise ValueError(f"array shape {matrix.shape} does not match {len(names)} names")
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate channel names in {list(names)}")
        return cls({name: matrix[:, index] for index, name in enumerate(names)}, sample_rate_hz)
