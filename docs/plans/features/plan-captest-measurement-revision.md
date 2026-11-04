# Plan: CapTest measurement revision

Status: PARTLY IMPLEMENTED, direction changed for TestBoard_7953 (see section 0).
Date: 2026-10-30, revised 2026-11-03; status table updated 2026-11-04
Scope: `CapTest/`, `Arduino_Sketches/TestBoard_7953_CapTest/`, `Arduino_Sketches/TestBoard_ADC124_CapTest/`. Production firmware stays untouched.

## 0. Status on 2026-11-04

**What changed.** TestBoard_7953 does not use the multi-gap method of sections 4.1-4.2 and 5 as its default. It uses **Vmid between samples**, the way production runs with Vmid between channels:

- CapTest firmware 1.3.0 adds `capalt`: one sensor frame, one Vmid frame, repeated.
- Every second cycle keeps the sensor connected with a long gap, so one burst holds two connected times.
- `capfit.analyze_alternate` fits the cycle-to-cycle approach to Vmid. `tau` gives `C_total`, and the effective connection capacitance `C_share` comes out of the same fit.
- It needs no second burst, no `C_f`, and no sensor start voltage. The sensor must start at least 0.3 V below `V_D`.

Method and board results: `docs/reports/TestBoard_7953_capacitance_report.md`, step 5 and "Switching to Vmid between samples".

**Board result (3 rested bursts per channel), against the 8-gap sweep:**

| Channel | Vmid between samples | 8-gap sweep |
| --- | --- | --- |
| 1:0 | 73.3 ± 0.1 pF | 71.4 pF (+2.7 %) |
| 2:5 | 73.4 ± 0.1 pF | 73.0 pF (+0.5 %) |
| 2:10 | 135.2 ± 0.4 pF | 134.4 pF (+0.6 %) |
| 2:7 | 257.5 ± 0.3 pF | 252.2 pF (+2.1 %) |

`C_share` at `V_D` is 6.5-6.8 pF on all four channels. No LCR reference exists yet.

| Plan item | Status |
| --- | --- |
| 4.1 Conditions and grouping fix | Not done. The 7953 default does not need it: one burst holds both connected times. Still needed before multi-gap runs or ADC124 conditions enter the app. |
| 4.2 Period model | Not in the app. Replaced on the 7953 by `analyze_alternate`. `tools/gap_sweep_7953.py` keeps the period model as the reference method. |
| 4.3 Validity of derived values | Partly done. Vmid between samples: `C_share` at `V_D`, an effective connection capacitance and not the node capacitance, has its own columns (`c_share_ratio`, `c_share_pf`); `ratio`, `C_D`, `C sensor` and `C_PZT` are left empty, and the GUI labels and help say so. A cycle whose closing Vmid reading is more than 5 mV from `V_D` is left out and flagged `VMID_RESET_FAILED`. `Vinf` is the fixed cycle of the fitted nonlinear model (`vinf_long_v` in the connect info for the level after a long cycle). Bursts of different methods are never averaged: one method is used and the channel is flagged `MIXED_METHODS`, and the CLI re-analysis gives one row per method. This separates methods only, not gaps or read orders (4.1 stays open for those). In that method `V0` is the first sample and the Vs0 check is skipped. New flag `ALT_SMALL_RANGE`: the sensor started less than 0.3 V below `V_D`, and the burst is excluded. The burst method on the 7953 no longer exports `V0`, ratio, `C_D`, `C sensor` and `C_PZT` (empty; the Vs0 check and `V0_INCONSISTENT` skip it), and carries `FRAME_DEPENDENT` and `c_total_method = tau_over_r`. `UNSTABLE_TAU` (sd over mean of `tau` above 5 % within one channel) is added for every board, and `VS0_HIGH` is two-sided. **Open:** `MODEL_MISFIT` and the `FRAME_DEPENDENT` test on measured period contrast (needs 4.2); the ADC124 still exports ratio, `C_D` and `C_PZT` from `V0`. |
| 4.4 Sensor history | Not done. It now matters for the default method, which needs the sensor at least 0.3 V below `V_D`. 2:7 needs about 3 s of rest, against the 300 ms default `discharge_ms`. Auto runs with M > 1 on large sensors may lose repeats to `ALT_SMALL_RANGE`. Freshness bug fixed: the connected channels are saved per boot id in `settings/<board>_session.json`; a board the app has no complete record of reports `fresh` unknown (empty), until the app sees it reboot. The `history` option, the rest check with 0.3-30 s waits and the rename of the GUI label are not done. |
| 4.5 Discard, window, GUI, CLI | `discard_us` and the adaptive window are not done. Vmid between samples sizes its own burst: at least 16 cycle pairs, capped by the 20 ms limit. GUI: Method, Vmid frames between samples and Long-cycle gap (automatic: 0.3 × `R_bias` × guessed C, 4-40 µs). CLI: `--method alternate|burst`. Raw files: the method is read from the packet flag, so old files re-analyse unchanged. No gaps setting or `1/tau` plot. **Added 2026-11-04:** an `ln(d)` tab (bottom tab bar, also saved with the plots as `ln_d.png`) for Vmid between samples: ln of the sensor's distance below `V_D` at the end of each cycle against the accumulated connected time, the fitted model run freely from the first cycle, and a dashed line of slope `-1/tau`. The burst plot's red line for that method stays a one-step prediction from the previous measured sample, and the help text says so. |
| Review fixes 2026-11-04 | Different methods are no longer averaged (`MIXED_METHODS`; see 4.3). Each cycle's closing Vmid reading is checked (`VMID_RESET_FAILED`). `Vinf` is the fixed cycle of the fitted nonlinear model, and is left empty, without a numpy overflow warning, when the fitted map has no stable settling level (noisy fits with a positive curvature). `C_share` has its own labels in the table, bar chart, burst title, CLI and help. |
| Vinf mode (not in the original plan) | Added 2026-11-04 for the TestBoard_7953, a process separate from the capacitance run (Manual tab button `Measure Vinf of selected`, CLI `--vinf --channels ...`, Vinf tab). Per channel and repeat three bursts: (1) a Vmid-between-samples burst, whose fit gives the **calculated** `Vinf`; (2) a long **alternating** burst, the sensor with Vmid, one connected time (`capalt` without a long gap), as production runs its channels, sized to settle to within 3 mV of `V_D` and then stay quiet for as long again (at most 2048 frames), whose **alternating `Vinf`** is the median of the sensor samples in its last quarter (`capfit.analyze_vinf_alternating`), the baseline production uses; (3) a **held** `capburst` for about 12 tau, with the median of its last quarter (`capfit.analyze_vinf`). No model is fitted for (2) and (3). The Vinf tab and the CSV (`write_vinf_csv`) give the three levels as millivolts from `V_D` (Vmid), their differences, the sd over repeats and the tail spread. Flags: `VINF_ALT_NOT_SETTLED` and `VINF_NOT_SETTLED` (the quarter before the tail differs by more than 3 LSB), and `VMID_RESET_FAILED` when a Vmid reading in the alternating tail is more than 5 mV from `V_D`. **Caveats:** the three levels come from different frame sequences (long and short cycles, equal short cycles, continuous connection) and the offset below `V_D` depends on the frame period, so differences are expected and are not an error of either; the alternating level matches production's baseline only for the same frame gap and number of Vmid frames. The bursts are not stored in the raw file; no board data has been taken with it yet. |
| 4.6 Simulators and tests | `sim.py` speaks `capalt`, long gap included, and its default firmware version is 1.3.0. Suite: 322 tests pass on 2026-11-04 (up from 247 at the start of the plan). **Open:** it has no frame-locked charge or voltage-dependent share, so the `c0 + c1·d` term is validated only on the board. `test_regression_7953.py` and the fit-model tests now pin `method="burst"`, rather than having their expected values updated. |
| 5 7953 default method | Vmid between samples (not multi-gap). "One connection per burst" stays selectable. It is labelled as a relative check (`c_total_method = tau_over_r`, `FRAME_DEPENDENT`) and its step values are empty. |
| 5 Limit check | Vmid between samples: the automatic frame count respects the 20 ms limit and the firmware rejects longer bursts. The burst method has no driver pre-check. |
| 5 Docs | `TESTBOARD_7953_CAPTEST.md` documents `capalt`. The fit-model, pin-leakage, 300 ms and re-command corrections are made, and the pre-sweep `RESULTS.md` values are marked superseded. |
| 5 Channels 1:7, 4:2 | Not inspected. |
| 6, 7.2 TestBoard_ADC124 | Not started. |
| 7.1 Firmware | Continue mode done (1.1.0, result below). Beyond the plan: `capalt` (1.2.0) and its long gap (1.3.0). Production sources untouched. |
| 8 Phase 2 acceptance | Range check met on the four channels above. The LCR comparison is still to do. |

**Next useful measurements:**

- Vinf mode on the four checked channels (1:0, 2:5, 2:10, 2:7): alternating, held and calculated `Vinf` against `V_D` and against each other. The alternating level is the one to compare with the production baseline. Note the measured level at one more frame period (a different `gap_ns`) to see the frame dependence directly.
- A known capacitor added to a channel. This checks the `C_total` scale and `R_bias` together.
- More gaps on 2:5. These test the model shape.
- The strip unplugged. This gives the sensor-side parasitics; `C_s` needs those and the node part `C_n`, which lies between 0 and `C_share`.

## 1. Why this plan exists

The 2026-10-30 gap sweep on TestBoard_7953 showed that the charge-up is not one RC: `tau` changes when only the frame period changes. Fitting the two paths together makes `C_total` agree across all three `R_bias` values, and shows that the current 7953 method reads 1.7-4x low.

The plan does five things:

1. Replaces the 7953 `C_total` method with a multi-period measurement.
2. Stops reporting 7953 `V0`, ratio and `C_D` until they have a valid basis.
3. Checks whether the ADC124 has the same frame effect on real sensors.
4. Tests whether the ADC124 read order affects the results.
5. Measures `C_D` directly on the ADC124, so `V_s0` no longer has to be assumed.

## 2. Current state

### 2.1 Algorithm

Both boards park the mux-output node at VMID, read a baseline `V_D`, connect a sensor and record the charge-up. The analysis in `captest/capfit.py` assumes:

```text
V(t)    = Vinf - (Vinf - V0) exp(-t / tau)
C_total = tau / R_bias
ratio   = C_D / C_total = (V0 - V_s0) / (V_D - V_s0)     (V_s0 defaults to 0 V)
C_D     = ratio x C_total
C_sensor = C_total - C_D
C_PZT   = C_sensor - C_parasitic                         (C_parasitic defaults to 5 pF)
leak_na = (V_D - Vinf) / R_bias
```

- **Fit models:** one or two exponentials. In `2exp`, `tau` is the slow time constant, with a fallback to `1exp` unless the second exponential is clearly needed. Defaults are `2exp` on the 7953 and `1exp` on the ADC124.
- **Discard:** the first `K = 2` samples per channel.
- **Burst sizing:** `c_guess_pf` (100 pF) only sizes automatic bursts.
- **Start-voltage check:** `start_voltage_check` estimates `V_s0` from `V0 x tau` across channels that share a node. It is a diagnostic only and is not fed back into the ratio.
- **Repeats:** M bursts per channel in pass-major order, separated by `discharge_ms`, then averaged.

### 2.2 Architecture

| Layer | Files | Role |
| --- | --- | --- |
| Board-neutral core | `capfit.py`, `runner.py`, `settings.py`, `client.py`, `link.py`, `cli.py`, `gui.py`, `flasher.py` | fit, sessions, exports, GUI, flashing |
| Registry | `boards.json`, `boards/<id>.json` | descriptors: channels, ranges, defaults, shown settings |
| Drivers | `captest/boards/testboard_7953.py`, `testboard_adc124.py`, `base.py` | planning, burst commands, decoding, time model |
| Simulators | `sim.py`, `boards/adc124_sim.py`, `simbase.py` | emulated devices that speak the real protocol |
| Firmware | one PlatformIO project per board | burst engine and packet |

| | TestBoard_7953 | TestBoard_ADC124 |
| --- | --- | --- |
| Mux | inside the ADS7953, set by the command word | external, GPIO, firmware-stamped switch (611.7 ns, no jitter) |
| Connection | one sensor | input X of all four muxes of the array |
| Frames | one per CS pulse, 1373 ns by default, `gap_ns` adjustable | continuous CS, 1037 ns at 16 MHz, 2071 ns at 8 MHz |
| Reads per burst | one channel | 1-4 muxes, round-robin, ascending IN order only |
| `R_bias` | 1 M, 470 k, 470 k, 249 k by ADC | array 1: 470 k, 1 M, 470 k, 1 M; array 2: 1 M, 470 k, 1 M, 470 k |
| Time zero | inferred from the frame timing | measured switch stamp plus 25 ns |

### 2.3 Testing methods

- **Offline:** 247 pytest cases collected in `CapTest/tests` (fit, protocol, registry, GUI, end-to-end on the simulators, 7953 regression).
- **QSPICE:** `tools/validate_qspice.py` and `validate_qspice_adc124.py`. They check the analysis against a circuit that holds the channel by construction, so they cannot detect a frame-locked effect in the real chip.
- **On the board:** CLI or GUI runs, the discharge check, and `tools/gap_sweep_7953.py` (not yet in the app).
- **Instrument checks:** the two verification checklists (scope, logic analyzer, LCR, resistor meter) are still open. No sensor capacitance has an LCR reference yet.

## 3. Evidence

"Measured" means taken from recorded data. "Inferred" means concluded from it but not directly observed.

### 3.1 TestBoard_7953 (measured, sweeps of 2026-10-30)

`tau` follows, with worst misfit under 1.2 % on large sensors and 2-7 % on small ones:

```text
1/tau = 1/(R_bias C_total) + (C_f / C_total) / T
```

| Sensor | ADC / R_bias | `C_total` from intercept | Default-gap `tau/R` | `C_f` |
| --- | --- | --- | --- | --- |
| Small | ADC1, 1 M | 72 pF | 18 pF | 4.0 pF |
| Small | ADC3, 470 k | 70-72 pF | 29-31 pF | 4.0 pF |
| Small | ADC4, 249 k | 72-78 pF | 40-44 pF | 4.4-4.5 pF |
| Large | ADC1, 1 M | 256 pF | 78 pF | 3.1 pF |
| Large | ADC2, 470 k | 255-263 pF | 122-127 pF | 3.1-3.2 pF |
| Large | ADC3, 470 k | 201-202 pF | 97-98 pF | 3.1-3.2 pF |
| Large | ADC4, 249 k | 212-232 pF | 134-148 pF | 3.2-3.3 pF |

- The large/small ratio of about 3 matches the sensor area ratio of 2.94 on ADC3 and ADC4.
- Four gaps give nearly the same `C_total` as eight. The longest single gap (35000 ns) alone still reads about 9 % low on ADC1:0.
- The `Vinf` offset below `V_D` shrinks as `T` grows (ADC1:0: 25 mV to 3 mV), so `leak_na` does not measure leakage on this board.
- The 1-exp residual falls as `T` grows (ADC1:0: 11 to 1.3 LSB). The two-exponential shape therefore largely tracks the frame effect, although long gaps also leave fewer samples on the curve.
- Large sensors read first/`V_D` of about 0.03 rested and about 0.75 only 500 ms after a burst. The 300 ms default does not discharge them.
- Suspect channels: 1:7 (unstable `tau`, 55.6 % misfit) and 4:2 (100 pF where a large sensor is expected).

**`C_f` depends on node voltage, not sensor size or `R_bias` (measured, default gap, rested first bursts, one burst per channel).** It falls from about 6.3 pF at 0-0.2 V to about 2.6 pF at 1.1-1.18 V, with small and large sensors matching at the same voltage. The 3.1 pF and 4-4.5 pF values in the table above are averages over different voltage ranges. This varying `C_f` is the likely source of the non-exponential shape. The transfer scales with sensor size only as a fraction, `C_f / C_total`.

**Mechanism, not established.** The data confirm a transfer once per frame, not its source. The ADC's sampling capacitor is unlikely: an op-amp buffer with 270 pF at its output (per the board designer) separates it from MXO, and the fitted target sits within tens of mV of Vmid instead of following the node. `C_f` is an equivalent value from the fit, not a measured capacitor. Internal mux re-selection or another path near Vmid remain possible.

### 3.2 TestBoard_ADC124 (measured, recorded files)

**Open inputs show no frame effect.** Recorded runs of 2026-10-23, array 2, mux 1, 1 M, `1exp`, K = 2. A 3-4 pF per-frame transfer at T = 1 us would be easy to see here.

| Channel | 1 input, 16 MHz (T 1037 ns) | 1 input, 8 MHz (T 2071 ns) | 4 inputs, 16 MHz (4150 ns per input) |
| --- | --- | --- | --- |
| 2:1:0 | 16.95 us | 16.95 us | 16.85-16.87 us |
| 2:1:1 | 17.06 us | 17.06 us | 16.94-16.97 us |
| 2:1:3 | 16.91 us | 16.90 us | 16.82-16.83 us |

**This has not been shown on real sensors.** No recording compares read patterns on the same sensors under controlled history. Array 1 runs at 16 MHz also repeat poorly (2026-10-23: `tau` sd 5-10 us, and 100 us on 1:3:1), while the same channels at 8 MHz had sd under 0.4 us. The cause is unknown.

**Charge retention (measured).** Between two runs 22.5 s apart, the step on array 1 changed by 2.02x and on array 2 by 0.998x. `Vs0_est` goes negative on array 1 and is not flagged.

### 3.3 Per-mux time axis (measured on `long_raw_bursts.json`, 4 inputs, 16 MHz)

| Mux position in the order | Input | First sample after connection | First sample used (K = 2) |
| --- | --- | --- | --- |
| 1st | IN1 | 3.79 us (its first sample, at -0.36 us, falls while the switch is open and is dropped) | 12.09 us |
| 2nd | IN2 | 0.68 us | 8.98 us |
| 3rd | IN3 | 1.71 us | 10.01 us |
| 4th | IN4 | 2.75 us | 11.05 us |

Those times depend on the assumed 3-clock hold instant.

## 4. Changes in the main CapTest app

### 4.1 Burst conditions (new core concept)

A connection can now be measured under several conditions, for example several gaps or read orders.

- Add `BoardDriver.conditions(settings) -> list`. The default is a single empty condition, so current behaviour is kept.
- `measure()` takes the condition. `Measurement` records it (`condition` plus the measured period).
- **Required fix:** `analyze_recorded`, `reanalyze`, `MeasurementSession._run` and `capfit.aggregate` currently average every burst of a channel. They must group by condition first, otherwise gaps get mixed into one meaningless mean.
- Runs stay pass-major, with the condition order shuffled per pass by a saved seed, as in `gap_sweep_7953.py`, so drift does not follow the condition.

### 4.2 Period model (new analysis)

Add `capfit.period_model(points, r_bias)`, moving the line fit out of the tool:

- **Inputs:** mean `tau` and `Vinf` per period.
- **Outputs:** `c_total_rc`, `c_frame`, `tau_misfit_pct`, `tau_ratio`, and the fit of `V_D - Vinf` against `1/T`, whose intercept gives `leak_rc_na` and whose slope gives `charge_per_frame_pc`.
- Requires at least 3 periods. With fewer it returns no `c_total_rc` and leaves `c_total` on `tau/R`.
- Sweep bursts use `1exp` with no discard, because that is what the validated sweep used.

`c_total` keeps its name, and a new `c_total_method` column (`rc_intercept` or `tau_over_r`) says what produced it. New CSV and JSON columns are additive.

### 4.3 Validity of derived values

- `ratio`, `C_D`, `C_sensor`, `C_PZT` and `V0` are exported as empty on a board or run marked "step not valid", rather than as numbers that look good.
- `leak_na` keeps its value on boards with a period model but is shown next to `leak_rc_na`.
- New flags:
  - `FRAME_DEPENDENT`: `tau_ratio` above 1.05 between the shortest and longest period.
  - `MODEL_MISFIT`: `tau_misfit_pct` above 10 %.
  - `UNSTABLE_TAU`: sd above 5 % within one condition. It would catch 1:7.
- `VS0_HIGH` becomes two-sided: `abs(value) > threshold` (`runner.py:111`). This changes results on existing data, because array 1 channels will gain the flag.

### 4.4 Sensor history

- **Discharge check:** extend the waits to 0.3, 1, 3, 10, 30 s, report first sample/`V_D` as well as `V0`, and start from a rested sensor.
- **Run option `history`:**
  - `primed` (default for sweeps): one excluded priming burst per channel, then a fixed interval.
  - `rested`: a long wait, for measurements that need a low start voltage.
- **Default `discharge_ms`:** do not change it until the check has run with sensors attached. Rename the GUI label to "wait between connections", since the wait does not ensure a discharge.
- **Freshness bug:** `connected_since_boot` lives only in the session (`runner.py:185`), so a second run on the same boot is wrongly marked `fresh`. Persist it per boot id in `settings/<board>_session.json`, and report `unknown` when the app has no record for that boot.

### 4.5 Other analysis and app changes

- **Time-based discard:** add `discard_us` alongside `discard_k`, because K = 2 means 2 us in one-input bursts and 8 us in four-input bursts. On array 2, MUX4 (median `tau` 8.7 us) the first used sample is already at about 1.3 `tau`. Default it to K-equivalent behaviour, and switch only after the re-analysis in phase 1 shows `tau` and `V0` change within noise on clean channels.
- **Adaptive window:** when a burst returns `SHORT_WINDOW`, size the next one at 6 x the fitted `tau`, up to the board limit. The 100 pF guess covers only 2.3 `tau` of a 260 pF sensor at 1 M.
- **GUI:**
  - Gaps setting.
  - Plot of `1/tau` against `1/T` for the selected channel, with the fit line.
  - New result columns and tooltips, plus Help text.
  - Read-order analysis view for the ADC124 (section 6.3).
- **CLI:** `--gaps`, `--orders`, `--history`. `tools/gap_sweep_7953.py` stays as a stand-alone diagnostic.
- **Raw files:** the condition comes from the packet itself, so existing raw files stay loadable and re-analysable.

### 4.6 Simulators and tests

- `sim.py`: add `c_frame_pf` with the frame-locked transfer and a `Vinf` offset; the default of 0 keeps existing tests valid.
- `adc124_sim.py`: support read order, the mux-off relaxation and the v2 packet.
- New tests:
  - The period model recovers the true `C_total` and `C_f` from the simulator.
  - Grouping by condition never mixes gaps, during either a run or `reanalyze`.
  - Stored 2026-10-30 sweep summaries are reproduced from their raw packets.
  - Old raw files still load.
  - Two-sided `VS0_HIGH`, and freshness across sessions.
- `test_regression_7953.py` and the "saved files byte-identical" rule in `settings.py` will conflict with the new defaults. Update those expected values in a separate commit that lists every changed value, and add the new fields to `BOARD_EXTRA_FIELDS` where appropriate.

## 5. Changes specific to TestBoard_7953

- **Default method (superseded 2026-11-03, see section 0):** Vmid between samples replaced this. Original proposal: `conditions()` returns the gaps of a new setting `gaps_ns`, default `200, 1000, 5000, 20000`, at 512 samples per burst. At least 3 repeats per gap.
- **Limit check:** the driver rejects combinations above the firmware's 20 ms critical section before sending them. At 512 samples this limits the gap to about 35000 ns.
- **Single-gap mode:** stays available as "fast relative check", with `c_total_method = tau_over_r` and `FRAME_DEPENDENT` set by default, so it cannot be mistaken for a true capacitance.
- **Default fit:** `1exp` when sweeping. `2exp` stays selectable for the fit-shape study only.
- **Step values:** `V0`, ratio, `C_D`, `C_sensor` and `C_PZT` are marked not valid. Their return depends on the mechanism test in section 7.1. The Vs0 check is also disabled, since it relies on the same `V0`.
- **Line detector:** keep the narrow 2-6.5 frame band as it is. It cannot see an effect that repeats identically every frame, and the docs must say so.
- **Docs:** correct the fit-model section, the "pin leakage" and "same channel re-commanded" assumptions, and the 300 ms discharge claim in `TESTBOARD_7953_CAPTEST.md`. Mark `RESULTS.md` values from before the sweep as superseded.
- **Channels to inspect:** 1:7 and 4:2, on the board, with a meter.

## 6. Changes specific to TestBoard_ADC124

### 6.1 Does the analysis account for each mux's delay?

**Yes.** All four muxes switch at one stamped instant, and `DriverADC124._analyze_channel` computes every sample's own time relative to it (`t = times[post_idx] - connect`, `testboard_adc124.py:348`). Each mux gets its own time axis and its own `V0` extrapolation to `t = 0`, so `tau` and `V0` are compensated for the 0.68-3.79 us differences in section 3.3.

Three limits remain:

- **The order in the list is not the order sampled.** IN1 is listed first but is sampled last, because its first frame lands inside the switch window.
- **The compensation is only as good as the model and constants.** Hold instant, pop latency and turn-on delay are assumptions. `V0` extrapolation also relies on the exponential holding back to `t = 0`. On the 2026-10-30 long run, moving from K = 2 to K = 0 changes the median `V0` by 0.3 mV or less on array 1, 3-7 mV on array 2 MUX1-3 and 25 mV on array 2 MUX4.
- **Timing is not the only thing the order changes.** Each input's sample also follows a different previous input, so any carry-over inside the ADC differs per mux.

### 6.2 Is it better to test with each mux first?

**Yes, as a diagnostic, not for every run.** Order does not change when a sensor starts charging, because all four connect together. It can only reveal errors in the time model or ADC carry-over. It is still worth doing, for a reason that goes beyond timing: with a fixed order, position is permanently tied to the mux, and because `R_bias` alternates per mux it is also tied to the resistor. A difference between muxes today cannot be attributed.

Rotation alone cannot test carry-over, because 1-2-3-4 and 2-3-4-1 keep the same previous input for every mux. The test therefore needs a reversed order as well.

### 6.3 Read-order test

- **Orders:** `1234`, `2341`, `3412`, `4123`, `4321`, plus each mux alone (`m = 1`) as the no-neighbour control.
- **History control:** one excluded priming burst per address, a fixed interval, 5 repeats, order shuffled per pass. Every repeat reconnects sensors that were recently charged, so without this control history would be mistaken for order.
- **Channels:** at least one address with sensors per array, and one open input as a reference.
- **Report per mux:** `tau`, `V0`, `V_D` and residual against order, with first-sample time as the x axis.
- **Decision rule:**
  - `tau` within 1 % and `V0` within max(3 LSB, 3 x repeat sd) across all orders: keep the fixed order, and record the check in `RESULTS.md`.
  - Differences that follow first-sample time: the time model is wrong. Fix the constants and re-test.
  - Differences that follow the previous input (rotations agree, reverse differs): ADC carry-over. Add idle frames between inputs or use `m = 1` for `V0`.

### 6.4 Frame effect on sensors

Run `m = 1` and `m = 4` at 8 and 16 MHz on sensor channels with the same history control. Run this first, because it needs no firmware change.

- **`tau` within 2 %:** the ADC124 is confirmed as plain RC, and `tau/R` stays its method.
- **`tau` depends on the period:** enable the period model of section 4.2 with clock and `m` as conditions. Only per-input periods of 1037, 2071, 4150 and 8283 ns are available, so the extrapolation will be weaker than on the 7953.

This should also explain the poor 16 MHz repeatability in section 3.2.

### 6.5 Measuring `C_D` and `V_s0`

Using the new mux-off relaxation (section 7.2):

```text
C_D  = tau_D / R_bias                      tau_D from the relaxation, expected 4-9 us
r    = C_D / C_total
V_s0 = (V0 - r V_D) / (1 - r)
```

- The result is a measured start voltage per sensor, which replaces both the `V_s0 = 0` assumption and the statistical Vs0 check on this board.
- Use `m = 1` for this pass: at 1037 ns the relaxation gets about 4-9 samples per `tau`. At `m = 4` it gets too few.
- **Validation:** open inputs should give about 8.9 pF, the earlier estimate from the step, and `tau_D` should be the same at 8 and 16 MHz.
- This assumes `R_bias` stays connected to the D node with the mux disabled. Confirm that against the schematic before implementing.

### 6.6 Other ADC124 items

- Mark ratio, `C_D` and derived values not valid on any array with `V_s0` above the threshold until 6.5 is in place.
- Keep one connection per address as the default. It reads all four sensors of an address from a single connection and avoids the charge retained by a previous read at that address.
- Update `TESTBOARD_ADC124_CAPTEST.md` and its guide (read order, position table, relaxation, packet v2).

## 7. Firmware changes

Production projects and hashes must stay unchanged (`git diff --stat` empty, same production `firmware.hex` SHA-256). CapTest firmware continues to reuse production sources read-only.

### 7.1 TestBoard_7953_CapTest: continue-mode burst

No change is needed for the multi-gap method: the existing `gap_ns` already covers it.

One addition directly tests whether re-commanding the channel causes the frame effect. SLAS605C, section 8.4.7, Table 7: with `DI15-12 = 0000` the device "continues to operate in the selected mode", and "in the Manual mode it continues with the last selected channel".

- **Command:** optional last token on `capburst`, `hold_mode` 0 or 1 (default 0).
- **Behaviour with 1:** frame `k` still sends the manual command for channel X. Later sample frames send `0x0000` instead of `sample_command` (`BurstEngine.cpp:117`, `:144-145`). The two release frames remain manual Vmid commands.
- **Packet:** flags bit 4 = continue mode. Same version, so old raw files are unaffected.
- **Tags:** the existing tag check confirms the channel stayed on X.

| Result, same gaps with `hold_mode` 0 and 1 | Conclusion |
| --- | --- |
| `C_f` drops clearly | re-commanding drives it; use continue mode, then re-measure `C_D` on the 7953 |
| `C_f` unchanged | not the command word. Possibly a re-select that happens anyway, or another path near Vmid. Next checks: same `T` at two SPI clocks, and a scope on the buffer output |

**Result (2026-10-30, firmware 1.1.0, 15 channels, 4 gaps, 5 repeats):** implemented and tested; `C_f` is unchanged (`C_total` within 0.4 %, `C_f` within 1.2 %, all channels). Second row of the table applies. Data: `CapTest/Results/TestBoard_7953/2026-10-30_continue_mode`.

**SPI clock check (2026-10-30, 20 and 10 MHz at matched `T` of 2.5, 5, 10 and 20 us, 15 channels, 5 repeats):** `C_f` is unchanged (10/20 MHz ratio: median 0.999, range 0.982-1.007), and so is `C_total` (median 1.000, range 0.993-1.001). The path does not conduct in proportion to the clocked time or the CS-high time; it behaves as a fixed amount per frame. Tool: `CapTest/tools/spi_clock_7953.py`. Data: `CapTest/Results/TestBoard_7953/2026-10-30_spi_clock`.

**Scope findings below are from 2026-11-02 and partly superseded.** The report holds the current state:

- The mux selects 172-180 ns after CS falls (CS and the node captured together on 3 November).
- The negative levels came from the scope, not the node. The overdrive explanation was withdrawn; a ground offset between probe and board during bursts is suspected but untested.
- The per-frame `C_f` matches the extra-frame charge of `capalt` (4.05 pF on 2:5).

**Scope check:** `CapTest/tools/scope_test_7953.py` guides the setup and fires each shot once the scope is armed. Run on 2026-11-02 at the OPA365 output and at MXO, with data in `CapTest/Results/TestBoard_7953/2026-11-02_*`. Values are cursor readings unless marked as approximate.

Channel 1:0 (small sensor):

- Once per frame the node rises in a flat-topped pulse, drops part of the way, and stays higher. The net jump is about 110 mV at 20 and 10 MHz and at gaps of 200 and 5000 ns.
- The pulse is 165 ns wide at 20 MHz and 270 ns at 10 MHz, and is the same distance before the next frame's connection at both gaps (283 ns at 10 MHz). It is therefore locked to the start of the frame, not to CS rising. CS-high time is 345 ns at gap 200 ns.
- The pulse ends 13 ns before the point where the connection falls, at both clocks. This suggests the mux selection on the 2nd SCLK falling edge, and a first-clock delay of at least about 105 ns at 20 MHz instead of the assumed 25 ns. Neither is confirmed, because CS and the pulse were never captured together.
- The pulse and the jump are also on MXO (179 mV before, 287 mV top, 248 mV settled, probed), so they start upstream of the buffer and are not ADC kick through the 270 pF.
- The node falls to about -156 mV at the connection (+66 mV with a probe on MXO, which fits about 14 pF of probe capacitance). The user saw no negative voltage at the sensor pads, so the negative level is created at the connection.
- The ADC reads near or above the pulse top, not the settled level, about 100 mV high at the bottom of the curve. The first sample therefore hides the negative start, and the effect on `tau` and `C_total` is not yet known.

**Channel changes since 2026-10-30:** repeating the 15-channel first bursts of `2026-10-30_gap_sweep_8gaps` shows 11 channels within about 2 % over their first four samples. 1:2 (37 to 86 mV first sample), 3:2 (74 to 201 mV) and 4:12 (65 to 133 mV) changed, and 4:2 moved about 5 %. Rest (600 s), the probe and burst order do not explain 1:2. Tails also differ: 1:2 and 4:12 are slower at frame 300. The cause is not known. The 2026-11-02 large-sensor shots on 1:2 therefore describe that channel in its present state, not a healthy large sensor, and do not contradict the `C_f` curve of section 3.1.

**ADC1 (1 M) against ADC2 (470 k), 8-gap sweep of 2026-11-02, all routed channels (`2026-11-02_gap_sweep_adc12`):** the user considers the ADC3 and ADC4 sensors unreliable, so conclusions use ADC1 and ADC2. The `1/tau` line fits within about 2.2 % on all but 1:2 (10.6 %) and 2:1 (7.9 %), which are excluded. Classes by fitted `C_total`: about 71 pF on 1 M (`C_f` 4.0 pF) and 73-78 pF on 470 k (`C_f` 4.04-4.08 pF), about 96 pF (`C_f` 3.7-3.8 pF), about 137 pF (`C_f` 3.3-3.7 pF) and about 255 pF (`C_f` 3.15 pF). These classes are not sensor sizes. The user states that channels 2, 7 and 12 hold 1.2 x 1.2 mm sensors and all others 0.7 x 0.7 mm, an area ratio of about 3. Large channels on ADC2 fit at 252-261 pF, which is 3.2-3.8x the 69-78 pF small channels and fits that ratio with little capacitance besides the sensor. Same-size small channels at 133-142 pF (about twice as much) and large channel 1:7 at 97 pF do not fit it, and their cause is unknown. A baseline with the strip unplugged has not been measured. The per-frame term stays the same when R_bias halves, between same-size channels at similar `C_total` (2:4, 2:5 against the 71 pF ADC1 channels). ADC1 and ADC2 channels fitted on 2026-10-30 repeat within about 1.3 %, except 1:2 and 1:7. `C_total = tau / R_bias` at the default gap reads about 18 pF for the 71 pF class on 1 M, and 31-33, 61-64 and 121-125 pF for the 75, 137 and 255 pF classes on 470 k. No LCR check has been made.

**Scope on ADC2, 470 k (`2026-11-02_scope_adc2`):** net jump about 115-123 mV on 2:5 (about 75 pF) (photo reading, about 75 pF) and about 25-34 mV on 2:7 (cursors on a repeat shot, about 252 pF), so the jump shrinks about 3.5-4.8x against a `C_total` ratio of about 3.4, for about 9 pC per frame on 2:5 and about 6-9 pC on 2:7. ADC steps match the frame model within about 7 % on both channels. The level after the fall is -143 mV on 2:5 and -236 mV on 2:7 (cursors), which argues against a fixed injected charge. Open: on 2:7 the scope's top-to-top rise (about 118 mV, cursors) exceeds the ADC step of 87 mV from the earlier shot, and the ADC reads about 70-100 mV above the pulse top. The repeat shot, at 10 s rest, read the same ADC samples (93, 181, 260 mV), so this comparison is within one burst.

**Rest time on 2:7 (`2026-11-02_rest_time_2_7`, gap 5000 ns, one burst per rest):** first sample 770 mV at 0.5 s, 640 mV at 1 s, 214 mV at 2 s, 95 mV at 3 s, and 93 mV from 5 s to 40 s. The curve is not a single exponential and stops at a fixed floor. Other channels have not been measured.

**Negative levels are in doubt:** the user confirms the OPA365 V- pin is at ground with no negative rail on the board, so its output cannot reach the -143 to -248 mV the scope showed at it. All negative levels, and the absolute scope levels and slopes in the first frames, should be treated as unconfirmed. The suspected cause is scope or probe recovery after the 1.25 V fall, because the parked level was off screen in those captures. The ADC samples are not affected. First test (`2026-11-02_scope_adc2_d`, gap 5000 ns): with the parked level on screen, 2:5 agrees with the ADC (first pulse top 297 mV against 292 mV by cursor, about 530 against 511 mV by photo). On 2:7, with the parked level about 130 mV off screen, the scope still reads low by about 113 mV at the first top (-21 against 93 mV), about 74 mV at the second, and about 0 by the sixth or seventh frame. The fall still appears negative. A 2:7 shot with nothing off screen is still needed.

If `C_f` stays, the 7953 step values stay invalid until a separate design handles them. That design is not part of this plan.

### 7.2 TestBoard_ADC124_CapTest: protocol version 2

Both additions extend `capburst` and use one new packet version:

```text
capburst <array> <address> <input_mask> <samples|auto> <park_us> <pre_samples>
         [<spiclock_hz> [<order> [<release_frames>]]]*
```

- **`order`:** digits of the conversion order, for example `2341`; `0` means ascending. It must be a permutation of the mask, otherwise `#NOT_OK`. `commands[]` is filled from it (`CapBurstEngine.cpp:41-44`).
- **`release_frames`:** default 0. After the post-connection frames, the loop calls `bank.disable()` with interrupts off and stamps it, as it does for the switch. It then keeps converting this many frames, and finally re-parks on VMID. The frame total still has to fit in 4096. `disable()` is public, but its in-burst timing and the re-enable through `switchAddress()` still have to be checked on the board.
- **Packet:** version 2, header 80 bytes, with the existing `header length` byte giving the size. Added fields: order (4 x u8), `release_frames` (u16), 2 reserved bytes, `release_begin_cycles`, `release_end_cycles` (u32 each).
- **Decoder:** accepts versions 1 and 2, and takes the length from the header. `inputs()` returns the recorded order, which `_analyze_channel` already uses through `slots.index`.
- **Compatibility:** `capstatus` reports version 1.1.0 and protocol 2. The app's identify check requires protocol 2 for order and relaxation tests, and otherwise offers a flash, as it does today.

No other firmware changes are planned. In particular, keep the frame loop's interrupt behaviour and the switch routine unchanged.

## 8. Phases and acceptance

Work on a feature branch. Each phase ends with passing `python -m pytest tests` in `CapTest/`.

| Phase | Work | Firmware | Accept when |
| --- | --- | --- | --- |
| 0 | ADC124 frame test (6.4); discharge check with long waits on both boards, via tools | none | results saved under `CapTest/Results/`; `R_bias` of 2-3 channels metered; one small and one large sensor measured with an LCR meter |
| 1 | Core: conditions, grouping fix, period model, flags, history, freshness, sim, tests; re-analysis of stored data | none | the period model reproduces the sweep's `C_total` from raw packets within 1 pF; old raw files load; simulator recovers true values within 2 % |
| 2 | 7953 multi-gap as default, GUI and CLI, step values marked invalid, docs | none | on the board, small sensors 70-78 pF and large 200-263 pF again; result within 15 % of LCR, otherwise stop and investigate |
| 3 | 7953 continue-mode test | 7953 | production hash unchanged; 5 repeats at the 4 gaps; conclusion recorded per table 7.1 |
| 4 | ADC124 protocol v2, order test | ADC124 | production hash unchanged; v1 raw files decode; test run; decision per 6.3 recorded |
| 5 | ADC124 relaxation and measured `V_s0` | uses phase 4 | open inputs give `C_D` near 8.9 pF; `tau_D` within 5 % between 8 and 16 MHz; rested and just-used sensors give the same `C_D` |
| 6 | User guides, Help text, checklists, `RESULTS.md`, `CapTest/README.md`, plan status in `docs/plans/README.md` | none | docs match the shipped defaults |

The 15 % LCR and other thresholds here are starting values. An LCR reads at a fixed test frequency and PZT capacitance can vary with frequency, so a persistent offset should be investigated, not simply tuned away.

## 9. Not verified and out of scope

**Not verified:**

- The mechanism of the 7953 frame effect.
- Absolute `C_total` (no LCR reference).
- The ADC124 frame behaviour with sensors attached.
- ADC124 hold instant, turn-on delay, and MUX-to-IN wiring.
- The cause of the remaining non-exponential shape on the 7953.
- The physical source of the `capalt` connection factor and of its voltage dependence, and the node capacitance `C_n` within `C_share`.
- Whether ADC124 and 7953 sensor values can be compared, since the sensor types on each board have not been confirmed the same.

**Out of scope:**

- Production firmware and the main GUI.
- QSPICE changes. The existing models hold the channel by construction, so they would need a chosen mechanism first.
- Using `C_PZT` for any calculation outside CapTest until this plan's phases 2 and 5 are complete.
