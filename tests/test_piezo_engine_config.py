import dataclasses
import json

import pytest

from core.piezo_engine.config import EngineConfig, TimingMode, TimingPolicy

LEAK_DT_S = 2.1583e-05
DECAY_BY_LABEL = {"PZT5_B": 1e-6}


def _config(**overrides) -> EngineConfig:
    return EngineConfig(timing=TimingPolicy(TimingMode.AUTO, LEAK_DT_S, DECAY_BY_LABEL), **overrides)


def test_defaults_match_the_agreed_engine_values():
    config = _config()
    assert (config.blip_window_samples, config.integration_window_samples) == (3, 30)
    assert (config.jerk_window_samples, config.smoothing_window_samples, config.vref_voltage) == (22, 6, 3.3)


def test_to_dict_is_json_safe_and_canonical():
    payload = _config().to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["timing"] == {
        "mode": "auto", "leak_dt_s": LEAK_DT_S, "pre_sample_decay_s_by_label": DECAY_BY_LABEL,
    }


def test_equal_configs_compare_equal_a_changed_field_does_not_and_a_dict_round_trip_is_equal():
    base = _config()
    assert base == _config() and base is not _config()
    assert _config(blip_window_samples=5) != base
    assert _config(jerk_window_samples=23) != base
    assert dataclasses.replace(base, timing=TimingPolicy(TimingMode.CONTINUOUS)) != base
    assert EngineConfig(timing=TimingPolicy(TimingMode.AUTO, LEAK_DT_S, {"PZT5_B": 2e-6})) != base
    assert dataclasses.replace(base, force=dataclasses.replace(base.force, d33_pc_per_n=base.force.d33_pc_per_n + 1)) != base
    assert EngineConfig.from_dict(json.loads(json.dumps(base.to_dict()))) == base


def test_config_and_timing_are_frozen_and_decay_map_is_immutable():
    config = _config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.vref_voltage = 5.0
    with pytest.raises(TypeError):
        config.timing.pre_sample_decay_s_by_label["PZT5_C"] = 1.0


@pytest.mark.parametrize("kwargs", [
    {"mode": TimingMode.AUTO},
    {"mode": TimingMode.MANUAL, "leak_dt_s": 0.0},
    {"mode": TimingMode.CONTINUOUS, "leak_dt_s": 1e-5},
    {"mode": TimingMode.CONTINUOUS, "pre_sample_decay_s_by_label": {"A": 1e-6}},
    {"mode": TimingMode.AUTO, "leak_dt_s": 1e-5, "pre_sample_decay_s_by_label": {"A": -1.0}},
])
def test_timing_policy_rejects_inconsistent_state(kwargs):
    with pytest.raises(ValueError):
        TimingPolicy(**kwargs)


@pytest.mark.parametrize("overrides", [
    {"blip_window_samples": 4}, {"integration_window_samples": 0}, {"vref_voltage": 0.0},
])
def test_engine_config_rejects_invalid_values(overrides):
    with pytest.raises(ValueError):
        _config(**overrides)
