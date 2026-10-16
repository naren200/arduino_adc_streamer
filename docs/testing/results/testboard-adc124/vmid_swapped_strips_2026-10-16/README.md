# Tests 21-23 after exchanging sensor strips - 16 October 2026

All 63 recordings completed using the exact original diagnostic image and acquisition plan.
Array A now carries the former Array B strip; Array B carries the former Array A strip.
Array names identify PCB circuitry. Individual connector-contact mapping was not independently verified.

## Results

- Test 21, over all 25 sensor routes per array: the median matched first-response change is +38.0 to -7.0 counts on Array A (original to swapped), and -1587.0 to -1484.0 counts on Array B.
- The larger downward response remains on Array B after exchanging strips. A property confined to its original strip cannot explain the entire Array B deficit. This supports an array-specific circuit contribution, without isolating the MUX chip from other board-path differences.
- Array A's median change reverses sign between sessions. The earlier upward response is therefore not a universal property established for the TMUX1108. Strip properties or acquisition/session history also matter.
- Test 23 reproduces the array-specific VMID-wait sensitivity: Array A MUX centers change +0.0 to +0.0 counts from 1 to 10 ms, while Array B changes -275.5 to -139.0 counts. This operating pattern stays with the PCB array after the strip exchange.
- At matching Test 23 settings, individual route baselines differ by at most 2 counts on Array A and 16 counts on Array B between sessions. The measured median cycle periods match exactly at the recorded microsecond resolution for all three VMID waits.
- All post-hold readings are measured after reconnection. The repeat supports conclusions about charge retention and recovery of the complete sensor path, not a direct measurement of off-state leakage current or its destination.

## Acquisition

- LPSPI, requested 16 MHz, both arrays coordinated, 5 us sensor settling.
- Test 21: three full before/post pairs, 20 s timed VMID park without conversions, exactly one full post-hold cycle.
- Test 22: MUX address 2, each MUX number, both ADC orders, three before/post pairs each, ten retained same-input conversions per ADC.
- Test 23: VMID waits 1/5/10 ms, three 12 s records each, exclude 2 s and analyze 10 s.
- Total parked interval exceeds the timed 20 s because of host setup; hold_intervals.csv retains both intervals.
- Temporal SD and peak-to-peak include drift and excursions; transient ten-reading bursts are not labelled noise.

## Integrity

- 846,274 cycles acquired; 846,228 received; 46 whole-cycle discards, all counted in fast before scans.
- Zero ADC data/transfer/USB-write errors and controlled timing overruns.
- All post-hold cycles, all long-VMID regular scans and every final before cycle received.
- Frozen production firmware and saved settings restored; acquisition stopped.
- Original dataset, result CSVs and 79-page report preserved.

## Files

- PDF: docs\reports\exports\TestBoard_ADC124_Swapped_Strips_Comparison_2026-10-16.pdf
- comparison.json and *_comparison.csv join the same board route between sessions.
- inferred_same_strip_*.csv track the reported strip movement, assuming matching connector contact order.
- Test 22 strip matching preserves whether the tracked strip is read first or second by reversing the ADC-order key.
- Per-route and per-repetition originals, counts/volts, baseline/noise/spread and holds are retained alongside comparisons.
- verification.json records image identity, restoration and evidence hashes.
- Raw data, setup, plan, commands, uploads, frozen firmware and frozen analysis tools: docs\testing\datasets\testboard-adc124\vmid_swapped_strips_2026-10-16

Recreate: `.venv/Scripts/python.exe Arduino_Sketches/TestBoard_ADC124/benchmarks/report_adc124_swapped_vmid.py`
