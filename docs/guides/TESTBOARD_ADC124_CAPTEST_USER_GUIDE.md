# TestBoard_ADC124 capacitance test: user guide

Board-specific notes for the shared CapTest application (general guide: [`CAPTEST_USER_GUIDE.md`](CAPTEST_USER_GUIDE.md); firmware design, wire protocol and assumptions: [`TESTBOARD_ADC124_CAPTEST.md`](../architecture/firmware/TESTBOARD_ADC124_CAPTEST.md); instrument checks: [`TESTBOARD_ADC124_CAPTEST_VERIFICATION_CHECKLIST.md`](TESTBOARD_ADC124_CAPTEST_VERIFICATION_CHECKLIST.md); what was measured: [`RESULTS.md`](../testing/results/testboard-adc124/capacitance-test/RESULTS.md)).

```text
cd CapTest
python -m captest --board TestBoard_ADC124            # GUI on this board
python -m captest --board TestBoard_ADC124 --port COM4 --auto --array 1 --out run.json
python -m captest --board TestBoard_ADC124 --port COM4 --channels 1:1:0,1:4:6,2:2:0
```

## What is measured and how a connection works

The board has two arrays; each has one ADC124S101 and four 8:1 muxes (MUX1..4) whose outputs go through unity buffers to the ADC inputs IN1..IN4 (MUX1..4 -> IN1..4 is an assumed wiring). **The four muxes of an array share one enable and three address pins**, so switching an address connects input X of **all four muxes at once**. Address 7 is VMID (1.65 V), the only common parking address.

Per burst the firmware (a) parks the array on VMID so every mux-output node ("D node") sits at VMID, (b) starts continuous 16-clock ADC frames and takes `pre-connect frames` readings of the still parked nodes (the baseline `V_D`), (c) switches the mux to address X with a time-stamped break-before-make, and (d) keeps converting. One burst can convert one to four muxes round-robin, so one connection yields up to four channels, each with its own fit. The connection instant is **measured** (cycle-counter stamps of the enable edge), not inferred from ADC frames.

Channels are written `array:mux:input`: arrays 1..2, muxes 1..4, inputs 0..5 (and 6 on MUX4 only): 25 channels per array, 50 in total. Address 7 and the unpopulated inputs are rejected by the firmware.

## Run modes

**Automatic** (Automatic tab, or `--auto [--array 1,2]`): every valid channel of the ticked arrays. By default one burst per address reads every populated mux there: at most 7 connections per array (addresses 0..5 read four muxes, address 6 only MUX4) and 14 for both arrays. Unticking **One burst per address** (`--per-channel`) gives one burst per channel; the other muxes of that address are still connected but not read. That is slower because each channel then also has to wait for its neighbours' connections to be discharged. Progress, Stop (the board is left parked on VMID) and **Re-measure failed** work as in the general guide.

**Manual** (Manual tab, or `--channels`): tick channels in the array grids (rows MUX1..4, columns inputs 0..6) or type `1:1:0,1:4:6,2:2:0` (`1:*:*` = a whole array, `1:2:0-3` a range). Selected channels at the same array and address are read together in one burst; **Measure this channel now** reads exactly one. A channel shows its time since last connected including connections made while reading a neighbour at the same address, and measuring it earlier than the discharge time only warns.

## Settings of this board

| Setting | Default | Meaning |
| --- | --- | --- |
| Samples per burst | auto | per channel; auto = 6 tau of 100 pF behind the largest `R_bias` of the channels read, at least 32 per channel; the firmware limit is 4096 frames per burst in total |
| Park time | 200 us | the bank sits on VMID this long before the first frame |
| Pre-connect frames | 8 | frames before the switch (rounded up to whole round-robin cycles); the baseline `V_D` is the mean of the last *V_D baseline samples* (4) per channel |
| SPI clock | 16 MHz | 8..16 MHz (the ADC124S101 specification range) |
| ADC hold instant | 3 SCLK | the ADC holds this many clocks after the frame start (assumption) |
| Mux turn-on delay | 25 ns | after the enable edge (assumption) |
| Frame stamp latency | 100 ns | the firmware time-stamps a result when it reads it, this much after the frame really ended (assumption) |
| Discharge time | 300 ms | see "What the board does between bursts" |
| VREF | 3.3 V | LSB 0.806 mV |
| Sensor start V_s0 | 0 V | assumed sensor voltage before the connection |
| Fit model | One exponential | the single exponential of the design; two exponentials can be selected |
| `R_bias` table | routes JSON | array 1: 470 k, 1 M, 470 k, 1 M; array 2: 1 M, 470 k, 1 M, 470 k (MUX1..4). The routes JSON and the firmware constants are compared; any disagreement is written to the Log at start |

Time stamps and sample instants: the PC reconstructs frame times from a robust line through the pop stamps (it ignores stalled frames), then `sample instant = frame end - (16 - hold) / f_SPI`, `connection = enable edge + turn-on delay`. A wrong assumption shifts the whole curve: 100 ns is 0.2 % of a 47 us time constant. The three assumptions are settings so they can be corrected when the scope has measured them.

## Reading the results

- **`tau` and `C_total = tau / R_bias`** are the robust numbers. They need a visible step: the amplitude (|Vinf - V0| in LSB) must be well above 50 LSB (flag `LOW_AMPLITUDE`) and the burst must span at least 4 tau (`SHORT_WINDOW`: raise the samples).
- **The ratio `C_D / C_total` is only valid when the sensor starts at `V_s0`** (default 0 V). On this board that is the weak point: a sensor stays near the voltage it was last connected to (VMID) for tens of minutes and then keeps drifting by about 1 mV/s (measured, see RESULTS). A channel connected earlier therefore shows a tiny step and a meaningless ratio near 1; the first connection of a sensor that never was connected, or an **open input** (no sensor: array 2 reads like that), starts at 0 V and gives `C_D` directly (`C_total` = `C_D` + pin, ratio about 0.5).
- `Vs0 est` and `VS0_HIGH` estimate the start voltage from the other channels of the same mux-output node (needs five measured channels); it assumes every sensor started at 0 V, so it is not meaningful for channels that were connected shortly before.
- `C_D` is a property of the mux (the D node), `C_total - C_D` the sensor side (PZT + trace + mux pin), `C_PZT` that minus the parasitic setting.

Quality flags (besides the general ones): `LOW_AMPLITUDE`, `SHORT_WINDOW`, `TOO_SOON` / `VS0_SUSPECT` (connected less than the discharge time ago, also by a neighbour), `V0_INCONSISTENT` (V0 varies over the bursts of one channel), `VS0_HIGH`, `HIGH_RESIDUAL` (> 2 LSB rms), `PERIODIC_RESIDUAL` (a pattern at the frame rate), `V0_OUT_OF_RANGE` (V0 outside 0..VREF), `DATA_ERRORS` (a response with non-zero upper bits), `INCOMPLETE_BURST` (transfer error, abort, no switch), `FRAME_PERIOD_SHORT`, `CLOCK_OUT_OF_SPEC`, `ENABLE_EDGE_JITTER` (the GPIO switch took more than 1 us), `STAMP_GAP` (stalled frames). The firmware cannot return a channel tag, so a wrong MUX-to-IN mapping or a selector mismatch is **not** detectable from the data.

## What the board does between bursts

After a burst the bank is parked on VMID again; the sensors that were connected float. They do not discharge to 0 V in any useful time on the measured board; the default discharge time (300 ms) therefore only protects against the fast settling of the node, not against the sensor's own charge. Run the **discharge check** (`--discharge-check 1:2:1`) to see how a channel's start voltage evolves; it reports "NO plateau" when `V0` keeps changing.

## Firmware

**Flash test firmware** / **Restore production firmware** (or `CapTest/scripts/flash_firmware.*`, `Arduino_Sketches/TestBoard_ADC124_CapTest/scripts/flash_test.*`, `flash_production.*`). The production firmware is never modified; `mcu*` tells them apart (`# TestBoard_ADC124_CapTest` / `# TestBoard_ADC124`). Both leave the board parked on VMID with both CS lines high.
