# Plan: CapTest force-data export and Force Calibration

Status: software hardening implemented; hardware acceptance pending.
Created: 5 November 2026.
Updated: 6 November 2026.

## Objective

Add two features to the **CapTest application**:

1. A versioned export of the measured electrical parameters and raw evidence
   needed for force reconstruction. The user selects the Array and PZT package
   to test; a partial file can be saved and later updated with further packages
   until it covers the whole board/layout.
2. A **Force Calibration** tab that uses those measurements and an initial
   `d33_eff`, guides placement/holding/removal of 20 g, 50 g, 100 g and 200 g
   masses on a selected five-channel PZT package, fits its effective sensitivity
   and supported leakage correction, and saves a separate calibration per group.

Initial hardware scope is TestBoard_7953. Keep the CapTest core board-neutral;
other drivers expose this feature only when they implement the required
acquisition and reconstruction capabilities. Loading/reviewing saved files is
available offline. ADC_Gui consumption of these files is a subsequent feature;
define its interchange contract here without changing ADC_Gui in this slice.

The physical model and limitations come from the
[capacitance-to-force guide](../../guides/TESTBOARD_7953_CAPACITANCE_TO_FORCE_GUIDE.md)
and [force-path review](../../reports/TESTBOARD_7953_FORCE_CALCULATION_REVIEW.md).
The [6 November implementation audit](../../reports/CAPTEST_FORCE_CALIBRATION_AUDIT_2026-11-06.md)
supplies concrete regression cases. This plan defines the required behavior;
the audit describes its reviewed snapshot, not a permanent statement that each
finding remains unfixed. Verify the current implementation against these
requirements before marking a slice complete.

### Implementation update — 6 November 2026

The software slice now includes separate electrical quality/readiness, explicit
donor-minus-own-blank median recovery with target constrained refitting (or
separately selected experimental same-ADC coefficient borrowing), revisioned
estimated provenance, unique recordings and atomic replayable trial sidecars.
Above-VD excursions remain provisional without the old default ceiling.
Range-failure retry restores only automatically skipped steps after recovery.
Leakage evaluation, application and rollback are distinct; application/window
edits replay the original captures and invalidate gain/validation. Force-error
reporting uses the actual endpoint accumulator without event-intercept removal.
Known-mass held-out validation requires a numeric limit, with honest provisional,
validated and stale statuses. Finger tests have a separate workflow-only mode.

Regression checks live in `CapTest/tests/test_force_hardening.py` and the existing
force/capture/Qt suites. The 7953 CapTest PlatformIO build succeeds. This records
software implementation, not completion of physical acceptance: known weights,
multiple positions/lags, both arrays and an intentionally exercised estimated
route still need the hardware checks below. ADC_Gui consumption remains outside
this feature slice.
See the [implementation and verification report](../../reports/CAPTEST_FORCE_CALIBRATION_IMPLEMENTATION_2026-11-06.md)
for the resulting behavior and validation scope.

## Repository findings and design decisions

- `CapTest/captest/gui.py` owns the standalone Qt app. Its `MainWindow` and Qt
  workers use widget-independent `MeasurementSession` logic in `runner.py`.
  This feature belongs there, not in `adc_gui.py` or its GUI mixins.
- Existing result CSV/JSON uses `ChannelResult.row()`, which is not a complete
  force record. Full coefficients and evidence must be explicitly exported.
  Alternate-fit `connect_info` contains `q_cycle_pc`, `c_share_1v_pf`, and timing
  diagnostics; `Measurement` also retains VD, fit results and its raw source.
- The original capacitance path measures bounded bursts for **one sensor
  channel** on the 7953 (2048 samples, at most 20 ms interrupt-masked). The new
  implementation includes a continuous scan path; verify and harden that path
  rather than rebuilding it or returning to sequential bursts for force capture.
- A user-entered/fitted `d33_eff` is an **effective system sensitivity** in the
  initial capacitance-proxy mode. It must not be advertised as an independent
  material d33 or proof of a calibrated electrical charge measurement.
- VD is the measured Vmid reference per ADC, not the quiet median of a sensor
  channel. Retain quiet baselines separately. Predict using absolute voltage
  before display baseline subtraction, filtering or thresholding.
- Above-VD voltage is an expected possible piezo response. ADC headroom,
  electrical-model validity and force-calibration validity are separate checks.
  Passing one check does not imply passing the others.
- An optional estimated-parameter fallback may enable provisional experiments
  on a package with a failed electrical measurement. It never silently changes
  measured evidence or automatically makes the package validated.

## 1. Resolve PZT groups from the sensor library

Load the board-matching entry from
[`sensors_library/sensor_configurations.json`](../../../sensors_library/sensor_configurations.json).
Do not use the file's global `selected_name`: it currently names another board
layout. Resolve/validate the explicit `board_profile` and selected layout.
Permit an explicitly selected compatible library file, and freeze its content
and hash for the session. Do not silently change mappings during calibration.

The bundled **TestBoard_7953** entry supplies:

- `arrays[array_id].adc_lanes`: physical ADCs for pair-local mux positions 1/2.
- `mux_mapping[PZTn].mux` and `.channels`: package connection and five inputs.
- `channel_sensor_map`: position order **B, L, C, R, T**.
- Package placement, polarity and channels-per-sensor metadata.

Confirmed mappings (input lists follow B/L/C/R/T):

| Package | Array 1 | Array 2 |
| --- | --- | --- |
| PZT6 | ADC1: 0,1,2,3,4 | ADC3: 0,1,2,3,4 |
| PZT7 | ADC1: 5,6,7,8,9 | ADC3: 5,6,7,8,9 |
| PZT1 | ADC2: 0,1,2,3,4 | ADC4: 0,1,2,3,4 |
| PZT3 | ADC2: 5,6,7,8,9 | ADC4: 5,6,7,8,9 |
| PZT5 | ADC2: 10,11,12,13,14 | ADC4: 10,11,12,13,14 |

There are **10 packages / 50 sensor channels**. Input 15 is reserved for Vmid.
The tab offers Array and PZT group selectors and displays the five resolved
physical routes. Save identities as `A1_PZT1`, `A2_PZT1`, etc.; bare `PZT1` is
ambiguous. Reject missing/duplicate positions, overlapping mappings, reserved
inputs, unrouted channels and board mismatches. Inputs 10-14 on ADC1/ADC3 are
unused by design and are not errors. Derive mappings from JSON at runtime; the
table above is documentation, not a second hard-coded route map.

## 2. Export complete force-calculation inputs

Add **Export Force Calculation Data** alongside existing exports. The user
selects the Array and PZT package(s) to measure; the electrical pass covers the
five routes of each selected package. The file may be saved as soon as one
package pass completes. Its coverage is explicit: per package `complete`,
`failed`, `missing` (not yet measured), plus board-level coverage
(e.g. 1 of 10 packages). Show measured, missing, failed and usable counts.

A saved partial file can be reopened and **updated**: measuring further
packages adds their records; re-measuring a package adds a new revision that
supersedes the earlier one (the earlier one is retained, not overwritten).
Updates require the same board instance, layout/hash and compatible
acquisition settings; otherwise refuse the merge and explain why. Write
atomically and update the manifest hash. An interrupted package pass is not
recorded as complete; its raw evidence may be kept as diagnostic data.

A package pass with failed channels may export its evidence, but the file
must explicitly mark unusable channels/groups and readiness. Never replace
failed/missing values with zero or mark an invalid group calibrated. Only
packages with complete, valid electrical inputs can enter the normal calibration
path; full-board coverage is not required. The explicit estimated-parameter path
below may permit provisional capture/fitting with different readiness labels.
Existing diagnostic/raw exports remain available.

Use a versioned JSON manifest (existing format `captest-force-inputs-1`, with
an explicit schema revision/migration for the new quality and estimate fields), with
raw evidence embedded or in a portable companion file referenced by a relative
path and content hash. An optional flattened CSV is for inspection; JSON plus
evidence is authoritative. Include all existing CapTest diagnostics relevant to
interpreting the force model, not only the current results-table columns.

| Section | Required contents |
| --- | --- |
| Provenance | Export/schema/model versions, creation time, app/firmware/build/protocol versions, board type and user board-instance ID, run/boot IDs, source hashes, simulated/hardware origin |
| Mapping | Frozen sensor-library layout/hash, arrays, group IDs, B/L/C/R/T routes and polarity conventions |
| Acquisition | ADC bits and voltage span, code-to-volts convention, requested/reported clock and engine where available, measured frame/connected times, sensor/Vmid counts, sequence, sampling phase, selected routes/order and timing provenance; the coefficient **acquisition signature** (section 3) |
| Coverage | Selected array/packages, per-package status and revision, board-level coverage, merge history |
| Electrical inputs per route | Rb and its source/tolerance, loaded Ctotal, tau, VD, c0, c1, qv; units, method, compatible voltage/timing domain and quality/availability status |
| Baseline and diagnostic values | Cshare at VD and 1 V below VD, q_cycle_pc, Cblank/C_net when measured, baseline matching/disconnection boundary, Vinf/quiet voltage and noise when available, existing fit/residual/timing/reset diagnostics and flags |
| Repeats and evidence | Per-burst parameters, used/excluded status and reason, aggregation method, measured spread, raw ADC words/channel tags, frame timestamps and raw packets/settings sufficient to reproduce results |
| Known and unknown force inputs | Optional independently established Coff, leakage law, KQ/KF or group d33_eff with their provenance; otherwise explicit unavailable/null, never an inferred measured value |

Canonical calculations use SI units. UI/CSV may show pF, pC/N, us and grams.
Serialize unavailable numbers as JSON null with a reason; prohibit NaN/Infinity.
Separate measured repeatability from systematic uncertainty. Do not invent
uncertainty, calibrated voltage domains or leakage measurements not in the data.

For each compatible alternate fit, retain direct coefficients. `c0` is already
stored per burst as `Measurement.c_share_ratio`, and `c1`/`qv` follow from
`connect_info` (see `analyze_alternate` in `capfit.py`). Recovery from exported
summary fields is needed only for legacy/row-only data:

```text
c0 = Cshare_at_VD / Ctotal_loaded
c1 = (Cshare_1V_below_VD - Cshare_at_VD) / (Ctotal_loaded * 1 V)
qv = q_cycle_pc * 1e-12 / Ctotal_loaded
```

Aggregate compatible per-burst coefficients explicitly, retaining their
association with VD, Rb and timing. Do not combine methods/configurations or
derive a supposedly joint fit by mixing unrelated summary means. Legacy raw
files may be reanalyzed to populate missing fields; result-only files lacking
the required evidence remain incomplete.

Record the fitted **voltage-distance range** of each route: the alternate fit
uses only cycles with `d > ALT_MIN_DISTANCE_V` (6 mV) after a start at least
`ALT_MIN_START_V` (0.3 V) below VD. The continuous-capture operating point
(section 3) is checked against this range.

### 2.1 Electrical-model quality and readiness

Coverage and quality are independent. A fully measured package may still be
unusable for force. Add explicit measured/estimated provenance and model-quality
status per route, plus derived package readiness and reasons. Preserve existing
coverage fields for partial-file compatibility; do not reuse `complete` to mean
that every model is trustworthy.

Define recorded quality criteria for residual RMS, repeat consistency, timing,
reset/tag integrity, parameter identifiability and model behavior over the
claimed voltage domain. `HIGH_RESIDUAL` blocks normal force readiness by default
until the route is re-measured or an explicitly reviewed fallback is selected.
Other fit warnings must be classified by policy rather than ignored merely
because the required numbers are finite. A null/non-finite equilibrium diagnostic
must be reported and investigated where it indicates an invalid model.

Use ADC4:1 / A2_PZT1 L from the audit as a regression fixture: all three bursts
have HIGH_RESIDUAL, mean fit RMS about 8.19 LSB, and fitted c0 about -0.077.
Negative c0 alone is not proof of an impossible physical capacitor or defective
sensor: c0 is an effective switching coefficient. Do not reinterpret it as Cn.
Combined fit quality and independent prediction checks decide usability.

### 2.2 Optional median-based estimate for a failed route

Offer **Use estimated electrical parameters** for a specifically selected failed
route. Re-measurement is the preferred path. The option must show the failed
measurement, reason, donor candidates, target board blank and resulting estimate
before the user applies it to a new model revision. Do not overwrite the failed
raw measurements or relabel an estimate as measured.

For healthy donors of the same sensor size/type and compatible strip/flex wiring:

```text
C_added_j = Ctotal_loaded_j - Cblank_j
C_added_est = median(C_added_j over approved healthy donors)
Ctotal_target_est = C_added_est + Cblank_target
```

Each donor uses its **own matching strips-off board blank**; the target uses its
own. Do not add the target blank to an already-loaded donor median, which would
double-count board capacitance. A strips-off subtraction estimates sensor plus
removed strip/flex capacitance, not Cs alone, and it does not identify Coff or Cn.
If strip wiring differs, quantify that uncertainty or exclude the donor; adding
the target board blank does not correct unknown target strip/flex differences.

Donor eligibility requires accepted measured fits, compatible board instance/
baseline boundary/method/configuration and explicit sensor-size/type/wiring
classification. Do not assume every outer position is the same size, or infer a
failed target's size from its unreliable fitted capacitance alone. Require an
explicit user classification when the library lacks that information, and save
its provenance. Prefer matched electrical paths and the same ADC when borrowing
switching behavior; donor count and diversity must be visible. Three or more
eligible independent donors are preferable for a robust median. A smaller set
must be labelled low-confidence, with no claim of outlier robustness; no healthy
donors means the estimate is unavailable. Reject nonpositive added-capacitance
values and nonpositive target estimates, rather than clamping them into validity.

Store donor identities, measured Ctotal and matching Cblank values, source hashes,
size/wiring metadata, spread/uncertainty and exclusion reasons. Shared systematic
errors are not reduced merely by having more donors. Missing or incompatible
target blank data blocks this parasitic-corrected estimate; never use a group
median board blank without a separate explicit assumption/provenance.

Replacing capacitance alone does not replace c0/c1/qv. Preferred recovery:

1. Fix or constrain target Ctotal to the estimate (including its uncertainty).
2. Re-fit c0, c1 and qv to the target's own compatible raw electrical transitions.
3. Compare residuals, repeat consistency and independent predictions with the
   original fit, then retain both revisions and their quality status.

If the target raw transitions cannot support that fit, a full donor model is a
separate, explicit experimental option with stronger compatibility requirements.
Do not silently copy normalized switching coefficients, assume Cshare is Cn, or
assume a common absolute switching charge just to rescale them. VD comes from the
target ADC's measured reference. Record the provenance of every substituted
parameter and label unsupported quantities unavailable.

Estimated models permit provisional package calibration only. A genuine bad
sensor/connection/circuit is not repaired by borrowing coefficients. Promotion
to validated effective package force requires independent known-weight loading,
holding and unloading, both directions, held-out repeats, contact positions and
the claimed voltage/timing range. Estimated electrical provenance remains even
after force validation; that validation does not establish true generated charge
or independently validate each substituted physical parameter. Re-measurement,
donor/baseline changes or a changed estimate invalidate dependent fits.

## 3. Continuous force-calibration acquisition

Add a driver capability for long-running, ordered, multi-channel acquisition
with measured timing. Implement it for the 7953 CapTest firmware and host
client/protocol together. Do not simulate continuity by interleaving separate
`capburst`/`capalt` requests and hiding the gaps.

The selected PZT group determines which five channels are analyzed, but the
**scan schedule always visits all routes of both arrays** (50 sensor routes),
not only the tested package. This keeps the per-channel revisit interval close
to production and prevents the much faster group-only rate, which increases
the accumulated bias (section 4). All routes are recorded; only the selected
group is analyzed for calibration.

The schedule has a configurable, recorded **added inter-visit lag**, so the
same package can be calibrated at several sampling rates (zero lag = fastest).
Each sampling rate is a separate capture signature with its own calibration
record; a coefficient fitted at one rate is not silently applied at another.
Comparing rates is a planned test (section 6): a fixed per-visit residual bias
accumulates faster at higher visit rates. A slower rate may reduce that error,
but leakage, read attenuation and signal bandwidth also change; charge/force
accuracy must be checked rather than assumed rate-independent. The production
firmware remains the later acceptance target;
capturing with the production firmware is deferred until these CapTest-firmware
tests are complete, and its schedule equivalence must then be demonstrated.

Start with manual sequencing, one retained sensor sample per visit and one
Vmid reset between routes on each ADC. Preserve correct two-frame pipeline
association, final flush/parking and tag checks. The two-time CapTest fit is
used to obtain circuit parameters; force capture runs a fixed production-like
schedule, not alternating short/long visits unnoticed.

**Coefficient transfer.** CapTest measurements show a frame-locked charge
transfer (tau depends on the frame period), so `c0`, `c1` and `qv` belong to the
capalt timing in which they were fitted: sensor/Vmid run lengths, `gap_ns`, the
long-gap stretch, SPI clock, first-clock delay and frame period. Store this as
an **acquisition signature** with each coefficient set and compare it with the
capture schedule's frame timing and connected time `Tc`. A mismatch blocks normal
calibration use. An explicit exploratory capture may proceed provisionally;
independent constant-force and force-response validation, linked as evidence,
is required before the changed context becomes validated. A user acknowledgement
alone is not validation. The off time between visits
of the same sensor differs anyway (a full two-array revisit versus one capalt
cycle), which is why leakage is handled in section 6. Also check that the
steady-state distance below VD of each analyzed route lies inside the fitted
voltage-distance range (section 2); report extrapolation instead of hiding it.

**VD in continuous capture.** CapTest takes VD from the park and lead Vmid
frames, while the reset frame closing each capalt cycle is checked only to
within 5 mV (`ALT_RESET_TOLERANCE_V`). With one Vmid frame per route, specify
which Vmid readings estimate VD (e.g. the reset readings after validating their
offset against a park measurement at the start and end of the capture), and
record VD stability. A frozen bias can compensate an approximately constant
prediction offset near the baseline. In the nonlinear model, even a constant VD
error can create state-dependent residual error away from that operating point;
the bias stage does not establish accuracy throughout a force excursion. Report
reference offset/drift and validate across the claimed voltage domain.

Define an additive, capability/version-negotiated capture protocol before
implementation: explicit configure/start/stop/status, bounded binary chunks,
sequence/boot/session identity, integrity checks, raw sensor codes/tags, valid
Vmid observations/reset diagnostics and enough frame timing to establish
connected, sample, release and same-sensor revisit intervals. Record which
timing quantities are measured versus derived. Extend wrapped clocks safely.

Use bounded acquisition/transport buffers and disk-backed raw recording for
multi-second holds. Do not mask interrupts throughout a hold or let plotting
cadence drop physical samples. On overrun, missing records, invalid tags,
clipping, reset failure, disconnect or restart, mark the affected trial invalid
and guide a retry. Stop/cancel/error must release resources and park the ADCs.
Older firmware keeps its existing burst/export workflows and reports the new
capture feature as unavailable. Do not automatically flash another image.

### 3.1 Recording identity and evidence retention

Give every session/trial a collision-resistant ID and use exclusive creation for
raw recordings. Group labels and counters are descriptive fields, not unique
filenames. Restart, Reset trials, retry, group switching and interrupted capture
must never truncate an existing .cap, .npz or trial metadata file. Reset clears
active wizard/fit state while preserving all saved evidence.

Create an atomic per-trial sidecar at capture start and update it at every mark
and terminal state. Persist device-clock placement/removal/hand-clear marks,
baseline boundaries, automatic or user-confirmed event windows, mass/repeat,
measured settings, electrical/estimated model revision and hashes, proposed/
applied corrections, raw/companion hashes and completion/error status. Keep
rejected, cancelled and partial captures reviewable without fitting or saving a
calibration first. An incomplete capture may remain diagnostic but cannot pass
calibration integrity checks. The sidecar/raw files must support replay of the
original decision, not just decoding its voltages with whichever model is current.

## 4. Shared residual, charge accumulation and force calculation

Create widget-independent helpers in `CapTest/captest/` for both live updates
and replay. Initialize each route on its first valid sample; it contributes no
preceding dQ. Maintain state independently per physical channel and generation.

With a fixed VD and negligible off-state leakage:

```text
d_prev = VD - V_previous
E = exp(-Tc / (Rb*Ctotal_loaded))
V_pred = VD - E*((1 - c0 - c1*d_prev)*d_prev + qv)
e = V_measured - V_pred
```

Use measured VD and loaded Ctotal, not Cs, C_net, a board blank or
`Ctotal-Cshare`. Compute in the calibrated physical voltage coordinate; apply
documented output polarity once. Use span/4096 for 12-bit 7953 codes and do not
reconvert data already in volts.

Existing baseline-centred display signals may remain available. If calculation
uses that coordinate, retain the fixed/tracked offset B and use
`d_prev = (VD - B) - (V_previous - B)`, with predicted and measured residual
expressed in the same coordinate. Re-zeroing a display must not replace VD,
discard absolute-voltage history or alter physical increments.

Initial weight-calibration mode follows the discussed **capacitance proxy**:

```text
dQ_proxy_i[k] = Ctotal_loaded_i * e_i[k]
Q_proxy_group(t) = sum_i sum_k dQ_proxy_i[k]
F_group(t) - F_group(t0) = (Q_proxy_group(t) - Q_proxy_group(t0)) / d33_eff_group
```

The UI plots per-channel residuals and charge proxies, group accumulated charge
proxy, and group relative force in N (optional mass equivalent in g). Call the
charge estimate a proxy in this mode: `Ctotal*e` is not established as true
generated charge on this switched board. Fitted d33_eff absorbs the tested
system gain; one group factor cannot remove arbitrary channel-dependent errors.

Store `charge_conversion_model=capacitance_proxy_v1` with every fit. Design the
processor interface to accept a later validated KQ/read-response model without
reinterpreting old coefficients. Analytical `Ctotal*e/E` is only an ideal-sharing
limit with the guide's additional assumptions; do not silently enable it or
derive Coff/Cn from Cshare. A change of conversion model requires recalibration.

Sum increments directly: **no additional multiplication by sample time**.
Timing already affects the circuit prediction/gain. No event-driven resets,
release-tail suppression, adaptive force zeroing, raw-voltage deadband or
stuck-force decay may alter calibration data. Display smoothing is separate.
Keep raw and corrected residuals and record any explicit bias correction.

**Bias sensitivity and drift budget.** Any constant bias in `e` becomes a linear
ramp in `Q_proxy`. Order-of-magnitude estimate (assumed: 75 pF routes, 104 us
full-board revisit, the review's example 600 pC/N, which is not a measured
value): a 0.01 LSB (about 6 uV) bias on all five routes drifts about 22 pC/s,
while the 20 g step is about 118 pC, so a 10 s hold drifts more than the step.
A 1 mV error in VD or in the modelled equilibrium produces roughly
`(1-a)*1 mV` of bias, with `1-a ~ c0 + (1-E) ~ 0.06-0.1`, i.e. about 0.1 LSB.
White noise, in contrast, largely cancels because each prediction uses the
measured previous sample (its random walk scales with about `(1-a)*sqrt(N)`).
Therefore:

- A labelled **per-route bias estimate** from the unloaded baseline window is a
  first-class stage. It is fitted on that window only, then frozen for the
  trial and recorded; it never adapts during a transition or hold.
- Added inter-visit lag (section 3) can reduce a fixed per-visit bias contribution;
  validate signal recovery separately when the rate changes.
- Set an explicit budget for allowed residual bias versus allowed drift over the
  hold and event windows, and report the measured bias against it.

The noise-cancellation estimate above assumes a locally linear transition and
appropriate measurement-noise statistics. Quantization, correlated noise and
nonlinear prediction error need measured uncertainty/drift checks; do not treat
the estimate as a guarantee.

Evaluate prediction uncertainty against the **residual and accumulated force**,
not only the full ADC-voltage excursion. In the audited T-channel recording,
the nonlinear c1 contribution was about 23 mV at a 0.65 V excursion. Calling it
roughly 4% of the swing neither measures its error nor bounds the force error:
e is a subtraction and can be near zero while the individual model terms are
much larger. One fitted d33_eff can absorb a consistent gain scale; it cannot
generally repair state-dependent prediction errors or residual bias accumulation.

## 5. Force Calibration tab and guided measurement

Inputs: current measured dataset or imported (possibly partial) force-input
manifest containing a qualified model for the selected package, or an explicitly
selected estimated model with provisional status;
board-instance/layout identity; Array and PZT group; initial signed d33_eff
in pC/N (finite and nonzero); capture schedule including the added inter-visit
lag; trial repeat count and timing. Default to five repeats per mass,
adjustable. Show dataset/group readiness, coefficient provenance, acquisition
signature match and calibration compatibility before capture.

Show a dashed red zero-force reference and keep it visible in automatic Y scaling.
Link force and raw-residual time axes so both pan/zoom together, with independent
N/mV scales and aligned plot areas. Use the same 30 s interval in both live plots
and redraw the full selected interval in both after completion or saved-trial
review. Preserve brief residual peaks when downsampling, rather than retaining
only a separate 5 s residual tail or skipping transient samples uniformly.

Allow an editable list of calibration masses in grams, defaulting to
`20, 50, 100, 200`. Two selected weights are sufficient with repeated trials;
reject empty, nonpositive, non-finite or duplicate masses. Run the distinct
masses from smallest to largest, keeping separate active state when the mass
list changes. Add reference **Force [N]** to the results table using
`mass [g] / 1000 * g`. Keep mounting and contact-position text as optional
annotations in logs/records; editing notes must not reset trials or select a
new calibration identity. Preserve each trial's original annotation on replay.
Explain the absolute validation-error budget, fractional hold-drift budget and
normally-off experimental voltage excursion guard in field/label hover help.

The initial d33_eff only scales the live force display. The step fit (section 6)
is linear and needs no starting value, so the initial value cannot bias it; the
fitted sign may differ from the initial one and is reported.

Support a fixed relative sign per B/L/C/R/T channel before summation, independent
of the existing common output polarity. Store selections per Array/PZT in the
force-input file, with +1 defaults for legacy files. Do not infer a sign solely
from capacitance-fit coefficients or rectify with absolute values. Correct each
charge contribution as `dQ_i = common_polarity * relative_sign_i * Ctotal_i *
(e_i-bias_i)`, then subtract any frozen charge-rate correction expressed in that
same signed coordinate. Both loading and removal retain their directions.
Use identical signs in live accumulation, drift/leakage analysis, gain fitting,
saved records and replay. Each analysis revision retains its original sign map.
Sign changes replay preserved captures, clear old drift/leakage corrections and
invalidate gain/validation. Keep electrical coefficients, voltages and raw
residual diagnostics in the original electrical coordinate. Confirm signs with
unclipped known-weight loading/removal at the intended contact positions.

Add a per-package press/release polarity check before drift and weight calibration.
Never assume C is opposite: some packages have matching signs, and any position
may differ. A dedicated listener takes a brief quiet sample, detects a natural
press and opposite release automatically, and stops shortly after release. No
stage buttons, minimum hold, long drift pretest or weight-trial state changes.
Use short blocks of electrical prediction residuals relative to recent quiet
bias to avoid mistaking ordinary RC recovery for release. Require consecutive
multi-channel activity to reject noise/spikes; stop after 20 s if no clear pair
is detected. Save raw/NPZ/sidecar evidence also on timeout/cancel. Compare per-route
signed event-charge steps without applying the current
relative/common signs or relying on the group sum. Require a strong response on
both events with opposite signs; weak, invalid, incomplete or conflicting channels
remain unclear. Allow clipping/headroom for this polarity-only check; keep it as
a persisted per-channel note, including saturated sample counts and unreliable
charge magnitudes. It must not block a clear direction or change the force-calibration
clipping checks. A steady group hold is not required. Display press/release pC,
confidence/reasons and suggested signs relative to the majority response. Apply
only after explicit review and only when every channel is clear. Save the accepted
check with the Array/PZT sign map, source recording/hash, model and automatically
detected event windows. Keep the legacy marked-test review available and
legacy/manual selections usable. A check that agrees with current signs does not
reset corrections; a changed sign map uses the normal invalidation/replay path.
Repeat at intended contact positions: bending/contact may change response signs;
this check does not establish intrinsic electrode polarity or known-force gain.

Use a stateful wizard driven by captured data and explicit user actions:

1. **Prepare:** select, for example, Array 1 / PZT1. Show its routes and ask the
   user to keep a load-spreading plate/fixture in place throughout. All added
   calibration load must pass through the included package; record mounting,
   contact position and any load bypass. No external load-cell connection is
   required: the known mass supplies endpoint reference force.
2. **Unloaded baseline:** start continuous capture with no added mass. Record
   quiet voltage, VD stability, residual statistics and accumulated-force slope.
   Fit and freeze the per-route bias estimate (section 4) and report it against
   the bias budget. Choose the known unloaded origin as zero relative force.
   Noise is expected; do not force every residual sample to zero.
3. **Place 20 g:** arm the event before prompting placement. Mark the action,
   capture the complete transition, and let the user mark **hand clear** (or
   apply a configurable minimum delay) because the hand adds force while placing
   or removing. Detect a stable post-placement window only after that, and allow
   the user to confirm/retry its boundaries. Never calibrate to impact peak.
4. **Hold:** record a configurable 10-30 s constant-load interval (initial
   default 10 s), show drift/slope and preserve the same physical accumulator.
5. **Remove 20 g:** arm before removal, capture the complete transition and
   a stable unloaded window. Check loading/removal consistency and return to zero.
   Removal may be cleaner but must not be assumed free of pull/adhesion artifacts.
6. **Repeat and increase:** guide the same sequence for 50 g, 100 g and 200 g,
   with the selected repeats. Preserve failed trials and reasons; let the user
   retry or skip a mass without claiming validation over the skipped range.
7. **Fit and validate:** compare initial and fitted d33_eff on the same data,
   show errors/drift and held-out validation, then let the user save the group
   result. Save trial evidence and provisional fits independently of successful
   validation. Switching groups starts a separate active state and coefficient
   without deleting or overwriting other groups' evidence.

Reference changes use `F=m*g`, with recorded g (default 9.80665 m/s^2):

| Added mass | Placement delta F | Removal delta F |
| --- | ---: | ---: |
| 20 g | +0.196133 N | -0.196133 N |
| 50 g | +0.4903325 N | -0.4903325 N |
| 100 g | +0.980665 N | -0.980665 N |
| 200 g | +1.96133 N | -1.96133 N |

Use stable pre/post windows, initially 0.5 s and configurable, with boundaries
outside the mechanical transient. Compare both the post-placement and pre-
removal holds; drift must not be hidden by picking only favorable endpoints.
After loading, display force should approach the known plateau; after removal
it should return to its unloaded origin. Starting a removal-only trial while
loaded gives a known negative step, not an inferred initial force of zero.

Show three separate indicators on every route: **ADC headroom/integrity**,
**electrical-model coverage** and **force-validation coverage**. The requested
masses are the target sequence, not a guarantee of measurable range.

- ADC saturation or capture-integrity failure invalidates the affected trial.
  Record the actual span, code limits and configured headroom guard; do not
  describe a headroom guard near 2.49 V as the exact physical rail. Recompute
  limits for the configured ADC range. Keep raw evidence for diagnosis.
- Above VD is not automatically an invalid force signal. Remove any default
  VD+0.25 V rejection: that number has no established physical significance.
  An optional user excursion guard may remain, off by default, with its source
  and value saved. It is an operational guard, not proof of a calibrated range.
- Record both above-VD and below-fit extrapolation, affected routes, duration,
  extrema and range violations at raw sample and predictor-input levels. Below-
  fit voltage must remain visibly out of model coverage; do not silently clamp
  or extrapolate it as validated. Exploratory recording may continue within
  valid ADC headroom, with separate provisional/invalid-analysis status.
- A polynomial being smooth above VD, a small term relative to total voltage,
  or an excursion observed in a finger trial does not validate the extension.
  Known-force/charge checks and held-out force trials must cover the new domain
  and relevant polarity/timing before declaring it validated. A training trial's
  observed extrema alone do not become an independent validated range.

Range failure may block escalation to heavier masses, but **Retry failed must
allow another attempt at the failed mass**, preserving failed attempts and their
IDs. Keep heavier masses blocked until the retry demonstrates suitable headroom
and the required quality status; then explicitly restore eligible pending steps.
Skipped masses stay distinguishable from failed trials and are not validated.
Prompts must match actions actually allowed by the state machine. With no
accepted events, offer recovery/diagnosis rather than "fit and validate".
Finger tests are workflow/exploration only and must be labelled so; an unknown
finger force cannot be assigned the wizard's nominal 20 g reference for gain fitting.
Do not fit saturated data or silently change ADC range/schedule while retaining
the previous calibration.

## 6. Fit d33_eff and leakage in separate, identifiable stages

For each accepted event j, calculate the signed charge-proxy difference between
its pre/post windows and the signed known force change:

```text
Q_proxy(t) ~ Q0_j + r_j*(t - t_j) + delta_Q_proxy_j*H(t - t_j)   over pre+post windows
delta_F_ref_j = +m_j*g for placement, -m_j*g for removal
delta_Q_proxy_j = d33_eff_group * delta_F_ref_j + intercept
```

`delta_Q_proxy_j` is the step at the event time `t_j` (H: unit step) after
removing the residual drift rate `r_j`, estimated from the pre-event window (or
jointly with the post window, recorded). A plain `Q_after - Q_before` would
include drift times elapsed time, and a single global intercept cannot absorb
that because events last different lengths of time. Report `r_j` per event and
flag events whose drift contribution is large compared with the step.

This is an **assumption-based step estimator**, not identification of the ramp's
physical cause. A genuine slow force variation can also be removed by the ramp
fit. Keep plain endpoint differences, pre-only/joint drift fits, original curves,
window selections and the correction magnitude available for comparison. Do not
claim a ramp is "not force" solely because it fitted a drift term. Test the
assumption on labelled known-mass plateaus, independent repeats and slow ramps;
large drift corrections or disagreement between estimators fail the relevant
validation budget or remain provisional.

Fit a common signed slope across valid masses/directions/repeats; inspect the
intercept as a bias diagnostic rather than hiding a large intercept. Report
initial/fitted pC/N, repeat spread, force errors, directional differences and
fit quality. Use separate repetitions for validation and retain their selection.
Treat 20-200 g as calibration range only when actual accepted data supports it.

Use one consistent force estimator for display, validation, export and downstream
consumption. Report actual deployed error `delta_Q_proxy/d33_eff - delta_F_ref`
as well as optional intercept-adjusted regression residuals. A fitted event
intercept is a diagnostic, not automatically a continuously applicable force
offset. A significant intercept requires investigation or an explicitly defined,
independently validated estimator; subtracting it only in error metrics must not
make the deployed force appear more accurate than it is.

Validate training/test sufficiency before fitting: keep all events from each
held-out repetition together and check the remaining sample count and rank.
Two repetitions of one mass leave only two training events when one is held out;
if the fitter requires three, report insufficient evidence rather than raising an
uncaught ModelError. Handle fit, validation and session errors consistently in
the UI. No numeric acceptance limit means validation has not passed.

**Sampling-rate comparison.** Repeat the calibration of the same package at
several added inter-visit lags (section 3), always with the full two-array
schedule. Compare per rate: bias, hold drift, fitted d33_eff, repeat spread and
validation error. Keep each rate as a separate record; select the operating
rate from this comparison rather than assuming the fastest one.

Calibrate leakage to make constant-force intervals remain constant, **not by
adjusting d33_eff to conceal drift**. Evaluate residual-vs-voltage and force
slope during unloaded, loaded hold and post-removal intervals. Preserve the
CapTest transition coefficients as the starting model; validate them on the
actual capture schedule before adding a correction.

Implement an optional off-state leakage model/correction with explicitly fitted
parameters and reference voltage. For a verified linear leakage path:

```text
V_before_no_change = Vleak + (V_after_previous_visit - Vleak)
                    * exp(-Toff/(Roff*Coff))
```

This physical form needs independently known Coff and a verified mapping from
ADC reading to stored sensor state. Otherwise fit a clearly labelled effective
observation/transition correction over the tested voltage/timing domain; do not
claim that a hold slope alone measured Roff or assume Rb is Roff. Add targeted
rest/variable-revisit characterization when needed to identify leakage.

Fit supported nuisance parameters on constant-force data first, freeze them,
then refit d33_eff from the load steps and validate both on separate trials.
Reference drift, qv error, temperature, cross-channel coupling and mechanical
creep can also produce drift. If the data cannot distinguish them, report the
ambiguity and failed hold validation; do not return arbitrary physical values.
Persist an explicit "leakage not identified" status where appropriate. Automatic
time-dependent force decay/zeroing is never a substitute for leakage calibration.

### 6.1 Separate unloaded-drift measurement before weights

Offer **Measure unloaded drift** using the unchanged full scan, no added weights
and hands off (default 10 s, adjustable 6-120 s). Discard 2 s of settling, fit a
constant per-channel residual bias on the first remaining half, and independently
check stationarity on the last half with block means. Reject invalid/headroom/
below-fit or changing data, retaining the prior correction. A qualified result
supplies frozen `bias_unloaded` to `dQ = polarity*Ctotal*(e-bias_unloaded)` in
both live and offline processing. Do not adapt it during weight transitions or
holds. This does not identify voltage-dependent leakage or physical Roff.

Check each weight trial's quiet baseline against the frozen correction; a shift
keeps force validation provisional rather than silently changing the correction.
When no prior correction exists, wait for the trial baseline bias before showing
integrated force. Capture and retain raw data, fit/check windows, uncertainty,
before/after held-out drift, models, scan and source hashes. Applying/replacing or
clearing this correction replays weight trials and invalidates gain/validation.
Changing the leakage predictor clears the old unloaded correction. Preserve an
offline review path and retain previous correction/evidence on cancel/failure.

### 6.2 Continuous linear-drift test followed by the first weight trial

Add a combined **Drift test → first weight trial** workflow for the observed curved
startup followed by linear drift. Search sliding recent windows for stable raw
charge slopes, excluding the initial transient. Fit the first half, check the
later half, and compare training subwindow slopes. Require the sum of absolute
channel slope changes to fit a recorded drift budget. Convert the configured
force-error budget (or hold-drift fraction of the smallest planned force) into a
charge-rate planning budget using the initial sensitivity and hold duration;
do not treat this as independent force validation. Wait up to 120 s or window
plus 2 s; timeout keeps prior correction and never falls back to the transient.

Keep acquisition uninterrupted into the first weight prompt. Freeze per-channel
charge drift rates and apply `dQ_corrected = dQ_raw - rate*actual_visit_interval`
through loading, holds and removal, live and offline. Store fit/check windows,
slopes, remaining drift, budget, immutable training-prefix identity and final
recording hash. Preserve the loading step and held-force plateau. The combined
workflow is started before collecting weight trials; further trials retain the
correction and recheck the unloaded slope after settling before arming placement,
without automatically refitting. Reset begins another combined run.

### 6.3 Evaluate, apply, replay, refit and validate leakage

Represent correction state explicitly: not evaluated, evaluated/not identified,
proposed, applied and validated. The tab needs distinct **Evaluate leakage** and
**Apply correction and reanalyse** actions, showing model/domain, objective and
uncertainty. Inspection/evaluation alone never changes the active model or
serializes a proposed correction as though it produced the saved gain.

Use integrity-valid raw captures with explicitly labelled constant-force windows
for diagnosis, including a rejected force trial when its rejection was hold/model
quality. Do not use corrupted/clipped data, mislabelled changing-force intervals,
or require a successfully accepted force trial merely to investigate its leakage.

On application, create a new correction revision and invalidate previous gain,
event estimates, validation and validated range. Replay every affected original
capture with that same applied revision, recompute and freeze per-route bias on
its original baseline window, review/recompute event windows and acceptance, fit
d33_eff again, and validate on separate evidence. Retain prior results for audit.
If reanalysis changes automatic windows, save both sets and the reason. Rollback
also creates a coherent model revision and requires appropriate refitting.

Live predicted voltage/residual, frozen bias, accumulation, saved events and
exported force must all use the same applied model revision. A proposed fit is
saved separately with `applied=false`; an applied correction must identify the
exact revised electrical/estimated model, trial analyses, gain and validation
that used it. Do not combine a corrected live accumulator with an uncorrected
residual display or a gain fitted before application.

A single package gain is the first feature. Per-channel sensitivity fitting is
future work unless enough independent placements/excitations make the fit full
rank; one known package load does not supply five independent channel forces.
Require independent contact-position checks before claiming position-independent
package accuracy. Bias and leakage corrections must never adapt during an
unlabelled force transition and erase slow real loading/unloading.

## 7. Save/load and future force-consumer contract

Save versioned `captest-force-calibration-1` records keyed by board-instance,
layout/hash, array and package, with separate d33_eff per group. Saving PZT1 must
not overwrite PZT3 or the other array's PZT1. Keep the initial input, fitted
coefficient, output sign convention, conversion model and calibration revisions.

Each group record contains the linked/embedded electrical manifest and hashes,
capture signature, effective leakage/correction model and validity, masses/g,
mounting/contact notes, raw time series and event windows, accepted/rejected
trials, fit/validation statistics, supported voltage/force/timing range and
simulated/hardware status. Preserve independent coefficients for different
schedules/sampling rates rather than replacing them with one global last-used
value. Recalibration after a physical remounting keeps prior revisions; optional
note edits alone do not change the calibration identity or reset trials.

Expose separate statuses such as **not fitted**, **fitted/provisional**,
**validated** and **stale**, plus measured/estimated provenance as an independent
field. Completing trials or obtaining a slope does not by itself mean validated.
Save raw evidence and provisional results at any stage, but export/apply a
validated package coefficient only after explicit configured force-error,
intercept, loading/removal consistency, held-force drift and domain criteria
pass on independent evidence. HOLD_DRIFT, LOAD_REMOVAL_INCONSISTENT, extrapolation
and estimated-model flags remain visible and participate in that decision;
they must not be bypassed by `accepted=true` alone. A failed validation cannot
be converted to success by changing only the record label.

Record acceptance limits, validation estimator, evidence split and the actual
passed/failed/insufficient-evidence outcome. Invalidate stale gain/validation on
new or edited trials, event marks/windows, coefficients, donor/blank inputs,
correction application, range, polarity, mapping, actual physical mounting or
schedule changes. Editing a mounting/contact annotation alone is not evidence
of a physical change and does not invalidate the fit.
Legacy `calibrated` status without this evidence imports as provisional rather
than acquiring a validation claim. Schema changes require explicit versioning
or a documented migration; preserve existing raw capture compatibility.

Use a portable manifest plus raw capture companions and atomic writes in the
existing board-specific result location. Allow explicit Save/Load and browsing
different groups. Never overwrite the sensor library's electrical mapping or
material sensitivity with a calibration result. On import, validate schema,
hashes, units, group routes and board/schedule compatibility; show stale or
unsupported records for review without silently applying them.

The original force-input export contains CapTest-measured inputs even before
weight calibration. Exporting an updated bundle after calibration can include
the fitted group sensitivities and leakage records. Do not require those values
to export the initial electrical dataset. Define the contract for future
ADC_Gui consumers to distinguish calibrated package force from electrical charge
and to reproduce the selected model without dividing by d33 twice.

## 8. Implementation slices and file ownership

1. **Recording protection and recovery:** harden existing `capture.py`,
   `force_session.py` and wizard state first: collision-proof evidence files,
   per-trial sidecars, Reset preservation, range-failure retry and accurate prompts.
2. **Data/schema/group quality and fallback:** extend existing
   `force_calibration.py`, `sensor_groups.py` and `force_export.py` with typed
   quality/provenance states, compatibility checks and donor/blank estimates.
   Add constrained target refitting in `capfit.py`/`runner.py` as needed.
3. **Electrical export:** update the action in `gui.py` and session methods;
   round-trip measured parameters/evidence, partial save and later update,
   without changing old formats.
4. **Capture capability:** verify/harden `boards/base.py`, `boards/testboard_7953.py`,
   the 7953 descriptor, `client.py`/`protocol.py`, and
   `Arduino_Sketches/TestBoard_7953_CapTest/` together. Specify/test the new
   protocol and timing semantics; reuse production acquisition components only
   where their behavior and timing are actually compatible.
5. **Pure processing/replay:** harden `force_model.py` and trial analysis:
   residual/domain checks, charge proxies, uncertainty, consistent deployed
   force/error estimators and leakage application/replay/refit/validation.
6. **Wizard/tab:** extend `force_calibration_panel.py` and its session/worker
   helpers; keep `MainWindow` as composition/wiring. Show per-route model quality,
   estimated provenance, ADC/model/validation coverage and actionable failure
   reasons. Acquire/process off the GUI thread with coherent model snapshots;
   rendering/status callbacks must not race away a rejection message.
7. **Persistence/docs:** finish multi-group Save/Load, independent per-trial
   persistence, quality/correction revisions, updated export bundles and
   migration/compatibility messages. Update
   `CapTest/README.md`, the CapTest user guide/help, 7953 CapTest protocol document
   and capacitance-to-force guide to describe the implemented behavior.

Do not change production protocol/firmware merely to expose this tab. If timing
equivalence requires a shared production-source change, scope and verify that
coupled change explicitly. Preserve existing capacitance/Vinf/baseline workflows
and other-board support throughout.

## 9. Verification and acceptance

Add focused tests under `CapTest/tests/` for:

- All ten bundled mappings, custom compatible layouts, invalid routes and array
  separation; group identity independent of global selected_name.
- Package selection, partial save and later update/merge (new package, superseding
  revision, refused merge on board/layout/settings mismatch), atomic writes.
- Acquisition-signature mismatch detection and operating point outside the
  fitted voltage-distance range, with exploratory capture separated from
  permission to call a model/force range validated.
- HIGH_RESIDUAL and other quality criteria block normal readiness even when
  all five routes have finite coefficients; preserve exportable failed evidence.
  Negative effective c0 alone does not imply a measured negative capacitor.
- Median estimates subtract each donor's matching blank before adding the
  target blank. Cover same-size/wiring classification, insufficient/bad donors,
  missing/incompatible blanks, no double subtraction/addition, donor spread,
  constrained target refit, separate measured/estimated provenance and invalidation
  after donor/baseline changes. Known synthetic target capacitance should be
  recovered within an independently specified uncertainty; fallback must not
  make a deliberately faulty sensor pass force validation.
- Bias stage frozen after the baseline; step-plus-ramp event fit recovering a
  known step under deliberate residual bias; drift scaling with visit rate.
- Complete/partial/failed-pass export readiness, every required coefficient,
  per-burst provenance, unknown/null values, strict JSON, portable raw evidence,
  legacy reanalysis and mapping/board/configuration mismatches.
- Constant-force nonlinear transitions yielding zero residual, independent
  synthetic force steps, correct SI conversions, sign and first-sample handling.
  Show explicitly that capacitance-proxy mode need not recover true charge.
- Initial-to-fitted d33_eff recovery for known simulated system gain, signed
  loading/removal, multiple masses, intercept/drift detection, held-out failures
  and failure of a common gain when channel responses differ by contact position.
- Leakage correction on independently generated identifiable data, deliberately
  ambiguous leakage/bias cases, preservation of held force and no hidden resets.
- Leakage evaluation does not apply a model or mislabel its saved state;
  application replays all trials with consistent bias/model revision, invalidates
  old fits, refits and validates, supports rollback and keeps live/offline parity.
- A 0.65 V above-VD excursion inside ADC headroom is retained as exploratory
  data without the old 0.25 V default rejection, yet cannot silently become a
  validated domain. Test both sides of the fitted range, configured guard limits,
  span-dependent clipping and non-finite/invalid predictor inputs.
- Slow real force ramps can resemble fitted drift; preserve them in raw output
  and prevent step-plus-ramp fitting from being presented as causal proof. Test
  significant intercepts against actual deployed error, not only regression
  residuals. HOLD_DRIFT/inconsistent load-removal and absent/failed validation
  cannot produce a validated calibration label.
- Two-repeat/single-mass hold-out insufficiency, training rank, missing numeric
  limits and fit/validation errors are reported without crashing. Any change to
  trials, models or correction makes previous validation stale.
- Continuous capture ordering, chunk fragmentation/CRC errors, wraparound,
  pipeline association, gaps, clipping, cancellation/disconnect, firmware
  capability gating, parking and lossless recording across long holds.
- Wizard transitions/prompts for all four masses, retry/skip semantics,
  unchanged physical acquisition during plot refresh, save/load per group and
  rejection of incomplete/out-of-range trials.
- Retry succeeds after the first 20 g range failure while heavier steps remain
  blocked until recovery. Reset/restart/group switching/cancel/retry never reuse
  a recording path for writing. Original raw/NPZ hashes remain unchanged.
- Failed and cancelled captures retain sidecars/marks, hashes, windows and reasons
  before a gain exists; replay reproduces the original classification. Exercise
  partial writes and interrupted sidecar updates without corrupting prior evidence.
- GUI rejection messages remain visible in the combined test suite, not only
  isolated reruns; investigate the audit's order/timing-sensitive prompt failure.

Run focused new tests plus affected existing alternate-fit, raw export/replay,
measurement-validity, GUI, protocol and board end-to-end suites. Build the 7953
CapTest firmware with its documented PlatformIO environment. No software test
alone establishes physical force calibration.

Hardware acceptance uses the final loaded wiring and the recorded full
two-array scan. Verify timing/VD/reset behavior, record accepted 20/50/100/200 g
trials where headroom permits, both directions and repeat placements, hold
drift, measured bias against its budget, contact-position sensitivity and at
least two added inter-visit lags. Include controlled above-VD response checks,
electrical-fit quality and a deliberately exercised estimated-model fallback.
Use independent known-weight/charge evidence; finger presses test workflow only.
Production-firmware capture is a later stage. Repeat at least one package on
each physical array and verify calibrations remain separate. Set
quantitative error/drift limits from the intended measurement requirement and
measured noise; record them before declaring validation successful.

The feature is complete when a selected package exports a replayable, updatable
electrical bundle; qualified measured or explicitly estimated models can drive
continuous guided capture; all trial evidence survives recovery; leakage
application/replay/refitting is coherent; and independent package records save/
reload with honest fitted/provisional/validated status and reproducible validation.
Passing software tests or a headroom check alone does not establish force accuracy.
