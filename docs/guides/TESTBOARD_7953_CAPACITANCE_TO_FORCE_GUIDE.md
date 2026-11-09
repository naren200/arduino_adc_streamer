# TestBoard_7953: offline capacitance measurement, production sampling, and force increments

This guide connects the CapTest measurement to production sampling with Vmid between sensor visits. It uses offline calibration values, realtime ADC voltages, and known acquisition parameters. The circuit-induced voltage change must be accounted for before a voltage change is interpreted as newly generated piezoelectric charge.

`Delta F[n] = F(t[n]) - F(t[n-1])` refers to consecutive samples of the **same sensor**. It is a force increment in newtons, not a force derivative in N/s.

In an earlier discussion, “innovation” meant measured voltage minus predicted voltage. This guide calls that quantity the **voltage residual**, `e[n]`. It is an intermediate calculation, not a new measurement.

Reviewed on 5 November 2026 against the current CapTest fit and production acquisition code. The evidence includes the 3 November sensor measurements and the 4 November strip-swap, disconnected-strip, and PCB-routing analysis. The force reconstruction described here is a proposed calculation and validation procedure; the existing production force calculator does not yet implement the full CapTest switching model. A good capacitance fit is not a completed charge/force calibration.

## 1. How we measure and calculate the sensor offline

### 1.1 Circuit and capacitances

![Sensor-side and shared-node capacitances, with the bias resistor on MXO](images/testboard7953_force/circuit.png)

The drawing shows Vmid selected. The piezoelectric sensor generates charge when force changes. Its capacitance `Cs` is in parallel with the sensor-side parasitic capacitance `Cp_s`. Both remain attached to the sensor when the mux selects another input. Here `Cp_s` includes strip/flex capacitance and the board route from the sensor connector to that mux input. The shared mux-output capacitance `Cn` includes the MXO route to the op-amp and its input capacitance; it is connected to that sensor only while selected. This is a lumped model for a configuration in which coupling to other signals is negligible or adequately characterized.

```text
Coff   = Cs + Cp_s                  sensor-side capacitance while disconnected
Ctotal = Coff + Cn                 capacitance while connected
Cs     = Ctotal - Cn - Cp_s
```

```mermaid
flowchart LR
    S["Cs + strip/flex + board input-route capacitance<br/>Coff: stays with this sensor while deselected"] --> M["Mux switch"]
    M --> N["MXO route + op-amp input capacitance<br/>Cn: shared node reset by selecting Vmid"]
    N --> B["Buffer -> ADC"]
    D["Vmid source"] --> R["Rb"]
    R --> N
```

`Rb` connects MXO to the physical Vmid source. It therefore charges the selected sensor, not every disconnected sensor continuously. The 270 pF capacitor after the buffer is not directly added to `Ctotal`.

For voltage-mode charge conversion, cable and input capacitances participate in storing the generated charge. Consequently, `Cs` alone is not always the appropriate voltage-to-charge conversion capacitance. See [PCB Piezotronics: voltage-mode signal conditioning](https://www.pcb.com/resources/technical-information/signal-conditioning-basics).

### 1.2 Offline inputs and outputs

Store a calibration record for each ADC/channel and acquisition configuration:

| Quantity | Units | How obtained |
| --- | --- | --- |
| `Rb` | ohm | Board resistor value; preferably measured rather than nominal |
| `Ctotal` | F | CapTest two-connected-time fit |
| `Cblank` / `C_net` | F | Matching disconnected-strip board baseline / loaded total minus that baseline; for identifying added capacitance, not a replacement for `Ctotal` or `Coff` |
| `c0` | dimensionless | Effective connection fraction at Vmid, from the same fit |
| `c1` | 1/V | Voltage dependence of the connection fraction |
| `qv` | V | Constant offset in the fitted voltage recurrence |
| `Coff` | F | Independent sensor-side capacitance characterization, if using the analytical charge calculation in section 3.3 |
| `KQ` | C/V | Offline electrical charge-response calibration, if using section 3.2; may be a table versus voltage and timing |
| `Sq` or `d33_eff` | C/N | Effective signed charge sensitivity of the mounted sensor |
| Off-state leakage law | e.g. ohm and reference voltage | Offline discharge measurement, if appreciable over a production revisit interval |
| Acquisition conditions | — | SPI engine/clock, connected time, sensor repeats, Vmid frames, scan order/rate, voltage range, wiring, and ADC range |

`KQ` is an effective conversion coefficient, not a physical capacitor. Although C/V has the units of capacitance, it includes the attenuation of the read operation. It is measured offline; it does not require a realtime charge or force measurement.

The material coefficient `d33` can be used as `Sq` only when the sensor orientation and mechanical load transfer justify it. Otherwise measure the effective sensitivity in the assembled sensor. Charge sensitivity and its calibration are discussed in [PCB Piezotronics: force sensors](https://www.pcb.com/resources/technical-information/introduction-to-force-sensors). Choose the sign so that the desired positive force direction is consistent with the voltage/charge polarity.

### 1.3 CapTest sequence and calculation

Use CapTest firmware 1.3.0 or newer and its `alternate` method. The default repeats one sensor frame followed by one Vmid frame. Every second cycle stretches the sensor-connected frame, giving two connected times in one burst.

```mermaid
flowchart LR
    P["Park shared node on Vmid"] --> S["Sensor visit: short connected time"]
    S --> V1["Select Vmid: reset shared node"]
    V1 --> L["Sensor visit: long connected time"]
    L --> V2["Select Vmid: reset shared node"]
    V2 --> S
```

Before measurement, allow the sensor to discharge enough to provide useful voltage range. Three seconds was sufficient for the tested 2:7 sensor. A fixed wait is not proof of discharge: the first reading must be at least 0.3 V below `VD`, or CapTest marks `ALT_SMALL_RANGE` and excludes the burst. Check the Vmid resets and fit flags, retain the raw packets, and repeat the measurement.

For a connected sensor held at constant force, with no additional force-generated charge during the burst, define:

```text
U[k]    = ADC sensor voltage sampled near the end of visit k, before mux release
VD      = measured physical Vmid reference
d[k]    = VD - U[k]
Tc[k+1] = connected-time interval assigned to the next visit, from timestamps
```

The fitted recurrence is:

```text
d[k+1] = exp(-Tc[k+1]/tau) * ((1 - c(d[k]))*d[k] + qv)
c(d)   = c0 + c1*d
tau    = Rb*Ctotal
```

For each trial `tau`, the model is linear in `(1-c0)`, `-c1`, and `qv`. CapTest solves those coefficients by least squares, then chooses the `tau` with the smallest residual error. The two connected times separate resistor charging from the common connection effect:

```text
Ctotal       = tau/Rb
Cshare(d)    = Ctotal*(c0 + c1*d)
Cshare_at_VD = Ctotal*c0             d = 0
```

`qv` is in volts in this equation. Its equivalent fitted charge parameter is `Ctotal*qv` in coulombs. It is not an additional realtime input or a proven microscopic switch-injection charge.

The recorded three-burst results are:

| Channel | `Ctotal`, mean +/- SD | `Cshare` at `VD` | `Cshare` 1 V below `VD` |
| --- | --- | --- | --- |
| 1:0 | 73.3 +/- 0.1 pF | 6.7 pF | 11.4 pF |
| 2:5 | 73.4 +/- 0.1 pF | 6.8 pF | 11.4 pF |
| 2:10 | 135.2 +/- 0.4 pF | 6.5 pF | 11.3 pF |
| 2:7 | 257.5 +/- 0.3 pF | 6.8 pF | 12.0 pF |

These standard deviations describe repeatability. They do not include resistor, timing, model, or calibration errors. The 2:7 result comes from its own short/long-time measurements, not an assumed small-sensor calibration.

Retain all fitted coefficients, not just `Ctotal`. If only the derived share values are available:

```text
c0 = Cshare_at_VD / Ctotal
c1 = (Cshare_1V_below_VD - Cshare_at_VD) / (Ctotal * 1 V)
qv = q_cycle_pc * 1e-12 / Ctotal
```

The current analyzer stores the last two diagnostics in `Measurement.connect_info`; the standard result row is not a complete force-calibration record. Save the raw packets and explicitly retain/export the required per-channel parameters.

### 1.4 Measuring parasitics and validating the scale

`Cshare` describes charge transfer during a connection. **It is not a measurement of `Cn`, and must not be subtracted as though it were a parasitic capacitor.**

To estimate the sensor capacitance, remove only the sensor while preserving its wiring and measure a matching blank. Under the additive model and a validated capacitance scale:

```text
Cs = Ctotal_loaded - Ctotal_blank
```

If unplugging a strip also removes its flex/traces, that subtraction includes the removed wiring capacitance. Record precisely where the disconnection occurred. For the 4 November test, with the strips unplugged at the board connectors, distinguish:

```text
Cstrip        = sensor-side capacitance removed with the strip, excluding Cs
Cboard_input  = capacitance of the individual board input route and input parasitics

Ctotal_loaded = Cs + Cstrip + Cboard_input + Cn
Cblank_board  = Cboard_input + Cn
C_net         = Ctotal_loaded - Cblank_board = Cs + Cstrip

Coff          = Cs + Cstrip + Cboard_input = Ctotal_loaded - Cn
```

CapTest's current baseline fields `c_board_pf` and `c_net_pf` report the board baseline and this loaded-minus-baseline value. `C_net` is not automatically Cs, and **neither C_net nor Cblank_board is Coff**. Baseline subtraction must use the same channel, compatible measurement method, board configuration, and disconnection boundary. Repeat unstable measurements rather than averaging them into one baseline or sensor calibration.

A bare input may settle too quickly to leave the required voltage range. In that case replace the sensor with a known capacitor, such as a measured approximately 100 pF capacitor:

```text
Cpar_fixture = Ctotal_with_calibration_capacitor - Ccal
Cs           = Ctotal_loaded - Cpar_fixture
```

The second equation identifies Cs only if the calibration fixture preserves all of the sensor's non-sensor capacitances. A known capacitor placed directly at the board connector instead gives a board baseline and leaves strip/flex capacitance in the loaded-minus-baseline result.

Two known capacitor values should produce their known capacitance difference and a consistent blank estimate. This checks the measurement scale. Removing only the sensor identifies `Cn + Cp_s`; unplugging the whole strip identifies `Cn + Cboard_input`. Neither total separates its shared-node and individual-input contributions, so neither by itself identifies Coff.

#### 1.4.1 Measured board parasitics and PCB routing

The disconnected-strip run has 50 unflagged channel means, each using three bursts. Median reported within-channel capacitance SD is 0.14 pF; this does not include systematic uncertainty. Group summaries are:

| ADC / channel group | Median Cblank, pF | Median effective Cshare at VD, pF |
| --- | ---: | ---: |
| ADC1, 0-9 | 13.67 | 6.64 |
| ADC2, 0-9 | 12.70 | 6.55 |
| ADC2, 10-14 | 17.33 | 6.92 |
| ADC3, 0-9 | 17.81 | 9.73 |
| ADC4, 0-9 | 15.05 | 7.11 |
| ADC4, 10-14 | 17.95 | 7.50 |

Use individual channel values from the [blank results](../../CapTest/Results/TestBoard_7953/2026-11-04_switching_strips/no_strips_connected.json) for calculations, rather than these rounded group medians. The original approximately 6.8 pF Cshare result applies to the tested ADC1/ADC2 channels; it is not a universal ADC correction.

The [net-length CSV](../../CapTest/boards/TestBoard_7953-NetLengthDetails.csv) identifies longer sensor routes on ADC3/ADC4, back-layer input routes on channels 10-14 of ADC2/ADC4, and an especially long MXO3 route through the back layer. Blank capacitance follows the input-route lengths closely. With one common slope and an independent ADC intercept, the fit gives 0.1227 pF/mm and 0.204 pF RMS residual across 50 channels. The ADC intercepts combine fixed input-side, shared-node, and model contributions; they are not Cn measurements.

![Measured board blank versus total channel-plus-MXO route length](../reports/images/testboard7953_strip_swap/total_route_length.png)

A compact alternative using total length and one additional ADC3 term is:

```text
Cblank_est_pF = 7.000 + 0.11247 * (Linput_mm + LMXO_mm) + 0.6928 * I_ADC3

I_ADC3 = 1 on ADC3, otherwise 0
RMS residual = 0.294 pF
```

The extra ADC3 term is equivalent to weighting the full MXO3 length by 1.196. This is an empirical board-baseline fit. It does not identify a physical back-layer capacitance ratio, Cn, or a new mux-switching coefficient. Use the measured blanks when available. Do not add the length estimate or multiply by 1.196 again after calibrating the actual loaded path: its effects are already present in that path's measured parameters.

The corresponding-channel ADC3-minus-ADC1 blank difference is 4.16 pF. Subtracting individual blanks reduces the repeat-b median signed strip difference from +4.48 to +0.58 pF for strip F and from +4.03 to -0.04 pF for strip O. This supports the board contribution. The approximately doubled capacitance group remains after subtraction: strip F's second-half channel 5 gives C_net of 60.65 / 60.54 pF on ADC2 / ADC4, while channel 10 gives 118.48 / 118.25 pF.

Channel 11 still has a fitted repeating-cycle voltage below its neighbors without either strip attached, despite an ordinary route length and an unflagged capacitance fit. A low residual therefore does not establish a correct physical force response. Save raw transitions and validate the actual production path, particularly for channel 11 and the unstable loaded channels. Full evidence, per-channel blanks, fit definitions, and exceptions are in the [strip-swap and parasitics report](../reports/TestBoard_7953_strip_swap_2026-11-04.md).

### 1.5 Offline calibration for charge and force

The force calculation needs the voltage response to newly generated charge as well as the no-additional-charge recurrence. CapTest alone establishes the latter.

The blank and routing measurements explain part of that circuit response but do not complete the charge-gain calibration. For example, sensor voltage may change between the ADC sample and mux release because of switch-off injection or continued resistor charging. An observed sample-to-sample recurrence alone does not uniquely identify the voltage left on the isolated sensor or its response to newly injected charge. Section 3.3 requires that physical relationship to be verified; otherwise use electrical or force calibration with the final loaded path.

For the electrical calibration route, use a known small charge input at the sensor side, with the final wiring and production sampling sequence. For example, a calibrated current pulse gives `Qcal = integral(Ical dt)`. Compare the ADC reading with its predicted reading for the same preceding state:

```text
e_cal = U_with_known_charge - U_predicted_without_that_charge
KQ    = Qcal/e_cal
```

Repeat for both polarities and the operating voltage/timing range. The injection timing must represent the intended force-change model: for charge accumulated mostly while disconnected, inject during that interval; for an assumed nearly constant charge-generation rate over a revisit interval, use that profile. Keep any calibration-fixture loading in the final configuration or account for its removal.

Alternatively, calibrate the full voltage-residual-to-force coefficient using a known force increment: `KF = DeltaF_cal/e_cal` in N/V. Then section 3 uses `DeltaF = KF*e` directly. This includes mounting effects and avoids separately identifying electrical charge gain and `Sq`.

Neither `KQ` nor `KF` has been established by the capacitance results in the table. If the analytical assumptions in section 3.3 are independently verified, that route can instead calculate the charge scale from capacitances and the switching model.

## 2. How we sample in realtime production

### 2.1 Match the calibration sequence

For the one-sensor-frame calibration above, use manual sequencing, one retained sensor frame per visit, and Vmid between sensor channels:

```text
Sensor A -> Vmid -> Sensor B -> Vmid -> Sensor C -> Vmid -> ... -> Sensor A
```

These are selections on one ADC. Scanning another ADC does not reset this ADC's MXO.

The updated production firmware inserts one Vmid command between sensor routes on each ADC. It keeps two final pipeline-flush commands per ADC scan to retrieve the last sensor result and park the ADC. For repeat 1 and channels A, B, C, the command sequence is `A, Vmid, B, Vmid, C, Vmid, Vmid`; the final pair is distinct from the single reset between sensor routes.

In production firmware, configure the corresponding settings explicitly while stopped: `adcseq manual*`, `channelrepeat 1*`, and `vmid true*`. The checked firmware defaults are three repeats and Vmid-between-channels disabled; do not assume startup matches this guide. Confirm the effective settings in status. Production retains the last sensor result when repeats are enabled, rather than averaging those results.

Using three sensor frames requires calibration or a validated model for three frames. The same Vmid count alone does not make that sequence equivalent to one sensor frame. Keep SPI engine, clock, connected-time convention, and wiring consistent; verify a timing change before reusing the connection coefficients.

![Production visits, Vmid resets, and the distinction between connected and revisit times](images/testboard7953_force/production_timing.png)

The Vmid visit restores the **shared node**. Sensor A remains isolated while B, C, and Vmid are selected. Force-generated charge can accumulate on A during that interval; `Rb` does not continuously discharge A through MXO.

### 2.2 Respect the command pipeline and sampling instant

The ADS7953 samples at CS falling and selects the next mux channel later in the frame, on the second SCLK falling edge. A command's sensor result therefore appears two command frames later in this firmware. See the [ADS7953 datasheet](https://www.ti.com/lit/gpn/ads7953), section 8.1 and Figure 1.

For an ADC initially parked on Vmid, the logical sequence is:

| Frame | Command sent | Channel selected later in this frame | Conversion returned at this frame |
| --- | --- | --- | --- |
| 0 | A | Vmid | Vmid |
| 1 | Vmid | A | Vmid |
| 2 | B | Vmid | A |
| 3 | Vmid | B | Vmid |
| 4 | C | Vmid | B |

Use the channel tags and firmware pipeline bookkeeping to associate each voltage with the sensor and its acquisition time. USB arrival time is not the ADC sampling time.

The scope also shows a disturbance starting at CS falling. The timing alone does not prove the ADC reads its settled top or the sensor's undisturbed voltage. The empirical transition model and electrical gain calibration must use the same sampling phase. The analytical route in section 3.3 additionally requires the sensor-state/readout relationship to be verified.

### 2.3 Realtime values and timing parameters

| Input | Source and use |
| --- | --- |
| `U[n-1]`, `U[n]` | Consecutive valid ADC voltages for the same channel |
| `VD` | Actual Vmid-reference voltage for that ADC; obtain offline before capture if stable, or monitor in realtime when available |
| `Tc[n]` | Effective connected time for the current visit, using the same convention as the CapTest fit |
| `Tvisit[n]` | Interval between successive retained samples of that sensor |
| Calibration record | Per-channel offline quantities from section 1 |

For a fixed schedule, `Tvisit = 1/fs_channel`. SPI clock frequency, total conversion rate, and per-sensor sample rate are different quantities. `Tc` is not `Tvisit`; do not use an entire scan interval as the exposure to `Rb`.

The [November 4 production speed test](../testing/benchmarks/testboard-7953/TESTBOARD_7953_SINGLE_VMID_RESULTS.md) measured median per-channel periods of 104 us at a requested 20 MHz SPI clock and 90 us at 30 MHz. Those results use all 50 channels across both arrays, LPSPI, manual sequencing, repeat 1 and one Vmid reset between sensor routes. They characterize the revisit interval for that configuration, not `Tc` or analog calibration accuracy.

The [ADS7953 datasheet](https://www.ti.com/lit/gpn/ads7953) specifies a 20 MHz serial interface. The 30 MHz result is an above-specification throughput experiment, not evidence of a validated production analog response.

The CapTest fit obtains its connected-time interval from frame timestamps. For production use, measure/derive the equivalent per-channel interval for the actual acquisition engine and schedule. In particular, do not substitute `16/f_SPI` without its software and inter-frame overhead. If production does not export frame timestamps, use offline timing characterization for a fixed configuration; arbitrary changing timing requires additional metadata.

The current production code performs Vmid conversions but normally stores only the sensor results in the outgoing sample array. It does not automatically provide each Vmid conversion to host processing. Use a verified stable `VD` calibration, or add explicit reference telemetry before depending on realtime Vmid values. A quiet sensor baseline may sit below `VD`; it is not interchangeable with the reference used to fit `c0`, `c1`, and `qv`.

Convert ADC codes with the actual ADC range. With 12-bit conversion and range 0..VREF:

```text
U = code * VREF/4096
```

For the 0..2*VREF range, use `2*VREF/4096`. If the input data is already in volts, do not convert it again.

## 3. How we calculate Delta F between samples

### 3.1 Predict the circuit-induced change

Assume `VD` and the acquisition configuration are stable over the interval, and that sensor-side leakage while disconnected is negligible. Use the **loaded** Ctotal, c0, c1, and qv for the actual ADC/channel and production sampling configuration. The voltage predicted with **no further change in force** is calculated below.

Use three distinct names:

- `V_previous`: the actual ADC voltage from the preceding visit to this sensor.
- `V_measured`: the actual ADC voltage from the current visit, including any response to a force change.
- `V_expected_no_force_change`: the calculated current voltage assuming the force has not changed since the preceding visit.

```text
d_prev = VD - V_previous
E      = exp(-Tc / (Rb*Ctotal))
d_pred = E * ((1 - c0 - c1*d_prev)*d_prev + qv)
V_expected_no_force_change = VD - d_pred
e = V_measured - V_expected_no_force_change
```

`V_measured` always means the actual measurement. Only `V_expected_no_force_change` assumes no further force change. The residual `e` is always measured voltage minus that calculated expectation.

For example, if the preceding reading was 1.200 V, the circuit model predicts 1.210 V with unchanged force, and the actual current reading is 1.215 V, then `e = 1.215 - 1.210 = 0.005 V`. The model explains 10 mV of the measured 15 mV rise; 5 mV remains for charge reconstruction, subject to noise and model error.

```mermaid
flowchart LR
    P["Previous voltage of this sensor"] --> M["Offline circuit model + current connected time"]
    M --> V["Predicted voltage: no further force change"]
    A["Current ADC voltage"] --> R["Voltage residual = measured - predicted"]
    V --> R
    R --> Q["Offline charge-response correction"]
    Q --> DQ["Generated charge increment: Delta Q"]
    DQ --> S["Divide by signed charge sensitivity Sq"]
    S --> DF["Force increment: Delta F"]
```

A held force can leave a voltage that continues to relax. That relaxation should follow `V_expected_no_force_change` and yield approximately zero `Delta F`. The calculation estimates a change of force, rather than declaring every electrical voltage change a force change.

#### 3.1.1 How parasitics affect the force calculation

Yes, the parasitics affect the charge-to-voltage response, but they do not introduce an additional force source or require a second correction after the actual loaded path has been calibrated:

| Calculation | Capacitance/calibration to use | Role of the blank analysis |
| --- | --- | --- |
| Predict no additional force charge | Loaded Ctotal in `E`, plus loaded c0, c1, qv | Explains path differences; do not substitute C_net for Ctotal |
| Empirically convert residual to charge | KQ calibrated with the sensor, strip, board route, and production sequence | Included in that gain; no additional blanket parasitic subtraction |
| Analytically convert residual to charge | Coff and a verified read map, section 3.3 | Blank total does not separate Cn and Cboard_input, so it does not supply Coff |
| Identify sensor capacitance Cs | Loaded total minus a matching sensor-free baseline | Board-only blank leaves strip capacitance in C_net |

In particular, board input-route capacitance is part of Coff and stores generated charge while the sensor is deselected. Removing it numerically from the voltage-to-charge conversion would omit charge stored there. Shared MXO capacitance reconnects each visit and affects sharing and resistor relaxation. Its role is included in the loaded transition and gain; Cshare is not an independently measured Cn.

In the verified ideal-sharing limit described in section 3.3, the charge formula is `DeltaQ = Ctotal_loaded*e/E`. It uses the loaded total even though Cs is smaller. For example, strip F ADC2:5 in repeat b has Ctotal = 73.52 pF and C_net = 60.65 pF. Using the latter merely as a replacement capacitance scale would give 60.65/73.52 = 0.825 of the former result, holding the other factors fixed. This illustrates why a sensor-capacitance subtraction must not automatically be applied to force gain. The ideal-sharing formula itself still needs validation on this board.

Moving a strip between ADC connectors changes the board path. Reuse its identity and independently established material/mechanical sensitivity if its mounting and load transfer are unchanged, but use electrical transition and gain calibration appropriate to the new path. A route-length factor is not a force multiplier.

### 3.2 Calculate charge using the offline electrical calibration

For small increments and an offline `KQ` calibrated for the relevant voltage, visit timing, and charge-generation profile:

```text
DeltaQ_C  = KQ_C_per_V * e_V
DeltaF_N  = DeltaQ_C / Sq_C_per_N

Sq_C_per_N = d33_eff_pC_per_N * 1e-12
```

Equivalently, use `DeltaF_N = KF_N_per_V*e_V` if the complete force response was calibrated directly. Do not also divide that result by `d33`.

An illustrative calculation, not a measured force calibration of this board:

```text
Offline charge-response coefficient KQ = 270e-12 C/V
Effective sensitivity Sq              = 600e-12 C/N
Predicted sensor voltage             = 1.240 V
Measured sensor voltage              = 1.250 V

e      = 0.010 V
DeltaQ = 270e-12 * 0.010 = 2.70e-12 C = 2.70 pC
DeltaF = 2.70/600 = 0.00450 N
```

The 270 pF-equivalent coefficient in this example is hypothetical; it is not the buffer's 270 pF capacitor. A voltage residual of zero must produce a charge increment and force increment of zero.

### 3.3 Analytical calculation from measured capacitances and model parameters

This route avoids an independently measured `KQ`, but adds physical assumptions that CapTest alone has not established:

- The sample represents the voltage state left on the sensor **after mux release**, including any switch-off effect, or a verified state/readout correction has been applied and the transition model expressed in that corrected voltage. Sampling immediately before release does not establish this equality.
- The generated charge accumulates predominantly while the sensor is disconnected; force change during the short read window is negligible.
- `Coff = Cs + Cp_s` is independently known, and off-state leakage is negligible over `Tvisit`.
- The switching law remains valid under the force signal, including its polarity and amplitude.

Under these assumptions, generated charge raises the pre-read voltage by `DeltaQ/Coff`. Linearizing the calibrated transition around `V_previous` gives:

```text
a_read = E * (1 - c0 - 2*c1*d_prev)
DeltaQ = Coff * e/a_read
DeltaF = Coff * e/(a_read*Sq)
```

`a_read` is the voltage derivative of this transition under the assumptions above. It accounts for attenuation of the new charge by the connection operation and resistor charging. It must be positive and well away from zero in the calibrated range. If correcting the sensor state changes the voltage coordinate, calculate the residual and derivative in that corrected coordinate, or include the observation gain explicitly; do not combine a corrected state with an uncorrected ADC residual. Differentiating the measured no-force recurrence alone does not prove this is the gain seen by newly generated charge.

Multiplying `e` by `Cs` or `Ctotal` alone omits the read correction. In the special case of pure instantaneous sharing, `c0 = Cn/Ctotal`, `c1 = 0`, `qv = 0`, and `a_read = E*Coff/Ctotal`, reducing the charge formula to `DeltaQ = Ctotal*e/E`. The observed switching effects mean that special case is not automatically valid here. In particular, Coff is not `Ctotal - Cshare`.

For larger increments within a monotonic calibrated voltage range, invert the same lumped model instead of linearizing it:

```text
d_now = VD - V_measured
A     = 1 - c0
B     = d_now/E - qv

If c1 is approximately zero:
    d_before_read = B/A
Otherwise:
    discriminant  = A*A - 4*c1*B
    d_before_read = 2*B / (A + sqrt(discriminant))

DeltaQ = Coff * (d_prev - d_before_read)
DeltaF = DeltaQ/Sq
```

Use this displayed root only for the calibrated monotonic branch continuous with the linear limit for `A > 0`. For the approximately zero-c1 case require `A` to be safely nonzero as well. Require a nonnegative discriminant and a pre-read voltage inside the calibrated monotonic range; do not silently clamp an invalid solution. This inversion is exact for the stated lumped-input model, not for an arbitrary unknown force waveform during the visit.

### 3.4 Leakage, bandwidth, and validity

If disconnected-state leakage matters, characterize it offline. For a verified linear leakage path from the sensor side to `Vleak`:

```text
tau_off             = Roff*Coff
V_before_no_change  = Vleak + (V_after_previous_visit - Vleak)*exp(-Toff/tau_off)
```

Use that no-additional-force state in the read model rather than the uncorrected previous state. `Rb` is not `Roff`. Use the actual disconnected duration and account for any delay between sampling and release. If the ADC reading is not the stored sensor voltage, use the verified observation/state conversion as well. A production gain calibration must include the same leakage behavior.

The measured residual is a time-weighted response to generated charge. If substantial force changes occur during the connected window or charge leaks significantly between visits, two endpoint voltages do not uniquely determine the unweighted charge generated at unknown times. Use an offline calibrated input response for an explicit waveform assumption, such as approximately constant charge-generation rate over one interval, and validate at the intended force bandwidth. Sampling faster than the force dynamics and keeping the connected window short helps make the disconnected-charge approximation useful.

Apply the calculation only when:

1. The channel, reference, voltage range, and sampling phase match the calibration.
2. Vmid resets succeed and the ADC is not clipped.
3. The sensor-frame count and connected timing match the model; a repeat-three production visit is not a repeat-one visit.
4. Reference drift, off-state leakage, temperature, and cross-channel coupling are within the calibrated limits.
5. The force signal lies in the calibrated voltage range. Charging from below Vmid does not validate extrapolation above Vmid.

Maintain `V_previous` and timing state separately for each sensor. The first valid sample initializes that state and has no preceding force increment. On a missing sample, timing change, or failed reset, flag/reinitialize or use a validated multi-visit model; do not treat the next sample as though only one normal visit occurred.

Calculate the voltage residual before applying display thresholds. If a noise threshold is needed, characterize the residual noise offline; thresholding or zeroing the raw voltage before this correction can discard part of a real loading or unloading signal.

An implementation outline for the electrical-calibration route is:

```python
# All capacitances in F, resistances in ohm, times in s, voltages in V.
# The offline record supplies loaded Ctotal, c0, c1, qv, KQ, and signed Sq.
# This outline assumes negligible off-state leakage and fixed VD.
# Initialize each channel on its first sample; do not calculate DeltaF then.
VD = reference_voltage[adc_of[channel]]
Tc = connected_time[channel]
Tvisit = sample_time - previous_sample_time[channel]
d_prev = VD - previous_voltage[channel]
E = exp(-Tc / (Rb[channel] * Ctotal[channel]))
predicted_voltage = VD - E * (
    (1.0 - c0[channel] - c1[channel] * d_prev) * d_prev + qv[channel]
)
voltage_residual = measured_voltage - predicted_voltage
delta_charge = charge_response_coefficient(channel, d_prev, Tc, Tvisit) * voltage_residual
delta_force = delta_charge / charge_sensitivity[channel]
previous_voltage[channel] = measured_voltage
previous_sample_time[channel] = sample_time
```

This is a proposed reconstruction step. The current [force calculator](../../data_processing/pzt_force_calculation.py) corrects an RC decay and optional mux exposure, but does not yet apply this nonlinear switching model. Its event resets and display behavior are separate from the physical charge calculation.

To obtain relative force, accumulate `F[n] = F[n-1] + DeltaF[n]` from a known initial force. This does not recover an unknown initial static force. Keep any zeroing or display reset explicit; a quiet piezoelectric voltage alone does not establish that a held force was released.

Validate with a known charge input and/or reference force waveform using the final production scan. Check constant-force intervals, loading, unloading, both signal polarities, and more than one scan rate. Average repeat calibrations from the same method and compatible conditions; compare different methods separately.

Detailed measurement evidence: [capacitance report, step 5](../reports/TestBoard_7953_capacitance_report.md#step-5-c_total-and-c_s-with-vmid-between-samples-one-vmid-frame). Acquisition implementation: [production sensor/Vmid sequence](../../Arduino_Sketches/TestBoard_7953/src/PztController.cpp) and [CapTest alternate fit](../../CapTest/captest/capfit.py). Workflow and protocol: [CapTest user guide](TESTBOARD_7953_CAPTEST_USER_GUIDE.md) and [CapTest firmware architecture](../architecture/firmware/TESTBOARD_7953_CAPTEST.md).


## 4. Implemented in CapTest: export and weight calibration

CapTest implements the capacitance-proxy path of section 3.1 (not the analytical Coff route of
3.3 and not a KQ calibration): `Q_proxy = sum Ctotal*e`, `F = (Q_proxy - Q_proxy(t0)) / d33_eff`,
with `charge_conversion_model=capacitance_proxy_v1` stored in every record. What it adds to the
model above:

- **Inputs** come from a `captest-force-inputs-1` file: loaded `Ctotal`, `tau`, `VD`, `c0`, `c1`,
  `qv`, the fitted distance range and an acquisition signature (alternate-method timing). The
  capture schedule must match that signature (SPI clock, gap, first-clock delay) or the user must
  accept the mismatch explicitly for provisional exploration; acknowledgement cannot validate
  it. The revisit interval, route set and lag legitimately differ and are recorded separately.
  Complete electrical coverage is not sufficient: HIGH_RESIDUAL and invalid measured
  coefficients block ordinary force readiness. Explicit donor/blank recovery may create an
  estimated revision with retained provenance; see the user guide.
- **Bias is the dominant error.** Any constant bias in `e` is accumulated into a force ramp
  (about `Ctotal*bias/Tvisit` per second per route; the bias scales with the visit rate and the
  force step does not). CapTest therefore fits a per-route bias on the unloaded baseline, freezes
  it and records it; it never adapts during a transition or hold, and no reset, deadband or decay
  is applied to calibration data. The scan always covers all 50 routes and has an added per-sweep
  lag so the sampling rate can be varied and compared.
- **Events are fitted as a step plus a ramp** in the accumulated proxy to estimate gain.
  The plain endpoint change, pre-only estimate and magnitude of the drift correction are
  retained too. A ramp fit does not prove a ramp is non-force drift. The gain is a common
  signed slope over known masses, directions and repeats; the intercept, direction mismatch,
  hold drift and a held-out repeat are reported. Deployed force errors use plain endpoint
  charge divided by the frozen training gain, without subtracting the event intercept.
- **Leakage** is an optional *effective* off-state correction fitted on constant-force windows
  and reported as "not identified" unless the windows differ enough in operating point to separate
  it from bias; a hold slope never measures `Roff`. Evaluation only proposes the correction.
  Explicit apply/rollback replays original recordings, re-estimates then freezes baseline bias,
  and invalidates the previous gain and validation. Live and offline processing use that same model.
- **Range**: ADC headroom or a voltage too far below the fitted range rejects the trial and
  blocks heavier masses until a successful retry. Above-VD voltage is recorded as provisional
  extrapolation with its extent/duration; no default VD+0.25 V ceiling applies. ADC headroom,
  electrical coverage and independently validated force range remain separate. An observed
  training excursion does not itself establish a validated domain.
- **Evidence and status**: unique raw/NPZ files and atomic per-trial sidecars retain marks,
  model hashes, corrections and analyses even for cancelled/rejected attempts. Offline replay
  reproduces the saved decision. Not-fitted and provisional records can be saved; only a gain
  with passed known-weight held-out errors, explicit limits and acceptable quality flags is
  embedded as validated. Workflow-only finger presses cannot supply a nominal known force.

The fitted value is an effective system sensitivity of the tested package, schedule and mounting.
It has not been validated on hardware; see the plan's acceptance section.
