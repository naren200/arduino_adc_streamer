# TestBoard ADC124 benchmarks

All ADC124 capture, benchmark, analysis and report tools live in this folder,
alongside the route manifest and shared helpers, matching TestBoard_7953.
`tmp/` is reserved for temporary inspection files and scratch artifacts.
Raw captures remain under ignored `docs/testing/datasets/`; reports and plots
remain under `docs/testing/results/`. Run the commands below from the repository root.

## Tools

| Purpose | Scripts |
| --- | --- |
| General matrix, smoke tests, sparse routes | `testboard_adc124_benchmark.py` |
| Quick production upload and 8/16 MHz check | `benchmark_adc124_lookahead_quick.py`, `analyze_adc124_lookahead_quick.py` |
| Scan-mode, FIFO/GPIO and VMID comparisons | `benchmark_adc124_scan_modes.py`, `benchmark_adc124_fifo_gpio.py`, `benchmark_adc124_vmid_settle.py` and matching `analyze_` / `report_` tools |
| Extended disconnected-board matrix | `benchmark_adc124_upgrade.py`, `analyze_adc124_upgrade.py`, `report_adc124_upgrade.py` |
| Full LPSPI-only selector-lookahead rerun | `benchmark_adc124_lookahead_lpspi.py`, `analyze_adc124_lookahead_lpspi.py`, `noise_adc124_lookahead_lpspi.py`, `report_adc124_lookahead_lpspi.py` |
| Connected PZT comparison and cold-start charging | `benchmark_adc124_connected.py`, `analyze_adc124_connected.py`, `analyze_adc124_charge.py`, `noise_adc124_connected.py`, `report_adc124_connected.py` |
| Settling captures and diagnostic profiles | `capture_adc124_settling.py`, `run_adc124_mux_profiles.py`, `report_adc124_settling.py`, `report_adc124_timing.py`, `report_adc124_mux_timing.py` |
| Noise analysis and timing models | `analyze_adc124_noise_patterns.py`, `analyze_adc124_selected_noise.py`, `adc124_quiet_windows.py`, `adc124_timing_model.py` |

Historical comparison runners check their corresponding firmware revisions;
use the general runner or quick lookahead runner for the current revision.
The specialized upload/profile tools currently target COM4.

The connected runner verifies the already-installed lookahead production
firmware and snapshots local sources/images, without uploading or resetting it.
Capture startup before the matrix, after physical power-off and sensor discharge.
Startup is 60 s at MUX/LPSPI/16 MHz/5 us/VMID off, with no warm-up discarded.
Time zero is the first recorded sweep, not the USB power edge. Additional
physically discharged attempts use distinct `--startup-name` values.
The matrix has the same 28 LPSPI settings and three repetitions as the latest
disconnected rerun. Every stage restores stopped original settings.

```powershell
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/benchmark_adc124_connected.py startup RAW_ROOT --discharge-description "Document power-off duration and discharge method"
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/analyze_adc124_charge.py RAW_ROOT/cold_poweron RESULT_ROOT/cold_poweron
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/benchmark_adc124_connected.py matrix RAW_ROOT
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/analyze_adc124_connected.py RAW_ROOT RESULT_ROOT
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/noise_adc124_connected.py RESULT_ROOT
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/report_adc124_connected.py RESULT_ROOT REPORT.pdf
```

The connected analyzer compares exact settings against the latest same-firmware
`selector_lookahead_lpspi_2026-10-14` results. It preserves unfiltered metrics
and identical common quiet-window metrics. Baseline plots are line graphs.
The charging analyzer uses absolute final-baseline thresholds B/3 and 2B/3,
confirmed for 2 ms, and never extrapolates a threshold missed before recording.
Every route's initial level, final baseline, censoring status and measured
interval remain in the startup CSVs. Crossing resolution is approximately one
sweep; interpolation does not add physical timing precision.

The full LPSPI-only lookahead runner repeats the extended disconnected matrix
without blocking/DMA or diagnostic captures: 28 settings, three repetitions,
1 s warm-up plus 15 s measurement. It covers both scan modes, all five sensor
delays with matching VMID delays and VMID off/on, the sensor-5-us/VMID-0-or-1-us
extras, and requested 8/16 MHz at sensor=VMID=5 us. Build both environments
before starting; the runner freezes their images/source, uploads only the
production image, verifies lookahead/zero-prime status, and restores stopped
production settings. The analyzer compares exact settings against the saved
`upgrade_disconnected_2026-10-14` LPSPI reference, preserving both unfiltered
and common quiet-window metrics. The noise tool adds selected-route time/PSD
plots and comparisons with the previously structured routes.

```powershell
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/benchmark_adc124_lookahead_lpspi.py RAW_ROOT --dry-run
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/benchmark_adc124_lookahead_lpspi.py RAW_ROOT
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/analyze_adc124_lookahead_lpspi.py RAW_ROOT RESULT_ROOT
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/noise_adc124_lookahead_lpspi.py RESULT_ROOT
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/report_adc124_lookahead_lpspi.py RESULT_ROOT REPORT.pdf
```

For the quick check, build both firmware environments first. The runner uploads
the built production HEX, checks 28 settings, and leaves production stopped with
the previous configuration restored. `--baseline` adds 24 matched captures of
an already installed `adc124-vmid-settle-v1` image before uploading the new build.
By default it checks only the new build; it does not upload an old baseline image.
Dry-run opens no serial port, uploads nothing and creates no output files.

```powershell
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/benchmark_adc124_lookahead_quick.py --dry-run
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/benchmark_adc124_lookahead_quick.py RAW_ROOT
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/analyze_adc124_lookahead_quick.py RAW_ROOT RESULT_ROOT
```

## General runner

`testboard_adc124_benchmark.py` owns the USB serial port, checks firmware
identity and status, configures a matrix, captures continuously through warm-up
and measurement, and compares DMA/LPSPI measurements with matching blocking
runs. Close the GUI and serial monitors first. Flash the ADC124 firmware,
keep sensors untouched for quiet benchmarks, and review the wiring and supply
validation in [`../README.md`](../README.md).

Current protocol 3 exposes **ADC** (array-sequential address groups) and **MUX**
(both banks switch before one shared wait; ADC A then B) modes. The original
`mux` name meant ADC mode. Protocol-1/2 captures retain that old interpretation
when decoded using their saved version and `payload_routes`. Legacy firmware
requires explicit `--scan-order adc --spi-frame single`; the runner maps this
to its former `scanorder mux`. The new MUX mode requires protocol 3.

`--spi-frame burst` is the default; `--spi-frame single` keeps per-conversion
CS as a comparison control. Historical firmware used one discarded pipeline prime
per ADC/address batch. Current `adc_selector_policy=lookahead` firmware uses
four conversions for a four-channel group and one for address 6 at repeat 1,
without acquisition primes. Startup may initialize each ADC once while parked.
VMID batches use four discarded conversions and select the next sensor input.
Analysis detects the status policy; historical captures keep their old word
budgets. Include both frame values to compare matched frame settings.

The completed [scan-mode hardware report](../../../docs/testing/results/testboard-adc124/scan_modes_2026-10-13/README.md)
includes production/diagnostic timings, burst controls and common-window noise
analysis. Its raw-only recorder, `Arduino_Sketches/TestBoard_ADC124/benchmarks/benchmark_adc124_scan_modes.py`,
avoids per-sample CSV work during capture and restores production firmware.

The [FIFO/GPIO comparison](../../../docs/testing/results/testboard-adc124/fifo_gpio_2026-10-14/README.md)
uses `Arduino_Sketches/TestBoard_ADC124/benchmarks/benchmark_adc124_fifo_gpio.py before|after RAW_ROOT` to retain
frozen images/source hashes, matched production/diagnostic matrices and stopped
production restoration. Build both environments and load the corresponding
production image first. The paired analyzer records failed quiet coverage
explicitly; supplementary long captures use the same all-channel time mask.

Protocols 2/3 continue sampling during USB stalls and intentionally discard
complete sweeps when USB is busy. `sampling_sweeps = usb_frames_sent +
usb_frames_discarded`; received frames must match **sent** frames. Accounted
discards are WARN, and missing transmitted frames remain FAIL. The old runner's
lifetime-sweeps/received comparison is unsuitable for protocol-2/3 captures.

Firmware with `vmid_settle_us`/`vmid_settle_mode` status adds independent VMID
settling. Use `--mux-settle-us 5 --vmid on --vmid-settle-us default 5 0 1`
to compare inheritance and explicit waits while retaining switching guards.
The default follows sensor settling dynamically and clears any stale override
on supported firmware. Explicit overrides require this capability. Each value
has distinct test IDs and comparison keys; sessions/results record requested
and effective delay/mode. Restore helpers preserve explicit zero as well.

The [VMID-settling report](../../../docs/testing/results/testboard-adc124/vmid_settle_2026-10-14/README.md)
and `Arduino_Sketches/TestBoard_ADC124/benchmarks/benchmark_adc124_vmid_settle.py` cover fresh previous-firmware,
default/explicit-5 parity and 0/1 us measurements across both modes/all engines.

From the repository root:

```powershell
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/testboard_adc124_benchmark.py --list-ports
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/testboard_adc124_benchmark.py --dry-run
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/testboard_adc124_benchmark.py --port COM7 --smoke-only
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/testboard_adc124_benchmark.py --port COM7
```

Replace COM7 with the board's port. Dry-run opens no port and creates no output.
Default: 36 configurations x 3 repetitions, each with 1 second warm-up and
5 seconds measurement. It compares arrays 1/2/both, `adc`/`mux` scan order,
blocking/DMA/LPSPI engines, and 2/5 us settling, with repeat 1, Vmid off and
requested 16 MHz SPI. Allow about 11 minutes plus command/capture overhead.
Smoke mode tests nine combinations (three array selections x three engines)
with ADC order, burst frames, repeat 1, Vmid off, 5 us settling, one repetition and at most
1 second measurement. Other requested array/engine/clock filters remain active.

To diagnose phase costs, upload `teensy41_profile` and add `--profile on`.
Production firmware rejects this option. `--profile off` is the default and
also turns off profiling on diagnostic firmware. Each run saves validated
stopped profiling status and its decoded summary to `firmware_profile.jsonl`.
Profiles cover the whole run including warm-up; nested transfer/validation
scopes overlap. Do production speed comparisons with profiling compiled out.

Run a reduced transport-stall check, including one deliberate 500 ms reader
pause after the first second:

```powershell
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/testboard_adc124_benchmark.py `
  --port COM7 --arrays both --scan-order mux --spi-engine lpspi `
  --mux-settle-us 5 --window-ms 2000 --repetitions 1 `
  --reader-pause-ms 500 --reader-pause-at-ms 1000
```

Sampling must continue, discard counters should account for omitted frames,
and write/error/parser counters must stay zero. Inspect sampling-period maxima;
a gap close to the reader pause is a sampling failure indicator. Windows may
buffer some data before USB fills, so a pause is not guaranteed to force
discard on every host. Pauses are limited to 0–1000 ms and must finish before
the timed run ends. This optional check opens the serial port only when run.

Expand the analog/settling matrix explicitly:

```powershell
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/testboard_adc124_benchmark.py `
  --port COM7 --arrays both --scan-order adc mux `
  --spi-clock-hz 8000000 12000000 16000000 --channel-repeat 1 2 3 `
  --mux-settle-us 0 2 5 10 --vmid off on --window-ms 2000 --repetitions 1
```

This example has 432 configurations; inspect its `--dry-run` first. The
settle value 0 retains the fixed switching allowances, but adds no analog
settling delay. SCLK requests are constrained to the ADC's 8–16 MHz AC range.

## Sparse routing

The manifest [`testboard_adc124_routes.json`](testboard_adc124_routes.json)
defines `full_array1`, `full_array2`, `both_full` and `pzt7_both`. The runner
checks reference and bias-resistor metadata against this board's fixed wiring.
Each route is `array:mux:input`; array number is also ADC number. Input numbers
are zero-based external MUX addresses; MUX numbers are 1–4 (ADC IN1–IN4).

```powershell
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/testboard_adc124_benchmark.py `
  --port COM7 --route-set pzt7_both --arrays both --scan-order mux
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/testboard_adc124_benchmark.py `
  --port COM7 --sensors 1 7 --arrays 1
uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/testboard_adc124_benchmark.py `
  --port COM7 --routes 1:1:0,1:4:6,2:2:0 --arrays both
```

`--sensors` filters by PZT numbers 1/3/5/6/7 on each array. PZT7 channels 1–4
are MUX1–MUX4 input 5; its fifth channel is MUX4 input 6. Duplicate/reserved
routes are rejected. Scan order determines actual acquisition and wire order;
the runner requires firmware `payload_routes` to match before starting.

## Artifacts and interpretation

Sessions default to a UTC timestamped folder below `benchmarks/results/`,
ignored by Git. `--output PATH` must name a new directory, preserving previous
sessions. Samples and raw USB bytes are written incrementally; only per-run
statistics stay in memory. Allow substantial disk space: every route sample
is retained. Choose shorter windows/filters for initial checks.

- `session.json`: arguments, board/route/resistor metadata, configuration
  matrix, session state and failure details.
- `commands.log`: timestamped commands and replies.
- `results.csv`: per-run PASS/WARN/FAIL, measured device rate, acquisition
  timing, parser diagnostics and firmware/received sweep counters.
  It also records sent/discarded counts, discard percentage, USB policy, and
  sampling-period diagnostics for protocols 2/3, plus scan semantics and frame mode.
- `channel_stats.csv`: per-route mean, sample standard deviation, extrema,
  Vmid offset, clipping and bias-resistor metadata, excluding warm-up.
- `comparisons.csv`: engine mean shifts, noise and throughput ratios against
  a successful blocking run with identical settings and repetition.
- `benchmark_report.xlsx`: Results, Channels, Engine comparisons and Notes.
- Each `TEST_ID__repN/` contains `capture.bin`, `samples.csv` (including
  warm-up and measurement phases), and before/after `status.json`.
  `firmware_profile.jsonl` preserves profiling evidence, including decoding
  or written/received reconciliation errors.

`--no-raw` omits raw binary bytes; `--no-excel` omits the workbook. CSVs are
saved after every completed run. Ctrl+C requests stop, retains existing results
and the current partial raw/sample files, and records an interrupted session.
No resume/overwrite mode is implemented; use a new directory.

**FAIL** means incorrect status/identity, new or existing firmware errors,
malformed frames, impossible ADC codes, insufficient measured duration,
failure to become idle, or a mismatch between transmitted/received sweep
counts (sent versus received for protocols 2/3). The runner stops at the first failure and preserves its artifacts.
Reset the Teensy after a transfer fault before retrying.

**WARN** flags quiet mean offset over 100 counts from nominal Vmid code 2048,
noise standard deviation over 10 counts, or clipping at 0/4095. Engine
comparison rows warn on a mean shift over 5 counts. Set thresholds using
`--vmid-warn-counts`, `--noise-warn-counts`, `--shift-warn-counts`.
Comparisons are separate from the stream-validation result and are absent
when no matching successful blocking baseline exists. Analog drift between
runs can also produce engine mean shifts; repeat under stable conditions.

Accounted USB discards also produce WARN, with their percentage reported. All
sampling and transport counters include warm-up. Severe stalls can still fail
the minimum delivered measurement-duration check. Warm-up is excluded relative
to the first received device timestamp; a delayed first frame can conservatively
exclude additional data. Compare analog results only when delivered coverage
is adequate and use the same reader settings for engine comparisons.

Rates use differences between **device sweep-start timestamps**, not the
host CSV-writing clock. For protocols 2/3 this is delivered-frame rate; intentional
discards lower it without necessarily slowing ADC sampling. Sampling-period
status measures the acquisition loop, and host logging can affect discard rate.
Samples share a sweep timing interval; exported start/end
timestamps are not individual ADC conversion times. ADC124S101 has no channel
identity tag, so a PASS cannot prove PCB channel wiring or detect every form
of corruption. Verify known-voltage routing and actual SCLK on hardware.
