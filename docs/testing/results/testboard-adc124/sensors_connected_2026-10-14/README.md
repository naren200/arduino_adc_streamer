# TestBoard_ADC124 - connected PZT experiment

Completed 28-setting LPSPI matrix, three repetitions per setting, compared against the latest same-firmware disconnected captures. Both arrays, manual repeat 1, burst SPI. Baseline plots are line graphs.

[PDF report](../../../../reports/exports/TestBoard_ADC124_Sensors_Connected_2026-10-14.pdf)

Unfiltered and common quiet-window baseline/noise data are retained in counts and volts. Cold-start attempts and every route's threshold coverage are in the startup subfolders. A missing one-third crossing is censored, never extrapolated. Time zero is first recorded sweep, not USB power edge.

Firmware unchanged; original settings restored and acquisition stopped. See analysis.json, setup_comparisons.csv, channel_comparisons.csv, selected_noise_audit.csv and figures/.

## Measured findings

84 connected captures; 7,091,086 full sweeps received, 268 accounted complete-sweep USB discards, zero firmware errors. Production stopped and original settings restored. All 85 focused checks passed. Local frozen firmware files match all 13 reference hashes.

Delivered rate changes across all exact matches: -0.00294% to +0.01184%.

| 5 us / 16 MHz | VMID | Connected Hz/channel | Median raw SD counts | Worst median raw SD counts | Largest individual-record p-p counts |
|---|---|---:|---:|---:|---:|
| ADC | off | 6748.965 | 0.04864 | 0.49768 | 5 |
| ADC | on | 3332.847 | 0.10395 | 0.49995 | 1 |
| MUX | off | 9213.612 | 0.06588 | 0.49883 | 2 |
| MUX | on | 4510.431 | 0.05377 | 0.49983 | 1 |

Largest exact-match baseline shift: +82 counts (+66.064 mV), B/MUX4/address 6, ADC scan, VMID off, 1 us, 16 MHz. Changes vary by route and setting; they are not calibrated offsets.

MUX/LPSPI/VMID off, 16 MHz, 1 and 5 us: no route/settings exceed median SD 0.5 counts with repeated ACF structure. Eleven route/settings have repeated structure below 0.5 counts; all eleven are tabulated and plotted. ADC short-settling bursts and rare 5 us spikes are shown separately. No electrical or mechanical cause is established.

Three physical power-off attempts are retained, including approximately 1 and 12 minutes unplugged. All 50 routes have startup curves. Nineteen B/3-to-2B/3 intervals are observed (0.187-0.920 ms in the 12-minute attempt); 31 are left-censored at first reading. Sample brackets accompany interpolated estimates. High first readings do not distinguish retained charge from a switched analog/ADC response. Time zero is the first sweep, not the power edge.

Noise-band arrival in the 12-minute attempt ranges from 0.543 to 85.633 ms using a 2-count band and the documented two-second confirmation criterion. All startup timing uses the 5 us MUX configuration.

## Detailed data

- [Every setting comparison](setup_comparisons.csv)
- [All 1,400 route comparisons](channel_comparisons.csv)
- [ADC input baseline centers](adc_input_baselines.csv)
- [Matched-address input spread](address_input_spread.csv)
- [Quiet per-channel measurements](channel_summary.csv)
- [Unfiltered per-channel measurements](unfiltered_channel_summary.csv)
- [Rare excursion audit](excursion_audit.csv)
- [All-route temporal pattern audit](selected_noise_audit.csv)
- [Startup channel timings and censoring](cold_poweron_12min_off/charge_channels.csv)
- [Timing budgets](timing_budgets.csv)
- [Verification record](verification.json)
