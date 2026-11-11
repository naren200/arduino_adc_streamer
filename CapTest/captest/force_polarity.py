"""Relative press/release signs from individual routes, independent of the group sum."""
from __future__ import annotations

import numpy as np

from . import force_model as fm

AUTO_TIMEOUT_S = 20.
AUTO_BIN_S = .01
AUTO_BASELINE_BINS = 25


def _polarity_range(model, volts, span_volts):
    """Clipping is diagnostic for a direction check, not a rejection of its sign."""
    report = fm.range_report(model, volts, span_volts=span_volts)
    v = np.asarray(volts)
    lsb = span_volts / (1 << fm.ADC_BITS_7953)
    report["saturated_sample_count"] = int(np.sum((v <= 0) | (v >= span_volts - lsb)))
    notes = []
    if report["saturated"]:
        notes.append("ADC clipped (%d samples); charge magnitude unreliable" % report["saturated_sample_count"])
    elif report["clipped"]:
        notes.append("ADC headroom guard entered; allowed for polarity")
    if report["outside_fitted_range"] or report["above_vd_count"]:
        notes.append("model extrapolation")
    return report, notes


def detect_press(models, samples, *, span_volts=2.5, leakage=None):
    """Listen for a short press and opposite release, with no action marks or hold.

    Use prediction innovations so ordinary RC relaxation after a press is not
    mistaken for release. A rolling 250 ms quiet baseline follows rest bias
    until the first event; it freezes for the press/release pair. Detection uses
    absolute channel activity, never the signed group sum or existing signs.
    """
    pending = {"format": "captest-press-polarity-2", "automatic": True,
               "phase": "quiet", "status": "listening", "complete": False}
    if set(samples) != set(models) or not samples or any(not len(d["t"]) for d in samples.values()):
        return pending
    refs = list(models)
    end = min(float(samples[r]["t"][-1]) for r in refs if len(samples[r]["t"]))
    count = int(end / AUTO_BIN_S)
    if count < AUTO_BASELINE_BINS + 7:
        return pending
    pending["phase"] = "listening"
    residuals, blocks = {}, []
    for ref in refs:
        d = samples[ref]
        e = fm.residuals(models[ref], d["volts"], d["tc"], d.get("valid"), d.get("generation"),
                         (leakage or {}).get(ref, fm.NO_LEAKAGE))
        residuals[ref] = e
        ix = np.floor(np.asarray(d["t"]) / AUTO_BIN_S).astype(int)
        use = (ix >= 0) & (ix < count) & np.isfinite(e)
        n = np.bincount(ix[use], minlength=count)
        total = np.bincount(ix[use], weights=e[use], minlength=count)
        blocks.append(np.divide(total, n, out=np.full(count, np.nan), where=n > 0))
    values = np.asarray(blocks).T
    width = AUTO_BASELINE_BINS
    # Baselines exclude the current bin and the first 50 ms of switch/startup history.
    history = np.lib.stride_tricks.sliding_window_view(values, width, axis=0)[:-1]
    baseline = np.median(history, axis=-1)
    diff = np.diff(history, axis=-1)
    sigma = 1.4826 * np.median(np.abs(diff - np.median(diff, axis=-1, keepdims=True)), axis=-1) / np.sqrt(2.)
    threshold = np.maximum(5 * sigma, span_volts / 4096)
    signals = values[width:] - baseline
    active = np.sum(np.abs(signals) > threshold, axis=1) > len(refs) / 2
    active[:5] = False
    pairs = np.flatnonzero(active[:-1] & active[1:])
    if not len(pairs):
        return pending
    p = int(pairs[0])
    press = p + width
    frozen = baseline[p]
    limits = threshold[p]
    direction = np.sign(np.mean(values[press:press + 2] - frozen, axis=0))
    signal = values - frozen
    opposite = np.sum((signal * direction < -limits), axis=1) > len(refs) / 2
    opposite[:press + 2] = False
    releases = np.flatnonzero(opposite[:-1] & opposite[1:])
    pending.update(phase="pressed", press_start_s=(press - 1) * AUTO_BIN_S)
    if not len(releases):
        return pending
    release = int(releases[0])
    # End after 80 ms without another opposite response, rather than a timed hold.
    finish = None
    quiet = 0
    for k in range(release + 2, count):
        quiet = 0 if opposite[k] else quiet + 1
        if quiet >= 8:
            finish = k + 1
            break
    pending.update(phase="released", release_start_s=release * AUTO_BIN_S)
    if finish is None:
        return pending
    p_start, r_start, r_end = (press - 1) * AUTO_BIN_S, release * AUTO_BIN_S, finish * AUTO_BIN_S
    same = np.sum(signal * direction > limits, axis=1) > len(refs) / 2
    p_end = r_start
    quiet = 0
    for k in range(press + 2, release):
        quiet = 0 if same[k] else quiet + 1
        if quiet >= 8:
            p_end = (k + 1) * AUTO_BIN_S
            break
    b_start, b_end = (press - width) * AUTO_BIN_S, press * AUTO_BIN_S
    rows = {}
    for i, ref in enumerate(refs):
        model, d = models[ref], samples[ref]
        t, e = np.asarray(d["t"]), residuals[ref]
        quiet_values = e[(t >= b_start) & (t < b_end) & np.isfinite(e)]
        bias = float(np.median(quiet_values))
        noise = max(float(1.4826 * np.median(np.abs(quiet_values - bias))), span_volts / 4096 / 4)
        row = {"status": "unclear", "reasons": [], "suggested_sign": None, "bias_v": bias}
        rows["%d:%d" % ref] = row
        active_samples, inputs = fm.range_window(t, p_start, r_end)
        report, row["notes"] = _polarity_range(model, np.asarray(d["volts"])[inputs], span_volts)
        row["range"] = report
        if report["invalid_input"]:
            row["reasons"].append("non-finite voltage")
        if (not np.all(np.isfinite(t)) or np.any(np.diff(t) <= 0)
                or not np.all(np.isfinite(e[active_samples]))
                or "valid" in d and not np.all(np.asarray(d["valid"])[inputs])):
            row["reasons"].append("invalid samples or timing")
        for name, a, b in (("press", p_start, p_end), ("release", r_start, r_end)):
            select = (t >= a) & (t < b)
            n = int(select.sum())
            charge = float(np.nansum(e[select] - bias) * model.c_total)
            bound = model.c_total * max(5 * noise * np.sqrt(n),
                5 * noise * n / np.sqrt(max(1, len(quiet_values))), 3 * span_volts / 4096)
            sign = (1 if charge > 0 else -1) if abs(charge) > bound else 0
            row.update({name + "_q_c": charge, name + "_threshold_c": bound, name + "_direction": sign})
            if not sign:
                row["reasons"].append(name + ": response too weak or noisy")
        if row["press_direction"] and row["press_direction"] == row["release_direction"]:
            row["reasons"].append("press and release do not have opposite signs")
        if not row["reasons"]:
            row["status"] = "clear"
    reference, ready = _suggest(rows)
    return {**pending, "phase": "complete", "complete": True, "clipping_policy": "note_only",
            "status": "ready_for_review" if ready else "unclear", "routes": rows,
            "reference_press_direction": reference,
            "windows_s": {"baseline": (b_start, b_end), "press": (p_start, p_end), "release": (r_start, r_end)},
            "note": "Automatically detected press/release; no timed hold or action marks. "
                    "Relative signs for this package/mounting only. Unknown finger force does not calibrate gain."}


def _suggest(rows):
    clear = [r for r in rows.values() if r["status"] == "clear"]
    vote = sum(r["press_direction"] for r in clear)
    reference = (1 if vote > 0 else -1) if abs(vote) and len(clear) > len(rows) / 2 else None
    for row in clear:
        if reference is not None:
            row["suggested_sign"] = row["press_direction"] * reference
    return reference, len(clear) == len(rows) and reference is not None


def check_press(models, samples, marks, *, span_volts=2.5, window_s=.5, guard_s=.3,
                leakage=None):
    """Suggest relative signs from marked press/release events, without fitting gain.

    The unloaded bias is measured per route. Each event uses its own pre-event
    slope; a steady *group* hold is not required. Both event signs must be clear
    and opposite. The majority press direction defines relative +1; this does
    not determine absolute force polarity or distinguish wiring from mechanics.
    """
    if set(models) != set(samples):
        raise fm.ModelError("polarity test needs every selected channel")
    windows = {
        "pre_press": (marks.place - guard_s - window_s, marks.place - guard_s),
        "post_press": marks.post_place or (marks.place_clear + guard_s, marks.place_clear + guard_s + window_s),
        "pre_release": marks.pre_remove or (marks.remove - guard_s - window_s, marks.remove - guard_s),
        "post_release": marks.post_remove or (marks.remove_clear + guard_s, marks.remove_clear + guard_s + window_s),
    }
    if (windows["pre_press"][0] < marks.baseline[0]
            or windows["post_press"][1] >= windows["pre_release"][0]):
        raise fm.ModelError("polarity event windows overlap or precede the baseline; review the marks")
    bias = {}
    for ref, model in models.items():
        d = samples[ref]
        e = fm.residuals(model, d["volts"], d["tc"], d.get("valid"), d.get("generation"),
                         (leakage or {}).get(ref, fm.NO_LEAKAGE))
        bias[ref] = fm.estimate_bias(ref, d["t"], e, marks.baseline).bias_v
    # Deliberately omit existing common/relative signs and frozen signed drift.
    charge = fm.accumulate(models, samples, bias, leakage=leakage)
    rows = {}
    for ref, model in models.items():
        d = samples[ref]
        t, q = charge.per_route_q[ref]
        row = {"status": "unclear", "reasons": [], "suggested_sign": None}
        rows["%d:%d" % ref] = row
        active, inputs = fm.range_window(t, windows["pre_press"][0], windows["post_release"][1])
        report, row["notes"] = _polarity_range(model, np.asarray(d["volts"])[inputs], span_volts)
        row["range"] = report
        row["bias_v"] = bias[ref]
        if (not np.all(np.isfinite(t)) or np.any(np.diff(t) <= 0)
                or np.any(~np.isfinite(np.asarray(d["tc"])[active]))
                or np.any(np.asarray(d["tc"])[active] <= 0)
                or "valid" in d and not np.all(np.asarray(d["valid"])[inputs])):
            row["reasons"].append("invalid samples or timing")
        if report["invalid_input"]:
            row["reasons"].append("non-finite voltage")
        for name, pre, post in (("press", windows["pre_press"], windows["post_press"]),
                                ("release", windows["pre_release"], windows["post_release"])):
            if len(t) < 2 or t[0] > pre[0] or t[-1] < post[1]:
                row["reasons"].append(name + ": incomplete event windows")
                continue
            try:
                event = fm.event_step(t, q, pre, post, mode="pre")
                # A conservative charge/noise floor avoids guessing a sign from
                # quantisation, weak contact or strongly varying endpoint windows.
                threshold = max(5 * event.rms, 5 * span_volts / 4096 * model.c_total)
                row[name + "_q_c"] = event.delta_q
                row[name + "_threshold_c"] = threshold
                row[name + "_direction"] = (1 if event.delta_q > 0 else -1) if abs(event.delta_q) > threshold else 0
                if not row[name + "_direction"]:
                    row["reasons"].append(name + ": response too weak or variable")
            except fm.ModelError as exc:
                row["reasons"].append(name + ": " + str(exc))
        if (row.get("press_direction") and row.get("release_direction")
                and row["press_direction"] == row["release_direction"]):
            row["reasons"].append("press and release do not have opposite signs")
        if not row["reasons"]:
            row["status"] = "clear"
    reference, ready = _suggest(rows)
    return {"format": "captest-press-polarity-1", "clipping_policy": "note_only", "status": "ready_for_review" if ready else "unclear",
            "routes": rows, "windows_s": windows, "reference_press_direction": reference,
            "note": "Relative signs for this package/mounting only; unknown finger force does not calibrate gain. "
                    "Repeat with the intended load-spreading plate and another press position to check mechanical sign changes."}
