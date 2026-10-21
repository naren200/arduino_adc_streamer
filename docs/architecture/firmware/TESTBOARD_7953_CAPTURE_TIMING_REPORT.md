# TestBoard_7953 capture timing report

21 October 2026 | Firmware review and existing measurements | Report 1 of 2

## What the four requested times mean

The sensor connects through the ADS7953 internal MUX to a shared OPA365 input and its bias resistor. Sampling happens at the ADC behind that buffer. The sensor connection, ADC acquisition window, sample instant, and result delivery are different events.

| Requested quantity | Finding for this board | Confidence |
| --- | --- | --- |
| Time connected to MUX output / op-amp | From the physical MUX connection to the next physical selection of a different input; usually repeat-count times the local ADC frame spacing | Sequence established; individual frame spacing needs measurement |
| Time ADC takes the sample | Track window ends at CS falling; conversion/result shifting follows | Datasheet edge definitions; actual window needs CS/SCLK trace |
| Connection to retained sample | Ends at the CS falling edge of the retained conversion, with a small aperture delay | Calculable from an edge trace; illustrative numbers on page 3 |
| Time disconnected | Next connection minus previous disconnection; normally the route revisit period minus its connected interval | Requires physical execution order and actual sweep spacing |

**Main result:** current code establishes the sequence and conversion counts, but saved sweep timestamps do not establish exact per-channel connected or pre-sample times. Those require local CS/SCLK timing. No new hardware measurements, firmware changes, or API functions were made for this report.

## Hardware and settings

| Item | Current repository value |
| --- | --- |
| Controller / converters | Teensy 4.1; four ADS7953; 12-bit results |
| Physical execution | ADC1 + ADC3, then ADC2 + ADC4; separate buses can overlap in DMA/LPSPI |
| Full populated sensor routing | ADC1/3: inputs 0-9; ADC2/4: inputs 0-14; total 50 routes |
| Shared MXO bias resistor | ADC1: 1 Mohm; ADC2: 470 kohm; ADC3: 470 kohm; ADC4: 249 kohm |
| Reserved input | Channel 15 = VMID; shared bias returns to VMID, not ground |
| SPI | Mode 0; requested default 20 MHz; programmed CS-high guard 40 ns |
| Firmware boot | Manual sequence; LPSPI; repeat 3; VMID between channels off; interleaved payload order |
| GUI profile defaults | Manual; repeat 1; requested 20 MHz; ADC payload order; both arrays; VMID off; preserve current engine |

The GUI's repeat default differs from firmware boot. A report or future calculator must use effective stopped status and the actual route list, rather than assume either default. The 2.5 V / 5 V reference command changes input range, not the programmed timing. Requested clocks above 20 MHz are experimental and outside the ADS7953 clock specification. [F1, F2, F3, D1]

<!-- pagebreak -->

## Capture process, step by step

| Step | Work performed | Duration or count |
| --- | --- | --- |
| 1. Prepare run | Build route destinations and manual command streams; park active ADCs on VMID | Once per run; parking uses 2 frames per active ADC; not part of each wire acquisition timestamp |
| 2. Begin sweep | Clear destination flags, record start, open active LPSPI sessions | Flags are cleared before start; sessions are inside acquisition |
| 3. First ADC pair | Clock ADC1 and ADC3 streams, if routed | DMA/LPSPI overlap the two buses; blocking transfers the pair sequentially per operation |
| 4. Per-frame analog work | Hold current sample at CS falling; move MUX for a future sample; acquire next sample | Edge timeline below; overlaps digital transfer |
| 5. Resolve results | Advance the 2-frame command/result mapping; validate returned tags; retain only final repeat | Included in transfer loop; no separate calibrated current-build duration |
| 6. Second ADC pair | Clock ADC2 and ADC4 streams, if routed | First pair is parked before same-bus ownership changes when both ADCs on that bus are active |
| 7. Finish acquisition | Verify every payload slot, restore SPI state, record end | Inside acquisition |
| 8. Encode frame | Header, 50 results for full routing, timing trailer | 14 + 2N bytes; 114 bytes for N=50 |
| 9. USB enqueue / service | Accept complete frame or discard if busy; process foreground work; next sweep | Outside acquisition; affects revisit spacing |
| 10. Stop or failure | Park active ADCs on VMID | Outside ordinary steady-state sweep; not zero duration |

## Electrical edge timeline

In ADS7953 operation, CS falling holds the sample; the second SCLK falling edge changes the MUX; the fourteenth SCLK rising edge starts acquisition for the next CS falling edge. A manual command in frame k is applied at the MUX in frame k+1 and its sample is returned in frame k+2. The two-frame mapping is not two extra conversion periods after the physical sample. [D1: section 8.1, Figure 1; F2]

```text
frame k           frame k+1                       frame k+2
command channel X -> MUX changes to X -> track opens -> HOLD X -> result X
                     falling SCLK #2   rising #14      CS falls
                     |<--- bias leakage before sample --->|
                     |<---- connected until next MUX change ---->|
```

At 20 MHz, 16 clock periods occupy **0.800 us**. The ADS7953 minimum acquisition time is **0.325 us**; the sampling aperture delay is typically **0.005 us**. The actual track window includes the end of one frame and its gap before the next. It must be measured, not set equal to 0.325 us. [D1: section 7.6]

The configured 0.040 us CS-high guard plus 0.800 us clocks is only a digital allowance of 0.840 us. It is not an accepted ADC period or a complete timing guarantee: first-clock delay, polling, GPIO, and the acquisition requirement also matter. Acquisition and conversion overlap across frames; adding 0.325 us to every firmware step double counts some time.

<!-- pagebreak -->

## Per-channel timing equations

Use physical edges, with times in us. Let C[j] be CS falling for local ADC frame j, Q[j] its second SCLK falling edge, and A[j] its fourteenth rising edge. Let d_on/d_off represent the small physical switch delay relative to Q; they remain uncalibrated here.

For a distinct channel X commanded first at frame k and repeated r times:

```text
t_on(X)       = Q[k+1] + d_on
t_sample(X)   = C[k+r+1]              # retained final repeat, nominal hold edge
t_track_start = A[k+r]               # acquisition for retained result
t_off(X)      = Q[k+r+1] + d_off     # if next command selects another input
T_connected   = t_off - t_on
T_pre_sample  = t_sample - t_on      # add aperture delay if required
T_ADC_track   = C[k+r+1] - A[k+r]
T_post_sample = t_off - t_sample
T_disconnected = next_t_on - t_off
```

For uniform local frame spacing P and Q[j] approximately C[j]+delta, ignoring switch delays: **T_connected = rP**, **T_pre_sample = rP-delta**, and **T_post_sample = delta**. This uses spacing on the same ADC, which may include time spent servicing the parallel bus.

### Numerical illustration, not a board measurement

Assume actual 20 MHz clocks, first rising edge 0.025 us after CS falls, delta=0.100 us, and uniform local CS spacing P=2.000 us. Then A[j] is 0.675 us after C[j]. These assumed edges yield:

| Repeat r | Connected | Connection to retained hold | Track window of retained sample | Hold to disconnection |
| --- | ---: | ---: | ---: | ---: |
| 1 | 2.000 us | 1.900 us | 1.325 us | 0.100 us |
| 2 | 4.000 us | 3.900 us | 1.325 us | 0.100 us |
| 3 | 6.000 us | 5.900 us | 1.325 us | 0.100 us |

The illustration does not estimate P from sweep duration divided by channel count. That division cannot resolve parallel execution, pipeline drains, or edge placement.

### Boundary cases that change these equations

With two active ADCs on a bus, manual streams end with VMID commands so sensors release before the next ADC starts. With one active ADC and optional VMID off, the two drain commands repeat the final sensor. That sensor remains connected through the sweep gap and into the following sweep until a different selection takes effect.

For a continuous single route on a bus with VMID off, the sensor can remain connected across all sweeps: there is no fresh connection each frame and steady-state disconnected time is zero. Measure pre-sample age from the original connection, not each frame start. VMID on or Auto-1 breaks that continuous hold. An unrouted input on an active ADC is ordinarily disconnected, while a routed input can have multiple retained measurements within one physical connection history only if the actual configuration permits that history.

<!-- pagebreak -->

## Sweep operation counts and timing budget

For each active ADC with n routes and manual repeat r, the current prepared stream contains:

```text
VMID off: frames = n*r + 2       # two drain commands at stream end
VMID on:  frames = n*(r + 2)     # two VMID commands after every route
```

All frames are real conversions, including responses discarded by the pipeline. No additional two-frame drain is appended after the VMID-on stream. Counts below exclude start/stop parking and assume both complete arrays, balanced corresponding ADC pairs, requested/actual 20 MHz, and ideal overlap between the two buses. [F2]

| Manual configuration | Frames on all ADCs | Paired operation slots | Clock time on critical bus | 40 ns guard allowance on that bus |
| --- | ---: | ---: | ---: | ---: |
| Repeat 1, VMID off | 58 | 29 | 23.200 us | 1.160 us |
| Repeat 2, VMID off | 108 | 54 | 43.200 us | 2.160 us |
| Repeat 3, VMID off | 158 | 79 | 63.200 us | 3.160 us |
| Repeat 1, VMID on | 150 | 75 | 60.000 us | 3.000 us |
| Repeat 2, VMID on | 200 | 100 | 80.000 us | 4.000 us |
| Repeat 3, VMID on | 250 | 125 | 100.000 us | 5.000 us |

These are operation-count budgets, not predicted valid sweep durations. They omit setup, transfer launch/completion, physical ADC acquisition constraints, response handling, interrupts and inter-sweep work. In blocking mode there is no bus overlap, so the serial clock occupancy sums over all 58 frames at repeat 1 / VMID off (46.400 us). For unbalanced routing, the two pair lengths are separately limited by their longer active stream.

## Auto-1 behavior

Auto-1 uses a cached ascending sparse channel mask plus channel 15. Effective repeat is always one, regardless of saved manual repeat. A cold stream can program the mask and restart the counter; steady streams resume from VMID without rewriting it. The stream ends when every requested sensor result has arrived, by which point the MUX has advanced to VMID. It has no configurable between-channel analog delay. [F2]

For a healthy persistent resume, the expected executed length is n+1 frames per ADC: one discarded VMID result plus n sensor results. Full balanced arrays therefore take 27 paired slots (11 for ADC1/3 and 16 for ADC2/4), with 21.600 us critical-bus clock occupancy at 20 MHz. Cold programming/restart has extra frames; do not apply the resume count to the first sweep. Tagged responses and stream termination should be checked in a trace before using these counts for analog timing calibration.

Changing payload scan order changes destination order while preserving the configured route order within each ADC; it does not turn payload positions into equally spaced sample times. Changing the route list can change manual channel order. Execution still runs the first ADC pair and then the second. Auto-1 physical channel order is ascending even when payload destinations use a different order.

<!-- pagebreak -->

## Existing measurements and what they establish

### Production full-matrix capture, 7 October 2026

Both arrays, 50 routes, ADC payload order, repeat 1, VMID off, requested 20 MHz, three repetitions per configuration, production profiling off. The table below recomputes medians of saved per-capture median durations and received periods from the full-matrix CSV. [M1]

| Sequence | Engine | Acquisition median | Received sweep-period median |
| --- | --- | ---: | ---: |
| Manual | Blocking | 86 us | 88 us |
| Manual | DMA | 150 us | 151 us |
| Manual | LPSPI | 58 us | 59 us |
| Auto-1 | Blocking | 85 us | 86 us |
| Auto-1 | DMA | 144 us | 146 us |
| Auto-1 | LPSPI | 60 us | 61 us |

The prior independently decoded report gives sustained manual LPSPI throughput of about 16,887 sweeps/s, with zero discards in those three full-array captures and maximum actual sampling period 64 us. Sustained rate is not the reciprocal of median period. The current review rechecked CSV statistics, not all raw binary recordings. [M1]

For manual LPSPI, 58 us acquisition versus 24.360 us clock-plus-guard accounting leaves 33.640 us outside that simplified allowance. This is an aggregate residual, including timing required by the ADC and controller; it is not measured software overhead or 33.640 us of additional sensor leakage on every route.

### Historical phase profile, 6 October 2026

An earlier diagnostic build measured these whole-run phase means for both full arrays, manual LPSPI, repeat 1, VMID off, requested 20 MHz. These are medians across three capture means, including warm-up. [M2]

| Phase | Time | Interpretation |
| --- | ---: | --- |
| Acquisition | 71.817 us | Inclusive acquisition scope |
| Transfer | 69.244 us | Includes clocks, polling, GPIO and response handling |
| Prepare / session / validate | 0.511 / 0.916 / 0.836 us | Partly overlapping acquisition scope |
| Encode / capacity observation | 0.521 / 0.187 us | Outside acquisition |
| USB write / foreground | 0.557 / 0.465 us | Enqueue / foreground scope, not USB wire latency |
| Profile bookkeeping | 1.461 us | Diagnostic instrumentation cost |
| Run preparation | 12.607 us | Once per run |

This predates the optimized word path and live nonblocking discard policy. Do not combine these phases with the later 58 us production acquisition or use the old millisecond USB stalls as current behavior. Component scopes do not partition acquisition exactly, and enabled profiling changes timing.

<!-- pagebreak -->

## Disconnection, leakage and future calculation inputs

For a route with one contiguous connected interval per stable cycle, **T_disconnected = T_revisit - T_connected**. The revisit period is between physical connections of this route, including inter-sweep work. It is not acquisition duration / 50. In the page-3 hypothetical repeat-1 case, a hypothetical 59 us physical revisit would give 57 us disconnected; this pairing is illustrative, not a measured 57 us result.

Bias leakage is toward VMID while connected. Sensor insulation leakage may be toward ground; switch leakage can have either sign. For a first-order connected approximation:

```text
dV/dt = -(V - Vmid)/(R_bias*C_eff) - V/(R_sensor*C_eff) + I_net_on/C_eff
disconnected: dV/dt = -V/(R_sensor*C_sensor) + I_net_off/C_sensor
```

Initial charge sharing at connection and charge injection at switching require additional terms. Do not put the ADC's sampling capacitor directly in parallel with sensor capacitance across the OPA365. OPA365 has typical 300 ns settling to 0.01% under its specified 5 V, gain-1, 4 V-step conditions; that is a component reference, not an established settling time for this board. [D2]

| Input for later functions | Available now | Still needed |
| --- | --- | --- |
| Exact route list / sequence / repeats / arrays | Firmware plan and GUI settings; read effective status | Snapshot for each calculation |
| Bias resistance | Per-ADC metadata and board reports | Actual tolerance / assembly confirmation |
| MUX and ADC edge schedule | Code and ADS7953 event definitions | CS/SCLK/DIN/SDO trace for each engine and topology |
| Physical connect / release | Nominal MUX-update edge | Analog switching delay and settling at MXO / buffer output |
| Capacitance / sensor insulation | No validated per-sensor values used here | Sensor capacitance, leakage resistance, cable and input parasitics |
| Switch / op-amp leakage | Component specifications provide conditions and bounds | Use temperature and voltage appropriate limits; verify assembly |
| Off-time / continuous hold | Determined by route-state history | Actual all-sweep cadence, including discarded sweeps |

## Focused measurement needed to complete exact timing

1. Record firmware/build identity, effective status, exact routes, sensor attachment, reference and requested clock. Start with full-array manual LPSPI / repeat 1 / VMID off at 20 MHz, then VMID on and repeats 2/3.
2. Capture both SCLK buses and CS pins 9, 24, 32, 33 with DIN/SDO as needed. Decode every 16-clock frame, command, channel tag, physical MUX update and retained result. Resolve the 40 ns guard and clock edges with adequate bandwidth.
3. Measure MXO / OPA365 input and output using a suitably low-capacitance probe. Time first connection, signal settling and disconnect relative to digital edges; account for probe loading on the high-impedance node.
4. Repeat for single ADC, two ADCs on one bus, both buses, sparse/unbalanced routing, Auto-1 cold/resume, and run/stop transitions. Capture enough cycles to report distributions and interrupts, not just one typical period.
5. Measure each sensor's C and insulation leakage, then calculate connected/pre-sample/off leakage with uncertainties. Verify the predicted retained voltage against a repeatable known input before exposing a future timing API.

No per-channel timing API is added here. A later API should return track start, hold/sample, result-ready, connection/release and off interval separately, with measured versus estimated provenance and an explicit continuous-connection state.

<!-- pagebreak -->

## Sources and review scope

Reviewed repository HEAD: `bc74949548091bb43aaffdfc37b169fce83f3a21`. Initial working tree was clean. The source manifest beside these reports records SHA-256 hashes; it excludes new report artifacts from the firmware snapshot. Existing datasets retain their historical firmware identity and are not represented as new measurements of this HEAD.

### Firmware and board sources

- **F1.** [Hardware constants](../../../Arduino_Sketches/TestBoard_7953/include/ConfigurableParameters.h), `Arduino_Sketches/TestBoard_7953/include/ConfigurableParameters.h`: clocks, pipeline length, bias values, repeat default, VMID input.
- **F2.** [PZT controller](../../../Arduino_Sketches/TestBoard_7953/src/PztController.cpp) and [header](../../../Arduino_Sketches/TestBoard_7953/include/PztController.h), `Arduino_Sketches/TestBoard_7953/`: manual/Auto-1 streams, parking, execution order, timestamps and transport.
- **F3.** [GUI board profile](../../../config/boards/profiles/testboard_7953.json), `config/boards/profiles/testboard_7953.json`; [driver](../../../Arduino_Sketches/TestBoard_7953/src/Ads7953Adc.cpp); [SPI implementation](../../../Arduino_Sketches/TestBoard_7953/src/SpiController.cpp).
- **F4.** [Shared-node hardware clarification](../../testing/results/testboard-7953/gui-noise-analysis/channel11_four_adc/report.md), `docs/testing/results/testboard-7953/gui-noise-analysis/channel11_four_adc/report.md`; [manual ghosting observations](../../testing/results/testboard-7953/TESTBOARD_7953_MANUAL_GHOSTING_RESULTS.md). These identify the common MXO / OPA365 bias topology; manual ghosting percentages are not timing calibration.

### Measured evidence

- **M1.** [Live USB full production matrix report](../../testing/benchmarks/testboard-7953/TESTBOARD_7953_LIVE_USB_FULL_MATRIX_RESULTS.md), `docs/testing/benchmarks/testboard-7953/TESTBOARD_7953_LIVE_USB_FULL_MATRIX_RESULTS.md`; [saved CSV](../../../Arduino_Sketches/TestBoard_7953/benchmarks/results/live_usb_production_adc_10_20/benchmark_results.csv), `Arduino_Sketches/TestBoard_7953/benchmarks/results/live_usb_production_adc_10_20/benchmark_results.csv`. Six 20 MHz full-route configurations, three repetitions each; acquisition/period table reproduced from repetition rows.
- **M2.** [Historical phase results](../../testing/benchmarks/testboard-7953/TESTBOARD_7953_PHASE_PROFILING_RESULTS.md), `docs/testing/benchmarks/testboard-7953/TESTBOARD_7953_PHASE_PROFILING_RESULTS.md`. Separate earlier diagnostic firmware; retained for phase attribution only.

### Primary component references

- **D1.** [Texas Instruments ADS7953 family datasheet, SLAS605C](https://www.ti.com/lit/ds/symlink/ads7953.pdf), July 2018; sections 7.6, 7.9, 8.1 and Figure 1. Checked 21 October 2026. The event definitions and acquisition minimum are component specifications, not measured firmware delays.
- **D2.** [Texas Instruments OPA365 datasheet, SBOS365G](https://www.ti.com/lit/ds/symlink/opa365.pdf), May 2023; section 7.6. Checked 21 October 2026. Full board settling and sensor leakage remain unmeasured in this report.

### Interpretation of transmitted timestamps

`block_end_us - block_start_us` is acquisition duration. `avg_dt_us` is that duration divided by route count, rounded to integer us and saturated at 65535. It is an aggregate throughput statistic, not a sample aperture or a uniform channel spacing. Start-to-start deltas between received frames can span intentionally discarded acquisitions. `sampling_period_max_us` includes every completed sweep but gives only an aggregate maximum, not channel edge timestamps. Unsigned timestamps wrap at 2^32 us (about 71.6 minutes).
