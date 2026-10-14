**Scan-order notice:** These earlier main results used `scanorder channel`. For the requested grouped scan, use the [fresh authoritative MUX results](../mux_2026-10-13/README.md). The original captures and reports are retained as historical evidence.

# TestBoard_ADC124 timing and engine comparison

Date: 2026-10-13. Production comparison and all 60 diagnostic profiles are complete. Ordinary production firmware and the original stopped configuration are restored and verified.

[Timing report PDF](../../../../reports/exports/TestBoard_ADC124_Timing_Engine_Comparison_2026-10-13.pdf)

## Firmware lifecycle and verified final state

The initial diagnostic upload reported programming/boot, but USB serial did not return. A pre-existing Teensy GUI loader was closed to avoid competing uploaders. The physical Program button restored production firmware. A second diagnostic upload and the final production restoration used one CLI uploader and a direct USB CDC 134-baud reboot, and both re-enumerated correctly. The initial failed attempt is retained for audit; its cause was not independently established.

Final status: TestBoard_ADC124, protocol 2, profiling unavailable, running false; both full arrays, blocking SPI, requested 16 MHz, repeat 1, MUX order, 5 us settling, VMID off; all error counters zero. [Firmware lifecycle/status and image/source hashes](firmware_lifecycle.json).

## Validated production results

90 ADC-order captures (three repeats per setting, 1 s warm-up + 5 s measured) plus 30 address-order controls (one repeat, 1 s warm-up + 3 s measured). Each matrix covers blocking/DMA/LPSPI, settling 1/2/5/10/50 us, VMID off/on, manual repeat 1, requested 16 MHz SPI, both full arrays.

Both connectors disconnected; charger connected. Bias A = 470k/1M/470k/1M, B = 1M/470k/1M/470k. Nominal reference 3.3 V and VMID 1.65 V.

Production: 1,085,688 complete frames received; 1,085,690 acquired sweeps, 2 intentionally discarded. Diagnostic: 60 captures (both orders, all three engines, five settling delays, VMID off/on), 1 s warm-up + 2 s measured each. Across all 180 captures, 1,472,776 complete sweeps received; 1,472,778 acquired sweeps, 2 intentionally discarded. All transmitted frames received, all ADC codes valid, independent parsers/timestamps/hashes agree, zero firmware error counters.

| Settling us | VMID | ADC order blocking Hz/ch | ADC order DMA Hz/ch | ADC order LPSPI Hz/ch | Address order LPSPI Hz/ch |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | off | 3753.7 | 2222.2 | 3882.5 | 7518.8 |
| 1 | on | 1288.9 | 711.2 | 1351.3 | 3759.4 |
| 2 | off | 3142.9 | 1990.6 | 3225.8 | 6711.4 |
| 2 | on | 1141.6 | 663.8 | 1190.5 | 3401.4 |
| 5 | off | 2109.7 | 1519.8 | 2145.9 | 5094.1 |
| 5 | on | 851.0 | 553.6 | 877.6 | 2651.3 |
| 10 | off | 1363.4 | 1090.2 | 1379.2 | 3610.1 |
| 10 | on | 597.0 | 433.7 | 609.9 | 1930.5 |
| 50 | off | 355.4 | 333.6 | 356.5 | 1091.4 |
| 50 | on | 176.2 | 158.6 | 177.3 | 610.8 |

## Timing derivation

ADC order: 50 groups, 52 actual MUX changes and 100 conversion words with VMID off; 100 changes and 350 words with VMID on. Address order: 14 groups, 16 changes/64 words off, 28 changes/134 words on. VMID-on mandatory handoff/end park calls are no-ops because the quads are already parked.

Each actual MUX change: EN off, 200 ns allowance, three address writes, EN on, 200 ns allowance, configured analog settling delay. Each conversion: nominal 16 clocks at 16 MHz = 1 us, plus firmware 100 ns CS setup and 100 ns high allowances. ADC track and conversion/output phases are 3 and 13 clocks; these overlap serial transfer and must not be added a second time. [TI ADC124S101 datasheet](https://www.ti.com/lit/ds/symlink/adc124s101.pdf).

At 5 us, LPSPI ADC-order VMID-off sweep: 260 us requested settling + 100 us clocking + 40.8 us explicit allowances + 64.20 us remaining acquisition + 1.00 us outside gap = 466.00 us. VMID-on: 500 + 350 + 110 + 178.37 + 1.12 = 1139.50 us. Numbers independently summarized/rounded.

The fixed remainder includes GPIO work, transfer and batch loops, delay-call overhead and interrupts. It is not a separately measured electrical switching time. USB enqueue overlaps acquisition; physical transfer/host latency is not inferable from rate. The core partial-buffer flush timer is nominally 75 us and asynchronous; it is not a 75 us sampling-loop wait.

Fitted ADC-order periods in us with settling in us: LPSPI approximately 205.72+51.987*delay off, 639.80+99.991*delay on; blocking 214.09+51.988*delay off, 675.61+99.982*delay on; DMA 398.01+51.985*delay off, 1306.18+99.986*delay on.

## Diagnostic phase and unit estimates

Profile means include warm-up, interrupts and probe overhead. Validation is nested inside transfer; transfer with VMID on includes VMID park/settle. Prepare/session/transfer are inside acquisition. Do not sum all profile fields.

At 5 us in LPSPI ADC order, VMID off: prepare 301.808 us/sweep, transfer 166.289 us/sweep, encode 0.242 us, USB enqueue 0.413 us. VMID on: prepare 290.562 us/sweep, transfer 858.288 us/sweep, encode 0.243 us, USB enqueue 0.415 us. Profile sweep periods are slower than production by about 1.99% off and 1.47% on at 5 us.

A uniform word/batch model is fitted to the ten VMID-off diagnostic profiles per engine across the two scan-order topologies. Estimated costs include diagnostic probes, driver work and CS allowances, not just pure SPI clocking:

| Engine | Estimated 16-bit word us | Read-batch overhead us | Prepare residual per actual switch us | Transfer fit RMS us |
| --- | ---: | ---: | ---: | ---: |
| blocking | 1.7237 | 0.0736 | 0.8088 | 0.3375 |
| dma | 3.5342 | 0.0961 | 0.8250 | 0.2446 |
| lpspi | 1.6021 | 0.1209 | 0.8059 | 0.2858 |

For a 5 us LPSPI address-group estimate: each single-value ADC-order visit costs approximately 9.13 us off or 23.07 us on, excluding shared sweep/session/USB work. Address-order groups for external inputs 0-5 produce four values in approximately 13.94 us off or 27.87 us on, so the amortized per-value group cost is 3.48 or 6.97 us. Input 6 produces one value and uses the single-value cost. These are diagnostic model estimates, not individual channel timestamps.

The per-switch residual includes 0.4 us explicit turn-off/turn-on allowances plus GPIO/delay/probe work. Exact electrical propagation and analog settling curves, individual sample apertures and USB host latency remain outside this experiment.
## Evidence and reproduction

- [120 capture-level metrics](production_capture_metrics.csv)
- [60 production setting summaries](production_summary.csv)
- [36 production timing fits](delay_fits.csv)
- [60 diagnostic capture metrics](diagnostic_capture_metrics.csv)
- [Diagnostic setting summaries](diagnostic_summary.csv)
- [Per-word and batch model estimates](unit_cost_estimates.csv)
- [Diagnostic raw-data session](../../../datasets/testboard-adc124/timing_profiles_retry_2026-10-13/session.json)
- [Complete analysis](timing_analysis.json)
- [Primary raw-data session](../../../datasets/testboard-adc124/timing_engines_2026-10-13/session.json)
- [Address-order control session](../../../datasets/testboard-adc124/timing_mux_control_2026-10-13/session.json)

From repository root:

```powershell
.venv/Scripts/python.exe scripts/capture_adc124_settling.py <new-primary-folder> --engines blocking dma lpspi --window-ms 5000
.venv/Scripts/python.exe scripts/capture_adc124_settling.py <new-controls-folder> --engines blocking dma lpspi --orders mux --repetitions 1 --window-ms 3000
.venv/Scripts/python.exe scripts/report_adc124_timing.py <primary-folder> <controls-folder> <profile-folder> <output-folder> --pdf <report.pdf>
.venv/Scripts/python.exe -m pytest tests/test_adc124_timing_model.py tests/test_adc124_settling_report.py tests/test_testboard_adc124_benchmark.py -q
```

60 focused tests passed. Existing production and diagnostic PlatformIO environments built successfully. PDF rendered and visually reviewed on all 11 pages. No firmware acquisition source/wire-format changes were made. Firmware image/source hashes and the pre-upload status are preserved.
