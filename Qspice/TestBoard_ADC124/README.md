# TestBoard_ADC124 PZT front-end – QSPICE model

Transient model of seven PZT sensors (SM311 / PZT-5H class, 0.7 mm x 0.7 mm x 0.25 mm) on the
TestBoard_ADC124 B bank: sensors → TMUX1308A (S0..S6) → OPA365 unity buffer → R21/C29 → `AIN_B4`.
MUX pin S7 is the VMID channel (VMID 1.65 V through 100 Ω, 150 pF to ground at the pin).
The op-amp non-inverting input is biased to VMID through R13 (470 kΩ). Supply is 3.3 V.

## Run

```bash
pip install numpy matplotlib
python run_simulation.py                                   # both scenarios
python run_simulation.py --scenario two_pulses             # ~1 s
python run_simulation.py --scenario baseline               # ~4 min, 60 ms of scanning at full resolution, ~12 M time points
python run_simulation.py --scenario baseline --set timing.run_duration_s=0.02
python run_simulation.py --scenario baseline_vmid1ms        # ~5 min
python run_simulation.py --compare baseline,baseline_vmid1ms # plot results/compare_*.png
python run_simulation.py --sweep timing.t_vmid_park_us=0,7,20 --no-plots     # two_pulses sweep
python build_model.py                                      # only regenerate the netlist
```

Scenarios live in `config.json` → `scenarios` (overrides applied on top of the base config):

| Scenario | What it does | Outputs (`results/<scenario>/`) |
| --- | --- | --- |
| `two_pulses` | Soft force pulses (40 µs rise and fall) on sensors 2 (0.10 N) and 4 (0.15 N), ~570 µs. A second zero-force run gives the baseline, so `delta = stimulated − baseline` isolates the signal. | `samples.csv`, `overview.png`, `switch_zoom.png` |
| `baseline` | No force. 60 ms (450 scan cycles) at the full 5 ns resolution. Runs of ≥ 200 cycles use periodic PULSE control sources and save only `V(AIN)`. | `summary.csv` (values at first cycle / 1 / 5 / 10 / 30 ms / last cycle), `samples_decimated.csv`, `baseline.png` |
| `baseline_vmid1ms` | Same as `baseline` but `t_vmid_settle_us = 1000`: one scan cycle is ~7.1 ms and each sensor is connected only 7 µs per cycle. 100 ms (14 cycles) at 5 ns, ~20 M time points, ~5 min. | `summary.csv`, `samples_decimated.csv` (all 14 cycles), `baseline_vmid1ms.png` |

QSPICE is invoked headless (`QSPICE64.exe deck.cir -binary`); set `QSPICE_EXE` if it is not in
`C:\Program Files\QSPICE`. `TestBoard_ADC124_PZT.cir` (the `two_pulses` netlist) is self-contained and opens in QSPICE.
Do not edit it by hand; it is regenerated from [config.json](config.json). Raw `.qraw` files are git-ignored and were deleted after the reports were generated (100–320 MB each); rerun the scenario to recreate them.

## GUI

[../Simulator/](../Simulator/README.md) loads this model through `simulator_model.json` (parameter forms, nodes, plot groups) and
`simulator_adapter.py` (builds the netlist from the edited configuration): change components, timing, sampling rate or the force
profile, pick nodes, run, save the image and compare runs. `python Qspice/Simulator/run_simulator.py`

## Reports

One report per result (Markdown with figures and tables), written by `python build_reports.py` from the saved CSV/PNG results:

- [reports/REPORT_two_pulses.md](reports/REPORT_two_pulses.md)
- [reports/REPORT_baseline.md](reports/REPORT_baseline.md) (VMID settle 5 µs)
- [reports/REPORT_baseline_vmid1ms.md](reports/REPORT_baseline_vmid1ms.md) (VMID settle 1 ms, with the comparison)

`results/baseline_offset_fanout.png` (from `python run_simulation.py --compare baseline,baseline_vmid1ms`) shows the baseline offset and the address fan-out of both baselines.

## Timing of one sensor visit (all in `config.json` → `timing`)

```text
address command ► settle ► ADC word (track 187.5 ns → HOLD = sample, then conversion) ► after-conversion ► VMID settle ► VMID park ► next address
                  1 µs     t_conversion_us = 1 µs                                        5 µs              5 µs         7 µs (placeholder)
                  |<------ sensor stays connected: settle + conversion + after (7 µs) ------>|<-- parked on VMID, no conversion -->|
```

| Key | Default | Meaning |
| --- | --- | --- |
| `t_sensor_settle_us` | 1 | Wait after the address command before the ADC starts tracking. |
| `adc_acquisition_ns` | 187.5 | ADC tracking window; the **sample instant** is `settle + acquisition` after the command (ADC124S101 at 16 MHz, 3 SCLK). |
| `t_conversion_us` | 1 | Full ADC word starting at the end of settle (3 acquisition + 13 conversion clocks at 16 MHz). |
| `t_after_conversion_us` | 5 | The 3 other muxes of the group are read; **this sensor stays connected**. |
| `t_vmid_settle_us` | 5 | VMID-channel settle (separate variable). No conversion, like the current firmware; the model samples the end of it only as a ghosting diagnostic. |
| `sampling_rate_hz` | not set | Optional per-sensor revisit rate. When set, the VMID park time is derived from it (error if the rate is too high for the other timing). |
| `t_vmid_park_us` | 7 | **Placeholder.** Extra time parked on VMID while the other mux groups are sampled and data is sent. Replace with the real value. With `vmid_visit_after_each = false` it extends the sensor dwell instead. |

The old `sample_lead_ns` key is gone: the sample instant is now the ADC hold above.

## What is modelled

| Block | Model |
| --- | --- |
| PZT | Force input in N (1 V = 1 N) → charge source `d33·dF/dt` ‖ `Cp = ε0·εr·A/t` ‖ insulation `R` (10 GΩ). Optional `loss_model = "parallel_r"` adds tan δ as a parallel resistor (off by default, see Caveats). Size, thickness, material constants and force gain are per-sensor configurable (`sensor_overrides`). |
| MUX (per channel) | Ron, off isolation, source/drain/on capacitances, S→D feedthrough capacitance, charge injection, off-state S→D leakage and D leakage. Break-before-make enforced by `switch_on_ns ≥ switch_off_ns + control_edge_ns`. |
| OPA365 | Behavioral: GBW, slew, rail clamp with headroom, output resistance, offset, bias current, input capacitances. Not the TI SPICE model. |
| Parasitics | Sensor trace R/C, adjacent-pin coupling, D-trace C, R13 stray C, `AIN` stray C, plus `leak_scale`. |

`run_simulation.py` runs each case twice (with forces, and a zero-force baseline) and samples `AIN_B4` at each ADC hold.
`delta = stimulated − baseline` removes offset/charge-injection terms. "Ghost" is the delta on VMID slots and on sensors
without a force pulse; "signal" covers sensors that have a pulse.

## TMUX1308A parameters checked against the datasheet

Source: TI SCDS477A (rev A, June 2025), VDD = 3.3 V, 25 °C, typical column. The first version of this model used
unverified guesses; several were wrong (Ron 5 Ω vs 120 Ω, leakage 20x too small, charge-injection sign).

| Config key | Value | Datasheet basis |
| --- | --- | --- |
| `ron_ohm` | 120 | §6.6 RON typ 120 Ω (max 330 Ω; 350 Ω at 85 °C). Fig. 6-2 shows ~65–185 Ω depending on signal voltage; `ron_vdep_per_V2` can approximate that, default 0. |
| `cs_off_pF` | 2 | §6.6 CSOFF typ 2 pF (max 14). |
| `cd_off_pF` | 7 | §6.6 CDOFF typ 7 pF (max 37), all channels off. |
| `c_on_pF` | 11 | §6.6 CSON/CDON typ 11 pF (max 40). Lumped value, so the extra capacitance added on the selected channel is `11 − 2 − 7 = 2 pF`. |
| `c_feedthrough_pF` | 0.1 | **Derived**: off isolation / crosstalk −90 dB at 1 MHz, 50 Ω (−110 dB at 100 kHz) ⇒ C ≈ 0.1 pF. |
| `qinj_pC` | −0.8 | §6.7 QINJ −0.8 pC (RS = 0 Ω, CL = 100 pF). Assumed to be the step seen when the channel turns off, opposite sign on turn-on. |
| `qinj_src_fraction` | 0 | The datasheet figure is the drain-side injection; the source-side share is not specified. |
| `ileak_s_off_nA`, `ileak_d_nA` | 1 | §6.6 IS(OFF), ID(OFF), ID(ON) ±1 nA typ at 25 °C. Limits: ±25 nA at 85 °C, ±800 nA at 125 °C (use `leak_scale`). The model applies the same sign to every channel, which is the worst case for offset. |
| `switch_on_ns` | 25 | §6.8 tTRAN typ 23 ns / tON(EN) typ 26 ns (max 51 / 40). |
| `switch_off_ns`, `control_edge_ns` | 1, 2 | Chosen so the break-before-make gap is `25 − 1 − 2 = 22 ns`, close to tBBM typ 24 ns (min 1 ns). tOFF(EN) typ 40 ns (max 65) is not modelled separately. |
| `roff_ohm` | 1e12 | Modelling choice; off isolation comes from the feedthrough capacitor. |

## PZT and OPA365 parameters checked against the supplied documents

| Config key | Value | Basis |
| --- | --- | --- |
| `d33_pC_per_N` | 600 | Steminc piezo material table, SM311 column (PZT-5H, Navy Type VI): d33 600 ×10⁻¹² m/V (d31 −270, g33 19.4 ×10⁻³ Vm/N). Cross-check: `d33/(ε0·εr)` = 19.4 mVm/N ✓. |
| `eps33_r` | 3500 | Same table: εT33/ε0 = 3500 at 1 kHz. |
| `tan_delta` | 0.025 | Same table: 2.5 % at 1 kHz. |
| `width_mm`, `length_mm`, `thickness_mm` | 0.7, 0.7, 0.25 | Confirmed by the user. Gives Cp ≈ 61 pF and 9.9 V/N open circuit, so forces above ~0.17 N push the sensor pin past the 3.3 V rail. |
| OPA365 `gbw_hz`, `slew_v_per_us`, `aol`, `vos_uV` | 50 MHz, 25 V/µs, 120 dB, 100 µV | SBOS365G §7.6 typ (GBW/SR specified at VS = 5 V, reused at 3.3 V). |
| `ib_pA` | 0.2 | §7.6 IB ±0.2 pA typ (±10 pA max, rises strongly above ~85 °C). |
| `cdiff_pF`, `ccm_pF` | 6, 2 | §7.6 input capacitance: differential 6 pF, common-mode 2 pF. |
| `ro_ohm` | 30 | §7.6 open-loop output impedance 30 Ω at 1 MHz. |
| `rail_margin_mV` | 10 | §7.6 output swing from rail 10 mV typ (20 max), RL = 10 kΩ. |

**Discrepancy to check:** the SMD22T25311S page lists a static capacitance of 2.0 nF ±30 % for the 22 mm × 0.25 mm disc, but
`ε0·3500·π·(11 mm)²/0.25 mm` ≈ 47 nF. The page value is a factor of ~23 lower than the material table implies (it would
correspond to εr ≈ 150). The model uses the material table; measure the real sensor with an LCR meter at 1 kHz and set
`eps33_r` (the thickness is now given as 0.25 mm) so `Cp` matches.

Still not verified: `r_insulation_ohm`, the layout parasitics, the VMID source, and the `t_vmid_park_us` value.

## Results

### `two_pulses` (0.7 × 0.7 × 0.25 mm sensors, 40 µs force ramps)

Sensor 2 gets 0.10 N, sensor 4 gets 0.15 N (amplitudes lowered from the earlier 0.5 / 0.8 N because the 61 pF sensor gives 9.9 V/N). Readings at the ADC hold, stimulated minus zero-force run:

| Sensor | Force at the sample | Open-circuit voltage | Reading at `AIN_B4` |
| --- | --- | --- | --- |
| 2, first visit (force still ramping) | 0.073 N | 721 mV | 530 mV |
| 2, flat top | 0.100 N | 988 mV | 549 mV |
| 4, flat top | 0.150 N | 1482 mV | 1090 mV |
| 2, release ramp | 0.087 N | 860 mV | 294 mV |
| 2, after release | 0 N | 0 | −403 mV |
| 4, after release | 0 N | 0 | −546 mV |

The reading is roughly 55–75 % of the open-circuit value because of two capacitive dividers: ≈ 7 pF of trace and mux capacitance on the sensor node (61/68) and ≈ 12 pF at the mux D node that the sensor shares its charge with on connection (68/80). The step on connection takes ~10 ns (Ron × the series capacitance); the slower recovery toward VMID has τ = R13 × (Cp + C_D) ≈ 34 µs.
After release the sensor node sits below VMID and recovers slowly through R13, hence the negative readings. Largest reading on any other
channel: about 4 mV; VMID slots: about 0.001 mV.

### `baseline` (no force)

Baseline of every channel, mV relative to VMID (1.65 V), identical for all seven sensors once settled:

| Time | Baseline |
| --- | --- |
| first cycle (21 µs) | +1.2 … −0.4 mV (initial condition: sensor nodes start at VMID) |
| 1 ms | −3.9 … −4.1 mV |
| 5 ms | −4.4 mV |
| 10 ms | −4.3 mV |
| 30 ms | −4.2 mV |
| last cycle (60 ms) | −4.35 mV |
| mean of the last 100 cycles | −4.36 mV, cycle-to-cycle std 0.01 mV |

The offset is the periodic steady state of the mux leakage (1 nA S→D off-leakage on each off channel, 1 nA at D) against the 470 kΩ bias that only
reconnects each sensor for 7 µs per 133 µs cycle; the sensor node settles with τ ≈ R13/duty × Cp ≈ 0.5 ms.
Solver wobble of ±0.15–0.3 mV appears at 15–35 ms (see `results/baseline/baseline.png`).

### Baseline comparison: VMID settle 5 µs vs 1 ms

Same circuit, same leakage; only `t_vmid_settle_us` differs (cycle 133 µs vs ~7.1 ms). Baseline of `AIN_B4`, mV relative to VMID:

| Time | VMID 5 µs | VMID 1 ms (addr 0 … addr 6) |
| --- | --- | --- |
| first cycle | +1.2 … −0.4 | +1.2 … −84.7 |
| 7 ms (cycle 1) | −4.0 (already settled) | −98.8 … −158.6 |
| 28 ms (cycle 4) | −4.35 | −251.2 … −271.5 |
| 57 ms (cycle 8) | −4.35 | −311.2 … −315.9 |
| 92 ms (last cycle) | −4.35 (all addresses identical) | −326.8 … −327.5 |

- With the 1 ms VMID settle the baseline is **~75× larger** (≈ −327 mV vs −4.35 mV) and has not fully converged at 92 ms: the per-cycle step shrinks by about ×0.65 each cycle
  (−325.4 → −326.8 mV over the last two cycles), so the asymptote is about −330 mV, reached with τ ≈ 2–3 cycles ≈ 16 ms.
- Mechanism (back-of-envelope estimate of the same order as the simulation, not separately simulated): the off-state leakage (1 nA on a 61 pF node = 16 mV/ms) discharges each floating sensor for ~7 ms between visits, i.e. ~115 mV per cycle. A 7 µs connection recovers only about a third of
  the deficit (charge sharing with the ≈ 12 pF D node restores ≈ 15 %, 7 µs of the 470 kΩ bias with τ ≈ 34 µs another ≈ 19 % of the remainder), so the equilibrium deficit is ≈ 115 mV / 0.31 ≈ 370 mV (simulated ≈ 330 mV).
  With a 133 µs cycle the same leakage costs only ~2 mV per cycle, giving ≈ −4.4 mV.
- Early on the channels fan out (addr 0 … addr 6 differ by up to 86 mV in the first cycle) because the sensors are read at different times after the common start, so later addresses have leaked longer; this
  fan-out shrinks to ~0.7 mV by the last cycle as the channels converge. With 5 µs there is no visible fan-out (≈ 0.01 mV).
- The size of the effect scales with the assumed leakage (1 nA, same sign on every channel, 25 °C typical) and inversely with Cp; real parts differ by channel and rise steeply with temperature (±25 nA max at 85 °C),
  so treat −330 mV as the model's worst case for equal-sign leakage, not as a prediction for the hardware.
- VMID-slot samples stay at +0.1 mV in both runs, so the VMID visit itself resets the D node either way; the difference is entirely the sensors' own leakage between visits.

**Why 60 ms instead of 3 s.** Every time constant in the circuit is ≤ ~1 ms (the slowest is the sensor node recharging through R13, ~0.5 ms; the
10 GΩ insulation leak is negligible against it), so the baseline at 3 s equals the 5–60 ms steady state above. A brute-force 3 s run at the full
5 ns resolution is ~6·10⁸ time points (~10 GB, hours). A cheaper 3 s run with a 1 µs step cap was tried and rejected: the 2 ns charge-injection
edges are not resolved, and the sampled baseline jumped by ±5 mV between cycles (−9.4, −3.8, −6.6, −4.5 mV at 10 ms, 0.1 s, 1 s, 3 s) with the
value changing when the step cap changed. Leakage sign and size also matter: the leakage is applied with the same sign on all channels, which is
the worst case; real parts have random ±1 nA values, which would give channel-to-channel baseline differences.

## Caveats – read before using the numbers

- **Placeholder parameters (not verified):** `t_vmid_park_us`, `r_insulation_ohm` and the layout parasitics are
  assumed. Edit `config.json` before drawing hardware conclusions.
- The ADC124S101 input (sample-and-hold capacitor, kickback during tracking) is not modelled; `AIN_B4` is the open node behind R21/C29.
- The TMUX1308A injection-current-control clamp (pins above VDD or below GND shunted to ground, §8.3.6) is not modelled. Keep
  sensor voltages within 0..3.3 V (the default forces stay within about 1.4–2.4 V).
- tan δ is **not** applied by default (`loss_model = "none"`): modelled as a parallel resistor it would be a ~100 MΩ DC leak that dominates multi-ms baselines. At the µs scale of the signals the difference is small. `"parallel_r"` restores it.
- The op-amp model has no rail wind-up recovery, noise, or PSRR; the supply is ideal and VMID is an ideal source.
- Force is assumed to couple fully into the PZT; no mechanical dynamics.
- Sensor address numbering is the MUX address (0..6), not the pin label; S0..S6 = O6_P1B, O6_P2B, O6_P3B, O6_P4B,
  O6_P5B, O7_P4B, O7_P5B.
