"""Explicit donor/blank estimates and constrained target refits (never measured Cs)."""
from __future__ import annotations

import copy
import math
from types import SimpleNamespace

import numpy as np

from . import capfit, protocol, runner
from .force_evidence import digest
from .force_export import ForceExportError, route_quality, route_record, _now
from .settings import Settings


def _route(inputs, group, ref):
    rev = inputs.latest(group)
    if rev:
        for route in rev["routes"]:
            if tuple(route["ref"]) == tuple(ref):
                return route
    raise ForceExportError("route %s in %s has no electrical record" % (ref, group))


def _blank(route, boundary):
    blank = route.get("baseline", {})
    value = blank.get("c_board_pf")
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ForceExportError("%s: a matching measured board blank is required" % route["ref"])
    if blank.get("method") != capfit.C_TOTAL_VMID_CYCLES:
        raise ForceExportError("%s: blank method is not the alternate method" % route["ref"])
    if blank.get("flags") or blank.get("note"):
        raise ForceExportError("%s: flagged/mismatched blank must be reviewed or re-measured" % route["ref"])
    if blank.get("disconnection_boundary", boundary) != boundary:
        raise ForceExportError("blank disconnection boundaries differ")
    return float(value) * 1e-12


def stale_estimate_reasons(inputs, routes):
    reasons = []
    for route in routes:
        estimate = route.get("estimate", {})
        for donor in estimate.get("donors", []):
            try:
                current = _route(inputs, donor["group"], donor["ref"])
                if digest(current) != donor["route_sha256"]:
                    reasons.append("%s: donor %s changed; re-estimate and recalibrate" %
                                   (route["ref"], donor["ref"]))
            except ForceExportError as exc:
                reasons.append(str(exc))
    return reasons


def estimate_route(inputs, group_id, target_ref, donors, *, sensor_class, wiring_class,
                   boundary, borrow_model=False):
    """Return a proposed route. Caller reviews it before committing a new revision.

    Donor classes are explicitly declared by the user for this selection. Matching
    acquisition/board/layout context is enforced by requiring one manifest.
    """
    if not all(str(x).strip() for x in (sensor_class, wiring_class, boundary)):
        raise ForceExportError("sensor size/type, wiring class and blank boundary are required")
    target = _route(inputs, group_id, target_ref)
    if not route_quality(target):
        raise ForceExportError("select a failed electrical route; measured healthy routes are retained")
    target_blank = _blank(target, boundary)
    selected, evidence, added, seen = [], [], [], set()
    for gid, ref in donors:
        ref = tuple(ref)
        if ref == tuple(target_ref) or ref in seen:
            raise ForceExportError("target/self or duplicate donor")
        seen.add(ref)
        donor = _route(inputs, gid, ref)
        if route_quality(donor) or donor.get("provenance", "measured") != "measured":
            raise ForceExportError("%s: donors must have qualified measured fits" % (ref,))
        declared = donor.get("classification")
        if declared and (declared.get("sensor_class") != sensor_class or
                         declared.get("wiring_class") != wiring_class):
            raise ForceExportError("donor size/type/wiring classification differs")
        cblank = _blank(donor, boundary)
        net = donor["c_total_f"] - cblank
        if net <= 0:
            raise ForceExportError("%s: nonpositive loaded-minus-blank capacitance" % (ref,))
        added.append(net)
        selected.append(donor)
        evidence.append({"group": gid, "ref": list(ref), "route_sha256": digest(donor),
                         "c_total_f": donor["c_total_f"], "c_blank_f": cblank,
                         "c_added_f": net})
    if not added:
        raise ForceExportError("select at least one healthy, matching measured donor")
    ctotal = float(np.median(added)) + target_blank
    proposed = copy.deepcopy(target)
    mode = "constrained_target_refit"
    if borrow_model:
        # Sharing is path-specific: do not pool arbitrary ADCs into a physical model.
        if any(d["ref"][0] != target_ref[0] for d in selected):
            raise ForceExportError("experimental full-model donors must use the target ADC")
        for key in ("c0", "c1_per_v", "qv_v"):
            proposed[key] = float(np.median([d[key] for d in selected]))
        vd = [b.get("v_d_v") for b in target.get("per_burst", [])]
        vd = [v for v in vd if isinstance(v, (float, int)) and math.isfinite(v)]
        if not vd and isinstance(target.get("v_d_v"), (float, int)):
            vd = [target["v_d_v"]]
        if not vd:
            raise ForceExportError("target ADC reference measurement is unavailable")
        proposed.update(v_d_v=float(np.median(vd)), c_total_f=ctotal,
                        tau_s=target["r_bias_ohm"] * ctotal, flags=[], status="usable", reason="")
        # Only claim the common observed donor domain; target still needs validation.
        proposed["d_min_v"] = max(d.get("d_min_v") or 0 for d in selected)
        proposed["d_max_v"] = min(d.get("d_max_v") or 0 for d in selected)
        proposed["per_burst"] = []
        proposed["residual_rms_lsb"] = None
        proposed["diagnostics"] = {"note": "switching model borrowed; no measured target residual"}
        mode = "borrowed_switching_model"
    else:
        settings = Settings.from_dict(inputs.latest(group_id)["settings"])
        measured, ids = [], {}
        for entry in target.get("per_burst", []):
            pid = entry.get("raw_packet_id")
            if not pid:
                continue
            packet = inputs.raw_packet(pid)
            burst = protocol.decode_burst_packet(packet)
            if not burst.flags & protocol.FLAG_ALTERNATE:
                continue
            m = capfit.analyze_alternate(burst, target["r_bias_ohm"], settings.analysis(),
                                        fixed_c_total_f=ctotal)
            m.ref = tuple(target_ref)
            m.source = SimpleNamespace(since_previous_s=entry.get("since_previous_s"),
                                       fresh=entry.get("fresh"))
            ids[id(m.source)] = pid
            measured.append(m)
        if not measured:
            raise ForceExportError("no compatible target raw transitions; re-measure or explicitly borrow a model")
        agg = capfit.aggregate(measured, method=capfit.C_TOTAL_VMID_CYCLES)
        result = runner.ChannelResult(target_ref[0], target_ref[1], agg, measured,
                                      target["r_bias_ohm"], len(measured), failed=agg.count == 0,
                                      ref=tuple(target_ref), label=target.get("label", ""))
        proposed = route_record(result, target["position"], settings, ids)
        proposed["baseline"] = copy.deepcopy(target["baseline"])
        issues = route_quality(proposed)
        if issues:
            raise ForceExportError("constrained target refit is still invalid: " + "; ".join(issues))
    proposed["provenance"] = "estimated"
    proposed["classification"] = {"sensor_class": sensor_class, "wiring_class": wiring_class,
                                   "source": "explicit user declaration for target and selected donors"}
    proposed["estimate"] = {"method": mode, "target_original_sha256": digest(target),
                            "donors": evidence, "target_blank_f": target_blank,
                            "disconnection_boundary": boundary, "sensor_class": sensor_class,
                            "wiring_class": wiring_class, "c_added_median_f": float(np.median(added)),
                            "c_added_mad_f": float(np.median(np.abs(added - np.median(added)))),
                            "confidence": "low" if len(added) < 3 else "donor_spread_only",
                            "note": "Includes strip/flex capacitance; not measured Cs, Coff or charge gain",
                            "original_residual_rms_lsb": target.get("residual_rms_lsb"),
                            "refit_residual_rms_lsb": proposed.get("residual_rms_lsb"),
                            "created": _now()}
    proposed["quality"] = {"status": "provisional", "reasons": [],
                            "requires": "independent package-force validation"}
    return proposed


def apply_estimate(inputs, group_id, proposed):
    if proposed.get("provenance") != "estimated" or route_quality(proposed):
        raise ForceExportError("not a usable explicit estimate")
    current = inputs.latest(group_id)
    original = _route(inputs, group_id, proposed["ref"])
    if digest(original) != proposed["estimate"]["target_original_sha256"]:
        raise ForceExportError("target changed since preview; calculate a fresh estimate")
    if stale_estimate_reasons(inputs, [proposed]):
        raise ForceExportError("a donor changed since preview")
    revision = copy.deepcopy(current)
    revision.update(revision=current["revision"] + 1, measured_at=_now(), superseded=False,
                    source="explicit estimated-parameter recovery")
    revision["routes"] = [copy.deepcopy(proposed) if r["ref"] == proposed["ref"] else r
                          for r in revision["routes"]]
    from .force_export import package_status
    revision["status"] = package_status(revision["routes"], len(revision["routes"]))
    current["superseded"] = True
    inputs.data["packages"][group_id].append(revision)
    inputs.data["schema_version"] = 2
    inputs.data["force_inputs"]["d33_eff_c_per_n"].pop(group_id, None)
    inputs.data["merge_history"].append({"at": _now(), "group": group_id,
                                         "revision": revision["revision"], "status": "estimated"})
    inputs._refresh()
    return revision
