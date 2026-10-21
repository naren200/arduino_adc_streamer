"""Write one Markdown report per simulation result into reports/ (figures copied to reports/images/).

Reads results/<scenario>/*.csv|png and config.json; does not run QSPICE.  Run after run_simulation.py:
    python build_reports.py
"""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

import build_model as bm

HERE = bm.HERE
RES = HERE / "results"
OUT = HERE / "reports"
IMG = OUT / "images"


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def copy(src: Path, name: str) -> str:
    IMG.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, IMG / name)
    return f"images/{name}"


def common_setup(cfg: dict, t: dict) -> str:
    sp = bm.sensor_params(cfg, 0)
    sd, c, m, o, par = cfg["sensor_defaults"], cfg["circuit"], cfg["mux"], cfg["opamp"], cfg["parasitics"]
    cp_pf = sp["cp"] * 1e12
    per_addr = t["t_sensor_settle_us"] + t["t_conversion_us"] + t["t_after_conversion_us"] + (
        t["t_vmid_settle_us"] + t["t_vmid_park_us"] if t["vmid_visit_after_each"] else t["t_vmid_park_us"])
    cycle = per_addr * len(t["scan_order"])
    return f"""| Item | Value | Basis |
| --- | --- | --- |
| Sensors | 7 × SM311 (PZT-5H), {sd['width_mm']} × {sd['length_mm']} × {sd['thickness_mm']} mm | size and thickness from the user |
| Sensor model | d33 = {sd['d33_pC_per_N']:.0f} pC/N, εr = {sd['eps33_r']:.0f}, Cp = {cp_pf:.1f} pF, {sp['d33'] / sp['cp']:.2f} V/N open circuit, insulation {sd['r_insulation_ohm']:.0e} Ω, loss model "{sd['loss_model']}" | Steminc material table (SM311) |
| MUX | TMUX1308A at 3.3 V: Ron {m['ron_ohm']:.0f} Ω, CS(off) {m['cs_off_pF']} pF, CD(off) {m['cd_off_pF']} pF, CON {m['c_on_pF']} pF, Qinj {m['qinj_pC']} pC, leakage {m['ileak_s_off_nA']} nA (S→D, same sign on all channels) and {m['ileak_d_nA']} nA (D), ton {m['switch_on_ns']:.0f} ns | datasheet SCDS477A typical values |
| Buffer | OPA365 behavioral: GBW {o['gbw_hz'] / 1e6:.0f} MHz, slew {o['slew_v_per_us']:.0f} V/µs, Ro {o['ro_ohm']:.0f} Ω, Ibias {o['ib_pA']} pA | datasheet SBOS365G typical values |
| Bias / output | R13 = {c['r_bias_ohm'] / 1e3:.0f} kΩ to VMID ({c['vmid_v']} V), R21 = {c['r_out_ohm']:.0f} Ω, C29 = {c['c_out_pF']:.0f} pF, VMID channel 100 Ω + 150 pF | schematic |
| Timing per address | settle {t['t_sensor_settle_us']} µs, ADC word {t['t_conversion_us']} µs (hold at +{t['adc_acquisition_ns']} ns after settle), after-conversion {t['t_after_conversion_us']} µs, VMID settle {t['t_vmid_settle_us']:g} µs, VMID park {t['t_vmid_park_us']} µs | user; park time is a placeholder |
| Scan | {len(t['scan_order'])} addresses, {per_addr:g} µs each, one cycle = {cycle:g} µs | derived |
| Simulator | QSPICE, transient, 5 ns maximum step | |
"""


CAVEATS = """- Placeholder inputs (not verified against hardware): `t_vmid_park_us`, the 10 GΩ sensor insulation resistance, the layout parasitics, VMID as an ideal source.
- The ADC input (sample-and-hold capacitor, kickback) is not modelled; `AIN_B4` is the node behind R21/C29.
- Mux leakage is applied with the same sign (S→D) and size (1 nA, 25 °C typical) on every channel, which is a worst case for offset. Real parts vary per channel and leak far more when hot (±25 nA max at 85 °C).
- The mux injection-current clamp (pins above VDD or below GND) is not modelled.
"""


def report_two_pulses(cfg_all: dict) -> None:
    cfg = bm.apply_overrides(cfg_all, [])
    rows = read_csv(RES / "two_pulses" / "samples.csv")
    driven = {int(f["address"]): f for f in cfg["forces"]}
    sig = [r for r in rows if r["kind"] == "sensor" and int(r["address"]) in driven]
    ghost: dict = {}
    for r in rows:
        key = "VMID slots" if r["kind"] == "vmid" else f"address {r['address']}"
        if r["kind"] == "vmid" or int(r["address"]) not in driven:
            ghost[key] = max(ghost.get(key, 0.0), abs(float(r["delta_mV"])))
    f_txt = "; ".join(f"address {a}: {f['amp_N']} N, ramp {f['rise_us']:g} µs up / {f['fall_us']:g} µs down, hold {f['width_us']:g} µs, start {f['t_start_us']:g} µs"
                      for a, f in driven.items())
    tbl = "\n".join(
        f"| {r['cycle']} | {r['address']} | {float(r['t_sample_us']):.1f} | {float(r['force_N']):.3f} | {float(r['open_circuit_mV']):.0f} | {float(r['v_ain']):.4f} | {float(r['v_ain_baseline']):.4f} | {float(r['delta_mV']):+.1f} |"
        for r in sig)
    gtxt = ", ".join(f"{k}: {v:.2f} mV" for k, v in sorted(ghost.items()))
    img1 = copy(RES / "two_pulses" / "overview.png", "two_pulses_overview.png")
    img2 = copy(RES / "two_pulses" / "switch_zoom.png", "two_pulses_switch_zoom.png")
    (OUT / "REPORT_two_pulses.md").write_text(f"""# Report: two soft force pulses (TestBoard_ADC124 PZT front end)

Scenario `two_pulses` of the QSPICE model in `Qspice/TestBoard_ADC124/`. Simulated, not measured.

## Question

How does a force applied to two of the seven PZT sensors appear in the `AIN_B4` samples, how much of it leaks into the other channels, and what happens when the force is released?

## Setup

{common_setup(cfg, cfg['timing'])}
Force pulses: {f_txt}. Pulse amplitudes are limited by the 9.9 V/N sensitivity of the small sensor: 0.15 N already puts the sensor pin at about 3.0 V.

Method: every ADC hold instant is sampled from the simulated `AIN_B4`. A second run with all forces set to zero is subtracted (`delta`), which removes the baseline offset, charge-injection and leakage terms and leaves the sensor signal.

## Results

![Overview](./{img1})

Sensors with a force pulse, one row per scan cycle:

| Cycle | Address | Hold time [µs] | Force at hold [N] | Open-circuit [mV] | AIN [V] | AIN, zero-force run [V] | delta [mV] |
| --- | --- | --- | --- | --- | --- | --- | --- |
{tbl}

![Switch zoom](./{img2})

Largest |delta| on channels without a force pulse: {gtxt}.

## Findings

1. **Signal level.** At the flat top the reading is 549 mV for 0.10 N (open-circuit 988 mV) and 1090 mV for 0.15 N (open-circuit 1482 mV), i.e. 55–74 % of the open-circuit value. Two capacitive dividers act on the 61 pF sensor: the sensor node also carries ≈ 7 pF of trace and mux capacitance (61/68 ≈ 0.89), and on connection it shares its charge with the ≈ 12 pF of the mux D node (mux drain, trace, op-amp input; 68/80 ≈ 0.85), so a first visit keeps ≈ 0.76 of the open-circuit value (sensor 4: 74 %). Visits after the first find less charge, because every connection removes part of it (finding 3).
2. **Ramp sampling.** Sensor 2's first visit (cycle 0) falls on the force ramp (0.073 N) and reads 530 mV: the readings follow the instantaneous force, not the pulse amplitude.
3. **Droop with every visit.** Both flat-top readings fall from one visit to the next (sensor 2: 549 → 294 mV is partly the release ramp, but sensor 4: 1090 → 771 mV is on the flat top). The piezo only generates charge while the force changes, so each connection to the bias network (charge sharing plus 7 µs of R13 pulling the node toward VMID) removes charge that is not replaced; a static force is therefore not read back at a constant level.
4. **Release.** After the force is removed the readings go negative (−403 mV on sensor 2, −546 mV on sensor 4) and recover slowly through R13, because each visit only restores part of the deficit.
5. **Crosstalk.** Unforced channels move by at most a few mV (address 3, between the two driven sensors: 3.95 mV, address 5: 2.26 mV, address 1: 1.77 mV); VMID slots show about 0.001 mV, so the VMID visit resets the D node completely.

## Caveats

{CAVEATS}
## Reproduce

```bash
python run_simulation.py --scenario two_pulses
python build_reports.py
```

Source data: `results/two_pulses/samples.csv`; netlist: `TestBoard_ADC124_PZT.cir`.
""", encoding="utf-8")


def baseline_tables(name: str, cfg: dict) -> tuple[str, list, list]:
    vmid = cfg["circuit"]["vmid_v"]
    with (RES / name / "samples_decimated.csv").open(encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    body = [[float(x) for x in r] for r in rows[1:]]
    return rows[0], body, vmid


def report_baseline(name: str, title: str, cfg_all: dict, overrides: dict, extra_findings: str, image: str) -> None:
    cfg = bm.apply_overrides(cfg_all, [])
    for k, v in overrides.items():
        node = cfg
        parts = k.split(".")
        for p in parts[:-1]:
            node = node[p]
        node[parts[-1]] = v
    hdr, body, vmid = baseline_tables(name, cfg)
    n_addr = len(hdr) - 2
    if name == "baseline":
        pick = [0, 1, 4, 10, 30, 60, 100, 200, 300, len(body) - 1]
        pick = sorted({min(i, len(body) - 1) for i in pick})
    else:
        pick = list(range(len(body)))
    lines = []
    for i in pick:
        cyc, t = int(body[i][0]), body[i][1]
        off = [(v - vmid) * 1e3 for v in body[i][2:]]
        fan = max(off) - min(off)
        lines.append(f"| {cyc} | {t * 1e3:.3f} | " + " | ".join(f"{x:+.2f}" for x in off) + f" | {sum(off) / n_addr:+.2f} | {fan:.3f} |")
    head = "| Cycle | t [ms] | " + " | ".join(f"addr {a}" for a in cfg["timing"]["scan_order"]) + " | mean | fan-out (max−min) |"
    sep = "| --- | --- | " + " | ".join("---" for _ in range(n_addr)) + " | --- | --- |"
    img = copy(RES / name / image, f"{name}.png")
    imgf = copy(RES / "baseline_offset_fanout.png", "baseline_offset_fanout.png")
    t = cfg["timing"]
    (OUT / f"REPORT_{name}.md").write_text(f"""# Report: {title}

Scenario `{name}` of the QSPICE model in `Qspice/TestBoard_ADC124/`. Simulated, not measured.

## Question

With no force on any sensor, where does the `AIN_B4` baseline of each sensor channel sit relative to VMID ({vmid} V), how long does it take to settle, and how much do the seven addresses differ from each other (fan-out)?

## Setup

{common_setup(cfg, t)}
No force is applied. Sensor nodes start at VMID (initial condition). One ADC-hold sample per address per scan cycle is taken from `AIN_B4`; the table below lists the offset of that sample from VMID.

## Results

![Baseline per address and last cycle waveform](./{img})

| Cycle | t [ms] | {" | ".join(f"addr {a}" for a in cfg['timing']['scan_order'])} | mean | fan-out (max−min) |
{sep.replace('| --- | --- | ', '| --- | --- | ', 1)}
{chr(10).join(lines)}

(offsets in mV relative to VMID; fan-out in mV)

![Offset and fan-out, 5 µs vs 1 ms VMID settle](./{imgf})

{extra_findings}

## Caveats

{CAVEATS}
## Reproduce

```bash
python run_simulation.py --scenario {name}
python run_simulation.py --compare baseline,baseline_vmid1ms
python build_reports.py
```

Source data: `results/{name}/samples_decimated.csv` and `summary.csv`.
""", encoding="utf-8")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    cfg = bm.load_config()
    base = bm.apply_overrides(cfg, [])
    report_two_pulses(base)

    report_baseline("baseline", "baseline with no force, VMID settle 5 µs", base, {}, """## Findings

1. **Offset.** The baseline settles to **−4.35 mV** relative to VMID (1.64565 V). The first cycle starts at +1.2 mV only because the sensors are initialised at VMID.
2. **Settling.** The offset reaches −3.9 … −4.1 mV after 1 ms and is within about 0.3 mV of its final value from ~5 ms on. The time constant is about 0.5 ms: the sensor node recharges through R13 (470 kΩ) while connected, which happens for 7 µs out of every 133 µs, so the effective resistance is ≈ 470 kΩ × 133/7 ≈ 9 MΩ against Cp = 61 pF.
3. **Fan-out.** The addresses differ by up to 1.6 mV in the first cycle (later addresses have been connected later, so they started leaking later), 0.18 mV at 1 ms and 0.001–0.01 mV once settled: all seven addresses read the same −4.35 mV.
4. **Origin of the offset.** Off-state mux leakage (1 nA per off channel plus 1 nA at the drain) against the 470 kΩ bias: ≈ 1 nA × 9 MΩ ≈ 9 mV as an upper estimate; the simulation gives −4.4 mV. (This attribution is an estimate, not a separate simulation.)
5. **Solver wobble.** Between about 15 and 35 ms the sampled value wobbles by up to ±0.2 mV; this is numerical noise of the 5 ns-step simulation, not a physical drift (cycle-to-cycle std over the last 100 cycles is 0.01 mV).
6. **Why 60 ms and not 3 s.** All time constants are ≤ ~1 ms, so the value at 3 s equals the 5–60 ms steady state. A brute-force 3 s run at 5 ns would need ~6·10⁸ time points (~10 GB); a 3 s run with a 1 µs step cap was tried and rejected because the 2 ns charge-injection edges are not resolved and the sample jumped by ±5 mV from cycle to cycle.
7. **VMID slots** read +0.1 mV vs VMID in the last cycle (VMID source resistance and mux leakage), i.e. the VMID visit resets the D node.""", "baseline.png")

    report_baseline("baseline_vmid1ms", "baseline with no force, VMID settle 1 ms", base,
                    {"timing.t_vmid_settle_us": 1000.0}, """## Findings

1. **Offset.** The baseline is **−327 mV** at 92 ms (−326.8 … −327.5 mV across the addresses) and still falling slowly: the step shrinks by about ×0.65 per cycle (−325.4 → −326.8 mV over the last two cycles), so the asymptote is about **−330 mV**, with τ ≈ 2–3 cycles ≈ 16 ms. This is ~75× the −4.35 mV of the 5 µs case.
2. **Mechanism (back-of-envelope estimate, same order as the simulation).** A floating sensor leaks 1 nA into a 61 pF node, i.e. 16 mV/ms, so about 115 mV per 7.1 ms cycle. The 7 µs connection recovers only about a third of the deficit: charge sharing with the ≈ 12 pF D node restores ≈ 15 %, and 7 µs of the 470 kΩ bias (τ = 470 kΩ × (61 + 12) pF ≈ 34 µs) another ≈ 19 % of the remainder. Equilibrium: 115 mV / 0.31 ≈ 370 mV, the same order as the simulated ≈ 330 mV. With a 133 µs cycle the same leakage costs only ~2 mV per cycle.
3. **Fan-out.** In the first cycle the addresses spread by 85.9 mV (address 0 +1.2 mV, address 6 −84.7 mV): address k is read k × 1.014 ms after address 0, and the first cycle starts from VMID. The spread decays by about ×0.7 per cycle (59.8, 41.7, 29.1, 20.3 … mV) and is **0.7 mV** in the last cycle. With the 5 µs VMID settle there is no visible fan-out after 1 ms (≤ 0.01 mV).
4. **Not converged.** After 14 cycles the baseline and the fan-out are still moving (0.7 mV fan-out, ~1 mV per cycle of baseline drift decreasing). The listed "steady state" row of `summary.csv` for this scenario averages all 14 cycles and should be ignored; use the last-cycle row.
5. **VMID slots** still read +0.1 mV, so the VMID visit resets the D node regardless of its length. The whole difference to the 5 µs case is the leakage of the sensors between their visits.
6. **Sensitivity.** The result scales with the assumed leakage (1 nA, same sign on all channels) and inversely with Cp. Real leakage differs per channel and rises steeply with temperature, so treat −330 mV as this model's equal-sign worst case rather than a hardware prediction.

### Comparison with the 5 µs VMID settle

| | VMID settle 5 µs | VMID settle 1 ms |
| --- | --- | --- |
| Scan cycle | 133 µs | 7.1 ms |
| Baseline offset (last cycle) | −4.35 mV | −327 mV (asymptote ≈ −330 mV) |
| Settling time constant | ~0.5 ms | ~16 ms |
| Fan-out between addresses (last cycle) | 0.001 mV | 0.7 mV (first cycle 85.9 mV vs 1.6 mV) |
| VMID-slot residue | +0.1 mV | +0.1 mV |""", "baseline_vmid1ms.png")
    print("reports written to", OUT)


if __name__ == "__main__":
    main()
