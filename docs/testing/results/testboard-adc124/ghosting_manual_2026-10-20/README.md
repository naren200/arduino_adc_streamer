# Manual ghosting comparison updated 20 October 2026

The original DOCX contains six blocking-SPI conditions and 48 array/MUX observations.
The user subsequently supplied three LPSPI rerun rates and reported unchanged ghosting.
The same sensor strip was moved between arrays for each test. Manual presses were uncontrolled.

## Engine comparison

- G0, ADC scan, sensor settle 5 us, VMID off: LPSPI 6,736 SPS/channel; blocking rate missing.
- G4, ADC scan, sensor settle 1 us, VMID wait 0 us, no VMID ADC scan: blocking 9,320 to LPSPI 10,000 SPS/channel (+7.30%).
- G5, ADC scan, sensor settle 1 us, VMID wait 5 us, no VMID ADC scan: blocking 5,620 to LPSPI 5,880 SPS/channel (+4.63%).
- LPSPI G1-G3 rates and all new main-signal amplitudes were not supplied; missing values remain blank.
- Current GUI and firmware defaults are now LPSPI. The original tests were blocking, confirmed by the user.
- Ghosting is qualitatively unchanged for the reruns; this does not establish equal main-signal fidelity.
- The 5 us wait adds 70.64 us per blocking cycle and 70.07 us per LPSPI cycle, consistent with 14 shared-within-array waits in ADC order.

## Conclusions and evidence

VMID visits strongly suppress the large observed ghosts. VMID ADC conversions are not essential for substantial suppression.
G5 remains the conservative validation candidate at 5,880 LPSPI SPS/channel. G4 reaches 10,000 but still needs a matched-input amplitude check.
No ghost is not encoded as zero percent. Source DOCX unchanged; no hardware configuration or acquisition was performed for this revision.

- source_records.json: original source paragraphs/tables, 48 records and subsequent user clarifications.
- manual_observations.csv: original blocking observations and amplitudes only.
- conditions.csv: original blocking configuration/rate data.
- engine_rate_comparison.csv and lpspi_rerun_source.json: new engine results and provenance.
- report_revisions/blocking_only: preserved previous PDF, builder, verification and analysis files.
- verification.json and pdf_quality.json: input hashes, table checks and visual QA.
- PDF: docs/reports/exports/TestBoard_ADC124_Ghosting_Comparison_2026-10-20.pdf.

Rebuild: `.venv/Scripts/python.exe Arduino_Sketches/TestBoard_ADC124/benchmarks/report_adc124_ghosting.py`
