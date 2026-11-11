# TestBoard_ADC124 capacitance test: verification checklist (needs your equipment)

Everything below could not be checked from the PC and the Teensy alone. Each item says what to measure, which assumption it settles and where to enter the result (the setting is in the CapTest Settings tab of the ADC124 board). Software-only checks that were done are in [`RESULTS.md`](../testing/results/testboard-adc124/capacitance-test/RESULTS.md); the assumptions are listed in [`TESTBOARD_ADC124_CAPTEST.md`](../architecture/firmware/TESTBOARD_ADC124_CAPTEST.md).

## Oscilloscope on a mux-output node / ADC input (AIN1..4)

Guided version: run `python tools/scope_test_adc124.py --port COM4` from `CapTest/`. It sizes the scope settings from a dry-run burst, fires each burst when the scope is armed (trigger on the EN disable edge), prints what the ADC saw and the levels and times the scope should show, asks for your cursor readings and prints the differences, including the implied `enable_delay_ns`. `--channel 1:1:0` probes a sensor on array 1 (about 50 mV step); the default `2:1:0` is an open input of array 2 (about 1 V step).

- [ ] **Real connection edge.** Trigger on EN or the address pin; compare the rise of the node with the firmware's `enable edge` (`switch_end - 200 ns`) plus the assumed mux turn-on delay (25 ns). Settles *Mux turn-on delay* (`enable_delay_ns`). The firmware's switch took 611.7 ns with no measurable jitter (200 bursts); the open window (disable to enable) is about 410 ns: confirm it on the pins.
- [ ] **Charge injection** on the D node when EN / the address change (QSPICE predicted about 1.606 V instead of 1.65 V after the break-before-make). It lowers `V_D`, so it moves `C_D / C_total` (a few percent), not `tau`.
- [ ] **ADC input kickback** at the frame rate (a ripple at 1 / 1.04 us) and the R21/C29 filter (about 15 ns): negligible against tau of tens of us, but the `PERIODIC_RESIDUAL` flag relies on it staying small.

## Logic analyzer on CS, SCLK, MOSI, MISO, EN, A0..A2 (both arrays)

- [ ] **Frame structure.** One CS fall for the whole burst, 16 clocks per frame, a 37 ns pause between frames at 16 MHz (the firmware measures 1037 ns per frame; 2071 ns at 8 MHz). Confirms *continuous CS* and the stamp-based time model.
- [ ] **ADC hold instant.** Where the ADC124S101 really samples relative to the frame start (assumed 3 SCLK periods; `hold_clocks`). A step on AIN from a function generator, with the sample code compared against the frame time, shows it. An error of one clock (62 ns) is about 0.1 % of tau.
- [ ] **Pop latency.** The frame-stamp latency (assumed 100 ns) is the delay between the end of a frame and the firmware reading its result; compare a GPIO toggle in the firmware with SCLK if you need better than 0.2 %.
- [ ] **Channel identity.** The ADC returns no channel tag, so the MUX1..4 -> IN1..4 wiring (`mux_to_adc_input`) and the one-frame selector latency are assumptions. Apply a DC level (or a function generator) to one mux input at a known address and see which IN code moves, or check the traces. Wrong wiring would swap `R_bias` and `C_D` between muxes.
- [ ] **EN polarity** (array 1 TMUX1108 active high, array 2 TMUX1308A active low) and that the bank is really parked on address 7 (VMID) between bursts.

## LCR meter, multimeter, temperature

- [ ] **LCR (1 kHz) on a disconnected sensor** and on an unpopulated input: compare with `C_total - C_D` and with the open-input `C_total` (array 2 reads about 17 pF with `C_D` about 8.9 pF: pin + trace about 8 pF). Settles `C_parasitic` and the absolute scale.
- [ ] **`R_bias` of all eight resistors with a meter, in circuit.** The routes JSON and the firmware table agree (array 1: 470 k, 1 M, 470 k, 1 M; array 2: 1 M, 470 k, 1 M, 470 k). The measured taus are consistent with it (470 k mux about 33 us, 1 M mux about 73 us on array 1; array 2 open inputs 8.1 / 17.8 us), but the tolerance maps 1:1 to `C_total`.
- [ ] **Two-resistor cross-check.** Two sensors of the same type on a 470 k and a 1 M mux (same address): `C = (tau_1M - tau_470k) / (1 M - 470 k)` gives `C` without trusting either resistor.
- [ ] **Temperature.** Repeat an automatic run at a second temperature (capacitance, leakage and `R_bias` drift).
- [ ] **Sensor start voltage.** The sensors on array 1 sit near VMID for tens of minutes after a connection and then drift upward by about 1 mV/s (see RESULTS); the `V_s0 = 0` assumption of the ratio cannot be met by waiting. Find a way to start sensors at a known voltage (short the strip to ground with the board off and probe with a high-impedance meter; check whether handling, temperature or the strip's pyro/piezoelectric charge explain the drift) and repeat the discharge check afterwards. Until then use `tau` / `C_total`, and read `C_D` from open inputs.
- [ ] **470 k versus 1 M mux on the same address**: the measured tau ratio on array 1 is 2.2 (expected 2.13); confirm with the resistor values above.
- [ ] **Unplug a strip** and measure the same channels: the difference is the sensor.

## Repeat

- [ ] Automatic run at **8 MHz and 16 MHz** on sensors with a fresh start voltage (software-only check on open inputs: tau 16.9..17.1 us at both clocks).
- [ ] Production firmware restored and verified: `mcu*` answers `# TestBoard_ADC124`, `status*` the production list.
