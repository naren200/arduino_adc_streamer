# Manual ghosting comparison prepared 20 October 2026

Source: docs/testing/benchmarks/testboard-adc124/Ghosting Tests TestBoard_ADC124.docx.
Six conditions G0-G5, 48 observations. Source unchanged; no hardware test or configuration change performed.

## Conclusions

- VMID visits strongly suppress the large ghosts seen without VMID.
- VMID ADC conversions are not essential for substantial suppression under the tested conditions.
- G5 (ADC order, 1 us sensor settle, 5 us VMID wait, no VMID ADC scan) is the conservative controlled-validation starting point at 5,620 samples/s per channel.
- G4 reaches 9,320 samples/s per channel, but its smaller Array B main amplitudes require a matched-input comparison before adoption.
- Current firmware and GUI profile defaults are blocking SPI, not LPSPI. Historical runtime engine identity is not independently logged.
- No ghost is an observation without a calibrated detection floor; it is not encoded as zero percent.

## Evidence

- source_records.json contains all source paragraphs/tables and normalized records.
- manual_observations.csv preserves every ghost observation and main-signal amplitude, with nominal unit conversions.
- Illustrative ghost-voltage products are not measured bounds and assume comparable amplitude metrics.
- conditions.csv contains the six configurations and reported GUI timing.
- PDF: docs/reports/exports/TestBoard_ADC124_Ghosting_Comparison_2026-10-20.pdf.
- verification.json records input hashes, independent PDF table checks and visual QA.
- report_builder.py is the reproducible builder snapshot.

Rebuild from the repository root:
`.venv/Scripts/python.exe Arduino_Sketches/TestBoard_ADC124/benchmarks/report_adc124_ghosting.py`
