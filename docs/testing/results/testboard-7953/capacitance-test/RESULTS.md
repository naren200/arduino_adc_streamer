# TestBoard_7953 capacitance test: verification results

Run 2026-10-23, branch `feature/testboard7953-captest`, firmware git `66d5c95` (+ the uncommitted new project), host Windows 11, PlatformIO 6.2.0, `teensy@6.0.0`, Python 3.11, PyQt6 6.10, numpy 2.3 (no scipy). Design and protocol: [`TESTBOARD_7953_CAPTEST.md`](../../../../architecture/firmware/TESTBOARD_7953_CAPTEST.md).

## Builds

| Build | FLASH code | RAM1 variables | RAM2 variables | `firmware.hex` SHA-256 |
| --- | ---: | ---: | ---: | --- |
| Production `pio run -d Arduino_Sketches/TestBoard_7953` | 36,208 B | 17,664 B | 12,832 B | `844d87fb8997dcb381e8d8a162bc6d39af667aa6cf641dfcba3b8135a0036dbc` before and after (also after `-t clean` and rebuild) |
| Test `pio run -d Arduino_Sketches/TestBoard_7953_CapTest` | 27,504 B | 33,376 B | 12,544 B | `9800af2da5c12f8ed1a4f730955e764b0b9e43afd650d2759ae2f6ca8e293ada` |

`git diff --stat -- Arduino_Sketches/TestBoard_7953` is empty. The test firmware's RAM1 is larger by the burst buffers (words, timestamps, 12.4 KB packet buffer: about 24 KB for the maximum N = 2048).

## Offline tests (no board)

`python -m pytest tests` in `CapTest` (then still in `Arduino_Sketches/TestBoard_7953_CapTest`, before the PC application moved): **83 passed** (82 offline; the QSPICE check is the 83rd and runs only where QSPICE is installed), covering:

- `capfit`: noiseless `tau`, `V0`, `Vinf` within 1 %; with 1 LSB rms noise `C_total` within 2 % and `C_D/C_total` within 0.01 absolute for `R_bias` in {249 k, 470 k, 1 M} and `C_total` in {60, 80, 100} pF (9 combinations); numpy-only fit; non-uniform timestamps; timestamp wrap-around; tag mismatch discarded and flagged; missing samples; low amplitude; alternate-frame pattern flagged; slow structure not flagged; too-soon flags; aggregation; plateau test.
- Protocol: encode/decode round trip, documented example frame, CRC check value, CRC failure, truncated packets, bad magic, implausible lengths, `#NOT_OK` for six invalid commands, identification of capacitance-test / production / silent firmware.
- End-to-end on the simulated device, CLI and runner: automatic mode (all 50 channels in order, progress, accuracy against ground truth), subset of ADCs, Stop leaves the board parked (last command `capabort`), a failing channel is recorded and re-measured, discharge wait for M > 1, manual selection `2:0,2:5,4:1-3`, "too soon" warning (not blocking) and its physical effect on `V0`, boot-id reset restores the fresh flags, discharge check, raw save/reload with different `R_bias`, exports.
- Headless Qt (`QT_QPA_PLATFORM=offscreen`): connect and identify, flash buttons disabled in simulation, automatic run of one ADC, manual selection helpers and warning, boot-id reset, Stop, settings round trip, discharge check, exports and plot images, production-firmware message, flash buttons (confirmation text, streamed dry-run output).
- Flash scripts: `--dry-run` / `-DryRun` of the PowerShell and the bash scripts for both firmware (printed command, port, pinned platform), command construction on both platforms.
- Optional QSPICE validation (`tools/validate_qspice.py`, QSPICE64.exe present, ~6 s): a burst generated from `Qspice/TestBoard_7953` (one sensor connected for 400 frames, sensor start 0 V, pin leakage off, frame gap 200 ns) analysed by the same code:

| Cp set in model | extracted C_total | model node capacitance (sensor + 10 pF MXO + 14 x 0.1 pF + 2 pF) | extracted C_D | model C_D | ratio | model ratio |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 60 pF | 77.93 pF | 77.40 pF | 13.47 pF | 13.40 pF | 0.173 | 0.173 |
| 100 pF | 117.91 pF | 117.40 pF | 13.46 pF | 13.40 pF | 0.114 | 0.114 |

`Delta C_total` for `Delta Cp` = 40 pF: 39.99 pF. This checks the analysis (time base, `t_c`, fit, ratio) against a circuit model that holds the channel by construction; it does not test the unknown same-channel mux behaviour of the real device.

## On the board (TestBoard_7953 on COM3, user-confirmed)

The board was running the production firmware (found through `capstatus*` `#NOT_OK` and its normal `status*` reply). Sequence performed:

1. `scripts/flash_test.ps1 -Port COM3 -Yes`: built and uploaded, SHA-256 above. `capstatus*` answered `firmware=TestBoard_7953_CapTest`, version 1.0.0, git `66d5c95`, boot id `0xBC680E00`.
2. `capvmid` on all four ADCs: mean code 2047.0 / 2046.4 / 2046.2 / 2046.8 (1.2495 V), min 2046, max 2047, tag errors 0.
3. One burst per ADC (channel 0), then the **automatic mode** on ADC2 + ADC4 and on **all four ADCs with M = 3** (50 channels, 150 bursts), then the **discharge check** on 2:0 and 4:5, then the GUI class (headless Qt) on the real port. Raw bursts and results are saved in this folder (`first_look*`, `auto_adc2_adc4*`, `auto_all_m3*`, `discharge_2_0_raw.json`).
4. Command rejections on the board: channel 15, ADC1 channel 10, `gap_ns` 100 (period < 1.0 us), N = 5000, ADC 5, N = 7 all `#NOT_OK`; N = 2048 with park 10 ms accepted (12,390-byte packet decoded, CRC ok); `auto` gave N = 601; `spiclock 21000000` rejected; at 10 MHz the frame period was 2213 ns; `capabort` ok.
5. **Restore**: `scripts/flash_production.ps1 -Port COM3 -Yes` uploaded the production image (SHA-256 `844d87fb...`, identical to the baseline build); afterwards `capstatus*` returned `#NOT_OK` and `status*` the normal production reply (`# Current mode: PZT`, `# vmid_channel=15`, ...). The board was left on the **production firmware**.

### What the real data shows

- Returned channel tags are the commanded channel for every sample after the two lead frames (tag errors 0 in all 150 bursts). Vmid words have tag 15.
- Frame period **1373.3 ns** (824 cycles at 600 MHz), min = mean within 0.1 ns over a burst, max 1396.7 ns (one longer first frame); well above the 1.0 us limit; stable over N up to 2048. At 10 MHz 2213 ns.
- Residuals look random: rms 1.1-1.5 LSB, lag-1 autocorrelation about 0.7 (coloured, no line at 2..6.5 frames: 0 detections in 180 bursts; an injected alternate-frame pattern is detected in the unit test).
- `V0` versus wait (discharge check, 2:0): 0.852 / 0.718 / 0.488 / 0.460 V at 10 / 30 / 100 / 300 ms; (4:5): 0.903 / 0.762 / 0.473 / 0.458 V. The sensor-side node discharges with a time constant of roughly 30-50 ms; after 100 ms the ratio is still about 6 % high, after 300 ms it plateaus. Hence the 300 ms default discharge time.
- `Vinf` settles 30-140 nA x `R_bias` below Vmid (implied leakage current 49-102 nA ADC1-3, 97-141 nA ADC4), the order of magnitude expected from 16 pins at 3-7 nA each (datasheet Figure 43).
- Repeatability over M = 3 (std of `C_total`): at most 0.05 pF (ADC1), 0.12 (ADC2), 0.11 (ADC3), 0.19 pF (ADC4).

Per ADC (all routed channels, M = 3, strips as connected at the time of the run):

| ADC | R_bias | tau [us] | C_total [pF] | C_D / C_total | implied leakage [nA] | residual rms [LSB] |
| --- | ---: | --- | --- | --- | --- | --- |
| 1 | 1 M | 3.66 - 4.11 | 3.66 - 4.11 (median 3.97) | 0.327 - 0.366 | 51 - 76 | 1.34 - 1.46 |
| 2 | 470 k | 2.85 - 4.24 | 6.07 - 9.02 (median 6.29) | 0.294 - 0.426 | 57 - 102 | 1.16 - 1.50 |
| 3 | 470 k | 4.00 - 4.33 | 8.51 - 9.21 (median 8.83) | 0.424 - 0.477 | 49 - 93 | 1.10 - 1.43 |
| 4 | 249 k | 2.47 - 3.16 | 9.93 - 12.69 (median 10.16) | 0.316 - 0.422 | 97 - 141 | 1.09 - 1.16 |

### Interpretation (not established)

- `tau` does **not** scale with the nominal `R_bias` (ADC1 with the 4x larger resistor than ADC4 has the *larger* `tau` but a *smaller* `C_total`); the extracted `C_total` of every channel is only 4-13 pF with a ratio of 0.3-0.48, and all channels of one ADC look alike. That is what an **unpopulated board** (PZT strips unplugged) would give: only mux pin, trace and shared-node capacitance. Whether the strips were attached during this run is not known to the session; with strips attached, tens of picofarads and `tau` of tens of microseconds (the simulator's regime) would be expected. The run therefore demonstrates the firmware, protocol, timing and analysis on the real hardware, but **not** a measurement of PZT capacitances, and the `C_D` in these tables is not independently verified.
- The ADC2/ADC3 difference (both 470 k nominal, 6.3 versus 8.8 pF, ratio 0.39 versus 0.45) and the channels 10..14 of ADC2/ADC4 (longer traces: +2 pF) are real features of these data; resistor tolerance and layout are candidates; see the checklist.
- The measured 4 pF open-channel level also makes the "open channel gives `C_D` directly: `tau / R_bias`" cross-check an upper bound for `C_D` only (the pin and trace capacitance are in it).

## Sensors attached (user run, 2026-10-23, all 50 channels, M = 1, 300 ms discharge)

Data: [`sensors_attached_raw.json`](sensors_attached_raw.json) (raw packets, re-analysis with `python -m captest --reanalyze sensors_attached_raw.json --fit 2exp`) and the original table with the single-exponential fit [`sensors_attached_1exp_results.csv`](sensors_attached_1exp_results.csv). The sensor strips on ADC1 and ADC2 have known broken sensors; ADC3/ADC4 should be reliable; the large sensors (1.2 mm; the others 0.7 mm) are channels 2 and 7 on ADC1 and ADC3 and 2, 7, 12 on ADC2 and ADC4. Bias resistors measured in circuit: 0.999 M, 469.4 k, 469.4 k, 248.1 k.

| ADC | R_bias | 1 exp: tau, rms | 2 exp: tau slow / fast, rms | C_total shift 2 exp vs 1 exp (median) |
| --- | ---: | --- | --- | ---: |
| 1 | 1 M | 17.6 us, 7.1 LSB | 22.6 / 8.2 us, 1.22 LSB | +29 % |
| 2 | 470 k | 14.6 us, 8.6 LSB | 23.7 / 10.5 us, 1.04 LSB | +24 % |
| 3 | 470 k | 15.9 us, 7.3 LSB | 20.2 / 8.1 us, 1.11 LSB | +25 % |
| 4 | 249 k | 20.8 us, 9.6 LSB | 24.1 / 9.6 us, 0.82 LSB | +19 % |

- The single-exponential residuals have the same shape on every channel (negative before 0.5 tau, positive 0.5-2 tau, negative 2-5 tau; 12-30 LSB in those windows); the second exponential removes them and 50/50 channels use it. Re-analysing the 50 bursts with both models takes about 1 s.
- The time constants are about the same on all four ADCs although `R_bias` differs by 4x and the in-circuit resistor readings confirm the nominal values, so `tau` is not `R_bias * C` and the absolute `C_total` is not established (see the design document).
- Large-to-small sensor-side capacitance ratio (single exponential): about 3.0 on ADC1, 3.0-3.3 on ADC2, 3.4 on ADC3 (area ratio 1.2^2 / 0.7^2 = 2.94) but only 1.8 on ADC4, as if each ADC4 channel carried about 40 pF of fixed capacitance.
- Start voltage (two-exponential V0, `VS0_HIGH`): ADC2 channel 3 about +0.39 V, ADC3 channels 8, 2, 3 about +0.18, +0.17, +0.06 V; the other channels are within 0.03 V of their ADC's median.
- Measured on an empty board earlier in the day: single exponential fits to the noise floor with tau 2.5-4.3 us.

## Not done (needs the user's equipment or conditions)

Scope verification of the same-channel mux behaviour, first-clock delay and acquisition; LCR comparison with a disconnected sensor; meter check of `R_bias`; two-temperature repeat; a **power-cycle** (not just reset) first-burst run with the strips attached; the GUI buttons for flashing were tested with the scripts' dry run and the real scripts from the command line, not by clicking them on the board. See [`TESTBOARD_7953_CAPTEST_VERIFICATION_CHECKLIST.md`](../../../../guides/TESTBOARD_7953_CAPTEST_VERIFICATION_CHECKLIST.md).
