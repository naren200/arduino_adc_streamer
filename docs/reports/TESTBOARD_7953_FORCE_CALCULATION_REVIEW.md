# TestBoard_7953 force calculation review and required changes

Reviewed: 5 November 2026.

> **Update (2026-11-10):** the Force Calibration tab reviewed here was removed from the application and archived under `Legacy/`. Findings about it are historical; the Analysis and Pressure Map findings still apply.

Scope: Analysis, Pressure Map, and Force Calibration in `adc_gui.py`, compared
with the current working copy of the
[capacitance-to-force guide](../guides/TESTBOARD_7953_CAPACITANCE_TO_FORCE_GUIDE.md).
This is a code review and implementation proposal. No application or firmware
behavior was changed, and no hardware force calibration was performed.

## Finding

The existing calculated force is an RC reconstruction with event/display
heuristics. It does **not** implement the TestBoard_7953 switching recurrence.
Changing the capacitance setting, leak resistance, or sample interval cannot
make it implement that recurrence. The guide also requires a calibrated
residual-to-charge or residual-to-force gain; capacitance measurements alone
do not establish that gain.

Analysis and the Pressure Map Force Display share the RC integrator, but their
preprocessing, timing, and reset policies differ. The Force Calibration tab
does not currently calculate physical sample-to-sample force increments or fit
the gain needed by the guide.

Here `dF` means `F[n] - F[n-1]` for consecutive retained samples of the **same
physical sensor channel**, in N. It is not `dF/dt`, in N/s. Accumulation is
`F[n] = F[n-1] + dF[n]`; there is no additional multiplication by sample time.

## 1. Current calculation and tab paths

### Shared RC calculation

[PztForceChannelIntegrator](../../data_processing/pzt_force_calculation.py)
implements, before its event/reset adjustments:

```text
v[n]       = baseline-centred voltage, subject to event/noise gating
tau_on     = Rleak * C
alpha      = exp(-Tconnected/tau_on - Toff/(Roff*C))
beta       = exp(Tpre_sample/tau_on)
dF_RC      = (C/Sq) * beta * (v[n] - alpha*v[n-1])
F          = F + dF_RC
```

The off-state term is optional. If connected exposure is unspecified,
`Tconnected` defaults to the entire interval between samples. `Tpre_sample`
defaults to zero. Capacitance is selected globally as center or outer, and
`Sq` is the positive `d33` setting, converted from pC/N to C/N.

This is a predicted-decay subtraction, not simply `C*(V[n]-V[n-1])/Sq`.
However, it has no `c0`, `c1`, `qv`, independently calibrated `KQ`/`KF`, or
physical Vmid-reference record. The `beta` RC correction is not the guide's
general calibrated read gain.

The state machine also thresholds/zeros voltage, suppresses release tails,
ends events near force zero, and can decay a quiet residual force to zero.
The returned `PztForceStepResult.delta_force_n` is the final accumulator change
**including** any reset or artificial decay. It therefore cannot always be
interpreted as newly generated physical force change.

### Comparison

| Surface | Actual active input/calculation | Consequence for TestBoard_7953 |
| --- | --- | --- |
| Analysis calculated-force traces | `prepare_analysis_data` converts counts to voltage, optionally filters, then `build_calculated_pzt_force_traces` calls the shared batch RC helper. Per-label quiet baseline/noise estimates can be supplied; otherwise the trace median is subtracted. | Quiet baseline is not necessarily physical `VD`. Optional filtering precedes prediction. The output is accumulated force, with per-channel event resets, rather than a separate physical dF trace. |
| Analysis timing | `_auto_pzt_mux_connected_time_s` explicitly returns unavailable when `testboard_acquisition` metadata exists. Manual, continuous, and total-sample-rate inference are alternatives. | Auto calculated force is skipped with a status message. The alternatives do not establish the calibrated ADS7953 connected interval. `1/fs_total` is not a measurement of that interval. |
| Pressure Map **Force Display** | The active TestBoard path is `LiveAcquisitionMixin` -> `AcquisitionWorker._force` -> `ForceBlockWorker._integrate_sweeps` -> `PressureForceDisplayEngine`. It subtracts plot/worker baselines, applies polarity, and uses the same channel integrator. | The batch carries no MUX exposure model (`mux_timing=None`). Thus the RC integrator uses the full per-channel revisit interval as leak exposure. Center/outer capacitance and one d33 do not represent calibrated physical routes. |
| Pressure Map Force Display aggregation | Channel force accumulators feed shear extraction and normal-force summation. Package resets/quiet-force decay are coordinated. Jerk grids provide the spatial shapes painted by accumulated force layers. | Package force is not integrated a second time, which should be preserved. Raster shape and its N-labelled intensity are inferred visualizations, not an independent pressure measurement in Pa. A missing Jerk shape can defer raster painting while scalar force already exists. |
| Pressure Map pressure/shear signal path | `_prepare_signal_integration_integrated_series` uses optional median baseline removal, optional HPF/DC removal, a rolling sum of voltage samples, polarity and configurable gains. | This is a separate force-like signal path. Its rolling sum has no TestBoard charge reconstruction; it is not physical sample-to-sample dF. Changing the rolling window changes its response. |
| Force Calibration | Sources are Raw Piezo, Heatmap Signals, and Pressure/Shear Signals. Raw reads the latest buffer sweep in counts; heatmap uses processed intensities; pressure/shear uses the preceding rolling-sum path. Rows store latest T/B/L/R/C values and summary fields. | It has no switching-residual source, reconstructed-dF source, or KQ/KF fit. These rows are not complete electrical/force calibration records. |

Relevant entry points:
[Analysis](../../data_processing/analysis_workbench.py),
[live options](../../data_processing/live_acquisition.py),
[acquisition worker](../../data_processing/acquisition_worker.py),
[force worker](../../data_processing/force_block_worker.py),
[force display](../../data_processing/pressure_force_display.py),
[Pressure Map panel](../../gui/signal_integration_panel.py), and
[Force Calibration panel](../../Legacy/gui/force_calibration_panel.py) (since archived).

### Two additional input/calibration issues

1. `AcquisitionWorker._consume` passes its force consumer a block **after**
   ghost correction when enabled and an unconditional causal median-of-three
   blip filter. Consequently, a buffer named raw is not necessarily the
   original ADC measurement. With ghost removal disabled, the worker archives
   the original raw block; with it enabled, the archive retains the processed
   representation. Analysis can also apply its own filter. The guide's
   recurrence cannot be assumed valid on these altered samples.
2. The Force Calibration measurement window's `force_z_peaks` and
   `force_x_peaks` are currently populated by derived shear T-B/L-R values.
   They feed legacy `max_force_z/x` fields and are displayed as shear columns.
   `ForceProcessorMixin.process_force_data` stores the external load-cell
   signal separately; it does not pair it with this measurement window.
   Requiring a connected load cell before Start Measure does not establish
   that reference force is being recorded in the calibration rows.

## 2. Required physical reconstruction

Implement a separate TestBoard switching-model calculation, shared by batch
and live callers. For the guide's negligible-off-leakage, fixed-reference case:

```text
d_prev = VD - V_previous
E      = exp(-Tc / (Rb*Ctotal_loaded))
d_pred = E * ((1 - c0 - c1*d_prev)*d_prev + qv)
V_expected_no_force_change = VD - d_pred
e      = V_measured - V_expected_no_force_change

Electrical calibration route:
    dQ = KQ(d_prev, Tc, Tvisit, ...) * e
    dF = dQ / Sq_signed

Direct force calibration route:
    dF = KF(d_prev, Tc, Tvisit, ...) * e

F_relative = F_relative + dF
```

Use raw ADC voltage in the coordinate used for calibration. A zero residual
must produce exactly zero reconstructed dQ/dF. Apply the calibrated sign once,
through signed sensitivity/gain or an explicitly equivalent output convention;
do not reverse nonlinear-model input voltage as a display operation.

Use loaded `Ctotal` in the transition. Do not substitute `Cs`, `C_net`, or
`Cblank`, subtract `Cshare` as if it were `Cn`, or apply a route-length force
multiplier. Calibrate KQ on the final loaded path, or calibrate KF directly.
Do not divide KF-derived force by d33 again.

The analytical alternative `dF = Coff*e/(a_read*Sq)` is conditional:
`a_read = E*(1-c0-2*c1*d_prev)`. It requires independent Coff and verification
of the sensor-state/readout relationship, charge timing and monotonic range.
CapTest's fit and disconnected-strip baselines do not establish those facts.
Use the guide's nonlinear inversion only after its additional assumptions and
domain checks are satisfied. Do not enable it merely because Ctotal is known.

## 3. Changes needed, in implementation order

### A. Define and retain complete calibration records

Add a versioned, per-board/per-ADC/per-input record containing:

- Physical route and sensor/mounting identity; calibration method and version.
- Measured physical `VD` per ADC, actual ADC span and conversion convention.
- `Rb`, loaded `Ctotal`, `c0`, `c1`, `qv`, with explicit SI units.
- Either KQ plus signed Sq, or signed KF; voltage/timing domains and waveform
  assumptions, uncertainty/quality flags, temperature/reference limits as measured.
- Acquisition signature: engine, clock, sequence, retained repeat, Vmid reset
  pattern, scan order/selected routes, connected-time convention and timing map.
- Off-state leakage/state correction only where independently characterized.

Use the existing frozen descriptor's route mapping to associate physical
`(ADC lane, input)` with array/package/position. Never key calibration solely
by C versus outer, display-array selection, or a reusable label such as PZT1.
Moving a strip changes electrical-path calibration even if its mechanical
sensitivity remains applicable.

Extend CapTest export/import. `capfit.py` retains `c_share_1v_pf` and
`q_cycle_pc` in each measurement's `connect_info`; `ChannelResult.row()` does
not export those coefficients as a complete force record. For compatible fits:

```text
c0 = Cshare_at_VD / Ctotal
c1 = (Cshare_1V_below_VD - Cshare_at_VD) / (Ctotal * 1 V)
qv = q_cycle_pc * 1e-12 / Ctotal
```

Retain raw packets, VD, timing, flags and per-burst fits. Average only compatible
measurements with a defined aggregation procedure; missing parameters must not
silently become zero. Existing capacitance results supply no measured KQ/KF.

### B. Provide the right acquisition conditions and timing

For the guide's initial supported production mode, require verified effective
manual sequencing, channel repeat 1, and Vmid reset between sensor channels.
The firmware defaults are repeat 3 and Vmid-between-channels false; the GUI
profile defaults to repeat 1 but also Vmid false. Force mode must check the
effective status rather than assume either default is suitable.

The present manual firmware already inserts one Vmid command between routes
and retains two tail pipeline-flush commands; it keeps only the final sensor
result when repeating. Preserve that bookkeeping. Do not reinterpret discarded
repeat conversions as host samples or remove the pipeline flushes.

Supply calibrated `Tc` per route/visit and same-sensor `Tvisit`. Current
per-frame footer timing and interpolated within-sweep offsets do not identify
the physical connected window. Characterize that window for a fixed production
schedule with the CapTest convention, including engine/inter-frame overhead;
store its provenance with the capture. Do not use `16/fSPI`, total-rate inverse,
or the complete revisit time as an unverified replacement.

Use offline VD only while its stability is verified. Per-visit live Vmid
monitoring or arbitrary variable connected timing requires additional
firmware/status telemetry and corresponding parser/export support; these are
conditional extensions, not prerequisites for a validated fixed schedule.
Returned channel tags do not themselves prove that Vmid reset settled.

### C. Implement a shared measurement core before display processing

Add a small widget-independent switching-model processor under
`data_processing/`, selected explicitly for calibrated TestBoard captures.
Retain the existing RC model for compatible older workflows.

Its per-channel state must retain the actual previous voltage and timestamp,
calibration identity and acquisition generation. Its result should expose
measured/predicted voltage, residual, dQ when available, physical dF,
accumulated relative force, and validity/reason. The first valid sample only
initializes state. Missing visits, clipping, invalid timing/reference/model
range or calibration mismatch must flag/reinitialize or invalidate the
accumulated history, never bridge an ordinary single-visit recurrence silently.
After a gap, earlier absolute relative-force continuity is lost unless supplied
by a validated recovery model/reference.

Feed it absolute samples **before** ghost subtraction, median blip filtering,
HPF, rolling sums and event gates. Keep raw capture data and preprocessing
provenance sufficient to reproduce live results offline. If a future calibrated
observation filter is used, it must explicitly account for its temporal response.

Use `code*span/4096` for the guide's ADS7953 convention. Current Analysis and
live force conversion use `span/4095`; reconcile this small scale difference
(about 0.0244%) across calibration, reconstruction and exports. It is not the
main error. Distinguish counts from already-voltage data and do not rescale
twice. A legacy file's scaling/preprocessing convention must remain identifiable.

### D. Connect Analysis and Pressure Map to that core

Analysis should use saved physical calibration and acquisition metadata,
calculate before optional display filtering, and offer residual, dF and relative
F traces. Replace the TestBoard Auto timing dead end with resolution of a
validated saved timing record. Reject incompatible or incomplete records with
a useful reason; manual entry alone is not evidence of calibration.

The active TestBoard live change belongs in `AcquisitionWorker._force` and
its options/data path in `live_acquisition.py`, not only in the legacy
`process_pressure_force_block` GUI path. Keep ordered, exactly-once updates
for both arrays and existing capture-gap handling. Reuse channel forces for
package normal/shear calculations without integrating twice. Validate package
normal/shear transfer against reference loads as well as individual channels.
Jerk raster shapes may remain a visualization heuristic, with explicit units.

Keep measured dF and relative F separate from event detection, natural-zero,
release-tail suppression, stuck-force decay and user display zeroing. In a
measurement mode, a quiet held force must not be removed by these heuristics.
Display offsets/resets should be explicit and recorded without overwriting
physical increments. Any noise gating should follow residual calculation and
use characterized residual noise; retain ungated results for audit/calibration.

### E. Make Force Calibration acquire and fit the required response

Add a switching-residual/calculated-force source using the same measurement
core. Record synchronized full sensor/residual and external reference-force
time series, rather than pairing independent peaks or the latest display row.
Account for load-cell bandwidth, timing offset and sample-rate differences.

Provide explicit electrical-charge calibration (`KQ=Qcal/e`) and/or known-force
calibration (`KF=dFref/e`) workflows, with repeated signed increments over the
operating state/timing range. Use regression/appropriate interval response and
quality checks where noise makes single ratios unstable. Package reference
force alone does not uniquely identify five independent channel gains without
known load distribution or enough independent calibration excitations.

Persist approved coefficients, raw evidence and fit quality in the shared
record consumed by Analysis and live force. Preserve legacy signal rows as
their original source/units; they cannot be automatically promoted to a physical
force calibration. Keep derived shear columns distinct from reference X/Z.

## 4. Numerical counterexample and verification

A synthetic no-additional-force transition demonstrates the model gap without
claiming a measured board error. Inputs were `Ctotal=73.3 pF`,
`c0=6.7/73.3`, `c1=(11.4-6.7)/73.3 per V`, `VD=1.25 V`, and
`V_previous=1.200 V`. The illustrative choices were `Rb=1 Mohm`, `Tc=1 us`,
`Tvisit=104 us`, `qv=0`, `Sq=600 pC/N`. Thresholds and automatic resets were
excluded, and the actual shared integrator was called with voltage centred on
the exact VD, so baseline error was not responsible for the difference.

| Calculation for the predicted no-force reading, 1.205343956951 V | dF |
| --- | ---: |
| Guide recurrence followed by residual/gain | 0 N |
| Current RC integrator, unspecified exposure as in the live TestBoard batch | -0.003977276839 N |
| Current RC integrator, even with the illustrative true 1 us exposure supplied | +0.000570085938 N |

This isolates the omitted connection terms: correcting exposure is necessary
but insufficient. The values are a model counterexample, not calibrated force
measurements on hardware.

Existing tests run during this review:

```text
python -m pytest -q tests/test_pzt_force_calculation.py tests/test_analysis_workbench.py
    tests/test_pressure_force_display.py tests/test_force_calibration_panel.py
101 passed

python -m pytest -q tests/test_testboard_acquisition_pipeline.py tests/test_generic_acquisition.py
47 passed
```

These tests validate current software contracts, not the proposed physical
model. Implementation acceptance needs new independent checks for no-force
nonlinear transitions, signed known-charge/known-force increments, first sample,
gaps/clipping/range failures, scaling, per-route calibration and array isolation,
live/offline equivalence and preservation of held-force state.

Hardware acceptance must use the final loaded path and production scan: verify
Vmid resets and sampling phase, characterize Tc/VD, test loading/hold/unloading
and both polarities across the calibrated range, repeat at multiple scan rates,
and compare with known charge and/or aligned reference force. Set quantitative
acceptance limits from measured residual noise, gain uncertainty and intended
force bandwidth. Until that gain and validation exist, label the result as an
uncalibrated estimate. Relative reconstruction also needs a known initial force;
it cannot recover an unknown static load already present when capture starts.
