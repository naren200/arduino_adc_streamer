"""Force-calculation input export: ``captest-force-inputs-1``.

A manifest holds the electrical inputs and raw evidence needed to rebuild the force model of
the PZT packages the user measured. It may be partial (some packages) and is updated later by
measuring further packages: a re-measured package adds a revision that supersedes the earlier
one (the earlier one is kept). JSON plus the embedded raw packets is authoritative; the
flattened CSV is only for inspection.

Everything is SI inside the file (volts, farads, seconds, ohms); unavailable numbers are JSON
null with a reason, never NaN/Infinity and never zero.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from . import __version__, capfit, force_model, sensor_groups
from .settings import Settings

FORMAT = "captest-force-inputs-1"
SCHEMA_VERSION = 2

Channel = Tuple[int, ...]

# Package states in the coverage section.
COMPLETE = "complete"          # all five routes measured and usable: calibration may start
FAILED = "failed"              # all routes measured, at least one unusable: evidence only
INCOMPLETE = "incomplete"      # interrupted pass (not all routes measured): diagnostic only
MISSING = "missing"            # not measured yet

NOT_ESTABLISHED = "not established by CapTest"


def route_quality(route: dict) -> List[str]:
    """Quality is independent of coverage; also checks legacy manifests on use."""
    reasons = []
    if route.get("status") != "usable":
        reasons.append(route.get("reason") or "no usable electrical fit")
    bursts = route.get("per_burst", [])
    # Aggregates retain diagnostic flags from excluded bursts too. They must not
    # disqualify a healthy fit made only from other, usable bursts.
    burst_flags = {flag for burst in bursts for flag in burst.get("flags", [])}
    flags = set(route.get("flags", [])) - burst_flags if bursts else set(route.get("flags", []))
    for burst in bursts:
        if burst.get("status") == "used":
            flags.update(burst.get("flags", []))
    rejected = {capfit.HIGH_RESIDUAL, capfit.TAG_MISMATCH, capfit.VMID_RESET_FAILED,
                capfit.FIT_FAILED, capfit.INCOMPLETE, capfit.FEW_SAMPLES,
                capfit.ALT_SMALL_RANGE}
    reasons.extend(sorted(flags & rejected))
    for name in ("r_bias_ohm", "c_total_f", "v_d_v", "c0", "c1_per_v", "qv_v"):
        value = route.get(name)
        if not _finite(value) or (name in ("r_bias_ohm", "c_total_f") and value <= 0):
            reasons.append(name + " is unavailable/invalid")
    return list(dict.fromkeys(reasons))


class ForceExportError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize(value):
    """Plain, strict-JSON data: numpy scalars/arrays converted, NaN/Infinity -> None."""
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, np.ndarray):
        return sanitize(value.tolist())
    if isinstance(value, np.generic):
        return sanitize(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _finite(x) -> bool:
    return x is not None and isinstance(x, (int, float, np.floating)) and math.isfinite(float(x))


def acquisition_signature(settings: Settings) -> str:
    """Timing context in which a coefficient set was fitted (alternate method, 7953)."""
    return force_model.signature_of(
        method=settings.method, sensor_run=1, vmid_frames=settings.vmid_frames,
        gap_ns=settings.gap_ns, gap_long_ns=settings.gap_long_ns,
        spi_clock_hz=settings.spi_clock_hz, first_clock_delay_ns=settings.first_clock_delay_ns,
        park_us=settings.park_us, vref=settings.vref)


def _burst_coefficients(m: capfit.Measurement) -> Optional[dict]:
    """c0, c1, qv of one alternate-method burst (None when the fields are missing)."""
    info = m.connect_info or {}
    needed = (m.c_total, m.v_d, m.c_share_ratio, info.get("q_cycle_pc"), info.get("c_share_1v_pf"))
    if not all(_finite(x) for x in needed) or m.c_total <= 0:
        return None
    c_total = float(m.c_total)
    c0 = float(m.c_share_ratio)
    c1 = (float(info["c_share_1v_pf"]) * 1e-12 - c0 * c_total) / (c_total * 1.0)
    qv = float(info["q_cycle_pc"]) * 1e-12 / c_total
    return {"c0": c0, "c1": c1, "qv_v": qv}


def _distance_range(m: capfit.Measurement) -> Tuple[Optional[float], Optional[float]]:
    d = np.asarray(m.alt_d_v, dtype=float)
    d = d[np.isfinite(d) & (d > 0)]
    d = d[d > capfit.ALT_MIN_DISTANCE_V]
    if len(d) == 0:
        return None, None
    return float(d.min()), float(d.max())


def route_record(result, position: str, settings: Settings, raw_ids: Dict[int, str]) -> dict:
    """Electrical inputs, diagnostics and per-burst evidence of one sensor route.

    ``raw_ids`` maps id(raw burst) to its packet id in the manifest."""
    ref = tuple(result.ref)
    used_ids = {id(m) for m in result.used}
    reasons: List[str] = []
    bursts = []
    coeffs = []
    d_lo: List[float] = []
    d_hi: List[float] = []
    for i, m in enumerate(result.measurements):
        coef = _burst_coefficients(m) if m.c_total_method == capfit.C_TOTAL_VMID_CYCLES else None
        status, why = "excluded", []
        if id(m) in used_ids and coef is not None:
            bad = [f for f in m.flags if f in (capfit.TAG_MISMATCH, capfit.VMID_RESET_FAILED)]
            if bad:
                why = bad
            else:
                status = "used"
        else:
            if m.c_total_method != capfit.C_TOTAL_VMID_CYCLES:
                why.append("method %s is not the alternate (Vmid between samples) method"
                           % m.c_total_method)
            elif coef is None:
                why.append("coefficients unavailable")
            why += [f for f in m.flags if f in (capfit.FIT_FAILED, capfit.INCOMPLETE,
                                                capfit.FEW_SAMPLES, capfit.ALT_SMALL_RANGE)]
        lo, hi = _distance_range(m)
        entry = {
            "index": i, "status": status, "reasons": why, "flags": list(m.flags),
            "method": m.c_total_method, "fit_model": m.fit_model_used,
            "tau_s": m.tau, "c_total_f": m.c_total, "v_d_v": m.v_d,
            "c0": coef["c0"] if coef else None, "c1_per_v": coef["c1"] if coef else None,
            "qv_v": coef["qv_v"] if coef else None,
            "d_min_v": lo, "d_max_v": hi, "n_used": m.n_used,
            "residual_rms_lsb": m.residual_rms_lsb, "amplitude_lsb": m.amplitude_lsb,
            "vinf_v": m.vinf, "leak_na": m.leak_na,
            "diagnostics": {k: float(v) for k, v in (m.connect_info or {}).items()},
            "raw_packet_id": raw_ids.get(id(m.source)) if m.source is not None else None,
            "since_previous_s": getattr(m.source, "since_previous_s", None),
            "fresh": getattr(m.source, "fresh", None),
        }
        bursts.append(entry)
        if status == "used":
            coeffs.append((m, coef))
            if lo is not None:
                d_lo.append(lo)
                d_hi.append(hi)

    base = {
        "ref": list(ref), "position": position, "label": result.label,
        "r_bias_ohm": float(result.r_bias), "r_bias_source": "CapTest bias-resistor table",
        "r_bias_tolerance": float(settings.bias_tolerance),
        "bursts_total": len(result.measurements), "bursts_used": len(coeffs),
        "per_burst": bursts, "unavailable": {},
        "provenance": "measured",
        "baseline": {"c_board_pf": result.c_board_pf if _finite(result.c_board_pf) else None,
                     "c_net_pf": result.c_net_pf if _finite(result.c_net_pf) else None,
                     "method": getattr(result.baseline, "method", None),
                     "flags": getattr(result.baseline, "flags", ""),
                     "note": result.net_note or result.baseline_note or ""},
    }
    if result.failed or not coeffs:
        reason = result.message or "no usable alternate-method burst"
        base.update({"status": "failed", "reason": reason, "flags": list(result.flags)})
        for name in ("c_total_f", "tau_s", "v_d_v", "c0", "c1_per_v", "qv_v"):
            base[name] = None
            base["unavailable"][name] = reason
        return base

    def mean(values):
        return float(np.mean(values))

    c_totals = [float(m.c_total) for m, _ in coeffs]
    base.update({
        "status": "usable", "reason": "", "flags": list(result.flags),
        "method": capfit.C_TOTAL_VMID_CYCLES,
        # Coefficients are aggregated per burst and kept with the VD/Rb/timing of those bursts;
        # methods and configurations are never combined.
        "aggregation": "mean of used bursts of one method (per-burst values retained)",
        "c_total_f": mean(c_totals),
        "c_total_std_f": float(np.std(c_totals, ddof=1)) if len(c_totals) > 1 else None,
        "tau_s": mean([float(m.tau) for m, _ in coeffs]),
        "v_d_v": mean([float(m.v_d) for m, _ in coeffs]),
        "v_d_std_v": (float(np.std([float(m.v_d) for m, _ in coeffs], ddof=1))
                      if len(coeffs) > 1 else None),
        "c0": mean([c["c0"] for _, c in coeffs]),
        "c1_per_v": mean([c["c1"] for _, c in coeffs]),
        "qv_v": mean([c["qv_v"] for _, c in coeffs]),
        "d_min_v": min(d_lo) if d_lo else None, "d_max_v": max(d_hi) if d_hi else None,
        "residual_rms_lsb": mean([float(m.residual_rms_lsb) for m, _ in coeffs]),
    })
    info = [m.connect_info for m, _ in coeffs]
    base["diagnostics"] = {
        "c_share_vd_f": base["c0"] * base["c_total_f"],
        "c_share_1v_pf": mean([i["c_share_1v_pf"] for i in info]),
        "q_cycle_pc": mean([i["q_cycle_pc"] for i in info]),
        "vinf_v": mean([float(m.vinf) for m, _ in coeffs if _finite(m.vinf)]) if any(
            _finite(m.vinf) for m, _ in coeffs) else None,
        "vinf_long_v": mean([i["vinf_long_v"] for i in info if "vinf_long_v" in i]) if any(
            "vinf_long_v" in i for i in info) else None,
        "t_short_us": mean([i["t_short_us"] for i in info if "t_short_us" in i]) if any(
            "t_short_us" in i for i in info) else None,
        "t_long_us": mean([i["t_long_us"] for i in info if "t_long_us" in i]) if any(
            "t_long_us" in i for i in info) else None,
        "resets_failed": sum(i.get("resets_failed", 0.0) for i in info),
    }
    baseline = result.baseline
    base["baseline"].update({
        "c_board_pf": result.c_board_pf if _finite(result.c_board_pf) else None,
        "c_net_pf": result.c_net_pf if _finite(result.c_net_pf) else None,
        "note": result.net_note or result.baseline_note or "",
        "method": getattr(baseline, "method", None) if baseline is not None else None,
    })
    base["provenance"] = "measured"
    base["quality"] = {"reasons": route_quality(base)}
    base["quality"]["status"] = "rejected" if base["quality"]["reasons"] else "qualified"
    return base


def package_status(routes: Sequence[dict], expected: int) -> str:
    if len(routes) < expected:
        return INCOMPLETE
    return COMPLETE if all(r["status"] == "usable" for r in routes) else FAILED


class ForceInputs:
    """In-memory manifest. Build with :meth:`new`, load with :meth:`load`, grow with
    :meth:`add_package`, write with :meth:`save`."""

    def __init__(self, data: dict):
        self.data = data
        self.source_path = None
        self.sensor_params_path = None

    # ------------------------------------------------------------------ creation
    @classmethod
    def new(cls, group_map: sensor_groups.GroupMap, settings: Settings, *,
            board_instance: str = "", simulated: bool = False, firmware: Optional[dict] = None,
            run_id: str = "") -> "ForceInputs":
        data = {
            "format": FORMAT, "schema_version": SCHEMA_VERSION,
            "charge_conversion_model": force_model.CHARGE_MODEL,
            "created": _now(), "updated": _now(),
            "provenance": {
                "app": "CapTest", "app_version": __version__,
                "board_type": settings.board_id, "board_instance": board_instance,
                "origin": "simulated" if simulated else "hardware",
                "firmware": dict(firmware or {}), "run_id": run_id,
                "boot_ids": [], "source_hashes": [],
            },
            "mapping": {**group_map.identity(),
                        "groups": {g.group_id: g.to_json() for g in group_map.ordered()}},
            "acquisition": {
                "adc_bits": force_model.ADC_BITS_7953, "voltage_span_v": settings.vref,
                "code_to_volts": "code * span / 2**bits", "settings": json.loads(settings.to_json()),
                "signature": acquisition_signature(settings),
                "timing_provenance": "frame timestamps of each burst; connected times fitted "
                                     "per burst (see per-burst diagnostics)",
            },
            "force_inputs": {
                "coff_f": None, "leakage_law": None, "kq_c_per_v": None, "kf_n_per_v": None,
                "d33_eff_c_per_n": {},
                "reason": NOT_ESTABLISHED + ": null means unavailable, never an inferred value",
            },
            "packages": {}, "raw_packets": {}, "merge_history": [],
            "coverage": {},
        }
        self = cls(data)
        self._refresh()
        return self

    # --------------------------------------------------------------- properties
    @property
    def group_ids(self) -> List[str]:
        return list(self.data["mapping"]["groups"])

    def latest(self, group_id: str) -> Optional[dict]:
        revs = self.data["packages"].get(group_id) or []
        live = [r for r in revs if not r.get("superseded")]
        return live[-1] if live else None

    def revisions(self, group_id: str) -> List[dict]:
        return list(self.data["packages"].get(group_id) or [])

    def status(self, group_id: str) -> str:
        rev = self.latest(group_id)
        return rev["status"] if rev else MISSING

    def ready_groups(self) -> List[str]:
        return [g for g in self.group_ids if not self.quality_issues(g)]

    def quality_issues(self, group_id: str) -> List[str]:
        rev = self.latest(group_id)
        if rev is None or len(rev["routes"]) != len(self.data["mapping"]["groups"][group_id]["routes"]):
            return ["incomplete electrical coverage"]
        reasons = []
        for route in rev["routes"]:
            reasons.extend("%s: %s" % (route["ref"], reason) for reason in route_quality(route))
        from .force_estimates import stale_estimate_reasons
        reasons.extend(stale_estimate_reasons(self, rev["routes"]))
        return reasons

    def model_provenance(self, group_id: str) -> str:
        rev = self.latest(group_id)
        return "estimated" if rev and any(r.get("provenance") == "estimated"
                                          for r in rev["routes"]) else "measured"

    def coverage(self) -> dict:
        return self.data["coverage"]

    # ---------------------------------------------------------------- compatibility
    def mismatches(self, group_map: sensor_groups.GroupMap, settings: Settings,
                   board_instance: Optional[str] = None) -> List[str]:
        """Reasons a new pass cannot be merged into this file (empty list = compatible)."""
        problems: List[str] = []
        d = self.data
        if settings.board_id != d["provenance"]["board_type"]:
            problems.append("board type %s differs from %s"
                            % (settings.board_id, d["provenance"]["board_type"]))
        if group_map.layout_hash != d["mapping"]["layout_hash"]:
            problems.append("sensor-library layout changed (hash differs)")
        if board_instance is not None and board_instance != d["provenance"]["board_instance"]:
            problems.append("board instance %r differs from %r"
                            % (board_instance, d["provenance"]["board_instance"]))
        if acquisition_signature(settings) != d["acquisition"]["signature"]:
            problems.append("acquisition settings differ (%s)" % _signature_diff(
                d["acquisition"]["signature"], acquisition_signature(settings)))
        if settings.vref != d["acquisition"]["voltage_span_v"]:
            problems.append("ADC voltage span differs")
        return problems

    # ------------------------------------------------------------------ adding
    def add_package(self, group: sensor_groups.PztGroup, results: Dict[Channel, object],
                    settings: Settings, group_map: sensor_groups.GroupMap, *,
                    board_instance: Optional[str] = None, simulated: bool = False,
                    boot_id: Optional[int] = None, source: str = "") -> dict:
        """Record a measurement pass of one package. Raises ForceExportError when the file's
        board/layout/acquisition context does not match (nothing is changed then)."""
        problems = self.mismatches(group_map, settings, board_instance)
        origin = "simulated" if simulated else "hardware"
        if origin != self.data["provenance"]["origin"]:
            problems.append("origin %s differs from %s" % (origin, self.data["provenance"]["origin"]))
        if group.group_id not in self.data["mapping"]["groups"]:
            problems.append("%s is not a group of this file" % group.group_id)
        if problems:
            raise ForceExportError("cannot add %s: %s" % (group.group_id, "; ".join(problems)))
        wanted = {r.ref for r in group.routes}
        stray = [ref for ref in results if tuple(ref) not in wanted]
        if stray:
            raise ForceExportError("results of other routes given for %s: %s"
                                   % (group.group_id, stray))

        raw_ids: Dict[int, str] = {}
        packets = self.data["raw_packets"]
        for ref, result in results.items():
            for m in result.measurements:
                raw = m.source
                if raw is None:
                    continue
                pid = hashlib.sha256(raw.packet).hexdigest()[:24]
                packets.setdefault(pid, base64.b64encode(raw.packet).decode("ascii"))
                raw_ids[id(raw)] = pid
        routes = []
        for route in group.routes:
            result = results.get(route.ref)
            if result is None:
                continue
            routes.append(route_record(result, route.position, settings, raw_ids))
        status = package_status(routes, len(group.routes))
        revs = self.data["packages"].setdefault(group.group_id, [])
        for r in revs:
            r["superseded"] = True
        revision = (max((r["revision"] for r in revs), default=0)) + 1
        entry = {"revision": revision, "measured_at": _now(), "status": status,
                 "superseded": False, "routes": routes, "source": source,
                 "settings": json.loads(settings.to_json())}
        if status == INCOMPLETE:
            entry["note"] = "interrupted pass: diagnostic evidence only, not usable for calibration"
        revs.append(entry)
        prov = self.data["provenance"]
        if boot_id is not None and boot_id not in prov["boot_ids"]:
            prov["boot_ids"].append(boot_id)
        if source and source not in prov["source_hashes"]:
            prov["source_hashes"].append(source)
        self.data["merge_history"].append({"at": _now(), "group": group.group_id,
                                           "revision": revision, "status": status})
        self._refresh()
        return entry

    def set_d33_eff(self, group_id: str, d33_c_per_n: float, provenance: dict) -> None:
        """Embed a fitted group sensitivity (updated bundle after calibration)."""
        if group_id not in self.data["mapping"]["groups"]:
            raise ForceExportError("unknown group %s" % group_id)
        if not (math.isfinite(d33_c_per_n) and d33_c_per_n != 0):
            raise ForceExportError("d33_eff must be finite and nonzero")
        self.data["force_inputs"]["d33_eff_c_per_n"][group_id] = {
            "value": d33_c_per_n, "provenance": provenance,
            "status": provenance.get("status", "fitted_provisional"),
            "electrical_manifest_sha256": self.electrical_hash(),
            "relative_polarity": self.relative_polarity(group_id),
            "charge_conversion_model": force_model.CHARGE_MODEL,
            "note": "effective system sensitivity for the capacitance proxy; apply once, "
                    "never divide by d33 twice"}
        self._refresh()

    def relative_polarity(self, group_id: str) -> dict:
        if group_id not in self.data["mapping"]["groups"]:
            raise ForceExportError("unknown group %s" % group_id)
        positions = [r["position"] for r in self.data["mapping"]["groups"][group_id]["routes"]]
        saved = self.data["force_inputs"].get("relative_polarity", {}).get(group_id, {}).get("values", {})
        if set(saved) - set(positions) or any(v not in (-1, 1) for v in saved.values()):
            raise ForceExportError("invalid relative channel polarity for " + group_id)
        return {p: saved.get(p, 1) for p in positions}

    def route_polarity(self, group_id: str) -> dict:
        signs = self.relative_polarity(group_id)
        return {(r["adc"], r["input"]): signs[r["position"]]
                for r in self.data["mapping"]["groups"][group_id]["routes"]}

    def set_relative_polarity(self, group_id: str, values: dict, *, press_check=None):
        previous = self.relative_polarity(group_id)
        if set(values) != set(previous) or any(v not in (-1, 1) for v in values.values()):
            raise ForceExportError("provide +1 or -1 for every position in " + group_id)
        self.data["force_inputs"].setdefault("relative_polarity", {})[group_id] = {
            "values": {p: int(values[p]) for p in previous}, "updated": _now(),
            "source": "explicit user selection; fixed during loading and removal"}
        if press_check is not None:
            self.data["force_inputs"]["relative_polarity"][group_id]["press_check"] = sanitize(press_check)
        self._refresh()

    # ------------------------------------------------------------- derived data
    def route_models(self, group_id: str, v_span: Optional[float] = None
                     ) -> Dict[Channel, force_model.RouteModel]:
        """RouteModels of the group's latest COMPLETE revision (SI). Raises when the package is
        not ready: a failed/missing package never calibrates."""
        rev = self.latest(group_id)
        issues = self.quality_issues(group_id)
        if issues:
            raise ForceExportError("%s is %s: its electrical inputs are not usable for force "
                                   "calibration: %s" % (group_id, self.status(group_id), "; ".join(issues)))
        signature = self.data["acquisition"]["signature"]
        models = {}
        for r in rev["routes"]:
            ref = tuple(r["ref"])
            models[ref] = force_model.RouteModel(
                ref, r["r_bias_ohm"], r["c_total_f"], r["v_d_v"], r["c0"], r["c1_per_v"],
                r["qv_v"], r["d_min_v"] if r["d_min_v"] is not None else float("nan"),
                r["d_max_v"] if r["d_max_v"] is not None else float("nan"), signature)
        return models

    def _refresh(self) -> None:
        electrical = self.electrical_hash()
        for gid, entry in self.data["force_inputs"]["d33_eff_c_per_n"].items():
            if entry.get("electrical_manifest_sha256") not in (None, electrical):
                entry["status"] = "stale"
                entry["stale_reason"] = "electrical inputs changed since force validation"
            expected = entry.get("relative_polarity", {p: 1 for p in self.relative_polarity(gid)})
            if expected != self.relative_polarity(gid):
                entry["status"] = "stale"
                entry["stale_reason"] = "relative channel polarity changed since force validation"
        groups = self.data["mapping"]["groups"]
        per = {}
        for gid in groups:
            rev = self.latest(gid)
            if rev is None:
                per[gid] = {"status": MISSING, "revision": None, "usable_routes": 0,
                            "failed_routes": 0, "measured_routes": 0, "missing_routes": len(groups[gid]["routes"])}
                continue
            n = len(groups[gid]["routes"])
            usable = sum(1 for r in rev["routes"] if r["status"] == "usable")
            per[gid] = {"status": rev["status"], "revision": rev["revision"],
                        "usable_routes": usable, "measured_routes": len(rev["routes"]),
                        "failed_routes": len(rev["routes"]) - usable,
                        "missing_routes": n - len(rev["routes"])}
            per[gid]["quality_reasons"] = self.quality_issues(gid)
            per[gid]["model_provenance"] = self.model_provenance(gid)
        counts = {s: sum(1 for v in per.values() if v["status"] == s)
                  for s in (COMPLETE, FAILED, INCOMPLETE, MISSING)}
        self.data["coverage"] = {
            "packages": per, "board": {**counts, "total": len(groups)},
            "routes_usable": sum(v["usable_routes"] for v in per.values()),
            "routes_total": sum(len(g["routes"]) for g in groups.values()),
            "ready": len(self.ready_groups()) == len(groups),
            "ready_groups": self.ready_groups(),
        }
        self.data["updated"] = _now()

    def summary_text(self) -> str:
        c = self.data["coverage"]["board"]
        return ("%d of %d packages complete, %d failed, %d incomplete, %d not measured"
                % (c[COMPLETE], c["total"], c[FAILED], c[INCOMPLETE], c[MISSING]))

    # ------------------------------------------------------------------ files
    def _body(self) -> dict:
        body = sanitize(self.data)
        body.pop("content_sha256", None)
        return body

    def content_hash(self) -> str:
        text = json.dumps(self._body(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def electrical_hash(self) -> str:
        """Hash of the electrical inputs only (mapping, acquisition, packages with their raw
        evidence). Calibration records refer to this, so embedding a fitted sensitivity or
        merge bookkeeping later does not make a record look stale; re-measuring does."""
        body = sanitize({"mapping": self.data["mapping"], "acquisition": self.data["acquisition"],
                         "packages": self.data["packages"], "raw_packets": self.data["raw_packets"],
                         "provenance": {k: self.data["provenance"][k]
                                        for k in ("board_type", "board_instance", "origin")}})
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def save(self, path) -> Path:
        """Save the full evidence manifest and its compact SensorParams companion."""
        from . import sensor_params
        path = Path(path)
        companion = sensor_params.companion_path(self, path)
        self.data["schema_version"] = SCHEMA_VERSION
        self._refresh()  # upgrade legacy derived readiness and stale-gain status on explicit save
        body = self._body()
        body["content_sha256"] = self.content_hash()
        text = json.dumps(body, indent=1, allow_nan=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.data["content_sha256"] = body["content_sha256"]
        self.source_path = path
        self.sensor_params_path = sensor_params.write(self, companion)
        return path

    def update_sensor_params(self, group_id, state):
        """Persist a small calibration snapshot, excluded from electrical identity."""
        self.data.setdefault("sensor_params_state", {})[group_id] = sanitize(state)
        if self.source_path is not None:
            self.save(self.source_path)

    @classmethod
    def load(cls, path, group_map: Optional[sensor_groups.GroupMap] = None) -> "ForceInputs":
        """Load and validate (schema, hash, strict JSON, packet hashes, group routes)."""
        def refuse(constant):
            raise ForceExportError("file contains %s: not strict JSON" % constant)

        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=refuse)
        except OSError as exc:
            raise ForceExportError("cannot read %s: %s" % (path, exc)) from exc
        except ValueError as exc:
            if isinstance(exc, ForceExportError):
                raise
            raise ForceExportError("%s is not valid JSON: %s" % (path, exc)) from exc
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise ForceExportError("not a %s file" % FORMAT)
        if data.get("schema_version") not in (1, SCHEMA_VERSION):
            raise ForceExportError("unsupported schema version %r" % data.get("schema_version"))
        stored = data.get("content_sha256")
        self = cls(data)
        if stored != self.content_hash():
            raise ForceExportError("content hash mismatch: the file was modified or damaged")
        for pid, b64 in data.get("raw_packets", {}).items():
            try:
                packet = base64.b64decode(b64, validate=True)
            except ValueError as exc:
                raise ForceExportError("raw packet %s is not valid base64" % pid) from exc
            if hashlib.sha256(packet).hexdigest()[:24] != pid:
                raise ForceExportError("raw packet %s does not match its hash" % pid)
        if data["charge_conversion_model"] != force_model.CHARGE_MODEL:
            raise ForceExportError("unsupported charge conversion model %r"
                                   % data["charge_conversion_model"])
        for gid, revs in data.get("packages", {}).items():
            routes = data["mapping"]["groups"].get(gid, {}).get("routes")
            if routes is None:
                raise ForceExportError("package %s is not in the mapping" % gid)
            expected = {(r["adc"], r["input"]) for r in routes}
            for rev in revs:
                for r in rev["routes"]:
                    if tuple(r["ref"]) not in expected:
                        raise ForceExportError("%s has a route %s outside its mapping"
                                               % (gid, r["ref"]))
        if group_map is not None:
            self.check_mapping(group_map)
        for gid in self.group_ids:
            self.relative_polarity(gid)
        self.source_path = Path(path)
        if self.data.get("sensor_params_export"):
            from .sensor_params import companion_path
            self.sensor_params_path = companion_path(self, path)
        return self

    def check_mapping(self, group_map: sensor_groups.GroupMap) -> None:
        """Raise when the stored layout differs from the current sensor library entry."""
        if self.data["mapping"]["layout_hash"] != group_map.layout_hash:
            raise ForceExportError("the sensor-library layout differs from the one this file was "
                                   "measured with (hash mismatch): review before use")
        for g in group_map.ordered():
            if self.data["mapping"]["groups"].get(g.group_id) != g.to_json():
                raise ForceExportError("group %s routes differ from the file" % g.group_id)

    def raw_packet(self, packet_id: str) -> bytes:
        return base64.b64decode(self.data["raw_packets"][packet_id])

    # ------------------------------------------------------------------ CSV
    def write_csv(self, path) -> Path:
        """Flattened inspection table of the latest revision of every package."""
        rows = []
        for gid in self.group_ids:
            rev = self.latest(gid)
            if rev is None:
                continue
            for r in rev["routes"]:
                rows.append({
                    "group": gid, "revision": rev["revision"], "package_status": rev["status"],
                    "adc": r["ref"][0], "input": r["ref"][1], "position": r["position"],
                    "status": r["status"], "reason": r.get("reason", ""),
                    "model_provenance": r.get("provenance", "measured"),
                    "relative_polarity": self.relative_polarity(gid)[r["position"]],
                    "force_quality_reasons": "; ".join(route_quality(r)),
                    "r_bias_ohm": r["r_bias_ohm"],
                    "c_total_pf": None if r["c_total_f"] is None else r["c_total_f"] * 1e12,
                    "tau_us": None if r["tau_s"] is None else r["tau_s"] * 1e6,
                    "v_d_v": r["v_d_v"], "c0": r["c0"], "c1_per_v": r["c1_per_v"],
                    "qv_mv": None if r["qv_v"] is None else r["qv_v"] * 1e3,
                    "d_min_v": r.get("d_min_v"), "d_max_v": r.get("d_max_v"),
                    "bursts_used": r["bursts_used"], "bursts": r["bursts_total"]})
        path = Path(path)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            if rows:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        return path


def _signature_diff(a: str, b: str) -> str:
    da, db = dict(p.split("=", 1) for p in a.split(";")), dict(p.split("=", 1) for p in b.split(";"))
    diffs = ["%s %s -> %s" % (k, da.get(k), db.get(k)) for k in sorted(set(da) | set(db))
             if da.get(k) != db.get(k)]
    return ", ".join(diffs)


def results_for_group(results: Dict[Channel, object], group: sensor_groups.PztGroup) -> Dict[Channel, object]:
    """The session results that belong to a package's five routes."""
    wanted = set(group.refs)
    return {tuple(ref): r for ref, r in results.items() if tuple(ref) in wanted}


def from_raw_file(path, group_map: sensor_groups.GroupMap, group: sensor_groups.PztGroup,
                  forces: Optional["ForceInputs"] = None, *, board_instance: str = "",
                  settings: Optional[Settings] = None) -> "ForceInputs":
    """Rebuild (or update) a manifest from a saved CapTest raw file: legacy raw files carry the
    packets, so the required coefficients are recomputed (result-only files cannot be used)."""
    from . import runner

    saved, _ = runner.load_raw(path)
    use = settings or saved
    results = {r.ref: r for r in runner.reanalyze(path, use)}
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if forces is None:
        forces = ForceInputs.new(group_map, use, board_instance=board_instance)
    forces.add_package(group, results_for_group(results, group), use, group_map,
                       board_instance=board_instance, source=digest)
    return forces
