# PZT6 unloaded-drift and weight-trial review

Reviewed the two `unloaded_A2_PZT6_235074f42bea40c0a3bbd9a91cdcaadc_00[12]`
recordings and their hashed `.trial.json` evidence. Both recordings have intact
transport and no ADC saturation or eight-code headroom violations. Analysis uses
the original captured polarity (all relative signs +1), before the polarity UI
change. Neither raw recordings nor saved sidecars were modified.

The user clarified that attempt 002 used a finger press to inspect the trace
before, during and after contact, rather than a steady calibration mass.
Consequently, a slope during contact cannot be identified as drift: finger
force can change. The guided workflow's mass/placement labels below describe
software states, not a verified constant mechanical load.

## Attempt 001: standalone unloaded measurement

The 10 s measurement was rejected for two independent reasons:

- L (ADC3:1) had one below-fit sample at 0.000082752 s: code 227,
  0.138550 V, versus the model's tolerated lower boundary of 0.147748 V.
  This is a valid ADC value during discarded startup. It should not reject
  the later fit over 2–6 s and independent check over 6–10 s.
- L's mean residual changed from -1.940055 mV in training to -1.890132 mV
  in the check: 49.923 microvolts, exceeding its 44.001 microvolt stability
  tolerance. Removing the startup-range rejection therefore does not qualify
  this attempt. Its fixed windows still contained a changing residual bias.

## Attempt 002: linear drift passed; finger test rejected by weight analysis

The continuous **Drift test → first weight trial** qualified at 15.040694 s:

| Quantity | Result |
| --- | --- |
| Training window | 5.040694–10.040694 s |
| Independent check | 10.040694–15.040694 s |
| Raw check charge slope | -578.163 pC/s |
| Corrected check charge slope | +0.973 pC/s |
| Corrected slope at initial 600 pC/N | +0.001621 N/s |
| Combined slope-change bound / allowed | 11.592 / 11.768 pC/s |

The configured hold-drift fraction was 1.0 (100%), so the qualification used
that relatively permissive budget. Passing establishes the tested unloaded
section, not arbitrary future loads or a physical leakage resistance.

The subsequent weight trial was rejected for `OUTSIDE_FITTED_RANGE` and
`NO_STABLE_POST_PLACEMENT`:

1. The only below-fit sample was L's very first sample: code 224, 0.136719 V,
   at 0.000082752 s. The force baseline starts at 15.340694 s. The old check
   incorrectly scanned discarded pretest/startup history as part of the force
   phase. Reanalysis with the corrected scope removes this reason. L's minimum
   voltage within the checked force interval is 1.036377 V, including its
   initial predictor input.
2. The corrected trace did not settle during the finger press. Over 26–33 s its slope
   is approximately +0.1630 N/s at the initial 600 pC/N display scale. After
   removal, over 38.5–40 s, it is approximately +0.1101 N/s. In contrast, its
   unloaded 15.5–18 s slope is -0.00170 N/s. The changing trace explains
   the stable-window failure but does not establish that drift correction
   failed under load: the applied finger force was not known to be constant.
   The short post-release interval likewise does not establish a settled
   unloaded slope; relaxation or other contact effects remain possible.

There is also a timing mismatch. Large placement-related voltage changes on
B/T begin around 21.4 s, before the placement mark at 23.515168 s. The selected
pre-placement window, 22.715168–23.215168 s, is therefore after those changes.
A prominent L removal-related change occurs at 35.531670 s, ahead of the
removal mark at 36.354334 s. The sampled changes do not prove their mechanical
cause, but the labelled windows cannot be assumed to bracket the actual events.
These windows require review before estimating sensitivity.

All force slopes here use the initial sensitivity, not a validated force gain.
The data alone cannot separate electrical-model error, voltage-dependent
leakage, changing finger force, mechanical creep/contact effects, or other
charge sources. This diagnostic run cannot establish force sensitivity or
force-hold accuracy. Its stable-window rejection is not proof of a hardware
or drift-test failure.

## Meaning of a low reading

Code 224 is well inside the ADC's 0–4095 codes. It is neither an overflow nor
an integration limit. `OUTSIDE_FITTED_RANGE` is a software rule: the voltage
lies outside the region used to fit the electrical predictor (including its
5% distance margin). A valid physical force signal can cross that boundary.
Duration alone does not determine validity, and samples must not be dropped
or clamped merely for being low. One sample also enters the next prediction.

ADC rail saturation, the eight-code headroom guard, and model extrapolation
are distinct. Actual saturation loses charge information; model extrapolation
creates uncertainty in the conversion, even with a valid ADC value. The
remaining lower-model boundary during the force phase is still conservative
software policy. A symmetric provisional extrapolation policy below/above VD
would be reasonable if paired with validation over the actual voltage range;
it would not establish a known force or aligned event windows for this finger test.

## Correction made following this review

Range/headroom checks now use the selected force-analysis interval or unloaded
fit/check interval and the preceding sample required by its first prediction.
The live indicators follow the same scope. Global capture integrity checks
remain global. Full raw data remains preserved, and force range diagnostics
also retain full-capture voltage extrema. New analyses save
`range_policy: analysis_window_v2`; old evidence replays its original
`full_capture_v1` decision unless explicitly reanalysed.

On the actual attempt 002, the corrected analysis yields only
`NO_STABLE_POST_PLACEMENT`. Attempt 001 still fails its residual-stability check.

For another finger diagnostic, use the linear pretest, leave the frozen
correction unchanged, then record an untouched interval before contact and a
longer untouched interval after release. Exclude the release transient and
compare settled slopes. A settled return to approximately the original slope
supports repeatability of the unloaded correction; a constant force offset is
a separate question from a continuing slope. Do not learn or subtract drift
from the unknown force during finger contact. For gain/hold validation, use
a known steady weight and review event marks before fitting sensitivity.
