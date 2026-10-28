"""Generate the QSPICE netlist for the TestBoard_ADC124 PZT + TMUX1308A + OPA365 front end.

Usage:
    python build_model.py [--config config.json] [--out TestBoard_ADC124_PZT.cir]
                          [--set section.key=value ...] [--no-force]

The netlist is self-contained (the behavioral cells in models/ are embedded), so it
can be opened directly in QSPICE.  `build()` returns (netlist_text, slots) where
`slots` describes the MUX address schedule used for sampling in run_simulation.py.
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
VMID_ADDR = 7
N_MUX_CH = 8                      # channels of the TMUX1308A; every one that is off leaks ileak_s_off from its source into the drain
PIN_NAMES = ["O6_P1B", "O6_P2B", "O6_P3B", "O6_P4B", "O6_P5B", "O7_P4B", "O7_P5B", "VMID"]


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


def sensor_params(cfg: dict, addr: int) -> dict:
    p = dict(cfg["sensor_defaults"])
    p.update({k: v for k, v in cfg.get("sensor_overrides", {}).get(str(addr), {}).items() if not k.startswith("_")})
    area = (p["width_mm"] * MILLI) * (p["length_mm"] * MILLI)
    cp_calc = EPS0 * p["eps33_r"] * area / (p["thickness_mm"] * MILLI)
    override = p.get("cp_pF")
    if override is None:
        cp, source = cp_calc, "calculated"
    else:
        if not float(override) > 0:
            raise ValueError(f"sensor {addr}: capacitance override cp_pF must be positive (leave it empty to calculate it from the size)")
        cp, source = float(override) * PICO, "override"
    rp = None
    if p.get("loss_model", "none") == "parallel_r" and p["tan_delta"] > 0:
        rp = 1.0 / (6.283185307 * p["tan_delta_ref_hz"] * cp * p["tan_delta"])
    return {"cp": cp, "d33": p["d33_pC_per_N"] * PICO * p["force_gain"], "rp": rp, "rins": p["r_insulation_ohm"],
            "cp_calc": cp_calc, "cp_source": source}


def adc_range(cfg: dict) -> dict:
    """ADC input range: 0 .. VDD when VMID is above 0, -VDD .. +VDD (bipolar, twice the span) when VMID is 0."""
    bits = int(cfg["adc"].get("bits", 12))
    if bits < 2:
        raise ValueError("adc.bits must be at least 2")
    vref = float(cfg["circuit"]["vdd_v"])
    bipolar = float(cfg["circuit"]["vmid_v"]) == 0
    v_low = -vref if bipolar else 0.0
    return {"bits": bits, "vref": vref, "v_low": v_low, "v_high": vref, "bipolar": bipolar, "lsb": (vref - v_low) / 2 ** bits}


def adc_timing(t: dict) -> dict:
    """ADC / mux-group timing derived from the number of muxes and the SPI clock (all in seconds).

    All muxes switch together and share the settle time. The ADC then converts the muxes one after the other, one SPI word
    (adc_word_clocks SCLK cycles) each; the simulated mux is the first one. Its sample is held adc_acquisition_clocks after
    the settle time, and it waits (n_mux - 1) words on the channel while the other muxes are converted. With timing.repeat > 1
    every mux input is read that many times in a row before the muxes switch (only the last repeat is kept).
    """
    f = float(t["adc_spi_clk_hz"])
    n_mux = int(t["n_mux"])
    if f <= 0:
        raise ValueError("timing.adc_spi_clk_hz must be positive")
    if n_mux < 1:
        raise ValueError("timing.n_mux must be at least 1")
    if t["adc_acquisition_clocks"] > t["adc_word_clocks"]:
        raise ValueError("timing.adc_acquisition_clocks must not exceed adc_word_clocks")
    rep = int(t.get("repeat", 1))
    if rep < 1:
        raise ValueError("timing.repeat must be at least 1")
    word = t["adc_word_clocks"] / f
    acq = t["adc_acquisition_clocks"] / f
    settle = t["t_sensor_settle_us"] * MICRO
    # Firmware order (capture-timing report): each mux input is read `repeat` words in a row, then the next input. Only the last
    # repeat is kept, so the simulated (first) mux is held after (repeat - 1) extra words, and every extra repeat adds one word per mux.
    return {"word": word, "acq": acq, "n_mux": n_mux, "repeat": rep, "wait_others": (n_mux - 1) * rep * word, "settle": settle,
            "hold_offset": settle + (rep - 1) * word + acq, "busy": settle + n_mux * rep * word,
            "words": n_mux * rep * word}


def park_on_vmid(t: dict) -> bool:
    """timing.park_on_vmid (default true): the park time is spent on VMID; false = the last sampled channel stays connected."""
    return bool(t.get("park_on_vmid", True))


def cycle_info(cfg: dict) -> dict:
    """Park time and scan cycle [s] of the configured timing; shared by the schedule and the Derived values.

    Park on VMID (default): every visit is settle + n_mux * repeat words; with timing.vmid_visit_after_each a VMID settle + park
    follows every address, otherwise one park follows the sweep. Park on the last channel: there is no VMID visit; the last address
    of the sweep stays connected for the park time, and a visit to the address that is already connected (one-channel scan) needs
    no mux command and no settle, only its n_mux * repeat words. If timing.sampling_rate_hz is set, the park time absorbs the
    remainder of the per-sensor period (ValueError when the rate is too high).
    """
    t = cfg["timing"]
    adc = adc_timing(t)
    scan = [int(a) for a in t["scan_order"]]
    n_scan = len(scan)
    park = t["t_vmid_park_us"] * MICRO
    settle_v = t["t_vmid_settle_us"] * MICRO
    visit = bool(t["vmid_visit_after_each"])
    busy = adc["busy"]
    on_vmid = park_on_vmid(t)
    if on_vmid:
        visits = [busy] * n_scan
        min_cycle = n_scan * (busy + (settle_v if visit else 0.0))
    else:
        # steady state: the address before the first one is the last one of the previous sweep
        visits = [(0.0 if scan[i] == scan[i - 1] else adc["settle"]) + adc["words"] for i in range(n_scan)]
        min_cycle = sum(visits)
    if t.get("sampling_rate_hz"):
        # per-sensor revisit rate: the cycle length is fixed and the park time absorbs the remainder
        rate = float(t["sampling_rate_hz"])
        if on_vmid:
            park = (1.0 / rate / n_scan - busy - settle_v) if visit else (1.0 / rate - n_scan * busy)
        else:
            park = 1.0 / rate - min_cycle
        if park < 0:
            raise ValueError(f"timing.sampling_rate_hz = {rate:g} Hz is too high: with {n_scan} addresses the "
                             f"minimum cycle is {min_cycle * 1e6:.1f} us (max {1.0 / min_cycle:.1f} Hz)")
    if on_vmid:
        cycle = n_scan * (busy + settle_v + park) if visit else n_scan * busy + park
    else:
        cycle = min_cycle + park
    return {"park": park, "cycle": cycle, "min_cycle": min_cycle, "visits": visits, "on_vmid": on_vmid,
            "switches": sum(1 for v in visits if v > adc["words"])}


def build_schedule(cfg: dict) -> tuple[list[dict], float, bool]:
    """Return (slots, t_stop, periodic). Each slot = address connected from t0 to t1.

    Sensor slot: settle + n_mux * repeat ADC words, the retained sample is held at t0 + settle + (repeat - 1) words + acquisition.
    With a VMID visit after every sensor: VMID slot = t_vmid_settle then t_vmid_park (other mux groups sampled), no
    conversion; sampled at the end of t_vmid_settle as a ghosting diagnostic only. Without it the sensors follow each
    other directly and the mux parks on VMID once per sweep, for t_vmid_park, after the last address.
    With timing.park_on_vmid false there is no VMID visit: the last address of every sweep stays connected for the park time (see
    cycle_info) and a repeated visit to the connected address has no settle, so a one-channel scan never switches the mux.
    If timing.sampling_rate_hz is set, t_vmid_park_us is derived from it. If timing.run_duration_s is set, num_cycles is derived from it. Runs of simulation.periodic_min_cycles or more
    cycles use periodic PULSE control sources (no tail) so very long runs stay small.
    """
    t = cfg["timing"]
    mux = cfg["mux"]
    us = MICRO
    adc = adc_timing(t)
    settle = adc["settle"]
    settle_v = t["t_vmid_settle_us"] * us
    visit = bool(t["vmid_visit_after_each"])
    n_scan = len(t["scan_order"])
    busy = adc["busy"]                                # connected per visit: settle + one ADC word per mux (this mux is first)
    info = cycle_info(cfg)
    park, cycle = info["park"], info["cycle"]
    on_vmid = info["on_vmid"]
    n_cycles = int(t["num_cycles"])
    if t.get("run_duration_s"):
        n_cycles = max(1, int((t["run_duration_s"] - t["t_start_us"] * us) / cycle))
    periodic = n_cycles >= int(cfg["simulation"].get("periodic_min_cycles", 200))
    slots: list[dict] = []
    now = t["t_start_us"] * us
    scan = [int(a) for a in t["scan_order"]]
    if not on_vmid:
        # park on the last channel: no VMID visit; the last address of the sweep stays connected for the park time and a visit to
        # the address that is already connected (one-channel scan) is not a mux switch, so it has no settle wait
        prev = VMID_ADDR                              # before the first command the mux is parked on VMID
        for cycle_i in range(n_cycles):
            for i, addr in enumerate(scan):
                lead = settle if addr != prev else 0.0
                dur = lead + adc["words"] + (park if i == n_scan - 1 else 0.0)
                slots.append({"cycle": cycle_i, "address": addr, "kind": "sensor", "t0": now, "t1": now + dur,
                              "t_sample": now + lead + (adc["repeat"] - 1) * adc["word"] + adc["acq"]})
                now += dur
                prev = addr
    else:
        for cycle_i in range(n_cycles):
            for addr in t["scan_order"]:
                slots.append({"cycle": cycle_i, "address": int(addr), "kind": "sensor", "t0": now, "t1": now + busy,
                              "t_sample": now + adc["hold_offset"]})
                now += busy
                if visit:
                    dwell = settle_v + park
                    slots.append({"cycle": cycle_i, "address": VMID_ADDR, "kind": "vmid", "t0": now, "t1": now + dwell,
                                  "t_sample": now + settle_v})
                    now += dwell
            if not visit and park > 0:
                # no VMID visit between sensors: the mux parks on VMID once, after the last address of the sweep
                slots.append({"cycle": cycle_i, "address": VMID_ADDR, "kind": "vmid", "t0": now, "t1": now + park,
                              "t_sample": now + park})
                now += park
    if not periodic:
        now += t["tail_us"] * us
        slots.append({"cycle": -1, "address": VMID_ADDR if on_vmid else scan[-1], "kind": "tail", "t0": slots[-1]["t1"],
                      "t1": now, "t_sample": now})
    # sanity: break-before-make needs the new channel to turn on after the old one is fully off
    ton, toff, edge = mux["switch_on_ns"] * NANO, mux["switch_off_ns"] * NANO, mux["control_edge_ns"] * NANO
    if ton < toff + edge:
        raise ValueError("mux.switch_on_ns must be >= switch_off_ns + control_edge_ns (break-before-make)")
    if settle <= ton + edge:
        raise ValueError(f"t_sensor_settle_us ({settle*1e9:.0f} ns) must exceed switch_on_ns + control_edge_ns")
    shortest = min(sl["t1"] - sl["t0"] for sl in slots if sl["kind"] != "tail")
    if shortest <= ton + edge:
        raise ValueError(f"shortest dwell {shortest*1e9:.1f} ns must exceed switch_on_ns + control_edge_ns")
    return slots, now, periodic


def connection_windows(cfg: dict, slots: list[dict]) -> dict[int, tuple[float, float, bool]]:
    """{slot index: (t_on, t_off, node_reset)} of the connection that holds the conversion of every sensor slot.

    Consecutive slots of one address are one connection (a channel parked on itself never switches). t_on / t_off are the middle
    of the enable edges (command + switch delay); the last connection ends with the run. node_reset: the mux node was parked
    on VMID before the connection (the first connection of the run and every one that follows a VMID slot).
    """
    mux = cfg["mux"]
    ton, toff, edge = mux["switch_on_ns"] * NANO, mux["switch_off_ns"] * NANO, mux["control_edge_ns"] * NANO
    merged: list[dict] = []
    for i, s in enumerate(slots):
        if merged and merged[-1]["address"] == s["address"]:
            merged[-1]["idx"].append(i)
            merged[-1]["t1"] = s["t1"]
        else:
            merged.append({"address": s["address"], "idx": [i], "t0": s["t0"], "t1": s["t1"]})
    out: dict[int, tuple[float, float, bool]] = {}
    for j, m in enumerate(merged):
        if m["address"] == VMID_ADDR:
            continue
        t_on = m["t0"] + ton + edge / 2
        t_off = merged[j + 1]["t0"] + toff + edge / 2 if j + 1 < len(merged) else m["t1"]
        reset = j == 0 or merged[j - 1]["address"] == VMID_ADDR
        for i in m["idx"]:
            out[i] = (t_on, t_off, reset)
    return out


def _parallel(*r: float | None) -> float:
    g = sum(1.0 / x for x in r if x)
    return 1.0 / g if g else float("inf")


def leak_info(cfg: dict, addr: int) -> dict:
    """Leakage and charge-sharing data of sensor `addr` for the simulator's force calculation (SI units)."""
    c, mux, par, op = cfg["circuit"], cfg["mux"], cfg["parasitics"], cfg["opamp"]
    sp = sensor_params(cfg, addr)
    c_on_extra = max(0.0, mux["c_on_pF"] - mux["cs_off_pF"] - mux["cd_off_pF"])
    # the op-amp differential capacitance sits between the + input and the output of a follower (bootstrapped), only the common-mode one loads the node
    c_node = mux["cd_off_pF"] + c_on_extra + par["muxd_trace_c_pF"] + par["r_bias_par_c_pF"] + op["ccm_pF"]
    ls = mux["leak_scale"]
    # connected node: the drain leakage discharges it, the off-state source->drain leakage of the other channels charges it
    i_conn = (mux["ileak_d_nA"] - (N_MUX_CH - 1) * mux["ileak_s_off_nA"]) * NANO * ls
    return {"c_side_F": (par["sensor_trace_c_pF"] + mux["cs_off_pF"]) * PICO, "c_node_F": c_node * PICO,
            "r_conn_ohm": _parallel(c["r_bias_ohm"], sp["rp"]), "v_conn_V": c["vmid_v"],
            "i_conn_A": i_conn,
            "r_float_ohm": _parallel(sp["rins"], sp["rp"]), "v_float_V": 0.0, "i_float_A": mux["ileak_s_off_nA"] * NANO * ls}


def slots_per_cycle(slots: list[dict]) -> int:
    return sum(1 for sl in slots if sl["cycle"] == 0)


def schedule_period(cfg: dict, slots: list[dict]) -> float:
    """Repeat period of the periodic sources. Parked on the last channel the first slot of the run can be longer (the first
    connection pays the settle of a one-channel scan), so the period is taken between the same sample instants of two cycles."""
    per_cycle = slots_per_cycle(slots)
    if park_on_vmid(cfg["timing"]):
        return slots[per_cycle]["t0"] - slots[0]["t0"]
    return slots[per_cycle]["t_sample"] - slots[0]["t_sample"]


def control_pulse_lines(cfg: dict, slots: list[dict]) -> list[str]:
    """Periodic PULSE control sources (one cycle of the schedule repeated); VMID is a series chain of pieces."""
    mux = cfg["mux"]
    ton, toff, edge = mux["switch_on_ns"] * NANO, mux["switch_off_ns"] * NANO, mux["control_edge_ns"] * NANO
    per_cycle = slots_per_cycle(slots)
    c0 = [dict(x) for x in slots[:per_cycle]]
    cycle = schedule_period(cfg, slots)
    seq = c0 + [dict(c0[0], t0=c0[0]["t0"] + cycle)]
    lines: list[str] = []
    vmid_pieces = []
    used = set()
    # one channel parked on itself: it is switched on once and stays on (no period, no off edge)
    steady = len({sl["address"] for sl in c0}) == 1 and c0[0]["address"] != VMID_ADDR
    for i, sl in enumerate(c0):
        if steady:
            t_on = sl["t0"] + ton
            used.add(sl["address"])
            lines.append(f"Vc{sl['address']} c{sl['address']} 0 PWL(0 0 {t_on:.9e} 0 {t_on + edge:.9e} 1)")
            continue
        t_on, t_off = sl["t0"] + ton, seq[i + 1]["t0"] + toff
        pw = t_off - t_on - edge
        if pw <= 0:
            raise ValueError("a dwell is too short for the switch timing")
        spec = f"PULSE(0 1 {t_on:.9e} {edge:.9e} {edge:.9e} {pw:.9e} {cycle:.9e})"
        if sl["address"] == VMID_ADDR:
            vmid_pieces.append(spec)
        else:
            if sl["address"] in used:
                raise ValueError("periodic mode needs each sensor address once per scan")
            used.add(sl["address"])
            lines.append(f"Vc{sl['address']} c{sl['address']} 0 {spec}")
    for a in range(7):
        if a not in used:
            lines.append(f"Vc{a} c{a} 0 0")
    t_first_off = seq[0]["t0"] + toff
    chain = [f"PWL(0 1 {t_first_off:.9e} 1 {t_first_off + edge:.9e} 0)"] + vmid_pieces
    prev = "0"
    for k, spec in enumerate(chain):
        node = "c7" if k == len(chain) - 1 else f"c7_{k}"
        lines.append(f"Vc7_{k} {node} {prev} {spec}")
        prev = node
    return lines


def control_pwl(cfg: dict, slots: list[dict]) -> dict[int, list[tuple[float, float]]]:
    """Per-address analog enable waveforms (0..1) derived from the schedule."""
    mux = cfg["mux"]
    ton, toff, edge = mux["switch_on_ns"] * NANO, mux["switch_off_ns"] * NANO, mux["control_edge_ns"] * NANO
    merged: list[dict] = []
    for s in slots:
        if merged and merged[-1]["address"] == s["address"]:
            merged[-1]["t1"] = s["t1"]
        else:
            merged.append(dict(s))
    pts: dict[int, list[tuple[float, float]]] = {a: [(0.0, 0.0)] for a in range(8)}
    pts[VMID_ADDR] = [(0.0, 1.0)]  # parked on VMID before the first command
    if merged and merged[0]["address"] != VMID_ADDR:
        # leave the initial park when the first address command arrives (break-before-make); without this point the
        # waveform ramps linearly from 1 at t = 0 down to the next VMID visit and S7 conducts for most of the run
        t_leave = merged[0]["t0"] + toff
        pts[VMID_ADDR] += [(t_leave, 1.0), (t_leave + edge, 0.0)]
    for i, s in enumerate(merged):
        a = s["address"]
        t_on = s["t0"] + ton
        if i == 0 and a == VMID_ADDR:
            t_on = None  # already on
        t_off = merged[i + 1]["t0"] + toff if i + 1 < len(merged) else None
        if t_on is not None:
            pts[a] += [(t_on, 0.0), (t_on + edge, 1.0)]
        if t_off is not None:
            pts[a] += [(t_off, 1.0), (t_off + edge, 0.0)]
    return pts


def check_sines(cfg: dict) -> None:
    seen: set[int] = set()
    for s in cfg.get("forces_sin", []):
        if float(s["freq_hz"]) <= 0 or float(s["cycles"]) <= 0:
            raise ValueError(f"sine force on address {s['address']}: frequency and number of cycles must be positive")
        if int(s["address"]) in seen:
            raise ValueError(f"sine force on address {s['address']}: only one sine row per sensor is supported")
        seen.add(int(s["address"]))


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


def pulse_pwl(cfg: dict, addr: int, t_stop: float, enabled: bool = True) -> list[tuple[float, float]]:
    """Piecewise-linear force of the pulse rows; the sine row is a separate SIN source (see force_source)."""
    pulses = [f for f in cfg["forces"] if int(f["address"]) == addr] if enabled else []

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


def force_source(cfg: dict, addr: int, t_stop: float, enabled: bool = True) -> str:
    """Netlist of the applied-force source of sensor `addr` (node f<addr>, newtons as volts): a PWL source for the pulse rows and, when the
    sensor has a sine row, QSPICE's SIN source (at most one per sensor), in series with the pulses if there are any."""
    check_sines(cfg)
    pwl = pulse_pwl(cfg, addr, t_stop, enabled)
    sn = next((f for f in cfg.get("forces_sin", []) if enabled and int(f["address"]) == addr), None)
    if sn is None:
        return fmt_pwl(f"Vf{addr}", f"f{addr}", pwl)
    if any(v for _, v in pwl):
        return fmt_pwl(f"Vf{addr}", f"f{addr}_pulse", pwl) + "\n" + sine_source(f"Vfs{addr}", f"f{addr}", f"f{addr}_pulse", sn)
    return sine_source(f"Vf{addr}", f"f{addr}", "0", sn)


def fmt_pwl(name: str, node: str, pts: list[tuple[float, float]]) -> str:
    body = " ".join(f"{t:.9e} {v:.9e}" for t, v in pts)
    # wrap long PWL lists with continuation lines
    words = body.split(" ")
    lines, cur = [], []
    for i in range(0, len(words), 8):
        lines.append(" ".join(words[i:i + 8]))
    out = f"{name} {node} 0 PWL(" + lines[0]
    for l in lines[1:]:
        out += "\n+ " + l
    return out + ")"


def build(cfg: dict, with_force: bool = True) -> tuple[str, list[dict]]:
    c, par, mux, op, sim = cfg["circuit"], cfg["parasitics"], cfg["mux"], cfg["opamp"], cfg["simulation"]
    slots, t_stop, periodic = build_schedule(cfg)
    ctl = None if periodic else control_pwl(cfg, slots)
    ls = mux["leak_scale"]
    L: list[str] = []
    a = L.append

    a("* TestBoard_ADC124 - 7x PZT (SM311 class) -> TMUX1308A -> OPA365 buffer -> AIN_B4")
    a("* GENERATED by build_model.py from config.json - edit the config, not this file.")
    a("* Sensor k is connected to MUX pin S<k>; address 7 = VMID reference channel.")
    a("")
    a("* ---- global parameters ----")
    params = {
        "mux_ron": mux["ron_ohm"], "mux_ron_vdep": mux["ron_vdep_per_V2"], "mux_vref": c["vmid_v"],
        "mux_roff": mux["roff_ohm"], "mux_cs_off": mux["cs_off_pF"] * PICO,
        "mux_c_on_extra": max(0.0, mux["c_on_pF"] - mux["cs_off_pF"] - mux["cd_off_pF"]) * PICO,
        "mux_c_ft": mux["c_feedthrough_pF"] * PICO, "mux_qinj": mux["qinj_pC"] * PICO,
        "mux_qinj_src_frac": mux["qinj_src_fraction"], "mux_ileak_s_off": mux["ileak_s_off_nA"] * NANO * ls,
        "op_gbw": op["gbw_hz"], "op_aol": op["aol"], "op_sr": op["slew_v_per_us"] * 1e6,
        "op_vos": op["vos_uV"] * MICRO, "op_ib": op["ib_pA"] * PICO,
        "op_cdiff": op["cdiff_pF"] * PICO, "op_ccm": op["ccm_pF"] * PICO,
        "op_ro": op["ro_ohm"], "op_vmargin": op["rail_margin_mV"] * MILLI,
    }
    for k, v in params.items():
        a(f".param {k}={v:.9e}")
    a("")
    a("* ---- behavioral cells ----")
    for lib in ("TMUX1308A_channel.lib", "OPA365_behavioral.lib"):
        a(f"* --- begin models/{lib} ---")
        a((HERE / "models" / lib).read_text(encoding="utf-8").rstrip())
        a(f"* --- end models/{lib} ---")
    a("")
    a("* ---- supply and reference ----")
    rng = adc_range(cfg)
    if rng["bipolar"]:
        a("* VMID = 0: bipolar analog supply, the buffer rails are -VDD .. +VDD (the real ADC124S101 inputs cannot go below 0 V)")
        a(f"Vdd vdd 0 {rng['vref']}")
        a(f"Vss vss 0 {-rng['vref']}")
    else:
        a(f"Vdd vdd 0 {c['vdd_v']}")
    a(f"Vvmid vmid 0 {c['vmid_v']}")
    a("")
    a("* ---- PZT sensors: force [N] as volts -> charge source (d33*dF/dt) || Cp || loss ----")
    for addr in range(7):
        sp = sensor_params(cfg, addr)
        a(f"* sensor {addr} -> S{addr} ({PIN_NAMES[addr]}): Cp={sp['cp']/PICO:.1f} pF, d33={sp['d33']/PICO:.1f} pC/N")
        a(force_source(cfg, addr, t_stop, with_force))
        a(f"Bq{addr} 0 p{addr} I={sp['d33']:.9e}*ddt(V(f{addr}))")
        a(f"Cp{addr} p{addr} 0 {sp['cp']:.9e}")
        if sp["rp"]:
            a(f"Rp{addr} p{addr} 0 {sp['rp']:.9e}")
        a(f"Ri{addr} p{addr} 0 {sp['rins']:.9e}")
        a(f"Rt{addr} p{addr} s{addr} {par['sensor_trace_r_ohm']}")
        a(f"Cpar{addr} s{addr} 0 {par['sensor_trace_c_pF'] * PICO:.9e}")
        if addr < 6 and par["c_coupling_adjacent_pF"] > 0:
            a(f"Ccpl{addr} s{addr} s{addr+1} {par['c_coupling_adjacent_pF'] * PICO:.9e}")
    a("")
    a("* ---- VMID channel (S7): 100 ohm from VMID, 150 pF to ground at the S7 pin ----")
    a(f"R5 vmid s7 {c['r_vmid_series_ohm']}")
    a(f"C5 s7 0 {c['c_vmid_s7_pF'] * PICO:.9e}")
    a("")
    a("* ---- MUX control (analog enables) ----")
    if periodic:
        for line in control_pulse_lines(cfg, slots):
            a(line)
    else:
        for addr in range(8):
            a(fmt_pwl(f"Vc{addr}", f"c{addr}", ctl[addr]))
    a("")
    a("* ---- TMUX1308A: 8 channels + shared drain parasitics ----")
    for addr in range(N_MUX_CH):
        a(f"Xch{addr} s{addr} muxd c{addr} TMUX_CH")
    a(f"Cd muxd 0 {mux['cd_off_pF'] * PICO:.9e}")
    a(f"Cdtr muxd 0 {par['muxd_trace_c_pF'] * PICO:.9e}")
    a(f"Ild muxd 0 DC {mux['ileak_d_nA'] * NANO * ls:.9e}")
    a("")
    a("* ---- OPA365 unity buffer, bias R13 to VMID, output R21/C29 ----")
    a(f"R13 muxd vmid {c['r_bias_ohm']}")
    if par["r_bias_par_c_pF"] > 0:
        a(f"Crb muxd vmid {par['r_bias_par_c_pF'] * PICO:.9e}")
    a(f"XU13 muxd bufo bufo vdd {'vss' if rng['bipolar'] else '0'} OPA365_BEH")
    a(f"R21 bufo ain {c['r_out_ohm']}")
    a(f"C29 ain 0 {c['c_out_pF'] * PICO:.9e}")
    a(f"Cain ain 0 {par['ain_stray_c_pF'] * PICO:.9e}")
    if periodic:
        a("* ---- solver breakpoints at every ADC hold / VMID diagnostic instant (exact samples with a coarse max step) ----")
        per_cycle = slots_per_cycle(slots)
        cyc = schedule_period(cfg, slots)
        for k, sl in enumerate(slots[:per_cycle]):
            a(f"Vbp{k} bp{k} 0 PULSE(0 1m {sl['t_sample']:.9e} 1n 1n 1n {cyc:.9e})")
            a(f"Rbp{k} bp{k} 0 1k")
    a("")
    a("* ---- initial conditions ----")
    ic = c["vmid_v"] if c["initial_sensor_node_v"] is None else c["initial_sensor_node_v"]     # empty = follows VMID
    a(" ".join([".ic"] + [f"V(s{k})={ic} V(p{k})={ic}" for k in range(7)]))
    a("")
    a(f".tran 0 {t_stop:.9e} 0 {sim['max_step_ns'] * NANO:.9e}")
    if periodic:
        names = ["ain"]  # long runs: keep the .qraw small
    else:
        names = ["ain", "bufo", "muxd", "vmid"] + [f"s{k}" for k in range(8)] + [f"f{k}" for k in range(7)] + [f"c{k}" for k in range(8)]
    a(".save " + " ".join(f"V({n})" for n in names))
    a(".end")
    return "\n".join(L) + "\n", slots


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--out", default=str(HERE / "TestBoard_ADC124_PZT.cir"))
    ap.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE")
    ap.add_argument("--no-force", action="store_true", help="zero all applied forces")
    args = ap.parse_args()
    cfg = apply_overrides(load_config(Path(args.config)), args.set)
    text, slots = build(cfg, with_force=not args.no_force)
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(slots)} MUX slots)")


if __name__ == "__main__":
    main()
