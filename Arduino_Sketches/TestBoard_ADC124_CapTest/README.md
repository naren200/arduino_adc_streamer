# TestBoard_ADC124_CapTest

Capacitance-test firmware (Teensy 4.1) for TestBoard_ADC124. Separate from the production firmware in `../TestBoard_ADC124/`, which this project only reads (four of its source files are compiled in, none is modified). The PC application is the shared CapTest tool in [`../../CapTest/`](../../CapTest/).

```text
platformio.ini     env teensy41_captest (pinned platform teensy@6.0.0)
src/, include/     CapBurstEngine (LPSPI frame loop with in-burst GPIO mux switch),
                   CapTestFirmware (commands, packet), CapTestProtocol.h (constants, flags)
scripts/           pre_captest.py (production USB-core patch + git hash);
                   flash_test.* / flash_production.* wrap ../../CapTest/scripts/flash_firmware.*
```

- Build: `pio run -d Arduino_Sketches/TestBoard_ADC124_CapTest`
- Flash the test firmware: `scripts/flash_test.ps1 [-Port COMx] [-DryRun] [-Yes]` / `scripts/flash_test.sh [--port P] [--dry-run] [--yes]`, or `pio run -d Arduino_Sketches/TestBoard_ADC124_CapTest -t upload`.
- Back to production: `scripts/flash_production.ps1` / `.sh`, or `pio run -d Arduino_Sketches/TestBoard_ADC124 -e teensy41 -t upload`. Never flash the `teensy41_profile` or `teensy41_pcbdiag` environments from these scripts.
- Identify what is on the board: `mcu*` answers `# TestBoard_ADC124_CapTest` (test) or `# TestBoard_ADC124` (production); `capstatus*` only exists in the test firmware.

Neither firmware stores anything in EEPROM or flash, so switching between them loses nothing.

Docs: [protocol and design](../../docs/architecture/firmware/TESTBOARD_ADC124_CAPTEST.md) · [plan](../../docs/plans/firmware/plan-testboard-adc124-capacitance-test.prompt.md).
