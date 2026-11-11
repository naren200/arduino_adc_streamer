"""Generate the QSPICE netlist for the Basic_Model: one ideal PZT -> ideal unity buffer -> ideal ADC.

Usage:
    python build_model.py [--config config.json] [--out Basic_Model.cir]
                          [--set section.key=value ...] [--no-force]

Everything is ideal: no parasitics, no multiplexer, no op-amp errors (infinite bandwidth, no offset, no input current, no
rails), no ADC input network. The only capacitance is the PZT itself. The PZT is a charge source (d33 * dF/dt) in parallel
with its capacitance Cp; the bias resistor returns the sensor node to ground or to VMID; the ADC samples the buffer output
at the configured sampling rate (the quantisation is done by the simulator from the saved node, see adc_range()).

The netlist is self-contained, so it can be opened directly in QSPICE. `build()` returns (netlist_text, sample_times).
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
EPS0 = 8.8541878128e-12
PICO, NANO, MICRO, MILLI = 1e-12, 1e-9, 1e-6, 1e-3
BIAS_CHOICES = ("ground", "vmid")
MAX_SAMPLES = 2_000_000
MAX_POINTS = 2_000_000            # upper limit of the automatic solver step (time points of a run)


def load_config(path: Path = HERE / "config.json") -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def apply_overrides(cfg: dict, overrides: list[str]) -> dict:
    """Apply 'section.key=value' (value parsed as JSON, else string) overrides."""
    cfg = copy.deepcopy(cfg)
    for item in overrides:
        key, _, raw = item.partition("=")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        node = cfg
        parts = key.split(".")
        for p in parts[:-1]:
            node = node[p]
        node[parts[-1]] = value
    return cfg


def sensor_params(cfg: dict) -> dict:
    p = cfg["sensor_defaults"]
    area = (p["width_mm"] * MILLI) * (p["length_mm"] * MILLI)
    cp_calc = EPS0 * p["eps33_r"] * area / (p["thickness_mm"] * MILLI)
    override = p.get("cp_pF")
    if override is None:
        cp, source = cp_calc, "calculated"
    else:
        if not float(override) > 0:
            raise ValueError("sensor capacitance override cp_pF must be positive (leave it empty to calculate it from the size)")
        cp, source = float(override) * PICO, "override"
    return {"cp": cp, "d33": p["d33_pC_per_N"] * PICO * p["force_gain"], "cp_calc": cp_calc, "cp_source": source}


def bias_voltage(cfg: dict) -> float:
    """Voltage the bias resistor returns the sensor node to: 0 V (ground) or the VMID level."""
    c = cfg["circuit"]
    if c["bias_to"] not in BIAS_CHOICES:
        raise ValueError(f"circuit.bias_to must be one of {', '.join(BIAS_CHOICES)}")
    return 0.0 if c["bias_to"] == "ground" else float(c["vmid_v"])


def adc_range(cfg: dict) -> dict:
    """ADC input range. Bias on ground: -VDD/2 .. +VDD/2 (the signal swings around 0 V). Bias on VMID: 0 .. VDD.

    ``rest_v`` is the voltage the sensor node rests at (the force calculation starts from it).
    """
    bits = int(cfg["adc"].get("bits", 12))
    if bits < 2:
        raise ValueError("adc.bits must be at least 2")
    vdd = float(cfg["circuit"]["vdd_v"])
    if not vdd > 0:
        raise ValueError("circuit.vdd_v must be positive")
    rest = bias_voltage(cfg)
    ground = cfg["circuit"]["bias_to"] == "ground"
    v_low, v_high = (-vdd / 2, vdd / 2) if ground else (0.0, vdd)
    return {"bits": bits, "vdd": vdd, "v_low": v_low, "v_high": v_high, "bipolar": v_low < 0, "rest_v": rest,
            "lsb": (v_high - v_low) / 2 ** bits}


def timing(cfg: dict) -> dict:
    """Sampling period, first sample and run length [s]."""
    adc, sim = cfg["adc"], cfg["simulation"]
    fs = float(adc["sampling_rate_hz"])
    if not fs > 0:
        raise ValueError("adc.sampling_rate_hz must be positive")
    if fs > 50e6:
        raise ValueError("adc.sampling_rate_hz must not exceed 50 MHz")
    t_stop = float(sim["run_duration_ms"]) * MILLI
    if not t_stop > 0:
        raise ValueError("simulation.run_duration_ms must be positive")
    t0 = float(adc["t_first_sample_us"]) * MICRO
    if t0 < 0:
        raise ValueError("adc.t_first_sample_us must not be negative")
    return {"period": 1.0 / fs, "t_first": t0, "t_stop": t_stop}


def sample_times(cfg: dict) -> list[float]:
    """Conversion instants: t_first + k / fs up to the end of the run."""
    t = timing(cfg)
    if t["t_first"] > t["t_stop"]:
        n = 0
    else:
        n = int(math.floor((t["t_stop"] - t["t_first"]) / t["period"] + 1e-9)) + 1
    if n < 1:
        raise ValueError(f"no ADC sample falls into the run: the first sample ({t['t_first'] * 1e6:g} us) is after the end "
                         f"({t['t_stop'] * 1e3:g} ms)")
    if n > MAX_SAMPLES:
        raise ValueError(f"{n} ADC samples in the run (maximum {MAX_SAMPLES}): lower the sampling rate or shorten the run")
    return [t["t_first"] + k * t["period"] for k in range(n)]


def max_step(cfg: dict) -> float:
    """Maximum solver step [s]. Empty simulation.max_step_us = automatic: 1/20 of the sample period and 1/1000 of the run (the
    ADC instants are solver breakpoints, force corners are breakpoints of the force sources, and QSPICE refines the step
    by itself where the circuit changes fast), never more than MAX_POINTS time points."""
    t = timing(cfg)
    user = cfg["simulation"].get("max_step_us")
    if user is not None:
        if not float(user) > 0:
            raise ValueError("simulation.max_step_us must be positive (leave it empty for the automatic step)")
        return float(user) * MICRO
    return max(min(t["period"] / 20.0, t["t_stop"] / 1000.0), t["t_stop"] / MAX_POINTS)


def bias_tau(cfg: dict) -> float:
    """Time constant R_bias * Cp of the sensor node [s]."""
    r = float(cfg["circuit"]["r_bias_ohm"])
    if not r > 0:
        raise ValueError("circuit.r_bias_ohm must be positive")
    return r * sensor_params(cfg)["cp"]


def check_sines(cfg: dict) -> None:
    rows = cfg.get("forces_sin", [])
    for s in rows:
        if float(s["freq_hz"]) <= 0 or float(s["cycles"]) <= 0:
            raise ValueError("sine force: frequency and number of cycles must be positive")
    if len(rows) > 1:
        raise ValueError("sine force: only one sine row is supported (one sensor)")


def check_addresses(cfg: dict) -> None:
    for row in list(cfg.get("forces", [])) + list(cfg.get("forces_sin", [])):
        if int(row["address"]) != 0:
            raise ValueError(f"Basic_Model has one sensor (address 0), a force row has address {row['address']}")


def sine_value(s: dict, t: float) -> float:
    """Force [N] of the sine row at time t, as the netlist's SIN source gives it: amp * sin(2 pi f (t - t_start)) for `cycles` periods,
    zero before, and after the last cycle the value it ended on (QSPICE holds it; zero for whole and half cycles)."""
    t0, f = s["t_start_us"] * MICRO, float(s["freq_hz"])
    t = min(max(t, t0), t0 + float(s["cycles"]) / f)
    return s["amp_N"] * math.sin(2 * math.pi * f * (t - t0))


def sine_ends_off_zero(s: dict) -> bool:
    """The sine stops away from a zero crossing (cycles not a multiple of 0.5): the source then holds its last value."""
    return abs(math.sin(2 * math.pi * float(s["cycles"]))) > 1e-3


def sine_source(name: str, node: str, ref: str, s: dict) -> str:
    """QSPICE sine source: SIN(offset amplitude frequency delay damping phase cycles), smooth and exact (no corners)."""
    return (f"{name} {node} {ref} SIN(0 {s['amp_N']:.9e} {float(s['freq_hz']):.9e} {s['t_start_us'] * MICRO:.9e} 0 0 "
            f"{float(s['cycles']):.9e})")


def pulse_pwl(cfg: dict, t_stop: float, enabled: bool = True) -> list[tuple[float, float]]:
    """Piecewise-linear force of the pulse rows; the sine row is a separate SIN source (see force_source)."""
    pulses = list(cfg["forces"]) if enabled else []

    def trap(f, t):
        t0 = f["t_start_us"] * MICRO
        r, w, fl = f["rise_us"] * MICRO, f["width_us"] * MICRO, f["fall_us"] * MICRO
        a = f["amp_N"]
        if t <= t0:
            return 0.0
        if t < t0 + r:
            return a * (t - t0) / r
        if t <= t0 + r + w:
            return a
        if t < t0 + r + w + fl:
            return a * (1 - (t - t0 - r - w) / fl)
        return 0.0

    times = {0.0, t_stop}
    for f in pulses:
        t0 = f["t_start_us"] * MICRO
        r, w, fl = f["rise_us"] * MICRO, f["width_us"] * MICRO, f["fall_us"] * MICRO
        times.update(x for x in (t0, t0 + r, t0 + r + w, t0 + r + w + fl) if x <= t_stop)
    return [(t, sum(trap(f, t) for f in pulses)) for t in sorted(times)]


def force_source(cfg: dict, t_stop: float, enabled: bool = True) -> str:
    """Netlist of the applied-force source (node f0, newtons as volts): a PWL source for the pulse rows and, when there is a sine row,
    QSPICE's SIN source (one per sensor), in series with the pulses if there are any."""
    check_sines(cfg)
    pwl = pulse_pwl(cfg, t_stop, enabled)
    sn = next(iter(cfg.get("forces_sin", [])), None) if enabled else None
    if sn is None:
        return fmt_pwl("Vf0", "f0", pwl)
    if any(v for _, v in pwl):
        return fmt_pwl("Vf0", "f0_pulse", pwl) + "\n" + sine_source("Vfs0", "f0", "f0_pulse", sn)
    return sine_source("Vf0", "f0", "0", sn)


def fmt_pwl(name: str, node: str, pts: list[tuple[float, float]]) -> str:
    body = " ".join(f"{t:.9e} {v:.9e}" for t, v in pts)
    words = body.split(" ")
    lines = [" ".join(words[i:i + 8]) for i in range(0, len(words), 8)]    # wrap long PWL lists with continuation lines
    out = f"{name} {node} 0 PWL(" + lines[0]
    for line in lines[1:]:
        out += "\n+ " + line
    return out + ")"


def build(cfg: dict, with_force: bool = True) -> tuple[str, list[float]]:
    c = cfg["circuit"]
    check_addresses(cfg)
    times = sample_times(cfg)
    tm = timing(cfg)
    t_stop = tm["t_stop"]
    sp = sensor_params(cfg)
    rng = adc_range(cfg)
    v_bias = rng["rest_v"]
    r_bias = float(c["r_bias_ohm"])
    if not r_bias > 0:
        raise ValueError("circuit.r_bias_ohm must be positive")
    ic = v_bias if c.get("initial_sensor_node_v") is None else float(c["initial_sensor_node_v"])
    L: list[str] = []
    a = L.append

    a("* Basic_Model - one ideal PZT (SM311 class) -> ideal unity buffer -> ideal ADC")
    a("* GENERATED by build_model.py from config.json - edit the config, not this file.")
    a("* No parasitics, no multiplexer; the only capacitance is the PZT. The supply sets the ADC range only (an ideal buffer has no rails).")
    a(f"* Bias resistor to {'ground' if c['bias_to'] == 'ground' else 'VMID'}; ADC range {rng['v_low']:g} .. {rng['v_high']:g} V.")
    a("")
    if c["bias_to"] == "vmid":
        a("* ---- VMID reference ----")
        a(f"Vvmid vmid 0 {v_bias}")
        a("")
    a("* ---- PZT: force [N] as volts -> charge source (d33*dF/dt) || Cp ----")
    a(f"* Cp={sp['cp'] / PICO:.1f} pF ({sp['cp_source']}), d33={sp['d33'] / PICO:.1f} pC/N")
    a(force_source(cfg, t_stop, with_force))
    a(f"Bq0 0 p0 I={sp['d33']:.9e}*ddt(V(f0))")
    a(f"Cp0 p0 0 {sp['cp']:.9e}")
    a("")
    a(f"* ---- bias resistor to {'ground' if c['bias_to'] == 'ground' else 'VMID'} ----")
    a(f"Rbias p0 {'0' if c['bias_to'] == 'ground' else 'vmid'} {r_bias:.9e}")
    a("")
    a("* ---- ideal unity buffer (the ADC input is the buffer output) ----")
    a("Ebuf ain 0 p0 0 1")
    a("")
    a(f"* ---- ADC: {len(times)} conversions, first at {tm['t_first']:.9e} s, every {tm['period']:.9e} s; solver breakpoint at each ----")
    a(f"Vbp bp 0 PULSE(0 1m {tm['t_first']:.9e} 1n 1n 1n {tm['period']:.9e})")
    a("Rbp bp 0 1k")
    a("")
    reltol = float(cfg["simulation"]["reltol"])
    if not 0 < reltol < 1:
        raise ValueError("simulation.reltol must be between 0 and 1 (QSPICE default 1e-3)")
    a("* tight tolerances: the default reltol of 1e-3 leaves ~0.5 mV of error on a 0.5 V signal, several LSB of a 16-bit ADC")
    a(f".options reltol={reltol:g} abstol=1e-15 vntol=1e-9")
    a(f".ic V(p0)={ic}")
    a(f".tran 0 {t_stop:.9e} 0 {max_step(cfg):.9e}")
    a(".save V(ain) V(p0) V(f0)" + (" V(vmid)" if c["bias_to"] == "vmid" else ""))
    a(".end")
    return "\n".join(L) + "\n", times


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--out", default=str(HERE / "Basic_Model.cir"))
    ap.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE")
    ap.add_argument("--no-force", action="store_true", help="zero all applied forces")
    args = ap.parse_args()
    cfg = apply_overrides(load_config(Path(args.config)), args.set)
    text, times = build(cfg, with_force=not args.no_force)
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(times)} ADC samples)")


if __name__ == "__main__":
    main()
