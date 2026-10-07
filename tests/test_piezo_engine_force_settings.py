"""ForceSettings: documented profile values, hashing, validation, single-definition constants."""

import dataclasses
import json

import pytest

import constants.pzt_force as app_pzt_force
from core.piezo_engine import config as engine_config
from core.piezo_engine import force_settings as engine_force_settings
from core.piezo_engine.config import (
    ANALYSIS_FORCE_SETTINGS,
    ROLE_NORMAL,
    ROLE_SHEAR,
    EngineConfig,
    ForceRoleThresholds,
    ForceSettings,
    TimingMode,
    TimingPolicy,
)

ANALYSIS_SETTINGS = dict(engine_force_settings.ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS)


def test_analysis_profile_and_base_profile_resolve_to_their_documented_values():
    assert ANALYSIS_FORCE_SETTINGS == ForceSettings.from_mapping(ANALYSIS_SETTINGS)
    assert ANALYSIS_FORCE_SETTINGS.d33_pc_per_n == 120.0
    assert ANALYSIS_FORCE_SETTINGS.normal.noise_threshold_n == 0.01
    assert ANALYSIS_FORCE_SETTINGS.stuck_force_decay_tau_s == 0.5
    base = ForceSettings.from_mapping({})
    assert (base.d33_pc_per_n, base.shear.noise_threshold_n, base.shear.zero_band_min_n) == (600.0, 0.005, 0.02)


def test_engine_config_default_force_is_the_analysis_profile_and_is_hashed():
    config = EngineConfig(timing=TimingPolicy(TimingMode.CONTINUOUS))
    assert config.force is ANALYSIS_FORCE_SETTINGS
    payload = config.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["force"]["d33_pc_per_n"] == 120.0
    assert payload["force"]["normal"] == {
        "noise_threshold_n": 0.01, "zero_band_min_n": 0.05, "zero_min_event_peak_n": 0.05}


def _changed_value(value):
    if isinstance(value, bool):
        return not value
    return 0.5 if value is None else value * 1.5


def _leaf_changes(settings: ForceSettings):
    """Every (description, changed ForceSettings) pair, descending into the role thresholds."""
    for field in dataclasses.fields(settings):
        value = getattr(settings, field.name)
        if isinstance(value, ForceRoleThresholds):
            for inner in dataclasses.fields(value):
                changed = dataclasses.replace(value, **{inner.name: _changed_value(getattr(value, inner.name))})
                yield f"{field.name}.{inner.name}", dataclasses.replace(settings, **{field.name: changed})
        elif field.name == "capacitance_unit":
            yield field.name, dataclasses.replace(settings, capacitance_unit="nF")
        else:
            yield field.name, dataclasses.replace(settings, **{field.name: _changed_value(value)})


def test_config_differs_when_any_force_field_changes():
    timing = TimingPolicy(TimingMode.CONTINUOUS)
    base = EngineConfig(timing=timing)
    assert base == EngineConfig(timing=timing)
    for description, changed in _leaf_changes(ANALYSIS_FORCE_SETTINGS):
        assert EngineConfig(timing=timing, force=changed) != base, description


@pytest.mark.parametrize("overrides", [
    {"d33_pc_per_n": 0.0}, {"rleak_ohm": -1.0}, {"center_capacitance_value": 0.0}, {"capacitance_unit": "mF"},
])
def test_invalid_physical_values_are_rejected_at_construction(overrides):
    with pytest.raises(ValueError):
        dataclasses.replace(ANALYSIS_FORCE_SETTINGS, **overrides)


def test_role_thresholds_select_by_role():
    assert ANALYSIS_FORCE_SETTINGS.role_thresholds(ROLE_NORMAL) is ANALYSIS_FORCE_SETTINGS.normal
    assert ANALYSIS_FORCE_SETTINGS.role_thresholds(ROLE_SHEAR) is ANALYSIS_FORCE_SETTINGS.shear


def test_app_constants_are_the_single_core_definitions():
    assert app_pzt_force.ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS is engine_force_settings.ANALYSIS_PZT_FORCE_DEFAULT_SETTINGS
    assert app_pzt_force.PZT_FORCE_DEFAULT_SETTINGS is engine_force_settings.PZT_FORCE_DEFAULT_SETTINGS
    assert engine_config.SHEAR_JERK_SMOOTHING_WINDOW_SAMPLES == 6
    assert EngineConfig(timing=TimingPolicy(TimingMode.CONTINUOUS)).smoothing_window_samples == 6


def test_leading_warmup_is_the_longest_window_minus_one():
    timing = TimingPolicy(TimingMode.CONTINUOUS)
    assert EngineConfig(timing=timing).leading_warmup_samples == 29
    assert EngineConfig(timing=timing, integration_window_samples=10, jerk_window_samples=40).leading_warmup_samples == 39
