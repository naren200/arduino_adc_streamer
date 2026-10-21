# TestBoard_ADC124 capture timing report

21 October 2026 | Firmware review and existing measurements | Report 2 of 2

## What the four requested times mean

This board connects four sensor channels at once: each array's four external MUXes share address and enable controls. Each MUX output has an OPA365 buffer and a bias resistor; the ADC subsequently reads the buffers in sequence. A sensor therefore leaks through its bias resistor while earlier ADC inputs are sampled, even though its own sample has not started.

| Requested quantity | Finding for this board | Confidence |
| --- | --- | --- |
| Time connected to MUX output / op-amp | Physical EN/address connection until the bank next disconnects or changes address | Sequence established; numerical budget on pages 3-4 |
| Time ADC takes the sample | Nominal 3 SCLK acquisition cycles, then 13 conversion cycles | Datasheet; actual hold edge includes clock/CS phase |
| Connection to retained sample | Turn-on guard + sensor settle + preceding words + retained repeat's acquisition | Route-dependent; array B waits for array A in shared MUX mode |
| Time disconnected | Next physical connection minus previous physical disconnection | Boundary routes can stay connected across sweep and USB work |

**Main result:** settling is a bank-level wait, not the complete per-channel pre-sample leakage time. ADC input/MUX number, repeat, scan mode, sparse routing and sweep boundaries all change the four quantities. Current firmware has no ADC conversions during optional VMID visits. No new hardware measurements, firmware changes, or API functions were made for this report.

## Hardware and settings

| Item | Current repository value |
| --- | --- |
| Controller / converters | Teensy 4.1; two ADC124S101; one shared SPI bus |
| Array A / array B switches | Four TMUX1108 / four TMUX1308A; both banks supplied at 3.3 V |
| MUX outputs | MUX1-4 -> OPA365 buffers -> ADC IN1-4 respectively |
| Shared bias values A | MUX1-4: 470 kohm, 1 Mohm, 470 kohm, 1 Mohm |
| Shared bias values B | MUX1-4: 1 Mohm, 470 kohm, 1 Mohm, 470 kohm |
| Sensor addresses | 0-5 on every MUX; address 6 only on MUX4; 25 routes per array |
| Hardware reference selections | Address 7 on all MUXes; address 6 on MUX1-3 also wired to VMID |
| ADC span / VMID | Fixed 3.3 V / 1.65 V; bias leakage toward VMID |
| Firmware and GUI defaults | ADC scan order, LPSPI, burst frames, 16 MHz requested, repeat 1, sensor settle 5 us, VMID off; VMID delay follows sensor delay |

The local VMID-input capacitor is 150 pF. It is not permanently across the common sensor node; exact placement relative to the nearby 100 ohm resistor is unconfirmed. Do not invent that network's net order or use 150 pF as the sensor capacitance. [F1, F2, F4]

<!-- pagebreak -->

## Capture process, step by step

| Step | Current behavior | Time / count |
| --- | --- | --- |
| 1. Prepare run | Cache route groups; link the final command of each group to the next group's first ADC input; park both banks | Once per run; outside acquisition timestamps |
| 2. Initialize selector | If unknown/different, one discarded conversion per active ADC selects its first input | At most 2 words at run start; no hidden prime per group |
| 3. Begin acquisition | Record start; open one LPSPI session | Included in acquisition |
| 4. Disconnect old address | Drive EN inactive | Additional programmed 0.200 us turn-off guard before address writes |
| 5. Change address / reconnect | Cached address writes, then EN active | GPIO duration unmeasured; programmed 0.200 us guard after enable |
| 6. Sensor settling | Wait once for participating bank(s) | S = muxsettle, 0-1000 us; skipped if address already enabled/unchanged |
| 7. Read sensor group | Keep external address fixed, clock populated MUX inputs and repeats | 16 clocks per conversion; only final repeat retained |
| 8. Optional VMID visit | Disable, address 7, enable, guard, VMID wait | V = vmidsettle; follows S by default; no ADC clocks |
| 9. Finish acquisition | Restore ordinary SPI state; record end | End includes all in-sweep switching, waits and reads |
| 10. Encode and enqueue | Build frame; enqueue complete frame or discard if busy | Outside acquisition; 114 bytes for 50 routes |
| 11. Stop / failure | Park both banks enabled at address 7 | Outside ordinary steady-state sweep; unselected arrays disabled during runs |

The 200 ns pre/post guards are firmware allowances, not measurements of switch turn-on/off. A zero analog wait retains both guards. Cached same-address selections can skip switching and settling entirely. [F1, F2, F3]

## Physical execution order

**ADC mode:** A address 0, ADC IN1-4; A address 1, IN1-4; ... A address 6, IN4; then all B groups. Each bank has independent settling. A bank is not parked merely because the other ADC owns SPI.

**MUX mode:** enable the participating banks at address 0, share one settle wait, read A IN1-4, then B IN1-4; repeat addresses 1-5; at address 6 read A IN4 then B IN4. Both ADCs remain sequential on one bus. B sensors are connected while A is read. Sparse phases include only the banks present at that address. [F2]

Burst framing keeps CS low across all conversions in an ADC/address batch. Single framing pays CS setup/high allowances per word. The input selector written in each word chooses the next conversion. Prepared lookahead preserves it across VMID visits and sweep boundaries; there is no ADS7953 two-frame pipeline. [F2, F3]

<!-- pagebreak -->

## ADC sampling window and per-channel equations

ADC124S101 acquisition nominally lasts 3 SCLK cycles; conversion occupies the next 13. With mode-0 SCLK low before CS, CS falling begins tracking and hold follows three physical falling clock edges. Burst words resume tracking at the preceding word boundary; clock gaps can stretch a window. Identify hold from actual CS/SCLK edges. [D1: pp. 5, 16-17]

| Actual SCLK | One clock | Nominal acquisition | Conversion | 16-clock word |
| --- | ---: | ---: | ---: | ---: |
| 8 MHz | 0.125 us | 0.375 us | 1.625 us | 2.000 us |
| 16 MHz | 0.0625 us | 0.1875 us | 0.8125 us | 1.000 us |

Let E be a bank's enable edge; t_on = E + d_on be physical connection; H the hold edge of the retained repeat; D the next disable edge; t_off = D + d_off. Then:

```text
T_connected    = t_off - t_on
T_pre_sample   = H - t_on
T_ADC_track    = H - track_start
T_disconnected = next_t_on - t_off
```

For a new-address burst, let g_on=0.200 us, c_setup=0.100 us, W=16/f, a=3/f, r=repeat, and b the number of requested MUX inputs earlier in this ADC's group. With ideal uninterrupted words:

```text
H - E = g_on + S + T_prior_ADC_batches + c_setup + (b*r + r-1)*W + a
T_pre_sample = (H - E) - d_on
```

`T_prior_ADC_batches` is zero in ADC mode and for A in full shared MUX mode. For B in a full address-0..5 MUX phase it includes A's 4r words, A's setup/high allowances, and software time between batches. It is not another settle wait. In single framing, earlier word costs also include their per-word setup/high overhead.

### Numerical budget: S=5 us, repeat 1, 16 MHz, burst

The table measures enable-command to hold, with only configured guards and ideal clocks; GPIO/API/interrupt overhead is omitted. Both-bank enable skew is assumed zero. Subtract the corresponding switch turn-on delay to obtain physical connection-to-hold.

| Scan / ADC | MUX1 | MUX2 | MUX3 | MUX4 |
| --- | ---: | ---: | ---: | ---: |
| ADC mode, A or B | 5.4875 us | 6.4875 us | 7.4875 us | 8.4875 us |
| MUX mode, A | 5.4875 us | 6.4875 us | 7.4875 us | 8.4875 us |
| MUX mode, B | 9.6875 us | 10.6875 us | 11.6875 us | 12.6875 us |

Each extra repeat adds `(b+1)*W` to retained hold time within that ADC's batch. B also waits for A's added repeats in MUX mode. At address 6 only MUX4 is read, so b=0 and B waits for one A word. These are calculated allowances, not measured per-channel times.

<!-- pagebreak -->

## Connected dwell and sweep-boundary effects

All four MUXes in a bank connect the same address together, including sensor paths not requested for ADC readout. At addresses 0-5, the four sensors share approximately the same connected duration, but have different pre-sample durations. Sparse ADC selection does not independently disable unused MUX outputs. [F2, F3]

For a normal full four-input group followed immediately by an address switch, the enable-to-next-disable budget is:

```text
ADC mode: g_on + S + (c_setup + 4*r*W + c_high)
MUX mode: g_on + S + 2*(c_setup + 4*r*W + c_high)
T_connected = enable_to_disable + d_off - d_on
```

At 16 MHz, S=5 us, r=1, c_setup=c_high=0.100 us, these budgets are **9.400 us in ADC mode** and **13.600 us in full shared MUX mode**. Add measured software gaps and bank skew. This applies to non-boundary groups; it is not a constant for every channel.

### VMID on

After each phase, the participating banks disconnect sensors and select address 7 before waiting V us. This ends the sensor connection before the VMID wait. Thus V extends off-time/revisit time but does not directly extend that just-finished sensor's connected interval. It can still affect subsequent analog initial conditions.

### VMID off

No end-of-sweep or ADC-ownership park is performed. In ADC mode, A stays at its last address throughout B's scan; B stays at its last address throughout the next A scan. With full routing this is address 6: MUX4's sensor stays connected much longer than ordinary sensors. MUX1-3 are connected to hardware VMID during this address-6 dwell.

In shared MUX mode, both banks remain at address 6 through encoding, USB enqueue and foreground work until the next sweep switches them to address 0. MUX4's sensor therefore includes that gap in its connected time. If a sparse bank uses only one external address with VMID off, its sensors can stay connected continuously across sweeps; unchanged selections skip the settle wait. Even unrouted sensors on that selected address can remain connected.

### Disconnected time

For one contiguous connection per periodic visit, `T_disconnected = T_revisit - T_connected`. Compute the route's physical revisit and dwell; never subtract the ADC's 0.1875 us acquisition window instead of its full sensor-connected time. For continuous holding, off-time is zero and pre-sample age grows from the original connection.

As an arithmetic illustration only, a 100 us physical revisit with the page-3/4 ordinary ADC-group budget at S=1 us would have 5.400 us enable-to-disable dwell and about 94.600 us off-time, before switch delays and software overhead. This is not a measured per-route result from the 100 us G4 sweep.

<!-- pagebreak -->

## Full-sweep timing budget for current firmware

Both full arrays contain 50 useful routes, 14 ADC/address batches, and 50r conversion words. VMID on adds **zero** conversion words. Burst framing uses 14 CS frames; single framing uses 50r. At repeat 1 this saves 36 pairs of configured setup/high delays, or 7.200 us, before engine overhead. [F1, F2, F3]

| Configured contribution, full arrays / repeat 1 | ADC order | Shared MUX order |
| --- | ---: | ---: |
| 16 MHz clocks, 50 words | 50.000 us | 50.000 us |
| Burst setup/high: 14 x 0.200 us | 2.800 us | 2.800 us |
| Sensor waits, if all addresses change | 14S | 7S |
| VMID waits when enabled | 14V | 7V |
| Switching guards, VMID off | 14 x 0.400 = 5.600 us | 7 x 0.400 = 2.800 us |
| Switching guards, VMID on | 28 x 0.400 = 11.200 us | 14 x 0.400 = 5.600 us |

Shared mode overlaps switching guards and analog waits, not ADC words. ADC-mode guards are independent; full MUX phases pay the shared guard once after the last disable and once after the last enable. These budgets exclude register writes, sessions, batch handling, polling, interrupts, encoding and foreground work. They are not complete timing predictions and do not apply unchanged to sparse/same-address captures.

### Compare with latest user-reported LPSPI rates

20 October 2026 observations: both full arrays, ADC order, repeat 1, no VMID ADC conversions. The G0/G4/G5 source gives rates but no raw edge traces, raw sweep timestamps or firmware image identity. For budgeting only, use current default 16 MHz and burst framing; that exact wire clock/framing remains an assumption for these observations. [M1]

| Condition | Sensor S / VMID V | Reported rate per route | Reciprocal rate | Configured budget | Unassigned residual |
| --- | --- | ---: | ---: | ---: | ---: |
| G0: VMID off | 5 us / none | 6,736 Hz | 148.456 us | 128.400 us | 20.056 us |
| G4: VMID on | 1 us / 0 us | 10,000 Hz | 100.000 us | 78.000 us | 22.000 us |
| G5: VMID on | 1 us / 5 us | 5,880 Hz | 170.068 us | 148.000 us | 22.068 us |

The G5-G4 period increase is **70.068 us**, close to 14 x 5 us = **70.000 us** of added waits. This supports the ADC-order wait count. It does not isolate individual MUX switching, sample aperture or software phase costs. The reported rates are rounded observations; displayed reciprocal precision is arithmetic only.

At 8 MHz, the 50 repeat-1 words take 100 us rather than 50 us; at repeat 3 and 16 MHz they take 150 us. Analog wait counts stay unchanged for the same full routing. Engine overhead and actual divider selection still need their own measurements.

<!-- pagebreak -->

## Historical measurements: keep firmware histories separate

The 14 October selector-lookahead run provides raw-backed production LPSPI evidence with 50 useful routes, repeat 1, burst, requested 16 MHz and 5 us sensor/VMID waits. Three repetitions had 1 s warm-up and 15 s measurement. Its historical VMID-on path clocked 56 extra VMID words, unlike today's wait-only path. [M2]

| Historical mode | VMID | Sustained Hz per route, reported | SPI words per sweep |
| --- | --- | ---: | ---: |
| ADC | Off | 6,748.7 | 50 |
| ADC | On | 3,332.8 | 106 |
| MUX | Off | 9,213.3 | 50 |
| MUX | On | 4,510.5 | 106 |

The historical off path supports the scale of G0's 6,736 Hz, but captures differ. Do not use the old VMID-on rates to calculate present wait-only dwell. Earlier timing tools also counted per-batch selector primes and/or five-word VMID scans; those counts are historical. The current prepared plan has neither in steady-state capture.

That historical series reported 7,091,330 acquired sweeps, 7,090,915 received frames and 415 accounted USB discards, with zero transfer/data/USB-write errors. It establishes transport and aggregate timing under the recorded configuration; it does not establish per-channel analog connection edges. The present review did not re-decode all those raw files.

## Component timing relevant to physical boundaries

At 3.3 V, TMUX1108 enable-on is 14 ns typical / 25 ns maximum; enable-off is 7 ns typical / 12 ns maximum, with the datasheet's 200 ohm, 15 pF load conditions. These maxima cover the stated temperature ranges. Its off-source leakage is specified with voltage and temperature dependence. [D2: section 6.6]

TMUX1308A at 3.3 V specifies 26 ns typical / 40 ns maximum enable-on and 40 ns typical / 65 ns maximum enable-off at 25 C, using 10 kohm / 50 pF. Full-temperature maxima rise to 42 ns and 70 ns. Different test loads preclude treating the two typical numbers as measured bank skew. [D3: section 6.8]

For later off-time leakage calculations, the 3.3 V source-off leakage magnitudes are: TMUX1108, 1 pA typical / 50 pA maximum at 25 C, up to 500 pA over -40 to 125 C; TMUX1308A, 1 nA typical / 25 nA maximum at 25 C, up to 800 nA over -40 to 125 C. These use different datasheet terminal-voltage conditions and are not measured board currents. Preserve sign/voltage/temperature dependence rather than assuming constant discharge to ground. [D2, D3: section 6.6]

The 200 ns programmed guards exceed those tabulated switching delays, but full analog settling depends on sensor capacitance, resistance, buffer behavior and layout. The guards occur inside connection/leakage history: the post-enable guard is not all disconnected time. OPA365's typical 300 ns settling specification is for 0.01%, 5 V supply, gain 1 and a 4 V step; it is not a verified board settling time. [D4]

CS setup/high guards are each 100 ns in firmware. Actual first-clock phase, burst FIFO gaps, DMA byte gaps and inter-batch time must be measured. The converter has no returned channel tag; route correctness needs decoded DIN plus a known analog input, not response values alone.

<!-- pagebreak -->

## Leakage interpretation and inputs for later functions

While connected, the bias resistor pulls toward VMID. Disconnected sensor leakage toward ground and MUX off-source leakage are distinct contributions; off leakage need not discharge in only one direction. A first-order sensor model is:

```text
connected:    dV/dt = -(V-Vmid)/(R_bias*C_eff) - V/(R_sensor*C_eff) + I_on/C_eff
disconnected: dV/dt = -V/(R_sensor*C_sensor) + I_off/C_sensor
```

Use per-MUX resistor metadata, sensor capacitance/insulation, switching charge and buffer-input parasitics. ADC sampling capacitance is behind OPA365 and must not be directly added to the high-impedance sensor node. Connection charge sharing and the shared-node voltage left by previous sensors or VMID can change the initial voltage; a simple decay factor alone will not predict all ghosting.

| Input for future functions | Present evidence | Missing / must be recorded |
| --- | --- | --- |
| Route / MUX / array order | Prepared plan and payload routes | Exact effective configuration per run |
| Sensor and VMID delay | muxsettle / vmidsettle and follow-sensor state | Measured delay-call / GPIO overhead |
| Sample offsets | 16-clock words; nominal 3-cycle acquisition | Real clock, first-CS phase, hold edges, FIFO/DMA gaps |
| Sensor dwell and off-time | EN/address sequence; shared-bank topology | Per-bank enable skew, switch delays, boundary cadence |
| Analog leakage parameters | Bias values and component datasheets | Actual sensor C, insulation R, assembly tolerance, temperature |
| Local VMID network | 150 pF near VMID input; address-6 ties on MUX1-3 | Confirm resistor/capacitor net connectivity before modeling |
| Transport effects | Nonblocking whole-frame discard policy | All-sweep cadence, including discarded frames |

## Focused measurement needed for exact per-channel timing

1. Record firmware image/source identity, protocol version, exact routes, scan order, burst/single framing, engine, repeat, S, V, VMID and effective clock request. Preserve address-6 wiring as hardware metadata.
2. Capture EN pins 0/32, address pins 1/2/3 and 29/30/31, SCLK13, CS9/24 and DIN11. Resolve 100/200 ns guards and actual 8/16 MHz clocks. Decode retained repeat and selector lookahead for each group.
3. Observe a known sensor input, MUX common/buffer input and buffer output with low-capacitance probes. Correlate physical make/break and settling with EN and hold edges; account for probe loading.
4. Test ADC/MUX modes, VMID off/on, V=0/5 us, S=1/5 us, repeats 1/3, sparse and single-address routes. Include MUX4 address 6, across-array idle time and sweep/USB boundaries. Report typical and tail distributions.
5. Fit phase overhead only from matching builds and settings. Validate per-channel leakage predictions using measured sensor C/R and a repeatable injected signal before implementing future firmware/API functions.

The future result should distinguish physical connection, ADC track start/end, retained hold, result-ready and disconnection; include the active bank's shared history and a continuous-hold state. No calculation functions are implemented at this stage.

<!-- pagebreak -->

## Sources and review scope

Reviewed repository HEAD: `bc74949548091bb43aaffdfc37b169fce83f3a21`. Initial working tree was clean. The adjacent source manifest preserves reviewed-file SHA-256 hashes. No serial port was opened and no firmware uploaded. Current code is authoritative for operation counts; older reports remain evidence only of their recorded firmware/configurations.

### Firmware and hardware sources

- **F1.** [Hardware constants](../../../Arduino_Sketches/TestBoard_ADC124/include/ConfigurableParameters.h), `Arduino_Sketches/TestBoard_ADC124/include/ConfigurableParameters.h`: pin map, polarity, resistor values, delay/clock constants.
- **F2.** [Acquisition application](../../../Arduino_Sketches/TestBoard_ADC124/src/main.cpp), `Arduino_Sketches/TestBoard_ADC124/src/main.cpp`: prepared groups/phases, selector lookahead, wait-only VMID, start/stop and timestamps. Read this for current behavior; older README text still mentions VMID conversions in places.
- **F3.** [ADC/MUX driver](../../../Arduino_Sketches/TestBoard_ADC124/src/Adc124s101.cpp) and [SPI controller](../../../Arduino_Sketches/TestBoard_ADC124/src/SpiController.cpp), `Arduino_Sketches/TestBoard_ADC124/src/`; [GUI profile](../../../config/boards/profiles/testboard_adc124.json), `config/boards/profiles/testboard_adc124.json`.
- **F4.** [Board designer hardware clarifications](../../testing/results/testboard-adc124/mux_type_comparison_2026-10-15/README.md), `docs/testing/results/testboard-adc124/mux_type_comparison_2026-10-15/README.md`: buffer isolation, 3.3 V MUX supply, local 150 pF, uncertain resistor/capacitor order, address-6 VMID ties.

### Measured evidence

- **M1.** [20 October ghosting comparison notes](../../testing/results/testboard-adc124/ghosting_manual_2026-10-20/README.md), [engine/rate CSV](../../testing/results/testboard-adc124/ghosting_manual_2026-10-20/engine_rate_comparison.csv) and [rerun provenance](../../testing/results/testboard-adc124/ghosting_manual_2026-10-20/lpspi_rerun_source.json), `docs/testing/results/testboard-adc124/ghosting_manual_2026-10-20/`. User-reported LPSPI rates, not new instrumented captures.
- **M2.** [14 October selector-lookahead LPSPI report](../../testing/results/testboard-adc124/selector_lookahead_lpspi_2026-10-14/README.md) and [capture metrics](../../testing/results/testboard-adc124/selector_lookahead_lpspi_2026-10-14/capture_metrics.csv), `docs/testing/results/testboard-adc124/selector_lookahead_lpspi_2026-10-14/`. Historical VMID conversion path differs from current code.

### Primary component references, checked 21 October 2026

- **D1.** [TI ADC124S101, SNAS283D](https://www.ti.com/lit/gpn/ADC124S101), March 2013; pp. 5, 16-17: clock-cycle acquisition/conversion and next-input selection.
- **D2.** [TI TMUX1108, SCDS388B](https://www.ti.com/lit/ds/symlink/tmux1108.pdf), February 2024; section 6.6, 3.3 V timing and leakage.
- **D3.** [TI TMUX1308A, SCDS477A](https://www.ti.com/lit/ds/symlink/tmux1308a.pdf), June 2025; sections 6.6 and 6.8, leakage and switching conditions.
- **D4.** [TI OPA365, SBOS365G](https://www.ti.com/lit/ds/symlink/opa365.pdf), May 2023; section 7.6, component settling conditions.

### Interpretation of transmitted timing

Wire start/end cover acquisition, including in-sweep GPIO settling and VMID parking, but exclude USB enqueue. `avg_dt_us` is the rounded acquisition duration / route count, not the ADC track window or uniform channel spacing. A received-frame gap may contain discarded sweeps that still connected and sampled sensors. Sampling-period counters cover every completed sweep but do not expose per-channel edges. Unsigned microsecond timestamps wrap at 2^32 us (about 71.6 minutes).
