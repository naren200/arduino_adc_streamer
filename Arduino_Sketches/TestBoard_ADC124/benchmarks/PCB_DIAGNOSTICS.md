# Temporary ADC124 PCB diagnostic profile

This standalone experiment profile is compiled with `pio run -e teensy41_pcbdiag`
in the TestBoard_ADC124 project. Default `teensy41` production firmware and its
protocol are unchanged. The diagnostic environment excludes `main.cpp` and uses
`diagnostic_main.cpp` guarded by `TESTBOARD_DIAGNOSTIC`. Its MCU identity is
`ADC124_PCB_DIAGNOSTIC`; do not connect the desktop production GUI to it.

## Commands and labels

Commands end in `*` and acknowledge with `#OK` / `#NOT_OK`:

- `clear`: remove the stopped program.
- `config CLOCK ARRAYS FIRST_BANK INPUT_ORDER PERIOD_US`: clock 8/16 MHz;
  array bit mask 1/2/3; first bank 0/1; packed ADC input permutation (228 is
  IN1..4, 27 is IN4..1); zero period runs freely, nonzero schedules cycles.
- `step ADDRESS WAIT_US SLOT_US INPUT_MASK REPEAT FLAGS`: append up to 32
  steps; external address 0..7; input bit mask 0..15; repeat 1..10, maximum
  12 words/ADC burst. A zero input mask applies switching/waiting without SPI.
- Flags: 1 retain responses; 2 disable bank during wait (requires zero mask);
  4 disable immediately after reads; 8 disable at the end of the slot.
  Even without flag 1, a nonzero input mask clocks the prepared ADC batch.
- `run DURATION_MS`: validate and execute; duration checked between cycles.
- `single HOLD_US` (pcbdiag-v2): prepare selectors while parked at VMID,
  wait 0..30,000,000 us without conversions, acquire exactly one cycle, then
  park/stop. The long hold is outside the frame's CPU-tick timeline. Status
  exposes hold-start/end microseconds and the requested hold duration.
- `stop`, `status`, `mcu`: stop parks both banks at address 7. Commands received
  during acquisition first stop at a complete-cycle boundary.

The configured wait always runs, even for an unchanged address. A slot is
measured from phase entry; it includes switching, wait and transfers. The
normal 200 ns off/on guards and EN polarity remain unchanged. Per-cycle/session
clock, CS and FIFO behavior use the existing LPSPI driver. Lookahead selects the
next batch's first ADC input, including cycle wrap and unrecorded ADC batches.
Only run preparation primes selectors; the resulting response is not recorded.

Address 6 is a reference connection on MUX1-3 and a sensor on MUX4, on BOTH
arrays. The Python physical labels explicitly mark these reference responses.
Recorded words from repeated conversions are all retained and individually
labeled; this differs from production's last-repeat-only behavior.

## Diagnostic binary record (not the production envelope)

All fields little-endian. Header `<4sHHIIII>`: `ADG1`, total byte count, event
count, sequence, cycle-start microseconds, acquisition-end microseconds,
cumulative slot/period overruns. Each event has `<BBBBIII>`: step index, bank
index, physical external address, word count, ticks after switch/enable guard,
ticks before ADC burst, ticks after ADC burst. Ticks are relative to cycle
start at the reported CPU frequency. Each event is followed by its 12-bit
codes as uint16 words. Final uint32 is FNV-1a over header and events.

Markers bracket bursts; they are not oscilloscope measurements of GPIO edges
or exact analog apertures. `ADG1` records fit one 2048-byte atomic USB enqueue;
whole-cycle discards are counted, sequences are retained, and partial writes
are errors. The host saves the run ACK plus every binary byte before decoding.
It waits for one complete final long cycle beyond the requested run duration.

## Runner and evidence

`run_pcb_diagnostics.py` requires the frozen production backup and saved settings
in the dataset lifecycle record. It uploads diagnostic firmware and restores
the frozen production HEX/settings in `finally`, verifying stopped production
identity. It resumes only captures whose JSON state is `complete`. Failed
attempts are preserved separately rather than incorporated into analysis.

`pcb_diagnostics.py --plan` describes the automatic matrix. Captures comprise
boundary/reset controls, held addresses, eight-conversion recovery plus two
wait-only endpoints, address/bank/input order, bank isolation, powered leakage
intervals, tied-VMID input controls and three 120-second noise records. A later
short-dwell reset control uses 25 us sensor / 20 us reference slots.

Run `analyze_pcb_diagnostics.py`, `interpret_pcb_diagnostics.py`, then
`report_pcb_diagnostics.py` after acquisition.
Full raw baseline/noise statistics, per-conversion labels, bursts' time markers,
capture accounting, programs, frozen sources and images accompany the PDF.
Diagnostic rates include extra logging work and are not production benchmarks.
The completed matrix has 256 programs / 762 captures, including continuously
connected VMID bursts with one or both ADCs and reversed bank order.

The separate `benchmark_adc124_long_vmid.py` extension uses pcbdiag-v2 and
preserves the original matrix. It runs 63 additional captures: 27 pre/post pairs
(a continuous full scan followed by one post-20-second-hold cycle or ten-read
burst), and nine regular full scans with 1/5/10 ms VMID waits. Both ADC orders
are tested for ten-read bursts at external MUX address 2 on MUXes 1-4. The timed
hold is exactly 20 s after selector preparation; total parked time is longer
because host setup precedes that timer and is exported separately. Production
firmware/settings are restored unconditionally. Analyze with
`analyze_adc124_long_vmid.py`; completed results append report Tests 21-23.

The exchanged-strip repeat is isolated in
`vmid_swapped_strips_2026-10-16`. Run `benchmark_adc124_swapped_vmid.py` only
after the physical exchange. It checks the complete plan and exact diagnostic
HEX hash against the original Tests 21-23 before invoking the unchanged runner.
`report_adc124_swapped_vmid.py` verifies the 63-capture repeat and restoration,
then creates a separate comparison PDF and counts/volts CSVs. The original
dataset and report are preserved. Board-route comparisons retain the same PCB
array; `inferred_same_strip_*.csv` instead reverse the array mapping, assuming
corresponding strip contacts. For Test 22 the latter matching also reverses the
ADC-order key to preserve whether the tracked strip is read first or second.

## Verification

`tests/test_adc124_pcb_diagnostics.py` executes the actual diagnostic application
against the tagged ADC/SPI/GPIO model: every nonempty ADC mask, bank selection,
read order and repeats 1..3 across three consecutive cycles. It verifies event
labels, all returned words, checksum, park/error behavior, invalid commands,
USB discard accounting and long-isolation host timeout. Production driver,
scan-mode and benchmark tests remain separate. Build both Teensy environments
before upload; require zero data/transfer/USB errors and timing overruns in
hardware captures. Quiet analog data alone cannot prove physical channel identity.
