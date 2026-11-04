# reports

Reviews, diagnoses, and exported deliverables.

- [TestBoard_7953 capacitance measurement](TestBoard_7953_capacitance_report.md) — 2 November 2026, how the board measures a sensor, frame timeline, calculating `C_total`, `C_f` and `C_s` from ADC bursts, and scope results for ADC1 and ADC2.
- [TestBoard_7953 strip-swap comparison](TestBoard_7953_strip_swap_2026-11-04.md) — 4 November 2026, two strips swapped between ADC1-2 and ADC3-4, repeat runs, a no-strips control, per-channel board baselines and the link to the PCB net lengths.
- `firmware/` — firmware performance review, channel 11 firmware review, USB stream diagnosis.
- `gui/` — GUI performance review.
- `exports/` — exported PDF/DOCX reports. The current full summary is [TestBoard_7953_Test_Summary_2026-10-09.pdf](exports/TestBoard_7953_Test_Summary_2026-10-09.pdf), with an [editable Word copy](exports/TestBoard_7953_Test_Summary_2026-10-09.docx). It includes the four-ADC channel-11 controls, manual Vmid-off ghosting results at 5/10/20 MHz, and the Vmid-on comparison at 20 MHz. The earlier summary and standalone channel-11 report remain available.
- [Final 20/30 MHz comparison](exports/TestBoard_7953_Final_Comparison_2026-10-13.pdf) — 13 October 2026, LPSPI/manual/ADC order, both full arrays, Vmid on/off; Array 1 disconnected and Array 2 attached. Includes sampling rates, quiet-period noise in counts/volts, per-channel baselines and per-ADC spread.
