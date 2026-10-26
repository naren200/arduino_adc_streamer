"""Model-side features: regression of the default netlists, sensor capacitance override, ADC range and bipolar supply."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from qspice_sim import model_loader
from qspice_sim.config_utils import apply_overrides

QSPICE_DIR = Path(__file__).resolve().parents[2]
MODEL_IDS = ["TestBoard_ADC124", "TestBoard_7953"]
BASELINE = json.loads((Path(__file__).with_name("baseline_netlists.json")).read_text(encoding="utf-8"))


@pytest.fixture(scope="module", params=MODEL_IDS)
def model(request):
    return model_loader.load_model(QSPICE_DIR / request.param)


def _netlist(model, cfg):
    return model.build(cfg)["netlist"]


def _cp_values(netlist: str) -> dict[int, float]:
    return {int(m.group(1)): float(m.group(2)) for m in re.finditer(r"^Cp(\d+) p\d+ 0 (\S+)$", netlist, re.MULTILINE)}


# ------------------------------------------------------------------ regression baseline
def test_default_netlists_are_unchanged(model):
    """Every new setting at its default must leave the netlists, schedules and warnings of the original models untouched."""
    scenarios = {"default": model.default_config()}
    scenarios.update({n: apply_overrides(model.default_config(), sc.get("overrides", {})) for n, sc in model.scenarios.items()})
    for name, cfg in scenarios.items():
        built = model.build(cfg)
        ref = BASELINE[f"{model.id}/{name}"]
        assert hashlib.sha256(built["netlist"].encode()).hexdigest() == ref["sha256"], f"{model.id}/{name}: netlist changed"
        assert built["t_stop"] == ref["t_stop"] and len(built["samples"]) == ref["n_samples"]
        assert built["warnings"] == ref["warnings"]


# ------------------------------------------------------------------ B1: capacitance override
def test_capacitance_override_replaces_the_calculated_value(model):
    cfg = model.default_config()
    calc = _cp_values(_netlist(model, cfg))
    assert len(set(calc.values())) == 1                     # all sensors identical by default
    cfg["sensor_defaults"]["cp_pF"] = 80.0
    over = _cp_values(_netlist(model, cfg))
    assert all(v == pytest.approx(80e-12) for v in over.values()) and calc != over
    cfg["sensor_defaults"]["cp_pF"] = None                  # empty field: back to the formula
    assert _cp_values(_netlist(model, cfg)) == calc


def test_per_address_override_affects_only_that_sensor(model):
    cfg = model.default_config()
    base = _cp_values(_netlist(model, cfg))
    cfg["sensor_overrides"]["3"] = {"cp_pF": 25.0}
    cps = _cp_values(_netlist(model, cfg))
    assert cps[3] == pytest.approx(25e-12)
    assert {k: v for k, v in cps.items() if k != 3} == {k: v for k, v in base.items() if k != 3}


@pytest.mark.parametrize("bad", [0.0, -5.0])
def test_non_positive_capacitance_is_rejected(model, bad):
    cfg = model.default_config()
    cfg["sensor_defaults"]["cp_pF"] = bad
    with pytest.raises(ValueError, match="capacitance"):
        model.build(cfg)


def test_override_follows_into_the_loss_resistor_and_the_open_circuit_numbers(model):
    cfg = model.default_config()
    cfg["sensor_defaults"].update(loss_model="parallel_r", cp_pF=100.0)
    sp = model.adapter.bm.sensor_params(cfg, 0)
    assert sp["cp"] == pytest.approx(100e-12) and sp["cp_source"] == "override"
    assert sp["rp"] == pytest.approx(1.0 / (2 * 3.141592653589793 * 1000.0 * 100e-12 * 0.025), rel=1e-6)
    summary = dict(model.summary(cfg))
    assert summary["Sensor capacitance (sensor 0)"] == "100.0 pF (override)"
    assert float(summary["Sensitivity, open circuit"].split()[0]) == pytest.approx(600e-12 / 100e-12, rel=1e-3)


def test_derived_values_say_calculated_without_an_override(model):
    text = dict(model.summary(model.default_config()))["Sensor capacitance (sensor 0)"]
    assert text.endswith("(calculated)") and text.startswith("60.")


def test_field_hint_shows_the_calculated_capacitance(model):
    cfg = model.default_config()
    assert model.field_hints(cfg)["sensor_defaults.cp_pF"] == "calculated: 60.7 pF"
    cfg["sensor_defaults"]["cp_pF"] = 10.0                  # the hint keeps showing what is being overridden
    assert model.field_hints(cfg)["sensor_defaults.cp_pF"] == "calculated: 60.7 pF"
