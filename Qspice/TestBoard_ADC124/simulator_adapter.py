"""Adapter that lets the Qspice/Simulator GUI drive the TestBoard_ADC124 model.

Plugin contract (see Qspice/Simulator/README.md):
    build(cfg)   -> {"netlist": str, "t_stop": float, "samples": [{"t": s, "label": str}], "warnings": [str]}
    summary(cfg) -> [(label, text), ...]            optional, shown on the System tab
Both raise ValueError with a readable message for invalid parameters.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_sibling(name: str):
    """Import a sibling module under a model-specific name so several models can each have a build_model.py."""
    mod_name = f"qspice_model_{_HERE.name}_{name}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


bm = _load_sibling("build_model")


def build(cfg: dict) -> dict:
    text, slots = bm.build(cfg, with_force=True)
    sensor = [s for s in slots if s["kind"] == "sensor"]
    warnings: list[str] = []
    forces = cfg.get("forces", [])
    for f in forces:
        sp = bm.sensor_params(cfg, int(f["address"]))
        v_open = sp["d33"] * float(f["amp_N"]) / sp["cp"]
        if cfg["circuit"]["vmid_v"] + v_open > cfg["circuit"]["vdd_v"]:
            warnings.append(f"Force pulse on address {f['address']} ({f['amp_N']} N) gives {v_open:.2f} V open circuit: "
                            f"the sensor pin would exceed VDD ({cfg['circuit']['vdd_v']} V); the model has no input clamp.")
    return {
        "netlist": text,
        "t_stop": max(s["t1"] for s in slots),
        "samples": [{"t": s["t_sample"], "label": f"addr {s['address']}"} for s in sensor],
        "warnings": warnings,
    }


def summary(cfg: dict) -> list[tuple[str, str]]:
    t = cfg["timing"]
    us = 1e-6
    n = len(t["scan_order"])
    busy = (t["t_sensor_settle_us"] + t["t_conversion_us"] + t["t_after_conversion_us"]) * us
    settle_v = t["t_vmid_settle_us"] * us
    park = t["t_vmid_park_us"] * us
    if t.get("sampling_rate_hz"):
        park = 1.0 / float(t["sampling_rate_hz"]) / n - busy - (settle_v if t["vmid_visit_after_each"] else 0.0)
    per_addr = busy + (settle_v + park if t["vmid_visit_after_each"] else park)
    cycle = n * per_addr
    sp = bm.sensor_params(cfg, 0)
    out = [
        ("Connected dwell per sensor", f"{busy * 1e6:.2f} us"),
        ("VMID park time used", f"{park * 1e6:.2f} us" + ("  (derived from the sampling rate)" if t.get("sampling_rate_hz") else "")),
        ("Time per address", f"{per_addr * 1e6:.2f} us"),
        ("Scan cycle", f"{cycle * 1e6:.2f} us"),
        ("Sampling rate per sensor", f"{1.0 / cycle:,.1f} Hz" if cycle > 0 else "n/a"),
        ("Sensor capacitance (sensor 0)", f"{sp['cp'] * 1e12:.1f} pF"),
        ("Sensitivity, open circuit", f"{sp['d33'] / sp['cp']:.2f} V/N"),
        ("Force for +/-1.65 V swing", f"{1.65 / (sp['d33'] / sp['cp']):.3f} N"),
    ]
    if t.get("run_duration_s") and cycle > 0:
        out.append(("Scan cycles in the run", f"{int((t['run_duration_s'] - t['t_start_us'] * us) / cycle)}"))
    return out
