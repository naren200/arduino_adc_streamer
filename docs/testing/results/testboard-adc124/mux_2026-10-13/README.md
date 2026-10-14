# TestBoard_ADC124 - authoritative grouped-MUX results

Date: 2026-10-13. All 120 fresh captures use `scanorder mux`. These reports characterize the requested scan: one switch/settle per external address, ADC IN1-IN4 at inputs 0-5, and IN4 alone at input 6. Earlier channel-order main results are retained separately and are superseded for this scan plan.

- [Settling, VMID, quiet noise and baseline PDF (12 pages)](../../../../reports/exports/TestBoard_ADC124_MUX_Settling_VMID_2026-10-13.pdf)
- [Timing and SPI engines PDF (8 pages)](../../../../reports/exports/TestBoard_ADC124_MUX_Timing_Engines_2026-10-13.pdf)

## Test matrix and setup

90 production captures: 3 engines x 5 settling times x VMID off/on x 3 repetitions. Each uses 1 s warm-up + 15 s measurement; configuration order rotates between repetitions. The noise/baseline report uses the 30 LPSPI captures from this same matrix. An additional 30 diagnostic captures use all the same settings with one repetition, 1 s warm-up + 2 s measurement.

Both array connectors disconnected; charger connected; full 25-channel arrays A and B measured together, manual conversion / repeat 1, requested 16 MHz SPI, fixed nominal reference 3.3 V, VMID 1.65 V. Shared MUX/op-amp bias resistors A: 470k/1M/470k/1M; B: 1M/470k/1M/470k. One SPI bus, two sequential ADCs.

## Production rates

| Settling us | VMID | Blocking Hz/channel | DMA Hz/channel | LPSPI Hz/channel |
| ---: | --- | ---: | ---: | ---: |
| 1 | off | 7210.9 | 3929.3 | 7518.1 |
| 1 | on | 3597.1 | 1924.2 | 3759.4 |
| 2 | off | 6472.0 | 3703.7 | 6711.4 |
| 2 | on | 3268.0 | 1827.1 | 3401.4 |
| 5 | off | 4955.6 | 3150.4 | 5094.1 |
| 5 | on | 2566.4 | 1585.7 | 2651.3 |
| 10 | off | 3537.7 | 2512.6 | 3610.1 |
| 10 | on | 1886.8 | 1297.0 | 1930.5 |
| 50 | off | 1084.8 | 963.9 | 1091.4 |
| 50 | on | 606.2 | 528.9 | 610.8 |

Table uses medians of three repetition means. Useful total values/s = 50 x rate. Rate is received-frame throughput from device timestamps; counter-derived sampling/discard accounting is separate.

## Timing and quiet measurements

Grouped scan: 14 sensor-address groups per sweep. VMID off: 16 actual address changes (14 sensor + 2 mandatory parks), 64 16-bit words (14 primes + 50 retained). VMID on: 28 changes (14 sensor + 14 VMID parks), 134 words (64 sensor/prime + 70 VMID). The mandatory park calls are already parked with insertion on and do not add physical changes or waits.

LPSPI period fit: approximately 116.880 + 15.9884 x settling_delay us with VMID off; 237.851 + 27.9872 x settling_delay us with VMID on. The nominal 16 MHz ADC frame is 1 us, including both conversion and serial shifting. Requested settling, 200 ns switch allowances and 100 ns CS allowances are counted explicitly; GPIO/driver/interrupt work is the remaining acquisition cost.

Noise/baseline: common 50 ms time mask across all 50 channels, using the median channel SD within each of eight MUX/ADC-input groups. Threshold=max(0.60 counts, P20+0.15 counts, lower-40%-median+4*1.4826*MAD). Reject bins above any threshold plus one adjacent bin each side; no individual sample clipping or detrending. All 30 LPSPI captures retained the full 15 s measurement. No group-wide bursts exceeded this quantization-aware threshold; isolated/persistent channel variation is retained.

Largest repetition-median channel SD: 0.891375 counts. Largest repetition-median peak-to-peak: 8.0 counts. The strongest variation is B/MUX4/input0 at 1 us, VMID off. Volts=counts*3.3/4096. The report gives full per-channel and per-MUX values, baseline spread and repetition ranges.

## Integrity and final state

4,734,190 received complete sweeps across all 120 captures; 4,734,291 acquired, 101 firmware-accounted USB-busy whole-sweep discards. All transmitted frames were received; both independent parsers agreed on samples/timing/counts, codes were valid, device timestamps were monotonic after wrap handling, and firmware error counters stayed zero. Hashes were rechecked during analysis.

The diagnostic firmware upload/reboot and final production restoration both completed without user intervention. One CLI uploader and direct USB CDC reboot were used. Final status verifies ordinary production firmware (profiling unavailable), stopped, with original full-array/MUX-order/blocking/16 MHz/repeat-1/5 us/VMID-off settings. No firmware-source or protocol changes were made.

## Evidence

- [Production raw session, commands, statuses and hashes](../../../datasets/testboard-adc124/mux_production_2026-10-13/session.json)
- [Diagnostic raw session](../../../datasets/testboard-adc124/mux_profiles_2026-10-13/session.json)
- [Quiet analysis and thresholds](../mux_settling_2026-10-13/analysis.json)
- [500 per-channel noise/baseline summaries](../mux_settling_2026-10-13/per_channel_metrics.csv)
- [1,500 per-channel repetition rows](../mux_settling_2026-10-13/per_channel_repetitions.csv)
- [100 per-array/MUX group summaries](../mux_settling_2026-10-13/group_summary.csv)
- [90 production timing rows](../mux_timing_2026-10-13/production_capture_metrics.csv)
- [30 production timing summaries](../mux_timing_2026-10-13/production_summary.csv)
- [30 diagnostic phase rows](../mux_timing_2026-10-13/diagnostic_capture_metrics.csv)
- [Period fits](../mux_timing_2026-10-13/delay_fits.csv)
- [Firmware lifecycle, uploads, hashes and verified final status](../mux_timing_2026-10-13/firmware_lifecycle.json)

## Reproduce from repository root

```powershell
.venv/Scripts/python.exe scripts/capture_adc124_settling.py <new-production-folder> --orders mux --engines blocking dma lpspi --window-ms 15000
.venv/Scripts/python.exe scripts/run_adc124_mux_profiles.py <new-profile-folder> <timing-output-folder>
.venv/Scripts/python.exe scripts/report_adc124_settling.py <production-folder> <noise-output-folder> --engine lpspi --pdf <noise-report.pdf>
.venv/Scripts/python.exe scripts/report_adc124_mux_timing.py <production-folder> <profile-folder> <timing-output-folder> --pdf <timing-report.pdf>
.venv/Scripts/python.exe -m pytest tests/test_adc124_settling_report.py tests/test_adc124_timing_model.py tests/test_testboard_adc124_benchmark.py -q
```

61 focused host tests passed. Both PDFs rendered and visually reviewed on all 20 pages. Physical-channel reporting explicitly reorders the verified grouped wire payload to the array/MUX/input display order; the new regression test checks that mapping. Report generation refuses channel-order data for the grouped-MUX report. Electrical edge timing, stimulated channel identity, attached-sensor ghosting and USB host latency are not established by these captures.
