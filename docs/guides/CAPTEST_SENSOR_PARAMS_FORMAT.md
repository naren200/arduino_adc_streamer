# Compact CapTest sensor parameters

Saving a full force-input JSON also creates a compact companion beside it:
`TestBoard_7953_SensorParams_YYYY-MM-DD_HH-MM-SSZ.json`.
The timestamp is UTC and identifies the first compact export. Subsequent saves and
calibration phase outcomes replace that same companion atomically. The full file's
`sensor_params_export.file` records the name, so reopening the full file preserves it.
Independent full files get different companion names, even within one second.

The format is `captest-sensor-params-1`, schema version 1. It contains the latest
parameters and statuses only: no raw packets, waveforms, per-burst data, or trial
histories. Keep the full input JSON and recordings for verification and refitting.
Adding compact-export state does not change the electrical-manifest identity.

To create it for an existing full JSON, run from `CapTest`:

```powershell
python -m captest.sensor_params Results/TestBoard_7953/force_inputs_board_2026-11-09.json
```

This also records the companion name in the full JSON. It preserves all electrical
measurements and packets. Old full files do not contain the unloaded drift or trial
history, so this conversion does not invent those values or search unrelated runs.

## Fields

All electrical quantities use SI units. Missing values are JSON `null`, never zero,
an empty string, NaN, or the initial display sensitivity.

| Location | Meaning |
| --- | --- |
| `board`, `firmware` | Board type/instance, hardware or simulation origin, and measured firmware identity. |
| `created`, `updated` | UTC compact-export creation and update times. |
| `source_file`, `electrical_manifest_sha256` | Full input file name and electrical identity. The source file is for provenance; raw evidence is not needed for calculation. |
| `adc_bits`, `voltage_span_v` | Convert raw ADC codes into volts: `code * span / 2**bits`. |
| `electrical_acquisition_signature` | Timing/method used for the electrical coefficients. |
| `common_polarity` | Common board/library sign, applied once before summing channel charge. |
| `groups.<id>` | Array/PZT identity, measurement times, force status and one effective package gain. |
| `groups.<id>.capture_signature` | Scan and polarity context of force/drift testing. Do not silently reuse corrections at another scan rate/order or setup. |
| `groups.<id>.d33_eff_c_per_n` | Signed **package** sensitivity, populated only after known-mass fit and independent validation pass. A provisional fit or finger test leaves it `null`. |
| `groups.<id>.force_ready` | All package electrical inputs are usable and a current validated gain is available. This does not waive runtime clipping, model-range or transport checks. |
| `groups.<id>.leakage` | Applied model only (`lam`, `v_leak_v`, `kind`), otherwise `null`. A proposed/evaluated correction is not exported as applied. |
| `groups.<id>.channels` | Five channel records with position, ADC/input, coefficients and statuses. All mapped channels are present, including unmeasured ones. |

Channel coefficients are `r_bias_ohm`, `c_total_f`, `v_d_v`, `c0`, `c1_per_v`, and
`qv_v`. `tau = r_bias_ohm * c_total_f`, so a redundant tau is omitted. `d_min_v` and
`d_max_v` describe the electrical fit domain in `d = VD - V`. Each channel also
includes its relative polarity and measured/estimated parameter source.

The gain appears once per package because the weight test calibrates the sum of
five channel charges. Individual-channel newton values obtained with that gain are
contributions to the package sum, not independently calibrated sensor gains.
When available, validated force and voltage ranges accompany a calibrated gain.
Measurement times are nullable when the full source has no corresponding record.

## Statuses

- `captest_status`: `not_measured`, `failed`, `high_residual`, `invalid`, or
  `complete`. `captest_issues` retains blocking quality reasons. Coverage alone
  does not qualify a poor fit.
- `force_calibration_status`: `not_tested`, `in_progress`, `polarity_tested`,
  `polarity_unclear`, `workflow_tested`, `tested`, `failed`, `provisional`, `stale`,
  `invalid`, or `calibrated`. Only `calibrated` can have a non-null gain.
- `polarity_status`: `applied` or `not_tested`. Default +1 values are not a claim
  that a polarity test passed. Manual sign selections are also marked applied.
- `drift_status`: `not_measured`, `rejected`, `stale`, or `included`.
  `drift_last_test_status` reports the latest drift attempt; an unsuccessful
  attempt can leave an earlier qualified correction active.

An included drift correction passed an **unloaded** fit/check, not force validation
under load. Its root `drift_scope` is `unloaded_check_only`. It can make a loaded or
post-release trace worse if the residual changes, as observed in finger tests.
Remeasure and check it for the current board session, temperature, wiring and
mounting. It is not a physical Roff measurement or a guarantee of zero force.

Changes to the electrical identity or relative signs suppress incompatible gains
and drift corrections. Applying/rolling back leakage or clearing/replacing drift
updates the active snapshot and invalidates an existing fit. These state changes
are preserved in the full file as `sensor_params_state`, so a later save does not
silently restore an earlier correction.

## Calculation contract

The current electrical predictor is:

```text
d_prev = VD - V_previous                  # after optional applied leakage model
tau    = Rb * Ctotal
E      = exp(-Tc / tau)
V_pred = VD - E * ((1 - c0 - c1*d_prev)*d_prev + qv)
e      = V_measured - V_pred
```

`Tc` is the **current visit's measured connected time**. The electrical VD is not
automatically replaced by the channel's observed quiet voltage. With a leakage
model, first transform `V_previous` to
`Vleak + (V_previous - Vleak)*lam`; a null `v_leak_v` means VD.

For `drift_correction.kind == "residual_bias"`, subtract `bias_v` from `e` before
converting charge. Otherwise use `e` directly:

```text
dQ = common_polarity * relative_polarity * Ctotal * corrected_e
```

For `kind == "charge_rate"`, then subtract
`q_rate_c_per_s * actual_time_since_previous_visit`. This saved charge rate already
includes both polarity signs; do not flip it again or also subtract a residual bias.
An absent correction means no **saved** unloaded correction, not proof of no drift.

Accumulate the signed contributions and calculate relative package force:

```text
F_group = sum(Q_channel - Q_channel_at_zero) / d33_eff_c_per_n
```

The first sample has no predecessor and contributes no innovation. Do not bridge
invalid samples, missing packets or restarts with a fabricated force increment.
The zero reference/integrator state belongs to the consumer's current measurement;
it is not an absolute force offset exported by CapTest. A null gain permits charge
analysis but does not permit a calibrated newton result. Do not substitute the
initial display gain or rectify channel contributions.

No ADC_Gui importer or force-calculation changes are included in this feature.
