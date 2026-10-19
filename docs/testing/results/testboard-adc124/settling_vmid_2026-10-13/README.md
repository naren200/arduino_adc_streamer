**Scan-order notice:** These earlier main results used `scanorder channel`. For the requested grouped scan, use the [fresh authoritative MUX results](../mux_2026-10-13/README.md). The original captures and reports are retained as historical evidence.

# TestBoard_ADC124 settling / VMID hardware characterization

Date: 2026-10-13. COM4 production firmware; no flash or protocol changes.

Report: [12-page PDF](../../../../reports/exports/TestBoard_ADC124_Settling_VMID_Comparison_2026-10-13.pdf).

Setup: both array connectors disconnected, charger connected, both full arrays (50 routes), manual conversion, repeat 1, ADC order (`scanorder channel`), LPSPI, requested 16 MHz SPI. Fixed nominal reference 3.3 V; VMID 1.65 V. Bias resistors A: 470k/1M/470k/1M; B: 1M/470k/1M/470k.

Three repetitions per combination, 1 s warm-up + 15 s measurement each. Settling delays 1, 2, 5, 10, 50 us, with VMID off and on. Ten configurations, 30 validated captures. Configuration order rotates between repetitions.

| Settling us | VMID | Median delivered samples/s/channel | Array A baseline spread counts | Array B baseline spread counts |
| ---: | --- | ---: | ---: | ---: |
| 1 | off | 3882.6 | 41.0 | 207.0 |
| 1 | on | 1351.3 | 35.0 | 55.0 |
| 2 | off | 3225.8 | 42.0 | 181.0 |
| 2 | on | 1190.5 | 35.0 | 59.0 |
| 5 | off | 2145.9 | 44.0 | 133.0 |
| 5 | on | 877.6 | 36.0 | 63.0 |
| 10 | off | 1379.3 | 41.0 | 89.0 |
| 10 | on | 609.9 | 36.0 | 60.0 |
| 50 | off | 356.5 | 16.0 | 11.0 |
| 50 | on | 177.3 | 29.0 | 12.0 |

All 729,434 sent sweeps were received and validated (36,471,700 channel values including warm-up). Firmware acquired 729,435 sweeps and discarded one whole sweep while USB was busy. No malformed frames, invalid codes or transfer/data/USB-write errors. The original stopped full-array, MUX-order, blocking, 16 MHz, repeat-1, 5 us, VMID-off configuration was restored.

Quiet selection: 50 ms complete bins; median channel sample-SD for each of eight MUX/ADC-input groups. Common mask rejects bins exceeding any group threshold plus one neighbor each side. Threshold=max(0.60 counts, P20+0.15 counts, lower-40%-median+4*1.4826*MAD). The 0.60-count floor prevents adjacent-code quantization at slow rates from being classified as a burst. No bin exceeded this criterion after warm-up; all measurement windows retained 15 s. No pronounced charger-burst pattern was observed by this criterion. Individual channel variations are retained.

Median-over-repetitions channel SD was at most 0.5003 counts (0.0004031 V), peak-to-peak at most 2 counts (0.0016113 V). Zero SD means a constant quantized code, not zero analog noise. Baseline uses quiet channel median then median across repetitions. Baseline spread is max-min across channel baselines; baseline SD uses ddof=0; time-domain SD uses ddof=1. V=counts*3.3/4096.

Artifacts:

- [Capture metrics](capture_metrics.csv), including received rate, firmware counter-derived rate estimate and quiet coverage.
- [Per-channel metrics](per_channel_metrics.csv): 500 channel-setting summaries with counts/V and repetition ranges.
- [Per-channel repetitions](per_channel_repetitions.csv): 1,500 rows with actual sample counts, extrema and noise.
- [Group summaries](group_summary.csv): 100 array/MUX-setting rows in counts and volts.
- [Analysis and thresholds](analysis.json), plus 30 bin-envelope/mask CSVs and report figures.
- [Raw evidence](../../../datasets/testboard-adc124/settling_vmid_2026-10-13/session.json): 30 binary capture files, status snapshots, SHA-256 hashes, restored status and command log.

Reproduce from the repository root (use a new capture directory):

```powershell
.venv/Scripts/python.exe Arduino_Sketches/TestBoard_ADC124/benchmarks/capture_adc124_settling.py <new-capture-directory>
.venv/Scripts/python.exe Arduino_Sketches/TestBoard_ADC124/benchmarks/report_adc124_settling.py <capture-directory> <analysis-directory> --pdf <report.pdf>
.venv/Scripts/python.exe -m pytest tests/test_adc124_settling_report.py tests/test_testboard_adc124_benchmark.py -q
```

Dependencies: pyserial, numpy, matplotlib, reportlab. Host validation: 54 focused tests passed. Capture decoding uses the existing incremental frame parser and an independent structured reader; hashes are rechecked during analysis. PDF rendered and visually reviewed on all 12 pages.
