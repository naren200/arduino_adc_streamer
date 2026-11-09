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

**Sensor start voltage.** `Vs0 est` is estimated by comparing the channels of one ADC with each other (needs at least 5 measured channels of the ADC; the estimate appears and updates as more channels are measured). `VS0_HIGH` (more than 50 mV from zero, either sign) means that sensor started charged, so its `V0`, `C_D/C_total` and `C_D` are unreliable (tau is not affected). Measure it again after a second or more: if the value falls to its neighbours' it was a stray charge. With the method **one connection per burst**, `V0`, `C_D/C_total`, `C_D`, `C sensor` and `C_PZT` are left empty, the start-voltage check is not run, and the row carries `FRAME_DEPENDENT`: `tau / R_bias` (column `C_total from` = `tau_over_r`) reads low there and is only for comparing channels. `UNSTABLE_TAU` marks a channel whose `tau` differs by more than 5 % between its repeated bursts. With the default Vmid between samples the table shows `C_share/C_total` and `C_share [pF]` instead of the `C_D` columns: `C_share` is an effective connection capacitance (about 6.5-6.8 pF at `V_D` on the four channels checked), not the node capacitance, so do not subtract it from `C_total`. `VMID_RESET_FAILED` means the node did not return to `V_D` after a cycle; `MIXED_METHODS` means the channel has bursts from more than one method, and only one is averaged.

**Vinf mode** (Manual tab, `Measure Vinf of selected`; CLI `--vinf --channels 2:5,2:7`). Select channels and press the button. Per channel and repeat it takes three bursts: a Vmid-between-samples burst (the *calculated* `Vinf`), a long burst that alternates the sensor with Vmid for long enough to settle (the *alternating* `Vinf`: the median of the sensor samples in the quiet last quarter, like the baseline production uses), and a burst that holds the sensor connected (the *held* `Vinf`). The Vinf tab shows the three in millivolts from Vmid (the zero line), with the spread over repeats (Bursts per channel) and a table; `Save Vinf CSV` writes it. Nothing is added to the results table. The three come from different frame sequences, so differences are normal. `VINF_ALT_NOT_SETTLED` or `VINF_NOT_SETTLED` means the sensor was still moving at the end of the alternating or held burst; `VMID_RESET_FAILED` means Vmid readings in the tail were not at Vmid. The ln(d) tab next to the bar chart shows the Vmid-between-samples fit as ln of the distance below Vmid against connected time.

**Board baseline** (Settings tab, `Subtract board baseline`). The PCB adds 13-19 pF to every reading (net routing at about 0.12 pF per mm, plus the path from the mux output to the op-amp, which is longest on ADC3 and runs through the back plane; ADC3 reads about 4 pF above ADC1 and channels 10-14 of ADC2 and ADC4 about 4 pF above their channels 0-9). To measure it, unplug all strips, run **Automatic mode** on all four ADCs (3 bursts per channel is plenty) and press `Save current results as board baseline...` in the Settings tab (or run `python -m captest --port COM3 --auto --bursts 3 --save-baseline`). The file is `CapTest/settings/TestBoard_7953_baseline.json` (it is not committed: `settings/` is ignored by git). An earlier results file of such a run can be turned into a baseline without measuring again: `python -m captest --make-baseline CapTest/Results/TestBoard_7953/<run>/no_strips.json`. With `Subtract board baseline` on (`--subtract-baseline` on the CLI), every row also shows `Board baseline [pF]` and `C_net [pF]` = `C_total` - baseline of the same channel, and the bar chart shows `C_net`. `C_total` is never changed, and the switch applies to results already on screen. `C_net` is empty (hover the cell) when the baseline has no entry for the channel or was taken by another `C_total` method. Take the baseline with the same method and frame settings as the measurements; the Settings tab lists the settings that differ. The subtraction assumes board and sensor capacitance add: on the 04 Nov 2026 strip swap it removed the ADC3-ADC1 difference on small sensors (+4.4 pF to +0.2 pF), but left about 4 pF on the 200-260 pF sensors and about 1 pF between ADC4 and ADC2.

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

## Force export and Force Calibration (TestBoard_7953)

Two features prepare the force calculation of the capacitance-to-force guide. They need the
firmware 1.4.0 (continuous capture); older firmware keeps every burst workflow and the tab reports
that capture is unavailable. Nothing flashes automatically.

**Export Force Calculation Data** (Automatic tab, Data group) and the **Force Calibration** tab,
step 1: choose *Array* and *PZT* (a package is five sensors B, L, C, R, T on one ADC; routes come
from `sensors_library/sensor_configurations.json`, matched by board profile, not by its
`selected_name`). *Measure this package* runs the alternate method on its five routes. *Add results
to file* writes (or updates) a `captest-force-inputs-1` JSON file in the board's results folder: per
route the loaded C_total, tau, VD, c0, c1, qv, the fitted distance range, per-burst values and the
raw packets (embedded, hash-checked). The file may be partial: reopening it and adding other
packages updates it; re-measuring a package adds a new revision and keeps the old one. A merge is
refused when the board instance, layout hash or acquisition settings differ. Failed channels are
marked (never zero). Coverage and electrical quality are independent: a completed pass with
`HIGH_RESIDUAL` or invalid coefficients does not qualify for force fitting. Legacy files are
checked too; their originals are not rewritten when loaded.

**Recovering a failed electrical route.** Prefer re-measuring it. When that is impractical,
choose *Use estimated electrical parameters*. Select healthy measured donors and explicitly
declare the matching sensor size/type, strip/flex construction and strips-off disconnection
boundary. The library does not automatically establish those equivalences. Every selected
donor and the target need compatible measured board blanks. The estimate is:

```text
C_added_donor = C_total_loaded_donor - C_board_blank_donor
C_total_est_target = median(C_added_donor) + C_board_blank_target
```

This includes the sensor and attached strip/flex. It is not an independent measurement of
Cs or floating capacitance. Do not add shared-node or strip capacitance again. The normal
path fixes that estimated C_total and refits c0, c1 and qv to the target's own raw transitions;
if the fit remains poor, it cannot be used. A separately selected experimental option
borrows switching coefficients from healthy donors on the same ADC. It still uses the
target ADC's measured VD and requires independent package-force validation. Review the
preview, donor spread and confidence before applying. Fewer than three donors is labelled
low confidence. Applying creates a new revision, retains the original fit and donor hashes,
and invalidates the affected package sensitivity. Changing a donor makes the estimate stale.

Force Calibration, steps 2-3 (a guided capture of the selected package; the board always scans
**all 50 routes of both arrays**, so the visit rate stays near production):

1. Enter the initial effective d33_eff (signed, nonzero; it only scales the live display), the
   *added lag per sweep* (each lag is a separate calibration), **Calibration masses [g]**,
   repeats per mass (default 5) and hold
   time (10-30 s). Keep a load-spreading plate in place; all load must pass through the package.
   Masses default to `20, 50, 100, 200`; enter other positive distinct values, including just
   two weights such as `12.5, 25`. Commas, semicolons or spaces separate the values. Trials
   run from smallest to largest, with the chosen repeats for each mass. Changing the mass
   list selects a separate trial sequence. The table shows **Force [N]**, the expected total
   group force: `mass [g] / 1000 * g` (default gravity 9.80665 m/s²), rather than a measured force.
   **Relative polarity** supplies a fixed sign for each B/L/C/R/T channel. If the centre
   response is opposite, set C to **−1** and confirmed matching channels to **+1**, separately
   for the selected Array/PZT group. For that pattern the sum is proportional to
   `Q_B + Q_L - Q_C + Q_R + Q_T`, with the common output polarity applied once.
   The sign is applied before summation for both loading and removal; absolute values are
   never used. Raw ADC voltages and the lower raw-residual plot retain their electrical signs.
   Overall force direction also depends on the signed d33_eff. A fixed relative sign needs
   validation with unclipped loading/removal in the intended mounting and contact positions.
   Sign selections are saved in the force-input file. Changing them replays earlier captures,
   clears unloaded drift and inferred leakage corrections, and invalidates gain/validation;
   existing raw samples remain intact. Reset trials, run the drift test again, then refit
   and validate d33_eff. The correction does not repair ADC clipping or missing event windows.
   The tab shows the estimated sampling rate per channel for the chosen lag (the default is
   1500 us; 0 us is about 3.5 MB/s and overflowed the board's buffer on the first hardware test)
   and, during and after a trial, the measured rate of each channel (B, L, C, R, T). The measured
   rates are stored in every trial and summarised as `sampling_per_channel` in the calibration record.
   Hardware captures have a dedicated serial receiver and a bounded 16 MiB queue, allowing
   reception to continue during drift fitting, evidence hashing and plot preparation. No
   firmware update is needed for this host change. A lower lag such as 500 us may reduce
   voltage excursions, but must be checked on the board: record unloaded drift through
   the weight prompt, then press/release and confirm no CRC/gap/overrun failures, no
   clipped samples and the expected measured sampling rate. Repeat several trials and
   cancel/restart once to check cleanup. Remeasure drift at the new lag; do not reuse a
   correction measured at 1500 us. Queue exhaustion remains a capture failure, and
   missing samples are never interpolated for force integration.
   While this tab is selected the right-hand area of the window shows the force workspace: the prompt,
   the action buttons, the sampling rate, the force and residual plots and the step table.
   After a rejected trial the prompt says why. *Retry failed* clears the plots and makes the step pending
   again; *Reset trials* discards all trials of this calibration and starts from the first mass (capture
   files stay on disk). The selectors and competing measurements are locked while a trial runs.
   Optional mounting notes describe the fixture, support or plate; optional contact-position
   notes describe where the weight touches it. They are logged and saved with the trial,
   and editing them does not reset the calibration or select another calibration identity.
   Existing trial annotations remain unchanged when replayed. Validation is limited to the tested setup and
   positions. A finger press is useful for **Workflow test only**, which preserves evidence but
   cannot assign the nominal mass to unknown force or fit a sensitivity. Enter a maximum
   **Max. validation error** in newtons for known-mass validation; leaving it unspecified keeps the result provisional.
   This is the acceptable absolute error against the known reference force, not a maximum
   applied force. **Hold drift / step limit** is the allowed accumulated change between stable
   loaded windows divided by the placement step: `0.150` means 15%. It checks drift without
   correcting or zeroing it. **Optional excursion guard** is an experimental voltage cutoff
   above VD; normally leave it Off. ADC headroom checks remain active. Each field and its
   label have mouse-hover help.
   Before the drift/weights sequence, check relative polarity for this package:
   click **Record press test**. After a brief noise sample, gently press and release
   the intended plate loading all five sensors. The app detects both responses and
   stops automatically shortly after release. There are no stage buttons, timed
   hold or weight-trial baseline/drift waits. The weight plan and workflow-only
   selection stay unchanged; you can repeat without resetting trials.
   After capture, **Review press signs…** shows each channel's signed press/release
   charge and proposed sign, independently of the current signs and group sum.
   It does not require a steady group hold. Clipping/headroom is allowed for this
   direction-only check and recorded in Notes / reason, including the saturated
   sample count. Its charge values are diagnostic and unreliable when clipped.
   Weak, invalid or inconsistent responses remain unclear and cannot be applied.
   Known-weight force calibration retains its clipping checks. With no detected pair, the listener stops
   after 20 s and you can try again. **Apply signs to this package** saves the reviewed evidence and signs only
   for this Array/PZT; changing signs clears old drift corrections and invalidates
   the force fit. No centre reversal is assumed. Repeat at another intended contact
   position to check mounting dependence. A finger test supplies no gain reference.
2. For a curved startup followed by approximately linear drift, use
   **Drift test → first weight trial**, before collecting any weight trials. Keep the same
   wiring/plate, with no added mass, hands off. The app searches recent raw-charge windows
   after at least 2 s, fits a straight-line slope on the first half and checks it on the later
   half. It also compares earlier/later training slopes to reject startup curvature. The
   summed absolute changes in channel slopes must meet the charge-drift budget. This budget
   comes from Max. validation error, or the hold-drift fraction of the smallest planned mass
   force, divided by hold duration and scaled by the initial d33_eff. It is a planning check;
   independent known-weight force validation is still required.
   The unloaded-duration field selects the fit/check window (default 10 s), rather than a
   promise to finish that soon. It waits for a suitable section, up to 120 s or window + 2 s.
   A failed search rejects the test and keeps prior correction/evidence; it does not fit the
   early transient as a fallback. On success, the same acquisition continues into the first
   trial baseline and placement prompt. Do not add weight until the prompt is armed.
   Per-channel drift rate is frozen and subtracted as
   `dQ_corrected = dQ_raw - drift_rate * actual_intervisit_time` in both plots and analysis.
   A held weight should produce a flat shifted level; loading/removal steps are preserved.
   The shared recording retains both stages, a hash of the training prefix, and its final
   full-file hash. Further trials retain the frozen correction: Start trial first waits for
   settling and verifies that the quiet slope still matches it, without changing its value.
   A mismatch blocks the weight prompt rather than silently recalibrating drift.
   While waiting, the live prompt shows the latest slope-change bound, allowed
   budget, saved-drift mismatch and rejection reason. Capture CRC/gap failures
   stop the recording separately. For a fresh correction after the rest behaviour
   changes, reset trials and run **Drift test → first weight trial** again.
   Reset trials begins a new
   combined drift-to-first-weight run.
   The separate **Measure unloaded drift** operation remains available (default 10 s, adjustable
   6-120 s). Keep the same wiring, support and load-spreading plate, with no added mass and
   hands off. The first 2 s are discarded for settling. A per-channel constant residual bias
   is fitted on the first half of the remaining samples and checked on the later half using
   block-mean uncertainty. An unstable residual, capture fault, ADC headroom or below-fit
   voltage rejects this measurement and retains any previous correction. Qualification here
   checks unloaded stability; it does not establish known-force accuracy or physical Roff.
   A qualified correction is applied as `dQ = polarity * Ctotal * (e - bias_unloaded)` to both
   the live plot and trial analysis. It is frozen throughout placements, holds and removals;
   the plot reports held-out drift before/after correction using the initial display sensitivity.
   Earlier weight trials are replayed and their gain/validation invalidated if the correction
   changes. **Clear unloaded correction** replays trials using their own baseline bias.
   Changes to package/scan/model require a matching measurement; applying or rolling back a
   leakage predictor clears this separate correction. Original raw/NPZ samples and sidecars
   are retained and can be reviewed offline.
3. *Start trial*: the unloaded baseline is captured. With a separate unloaded correction,
   that frozen bias is used; this trial's baseline only checks for a shift (`UNLOADED_DRIFT_SHIFT`
   keeps validation provisional). Otherwise the per-route bias is fitted on the trial baseline
   and then frozen. Until a bias is available, the force trace waits rather than displaying raw
   baseline drift as force; raw residuals remain visible. The prompt to place the mass appears only after the event is armed; press *Mass placed
   now*, take your hand away, press *Hand clear*; hold; then *Mass removed now* and *Hand clear*.
   Windows are placed outside the transient (0.5 s windows, 0.3 s guard). A trial is rejected for
   a capture problem, ADC headroom (distinct from actual code saturation), a voltage that fell below the range the electrical fit
   covers, or no stable window. A voltage above VD is normal for a loaded PZT and is not rejected (it
   is flagged as provisional extrapolation with its largest excursion and duration). There is
   no default VD+0.25 V limit; the optional excursion guard is off by default. Passing headroom
   alone does not validate the electrical model above VD. Rejected trials keep their reasons;
   clipping or a range failure blocks heavier masses. Retry the failed mass; a successful retry
   restores automatically skipped steps, while user-skipped masses stay skipped.
   Voltage checks cover the force-analysis interval, including the preceding sample needed
   for its first prediction. Discarded startup/pretest samples do not reject the weight phase.
   Unloaded measurements likewise check their selected fit/check windows. Full raw recordings
   and capture voltage extrema remain preserved. A below-fit voltage can be a valid ADC value:
   the rejection concerns electrical-model coverage, not an invalid ADC code or integrated force.
   Saved decisions retain their original range-check policy for reproducible offline replay.
4. *Fit and validate*: a common signed slope over placements and removals (each event is a step
   plus a drift ramp), the intercept and flags, and a held-out repeat. *Evaluate leakage* fits an
   effective correction only when the data supports it and otherwise reports "not identified";
   nothing is applied automatically. Intact rejected hold/model trials may also be evaluated.
   *Apply correction* replays preserved captures, re-estimates the unloaded bias once, freezes it,
   and invalidates the previous gain and validation. *Roll back correction* does the same using
   the original model. Live and offline reconstruction use the applied model. A hold slope alone
   does not identify a physical Roff; the effective correction remains schedule-specific.
   *Review last trial windows* allows explicit baseline/action/stable-window edits followed by
   replay and refitting. It does not overwrite the recording or previous analysis.
   *Save calibration* writes one `captest-force-calibration-1`
   record per package and schedule (never overwriting another package, the other
   array's package or another sampling rate); earlier saved revisions are archived. Saving
   before a fit or successful validation is allowed and preserves the corresponding status.

The status is **not fitted**, **fitted provisional**, **validated** or **stale**. A slope alone
does not validate a calibration. A held-out repeat must meet an explicit error limit, with
acceptable hold drift, load/removal agreement, estimator agreement, intercept and reference
stability. Reported force errors use plain endpoint charge divided by the frozen training gain;
they do not subtract the fitted event intercept. Drift-corrected fit steps and regression
errors are retained separately. Estimated electrical provenance remains visible even when
effective package force passes validation. Only validated sensitivities enter the force bundle.
Failed/provisional validation records keep a tested range; they do not export a validated range.

Every attempt has a unique `.cap` recording, `.npz` samples and an atomic `.trial.json` sidecar
with hashes, button marks, settings/models, proposed/applied correction state and analysis history.
Cancelled and rejected attempts survive Reset and restart. *Open saved trial evidence* replays
the saved marks/model and decision offline without changing the active calibration. Its plot
uses the saved initial display sensitivity; it does not imply a validated force scale.

The fitted value is an **effective system sensitivity** for the capacitance-proxy conversion
(`capacitance_proxy_v1`), not a material d33 and not proof of calibrated charge. A software test
does not establish physical force calibration: the hardware acceptance steps are in the plan
(`docs/plans/features/plan-CapTest-Force_calibration.md`).

The force plot includes a dashed red line at zero. Its horizontal time axis is
linked to the raw residual plot: zooming or panning either plot moves both.
They use the same last 30 s during capture and the full selected trial interval
after completion or offline review. Vertical scales remain independent (N and
mV). Brief residual spikes are preserved when displaying long recordings;
residuals retain their original electrical signs before bias/polarity correction.

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
python -m captest --port COM3 --auto --bursts 3 --save-baseline        # all strips unplugged: board baseline
python -m captest --make-baseline CapTest/Results/TestBoard_7953/<run>/no_strips.json
python -m captest --port COM3 --auto --subtract-baseline --out results.json   # adds c_board_pf, c_net_pf
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
