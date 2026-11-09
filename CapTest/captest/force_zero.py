"""Frozen unloaded residual-bias correction, separate from voltage-dependent leakage."""
from __future__ import annotations

import math
import hashlib
from pathlib import Path
import numpy as np

from . import force_model as fm


def bias_values(record):
    if record and record.get("correction_kind") == "charge_rate":
        return {tuple(map(int, key.split(":"))): 0. for key in record["routes"]}
    return {tuple(map(int, key.split(":"))): row["bias_v"]
            for key, row in record["routes"].items()} if record else None


def drift_values(record):
    return {tuple(map(int, key.split(":"))): row["q_rate_c_per_s"]
            for key, row in record["routes"].items()} if record and record.get("correction_kind") == "charge_rate" else None


def verify_source(record, directory=None):
    if not record or not record.get("capture_file"):
        return
    path = Path(record["capture_file"])
    if not path.exists() and directory is not None:
        path = Path(directory) / path.name
    with path.open("rb") as handle:
        if record.get("capture_prefix_sha256"):
            data = handle.read(record["capture_prefix_bytes"])
            expected = record["capture_prefix_sha256"]
        else:
            data = handle.read()
            expected = record.get("capture_sha256")
    if expected and hashlib.sha256(data).hexdigest() != expected:
        raise fm.ModelError("unloaded correction source recording hash changed")


def analyse_linear(models, samples, *, end_s, window_s, model_basis, signature,
                   max_rate_error_c_per_s, leakage=None, polarity=1., span_volts=2.5,
                   route_polarity=None):
    """Search caller's recent tail for a line, then check its frozen slope on a later half.

    No fit uses the startup transient or a prior trial's baseline subtraction.
    The charge-rate correction uses actual inter-visit times, not a display detrend.
    """
    if (not all(math.isfinite(x) for x in (window_s, end_s, max_rate_error_c_per_s))
            or window_s < 6 or end_s < window_s + 2 or max_rate_error_c_per_s <= 0):
        raise fm.ModelError("linear drift test needs 2 s settling plus a window of at least 6 s")
    start, middle = end_s - window_s, end_s - window_s / 2
    if set(models) != set(samples):
        raise fm.ModelError("linear drift capture does not contain every selected route")
    q = fm.accumulate(models, samples, polarity=polarity, leakage=leakage, route_polarity=route_polarity)
    routes, reasons = {}, []
    raw_rate = corrected_rate = bound_sum = 0.
    for ref, model in models.items():
        t, charge = q.per_route_q[ref]
        data = samples[ref]
        domain, inputs = fm.range_window(t, start, end_s)
        report = fm.range_report(model, np.asarray(data["volts"])[inputs], span_volts=span_volts)
        bad_samples = "valid" in data and not np.all(np.asarray(data["valid"])[domain])
        bad_tc = np.any(~np.isfinite(np.asarray(data["tc"])[domain])) or np.any(np.asarray(data["tc"])[domain] <= 0)
        if report["invalid_input"] or report["clipped"] or report["outside_fitted_range"] or bad_samples or bad_tc:
            reasons.append("%s: invalid samples, ADC headroom or electrical range exceeded" % (ref,))
        slopes = []
        for a, b in ((start, middle), (middle, end_s)):
            blocks = np.arange(a, b + 1e-8, .5)
            if len(blocks) < 5 or t[0] > a or t[-1] < b - .05:
                raise fm.ModelError("insufficient samples in the linear drift window")
            block_q = np.interp(blocks, t, charge)
            slope, intercept = np.polyfit(blocks - a, block_q, 1)
            deviations = block_q - (slope * (blocks - a) + intercept)
            slopes.append((float(slope), float(np.std(deviations)), blocks, block_q))
        fitted, checked = slopes[0][0], slopes[1][0]
        # Compare both halves of the training section as well as the held-out slope.
        mid_fit = (start + middle) / 2
        early = (np.interp(mid_fit, t, charge) - np.interp(start, t, charge)) / (mid_fit - start)
        late = (np.interp(middle, t, charge) - np.interp(mid_fit, t, charge)) / (middle - mid_fit)
        change = max(abs(checked - fitted), abs(late - early))
        budget = max_rate_error_c_per_s
        stable = change <= budget
        if not stable:
            reasons.append("%s: drift slope is not stable enough; continue settling" % (ref,))
        bound_sum += change
        routes["%d:%d" % ref] = {"bias_v": 0., "q_rate_c_per_s": fitted,
            "check_q_rate_c_per_s": checked, "remaining_q_rate_c_per_s": checked - fitted,
            "slope_change_c_per_s": change, "rate_error_budget_c_per_s": budget,
            "fit_line_rms_c": slopes[0][1], "check_line_rms_c": slopes[1][1],
            "fit_window_s": (start, middle), "check_window_s": (middle, end_s),
            "stable": stable, "frozen": True}
        raw_rate += checked
        corrected_rate += checked - fitted
    if bound_sum > max_rate_error_c_per_s:
        reasons.append("combined channel slope changes exceed the drift budget; continue settling")
    return {"format": "captest-unloaded-drift-1", "correction_kind": "charge_rate",
        "status": "qualified" if not reasons else "rejected", "reasons": reasons,
        "routes": routes, "model_basis": model_basis, "signature": signature,
        "settle_s": start, "fit_window_s": (start, middle), "check_window_s": (middle, end_s),
        "check_raw_q_rate_c_per_s": raw_rate, "check_corrected_q_rate_c_per_s": corrected_rate,
        "slope_change_bound_c_per_s": bound_sum, "rate_error_budget_c_per_s": max_rate_error_c_per_s,
        "note": "Frozen linear-section charge drift; independent later check, not physical Roff"}


def _blocks(t, e, window, block_s=.25):
    means = []
    for start in np.arange(window[0], window[1] - block_s + 1e-8, block_s):
        values = e[(t >= start) & (t < start + block_s) & np.isfinite(e)]
        if len(values) >= 8:
            means.append(float(values.mean()))
    if len(means) < 4:
        raise fm.ModelError("unloaded drift measurement needs at least four populated blocks per window")
    return np.asarray(means)


def analyse(models, samples, *, duration_s, model_basis, signature, leakage=None,
            polarity=1., span_volts=2.5, skip_s=2., route_polarity=None):
    """Fit the first quiet window and independently check the last one.

    A constant voltage residual per visit produces dQ = Ctotal*e. Subtracting
    the frozen residual bias corrects charge increments, not just the plot.
    Block means reduce overconfidence from correlated samples. The holdout
    tests stationarity, not force accuracy or a physical off-state resistor.
    """
    if not math.isfinite(duration_s) or duration_s < 6:
        raise fm.ModelError("unloaded drift measurement must last at least 6 seconds")
    middle = (skip_s + duration_s) / 2
    fit_window, check_window = (skip_s, middle), (middle, duration_s)
    routes, reasons = {}, []
    leak = leakage or {}
    if set(models) != set(samples):
        raise fm.ModelError("unloaded capture does not contain every selected route")
    raw_rate = corrected_rate = 0.
    for ref, model in models.items():
        data = samples[ref]
        t, v = np.asarray(data["t"]), np.asarray(data["volts"])
        if (not np.all(np.isfinite(t)) or np.any(np.diff(t) <= 0)
                or not np.all(np.isfinite(v)) or not np.all(data.get("valid", True))):
            raise fm.ModelError("invalid unloaded samples/timestamps on %s" % (ref,))
        _, inputs = fm.range_window(t, skip_s, duration_s)
        report = fm.range_report(model, v[inputs], span_volts=span_volts)
        if report["clipped"]:
            reasons.append("%s: ADC headroom exceeded" % (ref,))
        if report["outside_fitted_range"]:
            reasons.append("%s: voltage fell below the electrical model's fitted range" % (ref,))
        e = fm.residuals(model, v, data["tc"], data.get("valid"), data.get("generation"),
                         leak.get(ref, fm.NO_LEAKAGE))
        train = _blocks(t, e, fit_window)
        check = _blocks(t, e, check_window)
        bias, check_bias = float(train.mean()), float(check.mean())
        uncertainty = float(np.sqrt(train.var(ddof=1) / len(train) + check.var(ddof=1) / len(check)))
        tolerance = max(5 * uncertainty, span_volts / (1 << fm.ADC_BITS_7953) / 20)
        stable = abs(check_bias - bias) <= tolerance
        if not stable:
            reasons.append("%s: unloaded residual is still changing; wait for settling and repeat" % (ref,))
        key = "%d:%d" % ref
        routes[key] = {"bias_v": bias, "std_v": float(train.std(ddof=1)),
                       "n": len(train), "fit_window_s": fit_window, "check_window_s": check_window,
                       "check_bias_v": check_bias, "remaining_bias_v": check_bias - bias,
                       "stability_tolerance_v": tolerance, "stable": stable, "frozen": True}
        sel = (t >= check_window[0]) & (t <= check_window[1]) & np.isfinite(e)
        rate = sel.sum() / (check_window[1] - check_window[0])
        sign = (route_polarity or {}).get(ref, 1)
        raw_rate += polarity * sign * model.c_total * check_bias * rate
        corrected_rate += polarity * sign * model.c_total * (check_bias - bias) * rate
    return {"format": "captest-unloaded-drift-1", "status": "qualified" if not reasons else "rejected",
            "reasons": reasons, "routes": routes, "model_basis": model_basis, "signature": signature,
            "settle_s": skip_s, "fit_window_s": fit_window, "check_window_s": check_window,
            "check_raw_q_rate_c_per_s": raw_rate, "check_corrected_q_rate_c_per_s": corrected_rate,
            "note": "Frozen unloaded residual bias per visit; does not identify voltage-dependent leakage"}
