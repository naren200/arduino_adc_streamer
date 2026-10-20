# TestBoard ADC124

Teensy 4.1 PlatformIO firmware and standalone benchmarks for two 25-channel
sensor arrays, two ADC124S101 converters, and eight external 8:1 MUXes.
This project adapts the USB envelope and SPI engines from TestBoard_7953.
Acquisition is implemented for the external GPIO MUX topology; ADS7953
Auto-1 sequencing and its two-frame response pipeline do not apply.

```powershell
pio run -d Arduino_Sketches/TestBoard_ADC124
pio run -d Arduino_Sketches/TestBoard_ADC124 -t upload
```

The project pins Teensy platform 6.0.0. Hardware constants are in
[`include/ConfigurableParameters.h`](include/ConfigurableParameters.h).
The confirmed reference/supply VA is fixed at **3.3 V**, Vmid is **1.65 V**,
and MUX1–MUX4 connect to ADC IN1–IN4 in order on each array. Firmware emits
raw 12-bit codes; voltage conversion is `code * 3.3 / 4096`. The ADC uses VA
as its reference; `ref` cannot change the electrical input span.

## Changes carried over from TestBoard_7953

The USB and acquisition improvements from ADS7953 commits `92be24d` and
`d48b4fe` are adapted for this board:

- The build applies the audited USB transmit/flush race repair to a **build-local
  copy** of the pinned Teensy core. The installed framework is unchanged. An
  unexpected core hash fails the build until reviewed. Build through PlatformIO;
  an Arduino IDE build without the middleware does not include this repair.
- Completed sweeps use an atomic nonblocking USB enqueue. When the complete
  frame cannot fit immediately, it is discarded and sampling continues with
  current data. No acquisition backlog is stored. USB/Windows may still deliver
  previously accepted bytes late. Unexpected write errors stop the run.
- Address groups, pipeline commands, discarded repeats, payload destinations,
  and address phases are prepared once per run. LPSPI retains one
  shared-bus transaction per sweep. Continuous-CS bursts use one CS frame per
  prepared ADC/address batch; `spiframe single` retains per-conversion CS frames.
  CS GPIO registers are cached; receive readiness **and final transfer completion**
  are required before CS release. Session setup uses a known 16-bit command
  instead of asynchronous TCR readback. SPI state is restored before USB enqueue
  and on failure. The 100 ns CS setup/high allowances are retained.
- Engine-specific conversion loops avoid per-word mode dispatch. LPSPI waits
  use the cycle counter with a 1 ms bound and foreground servicing every 10 us
  during extended waits; interrupts remain enabled. DMA retains EventResponder
  servicing. LPSPI bursts queue commands and drain responses within the actual
  FIFO capacities, waiting for final completion and idle before CS release.
  MUX switching uses cached register-write sequences; coordinated phases share
  switching guard waits and retain the full configured analog settling delay.

These changes do not import ADS7953 Auto-1 masks, its two-frame response delay,
20–30 MHz clock range, dual-bus overlap, or its ADC/channel wiring. ADC124 still
uses one shared bus, one-frame input selection, the original triple routes,
opposite MUX EN polarity, and fixed 3.3 V reference.

The binary frame is unchanged. Status now reports `protocol_version=3` and
`usb_stream_policy=discard_if_busy`. `sampling_sweeps`, `usb_frames_sent`,
`usb_frames_discarded`, `sampling_period_max_us`, and `sampling_period_over_1ms`
reset on each accepted run. `sweep_count` remains the lifetime completed-sweep
counter (including discarded sweeps). `usb_write_errors` is cumulative. On a
successful run, sampling equals sent plus discarded; only sent frames should
equal received frames. Use the updated runner to account for this policy.

## Protocol-3 scan modes

`scanorder adc*` is the former protocol-1/2 `mux` behavior: finish the populated
address groups of array A, then array B. The wire order is array, external
address, ADC input/MUX. Each address applies one analog settling wait to that
bank before its populated ADC inputs are read.

`scanorder mux*` now disables the changing participating banks, waits after the
last disable, updates their address pins, enables them and waits after the last
enable. Every bank receives its full switching guards, then **one shared
analog wait**. It reads ADC A's populated inputs then ADC B's on the shared SPI
bus. The wire order is external address, array, ADC input/MUX. Sparse routes
and single-array selections include only the banks/ADC inputs needed at each
address. Full arrays use inputs IN1..IN4 at addresses 0..5 and IN4 only at 6.

`channel`, `interleaved` and old scan-order aliases are rejected. The meaning
of `mux` changed explicitly at protocol 3: old captures must use their saved
protocol version and `payload_routes`, not reinterpret an old name. The runner
uses the new names. Original firmware can be tested explicitly with
`--scan-order adc --spi-frame single`, mapped to its former `scanorder mux`.
It cannot perform the new shared-bank mode. Saved capture metadata includes
protocol version, semantics and exact physical payload labels. There is still
no desktop ADC124 profile or persisted GUI acquisition setting: the existing
ADS7953 GUI's scan-order vocabulary is separate and unchanged.

CS rising makes this ADC's DOUT high impedance; its analog inputs are driven by
separate unity buffers. No SPI isolation requires parking the external bank
merely to read another ADC input or select the other chip. Protocol 3 preserves
parking at startup, run preparation, stop and failure, and optional VMID work.
It removes the copied ownership/end-of-sweep parking. EN is cached and toggled
only when an address change needs break-before-make, or explicitly disabling/
re-enabling an inactive bank. GPIO register masks are cached and unchanged
address bits are not rewritten. Address SET/CLEAR masks are grouped by GPIO
port, including signals from different banks on GPIO9. Write sequences are
precomputed for every address history; sparse banks need not start at the same
address. The 200 ns turn-off and turn-on guards remain and overlap in shared
phases. The cache uses 48,096 bytes on ARM to avoid mask assembly during scans.

With VMID on, each phase switches its participating banks to address 7 before
one corresponding VMID settling wait, without starting any ADC transfer.
Shared mode thus shares both sensor and VMID waits. With VMID off a bank may stay enabled at its last sensor address
between phases/sweeps; identical addresses skip redundant switching and waits.
Stop always leaves both banks enabled on VMID, with both ADC CS lines high.

TI SNAS283D pp.16-17 and 19 explicitly allow consecutive 16-clock conversions
with CS held low. Each sensor burst stays at one fixed external address.
The last command of each batch selects the first ADC input of the next batch
on that ADC, across wait-only VMID visits, sparse routes and sweep wraparound. No selector
prime is discarded inside a sweep; only the last requested repeat is stored
for each route. Blocking keeps one SPI
transaction/CS frame per batch; DMA launches one 2N-byte transfer from persistent
aligned buffers; LPSPI retains its sweep session and CS across all batch words.
It queues prepared words within TX/RX FIFO capacity, drains ordered responses,
checks error/timeout conditions and requires all responses, empty TX, transfer
completion and idle before final CS release. Individual-word reference frames
still require fresh completion for each word.
CS setup and high allowances are paid once per burst; timeout/error paths
release CS, restore SPI state and park safely. Interrupts remain enabled.

For full arrays at repeat 1, both modes use 50 words/14 sensor batches with
VMID either off or on. Pure configured CS-allowance savings versus single
framing are `(50-14)*0.2 = 7.2 us` in both cases. These are calculations,
not measured savings. The prior lookahead revision used 106 words/28 batches
with VMID on; removing its VMID ADC scans saves 56 conversion words and 14
CS frames per full sweep. Nominal clock-only savings are 56 us at 16 MHz or
112 us at 8 MHz; MUX switching guards and configured waits are unchanged.
Four-channel groups use four conversions; address 6 uses one IN4 conversion.
At nominal 16/8 MHz those batches occupy 4/8 us and 1/2 us of clocking,
respectively, before peripheral/software overhead.
Historical reports retain the word counts and timings of their captured revisions.
ADC mode uses 14/28 settling waits; shared MUX uses 7/14. Actual GPIO change
counts depend on routes, current cached state and VMID; analog delays are not
shortened to compensate for CPU overhead.

Current revision `adc124-vmid-wait-only-v1` changes VMID insertion to GPIO
switching and settling only. Status reports `vmid_scan_policy=wait_only`,
`adc_selector_policy=lookahead` and `adc_batch_prime_frames=0`; the ADC's
one-conversion selector latency (`adc_pipeline_frames=1`) is unchanged.
The previous `adc124-selector-lookahead-v1` scanned four ADC inputs at VMID.
The preceding revision `adc124-vmid-settle-v1` retains the FIFO/GPIO optimization
from `adc124-fifo-grouped-gpio-v2`; protocol remains 3.
For full arrays, coordinated MUX guard allowance is 2.8 us off / 5.6 us on,
versus 5.6 / 11.2 us with separate bank switching. These are configured
allowance estimates, not measured GPIO propagation or total speed gains.

## Selector lookahead validation

Selector lookahead is checked by the native model across both scan orders,
all SPI engines, single/burst framing, repeats 1..3, full/sparse/single-array
plans, VMID overrides and consecutive sweeps. An additional 48 cases exercise
8/16 MHz requests and explicitly verify four-word groups, the one-word IN4
group, and selector wraparound. Each capture checks exact conversion counts
and tagged physical payloads, so a hidden prime or incorrect selector fails.
Both production and diagnostic builds must pass before upload. The analog
effects of this shorter conversion history have not yet been measured on the
board. Hardware validation: upload the production build, run matched ADC/MUX
captures at 8/16 MHz, repeat 1, sensor settling 5 us and VMID off/on; verify
status policy, error counters, complete frames and near-VMID bias. Repeat
VMID-on captures with VMID delays 5/1/0 us and compare noise with the preceding
revision. Confirm individual channel identity with known input stimulation
or a logic analyzer; disconnected near-VMID readings cannot establish it.

## Independent VMID settling

`muxsettle N*` sets sensor settling. `vmidsettle default*` (the boot default)
follows that value dynamically. `vmidsettle N*` sets a fixed VMID override in
0..1000 us; later sensor-delay changes do not replace it. Set `default` again
to resume following. Status reports effective `vmid_settle_us` and
`vmid_settle_mode=follow_sensor|explicit`.

The VMID delay applies to optional wait-only VMID visits and configured VMID
parking at run preparation/stop/failure. Zero omits the extra analog wait;
the off/on guards remain, but there are no VMID ADC conversions during a sweep.
MUX mode shares the VMID wait after both banks complete switching.
Sensor settling and payload width stay unchanged. Startup uses inherited 5 us.

The requested wait is an added delay, not an exact dwell-time guarantee:
GPIO/loop overhead remains. At sweep boundaries, encoding/USB scheduling can
extend the visit. In ADC scan order, a bank may remain parked while the other
array is scanned. MUX order switches both participating banks together before
the shared wait. This change removes ADC scan time; it does not alter that scheduling.

For pressure/amplitude and ghosting checks, upload this production revision
and verify `vmid_scan_policy=wait_only`. Compare VMID off with VMID on at
0/1/5 us, keeping sensor delay, clock, scan order, repeat count, strip placement
and plotting/filter settings fixed. Record the pressed sensor and unpressed
routes together, retain absolute amplitudes as well as ghost/pressed amplitude
ratios, and save status with each capture. Real pressure and channel stimulation
are still needed to establish the analog effect; software checks do not show
whether amplitude or ghosting improves.

```text
muxsettle 5*
vmidsettle 0*        # sensor 5 us, VMID 0 us (guards remain)
vmid true*
vmidsettle 1*        # sensor 5 us, VMID 1 us
vmidsettle 5*        # explicitly match 5 us
vmidsettle default*  # follow future muxsettle changes
```

## Optional phase profiling

The normal `teensy41` environment compiles out profiling probes and storage.
Build and upload the diagnostic environment explicitly:

```powershell
pio run -d Arduino_Sketches/TestBoard_ADC124 -e teensy41_profile
pio run -d Arduino_Sketches/TestBoard_ADC124 -e teensy41_profile -t upload
```

It starts with profiling off. While stopped, `profile on*` / `profile off*`
toggle profiling; each run resets statistics. A profile command arriving during
capture stops the stream at a frame boundary and is rejected without changing
the setting. Summaries appear only in stopped `status*` replies. The runner's
`--profile on` saves phase cycles, bounded histograms, capacity observations,
and longest-period snapshots in each run's `firmware_profile.jsonl`. Default
`--profile off` normalizes diagnostic builds and remains compatible with the
original protocol-1 firmware.

`prepare` includes sensor and VMID address switching, guards and settling;
`transfer` includes their ADC batches. Protocol-2 profiles included VMID
switching/settling in `transfer`, so these scopes differ between versions.
`validate` measures response checks nested
inside transfer; phases are not all disjoint. Statistics cover warm-up and
measurement, include interrupt time, and add instrumentation overhead. Profile
observations are diagnostic; compare throughput using production builds.

## Connections

One physical bus uses Teensyduino **SPI**, not SPI1: MOSI 11, MISO 12, SCK 13.

| Array / ADC | CS | MUX type | EN | A0 | A1 | A2 | Enabled level |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |
| 1 / ADC1 | 9 | TMUX1108 | 0 | 1 | 2 | 3 | HIGH |
| 2 / ADC2 | 24 | TMUX1308A | 32 | 29 | 30 | 31 | LOW |

Both ADCs share that bus and are always transferred sequentially, including
DMA and direct LPSPI. Neither engine can acquire the arrays simultaneously.
Each array's four MUXes share address and enable GPIOs. Switching an address
changes all four ADC input signals at once. The firmware disables the MUX
group, waits 200 ns, changes its address pins, enables it, waits another
200 ns and then the configured analog settling delay. The default delay is
5 us; it is a starting value to characterize, not a measured settling time.

The ADC uses 16-clock MSB-first mode-0 transactions with 100 ns CS setup and
CS-high allowances. The allowed requested SPI clock is 8–16 MHz, default
16 MHz. TI specifies AC performance in this clock range. The clock requested
from Teensy may be rounded to its available divider; measure actual SCLK.

| MUX / ADC input | External input 0–4 | Input 5 | Input 6 | Input 7 |
| --- | --- | --- | --- | --- |
| MUX1 / IN1 | PZT1 channels 1–5 | PZT7 channel 1 | Unspecified; excluded | Vmid |
| MUX2 / IN2 | PZT3 channels 1–5 | PZT7 channel 2 | Vmid; excluded | Vmid |
| MUX3 / IN3 | PZT5 channels 1–5 | PZT7 channel 3 | Vmid; excluded | Vmid |
| MUX4 / IN4 | PZT6 channels 1–5 | PZT7 channel 4 | PZT7 channel 5 | Vmid |

The same routing applies to both arrays. Sensor channels are numbered 1–5;
no spatial B/L/C/R/T orientation is assumed.

| Array | MUX1 | MUX2 | MUX3 | MUX4 |
| --- | ---: | ---: | ---: | ---: |
| 1 bias resistance (ohms) | 470000 | 1000000 | 470000 | 1000000 |
| 2 bias resistance (ohms) | 1000000 | 470000 | 1000000 | 470000 |

Input **7** is the only common Vmid address used by firmware. Input 6 must
not be used to park a quad: MUX4 input 6 is a PZT7 sensor.

## USB commands

USB serial is 460800 baud. Commands end with `*`. Configuration succeeds with
`#OK` or fails with `#NOT_OK`. A successful `run` starts binary traffic with
no text ACK. A timed run stops without an ACK. Any subsequent command ends
an active stream at a complete frame boundary before its text response.

| Command | Meaning |
| --- | --- |
| `mcu*` | `# TestBoard_ADC124`, followed by `#OK` |
| `help*`, `status*` | Commands, configuration, canonical payload routes, cumulative sweep/error counters |
| `mode PZT*` | Only supported mode; PZR/555 hardware is absent |
| `array 1|2|both*` | Filter configured routes to the selected physical arrays |
| `adcchannels 1:1:0,1:4:6,2:2:0*` | Array/ADC `1..2`, MUX `1..4`, external input `0..6` |
| `scanorder adc*` | Group by address within array A, then B; independent bank settling |
| `scanorder mux*` | Switch participating banks together; one wait per address; ADC A then B |
| `spiframe burst|single*` | Continuous-CS prepared batches (default) or per-word reference path |
| `spiengine blocking|dma|lpspi*` | SPIClass, asynchronous byte DMA, or direct 16-bit LPSPI transfer |
| `spiclock 8000000..16000000*` | Requested Hz |
| `channelrepeat 1..3*` | Consecutive conversions per sensor; transmit only the final value |
| `muxsettle 0..1000*` | Sensor analog settling; also VMID while following the default |
| `vmidsettle default*` or `vmidsettle 0..1000*` | Follow sensor settling (default) or override VMID analog wait; zero retains guards |
| `vmid true|false*` | Optional discarded Vmid conversions between address groups |
| `vmid 7*`, `ground ...*` | Numeric enable form / legacy alias |
| `ref 3.3*` | Confirm fixed reference; all other values rejected |
| `run*`, `run 1..3600000*` | Continuous or timed run in milliseconds |
| `stop*` | Stop and park both quads at Vmid |
| `profile on|off*` | Stopped diagnostic firmware only; production firmware rejects it |

Routing rejects duplicates, unpopulated inputs, input 7, and input 6 on
MUX1–MUX3. Invalid route lists leave the old list intact. The default list
contains all 50 sensor channels. Selecting one array produces 25 payload
words; sparse selection produces one word per active route. Each frame is
one complete sweep. `repeat`, `buffer`, ADS7953 `adcseq`, and range-changing
`ref` commands are deliberately unsupported.

The ADC's DIN channel address applies to the **next** conversion. Run preparation,
with the MUXes parked at VMID, initializes each active ADC's first selector
with at most one discarded conversion if the selector is unknown/different.
These initialization conversions are outside acquisition timestamps/counters.
The prepared plan then keeps the GPIO address unchanged while receiving every
requested conversion, advances inputs on the final repeat, and selects the
next batch's first input in the last command. For a full scan, IN4 selects IN1
except before address 6, where it selects IN4; the single IN4 read then selects
IN1 for the next sweep. Visiting VMID does not clock or change that selection.
A selector mismatch inside acquisition stops the run without a hidden prime.
ADC124S101 has no
returned channel tag: software ordering can be tested, but electrical identity
still requires input stimulation or a logic analyzer.

Mandatory GPIO parking runs at startup, before runs, and on stop/failure. It leaves the
quads enabled at Vmid without requiring SPI traffic. Unselected arrays are
disabled during a run. With `vmid true`, after each address group the active
participating banks are parked for the configured VMID settling delay, with
no ADC scan. In shared MUX mode the banks share that wait. With `vmid false`,
startup/stop/failure parking still runs. Repeat and Vmid settings never change payload width.

## Binary frame

```text
AA 55 | count:u16 | samples:u16[count] | avg_dt_us:u16 | start_us:u32 | end_us:u32
```

All fields are little-endian. `status` reports exact `payload_routes` in
wire order. Start/end cover acquisition and GPIO settling/parking; USB write
time is excluded. Average is acquisition duration divided by route count,
rounded to the nearest integer microsecond and saturated at 65535. Received
sweep-start intervals include gaps for intentionally discarded frames, while
the sampling-period counters cover every completed sweep. Timestamps and cumulative counters wrap at 32
bits. The runner accounts for this wrap.

Transfer timeouts/start failures stop acquisition, increment `transfer_errors`,
and require a Teensy reset before another run. Invalid nonzero upper response
bits increment `data_errors` and stop the run. No partial sweep is transmitted.

## Benchmarks and validation

The [2026-10-14 VMID-settling comparison](../../docs/testing/results/testboard-adc124/vmid_settle_2026-10-14/README.md)
tests inherited/explicit 5 us, 0 us and 1 us with sensor settling fixed at 5 us,
both modes and all engines, against a fresh FIFO/GPIO firmware baseline.
Both builds and 100 focused tests passed. Noise and timing are reported
separately; discarded VMID values themselves are not exported.

The [2026-10-14 FIFO/GPIO comparison](../../docs/testing/results/testboard-adc124/fifo_gpio_2026-10-14/README.md)
compares fresh before/after production and diagnostic matrices, including
common-window noise and longer captures where short quiet coverage failed.
Both builds and 80 focused tests passed. Faster read scheduling changes analog
history; the report retains noise increases and baseline shifts alongside rates.

The [2026-10-13 scan-mode benchmark](../../docs/testing/results/testboard-adc124/scan_modes_2026-10-13/README.md)
records 252 captures across both modes, all three engines, VMID off/on and
1/2/5/10/50 us settling. Both firmware builds and 106 focused tests passed.
At 5 us, LPSPI MUX mode improved rate by 40.2% without VMID and 31.4% with
VMID against the supplied sequential reference. The report separates timing
estimates from measurements and retains a reproducible short-delay noise issue:
ADC/LPSPI at 1 us, VMID off, showed about 6 counts SD on B MUX2 input 1.
Production firmware was restored and acquisition left stopped.

The [2026-10-13 COM4 readiness check](../../docs/testing/benchmarks/testboard-adc124/TESTBOARD_ADC124_COM4_QUICK_RESULTS_2026-10-13.md)
completed 25 short captures with no malformed/missing transmitted frames or
firmware errors. With disconnected sensor strips and 500 us settling, all 50
channel means were 1.6500–1.6508 V. The report records shorter-delay offsets
and accounted USB discards; sensor response and longer validation remain.

Use [`benchmarks/README.md`](benchmarks/README.md) for dry-run, smoke, full
matrix, outputs and interpretation. This project has a standalone runner.
The desktop GUI detects `TestBoard_ADC124` and uses its dedicated
[`testboard_adc124.json`](../../config/boards/profiles/testboard_adc124.json)
profile and triple-route adapter. It exposes sensor settling and independent
VMID settling (including follow-sensor mode), alongside array, PZT, repeat,
Vmid and SPI-clock controls. Scan order is selectable in the GUI as ADC (array
sequential) or MUX (shared settling); change it while stopped and Configure again.
Scan order, engine and framing default to `adc`, `lpspi` and `burst` in the
profile JSON. Firmware also boots with LPSPI. Restart the GUI after profile edits
so its cached configuration uses the updated defaults.
The current GUI contract requires protocol 3 and verifies reported routes and
settling before capture. See the [configuration and hardware checks](../../docs/guides/configuration/ARRAY_CONFIGURATION_GUIDE.md#testboard_adc124-external-mux-arrays).

Host checks from the repository root:

```powershell
uv run --with ziglang --with unicorn --with pyelftools pytest tests/test_testboard_adc124_benchmark.py tests/test_testboard_adc124_firmware.py tests/test_adc124_scan_modes.py tests/test_adc124_settling_report.py tests/test_testboard_adc124_usb_patch.py
```

The optional Zig compiler executes the production driver, SPI controller, USB
wrapper and application against simulated GPIO/ADC/registers. Both environments
cover 708 base acquisition combinations, 216 independent VMID settling cases,
90 sparse VMID cases, equal-delay parity, shared waits,
redundant-switch avoidance, switching guards, CS bursts, stalls, timeout and
error cleanup. Unicorn verifies the compiled ARM USB writer against busy-ring,
disconnect, ring-wrap and autoflush-interruption scenarios. These tools are test
dependencies only; regular pytest skips checks with unavailable dependencies.
Build both environments first to run ARM verification. This simulation and the Teensy build
do not validate the physical board. Before accepting hardware results:

1. Verify 3.3 V VA, actual ADC VD / MISO logic level compatible with Teensy,
   and 1.65 V Vmid. Confirm there is no board inverter on EN.
2. Probe EN/address: array 1 enables high, array 2 low; address writes happen
   while disabled. Confirm CS9/CS24 are never asserted together.
3. Check 16 SCLK pulses per conversion, actual 8–16 MHz clock, CS setup/high timing, and
   DIN address bits 12:11. Verify DMA byte boundaries keep CS low.
4. Apply distinct known voltages to individual MUX inputs. Confirm route
   identity through every scan order, all engines and repeat counts 1–3,
   including both PZT7 input-5 routes and MUX4 input 6.
5. Check each array alone, both together and sparse routes. Confirm every
   quad parks at address 7 on stop, timed stop and failed acquisition.
6. With sensors untouched, compare settling delays 0/2/5/10 us, Vmid on/off,
   clocks 8/12/16 MHz, offset/noise, and the two resistor populations. Choose
   a delay after levels and noise stabilize. Repeat with isolated presses to
   examine analog carryover and crosstalk; the matrix alone is a quiet test.
7. Run a long session. Require zero firmware errors, no malformed frames,
   and matching sent/received frame counts. Sampling must equal sent plus
   discarded. Use the runner's 500 ms reader-pause check to verify sampling
   continues through USB stalls: discarded sweeps should increase without
   malformed frames, USB write errors or a pause-sized sampling-period spike.
8. Compare the original ADC124 production firmware with this production build
   using identical board conditions, sparse/full routes, engines, clocks,
   settling delays, repeats and Vmid settings. Confirm electrical CS timing,
   channel identity, offsets/noise and crosstalk separately from software speed.
   No ADC124 on-board speedup or analog acceptance is established by these
   offline checks.

Datasheet references: [ADC124S101](https://www.ti.com/lit/ds/symlink/adc124s101.pdf),
[TMUX1108](https://www.ti.com/lit/ds/symlink/tmux1108.pdf),
[TMUX1308A](https://www.ti.com/lit/ds/symlink/tmux1308a.pdf).
