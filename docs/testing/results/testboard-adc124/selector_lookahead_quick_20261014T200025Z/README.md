# ADC124 selector lookahead: quick hardware benchmark

2026-10-14 | COM4 | both sensor connectors disconnected | charger connected

Matched previous production (`adc124-vmid-settle-v1`) versus new production (`adc124-selector-lookahead-v1`). Both arrays, 50 channels, repeat 1, burst framing, sensor settling 5 us; default VMID settling follows 5 us. Each setting has one repetition, 0.5 s warm-up and 2 s measurement. Four additional LPSPI/16 MHz captures check explicit VMID settling 0/1 us in both scan modes. These are short sequential checks; noise drift and run-to-run variability are not eliminated.

52 validated captures; 582,046 complete frames received; 582,056 acquired; 10 accounted USB discards. Every sent frame was received. Transfer, data and USB-write errors: zero. Raw hashes, independent parsers, payload route labels, 12-bit ranges and nonoverlapping device timestamps passed.

## Matched complete-sweep rates

| MHz | Mode | Engine | VMID | Before sweeps/s | After sweeps/s | Gain |
| --- | --- | --- | --- | ---: | ---: | ---: |
| 8 | adc | blocking | off | 4245.0 | 4889.6 | +15.19% |
| 8 | adc | blocking | on | 2060.4 | 2365.4 | +14.80% |
| 8 | adc | dma | off | 3909.2 | 4424.8 | +13.19% |
| 8 | adc | dma | on | 1909.5 | 2157.5 | +12.99% |
| 8 | adc | lpspi | off | 4401.5 | 5046.8 | +14.66% |
| 8 | adc | lpspi | on | 2147.9 | 2450.3 | +14.08% |
| 8 | mux | blocking | off | 5056.8 | 6024.1 | +19.13% |
| 8 | mux | blocking | on | 2451.0 | 2901.4 | +18.38% |
| 8 | mux | dma | off | 4598.0 | 5326.2 | +15.84% |
| 8 | mux | dma | on | 2239.2 | 2587.3 | +15.55% |
| 8 | mux | lpspi | off | 5299.7 | 6229.5 | +17.55% |
| 8 | mux | lpspi | on | 2579.5 | 3024.2 | +17.24% |
| 16 | adc | blocking | off | 5880.4 | 6535.9 | +11.15% |
| 16 | adc | blocking | on | 2881.8 | 3198.3 | +10.98% |
| 16 | adc | dma | off | 5306.8 | 5799.8 | +9.29% |
| 16 | adc | dma | on | 2614.2 | 2855.0 | +9.21% |
| 16 | adc | lpspi | off | 6217.2 | 6748.9 | +8.55% |
| 16 | adc | lpspi | on | 3059.4 | 3332.8 | +8.94% |
| 16 | mux | blocking | off | 7575.8 | 8771.9 | +15.79% |
| 16 | mux | blocking | on | 3708.3 | 4262.2 | +14.94% |
| 16 | mux | dma | off | 6633.1 | 7417.6 | +11.83% |
| 16 | mux | dma | on | 3267.2 | 3650.9 | +11.74% |
| 16 | mux | lpspi | off | 8132.9 | 9213.7 | +13.29% |
| 16 | mux | lpspi | on | 4016.1 | 4510.4 | +12.31% |

## Independent VMID controls

| Mode | VMID us | Sweeps/s | Worst unfiltered SD, counts |
| --- | ---: | ---: | ---: |
| adc | 0 | 4336.4 | 0.500 |
| adc | 1 | 4097.0 | 0.500 |
| mux | 0 | 5364.3 | 0.491 |
| mux | 1 | 5137.0 | 0.496 |

## Samples and limitations

Across new-firmware captures, per-channel medians span 1855.0â€“2055.0 counts (1.4945â€“1.6556 V). Worst channel SD: 1.046 counts. 16 captures exceed the smoke warning thresholds of 100-count median offset from VMID or 10-count SD.

Previous-firmware median offsets reach 173 counts. Largest matched median change: -28 counts (-0.0226 V), route `2:1:3`, adc/blocking, 8 MHz, VMID off. Shortening the conversion sequence changes analog history; this run does not establish the cause of the offsets or remove their warnings.

Noise statistics include all measured samples without clipping, detrending or a quiet-window mask. This quick run verifies streaming and near-VMID response; it does not qualify connected-sensor noise or prove channel identity electrically. ADC124 has no returned channel tag. Nominal steady-state word counts are 50 with VMID off / 106 on, versus 64 / 134 before; startup selector initialization is outside sweep counters. Individual SCLK/CS edges were not probed.

New production firmware remains installed, stopped, with original settings restored. Exact final settings are retained in firmware_lifecycle.json.

Raw evidence: `docs\testing\datasets\testboard-adc124\selector_lookahead_quick_20261014T200025Z`.
