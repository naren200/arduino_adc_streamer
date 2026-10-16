# Added long-VMID tests - 15 October 2026

Completed 63 additional recordings with the PZT strips attached and untouched,
LPSPI at requested 16 MHz, coordinated external MUX switching, both arrays and
5 us sensor settling. These append Tests 21-23 to the existing PCB report.
The original 762 recordings are preserved in their separate dataset.

## Procedure

1. **First full cycle after parking.** Record a 3.5 s continuous full 50-sensor
   scan with software VMID off. Keep its last cycle and its analyzed 2.5 s
   baseline window. Park every external MUX at address 7, prepare the ADC
   selectors, then wait a timed 20 s with no ADC conversions. Acquire exactly
   one full cycle, with no explicit VMID visit between addresses. Three repeats.
2. **Ten readings of one selected path.** Use the same before scan and timed
   hold. Switch both external MUX groups to address 2, wait 5 us, and acquire
   ten consecutive readings of ADC INm on each ADC. Repeat for MUX numbers
   m=1,2,3,4 and both ADC read orders. Three repeats per condition. One input
   per MUX is tested, on both arrays; the ADC input stays fixed during each burst.
3. **Regular full scan with long VMID intervals.** Read all 50 sensors,
   inserting address-7 VMID and four discarded conversions per ADC after each
   address group. VMID wait = 1000, 5000 or 10000 us; sensor wait remains 5 us.
   Three 12 s recordings per condition; exclude the first 2 s, analyze 10 s.

The 20.000000-20.000001 s interval is the firmware-timed no-conversion hold
after selector preparation. Total recorded last-pre-cycle-end to post-cycle-start
intervals are 21.675-21.799 s because host setup occurs while parked before the
timed hold. Both intervals are exported; do not interpret this as an exact
20.000 s electrical end-of-sensor-to-next-sensor interval.

Software VMID off still visits the physical reference at address 6 on MUXes
1-3. Address 6 on MUX 4 remains the seventh sensor. All 50 physical sensor
routes remain in full-cycle captures.

## Measured results

- In the first complete cycle after the long hold, MUX median changes across
  the same six addresses 0-5 are **+33.5 to +46 counts on A**, versus
  **-1709 to -1299 counts on B**, relative to the preceding continuous-scan
  baselines. These are transient first-cycle codes, not new steady baselines.
- Across ten consecutive readings, median tenth-minus-first changes span
  **+1 to +4 counts on A** and **+66 to +241 counts on B**. B remains far below
  the preceding scanning baseline after ten readings. Read order changes
  elapsed settling and prior conversion activity together.
- Increasing regular VMID wait from 1 to 10 ms changes A's MUX baseline
  centers by **0 to +0.5 count**, but B's by **-280.5 to -143 counts**.
  The 1 Mohm B paths (MUXes 1/3) change more than the 470 kohm paths (2/4).
- Median full-cycle periods are approximately **7191, 35191 and 70193 us**,
  giving **139.063, 28.416 and 14.246 samples/s per sensor**. Seven VMID waits
  are shared between arrays per cycle. Retained sensor-code throughput is
  50 times cycle rate; total ADC activity includes 56 discarded VMID reads.
- The largest absolute first-versus-last two-second median shift within a
  regular scan's analyzed window is **4 counts** across addresses 0-5 and all
  repetitions. Raw SD therefore includes residual drift; it is not automatically
  a white-noise floor. Sampling rate/bandwidth differ between VMID settings.

Long VMID parking also disconnects the sensors. These observations combine
sensor retention/leakage, bias/reconnection and reference/conversion history.
They do not isolate charge absorption by the local reference capacitor or
identify a single component cause. No intentional mechanical stimulus was
applied; vibration was not instrumented.

## Integrity and restoration

- 846,272 cycles acquired, 846,029 received, 243 counted whole-cycle USB
  discards, all in the fast before recordings.
- All 27 single post-hold cycles and all nine long-VMID regular scans were
  received without discards. Every before recording includes its final cycle.
- Zero transfer, ADC-data or USB-write errors; zero controlled timing overruns.
- Raw SHA-256, frame schema/checksum, sequence and cycle accounting verified.
- 84 focused regression tests and both firmware builds passed. The actual
  diagnostic C++ model verifies a simulated 20 s hold without intervening
  conversions, ten retained responses, selector correctness and stopping.
- Frozen original production firmware and saved settings restored and verified
  stopped. Temporary identity is `adc124-pcbdiag-v2`; production protocol unchanged.

## Files

- `analysis.json`, `captures.csv`: complete extension and accounting.
- `hold_intervals.csv`: timed holds and recorded pre-to-post intervals.
- `full_cycle_routes.csv`, `full_cycle_repetitions.csv`, `full_cycle_mux.csv`:
  before-window baseline, last pre-cycle code, first post-cycle code and changes.
- `ten_conversion_readings.csv`, `ten_conversion_repetitions.csv`,
  `ten_conversion_endpoints.csv`: every conversion position, repetitions and
  first/tenth comparisons. Ten-reading transient variation is not labelled noise.
- `long_scan_routes.csv`, `long_scan_repetitions.csv`, `long_scan_mux.csv`:
  per-route baselines, raw SD/range, drift and cycle rate; counts and volts.
- `figures/`: line graphs for the report. Bands are min-to-max repetition ranges.
- Raw data, frozen production/diagnostic images, tool snapshot and serial logs:
  `docs/testing/datasets/testboard-adc124/vmid_long_hold_2026-10-15`.
- Combined PDF: `docs/reports/exports/TestBoard_ADC124_PCB_Diagnostics_2026-10-15.pdf`.
  The prior 65-page explanatory report is archived under the original results'
  `report_revisions/explained/report.pdf`.

Recreate the extension analysis, then the combined report:

```powershell
.venv/Scripts/python.exe Arduino_Sketches/TestBoard_ADC124/benchmarks/analyze_adc124_long_vmid.py
.venv/Scripts/python.exe Arduino_Sketches/TestBoard_ADC124/benchmarks/report_pcb_diagnostics.py
```

Diagnostic timing includes logging/driver overhead and is not a measured
production-driver optimization. The original acquisition-era tool snapshot is
retained unchanged; this extension has its own frozen tools and firmware.
