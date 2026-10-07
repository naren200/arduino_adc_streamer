"""Frozen engine configuration: the single record of every setting that
changes the engine's numbers, with a canonical dict so
training artifacts and live inference can rebuild the same engine.

``ForceSettings`` carries every setting the Shear/Normal Force stage reads;
``ForceSettings.from_mapping`` resolves an Analysis-style settings mapping
(same precedence and legacy-key rules as the Analysis tab) into it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from core.piezo_engine.force_integrator import (
    PztChannelPhysicalParams,
    pzt_capacitance_to_farads,
    pzt_capacitance_value_for_position,
    validate_pzt_force_settings,
)
from core.piezo_engine.force_settings import (
    ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS,
    PZT_FORCE_DEFAULT_SETTINGS,
    PZT_FORCE_PIC_COULOMB_TO_COULOMB,
)
from core.piezo_engine.median import DEFAULT_MEDIAN_WINDOW_SAMPLES, validate_median_window
from core.piezo_engine.shear_constants import SHEAR_POSITION_CENTER

DEFAULT_INTEGRATION_WINDOW_SAMPLES = 30
DEFAULT_JERK_INTEGRATION_WINDOW_SAMPLES = 22
# Trailing-average window (samples) that smooths only the per-channel dF fed
# into shear detection: it suppresses noise in the shear estimate without
# smoothing the raw Normal Jerk signal the shear is subtracted from.
SHEAR_JERK_SMOOTHING_WINDOW_SAMPLES = 6
DEFAULT_VREF_VOLTAGE = 3.3
ENGINE_CONFIG_SCHEMA_VERSION = 4


class TimingMode(str, Enum):
    AUTO = "auto"
    CONTINUOUS = "continuous"
    MANUAL = "manual"
    INFER_FROM_TOTAL_SAMPLE_RATE = "infer_from_total_sample_rate"


@dataclass(frozen=True)
class TimingPolicy:
    """How the MUX-aware leak exposure was resolved.

    ``leak_dt_s`` is the resolved connected time in seconds, or None when the
    leak acts over the full sample interval (CONTINUOUS). The pre-sample decay
    map is only meaningful when a MUX-connected leak exists, so CONTINUOUS
    forbids it.
    """

    mode: TimingMode
    leak_dt_s: float | None = None
    pre_sample_decay_s_by_label: Mapping[str, float] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", TimingMode(self.mode))
        decay = {str(label): float(value) for label, value in self.pre_sample_decay_s_by_label.items()}
        object.__setattr__(self, "pre_sample_decay_s_by_label", MappingProxyType(decay))
        self._validate()

    def _validate(self) -> None:
        if self.mode is TimingMode.CONTINUOUS:
            if self.leak_dt_s is not None or self.pre_sample_decay_s_by_label:
                raise ValueError("continuous timing takes neither leak_dt_s nor pre-sample decay")
        elif self.leak_dt_s is None or not self.leak_dt_s > 0.0:
            raise ValueError(f"{self.mode.value} timing needs a resolved leak_dt_s > 0")
        if any(value < 0.0 for value in self.pre_sample_decay_s_by_label.values()):
            raise ValueError("pre-sample decay must be >= 0")

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "leak_dt_s": self.leak_dt_s,
            "pre_sample_decay_s_by_label": dict(sorted(self.pre_sample_decay_s_by_label.items())),
        }

    @classmethod
    def from_dict(cls, record: Mapping) -> "TimingPolicy":
        return cls(
            mode=TimingMode(record["mode"]),
            leak_dt_s=record["leak_dt_s"],
            pre_sample_decay_s_by_label=record["pre_sample_decay_s_by_label"],
        )


ROLE_NORMAL = "normal"
ROLE_SHEAR = "shear"
OUTER_SENSOR_CAPACITANCE_KEY_POSITION = "L"


def resolve_role_threshold(
    supplied: Mapping[str, object],
    resolved: Mapping[str, object],
    role: str,
    sub_key: str,
    *,
    legacy_shared_key: str | None = None,
) -> float:
    """Role-specific force threshold: an explicit ``{role}_force_{sub_key}`` wins,
    then an explicit legacy shared key, then the resolved role default.

    The generic ``noise_threshold_n`` legacy fallback is NOT the volt-scale
    ``noise_threshold_v`` of pre-force-rate profiles; callers migrating an old
    saved ``*_threshold_v`` value must do so before calling this.
    """
    role_key = f"{role}_force_{sub_key}"
    if role_key in supplied:
        return float(supplied[role_key])
    if legacy_shared_key is not None and legacy_shared_key in supplied:
        return float(supplied[legacy_shared_key])
    return float(resolved[role_key])


@dataclass(frozen=True, kw_only=True)
class ForceRoleThresholds:
    """Event-machine thresholds (newtons) of one integrator role."""

    noise_threshold_n: float
    zero_band_min_n: float
    zero_min_event_peak_n: float

    @classmethod
    def resolve(
        cls, supplied: Mapping[str, object], resolved: Mapping[str, object], role: str
    ) -> "ForceRoleThresholds":
        return cls(
            noise_threshold_n=resolve_role_threshold(
                supplied, resolved, role, "noise_threshold_n", legacy_shared_key="noise_threshold_n"),
            zero_band_min_n=resolve_role_threshold(
                supplied, resolved, role, "zero_band_min_n", legacy_shared_key="force_zero_band_min_n"),
            zero_min_event_peak_n=resolve_role_threshold(
                supplied, resolved, role, "zero_min_event_peak_n", legacy_shared_key="force_zero_min_event_peak_n"),
        )


@dataclass(frozen=True, kw_only=True)
class ForceSettings:
    """Everything the Shear/Normal Force stage reads from the PZT force settings.

    Capacitances stay in the user's unit (``capacitance_unit``) so the record
    matches the settings it came from; ``physical_params`` converts to SI.
    ``stuck_force_quiet_hold_s`` is stored raw (the integrator clamps it to
    ``quiet_hold_clear_s`` itself).
    """

    center_capacitance_value: float
    outer_capacitance_value: float
    capacitance_unit: str
    rleak_ohm: float
    d33_pc_per_n: float
    off_mux_leak_enabled: bool
    off_mux_rleak_ohm: float | None
    force_zero_band_fraction: float
    normal: ForceRoleThresholds
    shear: ForceRoleThresholds
    quiet_hold_release_fraction: float
    quiet_hold_clear_s: float
    stuck_force_failsafe_enabled: bool
    stuck_force_quiet_hold_s: float
    stuck_force_decay_tau_s: float

    def __post_init__(self) -> None:
        self.physical_params(SHEAR_POSITION_CENTER)
        self.physical_params(OUTER_SENSOR_CAPACITANCE_KEY_POSITION)

    @classmethod
    def from_mapping(cls, settings: Mapping[str, object] | None) -> "ForceSettings":
        """Resolve an Analysis-style settings mapping.

        Like the Analysis tab, a partial mapping resolves against the BASE
        profile (``PZT_FORCE_DEFAULT_SETTINGS``, d33=600), not the Analysis
        profile: pass the merged ``{**ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS, **saved}``.
        """
        supplied = dict(settings or {})
        resolved = {**PZT_FORCE_DEFAULT_SETTINGS, **supplied}
        off_mux_ohm = resolved.get("off_mux_rleak_ohm")
        return cls(
            center_capacitance_value=pzt_capacitance_value_for_position(supplied, SHEAR_POSITION_CENTER),
            outer_capacitance_value=pzt_capacitance_value_for_position(
                supplied, OUTER_SENSOR_CAPACITANCE_KEY_POSITION),
            capacitance_unit=str(resolved["capacitance_unit"]),
            rleak_ohm=float(resolved["rleak_ohm"]),
            d33_pc_per_n=float(resolved["d33_pc_per_n"]),
            off_mux_leak_enabled=bool(resolved.get("off_mux_leak_enabled", False)),
            off_mux_rleak_ohm=None if off_mux_ohm in (None, "") else float(off_mux_ohm),
            force_zero_band_fraction=float(resolved["force_zero_band_fraction"]),
            normal=ForceRoleThresholds.resolve(supplied, resolved, ROLE_NORMAL),
            shear=ForceRoleThresholds.resolve(supplied, resolved, ROLE_SHEAR),
            quiet_hold_release_fraction=float(resolved["quiet_hold_release_fraction"]),
            quiet_hold_clear_s=float(resolved["quiet_hold_clear_s"]),
            stuck_force_failsafe_enabled=bool(resolved["stuck_force_failsafe_enabled"]),
            stuck_force_quiet_hold_s=float(resolved["stuck_force_quiet_hold_s"]),
            stuck_force_decay_tau_s=float(resolved["stuck_force_decay_tau_s"]),
        )

    def physical_params(self, sensor_position: str) -> PztChannelPhysicalParams:
        """Per-position RC-charge constants: center capacitance for ``C``, outer otherwise."""
        is_center = sensor_position == SHEAR_POSITION_CENTER
        value = self.center_capacitance_value if is_center else self.outer_capacitance_value
        capacitance_f = pzt_capacitance_to_farads(value, self.capacitance_unit)
        d33_c_per_n = self.d33_pc_per_n * PZT_FORCE_PIC_COULOMB_TO_COULOMB
        validate_pzt_force_settings(capacitance_f, self.rleak_ohm, d33_c_per_n)
        use_off_mux = self.off_mux_leak_enabled and self.off_mux_rleak_ohm is not None
        return PztChannelPhysicalParams(
            capacitance_f=capacitance_f,
            rleak_ohm=self.rleak_ohm,
            d33_c_per_n=d33_c_per_n,
            off_mux_rleak_ohm=self.off_mux_rleak_ohm if use_off_mux else None,
        )

    def role_thresholds(self, role: str) -> ForceRoleThresholds:
        return self.normal if role == ROLE_NORMAL else self.shear

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, record: Mapping) -> "ForceSettings":
        """Inverse of :meth:`to_dict` (every field explicit, no profile merging)."""
        fields = dict(record)
        fields["normal"] = ForceRoleThresholds(**fields["normal"])
        fields["shear"] = ForceRoleThresholds(**fields["shear"])
        return cls(**fields)


ANALYSIS_FORCE_SETTINGS = ForceSettings.from_mapping(ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS)


class EngineConfigMismatchError(ValueError):
    """A model bundle's engine config is not the one the live path would run."""


@dataclass(frozen=True, kw_only=True)
class EngineConfig:
    timing: TimingPolicy
    blip_window_samples: int = DEFAULT_MEDIAN_WINDOW_SAMPLES
    integration_window_samples: int = DEFAULT_INTEGRATION_WINDOW_SAMPLES
    jerk_window_samples: int = DEFAULT_JERK_INTEGRATION_WINDOW_SAMPLES
    smoothing_window_samples: int = SHEAR_JERK_SMOOTHING_WINDOW_SAMPLES
    vref_voltage: float = DEFAULT_VREF_VOLTAGE
    force: ForceSettings = ANALYSIS_FORCE_SETTINGS
    # False skips the force stage entirely (a live model whose feature set reads no
    # force pays nothing for it); the force outputs of every result are then None.
    compute_force: bool = True

    def __post_init__(self) -> None:
        validate_median_window(self.blip_window_samples)
        for name in ("integration_window_samples", "jerk_window_samples", "smoothing_window_samples"):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be >= 1")
        if not self.vref_voltage > 0.0:
            raise ValueError("vref_voltage must be > 0")

    @property
    def leading_warmup_samples(self) -> int:
        """Leading samples every output channel drops: the longest
        window-sum stage is only fully filled after ``window - 1`` samples."""
        return max(self.integration_window_samples, self.jerk_window_samples) - 1

    def to_dict(self) -> dict:
        """JSON-safe canonical form."""
        return {
            "schema_version": ENGINE_CONFIG_SCHEMA_VERSION,
            "blip_window_samples": int(self.blip_window_samples),
            "integration_window_samples": int(self.integration_window_samples),
            "jerk_window_samples": int(self.jerk_window_samples),
            "smoothing_window_samples": int(self.smoothing_window_samples),
            "vref_voltage": float(self.vref_voltage),
            "timing": self.timing.to_dict(),
            "force": self.force.to_dict(),
            "compute_force": bool(self.compute_force),
        }

    @classmethod
    def from_dict(cls, record: Mapping) -> "EngineConfig":
        """Inverse of :meth:`to_dict`; refuses a record written by another schema."""
        if record.get("schema_version") != ENGINE_CONFIG_SCHEMA_VERSION:
            raise ValueError(f"engine config schema {record.get('schema_version')!r} != {ENGINE_CONFIG_SCHEMA_VERSION}")
        return cls(
            blip_window_samples=record["blip_window_samples"],
            integration_window_samples=record["integration_window_samples"],
            jerk_window_samples=record["jerk_window_samples"],
            smoothing_window_samples=record["smoothing_window_samples"],
            vref_voltage=record["vref_voltage"],
            timing=TimingPolicy.from_dict(record["timing"]),
            force=ForceSettings.from_dict(record["force"]),
            compute_force=record["compute_force"],
        )
