"""Small, versioned consumer export; no packets, recordings or analysis histories.

This is a projection of ForceInputs, not a replacement for its evidence. SI units,
signed package gains, explicit unavailable values, and correction scope are retained.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import force_export as fx

FORMAT = "captest-sensor-params-1"


def companion_path(inputs, full_path):
    full_path = Path(full_path)
    link = inputs.data.get("sensor_params_export")
    if not link:
        stamp = datetime.now(timezone.utc)
        board = inputs.data["provenance"]["board_type"]
        board = "".join(c if c.isalnum() or c in "-_" else "_" for c in board)
        stem = "%s_SensorParams_%s" % (board, stamp.strftime("%Y-%m-%d_%H-%M-%SZ"))
        candidate = full_path.parent / (stem + ".json")
        i = 2
        while candidate.exists() or candidate.resolve() == full_path.resolve():
            candidate = full_path.parent / (stem + "_%d.json" % i)
            i += 1
        link = {"file": candidate.name, "created": stamp.strftime("%Y-%m-%dT%H:%M:%SZ")}
        inputs.data["sensor_params_export"] = link
    # The companion always stays next to its full manifest, including after Save As.
    name = link["file"]
    if Path(name).name != name or not name.endswith(".json"):
        raise fx.ForceExportError("invalid sensor-parameters companion filename")
    path = full_path.parent / name
    if path.resolve() == full_path.resolve():
        raise fx.ForceExportError("sensor-parameters companion cannot replace the full input file")
    return path


def _captest_status(route):
    if route is None:
        return "not_measured", []
    issues = fx.route_quality(route)
    if route.get("status") != "usable":
        return "failed", issues
    if "HIGH_RESIDUAL" in issues:
        return "high_residual", issues
    return ("invalid" if issues else "complete"), issues


def build(inputs):
    data = inputs.data
    electrical = inputs.electrical_hash()
    groups = {}
    for gid, mapping in data["mapping"]["groups"].items():
        revision = inputs.latest(gid)
        routes = {tuple(r["ref"]): r for r in revision["routes"]} if revision else {}
        state = data.get("sensor_params_state", {}).get(gid, {})
        signs = {"%d:%d" % ref: sign for ref, sign in inputs.route_polarity(gid).items()}
        current = (not state or (state.get("electrical_manifest_sha256") == electrical
                   and state.get("route_polarity") == signs))
        ready = not inputs.quality_issues(gid)
        gain_entry = data["force_inputs"]["d33_eff_c_per_n"].get(gid, {})
        if state:
            status = state.get("force_status", "not_tested") if current else "stale"
            gain = state.get("d33_eff_c_per_n") if status == "calibrated" else None
            capture_signature = state.get("capture_signature")
        else:
            gain_current = (gain_entry.get("electrical_manifest_sha256") == electrical
                            and gain_entry.get("relative_polarity") == inputs.relative_polarity(gid))
            status = ("calibrated" if gain_entry.get("status") == "validated" and gain_current
                      else "provisional" if gain_entry.get("status") == "fitted_provisional" and gain_current
                      else "stale" if gain_entry else "not_tested")
            gain = gain_entry.get("value") if status == "calibrated" else None
            capture_signature = gain_entry.get("provenance", {}).get("capture_signature")
        if not ready and status == "calibrated":
            status, gain = "stale", None
        if not fx._finite(gain) or gain == 0:
            gain = None
            if status == "calibrated":
                status = "invalid"
        drift = state.get("unloaded_correction") or {}
        drift_current = (current and ready and drift.get("status") == "qualified"
                         and drift.get("signature") == capture_signature)
        channels = []
        for mapped in mapping["routes"]:
            ref = (mapped["adc"], mapped["input"])
            route = routes.get(ref)
            cap_status, issues = _captest_status(route)
            row = route or {}
            key = "%d:%d" % ref
            dz = drift.get("routes", {}).get(key)
            correction = None
            if drift_current and dz and cap_status == "complete":
                if drift.get("correction_kind") == "charge_rate":
                    if fx._finite(dz.get("q_rate_c_per_s")):
                        correction = {"kind": "charge_rate", "q_rate_c_per_s": dz["q_rate_c_per_s"]}
                elif fx._finite(dz.get("bias_v")):
                    correction = {"kind": "residual_bias", "bias_v": dz["bias_v"]}
            drift_status = ("included" if correction else "stale" if drift else
                            "rejected" if state.get("last_drift_status") == "rejected" else "not_measured")
            channel = {"position": mapped["position"], "adc": ref[0], "input": ref[1],
                       "captest_status": cap_status, "captest_issues": issues,
                       "parameter_source": row.get("provenance") if route else None,
                       "force_calibration_status": status,
                       "polarity_status": "applied" if gid in data["force_inputs"].get("relative_polarity", {}) else "not_tested",
                       "relative_polarity": signs[key], "drift_status": drift_status,
                       "drift_correction": correction}
            for name in ("r_bias_ohm", "c_total_f", "v_d_v", "c0", "c1_per_v", "qv_v",
                         "d_min_v", "d_max_v"):
                channel[name] = row.get(name)
            domain = state.get("validated_voltage_domain", {}).get(key)
            if gain is not None and domain:
                channel["validated_voltage_range_v"] = [domain["v_min"], domain["v_max"]]
            channels.append(channel)
        groups[gid] = {"array": mapping["array"], "pzt": mapping["pzt"],
                       "captest_at": revision.get("measured_at") if revision else None,
                       "force_tested_at": state.get("tested_at"),
                       "force_calibrated_at": state.get("calibrated_at") if gain is not None else None,
                       "polarity_updated_at": data["force_inputs"].get("relative_polarity", {}).get(gid, {}).get("updated"),
                       "drift_measured_at": state.get("drift_measured_at") if drift_current else None,
                       "drift_last_test_status": state.get("last_drift_status"),
                       "force_calibration_status": status, "d33_eff_c_per_n": gain,
                       "force_ready": ready and gain is not None,
                       "capture_signature": capture_signature,
                       "leakage": state.get("leakage") if current and ready else None,
                       "channels": channels}
        if gain is not None:
            groups[gid]["validated_force_range_n"] = state.get("validated_force_range_n")
    link = data.get("sensor_params_export", {})
    return fx.sanitize({"format": FORMAT, "schema_version": 1,
        "created": link.get("created", fx._now()), "updated": fx._now(),
        "board": {k: data["provenance"].get(k) for k in ("board_type", "board_instance", "origin")},
        "firmware": data["provenance"].get("firmware", {}),
        "source_file": inputs.source_path.name if inputs.source_path else None,
        "electrical_manifest_sha256": electrical,
        "charge_conversion_model": data["charge_conversion_model"],
        "adc_bits": data["acquisition"]["adc_bits"], "voltage_span_v": data["acquisition"]["voltage_span_v"],
        "electrical_acquisition_signature": data["acquisition"]["signature"],
        "common_polarity": -1 if data["mapping"].get("reverse_polarity") else 1,
        "gain_scope": "signed_effective_package_gain", "drift_scope": "unloaded_check_only",
        "groups": groups})


def write(inputs, path):
    """Replace one small consumer file atomically; never emit NaN or raw evidence."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(build(inputs), indent=1, allow_nan=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def main():
    """Backfill the companion for an existing, hash-checked full input file."""
    import argparse
    parser = argparse.ArgumentParser(description="Create/update compact SensorParams beside a full CapTest force-input JSON.")
    parser.add_argument("force_inputs", type=Path)
    args = parser.parse_args()
    try:
        inputs = fx.ForceInputs.load(args.force_inputs)
        inputs.save(args.force_inputs)
    except (OSError, fx.ForceExportError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(inputs.sensor_params_path)


if __name__ == "__main__":
    main()
