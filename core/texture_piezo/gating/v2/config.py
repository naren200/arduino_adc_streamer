"""Gating v2 configuration: which good-signal stages run and how they are tuned."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class GatingV2Config:
    """No field has a default: every value comes from the training config at training time and
    from the model bundle's ``gating_params`` at inference time, so there is one source of truth.
    ``sustain_min`` is a block count (an integral number is accepted and stored as ``int``);
    ``burst_min`` is stored as ``float``."""

    idle_gate_enabled: bool
    strength_gate_enabled: bool
    concat_enabled: bool
    burst_min: float
    sustain_min: int
    block_s: float
    sustain_blocks: int
    expiry_s: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "sustain_min", _as_count(self.sustain_min, "sustain_min"))
        object.__setattr__(self, "burst_min", _as_non_negative_float(self.burst_min, "burst_min"))
        if self.block_s <= 0:
            raise ValueError(f"block_s must be > 0, got {self.block_s}")
        if self.sustain_blocks < 1:
            raise ValueError(f"sustain_blocks must be >= 1, got {self.sustain_blocks}")
        if self.expiry_s <= 0:
            raise ValueError(f"expiry_s must be > 0, got {self.expiry_s}")


def _as_count(value, name: str) -> int:
    is_integral = isinstance(value, int) or (isinstance(value, float) and math.isfinite(value) and value.is_integer())
    if isinstance(value, bool) or not is_integral:
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return int(value)


def _as_non_negative_float(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")
    return float(value)
