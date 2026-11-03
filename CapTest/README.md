# CapTest

One PC application for the capacitance-test firmware of every board of the TestBoard family. It measures, per PZT sensor channel, the total capacitance `C_total` at the channel node and the ratio `C_D / C_total` (`C_D` = the shared mux-output node) from the charge-up curve after a sensor is connected to a parked mux-output node, either **automatically on all channels** or **manually on chosen channels**. Moved here from `Arduino_Sketches/TestBoard_7953_CapTest/`, which now holds the TestBoard_7953 firmware only.

| Board id | Hardware | Firmware project (test / production) | Channels | Protocol, guide |
| --- | --- | --- | --- | --- |
| `TestBoard_7953` | 4 x ADS7953, mux switched by the ADC's own command word | `Arduino_Sketches/TestBoard_7953_CapTest` / `Arduino_Sketches/TestBoard_7953` | 50 as `adc:channel` | [protocol](../docs/architecture/firmware/TESTBOARD_7953_CAPTEST.md), [guide](../docs/guides/TESTBOARD_7953_CAPTEST_USER_GUIDE.md) |
| `TestBoard_ADC124` | 2 x ADC124S101 behind 8 GPIO-switched external muxes | `Arduino_Sketches/TestBoard_ADC124_CapTest` / `Arduino_Sketches/TestBoard_ADC124` | 50 as `array:mux:input` | [protocol](../docs/architecture/firmware/TESTBOARD_ADC124_CAPTEST.md), [guide](../docs/guides/TESTBOARD_ADC124_CAPTEST_USER_GUIDE.md) |

General guide (board selection, both run modes, results, files): [`docs/guides/CAPTEST_USER_GUIDE.md`](../docs/guides/CAPTEST_USER_GUIDE.md). The list above is generated from [`boards.json`](boards.json) (`python -m captest --list-boards`).

## Start

From PowerShell, launch the GUI with the repository's `uv` environment:

```powershell
cd C:\Code\arduino_adc_streamer\CapTest
uv run captest --gui --board TestBoard_ADC124 --port COM4
```

Click **Connect**, then use **Automatic** and **Run automatic** to measure the
selected arrays. Python source files are in `CapTest/captest/`; measurement files
are in `CapTest/Results/<board id>/`.

The results grid stays sorted by Array, MUX, Input (ADC124) or ADC, Ch (7953),
including during acquisition and re-analysis. The **Help** tab explains every
result column, the calculations, quality flags and the selected board's settings.

```text
cd CapTest
python -m captest                          # GUI (also: uv run captest, or python captest)
python -m captest --simulate --gui         # GUI on the emulated board, no hardware
python -m captest --list-boards
python -m captest --board TestBoard_ADC124 --simulate --auto --array 1 --out run.json
python -m captest --board TestBoard_ADC124 --port COM4 --channels 1:1:0,1:4:6,2:2:0
python -m captest --board TestBoard_7953 --simulate --auto --adc 1,2
python -m captest --detect --port COM4     # which firmware / board is attached
python -m pytest tests                     # QT_QPA_PLATFORM=offscreen is set by the GUI tests
```

Needs Python 3.10+, `PyQt6`, `pyqtgraph`, `numpy`, `pyserial` (project dependencies; `scipy` is optional and only polishes the fits). Without `--board` the GUI opens on the last used board (its settings are in `settings/<board id>.json`), the CLI on the first board of `boards.json` (or the board of `--settings`). New results go to `results/<board id>/` when only a file name is given. Both folders are ignored by git.

## Folders

```text
boards.json         the board registry (see below)
boards/<id>.json    descriptor of a board: data (channels, VREF, VMID, SPI range, defaults, bias sources)
captest/            package: protocol, capfit (numerics), runner (sessions), client, link, simbase,
                    settings, cli, gui, flasher, boards/ (registry + one driver module per board)
scripts/            flash_firmware.ps1 / .sh: build + upload the test or production firmware of any board
tests/              pytest suite: offline, simulated boards (parametrised over boards.json), headless Qt
tools/              validate_qspice.py (optional, needs QSPICE64.exe)
Results/, settings/ local output and per-board saved settings (not committed)
```

Common code stays common (framing/CRC, serial link, client, fit, runner, results/exports, settings, plots, flasher); everything that differs between boards is in the descriptor (data) and the driver (code).

## The registry

`boards.json` lists the boards. `descriptor` is relative to this folder, every other path to the repository root. The loader validates every entry (required keys, unique ids, descriptor and driver load, the firmware projects exist and contain the named PlatformIO environment, flash scripts and documents exist); a broken entry stays in the list **disabled with the reason** (greyed out in the board selector, shown by `--list-boards`) and never breaks the others.

## Firmware switching

The Teensy runs one image at a time. `scripts/flash_firmware.ps1 -Board <id> -Firmware test|production [-Port COMx] [-DryRun] [-Yes]` (bash: `flash_firmware.sh --board <id> --firmware test|production [--port P] [--dry-run] [--yes]`) reads the projects and environments from `boards.json`, shows what it will flash and only uploads an image (nothing else is erased; neither firmware keeps EEPROM/flash state). It refuses the `teensy41_profile` and `teensy41_pcbdiag` environments. Every firmware project has `flash_test.*` / `flash_production.*` wrappers. The GUI buttons **Flash test firmware** and **Restore production firmware** call the same script (confirmation dialog, log, reconnect).

## Adding a board

1. **Firmware project** with the CapTest firmware, answering `capstatus*` with the keys `firmware=`, `board=`, `protocol=` and `mcu*` with its firmware name (see the two existing protocol documents; reuse the production sources read-only).
2. **Descriptor** `boards/<id>.json`: copy a neighbour; keys `id, name, adc{bits,vref_volts}, vmid_volts, spi{min_hz,max_hz,default_hz}, channels{syntax, example, id_columns, display, group_title, ...board specific layout}, connection_model, touches, defaults, settings_fields, discharge_check, vmid_read, bias, simulator`. `defaults` are the initial `Settings` values; `settings_fields` selects which settings the GUI shows (names from `captest/gui.py FIELD_SPECS`).
3. **Driver** `captest/boards/<module>.py` with `create_driver(descriptor, entry)` returning a `BoardDriver` (see `captest/boards/base.py`): channel model (`groups`, `parse_spec`, `key`, `display`, `identity`), default bias table, planning (`single`, `plan`: which channels one burst reads and which sensors it touches), `measure` (build the burst command, return the raw packet and its decoding), `decode`, `analyze` (turn the burst into `capfit.BurstRecord` and `capfit.analyze_record`), `vmid`, and the simulator (`sim_config`, `create_simulator`).
4. **Entry** in `boards.json` (firmware block, docs block, flash script stems).
5. **Simulator profile**: the emulated device behind `create_simulator`; it must also answer as the production firmware (`kind="production"`) so the switching UI can be tested.
6. **Tests**: the registry test and the board-parametrised end-to-end and GUI tests pick the new board up without edits; `tests/test_registry.py::test_adding_a_board_is_one_entry_a_descriptor_and_a_driver` builds a throw-away board from scratch to prove it.

Keep `capfit` board-neutral: it takes times + voltages (+ baseline) + R_bias and returns tau / C_total / ratio / flags.
