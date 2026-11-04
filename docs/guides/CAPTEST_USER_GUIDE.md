# CapTest user guide (all boards)

CapTest measures, for every PZT sensor channel of a TestBoard, the total capacitance `C_total` at the channel node and the ratio `C_D / C_total` (`C_D` = capacitance of the shared mux-output node), from the charge-up curve after one sensor is connected to a node that was parked at VMID. `tau = R_bias x C_total`, so `C_total = tau / R_bias`; the step at the connection is the charge shared with the node, `C_D / C_total = (V0 - V_s0) / (V_D - V_s0)`.

One application serves every board with CapTest firmware. Board-specific guides: [TestBoard_7953](TESTBOARD_7953_CAPTEST_USER_GUIDE.md) · [TestBoard_ADC124](TESTBOARD_ADC124_CAPTEST_USER_GUIDE.md). Folder, registry and how to add a board: [`CapTest/README.md`](../../CapTest/README.md).

## Start and choose the board

```text
cd CapTest
python -m captest                    # GUI (also: uv run captest, or python captest)
python -m captest --simulate --gui   # emulated board, no hardware
python -m captest --list-boards
```

The **Board** box at the top lists the boards of `CapTest/boards.json`; a board whose files are incomplete is greyed out and its tooltip says why. Changing the board rebuilds the channel grids, the `R_bias` table, the settings (with that board's defaults), the result columns, the VMID targets and the flash buttons, and disconnects (it is refused while a measurement or an upload is running). Each board keeps its own saved settings in `CapTest/settings/<board id>.json`, and the window starts on the last used board.

**Detect** (and every **Connect**) asks the attached firmware who it is (`mcu*`, `capstatus*`) and matches the answer with the registry. If the firmware belongs to another board, the application offers to select that board (or, when you decline, says so and keeps measuring disabled). Raw files and settings files carry their board id: loading one that belongs to another board offers to switch to it.

## Which firmware is on the board?

The Teensy runs one firmware at a time: the board's **production** firmware, its **CapTest** firmware, or (ADC124) the PCB-diagnostic firmware. The line under the connection bar names it; the coloured banner says in words why measuring is or is not possible.

- "capacitance-test firmware 1.0.0 (git ..., boot 0x...)": ready.
- "production firmware is running": measuring is disabled; the application offers **Flash test firmware** right after connecting.

**Flash test firmware** / **Restore production firmware** ask for confirmation (naming the port and what is replaced), run `CapTest/scripts/flash_firmware.*` for the selected board, stream the output into the Log tab, wait for the Teensy to re-enumerate, reconnect and show the firmware that now runs. Disabled in simulation mode. Neither firmware stores anything on the board, so switching loses nothing. If the bootloader does not respond, press the Teensy program button once and try again. From a terminal: `CapTest/scripts/flash_firmware.ps1 -Board <id> -Firmware test|production [-Port COMx] [-DryRun] [-Yes]` (bash: `--board`, `--firmware`, `--port`, `--dry-run`, `--yes`).

## Automatic mode

Tab **Automatic**: tick the groups (7953: ADCs, ADC124: arrays; default all), set **Bursts per channel (M)** and press **Run automatic**. Every valid channel is measured without further interaction. The order is planned by the board (7953: one burst per channel; ADC124: for every array and address one burst reads up to four muxes at once; the unticked option "One burst per address" gives one burst per channel). With M > 1 the order is pass-major (pass 1 over everything, then pass 2, ...) and a sensor is never read again before the **discharge time** has passed (the run waits if needed).

Progress bar, elapsed and remaining time are shown. **Stop** ends the run after the current burst and leaves the board parked on VMID (`capabort`). A burst that fails (damaged reply, no usable data) is recorded in red and the run continues; **Re-measure failed** repeats only those channels.

## Manual mode

Tab **Manual**: tick channels in the grids (one per group; ADC124: mux rows x input columns), use **all / none / invert** per group or **Select all / none / Invert** globally, or type a list in the board's syntax (7953 `2:0,2:5,4:1-3`, ADC124 `1:1:0,1:4:6,2:2:0`, `*` and ranges accepted) and press **Apply**. **Measure selected** reads the ticked channels in the order the board plans them; right-click a channel (or a result row) for **Measure this channel now**; any order, any number of times.

The grid shows `*` after a channel that is still fresh since power-up (the firmware reports a boot id, so a reset makes every channel fresh again). The tooltip shows the time since the channel was last connected, **including connections that happened as a side effect of reading a neighbour** (ADC124: a burst connects input X of all four muxes of the array). Reading a channel less than the discharge time after its previous connection **warns but is not blocked**: the result carries `TOO_SOON` and `VS0_SUSPECT`.

## Results

The results grid stays in numeric channel order: **Array, MUX, Input** on ADC124,
or **ADC, Ch** on 7953, including during acquisition and re-analysis. The **Help**
tab has sections for the measurement model, every results column, quality flags
and the selected board's settings. Column-header tooltips also explain each value.

One row per channel: board columns (7953 ADC, Ch; ADC124 Array, MUX, Input), the fit model used, `tau`, `C_total` (+/- the `R_bias` tolerance in pF), `C_D/C_total`, `C_D`, `C_share/C_total` and `C_share` (7953 Vmid between samples only; they replace the `C_D` columns there), sensor-side capacitance, `C_PZT`, leakage, `V0`, `Vinf`, residual and amplitude in LSB, number of bursts, time since the previous connection, fresh flag and quality flags. M > 1 gives mean values (std in the exports). Selecting a row shows its last burst: the pre-connect baseline (green triangles, ADC124), the samples, the fit and the residuals, with the connection at t = 0. The **Bar chart** tab shows `C_total` and the ratio per channel, grouped (7953 by ADC, ADC124 by array and mux, since `C_D` belongs to the mux-output node); green ok, orange flagged, red failed.

Buttons: **Export CSV / JSON**, **Save raw bursts** (the exact device packets), **Load raw (re-analyse)**, **Save plots as images**; the file dialogs start in `CapTest/Results/<board id>/`. On TestBoard_7953 the default **Method** (Settings) is **Vmid between samples**. It needs CapTest firmware 1.3.0. The sensor is connected for one frame and the node returns to Vmid for one frame, repeated, and every second cycle the sensor stays connected longer. One burst then gives `C_total` and `C_share` without assuming the sensor start voltage. The *Fit* column reads `alt`. The sensor must still rest between connections: a burst that starts less than 0.3 V below Vmid is flagged `ALT_SMALL_RANGE` and left out of the mean. **One connection per burst** is the earlier exponential-fit method (`--method burst` on the command line). The **Fit model** box above the table applies to that method only. It switches between one and two exponentials on the stored samples without a new measurement; **Re-analyse with current settings** does the same for any other analysis setting (R_bias, VREF, V_s0, hold instant, ...).

**Discharge check**: connects the channel once, measures it again after growing waits and plots `V0` versus wait (waits per board: 7953 10/30/100/300 ms, ADC124 10/30/100/300/1000 ms). A plateau means the sensor returned to a fixed start voltage after that wait; "NO plateau" means `V0` still changes after the longest wait and the `V_s0 = 0` assumption cannot be met by waiting (see the ADC124 guide for what was measured).

## Settings

The Settings tab shows the settings of the selected board (descriptor `settings_fields`); hovering shows what each does. Common ones: samples per burst (auto: 6 tau of a guessed 100 pF), park time, SPI clock (board range), bursts per channel, discharge time, discard first K, VREF, sensor start `V_s0`, parasitic C, `R_bias` tolerance and table (editable per channel), fit model. 7953 only: frame gap, first-clock delay. ADC124 only: pre-connect frames, ADC hold instant, mux turn-on delay, frame-stamp latency, V_D baseline samples, one burst per address. **Save / Load settings** use a JSON file (`--settings`, `--save-settings` on the CLI).

## Command line

```text
python -m captest --list-boards
python -m captest --board <id> --port COM3 --detect | --status | --vmid <target>
python -m captest --board <id> --port COM3 --auto [--adc|--array 1,2] [--per-channel] [--bursts M] [--discharge-ms 300] --out results.json --csv results.csv --raw-out raw.json
python -m captest --board <id> --port COM3 --channels <list in the board's syntax> --bursts 3
python -m captest --board <id> --port COM3 --discharge-check <channel>
python -m captest --board <id> --simulate ...    # emulated board, virtual clock (instant discharge waits); --sim-realtime waits for real
python -m captest --reanalyze raw.json [--settings s.json] --out results.json
python -m captest --board <id> --port COM3 --auto --save-baseline    # no sensor connected: board baseline
python -m captest --board <id> --port COM3 --auto --subtract-baseline # adds c_board_pf and c_net_pf
python -m captest --make-baseline results_no_sensors.json            # baseline from an earlier results file
```

**Board baseline.** The Settings tab has `Subtract board baseline`: the C_total each input reads with no sensor connected (PCB routing, mux and op-amp path), saved in `CapTest/settings/<board id>_baseline.json` from a run with all sensors unplugged (`Save current results as board baseline...`). With it on, the table gets `Board baseline [pF]` and `C_net [pF]` (C_total minus the baseline of the same input), and the bar chart shows C_net. C_total itself is unchanged, and baseline and measurement must use the same C_total method. Only the TestBoard_7953 baseline has been measured so far.

Without `--board` the CLI uses the first board of `boards.json` (or the board of `--settings`). A bare output file name is written to `CapTest/Results/<board id>/`. Exit code 0 = all channels measured, 1 = some channel failed, 2 = the capacitance-test firmware of the selected board is not running.

## Simulation mode

Every board has an emulated device that speaks the real protocol (bytes in and out, same packets, CRC, `mcu*`, `capstatus*` and the production firmware's identity for the switching UI). Used by the GUI (**Simulation mode**), the CLI (`--simulate`) and the tests. The ADC124 emulator models eight mux-output nodes with their own `C_D` and `R_bias`, the shared-address connection of all four muxes, the leakage of every connected sensor, charge injection, ADC noise, frame jitter, pop latency and stalls.

## Files

| Where | What |
| --- | --- |
| `CapTest/Results/<board id>/` | default output (CSV, JSON, raw bursts); raw files are tagged with the board id |
| `CapTest/settings/<board id>.json`, `last_board.json` | saved settings per board, last used board |
| `CapTest/boards.json`, `CapTest/boards/<id>.json` | registry and board descriptors (committed) |
