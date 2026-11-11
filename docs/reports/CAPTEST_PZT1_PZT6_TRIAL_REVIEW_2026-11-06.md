# CapTest Array 2 PZT1/PZT6 trial review — 6 November 2026

Reviewed the saved trial sidecars and replayed their original raw recordings.
Times below are seconds from each capture's start. Both trials were labelled
20 g, repeat 1, with initial display sensitivity 600 pC/N. Recorded labels do
not independently establish the physically applied load.

## Recording integrity

| Group | Raw recording | Sweeps | Duration | Replay integrity |
| --- | --- | ---: | ---: | --- |
| A2_PZT1 | `trial_A2_PZT1_0831429538e342b6aba3fdd2d7265938_001.cap` | 23,808 | 39.405 s | Valid; no reported capture problems |
| A2_PZT6 | `trial_A2_PZT6_f50c321de9984934b54f64a457c15c28_001.cap` | 16,128 | 26.693 s | Valid; no reported capture problems |

Raw SHA-256 values:

- PZT1: `5a0c83b1f70c7c5a9f28879cbf378a92efdfe7da75704e36489e2705202c971c`
- PZT6: `69db53147bb130bc4777d5f6d7443135fb9178a869d7b11587e220ab037bbdc4`

Clean transport does not establish electrical-model or force accuracy.

## PZT1 rejection

1. **CLIPPED:** T, ADC4 input 4, reported code **4095** for **8 samples**
   at **15.9196–15.9312 s**, approximately **2.49939 V**. This is actual ADC
   full-scale coding, independent of force integration.
2. **ADC headroom / below-fit range:** C, ADC4 input 2, reached code **6**,
   approximately **3.662 mV**. There were **24 samples** within the low-headroom
   guard at 33.1218–33.1598 s, and **26 samples** below the electrical model's
   permitted lower voltage at 33.1201–33.1615 s. The lower rejection threshold,
   including the current 5% distance margin, was approximately **17.059 mV**.
   The trial-level message reports CLIPPED because T actually saturated; C also
   violated headroom, although it did not reach code zero.
3. **NO_STABLE_POST_PLACEMENT:** no acceptable loaded window was found after
   the hand-clear mark plus guard, beginning at **22.9115 s**. Placement could
   not produce a complete valid event pair. Estimator disagreement and a large
   drift correction were also flagged.

Placement was marked at **20.2694 s**, hand clear at **22.6115 s**; removal at
**34.8961 s**, hand clear at **36.3627 s**. The T saturation preceded the placement
mark by about **4.35 s**. The C low-voltage event preceded the removal mark by
about **1.78 s**. The recording establishes this timing mismatch, but cannot
establish whether its cause was delayed marking or earlier handling.

## PZT6 rejection

1. **ADC_HEADROOM_LIMIT:** T, ADC3 input 4, reported code **7** at
   **6.087793 s**, approximately **4.272 mV**. The software headroom guard
   includes codes 0–8 at the low end (4.883 mV). **No channel reached code 0
   or 4095** in this recording: this is a headroom rejection, not demonstrated
   ADC endpoint saturation.
2. **OUTSIDE_FITTED_RANGE:** T also fell below its model's permitted lower
   voltage, approximately **151.509 mV**, for two samples. Those samples were
   code 7 (4.272 mV) and code 109 (66.528 mV). The next sample was code 280
   (170.898 mV), back inside the permitted range. Loosening the headroom guard
   alone would not resolve this separate model-coverage failure.
3. **Hold and event-quality problems:** HOLD_DRIFT, LOAD_REMOVAL_INCONSISTENT,
   STEP_ESTIMATOR_DISAGREEMENT and LARGE_DRIFT_CORRECTION were flagged. The
   fitted removal/placement magnitude ratio was **2.356**, compared with the
   expected approximate ratio of 1. The hold drift corresponds to about
   **+0.489 N/s at the entered initial display sensitivity**. These are
   unvalidated display units, not established physical force measurements.

Placement was marked at **8.0244 s**, with hand clear at **10.2506 s**. The
T transient at 6.0878 s preceded that placement mark by approximately **1.94 s**.
The selected pre-placement window was **7.2244–7.7244 s**, after that transient.
If the transient was placement, the chosen pre-event window was not unloaded.
Removal was marked at 22.4691 s, with hand clear at 23.6542 s.

The sharp T sequence was:

| Time | ADC code | Voltage |
| ---: | ---: | ---: |
| 6.086136 s | 1990 | 1.214600 V |
| 6.087793 s | 7 | 0.004272 V |
| 6.089450 s | 109 | 0.066528 V |
| 6.091107 s | 280 | 0.170898 V |
| 6.092764 s | 507 | 0.309448 V |

This is a brief recorded voltage excursion. Its physical cause cannot be
determined from these samples alone.

## Drift correction and leakage-analysis message

Both sidecars contain `linear_pretest: false`, `checking_frozen_drift: false`,
no applied unloaded correction, and no pretest endpoint. Both used only the
automatic short baseline, **0.3–3.0175 s**. These runs therefore do not test
the combined linear-drift workflow or its frozen correction.

The screenshot's leakage-analysis refusal is expected for this rejected input:
the current evaluator excludes ADC headroom/clipping captures and requires
labelled constant-force windows. PZT1 also lacked a stable post-placement window.
No leakage correction was applied by that failed evaluation.

## Next measurement

Reset the weight trials (recordings remain saved), then use
**Drift test → first weight trial** before collecting another weight event.
Keep the sensor untouched until placement is armed. Mark the placement/removal
action at its start and hand clear separately, so pre-event windows precede the
voltage transition. The mismatch between marks and samples needs to be resolved
before fitting d33_eff; reducing load alone would not correct it.

The ADC endpoint/headroom and electrical-range failures are separate from drift.
A successful unloaded drift measurement does not recover information lost at
ADC saturation or establish model validity outside its measured voltage range.
The original recordings and application/firmware code were not modified by this review.
