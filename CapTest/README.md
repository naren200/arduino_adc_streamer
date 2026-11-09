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

## Force export and Force Calibration

TestBoard_7953 only (`supports_force_calibration` in its driver; other boards do not show the tab):

- `captest/sensor_groups.py` resolves the PZT packages from the sensor library.
- `captest/force_export.py` writes/updates `captest-force-inputs-1` files (partial files allowed).
- `captest/capture.py` + `client.py` + `simscan.py` implement the continuous-capture protocol
  (firmware 1.4.0, documented in `docs/architecture/firmware/TESTBOARD_7953_CAPTEST.md`).
- `captest/force_model.py` (residuals, charge proxy, bias, event step, d33_eff fit, leakage),
  `captest/force_calibration.py` (trial analysis, wizard, per-group records) and
  `captest/force_session.py` (controller) are widget-independent; `force_calibration_panel.py` is
  the Qt tab.
- `captest/force_estimates.py` and `force_estimation_panel.py` provide explicit recovery of a
  failed electrical route. They estimate median donor loaded-minus-blank capacitance plus
  the target's own measured blank, then refit the target transitions. Borrowing switching
  coefficients is a separate experimental option. Originals and donor provenance are kept.
- `captest/force_evidence.py` saves atomic trial sidecars and replays saved raw recordings
  with their marks, frozen bias, models and applied corrections. Reset and restart create
  unique files; rejected and cancelled captures remain available for review.

Electrical coverage and electrical quality are separate: a completed pass with
`HIGH_RESIDUAL` is not force-ready. Above-VD signals are recorded as provisional
extrapolation without a default 0.25 V ceiling. ADC headroom and below-fit coverage have
separate rejection reasons.

Use real known weights for gain fitting; choose **Workflow test only** for finger tests.
Enter calibration masses in grams (default `20, 50, 100, 200`; two weights are allowed).
The trial table also shows reference force in newtons. Mounting/contact text is optional
annotation; editing it preserves the session and each earlier trial's original notes.
Specify the maximum force error in newtons before a held-out validation can pass. Records
distinguish not fitted, fitted/provisional, validated and stale; only validated gains are
embedded in the force-input bundle. Force errors use the deployed endpoint accumulator
without subtracting a fitted event intercept.

The force plot has a dashed red zero reference. Force and raw residual plots share
one time axis and pan/zoom together, with independent vertical scales. Live plots
use the same last 30 seconds; completed trials and saved evidence show the full
selected interval in both plots. Residual peak downsampling preserves brief press
and release spikes when viewing a long recording.

**Relative polarity** provides fixed +1/-1 controls for B/L/C/R/T, saved per Array/PZT
group in the force-input file. Reverse an opposite channel before summing charge; unloading
remains signed (no rectification). Raw ADC/residual diagnostics keep their original signs.
Sign changes replay saved captures and invalidate drift/leakage corrections and force fits.
Remeasure drift and refit/validate d33_eff afterwards. Legacy files default to +1 for every
channel; the existing common output polarity is still applied once.

Before measuring drift or weights, use **Record press test** for the selected package.
After a brief noise sample, gently press and release the intended load-spreading
plate once. Both actions are detected automatically and recording stops shortly
after release: no stage buttons, minimum hold, or weight-trial baseline/drift wait.
The test does not change workflow-only mode or consume weight trials.
**Review press signs…** compares each channel's press/release charge separately,
ignoring the current sign map and the group sum. It proposes signs relative to the
majority press direction, with no assumption about C. Weak, invalid or conflicting
responses remain unclear and block Apply. Clipping/headroom is allowed for this
direction-only test and appears in Notes / reason, with the clipped sample count;
charge magnitudes from clipped signals are unreliable. Force calibration retains
its clipping checks. A steady group hold is not required for
this diagnostic. If no clear pair is detected, it stops after 20 seconds and you
can try again. Repeat at another contact position because mounting/bending can
change response directions. **Apply signs to
this package** explicitly saves the reviewed evidence with its recording hash in
the force-input file. Changing signs requires a new drift measurement and force fit.
Unknown finger force does not calibrate gain or establish physical electrode polarity.

**Evaluate leakage** proposes a correction; **Apply correction** replays original captures
and freezes a newly estimated unloaded bias. Apply, rollback, window edits and changed
acceptance limits invalidate the previous gain/validation. Refit and validate afterwards.

Before the weights, **Measure unloaded drift** records the same full scan with no added
load. It discards 2 seconds of settling, fits constant per-channel residual bias, and checks
it on a separate later window. A qualified correction is frozen and used before charge
integration in both live and offline calculations. It never adapts during a weight hold.
**Clear unloaded correction** returns to each trial's own baseline bias and replays earlier
trials. Changing the leakage predictor clears the separate unloaded correction. Raw
recordings and correction provenance are retained; this is not a measurement of Roff.

If Start trial is still checking a frozen linear drift correction, the prompt now
shows the current slope-change budget and mismatch from the saved drift. A fresh
unloaded measurement is needed if that correction no longer matches; waiting does
not automatically refit it. Capture gaps/CRC errors remain separate failures.
Live drift checks omit final timing statistics to reduce pauses in serial reading.
On drivers that support it, CapTest requests a 1 MiB receive buffer for the stream;
the driver may ignore that request. Final recordings still include full statistics.
Hardware force captures also use a dedicated serial receiver with a bounded 16 MiB
queue, so drift fitting, prefix hashing, replay and plot preparation do not pause
serial reception. Decoding and raw recording retain one consumer. Queue exhaustion
is an explicit capture failure; CRC, gap, clipping and other quality checks remain
in force. Simulated captures retain deterministic polling. This host change needs
no firmware update. A 500 us lag still needs a hardware retry to establish sustained
reliability; faster acquisition does not guarantee an unclipped press.
Without a prior unloaded correction, the initial baseline plot waits for its bias estimate
instead of showing uncorrected integration as force.

For a settling transient followed by linear drift, use **Drift test → first weight trial**
before collecting weights. It searches recent windows for a stable nonzero raw-charge slope,
fits and checks separate halves, then proceeds to the first weight prompt without stopping
acquisition. The correction is `dQ_corrected = dQ_raw - drift_rate * actual_visit_interval`.
It is frozen during the weight test. Subsequent Start trial runs recheck the same frozen
rate after settling before arming placement; they never refit it automatically. The selected unloaded duration is the search window;
settling may take longer, with a 120 s timeout (or window plus 2 s). Failed settling never
falls back to fitting the startup transient. Reset trials to start a new combined run.
The [7953 user guide](../docs/guides/TESTBOARD_7953_CAPTEST_USER_GUIDE.md#force-export-and-force-calibration-testboard_7953)
describes the workflow and its physical limits.

Tests: `tests/test_sensor_groups.py`, `test_force_model.py`, `test_force_export.py`,
`test_capture.py`, `test_force_calibration.py`, `test_force_session.py`, `test_gui_force.py`,
`test_force_hardening.py`, `test_force_zero.py`.

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
