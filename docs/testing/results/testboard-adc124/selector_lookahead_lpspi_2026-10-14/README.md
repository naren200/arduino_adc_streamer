# TestBoard_ADC124 selector lookahead - full LPSPI disconnected rerun

14 October 2026. Complete; production firmware stopped and original settings restored.

[PDF report](../../../../reports/exports/TestBoard_ADC124_Selector_Lookahead_LPSPI_2026-10-14.pdf)

Current captured revision `adc124-selector-lookahead-v1`, policy `lookahead`, zero batch-prime frames. Matched reference: `adc124-vmid-settle-v1` from the previous extended disconnected test. Both arrays, 50 useful routes, manual timed acquisition, repeat 1, burst SPI, LPSPI only. Connectors remain disconnected; charger connected; TPS7A2033PDBVR LDO. Nominal reference 3.3 V, VMID 1.65 V. Biases A: 470k/1M/470k/1M; B: 1M/470k/1M/470k.

## Matrix and verification

28 settings x 3 repetitions: ADC/MUX scan, sensor settling 1/2/5/10/50 us, VMID off/on and matching VMID delay at 16 MHz; extra sensor-5-us/VMID-on waits 0/1 us; requested 8 versus 16 MHz at sensor=VMID=5 us, VMID off/on. Each recording has 1 s warm-up plus 15 s measurement. Only production LPSPI captures; DMA, blocking and profiling were not rerun.

84 current plus 84 matched previous captures verified. Current: 7,090,915 full sweeps received, 7,091,330 acquired, 415 accounted USB discards. Every sent frame received; all transfer/data/USB-write errors zero. Raw hashes, two independent decoders, status/routes, 12-bit ranges and timestamp accounting passed. Both PlatformIO builds passed; 81 focused Python checks and two native firmware checks passed.

Current quiet records qualified: 84/84; minimum retained quiet duration 14.698 s.

## Matched 5 us, requested 16 MHz

| Mode | VMID | Previous Hz/channel | Current Hz/channel | Gain % | Current raw worst SD counts | Current quiet worst SD counts |
|---|---|---:|---:|---:|---:|---:|
| ADC | off | 6217.0 | 6748.7 | 8.55 | 0.5582 | 0.5582 |
| ADC | on | 3059.4 | 3332.8 | 8.94 | 0.4995 | 0.4995 |
| MUX | off | 8132.6 | 9213.3 | 13.29 | 0.4956 | 0.4956 |
| MUX | on | 4016.1 | 4510.5 | 12.31 | 0.4998 | 0.4998 |

## Noise and baseline definitions

Unfiltered statistics include every measured sample; quiet statistics apply the same two-stage common all-channel mask to both generations. The 50 ms group-envelope stage and fine coherent separated-state stage retain their prior definitions. No independent channel clipping, smoothing or detrending is used for temporal SD/p-p. Baseline line graphs use full-window medians; quiet baselines are also saved. Per-setting values are medians of three per-route repetition metrics. Baseline spread across routes is distinct from temporal noise. Nominal conversion is 3.3/4096 V/count. Zero digital SD is not zero analog noise; a below-0.5-count route need not be white.

The 1/5 us MUX/LPSPI/VMID-off noise audit checks every route at 16 MHz. Selection requires median unfiltered SD >0.5 counts and maximum absolute autocorrelation at lags 1-64 >=0.05 in at least two repeats. Time/PSD figures show every selected current route, representative previously structured routes, and additional ADC-1-us/MUX-2-us cases. This describes sampled patterns without establishing a charger, rail or acquisition cause. PSD section/window coverage is saved; aliasing and ADC aperture uncertainty remain.

## Conversion history

Full sweeps now use 50 SPI words with VMID off, 106 on, versus 64/134 previously. A four-input group uses four conversions; address 6 uses one. VMID conversion responses remain discarded. Removing the selector prime changes both SPI overhead and analog dwell. MUX mode reads ADC B after A, adding dwell for B. Nominal word savings are arithmetic budgets, not oscilloscope measurements.

## Data

- [Every exact-setting firmware comparison](firmware_comparisons.csv)
- [Every route baseline shift and unfiltered noise comparison](channel_comparisons.csv)
- [Unfiltered channel summaries](unfiltered_channel_summary.csv)
- [Unfiltered repetitions and correlations](unfiltered_channel_repetitions.csv)
- [Quiet channel summaries](channel_summary.csv)
- [Capture timing and transport accounting](capture_metrics.csv)
- [Baseline line graphs and other figures](figures/)
- [All-route selected-noise audit](selected_noise_audit.csv)
- [Noise spectra](noise_psd.csv)
- [Raw time zooms](noise_time_zoom.csv)
- [PSD coverage](noise_psd_coverage.csv)

Raw current: `docs\testing\datasets\testboard-adc124\selector_lookahead_lpspi_2026-10-14`. Raw matched reference: `docs\testing\datasets\testboard-adc124\upgrade_disconnected_2026-10-14\production`. Current source/production and diagnostic HEX snapshots and hashes are frozen under `current_firmware/`; only the production HEX was uploaded for this run. Lifecycle, command logs, status before/after and raw binaries provide reproducibility.

Reproduce with the four `*_lookahead_lpspi.py` tools in `Arduino_Sketches/TestBoard_ADC124/benchmarks`. No firmware source changes, commit or push were made. Physical channel identity and connected-sensor behavior require separate experiments. Preserve both disconnected generations for the planned connected-sensor comparison.
