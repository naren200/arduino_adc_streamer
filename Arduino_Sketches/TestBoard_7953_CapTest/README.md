# TestBoard_7953_CapTest

Capacitance-test firmware (Teensy 4.1) for TestBoard_7953. Separate from the production firmware in `../TestBoard_7953/`, which this project only reads (four of its source files are compiled in, none is modified). The PC application is the shared CapTest tool in [`../../CapTest/`](../../CapTest/) (moved there from this folder; it also serves TestBoard_ADC124).

```text
platformio.ini     env teensy41_captest (pinned platform teensy@6.0.0)
src/, include/     BurstEngine (tight LPSPI burst loop), CapTestFirmware (commands, packet)
scripts/           flash_test.* / flash_production.* (PowerShell + bash), pre_captest.py
```

- Build and flash: `pio run -d Arduino_Sketches/TestBoard_7953_CapTest -t upload`, or `scripts/flash_test.ps1 [-Port COMx] [-DryRun] [-Yes]` / `scripts/flash_test.sh [--port P] [--dry-run] [--yes]`.
- Back to production: `scripts/flash_production.ps1` / `.sh`, or `pio run -d Arduino_Sketches/TestBoard_7953 -e teensy41 -t upload`.
- Application: `cd CapTest; python -m captest` (GUI), `python -m captest --simulate --auto` (CLI on the emulated board). Tests: `cd CapTest; python -m pytest tests`.

Neither firmware stores anything in EEPROM or flash, so switching between them loses nothing.

Docs: [protocol and design](../../docs/architecture/firmware/TESTBOARD_7953_CAPTEST.md) · [user guide](../../docs/guides/TESTBOARD_7953_CAPTEST_USER_GUIDE.md) · [verification checklist](../../docs/guides/TESTBOARD_7953_CAPTEST_VERIFICATION_CHECKLIST.md) · [results](../../docs/testing/results/testboard-7953/capacitance-test/RESULTS.md) · [plan](../../docs/plans/firmware/plan-testboard7953-capacitance-test.prompt.md).

## Continuous capture (1.4.0)

`capcaps`, `capcfg`, `capstart`, `capstop`, `capstat` stream whole sweeps of up to 64 routes as
CRC-checked chunks (`src/ScanEngine.cpp`, `include/ScanEngine.h`; the frame runner shared with the
burst engine is `include/FrameRunner.h`). See `docs/architecture/firmware/TESTBOARD_7953_CAPTEST.md`.
Build check: `pio run` in this folder (`teensy41_captest`); the capture has not been run on the
board yet.
