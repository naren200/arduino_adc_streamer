# TestBoard_ADC124 automatic PCB diagnostics - 2026-10-15

Completed with connected, untouched PZT strips, LPSPI only. The independent
`teensy41_pcbdiag` firmware profile supports arbitrary external-address steps,
bank/input ordering, EN isolation, fixed phase slots and retaining every
conversion. Production firmware and its desktop protocol were not changed.
The board is restored to the frozen original production HEX and original
settings, stopped (`adc124-selector-lookahead-v1`).

## Explanatory report revision

The PDF was rewritten on 2026-10-15 using the existing recordings. Its 65 pages
contain a terminology guide, clickable contents and 20 numbered test sections.
Each test explains its purpose, exact procedure, parameters, measurement, legend
and conclusions. Recovery wait-only controls now use separate x/hollow-diamond
markers; the earlier overlapping x/+ markers are explained. Isolation timing
labels distinguish the programmed wait from the additional 100 us slot time.
The original PDF/builder/verification are preserved in report_revisions/original;
revised authoring sources and hashes are in report_revisions/explained.
The acquisition-era raw tool snapshot is preserved unchanged.

## Evidence and findings

- 762 complete captures across 256 programs; three repetitions except three
  120-second noise records. 7,919,928 acquired cycles, 7,919,811 received and
  117 accounted whole-cycle USB discards. Zero transfer/data/USB-write errors
  and zero controlled timing overruns. Checksums, event labels and sequence
  accounting were checked during acquisition.
- Address 6 is physically VMID on MUX1-3, but a sensor on MUX4. Removing that
  address at 5 us changes Array A/MUX2 baseline spread across the same six
  sensor addresses from 36 to 7 counts; MUX4 remains at 8 counts. Hidden
  reference visits explain much of the A/MUX4 distinction; resistance alone
  does not. This intervention changes cadence/duty too, addressed separately
  by equal-slot controls. Removing address 6 loses two sensors and is not a
  full-50-channel production fix.
- Holding a sensor address and reading a single ADC input yields about 2048
  counts on all eight paths. Large scanning deficits are therefore dynamic;
  this control also gives a sensor continuous bias connection.
- Extending powered EN isolation from 1 ms to 1 s changes first-reconnection
  baselines by -1 to 0 counts on A and -480 to -363 counts on B (approximately
  -387 to -292 mV on B). These are combined retention/reconnection responses,
  not direct isolated-sensor voltage or identified leakage/capacitance values.
  Reconnection uses EN only at unchanged address 2.
- Sensor burst readings rise 2-9 counts from first to eighth conversion. Even
  continuously connected VMID with the ADC input fixed shows a mean increase
  up to 2.162 counts; the bank read second starts 2.034-2.296 counts higher.
  A shared conversion-related effect contributes. Reference/supply ratio
  transients, buffer drive and digital coupling remain candidates; no rail
  voltage was probed and no defective component is identified.
- In matched slots, adding discarded conversions while connected to VMID
  changes median MUX centers by at most 1 count for both tested clamp intervals
  (50 and 20 us). This is not validation of a faster production schedule.
- The unreset long record has a burst/step and isolated spikes. Largest route
  ranges are 128 counts / 103.125 mV without reset and 72 counts / 58.008 mV
  with VMID reset, versus 2 counts held. The VMID record's small sustained SD
  does not mean it has no spikes. Sequential records and unmonitored vibration
  prevent distinguishing electrical interference from real PZT responses or
  establishing causal mode suppression.

## Artifacts

- PDF: `docs/reports/exports/TestBoard_ADC124_PCB_Diagnostics_2026-10-15.pdf`.
  It includes tables, baseline line graphs, recovery/retention curves,
  reference-only controls, raw noise traces, spike zooms and PSDs.
- `chapter_explanations.json`: structured explanations for all 20 tests.
- `analysis.json`, `captures.csv`: complete matrix, raw-root identity,
  capture timing and accounting.
- `channels.csv`, `channel_repetitions.csv`: per-route baseline, raw SD and
  peak-to-peak in counts and volts; repetition ranges/worst repetitions.
- `mux_spreads.csv`, `address_spreads.csv`: spatial baseline comparisons,
  distinct from temporal noise.
- `*_comparisons.csv`, `interpretation.json`: data-derived comparisons.
- `long_noise.csv`, `long_spikes.csv`, `long_quiet_channels.csv`,
  `long_quiet_metadata.json`, `correlations.csv`, `timing_correlations.csv`:
  long-record raw/quiet metrics and descriptive associations. SD-selected
  and peak-to-peak-selected example routes are explicitly distinguished.
- `verification.json`: final totals, image/source hashes, regression/build
  evidence, production restoration and PDF review.
- Raw root: `docs/testing/datasets/testboard-adc124/pcb_diagnostics_2026-10-15`.
  Includes `.bin/.npz/.json` captures, original production backup, diagnostic
  firmware snapshot, exact tool snapshot and command/upload logs. The separate
  smoke capture and incomplete archived host-timeout attempt are excluded.

## Reproduction and limitations

From the repository root, using the existing Python environment:

```powershell
.venv/Scripts/python.exe Arduino_Sketches/TestBoard_ADC124/benchmarks/analyze_pcb_diagnostics.py
.venv/Scripts/python.exe Arduino_Sketches/TestBoard_ADC124/benchmarks/interpret_pcb_diagnostics.py
.venv/Scripts/python.exe Arduino_Sketches/TestBoard_ADC124/benchmarks/report_pcb_diagnostics.py
```

The diagnostic firmware/host format is independent of the production GUI.
See `Arduino_Sketches/TestBoard_ADC124/benchmarks/PCB_DIAGNOSTICS.md` for commands,
record schema, timing-marker limits and restore requirements. SPI frequencies
are requested settings; SCLK was not electrically measured. Diagnostic rates
include logging overhead and are not production speed benchmarks.

Main statistics retain every steady-state event. Quiet statistics separately
screen separated states and sustained variance, then use the longest remaining
continuous interval, with exact thresholds/intervals exported. They do not select
the minimum SD. Isolated spikes inside that interval remain. A constant ADC code
does not establish zero analog noise. MUX type remains tied to array location,
ADC, buffer and layout; these tests do not uniquely identify a component cause.

## Added VMID tests

Tests 21-23 add 63 recordings from the separate vmid_long_hold_2026-10-15
dataset, giving 825 combined recordings. The combined PDF has 79 pages; new
tests start at page 56. They cover a timed 20 s VMID park, ten retained
conversions at external MUX address 2 on all eight MUX paths with both ADC
orders, and regular full scans with 1/5/10 ms VMID waits at 5 us sensor settling.
See ../vmid_long_hold_2026-10-15/README.md and verification.json for the new
measurements, timing qualifications, exact sources and restoration evidence.
The prior 65-page explanatory PDF is archived in report_revisions/explained.
