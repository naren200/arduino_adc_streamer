# TestBoard_ADC124 capacitance test: results

Run 2026-10-23, branch `feature/testboard-adc124-captest` (created from the not yet merged `feature/testboard7953-captest`), host Windows 11, PlatformIO 6.2.0, `teensy@6.0.0`, Python 3.12 + scipy 1.17 and Python 3.11 without scipy, PyQt6, numpy; board on COM4 (Teensy 4.1, TestBoard_ADC124; array 1 has sensors attached, array 2 reads like open inputs, see below). Design and protocol: [`TESTBOARD_ADC124_CAPTEST.md`](../../../../architecture/firmware/TESTBOARD_ADC124_CAPTEST.md). Guides: [`CAPTEST_USER_GUIDE.md`](../../../../guides/CAPTEST_USER_GUIDE.md), [`TESTBOARD_ADC124_CAPTEST_USER_GUIDE.md`](../../../../guides/TESTBOARD_ADC124_CAPTEST_USER_GUIDE.md). What still needs instruments: [`TESTBOARD_ADC124_CAPTEST_VERIFICATION_CHECKLIST.md`](../../../../guides/TESTBOARD_ADC124_CAPTEST_VERIFICATION_CHECKLIST.md).

## Builds

| Project | FLASH code / data / headers | RAM1 variables / code | RAM2 | `firmware.hex` SHA-256 |
| --- | ---: | ---: | ---: | --- |
| `Arduino_Sketches/TestBoard_ADC124` (production), before any change | 34,480 / 7,448 / 8,244 | 59,840 / 31,928 | 12,480 | `10c89bbbd5fb6643e6f747c605003718ff50ce222a02a0a85e452631901d507b` |
| same, at the end of the work and when flashed by `flash_production` | unchanged | unchanged | unchanged | **identical** |
| `Arduino_Sketches/TestBoard_ADC124_CapTest` (test, `teensy41_captest`) | 28,016 / 7,448 / 8,564 | 106,048 / 25,464 | 12,480 | `87fe9e4246452d00d279533b18f714301e56725e8c168bb4497e3165fe375de9` (git `adad98d`; the hash changes with the embedded git revision) |
| `Arduino_Sketches/TestBoard_7953` (production) | 36,208 / 7,448 / 8,564 | 17,664 / 33,656 | 12,832 | `844d87fb8997dcb381e8d8a162bc6d39af667aa6cf641dfcba3b8135a0036dbc` (same as recorded for the 7953 work) |
| `Arduino_Sketches/TestBoard_7953_CapTest` (test) | 27,504 / 7,448 / 9,076 | 33,376 / 24,952 | 12,544 | code size as recorded; the hash varies with the git revision |

`git diff --stat c6c4b27 -- Arduino_Sketches/TestBoard_ADC124 Arduino_Sketches/TestBoard_7953 Arduino_Sketches/TestBoard_7953_CapTest/src Arduino_Sketches/TestBoard_7953_CapTest/include Arduino_Sketches/TestBoard_7953_CapTest/platformio.ini` is empty: no production source and no 7953 CapTest firmware source was changed. The test firmware has 46 KB more RAM1 than production (the 4096-frame result arrays and the packet buffer); flash 6 KB less (no profiler, no address-change scan modes).

## Offline checks

| Check | Result |
| --- | --- |
| `python -m pytest CapTest/tests` (Python 3.12, scipy) | **240 passed** (89 of them are the pre-existing TestBoard_7953 tests, unchanged) |
| same, Python 3.11 without scipy (numpy-only fits) | **240 passed** |
| production host checks `uv run --with ziglang --with unicorn --with pyelftools pytest tests/test_testboard_adc124_benchmark.py tests/test_testboard_adc124_firmware.py tests/test_adc124_scan_modes.py tests/test_testboard_adc124_usb_patch.py` | **80 passed** |
| Move regression gate: seeded simulated 7953 automatic run before the move, after the move and after the generalisation | results JSON and CSV byte-identical; raw packets identical (only `wall_time` differs); packet hash and numbers are now a permanent test (`test_regression_7953.py`) |
| Accuracy on the simulated ADC124 (1 LSB rms noise, R_bias 470 k / 1 M, C_total 60 / 80 / 100 pF) | C_total within 2 % and C_D/C_total within 0.01 for all six cases; four interleaved channels of one burst within 2 % / 0.015 |
| QSPICE validation (`tools/validate_qspice_adc124.py`, independent circuit model of S2 -> TMUX1308A -> OPA365) | Cp 60 pF: C_total 79.22 pF vs 78.90 pF in the model (+0.4 %), C_D 11.85 vs 11.90 pF, ratio 0.150 vs 0.151; Cp 100 pF: 119.22 vs 118.90 pF, ratio 0.099 vs 0.100; Delta C_total for Delta Cp = 40 pF: **40.00 pF** |

Coverage: capfit and the board-neutral entry point, ADC124 protocol (round trip, the frame documented and measured on the board, CRC failure, truncation, rejections, identification of production / test / diagnostic firmware, board detection), driver (channel model, address-grouped planning, time model, quality flags), end-to-end automatic and manual modes parametrised over every board of `boards.json` (order, progress, Stop leaves the board parked, failed bursts, re-measure, the "too soon" warning including side-effect connections of neighbouring muxes, boot-id reset, discharge check, raw files, exports, CLI), the registry (the real file, malformed entries, a throw-away fake board), headless Qt on both boards (board switching, per-board settings, firmware/board mismatch, raw files select their board), the shared flash scripts (`--dry-run` with PowerShell and bash, wrappers, refusal of the diagnostic/profile environments).

## Firmware on the board

Flashed with `flash_test` (the first time with `pio run -t upload`) over COM4; the board reported the production firmware before. Details, packet layout and the jitter table: [`TESTBOARD_ADC124_CAPTEST.md`](../../../../architecture/firmware/TESTBOARD_ADC124_CAPTEST.md).

| Check | Result |
| --- | --- |
| `mcu*`, `capstatus*` | `# TestBoard_ADC124_CapTest`; `firmware=TestBoard_ADC124_CapTest board=TestBoard_ADC124 version=1.0.0 protocol=1 ... state=idle` |
| `capvmid` (all 8 inputs) | 2048..2052 counts, mean 2050.8 (1.652 V), no data errors |
| in-burst GPIO switch | deterministic: switch duration 611.7 ns, standard deviation 0.0 ns over 200 bursts (1.67 ns resolution), begins 53.3 ns after the last pre-connect pop; the "switch before the burst" fallback was not needed |
| frame period | 1037.1-1037.7 ns at 16 MHz (16 clocks + 37 ns gap), 2070.8 ns at 8 MHz; no transfer or data errors in any run |
| maximum burst | 4096 frames, 64 pre-connect frames, 24,642 bytes: ok |
| rejected commands | address 7, address 6 with mask 15, array 3, mask 0, 7 MHz clock, bad pre/samples: `#NOT_OK`, no packet |
| abort | a byte sent during a burst ended it at a frame boundary (`FLAG_ABORTED`, 814 frames returned) and was then processed |
| step at the intended frame | pre-connect samples at VMID (2048..2051), the step appears in the first frame after the switch |

## CapTest on the board (CLI and GUI)

Raw bursts and results of the runs are in this folder on the measuring PC (`auto_both_arrays_m3.json` / `.csv` / `_raw.json`: both arrays, 3 bursts per channel; `discharge_long_1_2_1_raw.json`); they are not committed because the repository ignores result JSON/CSV (`/docs/testing/results/**/*.json`), so the numbers that matter are in the tables below. Re-analysis: `python -m captest --reanalyze auto_both_arrays_m3_raw.json --fit 1exp`. The headless GUI (the real window code, `QT_QPA_PLATFORM=offscreen`, real serial port) was also driven against the board: it detected that the attached CapTest firmware belongs to TestBoard_ADC124 while TestBoard_7953 was selected, switched board, ran the automatic mode on array 1 (7 connections, 25 rows) and read VMID.

Automatic run, both arrays, M = 3 (single-exponential fit, default of this board):

| Array / MUX | R_bias | tau [us] (range) | C_total [pF] (range) | residual | step amplitude |
| --- | ---: | --- | --- | ---: | --- |
| 1 / MUX1 | 470 k | 33.0 (32.4-33.8) | 70.2 (69.0-72.0) | 0.5 LSB | 55-64 LSB |
| 1 / MUX2 | 1 M | 72.7 (71.6-74.2) | 72.7 (71.6-74.2) | 0.4 LSB | 56-68 LSB |
| 1 / MUX3 | 470 k | 34.2 (33.4-37.1) | 72.8 (71.1-79.0) | 0.5 LSB | 56-66 LSB |
| 1 / MUX4 | 1 M | 71.9 (70.9-73.1) | 71.9 (70.9-73.1) | 0.4 LSB | 55-70 LSB |
| 2 / MUX1 | 1 M | 17.8 (16.8-21.3) | 17.8 (16.8-21.3) | 0.6 LSB | 960-1150 LSB |
| 2 / MUX2 | 470 k | 8.1 (7.9-8.4) | 17.3 (16.8-18.0) | 0.8 LSB | 990-1100 LSB |
| 2 / MUX3 | 1 M | 18.0 (17.7-18.3) | 18.0 (17.7-18.3) | 0.6 LSB | 1015-1040 LSB |
| 2 / MUX4 | 470 k | 8.3 (7.8-9.1) | 17.7 (16.6-19.5) | 0.7 LSB | 1000-1130 LSB |

What the data say (measurements, not yet confirmed with instruments):

- **tau scales with R_bias**: on array 1 the 1 M muxes read 2.2 x the tau of the 470 k muxes on the same address (the resistor ratio is 2.13) and all 25 channels give C_total = 70-79 pF; the single exponential fits to 0.4-0.5 LSB rms (no structured residual, unlike the PZT strips on the 7953), so the one-exponential model is the right default here. The two-exponential option picked up a spurious slow tail on the large-amplitude array-2 channels (it reported tau_slow 200-1300 us), so it is available but not the default.
- **Array 2 behaves like 25 open inputs** (no sensor): C_total 17-18 pF on all of them, tau 8 us on the 470 k muxes and 17-18 us on the 1 M muxes (the same C_total behind both resistors), start voltage 0 V, ratio 0.5, which gives **C_D about 8.9 pF for MUX1/3 and 8.6-8.7 pF for MUX2/4** (the rest, about 8 pF, is pin + trace of the open input). A separate run measured 2:1:0, 2:1:1, 2:1:3 at **8 MHz: tau 16.95 / 17.06 / 16.90 us and at 16 MHz: 17.06 / 16.91 us** (same channels, 1 burst each): the time model does not depend on the SPI clock.
- **The ratio is not valid for array 1 yet**: the sensors there were still near VMID from earlier connections (step amplitude only 55-70 LSB, C_D/C_total near 1.0 with V_s0 = 0). `discharge_check` on 1:2:1 with waits of 2 / 6 / 15 / 40 / 90 s gives V0 = 1.602 / 1.611 / 1.628 / 1.657 / 1.685 V (pre-connect level 1.652 V): the sensor node does not return to 0 V, it **keeps drifting upward by about 1 mV/s** (a leakage of tens of femtoamps into 70 pF, or dielectric absorption, temperature or piezoelectric charge: not separated). `V_s0 = 0` therefore cannot be reached by waiting on this board; tau / C_total are usable as long as the step is well above the 50 LSB `LOW_AMPLITUDE` limit, the ratio only for sensors or open inputs that start at a known voltage. See the checklist for how to get a known start voltage.
- C_total of array 1 is 70-73 pF in this run but 79-90 pF in the first single-burst run on the same channels, taken when the steps were somewhat larger: with a step of a few tens of LSB the fit is sensitive to slow drifts, so repeat the array-1 numbers on discharged sensors.

## Restore path

`flash_production` (shared script, `-Port COM4 -Yes`) built and uploaded the production firmware: image SHA-256 `10c89bbb...507b` (the baseline hash), then `mcu*` answered `# TestBoard_ADC124` and `status*` the production list (`# board=TestBoard_ADC124`, `# protocol_version=3`, ...); `capstatus*` answered `#NOT_OK`; CapTest `--detect` reported "production of board TestBoard_ADC124", also when TestBoard_7953 was selected. The sequence production -> test -> production -> test -> production was run (the later steps with `flash_test` / `flash_production`).

**State of the board at the end of this work: production firmware (`# TestBoard_ADC124`, hex `10c89bbb...507b`).** To measure again press **Flash test firmware** in CapTest (board TestBoard_ADC124) or run `Arduino_Sketches/TestBoard_ADC124_CapTest/scripts/flash_test.ps1`.

## Not done / needs the user's equipment

Everything in [`TESTBOARD_ADC124_CAPTEST_VERIFICATION_CHECKLIST.md`](../../../../guides/TESTBOARD_ADC124_CAPTEST_VERIFICATION_CHECKLIST.md): scope on the mux output and AIN (real connection edge, charge injection, kickback), logic analyzer on CS/SCLK/EN/address (true timing, ADC hold instant, MUX-to-IN identity), LCR reading of a disconnected sensor, `R_bias` with a meter, a second temperature, the two-resistor cross-check, a way to start the sensors at a known voltage. Not measured here: the 8 vs 16 MHz comparison on sensors with a fresh start voltage (done on open inputs only), the repeat of array 1 on discharged sensors.
