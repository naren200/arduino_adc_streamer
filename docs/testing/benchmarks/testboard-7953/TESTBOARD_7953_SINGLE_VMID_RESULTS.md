# TestBoard 7953: single-VMID production sampling at 20 and 30 MHz

Measured on November 4, 2026 after uploading the updated production `teensy41`
firmware to COM3. All six measured captures passed on their first attempt.
With all 50 sensor channels enabled, the median per-channel cadence was
9,615.4 samples/s at the requested 20 MHz SPI clock and 11,111.1 samples/s at
30 MHz, a 15.56% increase.

## Acquisition settings and single-VMID verification

- Both full arrays: ADC1/ADC3 channels 0–9 and ADC2/ADC4 channels 0–14.
- Manual ADC sequencing, LPSPI engine, `scanorder adc`, `channelrepeat 1`.
- `vmid true`, 2.5 V ADC input range, production profiling unavailable/off.
- Three repetitions per SPI setting, each with 1 s warm-up and 5 s measurement.
- Clock order: three 30 MHz captures, then three 20 MHz captures.

The uploaded source inserts exactly one channel-15 command between successive
sensor routes on each ADC. Two trailing commands per ADC scan flush the final
sensor result through the two-frame response pipeline and leave the ADC parked.
For repeat 1 and sensor channels 0, 1, 2, the commands are
`0, 15, 1, 15, 2, 15, 15`.

Runtime wire-command regression checks passed for the production and profiling
controllers, each covering 1,188 digital acquisition configurations. The
single-VMID sequence was verified from source and these digital tests; physical
MUX and SCLK waveforms were not measured with a logic analyzer.

## Results

| SPI setting | Samples/s per channel | Median channel period | Median acquisition duration | Acquired sweeps | USB-discarded sweeps |
| --- | ---: | ---: | ---: | ---: | ---: |
| 20 MHz | 9,615.4 | 104 us | 102 us | 173,064 | 92 (0.053%) |
| 30 MHz | 11,111.1 | 90 us | 89 us | 199,337 | 159 (0.080%) |

Rates are the reciprocal of the median device sweep-start period, with the
median taken across the three repetitions. Every sweep contains one value per
channel, so the sweep rate is also each channel's sample rate. All three
repetitions at each clock had the displayed median period. Acquisition duration
excludes the interval between sweeps; it is not the per-channel sampling period.

The acquired/discarded counts include warm-up. USB discards are completed
acquisitions intentionally omitted when the transport is busy. All submitted
frames were received and reconciled against stopped firmware counters.

All ADC error counters, transfer timeouts, engine start errors, returned-channel
errors, USB write errors, framing faults and stream-integrity faults were zero.
There were no sensor sampling periods over 1 ms. The largest reported sensor
period was 105 us at 20 MHz and 92 us at 30 MHz.

These results characterize speed and detected digital integrity. They do not
measure analog ghosting or establish analog accuracy. SPI values are requested
settings; 30 MHz remains experimental and above the ADS7953's 20 MHz rating.
The earlier [30 MHz report](TESTBOARD_7953_LPSPI_30MHZ_RESULTS.md) used optional
between-channel VMID disabled and is a different acquisition configuration.

## Evidence

The local session is under
`Arduino_Sketches/TestBoard_7953/benchmarks/results/single_vmid_spi20_30_20261104_20261104221117/`.
It retains raw captures, command logs, result/sample/channel-statistic CSVs,
per-attempt stopped status, the uploaded HEX/source snapshot, provenance, and
`speed_summary.json`. Generated session artifacts are ignored by Git.

Uploaded HEX SHA-256:
`2856BEF3CFB92E43DDC488C6B7C5F8CF2EBF4ACCE4139D40B548C6140F8C847E`.
The base commit was `a235aa4cd7b96f029282e6570aa1eea700a0e106`, with the
single-VMID firmware change applied locally before upload. The board was left
with production firmware, stopped at 20 MHz and VMID enabled.
