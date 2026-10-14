# TestBoard_ADC124 upgraded firmware - disconnected-array reference

2026-10-14. Complete; current production firmware restored and acquisition stopped.

[PDF report including unfiltered noise analysis and the complete selected-route audit](../../../../reports/exports/TestBoard_ADC124_Upgrade_Disconnected_Noise_2026-10-14_Rev2.pdf)

## Test matrix

Both array connectors disconnected; charger connected. Bias resistors A: 470k/1M/470k/1M; B: 1M/470k/1M/470k. Nominal reference 3.3 V; counts convert using 3.3/4096 V/count. Both full arrays (50 channels), manual acquisition, repeat 1, burst SPI framing.

- ADC and shared-MUX modes; blocking, DMA and LPSPI engines.
- At 16 MHz: sensor delays 1/2/5/10/50 us, with explicit VMID delay equal to sensor delay, VMID off/on.
- Extra sensor-5-us/VMID-on settings: VMID delays 0/1 us.
- At sensor=VMID=5 us: 8 MHz versus 16 MHz, VMID off/on.
- 84 production settings, three repetitions each: 1 s warm-up + 15 s measurement.
- 84 separate diagnostic settings: 1 s warm-up + 2 s measurement.

## Verification

252 production + 84 diagnostic captures. 18,420,451 complete sweeps received; 18,421,217 acquired; 766 accounted USB discards. Every sent frame was received. All firmware error counters are zero. Raw hashes, two independent decoders, 12-bit ranges, status/configuration, payload routes and timestamp accounting passed. Both PlatformIO environments built successfully; 101 focused protocol/statistical checks passed.

All 252/252 current production quiet records qualify. Minimum retained between-burst duration: 4.817 s. The final verification also rechecked all 270 historical production captures (180 shared/burst and 90 original sequential), for 606 total verified captures.

## LPSPI at matched 5 us settling, 16 MHz

| Scan mode | VMID | Samples/s/channel | Worst quiet SD (counts) | Worst quiet SD (V) | Worst quiet p-p (counts) | Worst quiet p-p (V) |
|---|---|---:|---:|---:|---:|---:|
| ADC | off | 6217.0 | 0.4985 | 0.00040162 | 2.0 | 0.00161133 |
| ADC | on | 3059.4 | 0.4891 | 0.00039406 | 2.0 | 0.00161133 |
| MUX | off | 8132.6 | 0.4974 | 0.00040078 | 2.0 | 0.00161133 |
| MUX | on | 4016.1 | 0.4934 | 0.00039753 | 2.0 | 0.00161133 |

## VMID delay experiments at sensor settling 5 us, 16 MHz

| Mode | VMID delay us | Samples/s/channel | Worst quiet SD counts | Extra rejected sweep % |
|---|---:|---:|---:|---:|
| ADC | 0 | 3899.5 | 0.4989 | 0.00 |
| ADC | 1 | 3678.1 | 0.4907 | 0.00 |
| ADC | 5 | 3059.4 | 0.4891 | 0.00 |
| MUX | 0 | 4694.8 | 0.8149 | 0.00 |
| MUX | 1 | 4545.5 | 0.4998 | 0.00 |
| MUX | 5 | 4016.1 | 0.4934 | 0.00 |

## SPI clock experiments at sensor=VMID=5 us

| Mode | VMID | 8 MHz samples/s/channel | 16 MHz samples/s/channel |
|---|---|---:|---:|
| ADC | off | 4401.5 | 6217.0 |
| ADC | on | 2147.9 | 3059.4 |
| MUX | off | 5299.7 | 8132.6 |
| MUX | on | 2579.5 | 4016.1 |

## Historical comparison

Both yesterday's original sequential firmware and its later shared-MUX/burst firmware are included. Old `scanorder mux` meant sequential arrays and maps to current ADC mode; current MUX results against that source include a scan-behavior change. The later shared/burst source matches scan mode directly. Comparisons use requested 16 MHz and sensor=VMID delay. Environmental drift is not eliminated; actual SCLK and reference rails were not probed.

## Quiet-window method and burst visibility

The primary values use two stages, re-applied to every generation: (1) the original eight-group 50 ms lower-envelope mask; (2) a shared sweep-time mask for clearly separated secondary states departing coherently on at least two routes, plus one neighboring sweep each side. The detector preserves unimodal Gaussian noise and isolated channel variation; it performs no independent channel clipping. See Arduino_Sketches/TestBoard_ADC124/benchmarks/adc124_quiet_windows.py and the PDF for exact histogram criteria.

The broad 50 ms view concealed repeated submillisecond excursions at 5 us MUX/LPSPI with VMID off. B/MUX1/input4 and B/MUX3/input4 had about 1.43 counts broader-window SD. Removing detected coherent excursions and guards leaves about 4.8 s of between-burst data and worst quiet SD about 0.50 counts. Extra rejected percentage includes guards and is not a measured charger duty cycle. The electrical source was not established. Both broader and conditional noise values are retained, so lower conditional SD does not conceal burst burden.

Historical printed reports used the broader method. These comparisons recalculate their raw data with the same two-stage method as today. Original sequential records had 15 s measurement windows; later shared/burst records had 2 s. Retained durations and sample counts differ, especially for peak-to-peak comparisons.

## Tables and figures

- [All setting summaries](summary.csv)
- [Per-channel baselines and noise, counts and volts](channel_summary.csv)
- [All channel repetitions](channel_repetitions.csv)
- [Capture timing, counters, masks and diagnostics](capture_metrics.csv)
- [Firmware speed/noise comparisons](firmware_comparisons.csv)
- [Matched-route baseline shifts and noise changes](channel_comparisons.csv)
- [Broader-window setting metrics](broader_window_summary.csv)
- [Broader-window firmware comparisons](firmware_broader_comparisons.csv)
- [MUX/ADC lane group baselines](mux_group_summary.csv)
- [Baseline spread at each external address](address_baseline_spread.csv)
- [Measured timing fits](delay_fits.csv)
- [Baseline line graphs and other figures](figures/)
- [Unfiltered noise interpretation, graphs and LDO context](NOISE_PATTERNS.md)
- [All 12,600 raw channel/repetition noise records](unfiltered_noise_channels.csv)
- [Complete MUX/LPSPI/VMID-off route audit at 1/5 us](selected_noise_route_summary.csv)
- [Selected-channel spectra](selected_noise_psd.csv)
- [Selected-channel raw time zooms](selected_noise_time_zoom.csv)

Each production capture also has a packed common mask (`quiet_mask.npz`, unpack bits and truncate to its `frames` value), fine detector states, coarse 50 ms bins and thresholds. Raw start times reconstruct mask timing. Statistics use sample SD (ddof=1), retained extrema for peak-to-peak, median baseline, and per-route medians across repetitions. Baseline spread/population SD across channels are separate from temporal noise.

## Raw evidence and final state

Current dataset: `docs\testing\datasets\testboard-adc124\upgrade_disconnected_2026-10-14`. Its `current_firmware/` holds frozen source/header files, production and diagnostic HEX images, local hashes and commit identity. Runtime revision/policies identify initial production; MCU image readback was not performed. The session uploaded the frozen diagnostic and restored production images.

Final revision `adc124-vmid-settle-v1`; `profile_available=false`; `running=false`. Restored both arrays, ADC scan, blocking engine, requested 16 MHz, repeat 1, sensor delay 5 us, inherited VMID delay 5 us, VMID off. Final status is in the dataset firmware_lifecycle.json. No firmware source changes, commit or push were made.

## Connected-sensor follow-up

Preserve this disconnected dataset as the reference. Once the sensor strips are connected, repeat the same settings with explicit connected-setup metadata and compare each physical route's baseline shift, quiet SD, peak-to-peak, and burst rejection against this dataset. Sampling-rate results should also be checked for consistency. Do not reuse disconnected setup labels for that next phase.
