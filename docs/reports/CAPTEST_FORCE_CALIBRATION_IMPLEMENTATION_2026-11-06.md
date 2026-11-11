# CapTest force calibration implementation — 6 November 2026

The software hardening in the [feature plan](../plans/features/plan-CapTest-Force_calibration.md)
is implemented in the standalone CapTest application. This follows the
[audit](CAPTEST_FORCE_CALIBRATION_AUDIT_2026-11-06.md), which describes the earlier
reviewed snapshot. Original electrical measurements and recordings were retained.

## Resulting behavior

- A complete measurement pass and a qualified electrical model have separate states.
  HIGH_RESIDUAL and invalid coefficients block ordinary force readiness, including
  legacy records. A negative effective c0 alone is not treated as a negative physical capacitor.
- Failed routes can use an explicitly reviewed median of healthy, declared same-size/type
  and wiring donors. Each donor's own matching strips-off blank is subtracted before the
  target's measured blank is added. The target transitions are refitted at the estimated
  loaded capacitance. Borrowing same-ADC switching coefficients is a separate experimental
  option. Donors, hashes, spread, declarations and original measurements remain available.
- Above-VD signals have no default 0.25 V rejection ceiling. Excursions are provisional
  extrapolation, separate from ADC headroom, actual saturation and below-fit coverage.
- Every attempt uses unique raw/NPZ files and atomic trial sidecars. Button marks, hashes,
  model/correction identities, windows, rejected/cancelled decisions and reanalysis history
  survive reset/restart. Saved evidence can be replayed offline without changing the active
  calibration. Numerical analysis failures retain the completed capture too.
- Leakage evaluation proposes a correction. Apply/rollback replays original recordings,
  re-estimates then freezes unloaded bias and invalidates gain/validation. Window edits and
  changed limits also require refitting. Live and offline calculations use the applied model.
- Gain regression diagnostics and deployed force errors are separate. The latter use raw
  endpoint charge divided by the frozen training gain, without removing the fitted event
  intercept. Drift, load/removal disagreement, estimator disagreement and reference instability
  prevent validated status.
- Records distinguish not fitted, fitted provisional, validated and stale. Known-weight
  held-out validation needs a numeric error limit. Workflow-only finger tests cannot fit
  nominal weight or establish a supported mass range. Only validated gains enter the bundle;
  changed electrical inputs mark stored gains stale. Earlier saved revisions are archived.
- Retrying a range failure is allowed before heavier masses. Successful recovery restores
  automatically skipped steps while preserving user skips. Group/context switching preserves
  the corresponding session. CPU/disk analysis runs outside the Qt event loop; conflicting
  board measurements and board switching are guarded during capture/analysis.

Implementation is split among `CapTest/captest/force_estimates.py`,
`force_estimation_panel.py`, `force_evidence.py`, and the existing force export,
calibration, model, session, capture and panel modules. ADC_Gui and production
force reconstruction were not changed by this implementation.

## Verification

- The CapTest automated suite exercises numerical reconstruction, simulated captures,
  electrical export/migration, raw replay, calibration records and headless Qt workflows.
  New regression coverage is in `CapTest/tests/test_force_hardening.py` and
  `test_gui_force.py`.
- `python -m pytest -q --tb=short CapTest/tests`: **484 passed, 13 skipped**.
- Focused follow-up suites passed: 49 export/hardening/session tests and 53 Qt force/board
  tests. Final workflow-only supported-range and failed-validation range regressions also passed.
- `pio run -e teensy41_captest` in `Arduino_Sketches/TestBoard_7953_CapTest` succeeded.
  No firmware was uploaded.
- Python compilation and `git diff --check` passed. Existing pyqtgraph default-value
  deprecation warnings remain in the GUI test suite.
- The force tab and donor dialog were inspected using Qt's offscreen renderer with an
  explicitly loaded Segoe UI font. The controls fit the left pane without horizontal
  scrolling; vertical scrolling preserves access to the additional controls.

## Hardware acceptance still required

No physical force accuracy is established by these checks. Use the actual board,
final wiring/plate, known weights and declared limits to test both loading and
removal, long holds, multiple contact positions, at least two scan lags, both
arrays and an intentionally exercised estimated route. Preserve those captures
and held-out validation records. Firmware scan timing/reset behavior and
production-scan equivalence also require measurement.

The conversion remains `capacitance_proxy_v1`: fitted d33_eff is an effective
package/system sensitivity for the tested mounting and schedule. It does not
establish material d33, independent charge calibration or position-independent
force accuracy.

## Follow-up: unloaded drift before weights

Added **Measure unloaded drift** (10 s default; 6-120 s) and **Clear unloaded
correction**. The measurement uses the same full scan without added mass,
discards 2 s for settling, fits per-channel residual bias on the first remaining
half and checks it on the later half. A qualified bias is frozen and subtracted
before charge integration in live plots, weight analysis and saved replay.
An unstable/invalid measurement leaves the previous correction intact. Weight
baselines diagnose a shift without adapting the correction during force holds.
Changing a leakage predictor clears this separate bias correction; replacing or
clearing it replays weight trials and invalidates gain/validation.

The startup force trace now waits for a bias estimate instead of displaying the
initial uncorrected baseline integration as force. Raw samples/residuals remain
available. Constant drift correction does not identify physical Roff or establish
voltage-dependent leakage compensation. Known-weight validation remains required.

Verification: **507 passed, 13 skipped** in the full CapTest suite, followed by
**42 passed** in the focused zero/session/GUI checks after separating weight and
unloaded plot sample sources. The completed unloaded GUI was visually inspected;
the new controls fit without horizontal overflow. Synthetic tests verify removal
of constant residual bias while preserving loading, held force and removal.

## Follow-up: linear section and uninterrupted drift-to-weight capture

Added **Drift test → first weight trial**, run before collecting weights.
Unlike the fixed-window unloaded measurement, it searches recent raw-charge
windows until their fitted and held-out slopes agree within the recorded drift
budget. Training subwindow slopes also reject curvature. A nonzero linear slope
is allowed. The selected duration is the search window; the search times out at
120 s or window + 2 s and never falls back to fitting the startup transient.

The first trial then begins on the same CaptureSession and recording, without
capstop, resync, capstart or a changed acquisition state. Per-route rates in C/s
are frozen and subtracted using each actual visit interval. Live plots, event
analysis and replay use the same rates. Real loading/removal steps remain in
the accumulator. Raw training-prefix hashes remain valid while later weight
data is appended; the completed recording also has its full-file hash.

The gate's planning budget is derived from the configured force-error budget,
or the hold-drift fraction of the smallest planned mass force, scaled by initial
sensitivity and divided by hold duration. It is not independent known-force
validation. Further trials retain the frozen correction and recheck it after
settling before arming placement. A different stable slope blocks placement
rather than automatically fitting a replacement. Reset permits a new
combined drift-to-first-weight run. The existing standalone measurement remains
available separately.

Regression coverage includes an initial exponential transient followed by a
linear slope, actual variable revisit intervals, a complete continuous simulated
drift/placement/hold/removal capture, matching offline replay, timeout preserving
the old correction, and immutable-prefix hash verification. The new GUI workflow
was visually inspected at the armed placement prompt with no horizontal overflow.
The complete CapTest suite after these changes passed: **514 passed, 13 skipped**.

## Follow-up: relative polarity of individual channels

Added **Relative polarity** controls for B/L/C/R/T, saved per Array/PZT group in
the force-input file. They multiply each corrected charge contribution by a
fixed +1/-1 before summation, independently of the common output polarity.
Unloading stays signed; absolute values are not used. Raw electrical voltages,
CapTest coefficients and the raw residual plot retain their original coordinate.

The PZT1 replay supports the reported opposite centre response relative to
B/L/R on loading and removal. T's clipped loading data cannot establish its
relative response reliably. Sign choices therefore remain explicit, with +1
legacy defaults; clean known-weight trials must verify the declared pattern.

All force paths use the same sign map: live accumulation, drift measurement,
leakage evaluation, event analysis, gain fitting and saved replay. Drift rates
are learned in the signed charge coordinate; raw residual bias remains in volts
without sign reversal. Changing signs replays preserved trials, clears old
unloaded/inferred leakage corrections and invalidates gain/validation. Calibrated
gain entries become stale when their sign map differs. Each saved analysis keeps
its own map, so old and revised interpretations can be replayed independently.

Tests verify mixed-polarity summation, negative unloading, common-sign application
once, signed drift correction, per-group persistence without electrical-data
changes, old/new replay and GUI control locking. The Array 2/PZT1 controls were
visually checked with C = -1 and no horizontal overflow.
The complete CapTest suite passed after the polarity changes: **521 passed, 13 skipped**.

## Follow-up: per-package press/release polarity check

Added **Record press test** and **Review press signs…** beside the relative-sign
controls. Recording selects unknown-force workflow mode and uses finger-specific
action prompts. The checker compares each channel's event charge independently
of existing signs and the group sum. It fits local pre-event slopes, requires
clear opposite press/release responses, and proposes signs relative to the
majority press direction. There is no assumed centre reversal and no need for
a steady group hold. Weak, clipped, conflicting or incomplete responses cannot
be applied. Model extrapolation remains visible in the review.

**Apply signs to this package** saves the reviewed check, marks/model identity
and recording hash with the selected Array/PZT sign map. Closing the review
does not apply it. Agreement with existing signs preserves corrections;
changed signs use the existing replay/invalidation path. Unknown finger force
cannot fit force gain. Users should repeat at intended contact positions;
mechanical bending/contact may alter response direction, so this is not proof
of intrinsic electrode polarity.

Focused regression checks passed: **56** polarity/export/GUI tests and **49**
session/drift/evidence tests. Tests cover all-matching signs, opposite C, other
mixed patterns, globally reversed responses, weak/clipped/conflicting channels,
explicit application and per-group persistence with source hashes. Controls and
the review dialog were also rendered and visually inspected. The prior PZT6
finger recording returns unclear, without applying or modifying any signs.

## Follow-up, 2026-11-09: automatic polarity listener

Replaced the marked polarity-recording workflow with a dedicated short listener.
Click **Record press test**, allow the brief noise sample, then press and release
naturally. It detects both responses and stops automatically shortly after
release. There are no placement/hand-clear/removal buttons, minimum hold,
weight-baseline delay or drift-pretest wait. Stage buttons remain disabled.
The listener does not change workflow-only selection, consume/reset weight
trials, or invalidate an existing force fit. Applying changed signs still uses
the normal correction/fit invalidation path.

Detection uses short blocks of individual electrical prediction innovations
against recent quiet bias, without common/relative signs or a group sum. This
avoids calling ordinary RC relaxation a release. Consecutive activity across
multiple channels identifies the pair; each route must have clear opposite
event signs before Apply is enabled. Weak/conflicting/clipped responses remain
unclear. A 20 s idle/incomplete timeout and cancellation preserve diagnostic
evidence and leave the weight plan intact. Raw/NPZ files, source hashes, model
identity and automatically detected event windows are saved under purpose
`polarity_test` and format `captest-press-polarity-2`. Old marked recordings
remain reviewable through the earlier checker.

The simulated GUI press/release run stopped in under 3 s total, without any
stage button presses. Focused regression suite: **113 passed**. Subsequent
automatic-listener/UI checks: **11 passed**, including an additional check
that an existing force fit and its original trial evidence remain unchanged.
Noise-only input, a brief spike, RC decay without release, very short taps,
mixed/all-matching signs, weak/clipped channels, timeout/cancel, raw replay,
and GUI stage-button locking are covered. The listening screen was rendered
and visually inspected; physical hardware acceptance remains to be run.

## Follow-up, 2026-11-09: allow clipping in polarity-only checks

Clipping and the ADC headroom guard are now informational notes for both
automatic and legacy marked polarity checks. Clear opposite press/release
directions remain eligible for Apply. Non-finite/invalid, weak or conflicting
signals still block it. The review's **Notes / reason** column includes clipping,
saturated-sample counts and a warning that clipped charge magnitudes are
unreliable; live polarity messages no longer say to reject a clipped trial.
Reports persist the notes and `clipping_policy: note_only`. Force calibration
and unloaded-drift clipping rules were not changed.

Read-only replay of the user's A2_PZT3 recording
`polarity_A2_PZT3_1166f6b8ff2d4bc9ae71a2f016d7bb7b_001.cap` now yields
`ready_for_review`: B/L/R/T +1, C -1. L retains its 6 saturated-sample note and
R its 40-sample note. Neither original evidence nor active sign settings were
modified. Focused polarity, force-calibration and GUI tests: **88 passed**.
The actual-recording review dialog was rendered and visually inspected with
clipping notes visible and Apply enabled.

## Follow-up, 2026-11-09: synchronized force/residual plots

Added a dashed red zero-force reference that stays included in automatic
vertical scaling. Both plots share a linked horizontal axis, aligned plotting
areas and independent N/mV vertical scales; panning/zooming either moves both.
Live force and residual data use the same last 30 s rather than independent
30 s/5 s tails. Completion, drift results, fitting/window review and offline
replay redraw both plots over the same full selected interval. Valid recordings
without stable event windows can still show their diagnostic force trace.
Cancelled captures retain their own live view instead of redrawing a prior trial.
Residual curves use peak downsampling to preserve brief changes in long captures.

GUI suite: **33 passed**, with **6** relevant checks repeated after the final
cancel-view guard. Tests cover linked pan/zoom in either direction, visible
zero reference, matching live intervals, preserved residual spikes, and complete
residual history in failed-trial and saved-evidence review. Read-only replay of
`trial_A2_PZT3_c8776e2c6a164082bad1d4ab01127f62_001.trial.json` was rendered and
visually inspected: the press near 6 s and release near 20 s line up in both
plots, and the zero reference is visible. Its raw evidence was not modified.

## Follow-up, 2026-11-09: delayed trial start and interrupted prechecks

The approximately 40 s attempt was still verifying a frozen drift correction
when a transport gap stopped it: 214 sweeps were missing. Its latest summed
per-channel mismatch was 26.6 pC/s against a 1.77 pC/s budget. An earlier fresh
drift test passed at 26.12 s but lost data during the following trial baseline.
See `CAPTEST_PRETRIAL_DRIFT_START_FAILURE_2026-11-09.md` for the recording review.

Live checks now omit unused all-route timing statistics, reducing snapshot
time from approximately 33 to 3.8 ms on the saved recording while preserving
identical selected samples and ADC-reference statistics. Final results retain
full statistics. SerialLink requests a 1 MiB receive queue on supporting drivers
and falls back normally if resizing is unavailable. These changes reduce
avoidable host stalls; they do not prove the physical transport fault eliminated.

The live prompt now reports drift stability/mismatch budgets and rejection
reasons while waiting, and final failure messages retain the last precheck
alongside transport errors. A prior qualified result is not presented as a
passed new precheck before enough current data exists. No thresholds or capture
integrity rules were relaxed. Focused tests: 90 passed; full CapTest suite:
**554 passed, 13 skipped**. The updated wait/rejection display was visually checked.
