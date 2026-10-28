"""Sampling-rate / input-frequency / phase sweep of TestBoard_ADC124 on the real QSPICE model (headless, no GUI).

    python Qspice/Simulator/tools/sampling_sweep.py                  # run the whole sweep (resumes), then write the report
    python Qspice/Simulator/tools/sampling_sweep.py --dry-run        # list the cases only
    python Qspice/Simulator/tools/sampling_sweep.py --freqs 1000 10000 --phases 0 0.25 --workers 4
    python Qspice/Simulator/tools/sampling_sweep.py --report-only    # rebuild summary.csv, figures and report.md from the saved runs

Fixed system: one mux, channel 0 only, continuous sampling (no VMID visit between samples, no park on VMID, so the mux never
switches after the first connection), sensor capacitance overridden to 150 pF in the circuit and in the force calculation, input
force = 3 cycles of a sine of 0.2 N, 12-bit ADC without noise, leakage-compensated force calculation (the default of the Force tab).

Swept: sine frequency, ADC sampling rate (never below the Nyquist rate 2 f) and the phase between the sampling grid and the sine,
0 .. T/4 in steps of T/16. The sampling grid is the fixed ADC schedule; the force starts `3 samples + (phase * T mod Ts)` after the first
conversion (a delay of the force is the same as a later start of the sampling and keeps the connection transient at the same place;
whole sampling periods of delay do not move the samples on the sine, and would only lengthen the zero-force lead-in, in which the
calculated force drifts, see --literal-phase).

Results: Qspice/Simulator/results/sampling_sweep/ (git-ignored like all of results/)
    runs/f<freq>Hz_fs<rate>Hz_ph<phase>/samples.csv    one row per conversion: ADC input, code, ADC output, sensor voltage, input force, calculated force
                                                       (with leakage compensation, from the ideal ADC value, with the leakage currents set to 0, without compensation)
                                        waveform.npz   time, sensor output s0, ADC input ain, input force f0 (QSPICE time points)
                                        meta.json      parameters, timing, error metrics
    summary.csv, figures/*.png, report.md              made by sampling_sweep_report.py
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SIM_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SIM_DIR))

from qspice_sim import model_loader, runner  # noqa: E402
from qspice_sim.adc_model import build_sample_table  # noqa: E402
from qspice_sim.force_calc import FORCE_DEFAULTS, SRC_ADC_OUT, SRC_IDEAL, compute_all  # noqa: E402

MODEL_DIR = SIM_DIR.parent / "TestBoard_ADC124"
OUT_DIR = SIM_DIR / "results" / "sampling_sweep"
WORK_DIR = SIM_DIR / "runs" / "_sweep"

# ---------------------------------------------------------------------------------------------- fixed system
AMP_N = 0.2
CYCLES = 3
CP_PF = 150.0
BITS = 12
T_START_US = 20.0           # ADC start (as in the saved settings of the model)
LEAD_SAMPLES = 3            # conversions before the force starts (zero-force baseline)
MIN_TAIL_S = 200e-6         # observation after the last force cycle: at least this long and at least 3 sampling periods
TAIL_SAMPLES = 3

# ---------------------------------------------------------------------------------------------- swept values
FREQS_HZ = [10.0, 100.0, 1000.0, 10_000.0, 20_000.0]
BASE_RATES_HZ = [5e3, 10e3, 15e3, 20e3, 25e3, 30e3]
EXTRA_RATES_HZ = {10_000.0: [40e3, 50e3], 20_000.0: [40e3, 50e3, 60e3]}     # added where the base rates stop at Nyquist
PHASES_T = [0.0, 1 / 16, 2 / 16, 3 / 16, 4 / 16]    # fraction of the sine period T (0 .. T/4)
NYQUIST = 2.0


def rates_for(freq: float) -> list[float]:
    """Sampling rates of one input frequency: the base rates that satisfy Nyquist (>= 2 f) plus the extra ones listed for it.

    up to 5 kHz -> 5 .. 30 kHz;  10 kHz -> 20, 25, 30, 40, 50 kHz;  20 kHz -> 40, 50, 60 kHz.
    """
    return [r for r in BASE_RATES_HZ if r >= NYQUIST * freq - 1e-9] + EXTRA_RATES_HZ.get(freq, [])


def max_step_ns(freq: float) -> float:
    """Solver step: T/20000 limited to 5 ns .. 1 us (the mux switches only once, so the sine sets the step; 500 ns at 1 kHz changed the
    rms error by 0.02 % of the amplitude, 50 ns agrees with 5 ns)."""
    return float(min(1000.0, max(5.0, 1e9 / freq / 20000.0)))


@dataclass(frozen=True)
class Case:
    freq: float
    fs: float
    phase: float            # fraction of the sine period

    @property
    def key(self) -> str:
        return f"f{self.freq:g}Hz_fs{self.fs:g}Hz_ph{self.phase:.4f}"


def all_cases(freqs: list[float], phases: list[float], rates: list[float] | None) -> list[Case]:
    out = []
    for f in freqs:
        for fs in (rates_for(f) if rates is None else [r for r in rates if r >= NYQUIST * f - 1e-9]):
            out += [Case(f, fs, p) for p in phases]
    return out


# ---------------------------------------------------------------------------------------------- one run
def grid_offset(delay: float, ts: float) -> float:
    """The part of a delay that moves the samples on the sine: the delay modulo the sampling period (whole periods change nothing)."""
    n = delay / ts
    frac = n - round(n) if abs(n - round(n)) < 1e-6 else n - math.floor(n)
    return frac * ts


def make_config(model, case: Case, max_step: float | None = None, literal_phase: bool = False) -> tuple[dict, dict]:
    """Model configuration of a case and the timing facts derived from it (first conversion, force start, run length)."""
    cfg = model.default_config()
    cfg["forces"] = []
    cfg["sensor_defaults"]["cp_pF"] = CP_PF
    cfg["adc"]["bits"] = BITS
    cfg["simulation"].update(adc_noise_mV=0.0, max_step_ns=max_step_ns(case.freq) if max_step is None else max_step,
                             periodic_min_cycles=1)     # periodic control sources: exact conversion instants, small netlist
    t = cfg["timing"]
    t.update(scan_order=[0], n_mux=1, repeat=1, vmid_visit_after_each=False, park_on_vmid=False, t_start_us=T_START_US,
             sampling_rate_hz=case.fs, run_duration_s=1e-3)
    bm = model.adapter.bm
    slots, _, _ = bm.build_schedule(cfg)                 # only to find the first conversion; the run length is set below
    ts = 1.0 / case.fs
    period = 1.0 / case.freq
    t_first = slots[0]["t_sample"]
    delay = case.phase * period
    t_force = t_first + LEAD_SAMPLES * ts + (delay if literal_phase else grid_offset(delay, ts))
    t_force_end = t_force + CYCLES * period
    t_run = t_force_end + max(MIN_TAIL_S, TAIL_SAMPLES * ts)
    t["run_duration_s"] = t_run
    cfg["forces_sin"] = [{"address": 0, "t_start_us": t_force * 1e6, "freq_hz": case.freq, "cycles": float(CYCLES), "amp_N": AMP_N}]
    return cfg, {"t_first_s": t_first, "t_force_s": t_force, "t_force_end_s": t_force_end, "t_run_s": t_run, "ts_s": ts, "period_s": period}


def sine_fit(t: np.ndarray, y: np.ndarray, freq: float, t0: float) -> tuple[float, float] | None:
    """(amplitude, phase [deg]) of y = a sin(w (t - t0) + phi) + c at the known frequency; None when the samples cannot separate them.

    With exactly two samples per period the sine and cosine columns are proportional, so amplitude and phase are not determined.
    """
    w = 2 * math.pi * freq
    x = np.column_stack([np.sin(w * (t - t0)), np.cos(w * (t - t0)), np.ones(len(t))])
    if len(t) < 4 or np.linalg.matrix_rank(x, tol=1e-6 * np.linalg.norm(x, 2)) < 3:
        return None
    (a, b, _), *_ = np.linalg.lstsq(x, y, rcond=None)
    return float(math.hypot(a, b)), float(math.degrees(math.atan2(b, a)))


def error_metrics(t: np.ndarray, f_cal: np.ndarray, f_in: np.ndarray, freq: float, t_force: float, t_force_end: float) -> dict:
    """Error of the calculated force at the conversion instants, over the force window and over the whole run."""
    win = (t >= t_force) & (t <= t_force_end)
    err = f_cal - f_in
    e = err[win]
    out = {"n_window": int(win.sum()),
           "rms_err_N": float(np.sqrt(np.mean(e ** 2))), "rms_err_pct": float(np.sqrt(np.mean(e ** 2)) / AMP_N * 100),
           "max_err_N": float(np.max(np.abs(e))), "max_err_pct": float(np.max(np.abs(e)) / AMP_N * 100),
           "mean_err_N": float(np.mean(e)),
           "peak_cal_N": float(np.max(np.abs(f_cal[win]))), "peak_in_N": float(np.max(np.abs(f_in[win]))),
           "end_err_N": float(err[-1]), "max_err_all_N": float(np.max(np.abs(err))),
           "rms_baseline_N": float(np.sqrt(np.mean(err[t < t_force] ** 2))) if np.any(t < t_force) else float("nan")}
    out["peak_ratio"] = out["peak_cal_N"] / AMP_N
    fit = sine_fit(t[win], f_cal[win], freq, t_force)
    out["fit_gain"], out["fit_phase_deg"] = (fit[0] / AMP_N, fit[1]) if fit else (float("nan"), float("nan"))
    return out


_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        _MODEL = model_loader.load_model(MODEL_DIR)
    return _MODEL


def run_case(case: Case, out_root: Path, qspice: str, max_step: float | None = None, literal_phase: bool = False,
             keep_work: bool = False) -> dict:
    """Run one case with QSPICE, save samples.csv, waveform.npz and meta.json, return the metadata."""
    model = _model()
    cfg, timing = make_config(model, case, max_step, literal_phase)
    case_dir = out_root / "runs" / case.key
    case_dir.mkdir(parents=True, exist_ok=True)
    work = WORK_DIR / f"{case.key}_{os.getpid()}"
    t0 = time.time()
    prepared = runner.prepare(model, cfg)
    info = model.sensor_info(cfg)
    if abs(info[0]["cp_F"] - CP_PF * 1e-12) > 1e-18:
        raise RuntimeError(f"sensor capacitance is {info[0]['cp_F']} F, expected the {CP_PF} pF override")
    raw = runner.run_netlist(prepared.netlist, ["ain", "s0", "f0"], work, Path(qspice))
    try:
        time_pts = raw.time
        ain, s0, f0 = raw.column("ain"), raw.column("s0"), raw.column("f0")
        n_points = int(raw.n_points)
    finally:
        raw.close()
    t_s = np.array([s["t"] for s in prepared.samples])
    v_in = np.interp(t_s, time_pts, ain)
    table = build_sample_table(prepared.samples, v_in, model.adc_params(cfg))
    vmid = cfg["circuit"]["vmid_v"]
    fin = lambda ch, t: np.interp(t, time_pts, f0)  # noqa: E731
    settings = dict(FORCE_DEFAULTS, cp_pF=CP_PF)         # the calculated force uses the same 150 pF explicitly
    cal = compute_all(table, [0], info, dict(settings, source=SRC_ADC_OUT), vmid, fin)[0]
    cal_ideal = compute_all(table, [0], info, dict(settings, source=SRC_IDEAL), vmid, fin)[0]
    # leakage test: the same ADC values, force calculated (a) with the leakage currents set to 0 (the bias-resistor decay and the
    # charge sharing are still compensated) and (b) without any compensation (the ideal charge model, Force tab checkbox off)
    cal_no_i = compute_all(table, [0], info, dict(settings, i_conn_nA=0.0, i_float_nA=0.0), vmid, fin)[0]
    cal_no_comp = compute_all(table, [0], info, dict(settings, leak_comp=False), vmid, fin)[0]
    if not cal.leak_used:
        raise RuntimeError("the leakage-compensated force calculation was not applied")
    f_in = cal.fin
    s0_at = np.interp(t_s, time_pts, s0)
    cols = ["t_s", "adc_in_V", "adc_code", "adc_out_V", "sensor_s0_V", "force_in_N", "force_cal_N", "force_cal_ideal_adc_N",
            "force_cal_no_leak_current_N", "force_cal_no_compensation_N"]
    rows = np.column_stack([t_s, v_in, table.code, table.v_out, s0_at, f_in, cal.F, cal_ideal.F, cal_no_i.F, cal_no_comp.F])
    np.savetxt(case_dir / "samples.csv", rows, delimiter=",", header=",".join(cols), comments="",
               fmt=["%.9e", "%.9e", "%d", "%.9e", "%.9e", "%.9e", "%.9e", "%.9e", "%.9e", "%.9e"])
    np.savez_compressed(case_dir / "waveform.npz", t=time_pts, sensor_s0=s0.astype(np.float32), adc_in=ain.astype(np.float32),
                        force_in=f0.astype(np.float32))
    meta = {
        "key": case.key, "freq_hz": case.freq, "fs_hz": case.fs, "phase_T": case.phase, "phase_deg": case.phase * 360.0,
        "oversampling": case.fs / case.freq, "cycles": CYCLES, "amp_N": AMP_N, "cp_pF": CP_PF, "bits": BITS,
        "lsb_mV": table.adc.lsb * 1e3, "n_samples": int(len(t_s)), "n_clipped": int(table.clipped.sum()),
        "grid_phase": float(grid_offset(case.phase * timing["period_s"], timing["ts_s"]) / timing["ts_s"]),   # force offset inside one sampling period
        "literal_phase": literal_phase,
        "max_step_ns": cfg["simulation"]["max_step_ns"], "qspice_points": n_points, "wall_s": round(time.time() - t0, 2),
        "timing": timing,
        "metrics": error_metrics(cal.t, cal.F, f_in, case.freq, timing["t_force_s"], timing["t_force_end_s"]),
        "metrics_ideal_adc": error_metrics(cal_ideal.t, cal_ideal.F, f_in, case.freq, timing["t_force_s"], timing["t_force_end_s"]),
        "metrics_no_leak_current": error_metrics(cal_no_i.t, cal_no_i.F, f_in, case.freq, timing["t_force_s"], timing["t_force_end_s"]),
        "metrics_no_compensation": error_metrics(cal_no_comp.t, cal_no_comp.F, f_in, case.freq, timing["t_force_s"], timing["t_force_end_s"]),
        "warnings": prepared.warnings,
    }
    (case_dir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return meta


def _job(args):
    case, out_root, qspice, max_step, literal = args
    return case, run_case(case, Path(out_root), qspice, max_step, literal)


# ---------------------------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(OUT_DIR), help="result folder (default: results/sampling_sweep)")
    ap.add_argument("--freqs", type=float, nargs="+", default=FREQS_HZ, help="sine frequencies [Hz]")
    ap.add_argument("--rates", type=float, nargs="+", default=None, help="sampling rates [Hz] (default: the per-frequency lists, never below 2 f)")
    ap.add_argument("--phases", type=float, nargs="+", default=PHASES_T, help="phases as a fraction of the sine period T (0 .. 0.25)")
    ap.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1), help="parallel QSPICE processes")
    ap.add_argument("--max-step-ns", type=float, default=None, help="override the solver maximum step")
    ap.add_argument("--qspice", default=None, help="path of QSPICE64.exe")
    ap.add_argument("--literal-phase", action="store_true",
                    help="delay the force by the full phase x T instead of its part inside one sampling period (the zero-force lead-in then "
                         "grows with the phase, and so does the drift of the calculated force)")
    ap.add_argument("--force", action="store_true", help="re-run cases that already have a meta.json")
    ap.add_argument("--dry-run", action="store_true", help="list the cases and stop")
    ap.add_argument("--report-only", action="store_true", help="only rebuild summary.csv, figures and report.md")
    ap.add_argument("--no-report", action="store_true", help="do not write the report after the runs")
    args = ap.parse_args()
    out_root = Path(args.out)

    if not args.report_only:
        if any(not 0.0 <= p <= 0.25 + 1e-9 for p in args.phases):
            ap.error("phases are fractions of T between 0 and 0.25")
        cases = all_cases(args.freqs, args.phases, args.rates)
        todo = [c for c in cases if args.force or not (out_root / "runs" / c.key / "meta.json").exists()]
        by_freq = {f: sorted({c.fs for c in cases if c.freq == f}) for f in args.freqs}
        print(f"{len(cases)} cases ({len(cases) - len(todo)} already done, {len(todo)} to run)")
        for f, rs in by_freq.items():
            print(f"  {f:>8g} Hz: {', '.join(f'{r / 1e3:g}' for r in rs)} kHz x {len(args.phases)} phases")
        if args.dry_run:
            return 0
        qspice = str(runner.find_qspice(args.qspice))
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        t_start = time.time()
        failures = []
        with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(_job, (c, str(out_root), qspice, args.max_step_ns, args.literal_phase)): c for c in todo}
            for n, fut in enumerate(as_completed(futures), 1):
                c = futures[fut]
                elapsed = time.time() - t_start
                try:
                    _, meta = fut.result()
                    m = meta["metrics"]
                    print(f"[{n}/{len(todo)}] {c.key}: rms {m['rms_err_pct']:.2f} %  max {m['max_err_pct']:.2f} %  "
                          f"({meta['wall_s']} s, {meta['qspice_points']} pts)  eta {elapsed / n * (len(todo) - n) / 60:.1f} min", flush=True)
                except Exception as exc:  # noqa: BLE001 - keep the sweep going, list the failures at the end
                    failures.append((c.key, str(exc)))
                    print(f"[{n}/{len(todo)}] {c.key}: FAILED {exc}", flush=True)
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        print(f"sweep finished in {(time.time() - t_start) / 60:.1f} min, {len(failures)} failure(s)")
        for key, msg in failures:
            print(f"  {key}: {msg}")
        if failures:
            return 1
    if not args.no_report:
        import sampling_sweep_report
        sampling_sweep_report.build_report(out_root)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
