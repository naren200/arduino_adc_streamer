# TestBoard_7953 capacitance test: hardware verification checklist

These checks need equipment the coding session did not have (oscilloscope, LCR meter, multimeter, temperature control). Nothing here has been done. Tick each item and note the result next to it. Background: [`TESTBOARD_7953_CAPTEST.md`](../architecture/firmware/TESTBOARD_7953_CAPTEST.md).

## 1. Same-channel behaviour of the mux (datasheet unknown)

The datasheet does not say whether commanding the same channel in consecutive frames keeps the mux closed, or what the mux does while CS is high. The measurement assumes it holds.

Guided version: run `python tools/scope_test_7953.py --port COM3` from `CapTest/`. It walks through the probe and scope setup and fires each burst when the scope is armed.

- [ ] Scope on AIN (ADC input) or MXO with a low-capacitance probe, one ADC, a sensor (or a known capacitor) attached to the channel. Trigger on CS. Run `capburst 2 0 200 200 200`. Confirm there is no step or glitch at each CS edge during the burst, only the smooth exponential.
- [ ] Repeat with CS-high gaps of 200 ns and 2000 ns (`gap_ns`); the waveform must not change.
- [ ] Probe loading changes `C_total` by its own capacitance (a 10 pF probe adds 10 pF): note the probe and repeat the capacitance measurement with and without the probe to quantify.
- [ ] If a once-per-frame step exists: its size and sign give the per-frame error; the PC analysis cannot see it (every sample has the same phase) and flags only narrow residual lines at 2..6.5 frames.

## 2. Real frame timing and first-clock delay

- [ ] Scope on CS and SCLK of one bus: confirm the frame period is about 1.373 us (the firmware's cycle counter says 824 cycles at 600 MHz) and stable, the CS-high time is at least 200 ns, 16 clocks at 20 MHz.
- [ ] Measure the delay from CS falling to the first SCLK rising edge. The application assumes 25 ns (setting "First-clock delay"); enter the measured value.
- [ ] Measure the acquisition time (14th rising SCLK edge to the next CS fall); it must be at least 325 ns (expected about 2.5 x 50 ns + 200 ns + overhead).
- [ ] Verify CS and SCLK of the second bus are idle (CS high) during a burst on the first.

## 3. R_bias values (maps 1:1 to C_total)

- [x] Bias resistors measured in circuit with an ohmmeter, board off (2026-10-23, user): ADC1 0.999 M, ADC2 469.4 k, ADC3 469.4 k, ADC4 248.1 k (nominal 1 M / 470 k / 470 k / 249 k). In-circuit readings include the parallel paths, so they confirm only that no ADC has a resistor off by more than about 1 %: the R_bias table keeps the nominal values.
- [ ] Because the time constants do not scale with R_bias (see RESULTS.md), find what else sets them: check the schematic for a second resistance between the sensor pad and Vmid or the mux (series or bias resistors per channel, flex cable), and fit a known capacitor (for example 33 pF C0G in place of a sensor, or soldered to an unused channel) with the single-exponential model: it should give `tau = R_bias * C`.

## 4. Capacitance cross-check with an LCR meter

- [ ] Disconnect a sensor (unplug the strip) and measure it with an LCR meter at 1 kHz: C_PZT. Plug it in, run `captest` on that channel after power-up and compare `C_PZT` (and `C_total - C_D`).
- [ ] Measure the capacitance at the mux input pin of an **empty** channel (strips unplugged) with the LCR meter and compare with the application's `C_total` for that channel (the first board run measured 4-13 pF, which looks like an unpopulated board: confirm whether the strips were attached).
- [ ] Open-channel cross-check: for an empty channel `tau / R_bias` is the pin + trace + shared-node capacitance; its difference between two empty channels of one ADC is the trace capacitance difference.
- [ ] Compare ADC2/ADC3 (both 470 k): they measured C_total about 6.3 pF versus 8.8 pF on the empty board; find the cause (trace length, resistor tolerance, layout).

## 5. Sensor start voltage, charge injection and discharge

- [ ] Power-cycle the board (not just reset the Teensy), connect the strips and run the automatic mode once with M = 1: the first burst per channel has `V_s0 = 0` for sure.
- [ ] Probe the sensor node (AIN of an unused channel is not accessible; use the sensor pad) to confirm it sits at 0 V after the discharge wait and what voltage it ends at after a burst.
- [ ] Run the discharge check on three channels with sensors attached; the shortest sufficient wait gives the discharge time (the board measured about 30-50 ms time constant on empty channels).
- [ ] Charge injection: with a scope on MXO, trigger on the Vmid-to-sensor switch-over and look for a pedestal below Vmid after parking; compare the `V_D` reading with the DC Vmid (multimeter on the Vmid net).

## 6. Temperature

- [ ] Repeat the automatic run at two temperatures (for example 20 C and 40 C, board stabilised for 30 min). Pin leakage and the sensor leakage grow with temperature: compare the implied leakage current, `tau` and `V0`, and shorten the discharge time check accordingly.

## 7. Other notes to confirm

- [ ] The datasheet text for Figure 51 says the input is sampled on the CS rising edge while Figure 1 and section 8.3 say CS falling; confirm with a scope on AIN which edge freezes the value (this work assumes falling, as the existing timing report does).
- [ ] Confirm that `capabort` or the end of a burst leaves each converter on channel 15: VMID read on all four ADCs after a run should show tag 15 and about 1.25 V.
