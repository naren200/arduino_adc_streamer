"""In-app reference for the measurements, result columns and controls."""

from html import escape

from . import capfit


COLUMN_HELP = {
    "array": "Physical sensor array, numbered 1 and 2 on TestBoard_ADC124.",
    "mux": "Multiplexer within the array, numbered 1 through 4. Each MUX has its own shared output node and bias resistor.",
    "input": "External MUX input address. Inputs 0 through 5 are routed on every MUX; input 6 is routed only on MUX4. Address 7 is VMID, not a sensor result.",
    "adc": "Physical ADS7953 converter, numbered 1 through 4.",
    "channel": "Routed sensor input of the selected ADS7953 converter, numbered from 0.",
    "fit_model": "Model actually used for the accepted bursts: 1exp, 2exp, or mixed when different bursts used different models. Requesting two exponentials can still select one when the second component is not justified.",
    "tau_us": "Fitted RC time constant in microseconds. A single exponential completes about 63% of its recovery after one tau. For two exponentials this is tau_slow. It is fitted to the voltage-versus-time samples, not measured by a threshold crossing.",
    "tau_fast_us": "Fast time constant of the selected two-exponential fit, in microseconds. A dash means that component was not used.",
    "fast_pct": "100 * A_fast / (A_fast + A_slow): the fraction of the fitted voltage step assigned to the fast component. This is an amplitude fraction, not a capacitance fraction.",
    "c_total_pf": "C_total = tau / R_bias, converted from farads to pF by multiplying by 10^12. Includes the shared MUX output node and the connected sensor side. With two exponentials this uses the slow time constant and is an effective model-dependent capacitance.",
    "c_total_tol_pf": "C_total * R_bias tolerance (entered percent / 100). This is only the uncertainty contribution from the assumed resistor tolerance; it excludes fitting, timing, calibration and model errors. It is not the standard deviation across bursts.",
    "c_total_shift_pct": "100 * (C_total / C_total_1exp - 1). Compares the selected model with the single-exponential result. It is zero when the single exponential is used.",
    "c_total_1exp_pf": "tau_1exp / R_bias in pF, from the single-exponential fit to the same retained samples.",
    "c_total_2exp_pf": "tau_slow / R_bias in pF, from the valid two-exponential fit to the same retained samples, even if that fit was not selected. A dash means no valid two-exponential fit.",
    "ratio": "C_D / C_total = (V0 - V_s0) / (V_D - V_s0). V0 is extrapolated from the selected fit; V_D is the measured pre-connect baseline; V_s0 is the assumed sensor start voltage in Settings. Undefined when the baseline is missing or the denominator is effectively zero. Only physically meaningful when V_s0 and the charge-sharing model are valid.",
    "ratio_1exp": "(V0_1exp - V_s0) / (V_D - V_s0), using the single-exponential fit. Uses the same baseline and start-voltage assumption as the selected ratio.",
    "ratio_2exp": "(V0_2exp - V_s0) / (V_D - V_s0), using the valid two-exponential fit. A dash means that value is unavailable.",
    "c_d_pf": "C_D = ratio * C_total. Estimates capacitance of the shared MUX output node. It inherits the start-voltage and model assumptions of the ratio.",
    "c_sensor_pf": "C sensor = C_total - C_D. This is the sensor-side capacitance, including sensor, traces and MUX input parasitics; it is not the bare PZT capacitance.",
    "c_pzt_pf": "C_PZT = C sensor - C parasitic (the Settings value in pF). This is an estimate after assumed parasitics are removed. Negative or implausible values can indicate invalid assumptions or a poor fit.",
    "leak_na": "Leak = (V_D - Vinf) / R_bias * 10^9 nA. Positive means the fitted final level is below the baseline; negative means above. It is an inferred net current, not a direct leakage-current measurement.",
    "v0_v": "Fitted node voltage at the instant of connection, t = 0. It is extrapolated from retained samples after the discarded points; it is not necessarily the first ADC voltage.",
    "vs0_est_v": "Cross-channel estimate of sensor start voltage. At least five usable channels sharing one output node are needed (ADC124: same array and MUX; 7953: same ADC). K = median(V0 * tau), expected V0 = K / tau, r = clamp(expected V0 / mean V_D, 0, 0.95), then Vs0 est = (V0 - r * mean V_D) / (1 - r). Assumes a common output capacitance, resistor and mostly zero-start sensors. This diagnostic does not replace the V_s0 setting; negative estimates are possible.",
    "vinf_v": "Asymptotic node voltage predicted by the selected fit as time tends to infinity. Compared with the measured V_D baseline to infer leakage.",
    "residual_rms_lsb": "sqrt(mean((measured voltage - fitted voltage)^2)) / LSB over retained samples, with LSB = VREF / 4096. Smaller values mean the selected model follows the recorded curve more closely.",
    "amplitude_lsb": "abs(Vinf - V0) / LSB: the fitted recovery step in ADC counts. A small step makes tau and charge-sharing estimates sensitive to noise.",
    "bursts_used": "Number of usable fitted bursts contributing to this row, which can be less than the requested Bursts per channel. Zero produces a failed/red row.",
    "since_previous_s": "Elapsed seconds since the previous tracked connection before this measurement's first burst. ADC124 also tracks connections caused by reading neighbouring MUXes at the same address. A dash means no earlier connection was tracked.",
    "fresh": "Yes means no earlier connection was tracked in this measurement session since the last detected board boot. It is software history, not proof that the sensor is discharged; connections before this session are not known.",
    "flags": "Quality warnings from the bursts, plus aggregate/start-voltage checks. Amber rows contain flags; red rows have no usable fitted bursts; green rows have none of the implemented warnings. Green is not independent validation of wiring or calibration.",
}


SETTING_HELP = {
    "n_samples": "Post-connect samples requested per channel. Zero/auto sizes the window for about 6 * R_bias * Auto-N guess C. Increase N for SHORT_WINDOW or a slow recovery. ADC124 shares a 4096-frame burst budget across all channels read, including pre-connect frames, so requests are clamped to the available budget.",
    "c_guess_pf": "Capacitance guess in pF used only for automatic sample sizing. It does not constrain the fitted capacitance. Increase it if auto mode records too little of the recovery. Ignored when Samples per burst is explicitly set.",
    "park_us": "Time to hold the MUX output node at VMID before a burst, in microseconds. This establishes the output-node baseline; it does not ground or discharge the disconnected sensor.",
    "pre_samples": "ADC124 frames acquired before switching away from VMID. Rounded up to complete round-robin cycles across the converted inputs. More frames permit more baseline readings but consume part of the 4096-frame budget.",
    "gap_ns": "7953 CS-high interval between conversion frames, in nanoseconds. Changes physical frame period and acquisition time. The firmware checks the minimum 1000 ns frame period and 325 ns acquisition time.",
    "first_clock_delay_ns": "7953 assumed delay from CS falling to the first SCLK edge. The connection-time model adds 1.5 SCLK periods to this delay. This assumption changes the time origin and fitted V0; check it against the hardware timing.",
    "spi_clock_hz": "Requested serial clock frequency in Hz, limited to the selected board's range. It changes the sample spacing and automatic window size. A new burst is needed to measure at a different clock; changing settings cannot alter saved hardware data.",
    "hold_clocks": "ADC124 assumed sample/hold instant in SCLK periods after the start of a 16-clock frame. Sample time = reconstructed frame end - frame stamp latency - (16 - hold instant) / SPI frequency. This is a timing assumption to verify with equipment.",
    "enable_delay_ns": "ADC124 assumed analog MUX turn-on delay after the measured enable edge. Connection time = enable edge + this delay. It shifts t = 0 and the extrapolated V0, and therefore the capacitance ratio.",
    "pop_latency_ns": "ADC124 assumed delay from the real frame end to the firmware's result timestamp. Subtracted from reconstructed frame times when locating the sample/hold instant. This corrects the analysis time model, not the actual hardware latency.",
    "baseline_samples": "ADC124: average up to this many of the last valid pre-connect readings per converted input into V_D. If fewer exist, all available readings are used. No valid baseline produces NO_VMID_READING.",
    "per_address": "ADC124: one burst per array/address converts up to four MUX outputs round-robin. Unchecked means one burst per measured channel. Both choices physically connect the same address on all four MUXes of that array, including neighbours not read. This affects measurement planning, not the results-grid sort order.",
    "bursts": "Requested number of bursts per channel. Automatic acquisition runs passes across the selected channels and waits out the configured discharge interval when necessary. Usable per-burst results are averaged; M in the grid reports how many contributed.",
    "discharge_ms": "Minimum elapsed time between tracked sensor connections in automatic mode, in milliseconds. Manual measurement warns with TOO_SOON instead of blocking. This wait does not prove a zero start voltage: ADC124 sensors can retain charge for minutes. Use the discharge check to investigate drift.",
    "discard_k": "Ignore the first K post-connect samples of each channel before fitting, to reduce sensitivity to switching transients (ADC124 applies this after rejecting invalid codes). Excessive discarding can remove the fast part of the curve or leave FEW_SAMPLES. Re-analysis applies K to saved raw data.",
    "vref": "Voltage scale for the raw 12-bit codes: voltage = code * VREF / 4096. ADC124 has a fixed physical 3.3 V reference; editing this setting changes interpretation, not the board's electrical reference. VREF also sets the LSB used for residual and amplitude values.",
    "v_s0": "Assumed sensor voltage immediately before connection, used in (V0 - V_s0) / (V_D - V_s0). Zero is appropriate only for a known uncharged sensor. A power cycle or discharge wait does not guarantee zero. An incorrect value invalidates C_D and the separated sensor capacitances even when tau fits well.",
    "c_parasitic_pf": "Assumed input-pin/trace parasitic capacitance, subtracted from C sensor to estimate C_PZT. This does not change the fitted tau or C_total. Establish it from appropriate open-input or independent measurements.",
    "bias_tolerance": "Resistor tolerance entered as a percentage; 1% is stored as 0.01. The +/- tol column is C_total times that fraction. This is separate from the per-channel resistor value and does not account for all measurement uncertainties.",
    "method": "TestBoard_7953 acquisition method. Vmid between samples (the default; CapTest firmware 1.3.0, command capalt) connects the sensor for one frame, then returns the shared node to VMID for the set number of frames, repeated, as production does with VMID between channels. Every second cycle the sensor stays connected for the long-cycle gap. Each return to VMID resets the node to V_D, so the sensor's distance below V_D shrinks by a fixed ratio per cycle, which depends on the connected time through tau and not on the sensor's start voltage. Two connected times separate tau from C_share. One connection per burst (capburst) is the earlier exponential-fit method. Saved bursts are re-analysed by the method they were taken with.",
    "vmid_frames": "Vmid between samples: VMID frames after each sensor frame. On the test board one frame returned the node to within 0.6 mV of V_D, the same result as two.",
    "gap_long_ns": "Vmid between samples: CS-high gap of the connected frame in every second cycle, in nanoseconds. tau comes from the difference between the two connected times, so it has to be a fair part of tau; much longer and a few long cycles take the sensor to VMID, leaving few cycles to fit. Automatic (0) uses 0.3 x R_bias x the Auto-N guess C, limited to 4-40 us.",
    "fit_model": "Choose one exponential, or request two with automatic fallback to one. Both models are fitted to the same retained samples; the selected model drives tau, V0, ratio and capacitance columns. Changing this control re-analyses recorded samples without reconnecting a sensor.",
}


FLAG_HELP = {
    capfit.LOW_AMPLITUDE: f"Fitted step is below {capfit.LOW_AMPLITUDE_LSB:g} LSB. Noise can dominate the recovery estimate. Check the start voltage and connection; a larger window alone does not create a larger step.",
    capfit.HIGH_RESIDUAL: f"Selected-fit RMS residual exceeds {capfit.HIGH_RESIDUAL_LSB:g} LSB. Inspect the curve and residuals for noise, drift or an inadequate model; compare one and two exponentials.",
    capfit.PERIODIC_RESIDUAL: f"A residual spectral line has a period of 2 to {capfit.FRAME_PATTERN_MAX_PERIOD:g} retained-sample intervals, at least 1 LSB amplitude and 20 times the local spectral floor (requires at least 64 samples). Investigate repeating interference or conversion/switching effects; the flag alone does not identify the cause.",
    capfit.TOO_SOON: "The previous tracked connection was less than Discharge time ago, allowing 0.01 ms comparison tolerance. Automatic mode waits; manual mode can produce this warning. A neighbouring MUX's connection counts on ADC124.",
    capfit.VS0_SUSPECT: "The assumed sensor start voltage is suspect because the channel was connected too recently or its fitted V0 varies across repeated bursts. Check actual discharge behaviour before trusting the ratio or separated capacitances.",
    capfit.VS0_HIGH: f"Cross-channel Vs0 est exceeds {capfit.VS0_THRESHOLD_V:g} V. The estimate needs at least {capfit.VS0_MIN_CHANNELS} usable channels sharing an output node and assumes mostly zero-start sensors. Treat this as a diagnostic warning, not a direct start-voltage measurement.",
    capfit.V0_INCONSISTENT: "Across usable repeated bursts, sample standard deviation of V0 / abs(mean V0) exceeds 3%. Also adds VS0_SUSPECT. Investigate changing sensor charge, motion or unstable connection conditions.",
    capfit.VINF_OFFSET: f"The magnitude of the inferred net leakage exceeds {capfit.MAX_LEAK_NA:g} nA: abs((V_D - Vinf) / R_bias). Check the baseline, leakage paths and extrapolated final voltage.",
    capfit.NO_VMID: "No valid pre-connect VMID baseline was available. A recovery time constant may still fit, but ratios and derived C_D/sensor capacitances are unavailable. On ADC124, check Pre-connect frames and baseline data; on 7953, check the VMID-tagged responses.",
    capfit.FEW_SAMPLES: "Fewer than 16 usable samples remain after invalid-data rejection and Discard first K. The burst is excluded from aggregate means. Increase samples, reduce K appropriately, and inspect data errors.",
    capfit.FIT_FAILED: "No valid positive finite single-exponential time constant could be fitted, or acquisition failed before analysis. The burst is excluded from aggregate means. Inspect the Log and raw waveform; fix the acquisition or signal before interpreting capacitance.",
    capfit.INCOMPLETE: "Burst was truncated, aborted, had a transfer error, lacked a measured connection, or did not match the expected channel/data layout. The burst is excluded from aggregate means. Inspect the Log, connection and firmware status before repeating.",
    capfit.PERIOD_SHORT: "Frame timing failed a board-specific check. ADC124: firmware reports a short period, or reconstructed period is below 98% of 16 / SPI frequency. 7953: firmware reports it, or minimum result interval is below 1000 ns. Check clock and timing settings.",
    capfit.SHORT_WINDOW: "ADC124: the last retained sample is earlier than 4 * fitted tau after connection. The asymptote and time constant may be poorly constrained. Increase Samples per burst or Auto-N guess C; fewer inputs per burst allow more samples of one channel within the frame budget.",
    capfit.V0_OUT_OF_RANGE: "ADC124: fitted V0 is at or below -0.05 V, or at or above VREF + 0.05 V. The 50 mV margin allows small extrapolation errors. Check model fit, timing assumptions and reference voltage.",
    capfit.DATA_ERRORS: "ADC124 response contains non-zero bits above the 12-bit conversion code. Invalid readings are rejected. Inspect SPI integrity, clock and firmware; this does not detect swapped physical channels because ADC124 returns no channel tag.",
    capfit.CLOCK_OUT_OF_SPEC: "ADC124 burst clock is outside the board's 8 to 16 MHz specification range. Use a supported SPI clock and acquire again.",
    capfit.SWITCH_JITTER: "ADC124 firmware reports that the GPIO MUX switching sequence took more than 1000 ns. The edge is timestamped, but unusually slow switching warrants investigation of the timing and raw burst.",
    capfit.STAMP_GAP: "ADC124 firmware reports a result interval over three times the mean, or more than 10% of timestamps are rejected while reconstructing frame timing. Interrupts or stalls can disturb the assumed sampling schedule; inspect timing before trusting the fit.",
    capfit.TAG_MISMATCH: "7953 only: a retained response's channel tag differs from the requested input. Wrong-tag responses are rejected. Check channel selection and SPI sequencing; ADC124 has no channel-tag check.",
    capfit.ALT_SMALL_RANGE: f"Vmid between samples: the first sample was less than {capfit.ALT_MIN_START_V:g} V below V_D, so the sensor had not discharged since its last connection. Over so small a range tau cannot be separated from the per-connection charge (on the board such bursts were off by up to 20 %). The burst is excluded from the mean; wait longer between connections of a channel (Discharge time).",
    capfit.ACQUISITION_SHORT: f"7953 only: firmware reports an acquisition interval below {capfit.MIN_ACQUISITION_NS:g} ns. Increase the CS-high frame gap or lower the clock and acquire again.",
}


def _entry(title, text):
    return f"<h3>{escape(title)}</h3><p>{escape(text)}</p>"


def build_help_html(driver, columns, field_specs):
    """Build a board-specific document, using the same labels as the GUI."""
    adc124 = driver.id == "TestBoard_ADC124"
    baseline = (
        "ADC124 averages the last available pre-connect readings for each input into V_D. "
        "It reconstructs frame timing from result timestamps, rejects timestamp outliers from that "
        "timing-line fit, and applies the hold instant and stamp-latency settings. The measured "
        "MUX enable edge plus the turn-on delay defines t = 0. Samples during switching are "
        "excluded; each channel's retained data starts after Discard first K."
        if adc124 else
        "7953 averages the VMID-tagged readings at the end of parking and in the two lead frames "
        "into V_D. It accounts for the two-frame response pipeline, discards the first K sensor "
        "samples and rejects wrong channel tags. Cycle timestamps and the first-clock delay "
        "plus 1.5 SCLK periods define times relative to connection."
    )
    parts = [
        "<html><head><style>body {font-family: sans-serif;} p {line-height: 1.35;} "
        "h2 {color: #3979a8;} h3 {margin-bottom: 3px;}</style></head><body>",
        f'<h1>CapTest Help</h1><p>{escape(driver.entry.name if driver.entry else driver.id)}</p>',
        '<h2><a name="measurement"></a>Measurement and calculations</h2>',
        "<p>The board parks a shared MUX output node at VMID, connects a sensor, and records "
        "the voltage recovery. ADC voltage = code * VREF / 4096; one LSB = VREF / 4096. "
        "Only routed sensor channels produce result rows.</p>",
        f"<p>{escape(baseline)}</p>",
        _entry("Single exponential", "V(t) = Vinf - (Vinf - V0) * exp(-t / tau), where t is seconds "
               "since connection. The fit estimates V0, Vinf and a positive tau by minimising "
               "squared voltage residuals. C_total = tau / R_bias."),
        _entry("Two exponentials", "V(t) = Vinf - A_fast * exp(-t / tau_fast) - A_slow * exp(-t / tau_slow); "
               "V0 = Vinf - A_fast - A_slow. The displayed tau is tau_slow. The mapping of these "
               "components to separate physical capacitances is not established."),
        f"<p>Two exponentials are selected only when requested, the fit is valid, "
        f"tau_slow / tau_fast is at least {capfit.MIN_TAU_RATIO:g}, its RMS is below "
        f"{100 * capfit.MIN_RMS_IMPROVEMENT:g}% of the single-fit RMS, and the improvement exceeds "
        f"{capfit.MIN_RMS_GAIN_LSB:g} LSB. Otherwise the single fit is used.</p>",
        _entry("Vmid between samples (TestBoard_7953 default)", "The sensor is connected for one frame, "
               "then the node returns to VMID; every second cycle the connected frame is longer. With U_k "
               "the sensor sample of cycle k and d_k = V_D - U_k: d_(k+1) = exp(-Tc / tau) * ((1 - c) * d_k + q), "
               "Tc the connected time of the next cycle from the time stamps. For each tau the model is "
               "linear in (1 - c) and q; the tau with the smallest squared error is used. C_total = tau / "
               "R_bias, C_D column = C_share = c * C_total (the charge taken per connection, as a "
               "capacitance reset to V_D). c is fitted as c0 + c1 * d because the once-per-frame charge "
               "grows with the distance from V_D; the C_D column is C_share at V_D (c0). C_share is an effective connection capacitance, not the node capacitance, so C sensor and C_PZT are left empty for this method. The fit model column reads 'alt'. "
               "V0 is the first sample; Vinf is where the short cycles settle. The sensor start voltage is "
               "not used, so the Vs0 check is skipped."),
        _entry("Charge sharing and assumptions", "C_D / C_total = (V0 - V_s0) / (V_D - V_s0). "
               "C_D is the shared output-node capacitance; the remaining capacitance belongs "
               "to the sensor side. V_s0 is an assumption, not a voltage measured before each "
               "connection. Stored sensor charge, charge injection and timing errors can make "
               "the ratio unreliable even when the recovery fit looks clean. A power cycle, "
               "Fresh = yes or the configured discharge wait does not prove V_s0 = 0."),
        _entry("Repeated bursts and display", "Each numeric result is the mean of finite values "
               "from usable fitted bursts. FIT_FAILED, INCOMPLETE_BURST and FEW_SAMPLES exclude "
               "a burst; other flagged fits can still contribute. Flags combine warnings from "
               "all bursts and cross-channel checks. CSV/JSON also include burst variation. "
               "A dash means unavailable, not zero. Selecting a row plots its latest burst, "
               "so that plot can differ from the row's multi-burst mean."),
        '<h2><a name="columns"></a>Results columns</h2>',
        "<p>Rows stay in ascending numeric Array, MUX, Input order on ADC124, or ADC, Ch order "
        "on 7953, regardless of acquisition order. Identifiers retain the board's numbering.</p>",
    ]
    parts.extend(_entry(title, COLUMN_HELP.get(key, "Physical channel identifier."))
                 for title, key, _ in columns)
    parts.append('<h2><a name="flags"></a>Quality flags</h2>')
    parts.extend(_entry(flag, description) for flag, description in FLAG_HELP.items())
    parts.extend([
        '<h2><a name="settings"></a>Settings</h2>',
        "<p>These explanations follow the selected board's controls. Acquisition settings "
        "(sample count, clock, parking, burst count and scheduling) require new data. "
        "Re-analyse with current settings applies analysis choices such as R_bias, VREF, "
        "V_s0, parasitics, discarded samples and timing assumptions to saved raw samples. "
        "The fit-model selector re-analyses immediately. Save/load settings preserves these "
        "values per board; save raw bursts to retain the data for later analysis.</p>",
    ])
    for name in driver.settings_fields:
        parts.append(_entry(field_specs[name].label, SETTING_HELP[name]))
    parts.append(_entry("R_bias per channel [ohm]", "Editable bias resistance for each physical "
                        "channel, in ohms. C_total = tau / R_bias, so an incorrect resistor value "
                        "directly changes the capacitance scale. Defaults come from the board "
                        "configuration; verify actual values independently. Changing the table "
                        "and pressing Re-analyse recalculates stored results."))
    parts.append("</body></html>")
    return "".join(parts)
