# TestBoard_7953 capacitance test: user guide

Measures, for every PZT sensor channel of TestBoard_7953, the total capacitance `C_total` at the channel node and the ratio `C_D / C_total` (`C_D` = shared mux-output node), from the charge-up curve after one sensor is connected to the parked MXO node. Design, protocol and assumptions: [`TESTBOARD_7953_CAPTEST.md`](../architecture/firmware/TESTBOARD_7953_CAPTEST.md). Things you must check with instruments: [`TESTBOARD_7953_CAPTEST_VERIFICATION_CHECKLIST.md`](TESTBOARD_7953_CAPTEST_VERIFICATION_CHECKLIST.md).

## Install and start

Python 3.10+, `PyQt6`, `pyqtgraph`, `numpy`, `pyserial` (the project dependencies; `scipy` is optional and only polishes the fit).

```text
cd CapTest
python -m captest                      # GUI (also: uv run captest, or python captest, from this folder)
python -m captest --simulate --gui     # GUI on the emulated board, no hardware
python -m captest --simulate --auto    # CLI on the emulated board
```

## Which firmware is on the board?

The Teensy runs either the production firmware or the capacitance-test firmware. After **Connect** the title bar shows which one answered (`capstatus*`):

- "capacitance-test firmware 1.0.0 (git ..., boot 0x...)": ready.
- "production firmware is running": measuring is disabled; press **Flash test firmware**.

**Flash test firmware** / **Restore production firmware** ask for confirmation (naming the port and what is replaced), run `scripts/flash_test.*` or `scripts/flash_production.*`, stream their output into the Log tab, wait for the Teensy to re-enumerate, reconnect and show the new firmware. They are disabled in simulation mode. Neither firmware stores anything on the board, so switching loses nothing. If the bootloader does not respond, press the Teensy program button once and try again. The same scripts work from a terminal: `scripts\flash_test.ps1 -Port COM3 [-DryRun] [-Yes]`, `scripts/flash_production.sh --port /dev/ttyACM0 [--dry-run] [--yes]` (default: auto-detect, asks when more than one Teensy is found). To restore without the application:

```text
pio run -d Arduino_Sketches/TestBoard_7953 -e teensy41 -t upload
```

## Automatic mode

Tab **Automatic**: tick the ADCs (default all four: ADC1/ADC3 channels 0..9, ADC2/ADC4 channels 0..14, 50 channels), set **Bursts per channel (M)** and press **Run automatic**. The channels are measured in the order ADC1 0..9, ADC2 0..14, ADC3, ADC4. With M > 1 the order is pass-major (pass 1 over all channels, then pass 2, ...), and a channel is never measured again before the **discharge time** has passed (the run waits if needed). The first burst after power-up has the cleanest step, so M = 1 is the default and the first run after a power cycle is the most trustworthy.

Progress bar, elapsed and remaining time are shown. **Stop** ends the run after the current burst and leaves every converter parked on Vmid (`capabort`). A channel that fails (damaged reply, no usable burst) is recorded in red and the run continues; **Re-measure failed** repeats only those channels. **VMID read** shows the channel-15 reading of the chosen ADC (about 1.25 V, code about 2048, tag 15).

## Manual mode

Tab **Manual**: tick channels in the per-ADC grids, use **all / none / invert** per ADC or **Select all / none / Invert** globally, or type a list such as `2:0,2:5,4:1-3` (also `1:*`) and press **Apply**. **Measure selected** measures the ticked channels in the order of the ADC/channel numbers. Right-click a channel (or a table row) for **Measure this channel now**; any order, any number of times.

The channel grid shows `*` after a channel that is still fresh since power-up (the board reports a boot id; a reset or power cycle makes every channel fresh again). The tooltip shows the time since the channel was last connected. Measuring a channel less than the discharge time after its previous connection **warns (orange text and Log) but is not blocked**: the sensor may not be discharged, so the assumption `V_s0 = 0` is suspect and the result carries the flags `TOO_SOON` and `VS0_SUSPECT`.

## Results

The table shows ADC, channel, the fit model used, `tau` (the slow one for two exponentials), `tau fast`, the fast part of the step in percent, `C_total` (with the `R_bias` tolerance in pF) and `C_total vs 1 exp [%]` (what the two-exponential fit changes), `V0`, the estimated start voltage `Vs0 est`, and also: `tau`, `C_total` (with the `R_bias` tolerance in pF), `C_D/C_total`, `C_D`, sensor-side capacitance (`C_total - C_D`), `C_PZT` (sensor side minus the parasitic setting), the implied leakage current, `V0`, `Vinf`, residual rms and amplitude in LSB, the number of bursts used, the time since the previous connection, the fresh flag and the quality flags. M > 1 gives mean values (std in the exports). Selecting a row shows its last burst: samples, fit and residuals. The **Bar chart** tab shows `C_total` (with std) and the ratio per channel (green ok, orange flagged, red failed).

Buttons: **Export CSV / JSON**, **Save raw bursts** (the exact device packets, for later re-analysis), **Load raw (re-analyse)** (uses the current settings, e.g. another `R_bias`), **Save plots as images**. CLI: `python -m captest --reanalyze raw.json --out results.json`.

**Fit model.** Real PZT sensors charge with a fast and a slow component; a single exponential leaves 7-10 LSB of structured residual and `HIGH_RESIDUAL` on every channel, two exponentials reach the noise floor. The Settings tab chooses which model drives `tau`, `C_total`, `V0` and the ratio; the plot always shows the chosen fit (red) and the other model (dashed grey), and the table shows how much `C_total` changes (`C_total vs 1 exp`). With the two-exponential model `tau` is the slow time constant; `C_total = tau / R_bias` stays a formal conversion because the measured time constants do not scale with `R_bias` (see the design document before trusting absolute pF values).

**Switching the model afterwards.** The Results tab has a **Fit model** box above the table. Changing it (or the same box in the Settings tab) re-analyses every stored burst immediately; no measurement is repeated and nothing is sent to the board. The table then shows the chosen model in `Fit`, and always both models side by side (`C_total 1 exp`, `C_total 2 exp`, `ratio 1 exp`, `ratio 2 exp`), and a line above the table compares the medians (C_total, residual). **Re-analyse with current settings** does the same for any other analysis setting you changed on the stored samples (R_bias values, VREF, V_s0, discard K, parasitic C, first-clock delay). Saved raw files (**Load raw**) can be switched the same way. The box is locked while a measurement is running.

**Sensor start voltage.** `Vs0 est` is estimated by comparing the channels of one ADC with each other (needs at least 5 measured channels of the ADC; the estimate appears and updates as more channels are measured). `VS0_HIGH` (above 50 mV) means that sensor started charged, so its `V0`, `C_D/C_total` and `C_D` are unreliable (tau is not affected). Measure it again after a second or more: if the value falls to its neighbours' it was a stray charge.

**Discharge check**: connects the channel once, then measures it again after waits of 10, 30, 100 and 300 ms. `V0` versus wait should decrease and then plateau; the plot shows it and the Log gives the shortest wait that already agrees with the final value (within 5 %). Use it to choose the discharge time.

## Settings tab

| Setting | Default | Meaning |
| --- | --- | --- |
| Samples per burst N | auto | auto = 6 tau of a guessed 100 pF behind the channel's `R_bias`, at least 32, at most 1024; explicit values up to 2048 |
| Park time | 200 us | channel 15 before the connection so the shared node is at Vmid |
| Frame gap | 200 ns | CS-high time; 200 ns is the minimum that keeps the frame period above 1.0 us and the acquisition above 325 ns at 20 MHz |
| First-clock delay | 25 ns | assumed delay CS fall to first SCLK edge; shifts the connection instant |
| SPI clock | 20 MHz | maximum allowed by this firmware |
| Bursts per channel M | 1 | results are averaged |
| Discharge time | 300 ms | wait before a channel is connected again; see below |
| Discard first K | 2 | samples dropped at the start of every burst |
| Fit model | Two exponentials | One exponential, or two (falls back to one when the second is not needed, e.g. empty channels). Also `--fit 1exp\|2exp` on the CLI and `fit_model` in the settings JSON. |
| VREF | 2.5 V | code to volts |
| Sensor start V_s0 | 0 V | assumed sensor voltage before the connection |
| C parasitic | 5 pF | assumption: trace + mux pin, only used to derive `C_PZT` |
| R_bias tolerance | 1 % | shown next to `C_total` |
| R_bias table | JSON of the benchmarks folder | per ADC:channel, editable; ADC1 1 M, ADC2 470 k, ADC3 470 k, ADC4 249 k |

**Save / Load settings** use a JSON file (`--settings`, `--save-settings` on the CLI).

The discharge time default is 300 ms, not the 100 ms of the original plan: on the real board the settled `V0` of a channel was still about 6 % high after 100 ms and plateaued only after 300 ms (see [`RESULTS.md`](../testing/results/testboard-7953/capacitance-test/RESULTS.md)).

## Caveats (also tooltips in the application)

- `R_bias` tolerance maps 1:1 to `C_total`.
- Charge injection when the Vmid channel switches off makes `V_D` slightly lower than Vmid (not in the datasheet): a few percent effect on the ratio, none on `tau`.
- `V_s0 = 0` is only true for a sensor that is uncharged (first burst after power-up) or has discharged.
- An **open channel** (no sensor attached) measures only the pin, trace and shared-node capacitance; `tau / R_bias` of such a channel is an upper bound for `C_D`.
- `C_PZT` depends on the parasitic assumption.

## Command line

```text
python -m captest --port COM3 --status
python -m captest --port COM3 --vmid 1
python -m captest --port COM3 --auto [--adc 1,2] [--bursts M] [--discharge-ms 300] --out results.json --csv results.csv --raw-out raw.json
python -m captest --port COM3 --channels 2:0,2:5,4:1-3 --bursts 3
python -m captest --port COM3 --discharge-check 2:0
python -m captest --simulate ...        # emulated board, virtual clock (instant discharge waits); --sim-realtime waits for real
python -m captest --reanalyze raw.json [--settings s.json] --out results.json
```

Exit code 0 = all channels measured, 1 = some channel failed, 2 = the capacitance-test firmware is not running.

## Simulation mode

The emulated device speaks the real protocol (bytes in and out, same packets, CRC) and models each sensor as a capacitance that leaks away between connections, a shared node `C_D`, `R_bias` with tolerance, Vmid, ADC noise (1 LSB rms), quantisation, frame jitter, a first-clock delay different from the assumption, and an optional charge-injection pedestal and frame-periodic pattern. Used by the GUI (check **Simulation mode**), the CLI (`--simulate`) and the tests.

## Tests

```text
cd CapTest
python -m pytest tests                 # QT_QPA_PLATFORM=offscreen is set by the GUI test
python tools/validate_qspice.py        # optional, needs QSPICE64.exe
pio run -d Arduino_Sketches/TestBoard_7953_CapTest
```
