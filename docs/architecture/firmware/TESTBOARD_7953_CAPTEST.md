# TestBoard_7953 capacitance-test firmware and wire protocol

Implemented 2026-10-23. Code: [`Arduino_Sketches/TestBoard_7953_CapTest/`](../../../Arduino_Sketches/TestBoard_7953_CapTest/) (firmware in `src/`, `include/`); the PC application moved to the shared [`CapTest/`](../../../CapTest/) tool (package `captest/`). User guide: [`../../guides/TESTBOARD_7953_CAPTEST_USER_GUIDE.md`](../../guides/TESTBOARD_7953_CAPTEST_USER_GUIDE.md). Board results: [`../../testing/results/testboard-7953/capacitance-test/`](../../testing/results/testboard-7953/capacitance-test/RESULTS.md). Checks that need extra equipment: [`../../guides/TESTBOARD_7953_CAPTEST_VERIFICATION_CHECKLIST.md`](../../guides/TESTBOARD_7953_CAPTEST_VERIFICATION_CHECKLIST.md).

## Purpose

Measure, per PZT sensor channel of TestBoard_7953, the total capacitance `C_total` at the channel node (PZT + trace + mux pin + the shared MXO node) and the ratio `C_D / C_total` (`C_D` = shared MXO node), from the charge-up curve after one sensor is connected to a parked MXO node and kept connected.

## Separation from production firmware

The capacitance-test firmware is a **separate PlatformIO project** (`env teensy41_captest`). It compiles four production files straight from `Arduino_Sketches/TestBoard_7953/` (`SpiController.cpp`, `Ads7953Adc.cpp`, `ApiProtocol.cpp`, `UsbSerialController.cpp`, plus the production headers via `-I`) and runs the unmodified production pre-script for the pinned USB core patch. Nothing under `Arduino_Sketches/TestBoard_7953/` is edited. Acceptance, verified on 2026-10-23: `git diff --stat -- Arduino_Sketches/TestBoard_7953` is empty and the production `firmware.hex` SHA-256 is `844d87fb8997dcb381e8d8a162bc6d39af667aa6cf641dfcba3b8135a0036dbc` before and after (also after `pio run -t clean` and a rebuild).

The Teensy runs one image at a time. Neither firmware writes EEPROM or flash storage (production settings live in the PC GUI profiles), so switching loses nothing. `scripts/flash_test.*` and `scripts/flash_production.*` only build and upload an image.

Start-up equals production: CS lines high, both SPI buses initialised, every ADC primed with two Vmid commands (this is production's `Ads7953Adc::begin()`; it leaves each mux parked on Vmid). No other mux command is sent until a command arrives.

## Measurement sequence (firmware, per burst)

All four steps run back to back with interrupts masked and no USB output. Frame = one 16-bit LPSPI transfer with GPIO chip select, using production `SpiController::startLpspi16 / lpspiComplete / finishLpspi16`.

1. **Park.** Command channel 15 (Vmid) for at least 4 frames and `park_us` microseconds. The last 16 results (tag 15) are returned as the pre-burst Vmid words.
2. **Connect once, then hold.** Frame `k` (index 0) commands channel `X`; every following frame up to frame `N-1` commands `X` again. The mux switches on the 2nd falling SCLK edge of frame `k+1` (index 1). Frame index `j >= 2` returns the sample taken at the CS fall of frame `j` (sample `i = j - 2`). Indices 0 and 1 return the last two park results (tag 15) taken before the connection. `N + 2` frames are recorded.
3. **Release.** The last two recorded frames already command channel 15 (their mux effect lands after the last sample); one more Vmid frame follows, so at least 3 release frames are sent. The sensor is then left floating and discharges only by leakage; the PC enforces the discharge wait.
4. Statistics (min/sum/max frame period in cycle counts, tag errors, flags) are computed after interrupts are re-enabled. The burst is then sent.

Frame timestamps are `ARM_DWT_CYCCNT` read right after CS falls and the command word is queued (a constant few tens of ns after the physical CS edge, identical for every frame). CS-high time is enforced as `gap_ns`: the production code already waits 40 ns after CS rises, the burst loop waits `gap_ns - 40 ns` more. At 20 MHz the board measured **1373.3 ns per frame** (824 cycles at 600 MHz, max 1396.7 ns, i.e. 14 cycles of jitter on the first frame only). The software overhead (about 270 ns) lies on top of 16 clocks (800 ns) + `gap_ns` (200 ns).

### Command validation

`#NOT_OK` is returned (and no packet is sent) for: channel 15, channels not routed for that ADC (ADC1/ADC3 route 0..9, ADC2/ADC4 route 0..14), ADC outside 1..4, `N` outside 8..2048, `gap_ns > 100000`, `park_us > 10000`, `first_clock_delay_ns > 1000`, SPI clock above 20 MHz, a nominal frame period `16/f + gap_ns` below 1000 ns, an acquisition estimate (`first_clock_delay + 13 clocks + 325 ns`) longer than the period, or an estimated interrupts-off time above 20 ms. Only one ADC is ever clocked at a time; all other chip selects stay high. A burst cannot be interrupted once started (it lasts a few ms), so `capabort` only re-parks every converter on Vmid.

## Commands

ASCII, terminated by `*`, answered by `#OK ...` / `#NOT_OK ...` as in production. Lines starting with `# ` are comments carrying data.

| Command | Reply |
| --- | --- |
| `capstatus*` | comment line `# capstatus firmware=TestBoard_7953_CapTest version=1.3.0 protocol=1 git=<hash> build=<date_time> boot_id=0x<hex> uptime_ms=<n> cpu_hz=<n> spi_clock_hz=<n> max_samples=2048 min_period_ns=1000`, then `#OK`. `status*` is an alias. |
| `capvmid <adc> [n]*` | `# capvmid adc=<a> n=<n> mean_code=<x> min=<c> max=<c> tag_errors=<e>` then `#OK` (average of `n` (default 16, max 64) channel-15 readings). |
| `capburst <adc> <channel> <samples\|auto> <park_us> <gap_ns> [<first_clock_delay_ns> [<hold_mode>]]*` | `#OK capburst ...` followed immediately by one binary packet. `hold_mode` 1 (firmware 1.1.0, default 0) sends the ADS7953 "continue" command (`0x0000`, SLAS605C Table 7) on every sample frame after the connecting one, so the channel is not re-commanded; see "Same channel re-commanded every frame" below. An incomplete burst (transfer timeout) is acknowledged `#NOT_OK` but still sends its packet with `FLAG_INCOMPLETE`. `auto` covers 6 tau of a guessed 100 pF behind the ADC's bias resistor, capped at 1024 samples. `first_clock_delay_ns` (default 25) is only reported back for the analysis; the firmware does not apply it. |
| `capalt <adc> <channel> <frames> <park_us> <gap_ns> <sensor_run> <vmid_run> [<first_clock_delay_ns> [<gap_long_ns>]]*` | Firmware 1.2.0; `gap_long_ns` 1.3.0. Same park, limits and binary packet as `capburst`, with `FLAG_ALTERNATE` set. After the park the commands repeat `sensor_run` (1-8) commands of the channel, then `vmid_run` (1-8) commands of Vmid (input 15), like production with Vmid between channels (`channelrepeat` 1-3, then 2 Vmid frames). `frames` (8-2048) counts command frames; the last two always release to Vmid. Returned word `j >= 2` is the input commanded in frame `j - 2`, and its tag says which. Tag errors count tags that differ from that pattern. `gap_long_ns` (0 = off, otherwise at least `gap_ns`) is the CS-high gap of the frames during which the sensor is connected (command phases 1..`sensor_run`) in every second cycle, so one burst holds two connected times; the interrupt-masked limit is checked as if every frame used it. The CapTest application uses `capalt` with 1 sensor frame, 1 Vmid frame and an automatic long gap as its default TestBoard_7953 method (`captest/capfit.py` `analyze_alternate`; report step 5). |
| `capabort*` | parks all four ADCs on Vmid; `#OK`. `stop*` is an alias. |
| `spiclock <hz>*` | 100 kHz .. 20 MHz (stricter than production's 30 MHz), both buses. |
| `mcu*`, `help*` | as in production. |

Identification: the capacitance-test firmware answers `capstatus*` with `firmware=TestBoard_7953_CapTest`. The production firmware is unmodified; it answers `capstatus*` with `#NOT_OK` and still answers `status*` with `# Current mode: PZT ...`. The PC application reports "production firmware is running" in that case.

`boot_id` is a hash of the SNVS RTC counter, the chip id and the cycle counter at the first host command; `uptime_ms` is also reported. A changed boot id (or a smaller uptime) tells the PC the board was reset or power-cycled, so every sensor is "fresh" again.

## Burst packet (protocol version 1, little-endian)

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 2 | magic `CB` (0x43 0x42) |
| 2 | 1 | protocol version (1) |
| 3 | 1 | header length (56) |
| 4 | 1 | ADC number 1..4 |
| 5 | 1 | channel |
| 6 | 2 | `n_samples` N requested |
| 8 | 2 | `n_frames` recorded (N + 2 when complete) |
| 10 | 1 | `n_vmid` pre-burst Vmid words (max 16) |
| 11 | 1 | flags: bit0 period < 1.0 us, bit1 acquisition estimate < 325 ns, bit2 incomplete, bit3 tag errors, bit4 continue mode (`hold_mode` 1), bit5 sensor/Vmid alternation (`capalt`) |
| 12 | 4 | `cpu_hz` (`F_CPU_ACTUAL`) |
| 16 | 4 | `spi_clock_hz` |
| 20 | 4 | `gap_ns` |
| 24 | 4 | `first_clock_delay_ns` (as supplied) |
| 28 | 4 | `boot_id` |
| 32 | 4 | `park_us` requested |
| 36 | 4 | park frames executed |
| 40 | 4 | min frame period, cycles |
| 44 | 4 | sum of the `n_frames - 1` frame periods, cycles (mean = sum / (n_frames - 1)) |
| 48 | 4 | max frame period, cycles |
| 52 | 2 | timeout errors |
| 54 | 2 | tag errors (frames with index >= 2 whose tag differs from the channel) |
| 56 | 2 x n_vmid | Vmid result words, chronological (tag 15 in the top 4 bits) |
| ... | 2 x n_frames | response word of every frame: top 4 bits channel tag, low 12 bits code |
| ... | 4 x n_frames | `ARM_DWT_CYCCNT` at the start of every frame (wraps at 2^32; subtract and mask) |
| end-2 | 2 | CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no xor-out; check value of `123456789` is 0x29B1) over all preceding bytes |

Total length = 56 + 2·n_vmid + 6·n_frames + 2. Maximum 12,390 bytes (N = 2048, 16 Vmid words). Firmware RAM: `words_` + `timestamps_` + `vmid_words_` + packet buffer ≈ 24.5 KB static.

### Example (measured on the board, ADC2 channel 5, N = 8, park 50 us, gap 200 ns, 20 MHz; 150 bytes)

Full hex: [`example_packet_2_5_n8.hex`](../../testing/results/testboard-7953/capacitance-test/example_packet_2_5_n8.hex).

```text
header (56 bytes):
43 42 01 38 02 05 08 00 0a 00 10 00 00 46 c3 23 00 2d 31 01 c8 00 00 00
19 00 00 00 00 0e 68 bc 32 00 00 00 25 00 00 00 38 03 00 00 06 1d 00 00
46 03 00 00 00 00 00 00
Vmid words (16 x 2 bytes), frame words (10 x 2), timestamps (10 x 4), CRC (2) follow.
```

Decoded fields: adc 2, channel 5, N 8, n_frames 10, n_vmid 16, flags 0, cpu_hz 600,000,000, spi_clock_hz 20,000,000 (`00 2d 31 01`), gap_ns 200 (`c8 00 00 00`), first_clock_delay_ns 25 (`19 00 00 00`), boot_id 0xBC680E00, park_us 50, park_frames 37, period min/sum/max 824 / 7,430 / 838 cycles (`38 03 00 00`, `06 1d 00 00`, `46 03 00 00`), timeout errors 0, tag errors 0.

Vmid words `f7 ff` = tag 15, code 0x7FF (2047); frame words (low byte first): `ff f7 ff f7` = tag 15 for frames 0 and 1, then `21 54` = tag 5, code 0x421 (1057) for sample 0, `b1 55` = code 1457, `84 56` = 1668, ... the charge-up curve. Timestamps `66 76 5b 95`, `ac 79 5b 95`, ... are 0x955B7666, 0x955B79AC, ... (824-cycle steps, 1.373 us). The packet ends with the CRC `b5 13` = 0x13B5.

A byte-exact synthetic frame is part of the unit tests (`tests/test_protocol.py::test_documented_example_frame_is_stable`, `test_crc_known_vector`).

## Datasheet facts verified (SLAS605C, text extracted from the datasheet PDF)

- Manual-mode command word: DI15-12 = 0001, DI11 = 1 (program DI06..DI00), DI10-07 channel address (MSB DI10), DI06 range (0 = 0..VREF, range 1), DI05 power-down (never set by this firmware), DI04 = 0 (SDO carries the channel address), DI03-00 GPIO. The production `manualCommand()` (`0x1000 | 0x0800 | channel << 7`) matches. SDO word: DO15-12 channel address of the converted channel, DO11-00 data.
- A new multiplexer channel is selected on the second SCLK falling edge; the acquisition phase starts on the 14th SCLK rising edge; the next CS falling edge ends acquisition and samples. Minimum acquisition 325 ns, aperture delay 5 ns, minimum CS-high pulse 20 ns, CS low to first SCLK rising edge at least 4-6 ns.
- Inconsistency inside the datasheet: the Figure 51 text says the input is sampled "on the rising edge of CS of the (N+2)th frame", while section 8.3 and Figure 1 say the falling edge of CS starts the frame and samples. This work uses the falling edge, as the existing timing report does.

## Assumptions and unknowns (document, do not forget)

| Item | Assumption / status |
| --- | --- |
| Same channel re-commanded every frame | Datasheet silent on whether the mux stays closed. Inferred: the mux only changes on a 2nd falling SCLK edge, so it holds. A scope capture of CS and the node together (2026-11-03) shows the mux selecting 172-180 ns after CS falls; that it holds while CS is high is not shown. A once-per-frame charge transfer does exist (gap sweep, 2026-10-30), but sending the "continue" command instead of re-commanding the channel does not change it: 15 channels, 4 gaps, 5 repeats, `C_total` and the per-frame capacitance agree within 0.4 % and 1.2 % (`CapTest/tools/continue_mode_7953.py`). So the command word is not the cause. A re-selection the chip does regardless, or another path near Vmid, remain possible; the sampling capacitor is unlikely, since the buffer and its 270 pF output capacitor separate it from MXO. The analysis cannot see a once-per-frame effect (each frame is sampled at the same phase); it flags a narrow residual line at 2..6.5 frames. See the verification checklist. |
| Mux while CS is high | Not specified; inferred to hold. |
| First-clock delay (CS fall to first SCLK rising edge) | 25 ns default (an assumption from the timing report, also used by the QSPICE model). Shifts the connection instant `t_c = CS fall(k+1) + delay + 1.5 / f_sck` by the same amount; with samples about 1.4 us apart a 100 ns error is a 7 % shift of one sample interval and mostly affects `V0` (extrapolation to `t_c`), little `tau`. |
| Sensor start voltage `V_s0` | 0 V (a setting). Valid for the first burst after power-up or after the sensor has discharged. The default wait of the application is 300 ms, chosen from small sensors, whose node discharges with a 30-50 ms time constant. **It does not discharge large sensors:** on the large channels the first sample is about 0.03 of `V_D` when rested and about 0.75 only 500 ms after a burst, and on 2:7 the first sample reaches its floor only after about 3 s (770 mV at 0.5 s, 214 mV at 2 s, 93 mV from 5 s). The wait does not ensure a discharge. Vmid between samples needs the sensor at least 0.3 V below `V_D` (`ALT_SMALL_RANGE`), so large sensors need a longer wait there. |
| Charge injection when the Vmid channel switches off | Ignored. It lowers `V_D` slightly below Vmid (unspecified in the datasheet): a few percent effect on `C_D/C_total`, none on `tau`. |
| `R_bias` | From `benchmarks/testboard_7953_bias_resistors.json` (ADC1 1 M, ADC2 470 k, ADC3 470 k, ADC4 249 k); tolerance maps 1:1 to `C_total`. Confirm with a meter. |
| Net pin leakage | **Not established.** The first reading was that `Vinf` sits below Vmid by `I_leak * R_bias` (50-140 nA, consistent with 16 pins at 3-7 nA, datasheet Figure 43). The gap sweep of 2026-10-30 contradicts it: the offset shrinks as the frame period `T` grows (ADC1:0: 25 mV at the shortest gap, 3 mV at the longest), so it follows the per-frame charge, not a leakage current. `leak_na` is still reported (`Vinf` is a free fit parameter) but does not measure leakage on this board. |
| `C_parasitic` (trace, mux pin) | 5 pF default, an assumption used only to derive `C_PZT` from the sensor-side capacitance. |
| Residual colour | Real residuals correlate between neighbouring samples (lag-1 autocorrelation about 0.7, rms 1.1-1.5 LSB), so a white-noise line detector gives false alarms. |

## Analysis (PC side, `captest/capfit.py`)

`V = code / 4096 * VREF`. The first K = 2 samples and any with a wrong tag are dropped. Sample times are `(timestamp - timestamp[1]) / cpu_hz - delta`. The single-exponential fit `V(t) = Vinf - (Vinf - V0) exp(-t / tau)` uses a numpy-only variable-projection search (1-D over `tau`, exact linear solve for `Vinf` and the amplitude), polished with scipy's `curve_fit` when scipy is installed; the two-exponential alternative is described below. `C_total = tau / R_bias`; `C_D / C_total = (V0 - V_s0) / (V_D - V_s0)` with `V_D` the mean of the tag-15 readings (last 8 park words and frames 0 and 1); `C_D = ratio * C_total`; sensor-side `C = C_total - C_D`; `C_PZT = C - C_parasitic`.

### Validity of the single-connection values (`capburst`)

With one connection held for the whole burst, a charge also moves into the node once per frame, so `tau` depends on the frame period: `tau / R_bias` read 1.7-4 times below the `C_total` found by fitting several periods together (gap sweep, 2026-10-30; a single-exponential fit on the longest single gap alone is still about 9 % low). What the analysis therefore does for such a burst (packet flag: one connection):

- `FRAME_DEPENDENT` is set and `c_total_method` is `tau_over_r`. The `C_total` is a relative reading, for comparing channels, not a capacitance.
- `V0`, `C_D/C_total` (and its 1-exp and 2-exp forms), `C_D`, `C sensor` and `C_PZT` are exported empty. The fit still computes them on each measurement for inspection and re-analysis, but they have no valid basis, and the `V0_INCONSISTENT` check is skipped.
- The start-voltage check (`VS0_HIGH`, `Vs0 est`) is not run for these results, because it uses the same `V0`.
- `UNSTABLE_TAU` is set on any board when `tau` differs by more than 5 % (sample sd over mean) between the usable bursts of one channel.

The default method, Vmid between samples (`capalt`, `c_total_method` = `vmid_cycles`), does not depend on the frame period and does not need `V0` or the sensor start voltage. It reports `C_share` at `V_D` in its own columns (`c_share_ratio`, `c_share_pf`): an effective connection capacitance (the node plus the charge each connected frame moves), not the node capacitance `C_D`, so `ratio`, `C_D`, `C sensor` and `C_PZT` stay empty and `C_share` must not be subtracted as a parasitic. The fit assumes the node returns to `V_D` after every cycle and checks it: the Vmid reading that closes a cycle must be within 5 mV of `V_D` (about 1 mV on the board), otherwise that cycle's pair is left out and `VMID_RESET_FAILED` is set. `Vinf` is the level the sensor settles to after a short cycle, from iterating the fitted nonlinear model through the short and long cycles (`vinf_long_v` is the level after a long cycle).

Bursts of different methods on one channel are never averaged: the preferred method (most usable bursts, then Vmid between samples) is used and the row carries `MIXED_METHODS` and a message listing what was left out; `--reanalyze` on the CLI gives one row per method. See the [capacitance report](../../reports/TestBoard_7953_capacitance_report.md) and the [measurement revision plan](../../plans/features/plan-captest-measurement-revision.md).

The two-exponential shape described below was recorded with one connection per burst; it largely tracks the frame effect, so the fast/slow split is not a property of the sensor alone.

### Vinf mode (measured settling voltage)

A separate process from the capacitance measurement (Manual tab, `Measure Vinf of selected`; CLI `--vinf --channels`). For each channel and repeat it takes three bursts:

1. A Vmid-between-samples burst (see above): the fit gives the **calculated** `Vinf`.
2. A long **alternating** burst (`capalt`, one sensor frame and the Vmid frames, no long gap), the way production runs the channels. Its length comes from the fitted `C_total` and `C_share`: the distance below `V_D` shrinks by `exp(-T / tau) * (1 - C_share / C_total)` per cycle, the burst is twice the cycles needed to get within 3 mV, at least 64 cycles and at most 2048 frames. The **alternating `Vinf`** is the median of the sensor samples in the last quarter, the quiet period after the sensor has settled, which is the baseline production uses. `VINF_ALT_NOT_SETTLED` is set when the quarter before differs by more than 3 LSB; `VMID_RESET_FAILED` when a Vmid reading in the tail is more than 5 mV from `V_D`.
3. A **held** burst (`capburst`, the sensor stays connected) of about 12 tau (256 to 2048 samples); the median of its last quarter is the held `Vinf` (`VINF_NOT_SETTLED` as above).

Levels are reported as millivolts from `V_D` (the Vmid reading before the connection). They come from different frame sequences and the offset below `V_D` depends on the frame period, so they are not expected to be equal; the alternating one corresponds to production only for the same gap and number of Vmid frames. The bursts are not written to the raw file.

### Board baseline (`captest/baseline.py`)

The board adds capacitance to every `C_total`. Measured with no strips (2026-11-04, Vmid between samples, 3 bursts per channel, `Results/TestBoard_7953/2026-11-04_switching_strips/no_strips_connected.*`): 12.3-14.5 pF on ADC1 and ADC2 channels 0-9, 17.0-18.5 pF on ADC3, 14.7-15.5 pF on ADC4, and 16.6-19.4 pF on channels 10-14 of ADC2 and ADC4. A straight-line fit against the net lengths of `boards/TestBoard_7953-NetLengthDetails.csv` gives `C_total = 7.4 + 0.123 pF/mm x channel route + 0.055 pF/mm x MXO route + 1.5 pF if the MXO path uses the back layer`, rms 0.21 pF over the 50 channels; the MXO terms rest on four ADC values, so they only describe each ADC's offset.

The baseline file (`settings/<board>_baseline.json`, format `captest-baseline-1`) stores per input `c_total_pf`, its sd over the bursts, `c_share_pf`, the `c_total_method`, the number of bursts and the flags, plus the settings that matter (`method`, `gap_ns`, `gap_long_ns`, `vmid_frames`, `fit_model`, `discard_k`). With `Settings.subtract_baseline` on, `ChannelResult` carries the entry of its input and reports `c_board_pf` and `c_net_pf = c_total_pf - baseline`; `c_net_pf` is NaN when the input has no entry or when the baseline's method differs from the result's `c_total_method` (the two are never subtracted from each other). `MeasurementSession.save_baseline()` writes the current results, `apply_baseline()` re-attaches entries to results already held, and `runner.reanalyze()` attaches them offline. `Settings.to_json()` leaves `subtract_baseline` out while it is false, so earlier saved files keep their layout. The subtraction is additive and unchecked beyond that: on the strip swap it removed the ADC3-ADC1 offset of small sensors, but not about 4 pF on the 200-260 pF sensors.

### Fit models (selectable)

Real PZT sensors do not charge like one RC. On the strips attached to TestBoard_7953 a single exponential leaves a structured residual of 7-10 LSB rms (shape: negative before 0.5 tau, positive 0.5-2 tau, negative 2-5 tau; identical on every channel), while two exponentials reach the noise floor (0.8-1.2 LSB). Both models are always fitted; the setting **fit model** chooses which one drives the reported numbers:

| Model | Equation | Reported `tau`, `V0`, `C_total` |
| --- | --- | --- |
| `1exp` | `V = Vinf - (Vinf - V0) exp(-t/tau)` | `tau`, `V0` of the single exponential |
| `2exp` (default) | `V = Vinf - A_fast exp(-t/tau_fast) - A_slow exp(-t/tau_slow)`, `V0 = Vinf - A_fast - A_slow` | `tau = tau_slow`, `V0` extrapolated to `t_c` |

`C_total = tau / R_bias` in both cases and the ratio `C_D / C_total = (V0 - V_s0) / (V_D - V_s0)` uses the V0 of the model in use. The two-exponential fit is a numpy-only variable projection (log grid over both time constants around the single-exponential `tau`, exact linear solve for `Vinf` and both amplitudes, then a shrinking pattern search), polished with scipy's `least_squares` when scipy is installed. In `2exp` mode a burst silently falls back to `1exp` (`fit_model_used = 1exp`) unless the second exponential is clearly needed: `tau_slow / tau_fast >= 1.4`, rms below 85 % of the single-exponential rms and at least 0.3 LSB lower. Every result also carries `c_total_1exp` and `c_total_shift_pct` (C_total of the model in use versus the single exponential), `tau_fast`, `fast_fraction` (share of the step carried by the fast component), `v0_1exp`, `v0_2exp` and both rms values.

**What the components mean is not established.** On the recorded data (50 channels, all four ADCs) the slow time constant is 20-24 us and the fast 8-10 us, on all four ADCs, although `R_bias` differs by a factor of four (the in-circuit meter readings 0.999 M, 469.4 k, 469.4 k and 248.1 k confirm the nominal values). So `tau` is not simply `R_bias * C`: `C_total = tau / R_bias` is only a formal conversion and absolute picofarad values are unreliable until the dominant resistance is understood (candidates: dielectric absorption of the PZT, a series or additional bias resistance on the board, cable). The relative size of sensors on one ADC (for example the 1.2 mm sensors against the 0.7 mm ones) is meaningful. V0, and with it `C_D / C_total`, depends strongly on the model (ADC2 channel 0: 0.157 V with one exponential, 0.039 V with two).

The session keeps every recorded packet, and each result knows the packets it was computed from, so `MeasurementSession.reanalyze(settings)` (GUI: the Fit model box and **Re-analyse with current settings**) recomputes all results with other settings without a new measurement; channels that failed before their packet could be decoded keep their failed result.

### Sensor start voltage (`VS0_HIGH`)

`V_s0 = 0` is assumed, but a sensor can float at a few hundred millivolts (handling, temperature). `V_s0` does not change `tau`; it raises `V0` and therefore `C_D / C_total` and `C_D`. All channels of one ADC share `C_D`, so for sensors starting at 0 V `V0 = V_D * C_D / C_total` is proportional to `1 / tau`. `capfit.start_voltage_check` takes the robust constant `K = median(V0 * tau)` over the ADC's channels (at least 5 measured), the expected `V0 = K / tau` of each channel, and the excess over it gives the estimate `V_s0 = (V0 - r V_D) / (1 - r)` with `r = V0_expected / V_D` (no `R_bias` needed, independent of the sensor size). Beyond 50 mV on either side of zero the channel gets the flag `VS0_HIGH` and the estimate is shown as `Vs0 est`. It is re-evaluated whenever another channel of the ADC is measured. On the single-connection data of 2026-10-23 it flagged ADC2 channel 3 (+0.39 V), ADC3 channels 8 (+0.18 V), 2 (+0.17 V) and 3 (+0.06 V); the application no longer runs it on 7953 results, which have no valid `V0` (see above). It needs most channels of the ADC to start near 0 V. On the ADC124 it still runs.

Flags: `LOW_AMPLITUDE` (< 50 LSB), `TAG_MISMATCH`, `HIGH_RESIDUAL` (rms > 2 LSB), `PERIODIC_RESIDUAL`, `VS0_SUSPECT` / `TOO_SOON` (measured less than the discharge time after the previous connection) / `V0_INCONSISTENT` (V0 varies more than 3 % over the bursts of one channel), `FRAME_PERIOD_SHORT`, `ACQUISITION_SHORT`, `INCOMPLETE_BURST`, `FEW_SAMPLES`, `FIT_FAILED`, `NO_VMID_READING`, `VINF_NOT_VMID` (implied leakage above 250 nA), `VS0_HIGH` (estimated sensor start voltage more than 50 mV from zero, either sign), `FRAME_DEPENDENT` (single connection: `tau / R_bias` is a relative reading), `UNSTABLE_TAU` (`tau` varies more than 5 % over the bursts of one channel), `MIXED_METHODS`, `VMID_RESET_FAILED` (Vmid between samples: the node did not return to `V_D` after a cycle), `ALT_SMALL_RANGE` (Vmid between samples: the sensor started less than 0.3 V below `V_D`; the burst is excluded).

## Verification status

See [`RESULTS.md`](../../testing/results/testboard-7953/capacitance-test/RESULTS.md) for the build sizes, offline test results and the on-board run.
