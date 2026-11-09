"""Atomic trial evidence and stable model identities, independent of Qt."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .force_export import sanitize


def digest(value) -> str:
    return hashlib.sha256(json.dumps(sanitize(value), sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def write_json(path, value) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = sanitize(value)
    body.pop("content_sha256", None)
    body["content_sha256"] = digest(body)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return path


def read_json(path) -> dict:
    def refuse(value):
        raise ValueError("non-finite JSON value: " + value)
    with open(path, encoding="utf-8") as handle:
        body = json.load(handle, parse_constant=refuse)
    expected = body.pop("content_sha256", None)
    if expected != digest(body):
        raise ValueError("trial evidence hash mismatch")
    return body


def replay_trial(path, analysis_index=-1):
    """Replay an original decision offline, using saved marks/models and raw hashes.

    Incomplete/cancelled captures return diagnostic samples and no force trial.
    The original recording and sidecar are never modified.
    """
    from . import capture, force_model as fm, force_calibration as fc, force_zero
    body = read_json(path)
    if body.get("format") != "captest-force-trial-1":
        raise ValueError("not CapTest trial evidence")
    raw = Path(body["raw_file"])
    if not raw.exists():
        raw = Path(path).parent / raw.name
    if body.get("raw_sha256") and hashlib.sha256(raw.read_bytes()).hexdigest() != body["raw_sha256"]:
        raise ValueError("raw capture hash mismatch")
    models = {}
    for m in body["models"].values():
        m = dict(m)
        m["ref"] = tuple(m["ref"])
        for name in ("d_min", "d_max"):
            if m.get(name) is None:
                m[name] = float("nan")
        models[m["ref"]] = fm.RouteModel(**m)
    result = capture.replay_file(raw, keep_routes=list(models), span_volts=body["span_volts"])
    analyses = body.get("analyses", [])
    if not result.valid or not analyses:
        return body, result, None, models
    saved = analyses[analysis_index]
    force_zero.verify_source(saved.get("unloaded_correction"), Path(path).parent)
    trial_data = saved["trial"]
    marks_data = trial_data.get("marks_s")
    if marks_data is None:
        return body, result, None, models
    marks = fc.Marks(**marks_data)
    applied = saved.get("applied_correction")
    leak = (fm.LeakageModel(applied["lam"], applied.get("v_leak_v"), applied["kind"])
            if applied else fm.NO_LEAKAGE)
    limits = saved.get("limits", body["limits"])
    trial = fc.analyze_trial(fc.Capture(result.samples, body["span_volts"]), models, marks,
        trial_data["mass_g"], trial_data["repeat"], polarity=body["polarity"],
        leakage={ref: leak for ref in models}, window_s=saved.get("window_s", body["window_s"]),
        guard_s=saved.get("guard_s", body["guard_s"]), g=body["g"],
        load_removal_tol=limits["load_removal_fraction"], hold_drift_tol=limits["hold_drift_fraction"],
        position=body.get("contact_position", ""),
        fixed_bias=force_zero.bias_values(saved.get("unloaded_correction")),
        fixed_drift=force_zero.drift_values(saved.get("unloaded_correction")),
        route_polarity=fm.route_signs_from_json(saved.get("route_polarity", body.get("route_polarity"))),
        range_policy=saved.get("range_policy", fc.LEGACY_RANGE_POLICY),
        bias_source=digest(saved["unloaded_correction"]) if saved.get("unloaded_correction") else "")
    guard = body.get("excursion_guard_v")
    if guard is not None and any(v["max_above_vd_v"] > guard for v in trial.ranges.values()):
        trial.accepted = False
        trial.supports_mass = False
        trial.reasons.append("EXCURSION_GUARD: user voltage guard exceeded (not a physical calibration limit)")
    for adc, stats in result.vd_by_adc.items():
        model = next((m for ref, m in models.items() if ref[0] == adc), None)
        if model:
            shift = stats["mean_v"] - model.v_d
            if abs(shift) > .003:
                trial.flags.append("VD_SHIFT_ADC%d_%+.1fmV" % (adc, shift * 1e3))
            if stats["std_v"] > .002:
                trial.flags.append("VD_NOISY_ADC%d" % adc)
    trial.reference_kind = trial_data.get("reference_kind", "known_mass")
    trial.model_revision = saved["model_revision"]
    return body, result, trial, models
