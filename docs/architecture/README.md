# architecture

How the current system is built. Test evidence lives in [`../testing/`](../testing/README.md), reviews in [`../reports/`](../reports/README.md), and open plans in [`../plans/`](../plans/README.md).

## gui/

- `gui/GUI_GENERIC_ACQUISITION_REFACTOR.md` — shared, profile-driven GUI acquisition: board JSON profiles, routing, workers, rendering, and replay validation.
- `gui/HEATMAP_IMPLEMENTATION.md` — heatmap file map, current behavior (live data, PZT/PZR modes, shared package grouping with shear, persisted settings). `Legacy/data_processing/` and `Legacy/gui/` hold separate archived copies of the same modules.

## firmware/

Test-board firmware designs, implementation notes and timing reports:

- [TestBoard_7953 capture timing](firmware/TESTBOARD_7953_CAPTURE_TIMING_REPORT.md) ([PDF](firmware/TESTBOARD_7953_CAPTURE_TIMING_REPORT.pdf)) - capture steps, sensor connection, ADC sampling and disconnected intervals; 21 October 2026.
- [TestBoard_ADC124 capture timing](firmware/TESTBOARD_ADC124_CAPTURE_TIMING_REPORT.md) ([PDF](firmware/TESTBOARD_ADC124_CAPTURE_TIMING_REPORT.pdf)) - external MUX timing, per-channel sampling offsets, measured budgets and calibration gaps; 21 October 2026.

- `firmware/TESTBOARD_7953_FIRMWARE_OPTIMIZATION.md` — firmware optimization candidate.
- `firmware/TESTBOARD_7953_PHASE_PROFILING.md` — phase profiling instrumentation and procedure.
- `firmware/TESTBOARD_7953_LPSPI_WORD_PATH.md` — LPSPI word-path candidate.
- `firmware/TESTBOARD_7953_LIVE_USB_STREAM.md` — keep sampling and discard sweeps when USB is busy.
