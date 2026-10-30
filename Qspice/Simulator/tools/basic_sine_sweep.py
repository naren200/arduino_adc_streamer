"""Force-frequency / sampling-phase sweep of the Basic_Model on the real QSPICE model (headless, no GUI).

    python Qspice/Simulator/tools/basic_sine_sweep.py                  # run the whole sweep (resumes), then write the report
    python Qspice/Simulator/tools/basic_sine_sweep.py --dry-run        # list the cases and the force amplitudes only
    python Qspice/Simulator/tools/basic_sine_sweep.py --freqs 1000 5000 --n-phases 4 --workers 2
    python Qspice/Simulator/tools/basic_sine_sweep.py --report-only    # rebuild summary.csv, figures and report.md from the saved runs

Fixed system: Basic_Model (ideal PZT -> ideal unity buffer -> ideal 12-bit ADC, no noise unless --noise-mv), bias resistor 1 Mohm to ground (VMID = 0 V,
ADC range -1.65 .. +1.65 V), sensor capacitance 100 pF in the circuit and in the force calculation, ADC sampling rate 30 kHz, input force
= 3 cycles of a sine, leakage-compensated force calculation (the default of the Force tab; the only leakage of this model is the bias
resistor), calculated from the ADC-Out value.

Swept: sine frequency 0.1 Hz .. 10 kHz and the sampling phase: the first conversion at 0 .. Ts/4 (10 points, both ends included, Ts = 1/30 kHz)
after the fixed start of the force. The force amplitude of each frequency is chosen so that the steady-state voltage amplitude of the
sensor node is 500 mV: the charge d33 A / Cp the force creates is partly bled off by the bias resistor, a first-order high-pass of corner
1 / (2 pi R Cp) = 1.59 kHz, so  V = d33 A / Cp * w tau / sqrt(1 + (w tau)^2)  and  A = 0.5 V Cp / d33 * sqrt(1 + (w tau)^2) / (w tau).

Metrics (calculated force at the conversion instants against the applied force):
    RMS error      over the three cycles, in % of the force amplitude A
    peak-to-peak   per cycle: (max - min of the calculated force) against the input peak-to-peak 2 A; reported as the largest error of
                   the three cycles, the average of the three and the error of the last cycle (|error| in % of 2 A, the signed values are kept)

Results: Qspice/Simulator/results/basic_model_sine_sweep/ (git-ignored like all of results/)
    runs/f<freq>Hz_ph<k>/samples.npz   conversion instants, ADC code and ADC-Out, sensor voltage, applied and calculated force
                                       (stride-decimated above 40000 conversions; the metrics always use every conversion)
                         meta.json     parameters, timing, metrics, per-cycle peak-to-peak
    summary.csv, summary_by_frequency.csv, figures/*.png, report.md     made by this script (report part)
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import sys
import time
import zlib
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SIM_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SIM_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from qspice_sim import model_loader, runner  # noqa: E402
from qspice_sim.adc_model import build_sample_table  # noqa: E402
from qspice_sim.force_calc import FORCE_DEFAULTS, SRC_ADC_OUT, SRC_IDEAL, compute_all  # noqa: E402

MODEL_DIR = SIM_DIR.parent / "Basic_Model"
OUT_DIR = SIM_DIR / "results" / "basic_model_sine_sweep"
WORK_DIR = SIM_DIR / "runs" / "_basic_sine_sweep"

# ---------------------------------------------------------------------------------------------- fixed system
R_BIAS_OHM = 1e6
CP_PF = 100.0
FS_HZ = 30_000.0
BITS = 12
CYCLES = 3
V_SENSOR_TARGET = 0.5               # steady-state voltage amplitude of the sensor node [V]
FORCE_START_US = 100.0              # 3 sampling periods of zero-force baseline, whatever the sampling phase
TAIL_SAMPLES = 3                    # conversions after the last force cycle
SAVE_MAX_POINTS = 40_000
FREQS_HZ = [0.1, 1.0, 10.0, 100.0, 1000.0, 5000.0, 10_000.0]
N_PHASES = 10
PHASE_SPAN = 0.25                   # of the sampling period


def force_amplitude(freq: float, cp: float, d33: float, r: float, v_target: float = V_SENSOR_TARGET) -> float:
    """Force amplitude [N] that gives a steady-state sensor voltage amplitude v_target through the bias high-pass, 4 significant digits."""
    x = 2 * math.pi * freq * r * cp
    gain = x / math.sqrt(1 + x * x)
    return float(f"{v_target * cp / (d33 * gain):.4g}")


def sensor_amplitude(freq: float, amp_n: float, cp: float, d33: float, r: float) -> float:
    x = 2 * math.pi * freq * r * cp
    return d33 * amp_n / cp * x / math.sqrt(1 + x * x)


@dataclass(frozen=True)
class Case:
    freq: float
    k: int                  # phase index 0 .. n-1
    n_phases: int
    noise_mv: float = 0.0   # RMS of the white (Gaussian) noise added to the ADC input at every conversion

    @property
    def seed(self) -> int:
        """Noise seed of the case: reproducible, and different for every frequency and phase."""
        return zlib.crc32(self.key.encode())

    @property
    def offset_s(self) -> float:
        """Start of the sampling after the start of the force-timing frame: k/(n-1) of PHASE_SPAN sampling periods."""
        return (self.k / max(1, self.n_phases - 1)) * PHASE_SPAN / FS_HZ

    @property
    def key(self) -> str:
        return f"f{self.freq:g}Hz_ph{self.k}"


def make_config(model, case: Case) -> tuple[dict, dict]:
    cfg = model.default_config()
    bm = model.adapter.bm
    cfg["forces"] = []
    cfg["circuit"].update(bias_to="ground", vmid_v=0.0, r_bias_ohm=R_BIAS_OHM)
    cfg["sensor_defaults"]["cp_pF"] = CP_PF
    sp = bm.sensor_params(cfg)
    amp = force_amplitude(case.freq, sp["cp"], sp["d33"], R_BIAS_OHM)
    ts, period = 1.0 / FS_HZ, 1.0 / case.freq
    t_force = FORCE_START_US * 1e-6
    t_run = t_force + CYCLES * period + TAIL_SAMPLES * ts
    cfg["adc"].update(bits=BITS, sampling_rate_hz=FS_HZ, t_first_sample_us=case.offset_s * 1e6)
    cfg["simulation"].update(run_duration_ms=t_run * 1e3, adc_noise_mV=case.noise_mv, noise_seed=case.seed)
    cfg["forces_sin"] = [{"address": 0, "t_start_us": FORCE_START_US, "freq_hz": case.freq, "cycles": float(CYCLES), "amp_N": amp}]
    return cfg, {"amp_N": amp, "t_force_s": t_force, "t_run_s": t_run, "ts_s": ts, "period_s": period,
                 "sensor_amp_expected_V": sensor_amplitude(case.freq, amp, sp["cp"], sp["d33"], R_BIAS_OHM)}


# ---------------------------------------------------------------------------------------------- metrics
def error_metrics(t: np.ndarray, f_cal: np.ndarray, f_in: np.ndarray, freq: float, amp: float, t_force: float, ts: float) -> dict:
    """RMS error and the peak-to-peak errors of the three force cycles (calculated against applied, at the conversion instants)."""
    period = 1.0 / freq
    eps = 1e-3 * ts                         # a conversion on a cycle edge belongs to the cycle that starts there
    win = (t >= t_force - eps) & (t < t_force + CYCLES * period - eps)
    e = (f_cal - f_in)[win]
    pp_in = 2.0 * amp                       # the applied force of a whole cycle: the sine reaches +A and -A
    pp_cal, pp_in_sampled, n_cycle = [], [], []
    for c in range(CYCLES):
        m = (t >= t_force + c * period - eps) & (t < t_force + (c + 1) * period - eps)
        n_cycle.append(int(m.sum()))
        pp_cal.append(float(np.ptp(f_cal[m])) if m.sum() >= 2 else float("nan"))
        pp_in_sampled.append(float(np.ptp(f_in[m])) if m.sum() >= 2 else float("nan"))
    pp_cal, pp_in_sampled = np.array(pp_cal), np.array(pp_in_sampled)
    pp_err = (pp_cal - pp_in) / pp_in * 100.0            # signed, % of the input peak-to-peak
    pp_err_sampled = (pp_cal - pp_in_sampled) / pp_in_sampled * 100.0
    return {
        "n_window": int(win.sum()), "samples_per_cycle": [int(n) for n in n_cycle],
        "rms_err_N": float(np.sqrt(np.mean(e ** 2))), "rms_err_pct": float(np.sqrt(np.mean(e ** 2)) / amp * 100.0),
        "pp_in_N": pp_in, "pp_cal_N": [float(x) for x in pp_cal], "pp_cal_avg_N": float(np.mean(pp_cal)),
        "pp_err_pct": [float(x) for x in pp_err],
        "pp_err_max_pct": float(np.max(np.abs(pp_err))), "pp_err_avg_pct": float(np.mean(np.abs(pp_err))),
        "pp_err_last_pct": float(abs(pp_err[-1])), "pp_err_signed_avg_pct": float(np.mean(pp_err)),
        "pp_err_vs_sampled_input_max_pct": float(np.max(np.abs(pp_err_sampled))),
        "pp_err_vs_sampled_input_avg_pct": float(np.mean(np.abs(pp_err_sampled))),
        "pp_in_sampled_avg_N": float(np.mean(pp_in_sampled)),
    }


# ---------------------------------------------------------------------------------------------- one run
_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        _MODEL = model_loader.load_model(MODEL_DIR)
    return _MODEL


def run_case(case: Case, out_root: Path, qspice: str, keep_work: bool = False) -> dict:
    """Run one case with QSPICE, save samples.npz and meta.json, return the metadata."""
    model = _model()
    bm = model.adapter.bm
    cfg, tm = make_config(model, case)
    case_dir = out_root / "runs" / case.key
    case_dir.mkdir(parents=True, exist_ok=True)
    work = WORK_DIR / f"{case.key}_{os.getpid()}"
    t0 = time.time()
    prepared = runner.prepare(model, cfg)
    info = model.sensor_info(cfg)
    if abs(info[0]["cp_F"] - CP_PF * 1e-12) > 1e-18 or abs(info[0]["leak"]["r_conn_ohm"] - R_BIAS_OHM) > 1e-6:
        raise RuntimeError("the model does not use the 100 pF / 1 Mohm of the sweep")
    raw = runner.run_netlist(prepared.netlist, ["ain", "p0", "f0"], work, Path(qspice))
    try:
        time_pts, ain, p0, f0 = raw.time, raw.column("ain"), raw.column("p0"), raw.column("f0")
        n_points = int(raw.n_points)
    finally:
        raw.close()
    t_s = np.array([s["t"] for s in prepared.samples])
    v_in = np.interp(t_s, time_pts, ain)
    table = build_sample_table(prepared.samples, v_in, model.adc_params(cfg), noise_mv=case.noise_mv, seed=case.seed)
    fin = lambda ch, t: np.interp(t, time_pts, f0)  # noqa: E731
    settings = dict(FORCE_DEFAULTS, cp_pF=CP_PF)
    cal = compute_all(table, [0], info, dict(settings, source=SRC_ADC_OUT), cfg["circuit"]["vmid_v"], fin)[0]
    cal_ideal = compute_all(table, [0], info, dict(settings, source=SRC_IDEAL), cfg["circuit"]["vmid_v"], fin)[0]
    if not cal.leak_used:
        raise RuntimeError("the leakage-compensated force calculation was not applied")
    amp = tm["amp_N"]
    # the applied force at the conversion instants is read from the QSPICE force node (the instants are solver breakpoints); check it
    # against the exact SIN source value
    f_exact = amp * np.sin(2 * np.pi * case.freq * np.clip(t_s - tm["t_force_s"], 0.0, CYCLES / case.freq))   # = bm.sine_value
    fin_dev = float(np.max(np.abs(cal.fin - f_exact)) / amp)
    p0_s = np.interp(t_s, time_pts, p0)
    force_win = (time_pts >= tm["t_force_s"]) & (time_pts <= tm["t_force_s"] + CYCLES / case.freq)
    last = (time_pts >= tm["t_force_s"] + (CYCLES - 1) / case.freq) & (time_pts <= tm["t_force_s"] + CYCLES / case.freq)
    stride = max(1, math.ceil(len(t_s) / SAVE_MAX_POINTS))
    sl = slice(None, None, stride)
    np.savez_compressed(case_dir / "samples.npz", t_s=t_s[sl], adc_code=table.code[sl], adc_out_V=table.v_out[sl], adc_in_V=v_in[sl], noise_V=table.noise_v[sl],
                        sensor_V=p0_s[sl], force_in_N=cal.fin[sl], force_cal_N=cal.F[sl], force_cal_ideal_adc_N=cal_ideal.F[sl])
    st = {"stride": stride}
    meta = {
        "key": case.key, "freq_hz": case.freq, "phase_index": case.k, "phase_offset_us": case.offset_s * 1e6,
        "phase_of_ts": case.offset_s * FS_HZ, "fs_hz": FS_HZ, "oversampling": FS_HZ / case.freq, "cycles": CYCLES,
        "amp_N": amp, "r_bias_ohm": R_BIAS_OHM, "cp_pF": CP_PF, "bits": BITS, "lsb_mV": table.adc.lsb * 1e3,
        "noise_mV": case.noise_mv, "noise_seed": case.seed,
        "noise_rms_measured_mV": float(np.std(table.noise_v) * 1e3) if len(t_s) > 1 else 0.0,
        "n_samples": int(len(t_s)), "n_clipped": int(table.clipped.sum()), "qspice_points": n_points,
        "max_step_us": bm.max_step(cfg) * 1e6, "wall_s": round(time.time() - t0, 2),
        "sensor_amp_expected_V": tm["sensor_amp_expected_V"],
        "sensor_peak_V": float(np.max(np.abs(p0[force_win]))), "sensor_peak_last_cycle_V": float(np.max(np.abs(p0[last]))),
        "fin_deviation_from_sin_pct_of_amp": fin_dev * 100.0, "saved": st, "timing": tm,
        "metrics": error_metrics(cal.t, cal.F, cal.fin, case.freq, amp, tm["t_force_s"], tm["ts_s"]),
        "metrics_ideal_adc": error_metrics(cal_ideal.t, cal_ideal.F, cal_ideal.fin, case.freq, amp, tm["t_force_s"], tm["ts_s"]),
        "warnings": prepared.warnings,
    }
    (case_dir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return meta


def _job(args):
    case, out_root, qspice = args
    return case, run_case(case, Path(out_root), qspice)


# ---------------------------------------------------------------------------------------------- report
def load_runs(root: Path) -> list[dict]:
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((root / "runs").glob("*/meta.json"))]
    if not runs:
        raise SystemExit(f"no runs found in {root / 'runs'}")
    return sorted(runs, key=lambda r: (r["freq_hz"], r["phase_index"]))


def fmt_hz(f: float) -> str:
    return f"{f / 1e3:g} kHz" if f >= 1000 else f"{f:g} Hz"


def fmt_n(x: float) -> str:
    return f"{x:.4g} N" if x < 1e4 else f"{x:.3e} N"


MEASURES = [("rms_err_pct", "RMS error", "% of the force amplitude"),
            ("pp_err_max_pct", "Peak-to-peak error, worst cycle", "% of the input peak-to-peak"),
            ("pp_err_avg_pct", "Peak-to-peak error, average of the cycles", "% of the input peak-to-peak"),
            ("pp_err_last_pct", "Peak-to-peak error, last cycle", "% of the input peak-to-peak")]
SUMMARY_COLS = ["rms_err_pct", "pp_err_max_pct", "pp_err_avg_pct", "pp_err_last_pct", "pp_err_signed_avg_pct", "pp_cal_avg_N",
                "pp_err_vs_sampled_input_max_pct", "pp_err_vs_sampled_input_avg_pct"]


def write_summary(root: Path, runs: list[dict]) -> None:
    head = ["freq_hz", "phase_index", "phase_offset_us", "phase_of_ts", "amp_N", "pp_in_N", "sensor_amp_expected_V", "sensor_peak_V",
            "sensor_peak_last_cycle_V", "oversampling", "n_samples"] + SUMMARY_COLS + ["rms_err_pct_ideal_adc"]
    with (root / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(head)
        for r in runs:
            m = r["metrics"]
            w.writerow([r["freq_hz"], r["phase_index"], f"{r['phase_offset_us']:.6g}", f"{r['phase_of_ts']:.6g}", r["amp_N"], m["pp_in_N"],
                        r["sensor_amp_expected_V"], r["sensor_peak_V"], r["sensor_peak_last_cycle_V"], r["oversampling"], r["n_samples"]]
                       + [m[c] for c in SUMMARY_COLS] + [r["metrics_ideal_adc"]["rms_err_pct"]])
    freqs = sorted({r["freq_hz"] for r in runs})
    with (root / "summary_by_frequency.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        cols = ["rms_err_pct", "pp_err_max_pct", "pp_err_avg_pct", "pp_err_last_pct", "pp_cal_avg_N"]
        w.writerow(["freq_hz", "amp_N", "pp_in_N", "n_phases"] + [f"{c}_{s}" for c in cols for s in ("mean", "min", "max")])
        for f in freqs:
            rr = [r for r in runs if r["freq_hz"] == f]
            row = [f, rr[0]["amp_N"], rr[0]["metrics"]["pp_in_N"], len(rr)]
            for c in cols:
                v = np.array([r["metrics"][c] for r in rr])
                row += [float(v.mean()), float(v.min()), float(v.max())]
            w.writerow(row)


def noise_effect(runs: list[dict], root: Path, base_root: Path) -> dict[float, list[float]]:
    """Per frequency, the rms of (calculated force with noise - calculated force of the noise-free run of the same case) over the force
    window, in % of A: the part of the error that the noise adds (both runs have the same conversion instants)."""
    out: dict[float, list[float]] = {}
    for r in runs:
        p0, p1 = root / "runs" / r["key"] / "samples.npz", base_root / "runs" / r["key"] / "samples.npz"
        if not p1.exists():
            continue
        a, b = np.load(p0), np.load(p1)
        tf, T, ts = r["timing"]["t_force_s"], 1.0 / r["freq_hz"], r["timing"]["ts_s"]
        w = (a["t_s"] >= tf - 1e-3 * ts) & (a["t_s"] < tf + CYCLES * T - 1e-3 * ts)
        out.setdefault(r["freq_hz"], []).append(float(np.sqrt(np.mean((a["force_cal_N"][w] - b["force_cal_N"][w]) ** 2)) / r["amp_N"] * 100))
    return out


def build_report(root: Path, baseline: Path | None = None) -> None:
    import matplotlib.ticker
    import matplotlib.pyplot as plt
    from sampling_sweep_report import CAT, GRID, INK, INK2, RAMP, save, table

    runs = load_runs(root)
    noise_mv = float(runs[0].get("noise_mV", 0.0))
    noise_txt = f"white Gaussian noise {noise_mv:g} mV rms added to the ADC input at every conversion (before the quantisation)" if noise_mv else "no noise"
    base_runs = None
    if baseline is not None and noise_mv and baseline.resolve() != root.resolve() and (baseline / "runs").exists():
        base_runs = load_runs(baseline)
        if float(base_runs[0].get("noise_mV", 0.0)) != 0.0 or {r["key"] for r in base_runs} != {r["key"] for r in runs}:
            base_runs = None          # the comparison needs the noise-free run of the same cases
    write_summary(root, runs)
    freqs = sorted({r["freq_hz"] for r in runs})
    by_f = {f: [r for r in runs if r["freq_hz"] == f] for f in freqs}
    n_ph = len(by_f[freqs[0]])
    cols = [RAMP[i] for i in np.linspace(1, len(RAMP) - 1, len(freqs)).astype(int)]
    xt = dict(xscale="log")

    def stat(f, key, how):
        v = np.array([r["metrics"][key] for r in by_f[f]])
        return {"mean": v.mean, "min": v.min, "max": v.max}[how]()

    def ticks(ax):
        ax.set_xticks(freqs, [fmt_hz(f) for f in freqs], rotation=30, ha="right")
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())

    # 1) error vs input frequency: one panel per measure, line = mean over the phases, band = best..worst phase, dots = each phase
    fig, axes = plt.subplots(2, 2, figsize=(10.5, 7.4), sharex=True)
    for ax, (key, title, unit), c in zip(axes.flat, MEASURES, CAT):
        for f in freqs:
            ax.plot([f] * len(by_f[f]), [r["metrics"][key] for r in by_f[f]], ".", color=c, alpha=0.35, ms=4)
        ax.fill_between(freqs, [stat(f, key, "min") for f in freqs], [stat(f, key, "max") for f in freqs], color=c, alpha=0.15, lw=0)
        ax.plot(freqs, [stat(f, key, "mean") for f in freqs], "o-", color=c, lw=1.8, ms=5)
        ax.set(yscale="log", title=title, ylabel=f"[{unit}]", **xt)
        ticks(ax)
    for ax in axes[1]:
        ax.set_xlabel("force frequency")
    fig.suptitle(f"Force error vs force frequency (line = mean over {n_ph} sampling phases, band = best..worst phase, dots = phases)",
                 fontsize=10, color=INK2)
    p_err = save(fig, root, "error_vs_frequency.png")

    # 2) the four measures in one axes (phase mean)
    fig, ax = plt.subplots(figsize=(7.4, 4.4))
    for (key, title, _), c, mk in zip(MEASURES, CAT, "osv^"):
        ax.plot(freqs, [stat(f, key, "mean") for f in freqs], mk + "-", color=c, lw=1.6, ms=5, label=title)
    ax.set(yscale="log", ylabel="error [%]", xlabel="force frequency", title="All error measures (mean over the phases)", **xt)
    ticks(ax)
    ax.legend(fontsize=8)
    p_all = save(fig, root, "error_measures_overview.png")

    # 3) applied and calculated peak-to-peak (average of the cycles) vs frequency
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(7.4, 7.0), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    pp_in = [by_f[f][0]["metrics"]["pp_in_N"] for f in freqs]
    a1.plot(freqs, pp_in, "o-", color=INK2, lw=1.6, ms=6, label="applied (input) peak-to-peak")
    a1.fill_between(freqs, [stat(f, "pp_cal_avg_N", "min") for f in freqs], [stat(f, "pp_cal_avg_N", "max") for f in freqs],
                    color=CAT[0], alpha=0.2, lw=0)
    a1.plot(freqs, [stat(f, "pp_cal_avg_N", "mean") for f in freqs], "s--", color=CAT[0], lw=1.6, ms=5,
            label="calculated peak-to-peak (cycle average; mean, band = phases)")
    a1.set(yscale="log", ylabel="peak-to-peak force [N]", title="Applied and calculated peak-to-peak force vs frequency", **xt)
    a1.legend(fontsize=8)
    ratio = lambda f, how: stat(f, "pp_cal_avg_N", how) / by_f[f][0]["metrics"]["pp_in_N"]  # noqa: E731
    a2.fill_between(freqs, [ratio(f, "min") for f in freqs], [ratio(f, "max") for f in freqs], color=CAT[0], alpha=0.2, lw=0)
    a2.plot(freqs, [ratio(f, "mean") for f in freqs], "s--", color=CAT[0], lw=1.6, ms=5)
    a2.axhline(1.0, color=INK2, lw=1, ls=":")
    a2.set(xlabel="force frequency", ylabel="calculated / applied", title="Ratio of the average peak-to-peak", **xt)
    ticks(a2)
    p_pp = save(fig, root, "peak_to_peak_vs_frequency.png")

    # 4) sampling-phase dependence
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 3.9))
    for ax, (key, title, unit) in zip(axes, [MEASURES[0], MEASURES[2]]):
        for f, c in zip(freqs, cols):
            ax.plot([r["phase_of_ts"] for r in by_f[f]], [r["metrics"][key] for r in by_f[f]], "o-", color=c, lw=1.4, ms=3.5, label=fmt_hz(f))
        ax.set(yscale="log", xlabel="sampling start [fraction of the sampling period Ts]", ylabel=f"[{unit}]", title=title)
    axes[1].legend(title="force frequency", fontsize=7, ncol=2)
    p_ph = save(fig, root, "phase_dependence.png")

    # 5) waveform examples: applied (line) and calculated (dots) force, normalised to the amplitude, phase 0 and the last phase
    fig, axes = plt.subplots(len(freqs), 1, figsize=(10, 2.0 * len(freqs)))
    for ax, f in zip(np.atleast_1d(axes), freqs):
        r0 = by_f[f][len(by_f[f]) // 2]
        d = np.load(root / "runs" / r0["key"] / "samples.npz")
        tf = r0["timing"]["t_force_s"]
        win = (d["t_s"] >= tf - r0["timing"]["ts_s"]) & (d["t_s"] <= tf + CYCLES_OF(r0) / f + r0["timing"]["ts_s"])
        x = (d["t_s"][win] - tf) * f                               # in force periods
        a = r0["amp_N"]
        ax.plot(x, d["force_in_N"][win] / a, color=INK2, lw=1.2, label="applied")
        ax.plot(x, d["force_cal_N"][win] / a, ".-" if len(x) < 400 else "-", color=CAT[1], lw=1.0, ms=4, label="calculated (ADC-Out)")
        ax.set(ylabel=f"{fmt_hz(f)}\nF / A", xlim=(x[0], x[-1]))
        if f == freqs[0]:
            ax.legend(ncol=2, fontsize=8, loc="upper right")
    np.atleast_1d(axes)[-1].set_xlabel("time since the start of the force [force periods]")
    p_wave = save(fig, root, "waveform_examples.png")

    # 6) with noise vs the noise-free run of the same cases (grey)
    p_cmp, t_cmp = None, ""
    if base_runs is not None:
        bf = {f: [r for r in base_runs if r["freq_hz"] == f] for f in freqs}
        eff = noise_effect(runs, root, baseline)
        fig, axes = plt.subplots(2, 2, figsize=(10.5, 7.4), sharex=True)
        for ax, (key, title, unit), c in zip(axes.flat, MEASURES, CAT):
            for rr, col, lab in ((bf, INK2, "no noise"), (by_f, c, f"{noise_mv:g} mV rms noise")):
                lo = [min(r["metrics"][key] for r in rr[f]) for f in freqs]
                hi = [max(r["metrics"][key] for r in rr[f]) for f in freqs]
                ax.fill_between(freqs, lo, hi, color=col, alpha=0.13, lw=0)
                ax.plot(freqs, [np.mean([r["metrics"][key] for r in rr[f]]) for f in freqs], "o-", color=col, lw=1.7, ms=5, label=lab)
            ax.set(yscale="log", title=title, ylabel=f"[{unit}]", **xt)
            ticks(ax)
        axes[0, 0].plot(freqs, [np.mean(eff[f]) if f in eff else np.nan for f in freqs], "^:", color=INK, lw=1.3, ms=4,
                        label="noise only (noisy - noise-free calculation)")
        axes[0, 0].legend(fontsize=8)
        for ax in axes[1]:
            ax.set_xlabel("force frequency")
        fig.suptitle("Error with noise and without (line = mean over the phases, band = best..worst phase)", fontsize=10, color=INK2)
        p_cmp = save(fig, root, "noise_comparison.png")
        rows = []
        for f in freqs:
            row = [fmt_hz(f)]
            for key in ("rms_err_pct", "pp_err_avg_pct"):
                n0 = np.mean([r["metrics"][key] for r in bf[f]])
                n1 = np.mean([r["metrics"][key] for r in by_f[f]])
                row += [f"{n0:.3g}", f"{n1:.3g}", f"{n1 / n0:.3g}x"]
            row.append(f"{np.mean(eff[f]):.3g}" if f in eff else "")
            rows.append(row)
        t_cmp = table(rows, ["force frequency", "RMS, no noise [%]", "RMS, noise [%]", "ratio", "p-p avg, no noise [%]", "p-p avg, noise [%]",
                             "ratio", "noise-only change of the force, rms [% of A]"])

    # report.md
    rows = []
    for f in freqs:
        rr = by_f[f]
        m = rr[0]["metrics"]
        sa = np.mean([r["sensor_peak_last_cycle_V"] for r in rr]) * 1e3
        rows.append([fmt_hz(f), fmt_n(rr[0]["amp_N"]), f"{rr[0]['sensor_amp_expected_V'] * 1e3:.0f}", f"{sa:.0f}", f"{FS_HZ / f:,.0f}",
                     f"{rr[0]['n_samples']:,}"])
    t_setup = table(rows, ["force frequency", "force amplitude", "sensor amplitude, expected [mV]", "sensor peak, last cycle [mV]",
                           "samples / force period", "conversions in the run"])

    def cell(f, key):
        v = np.array([r["metrics"][key] for r in by_f[f]])
        return f"{v.mean():.3g} ({v.min():.3g} .. {v.max():.3g})"

    t_err = table([[fmt_hz(f)] + [cell(f, k) for k, _, _ in MEASURES] for f in freqs],
                  ["force frequency"] + ["RMS [% of A]", "p-p worst cycle [%]", "p-p cycle average [%]", "p-p last cycle [%]"])
    t_pp = table([[fmt_hz(f), fmt_n(by_f[f][0]["metrics"]["pp_in_N"])]
                  + [f"{stat(f, 'pp_cal_avg_N', h):.4g}" for h in ("mean", "min", "max")]
                  + [f"{stat(f, 'pp_cal_avg_N', 'mean') / by_f[f][0]['metrics']['pp_in_N']:.4f}",
                     f"{np.mean([r['metrics']['pp_err_signed_avg_pct'] for r in by_f[f]]):+.3g}"] for f in freqs],
                 ["force frequency", "applied p-p", "calculated p-p, mean [N]", "min [N]", "max [N]", "calc / applied", "signed p-p error [%]"])
    def cell_alg(f, key, method="metrics"):
        v = np.array([r[method][key] for r in by_f[f]])
        return f"{v.mean():.3g} ({v.max():.3g})"

    t_alg = table([[fmt_hz(f), cell(f, "pp_err_avg_pct").split(" ")[0], cell_alg(f, "pp_err_vs_sampled_input_avg_pct"),
                    cell(f, "rms_err_pct").split(" ")[0], cell_alg(f, "rms_err_pct", "metrics_ideal_adc")] for f in freqs],
                  ["force frequency", "p-p error vs the true 2 A [%], mean", "p-p error vs the input at the conversions [%], mean (worst phase)",
                   "RMS, ADC-Out [%], mean", "RMS, ideal ADC [%], mean (worst phase)"])
    notes = root / "notes.md"
    clip = max(r["n_clipped"] for r in runs)
    cmp_md = ""
    if p_cmp:
        cmp_md = (f"## With noise vs without\n\nThe same {len(runs)} cases as the noise-free run (`{baseline.name}`): same force, same conversion instants, "
                  f"the noise is the only difference. The last column is the rms of (calculated force with noise - calculated force "
                  f"without noise), i.e. what the noise alone does to the force.\n\n{t_cmp}\n\n![noise comparison]({p_cmp})\n\n")
    text = f"""# Basic_Model: force-frequency x sampling-phase sweep{f', {noise_mv:g} mV rms ADC noise' if noise_mv else ''}

Generated by `Qspice/Simulator/tools/basic_sine_sweep.py` from {len(runs)} QSPICE runs ({len(freqs)} frequencies x {n_ph} sampling phases).

## Setup (fixed)

| | |
|---|---|
| Model | Basic_Model: ideal PZT (charge source d33 x dF/dt, 600 pC/N) in parallel with Cp, ideal unity buffer, ideal ADC |
| Bias resistor | {R_BIAS_OHM / 1e6:g} Mohm to ground, VMID = 0 V (time constant R x Cp = {R_BIAS_OHM * CP_PF * 1e-12 * 1e6:g} us, corner {1 / (2 * math.pi * R_BIAS_OHM * CP_PF * 1e-12):,.0f} Hz) |
| Sensor capacitance | {CP_PF:g} pF in the circuit and in the force calculation |
| ADC | {BITS} bit, range -1.65 .. +1.65 V (LSB {runs[0]['lsb_mV']:.3f} mV), {noise_txt}, sampling rate {FS_HZ / 1e3:g} kHz fixed, {max(r['n_clipped'] for r in runs)} clipped conversions |
| Force | {CYCLES} cycles of a sine (QSPICE `SIN` source), start {FORCE_START_US:g} us after the first possible conversion |
| Force calculation | leakage-compensated (default of the Force tab), from the ADC-Out value, reference VMID |
| Sampling phase | first conversion {PHASE_SPAN:g} x Ts later in {n_ph} equal steps: 0 .. {PHASE_SPAN * 1e6 / FS_HZ:.3f} us (Ts = {1e6 / FS_HZ:.3f} us); the force start does not move |

### Force amplitude

The amplitude of each frequency was chosen so that the steady-state voltage amplitude of the sensor node is {V_SENSOR_TARGET * 1e3:g} mV, with the charge
leaked through the bias resistor taken into account: `V = d33 A / Cp x w tau / sqrt(1 + (w tau)^2)`, so
`A = {V_SENSOR_TARGET:g} V x Cp / d33 x sqrt(1 + (w tau)^2) / (w tau)`. Below the 1.6 kHz corner the resistor bleeds the charge off as fast as the force
creates it, so a slow force has to be very large for the same voltage: the table shows it (0.1 Hz needs {fmt_n(by_f[freqs[0]][0]['amp_N'])}, which is a
simulation value, not a physical load).

{t_setup}

## Results

Values are the mean over the {n_ph} sampling phases, with the best .. worst phase in brackets. The peak-to-peak error compares the peak-to-peak
of the calculated force of one cycle (max - min of the conversions inside that cycle) with the input peak-to-peak 2 A, relative to 2 A.

{t_err}

Average peak-to-peak (the second requested graph):

{t_pp}

### Where the error comes from

The peak-to-peak error above is measured against the true input peak-to-peak 2 A, so it also contains the peaks that fall between two
conversions (3 conversions per period at 10 kHz cannot reach +A and -A). The first column repeats it, the second compares the calculated
peak-to-peak with the peak-to-peak of the applied force *at the conversion instants* (what the sampling could at best show), so it is the
error of the force calculation alone. The RMS columns compare the ADC-Out value with an ideal ADC input (no quantisation{', no noise' if noise_mv else ''}).

{t_alg}

{cmp_md}## Figures

- `{p_err}`: the four error measures vs force frequency (RMS, worst-cycle / average / last-cycle peak-to-peak), mean, best..worst phase and every phase.
- `{p_all}`: the four measures in one axes.
- `{p_pp}`: applied and calculated peak-to-peak (average of the cycles) vs frequency, and their ratio.
- `{p_ph}`: error vs sampling phase for each frequency.
- `{p_wave}`: applied and calculated force of one phase, normalised to the amplitude.
{f"- `{p_cmp}`: the four error measures with and without noise." if p_cmp else ""}

![error vs frequency]({p_err})
![overview]({p_all})
![peak-to-peak]({p_pp})
![phase]({p_ph})
![waveforms]({p_wave})

Files: `summary.csv` (every run), `summary_by_frequency.csv` (mean / min / max over the phases), `runs/<case>/` (samples.npz, meta.json).
"""
    if notes.exists():
        text += "\n## Notes\n\n" + notes.read_text(encoding="utf-8").strip() + "\n"
    (root / "report.md").write_text(text, encoding="utf-8")
    print(f"report: {root / 'report.md'}  ({len(runs)} runs, clipped conversions: {clip})")


def CYCLES_OF(r: dict) -> int:
    return int(r["cycles"])


# ---------------------------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--noise-mv", type=float, default=0.0,
                    help="RMS of white Gaussian noise added to the ADC input at every conversion [mV] (default 0; the seed differs per case)")
    ap.add_argument("--out", default=None, help="result folder (default: results/basic_model_sine_sweep, with noise: "
                                                "results/basic_model_sine_sweep_noise_<x>mV; a folder never mixes noise levels)")
    ap.add_argument("--baseline", default=str(OUT_DIR), help="noise-free run that the report of a noisy run is compared with")
    ap.add_argument("--freqs", type=float, nargs="+", default=FREQS_HZ, help="force frequencies [Hz]")
    ap.add_argument("--n-phases", type=int, default=N_PHASES, help="sampling phases between 0 and Ts/4 (ends included)")
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1), help="parallel QSPICE processes")
    ap.add_argument("--qspice", default=None, help="path of QSPICE64.exe")
    ap.add_argument("--force", action="store_true", help="re-run cases that already have a meta.json")
    ap.add_argument("--dry-run", action="store_true", help="list the cases and stop")
    ap.add_argument("--report-only", action="store_true", help="only rebuild summary.csv, figures and report.md")
    ap.add_argument("--no-report", action="store_true", help="do not write the report after the runs")
    args = ap.parse_args()
    if args.noise_mv < 0:
        ap.error("--noise-mv must not be negative")
    out_root = Path(args.out) if args.out else (OUT_DIR if not args.noise_mv else
                                                OUT_DIR.with_name(f"{OUT_DIR.name}_noise_{args.noise_mv:g}mV".replace(".", "p")))

    if not args.report_only:
        cases = [Case(f, k, args.n_phases, args.noise_mv) for f in args.freqs for k in range(args.n_phases)]
        for c in cases:                                           # never mix noise levels in one folder, never overwrite another run
            meta = out_root / "runs" / c.key / "meta.json"
            if meta.exists() and float(json.loads(meta.read_text(encoding="utf-8")).get("noise_mV", 0.0)) != args.noise_mv:
                ap.error(f"{out_root} already holds runs with another noise level; use --out for a new folder")
        todo = [c for c in cases if args.force or not (out_root / "runs" / c.key / "meta.json").exists()]
        print(f"result folder: {out_root}   noise: {args.noise_mv:g} mV rms")
        model = _model()
        sp = model.adapter.bm.sensor_params(make_config(model, cases[0])[0])
        print(f"{len(cases)} cases ({len(cases) - len(todo)} already done, {len(todo)} to run); sampling offsets "
              f"{', '.join(f'{Case(args.freqs[0], k, args.n_phases).offset_s * 1e6:.3f}' for k in range(args.n_phases))} us")
        for f in args.freqs:
            a = force_amplitude(f, sp["cp"], sp["d33"], R_BIAS_OHM)
            print(f"  {f:>8g} Hz: amplitude {a:.5g} N -> sensor {sensor_amplitude(f, a, sp['cp'], sp['d33'], R_BIAS_OHM) * 1e3:.1f} mV, "
                  f"{FS_HZ / f:g} samples/period, {FS_HZ * CYCLES / f:,.0f} conversions")
        if args.dry_run:
            return 0
        qspice = str(runner.find_qspice(args.qspice))
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        t_start = time.time()
        failures = []
        todo.sort(key=lambda c: c.freq)             # the slow forces (long runs) first: they set the end of the sweep
        with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(_job, (c, str(out_root), qspice)): c for c in todo}
            for n, fut in enumerate(as_completed(futures), 1):
                c = futures[fut]
                try:
                    _, meta = fut.result()
                    m = meta["metrics"]
                    print(f"[{n}/{len(todo)}] {c.key}: rms {m['rms_err_pct']:.3g} %  p-p avg {m['pp_err_avg_pct']:.3g} %  "
                          f"sensor {meta['sensor_peak_last_cycle_V'] * 1e3:.0f} mV  ({meta['wall_s']} s, {meta['qspice_points']} pts, "
                          f"{(time.time() - t_start) / 60:.1f} min)", flush=True)
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
        build_report(out_root, Path(args.baseline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
