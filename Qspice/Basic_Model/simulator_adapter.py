"""Adapter that lets the Qspice/Simulator GUI drive the Basic_Model (one ideal PZT -> ideal buffer -> ideal ADC).

Plugin contract (see Qspice/Simulator/README.md):
    build(cfg)       -> {"netlist": str, "t_stop": float, "samples": [{"t": s, "label": str}], "warnings": [str]}
    summary(cfg)     -> [(label, text), ...]        optional, shown on the System tab
    adc_range(cfg)   -> {"v_low", "v_high", "rest_v"}   optional: the ADC range / rest voltage follow the bias choice
All raise ValueError with a readable message for invalid parameters.
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


def adc_range(cfg: dict) -> dict:
    """ADC input range and the voltage the sensor rests at: ground bias -VDD/2 .. +VDD/2, VMID bias 0 .. VDD."""
    rng = bm.adc_range(cfg)
    return {"v_low": rng["v_low"], "v_high": rng["v_high"], "rest_v": rng["rest_v"]}


def build(cfg: dict) -> dict:
    text, times = bm.build(cfg, with_force=True)
    t_stop = bm.timing(cfg)["t_stop"]
    rng = bm.adc_range(cfg)
    sp = bm.sensor_params(cfg)
    warnings: list[str] = []
    for sn in cfg.get("forces_sin", []):
        if bm.sine_ends_off_zero(sn):
            warnings.append(f"Force sine: {float(sn['cycles']):g} cycles end away from a zero crossing; QSPICE's SIN source "
                            f"then holds its last value ({bm.sine_value(sn, 1e9):+.3g} N) after the last cycle.")
    for kind, f in [("pulse", f) for f in cfg.get("forces", [])] + [("sine", f) for f in cfg.get("forces_sin", [])]:
        swing = abs(sp["d33"] * float(f["amp_N"]) / sp["cp"])
        sides = [name for over, name in ((rng["rest_v"] + swing > rng["v_high"], "upper"),
                                         (rng["rest_v"] - swing < rng["v_low"], "lower")) if over]
        if sides:
            warnings.append(f"Force {kind} ({f['amp_N']} N) gives {swing:.2f} V open circuit around {rng['rest_v']:g} V: "
                            f"the ADC would clip at its {' and '.join(sides)} limit ({rng['v_low']:g} .. {rng['v_high']:g} V); "
                            "the model has no input clamp, the ADC-Out value is clipped.")
    # the sensor is connected to the buffer and the bias resistor all the time: one connection that spans the run
    return {
        "netlist": text,
        "t_stop": t_stop,
        "samples": [{"t": t, "label": "addr 0", "address": 0, "t_on": 0.0, "t_off": t_stop, "node_reset": True} for t in times],
        "warnings": warnings,
    }


def adc_range_text(rng: dict) -> str:
    kind = "bipolar, bias on ground" if rng["bipolar"] else "unipolar, bias on VMID"
    return f"{rng['v_low']:g} .. {rng['v_high']:g} V ({kind}), LSB {rng['lsb'] * 1e3:.4g} mV at {rng['bits']} bit"


def summary(cfg: dict) -> list[tuple[str, str]]:
    tm = bm.timing(cfg)
    rng = bm.adc_range(cfg)
    sp = bm.sensor_params(cfg)
    tau = bm.bias_tau(cfg)
    sens = sp["d33"] / sp["cp"]
    n = len(bm.sample_times(cfg))
    bias = "ground (0 V)" if cfg["circuit"]["bias_to"] == "ground" else f"VMID ({rng['rest_v']:g} V)"
    f_up = (rng["v_high"] - rng["rest_v"]) / sens
    f_dn = (rng["rest_v"] - rng["v_low"]) / sens
    return [
        ("ADC sampling rate", f"{1.0 / tm['period']:,.1f} Hz  (period {tm['period'] * 1e6:.4g} us)"),
        ("ADC conversions in the run", f"{n}"),
        ("ADC input range", adc_range_text(rng)),
        ("Bias resistor returns to", bias),
        ("Sensor capacitance", f"{sp['cp'] * 1e12:.1f} pF ({sp['cp_source']})"),
        ("Sensitivity, open circuit", f"{sens:.2f} V/N"),
        ("Bias time constant R x Cp", f"{tau * 1e6:.4g} us  (corner frequency {1.0 / (6.283185307 * tau):,.1f} Hz)"),
        ("Decay of a held voltage per sample", f"{(1.0 - 2.718281828459045 ** (-tm['period'] / tau)) * 100:.2f} %"),
        ("Force to reach the ADC limits", f"+{f_up:.3f} N / -{f_dn:.3f} N"),
    ]


def field_hints(cfg: dict) -> dict[str, str]:
    """Text shown in the empty nullable fields: the value that is used when nothing is entered."""
    out: dict[str, str] = {}
    try:
        out["sensor_defaults.cp_pF"] = f"calculated: {bm.sensor_params(cfg)['cp_calc'] * 1e12:.1f} pF"
        out["circuit.initial_sensor_node_v"] = f"bias level: {bm.bias_voltage(cfg):g} V"
        out["simulation.max_step_us"] = f"auto: {bm.max_step({**cfg, 'simulation': {**cfg['simulation'], 'max_step_us': None}}) * 1e6:.4g} us"
    except (KeyError, ValueError):
        pass
    return out


def sensor_info(cfg: dict) -> dict[int, dict]:
    """Values of the simulator's force calculator: d33 [C/N, with the force gain], Cp [F], no extra node capacitance.

    The leakage data describe the real circuit: the sensor is always connected to the bias resistor (no multiplexer, no
    node capacitance, no side parasitics, no leakage current), so only the decay through the bias resistor is compensated.
    """
    sp = bm.sensor_params(cfg)
    rest = bm.bias_voltage(cfg)
    return {0: {"d33_C_per_N": sp["d33"], "cp_F": sp["cp"], "cextra_F": 0.0,
                "leak": {"c_side_F": 0.0, "c_node_F": 0.0, "r_conn_ohm": float(cfg["circuit"]["r_bias_ohm"]), "v_conn_V": rest,
                         "i_conn_A": 0.0, "r_float_ohm": 1e18, "v_float_V": rest, "i_float_A": 0.0}}}
