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
  and array parking transitions are prepared once per run. LPSPI retains one
  shared-bus transaction per sweep, with separate CS frames for each conversion.
  CS GPIO registers are cached; receive readiness **and fresh transfer completion**
  are required before CS release. Session setup uses a known 16-bit command
  instead of asynchronous TCR readback. SPI state is restored before USB enqueue
  and on failure. The 100 ns CS setup/high allowances are retained.
- Engine-specific conversion loops avoid per-word mode dispatch. LPSPI waits
  use the cycle counter with a 1 ms bound and foreground servicing every 10 us
  during extended waits; interrupts remain enabled. DMA retains EventResponder
  servicing. GPIO MUX switching and its settling delay still run for each group.

These changes do not import ADS7953 Auto-1 masks, its two-frame response delay,
20–30 MHz clock range, dual-bus overlap, or its ADC/channel wiring. ADC124 still
uses one shared bus, one-frame input selection, the original triple routes,
opposite MUX EN polarity, and fixed 3.3 V reference.

The binary frame is unchanged. Status now reports `protocol_version=2` and
`usb_stream_policy=discard_if_busy`. `sampling_sweeps`, `usb_frames_sent`,
`usb_frames_discarded`, `sampling_period_max_us`, and `sampling_period_over_1ms`
reset on each accepted run. `sweep_count` remains the lifetime completed-sweep
counter (including discarded sweeps). `usb_write_errors` is cumulative. On a
successful run, sampling equals sent plus discarded; only sent frames should
equal received frames. Use the updated runner to account for this policy.

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

`prepare` includes MUX address/parking work, and `transfer` includes the ADC
batch plus optional Vmid sampling. `validate` measures response checks nested
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
| `scanorder mux*` | Sort by array, external input, MUX; read up to four signals per GPIO address change |
| `scanorder channel*` | Sort by array, MUX, external input; finish each MUX's channels |
| `scanorder interleaved*` | Sort by external input, MUX, array; alternate arrays when both have that route |
| `scanorder array|adc*` | Aliases for `channel`, reported canonically as `channel` |
| `spiengine blocking|dma|lpspi*` | SPIClass, asynchronous byte DMA, or direct 16-bit LPSPI transfer |
| `spiclock 8000000..16000000*` | Requested Hz |
| `channelrepeat 1..3*` | Consecutive conversions per sensor; transmit only the final value |
| `muxsettle 0..1000*` | Extra analog settling delay in microseconds after address changes |
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

The ADC's DIN channel address applies to the **next** conversion. At each
address group, the firmware primes that selector with one discarded transfer,
keeps the GPIO address unchanged while receiving every requested conversion,
and advances to the next ADC input on the final repeat. ADC124S101 has no
returned channel tag: software ordering can be tested, but electrical identity
still requires input stimulation or a logic analyzer.

Mandatory GPIO parking runs at startup, before runs, before changing ADC
ownership, at the end of a two-array sweep, and on stop/failure. It leaves the
quads enabled at Vmid without requiring SPI traffic. Unselected arrays are
disabled during a run. With `vmid true`, after each address group the active
quad is parked and all four ADC inputs are converted once (plus one selector
prime); those five transfers are discarded. With `vmid false` parking still
runs. Repeat and Vmid settings never change payload width.

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

The [2026-10-13 COM4 readiness check](../../docs/testing/benchmarks/testboard-adc124/TESTBOARD_ADC124_COM4_QUICK_RESULTS_2026-10-13.md)
completed 25 short captures with no malformed/missing transmitted frames or
firmware errors. With disconnected sensor strips and 500 us settling, all 50
channel means were 1.6500–1.6508 V. The report records shorter-delay offsets
and accounted USB discards; sensor response and longer validation remain.

Use [`benchmarks/README.md`](benchmarks/README.md) for dry-run, smoke, full
matrix, outputs and interpretation. This project has a standalone runner.
The desktop GUI does not yet have an ADC124 board profile or triple-route
configuration support; its ADS7953 profile must not be used for this board.

Host checks from the repository root:

```powershell
uv run --with ziglang --with unicorn --with pyelftools pytest tests/test_testboard_adc124_benchmark.py tests/test_testboard_adc124_firmware.py tests/test_testboard_adc124_usb_patch.py
```

The optional Zig compiler executes the production driver, SPI controller, USB
wrapper and application against simulated GPIO/ADC/registers. Both environments
cover 486 digital acquisition combinations, sparse routes, stalls, timeout and
error cleanup. Unicorn verifies the compiled ARM USB writer against busy-ring,
disconnect, ring-wrap and autoflush-interruption scenarios. These tools are test
dependencies only; regular pytest skips checks with unavailable dependencies.
Build both environments first to run ARM verification. This simulation and the Teensy build
do not validate the physical board. Before accepting hardware results:

1. Verify 3.3 V VA, actual ADC VD / MISO logic level compatible with Teensy,
   and 1.65 V Vmid. Confirm there is no board inverter on EN.
2. Probe EN/address: array 1 enables high, array 2 low; address writes happen
   while disabled. Confirm CS9/CS24 are never asserted together.
3. Check 16 SCLK pulses, actual 8–16 MHz clock, CS setup/high timing, and
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
