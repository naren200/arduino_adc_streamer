"""Fixed-amplitude force-frequency x random-sampling-phase test of the Basic_Model on the real QSPICE model (headless, no GUI).

    python Qspice/Simulator/tools/basic_fixed_force_sweep.py                       # new time-stamped result folder, run, report
    python Qspice/Simulator/tools/basic_fixed_force_sweep.py --dry-run             # list the cases and the 20 phases only
    python Qspice/Simulator/tools/basic_fixed_force_sweep.py --out <folder>        # resume / extend a folder
    python Qspice/Simulator/tools/basic_fixed_force_sweep.py --out <folder> --report-only

Fixed system: Basic_Model (ideal PZT -> ideal unity buffer -> ideal 12-bit ADC), bias resistor 20 Mohm to ground (VMID = 0 V, ADC range
-1.65 .. +1.65 V), sensor capacitance 100 pF in the circuit and in the force calculation, white Gaussian noise 0.4 mV rms added to the ADC
input at every conversion (before the quantisation), ADC sampling rate 30 kHz, input force = 2 cycles of a sine of the SAME amplitude (0.25 N)
at every frequency, leakage-compensated force calculation (default of the Force tab) from the ADC-Out value.

Swept: sine frequency 0.1 Hz .. 10 kHz and the sampling phase: 20 random first-conversion times in [0, Ts/2) after the fixed start of the
force, none within PHASE_GUARD (0.01 Ts) of exactly 0.25 Ts or 0.5 Ts (the same 20 phases for every frequency; seeded, so a folder can
be extended or the report rebuilt).

Metrics (calculated force at the conversion instants against the applied force):
    RMS error        over the two cycles, in % of the force amplitude A
    peak-to-peak     last cycle: (max - min of the calculated force) against the input peak-to-peak 2 A, relative to 2 A (|error|, signed kept)
Statistics over the 20 phases: median and standard deviation (sample, n - 1).

Results: Qspice/Simulator/results/basic_model_fixed_force_R20M_noise0p4mV_<YYYYMMDD_HHMMSS>/ (git-ignored like all of results/)
    runs/f<freq>Hz_ph<kk>/samples.npz, meta.json     one QSPICE run each (samples stride-decimated above 40000 conversions)
    setup.json, summary.csv, summary_by_frequency.csv, figures/*.png, report.md
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
from datetime import datetime
from pathlib import Path

import numpy as np

SIM_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SIM_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from qspice_sim import model_loader, runner  # noqa: E402
from qspice_sim.adc_model import build_sample_table  # noqa: E402
from qspice_sim.force_calc import FORCE_DEFAULTS, SRC_ADC_OUT, SRC_IDEAL, compute_all  # noqa: E402

MODEL_DIR = SIM_DIR.parent / "Basic_Model"
RESULTS_DIR = SIM_DIR / "results"
WORK_DIR = SIM_DIR / "runs" / "_basic_fixed_force"

# ---------------------------------------------------------------------------------------------- fixed system
R_BIAS_OHM = 20e6
CP_PF = 100.0
FS_HZ = 30_000.0
BITS = 12
NOISE_MV = 0.4
CYCLES = 2
AMP_N = 0.25
FORCE_START_US = 100.0              # 3 sampling periods of zero-force baseline, whatever the sampling phase
TAIL_SAMPLES = 3                    # conversions after the last force cycle
SAVE_MAX_POINTS = 40_000
SWEEP_MAX_SAMPLES = 2_500_000       # ADC samples per run that the sweep allows (the model's own guard is 2,000,000)
FREQS_HZ = [0.1, 1.0, 10.0, 100.0, 1000.0, 5000.0, 10_000.0]
N_PHASES = 20
PHASE_SPAN = 0.5                    # of the sampling period
PHASE_SEED = 20261029
PHASE_GUARD = 0.01                  # of the sampling period: no phase closer than this to exactly 0.25 Ts or to 0.5 Ts


def draw_phases(n: int, seed: int = PHASE_SEED, guard: float = PHASE_GUARD) -> list[float]:
    """n random sampling phases in [0, PHASE_SPAN) of a sampling period, ascending (so the phase index follows the phase), none within
    `guard` of 0.25 Ts or of the end of the span (0.5 Ts). A phase that falls in a guard band is replaced by the next random draw, so
    the phases outside the bands are the same as without the guard."""
    rng = np.random.default_rng(seed)
    phases = [float(x) for x in rng.uniform(0.0, PHASE_SPAN, n)]
    banned = lambda x: abs(x - 0.25) < guard or x > PHASE_SPAN - guard  # noqa: E731
    phases = [x if not banned(x) else None for x in phases]
    for i, x in enumerate(phases):
        while x is None or banned(x):
            x = float(rng.uniform(0.0, PHASE_SPAN))
        phases[i] = x
    return sorted(phases)


@dataclass(frozen=True)
class Case:
    freq: float
    k: int                  # phase index
    phase: float            # first conversion, fraction of the sampling period after the start of the sampling frame
    noise_mv: float = NOISE_MV
    fs_hz: float = FS_HZ

    @property
    def key(self) -> str:
        return f"f{self.freq:g}Hz_ph{self.k:02d}"

    @property
    def seed(self) -> int:
        """Noise seed of the case: reproducible, and different for every frequency and phase."""
        return zlib.crc32(self.key.encode())

    @property
    def offset_s(self) -> float:
        return self.phase / self.fs_hz


def sensor_amplitude(freq: float, amp_n: float, cp: float, d33: float, r: float) -> float:
    """Steady-state voltage amplitude of the sensor node: d33 A / Cp x w tau / sqrt(1 + (w tau)^2)."""
    x = 2 * math.pi * freq * r * cp
    return d33 * amp_n / cp * x / math.sqrt(1 + x * x)


def make_config(model, case: Case) -> tuple[dict, dict]:
    cfg = model.default_config()
    bm = model.adapter.bm
    cfg["forces"] = []
    cfg["circuit"].update(bias_to="ground", vmid_v=0.0, r_bias_ohm=R_BIAS_OHM)
    cfg["sensor_defaults"]["cp_pF"] = CP_PF
    sp = bm.sensor_params(cfg)
    ts, period = 1.0 / case.fs_hz, 1.0 / case.freq
    t_force = FORCE_START_US * 1e-6
    t_run = t_force + CYCLES * period + TAIL_SAMPLES * ts
    cfg["adc"].update(bits=BITS, sampling_rate_hz=case.fs_hz, t_first_sample_us=case.offset_s * 1e6)
    cfg["simulation"].update(run_duration_ms=t_run * 1e3, adc_noise_mV=case.noise_mv, noise_seed=case.seed)
    cfg["forces_sin"] = [{"address": 0, "t_start_us": FORCE_START_US, "freq_hz": case.freq, "cycles": float(CYCLES), "amp_N": AMP_N}]
    return cfg, {"amp_N": AMP_N, "t_force_s": t_force, "t_run_s": t_run, "ts_s": ts, "period_s": period,
                 "sensor_amp_expected_V": sensor_amplitude(case.freq, AMP_N, sp["cp"], sp["d33"], R_BIAS_OHM)}


# ---------------------------------------------------------------------------------------------- metrics
def error_metrics(t: np.ndarray, f_cal: np.ndarray, f_in: np.ndarray, freq: float, amp: float, t_force: float, ts: float) -> dict:
    """RMS error over the force cycles and the peak-to-peak error of the last cycle (calculated against applied, at the conversions)."""
    period = 1.0 / freq
    eps = 1e-3 * ts                         # a conversion on a cycle edge belongs to the cycle that starts there
    win = (t >= t_force - eps) & (t < t_force + CYCLES * period - eps)
    last = (t >= t_force + (CYCLES - 1) * period - eps) & (t < t_force + CYCLES * period - eps)
    e = (f_cal - f_in)[win]
    pp_in = 2.0 * amp                       # the applied force of a whole cycle: the sine reaches +A and -A
    pp_cal, pp_in_sampled = float(np.ptp(f_cal[last])), float(np.ptp(f_in[last]))
    err = (pp_cal - pp_in) / pp_in * 100.0
    return {
        "n_window": int(win.sum()), "n_last_cycle": int(last.sum()),
        "rms_err_N": float(np.sqrt(np.mean(e ** 2))), "rms_err_pct": float(np.sqrt(np.mean(e ** 2)) / amp * 100.0),
        "pp_in_N": pp_in, "pp_cal_last_N": pp_cal, "pp_in_sampled_last_N": pp_in_sampled,
        "pp_err_last_signed_pct": err, "pp_err_last_pct": abs(err),
        "pp_err_vs_sampled_input_pct": abs(pp_cal - pp_in_sampled) / pp_in_sampled * 100.0,
    }


# ---------------------------------------------------------------------------------------------- one run
_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        _MODEL = model_loader.load_model(MODEL_DIR)
        # the model guards a run at 2,000,000 ADC samples (GUI protection); 0.1 Hz x 2 cycles at 100 kHz needs 2,000,013, so the sweep
        # raises the limit in its own process only (Basic_Model/build_model.py is not changed)
        _MODEL.adapter.bm.MAX_SAMPLES = max(_MODEL.adapter.bm.MAX_SAMPLES, SWEEP_MAX_SAMPLES)
    return _MODEL


def run_case(case: Case, out_root: Path, qspice: str) -> dict:
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
        raise RuntimeError("the model does not use the 100 pF / 20 Mohm of the test")
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
    # the applied force at the conversion instants is read from the QSPICE force node (the instants are solver breakpoints); check it
    # against the exact SIN source value
    f_exact = AMP_N * np.sin(2 * np.pi * case.freq * np.clip(t_s - tm["t_force_s"], 0.0, CYCLES / case.freq))
    fin_dev = float(np.max(np.abs(cal.fin - f_exact)) / AMP_N)
    p0_s = np.interp(t_s, time_pts, p0)
    force_win = (time_pts >= tm["t_force_s"]) & (time_pts <= tm["t_force_s"] + CYCLES / case.freq)
    last = (time_pts >= tm["t_force_s"] + (CYCLES - 1) / case.freq) & (time_pts <= tm["t_force_s"] + CYCLES / case.freq)
    stride = max(1, math.ceil(len(t_s) / SAVE_MAX_POINTS))
    sl = slice(None, None, stride)
    np.savez_compressed(case_dir / "samples.npz", t_s=t_s[sl], adc_code=table.code[sl], adc_out_V=table.v_out[sl], adc_in_V=v_in[sl],
                        noise_V=table.noise_v[sl], sensor_V=p0_s[sl], force_in_N=cal.fin[sl], force_cal_N=cal.F[sl],
                        force_cal_ideal_adc_N=cal_ideal.F[sl])
    meta = {
        "key": case.key, "freq_hz": case.freq, "phase_index": case.k, "phase_of_ts": case.phase, "phase_offset_us": case.offset_s * 1e6,
        "fs_hz": case.fs_hz, "oversampling": case.fs_hz / case.freq, "cycles": CYCLES, "amp_N": AMP_N, "r_bias_ohm": R_BIAS_OHM, "cp_pF": CP_PF,
        "bits": BITS, "lsb_mV": table.adc.lsb * 1e3, "noise_mV": case.noise_mv, "noise_seed": case.seed,
        "noise_rms_measured_mV": float(np.std(table.noise_v) * 1e3) if len(t_s) > 1 else 0.0,
        "n_samples": int(len(t_s)), "n_clipped": int(table.clipped.sum()), "qspice_points": n_points,
        "max_step_us": bm.max_step(cfg) * 1e6, "wall_s": round(time.time() - t0, 2),
        "sensor_amp_expected_V": tm["sensor_amp_expected_V"],
        "sensor_peak_V": float(np.max(np.abs(p0[force_win]))), "sensor_peak_last_cycle_V": float(np.max(np.abs(p0[last]))),
        "fin_deviation_from_sin_pct_of_amp": fin_dev * 100.0, "saved_stride": stride, "timing": tm,
        "metrics": error_metrics(cal.t, cal.F, cal.fin, case.freq, AMP_N, tm["t_force_s"], tm["ts_s"]),
        "metrics_ideal_adc": error_metrics(cal_ideal.t, cal_ideal.F, cal_ideal.fin, case.freq, AMP_N, tm["t_force_s"], tm["ts_s"]),
        "warnings": prepared.warnings,
    }
    (case_dir / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
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


MARKERS = "osv^DPX"                         # one marker per frequency in the phase graph
SUMMARY_COLS = ["rms_err_pct", "pp_err_last_pct", "pp_err_last_signed_pct", "pp_cal_last_N", "pp_err_vs_sampled_input_pct"]
STAT_COLS = ["rms_err_pct", "pp_err_last_pct", "pp_err_last_signed_pct", "pp_cal_last_N"]


def sd(v) -> float:
    return float(np.std(v, ddof=1)) if len(v) > 1 else 0.0


def write_summary(root: Path, runs: list[dict]) -> None:
    head = ["freq_hz", "phase_index", "phase_of_ts", "phase_offset_us", "amp_N", "sensor_amp_expected_V", "sensor_peak_last_cycle_V",
            "n_samples", "n_clipped"] + SUMMARY_COLS + ["rms_err_pct_ideal_adc"]
    with (root / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(head)
        for r in runs:
            w.writerow([r["freq_hz"], r["phase_index"], f"{r['phase_of_ts']:.6g}", f"{r['phase_offset_us']:.6g}", r["amp_N"],
                        r["sensor_amp_expected_V"], r["sensor_peak_last_cycle_V"], r["n_samples"], r["n_clipped"]]
                       + [r["metrics"][c] for c in SUMMARY_COLS] + [r["metrics_ideal_adc"]["rms_err_pct"]])
    freqs = sorted({r["freq_hz"] for r in runs})
    with (root / "summary_by_frequency.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["freq_hz", "amp_N", "pp_in_N", "n_phases"] + [f"{c}_{s}" for c in STAT_COLS for s in ("median", "std", "mean", "min", "max")])
        for f in freqs:
            rr = [r for r in runs if r["freq_hz"] == f]
            row = [f, rr[0]["amp_N"], rr[0]["metrics"]["pp_in_N"], len(rr)]
            for c in STAT_COLS:
                v = np.array([r["metrics"][c] for r in rr])
                row += [float(np.median(v)), sd(v), float(v.mean()), float(v.min()), float(v.max())]
            w.writerow(row)


def build_report(root: Path, pzt: bool = False) -> None:
    """report.md, figures and tables of a result folder. ``pzt``: the layout of PZT_Freq_Tests.docx (peak-to-peak graphs only, no RMS
    graphs; the RMS values stay in the tables) and the Word file PZT_Freq_Tests.docx."""
    import matplotlib.ticker
    import matplotlib.pyplot as plt
    from sampling_sweep_report import CAT, INK2, save, table

    SERIES = CAT + ["#008300", "#6250d6"]   # categorical slots 1-7 of the data-viz palette (blue, orange, aqua, yellow, magenta, green, violet)

    runs = load_runs(root)
    write_summary(root, runs)
    fs = float(runs[0]["fs_hz"])
    freqs = sorted({r["freq_hz"] for r in runs})
    by_f = {f: [r for r in runs if r["freq_hz"] == f] for f in freqs}
    n_ph = len(by_f[freqs[0]])
    noise_mv = float(runs[0]["noise_mV"])
    amp = float(runs[0]["amp_N"])
    r_bias, cp = runs[0]["r_bias_ohm"], runs[0]["cp_pF"] * 1e-12
    tau = r_bias * cp
    clip = max(r["n_clipped"] for r in runs)
    setup_file = root / "setup.json"
    guard = json.loads(setup_file.read_text(encoding="utf-8")).get("phase_guard_ts") if setup_file.exists() else None
    guard_txt = f", none within {guard:g} Ts ({guard * 1e6 / fs:.2f} us) of exactly 0.25 Ts or 0.5 Ts" if guard else ""

    def vals(f, key, method="metrics"):
        return np.array([r[method][key] for r in by_f[f]])

    med = lambda key, method="metrics": np.array([np.median(vals(f, key, method)) for f in freqs])  # noqa: E731
    std = lambda key, method="metrics": np.array([sd(vals(f, key, method)) for f in freqs])  # noqa: E731

    def ticks(ax, fs=None):
        fs = freqs if fs is None else fs
        ax.set_xticks(fs, [fmt_hz(f) for f in fs], rotation=30, ha="right")
        ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())

    def bars(ax, y, e, color, label=None, mk="o"):
        """median line with +-1 standard deviation bars; on a log axis the lower bar is cut at median / 50 where median - std <= 0."""
        lo = np.maximum(y - e, y / 50.0)
        ax.errorbar(freqs, y, yerr=[y - lo, e], fmt=mk + "-", color=color, lw=1.8, ms=5, capsize=3, elinewidth=1.2, label=label)

    # 1) error vs input frequency: median +- standard deviation over the phases (faint dots = each phase)
    panels = [("rms_err_pct", "RMS error", "% of the force amplitude"),
              ("pp_err_last_pct", "Peak-to-peak error, last cycle", "% of the input peak-to-peak")]
    pcolor = dict(zip([p[0] for p in panels], CAT))            # RMS blue, peak-to-peak orange, in every figure
    if pzt:
        panels = panels[1:]                                    # peak-to-peak graphs only
    wide = len(panels) == 2
    legend_txt = f"line = median over {n_ph} sampling phases, bars = +-1 standard deviation, dots = phases"
    fig, axes = plt.subplots(1, len(panels), figsize=(10.5, 4.3) if wide else (6.8, 4.6), sharex=True, squeeze=False)
    for ax, (key, title, unit) in zip(axes[0], panels):
        c = pcolor[key]
        for f in freqs:
            ax.plot([f] * len(by_f[f]), vals(f, key), ".", color=c, alpha=0.25, ms=4)
        bars(ax, med(key), std(key), c)
        ax.set(yscale="log", xscale="log", title=title, ylabel=f"[{unit}]", xlabel="force frequency")
        ticks(ax)
    fig.suptitle(f"Force error vs force frequency ({legend_txt})" if wide else f"Force error vs force frequency\n({legend_txt})",
                 fontsize=10, color=INK2)
    p_err = save(fig, root, "error_vs_frequency.png")

    # 1b) the same without the log scaling of the error axis (the frequency axis stays logarithmic: five decades); the lower row zooms into
    # 0.1 Hz .. 1 kHz, where the 5 and 10 kHz values of the upper row would flatten everything. Bars are the true median +- std.
    low = [i for i, f in enumerate(freqs) if f <= 1000.0]
    fig, axes = plt.subplots(2, len(panels), figsize=(10.5, 8.0) if wide else (6.8, 8.2), squeeze=False)
    for row, (idx, sub) in enumerate([(list(range(len(freqs))), "all frequencies"), (low, "zoom 0.1 Hz .. 1 kHz")]):
        fsel = [freqs[i] for i in idx]
        for ax, (key, title, unit) in zip(axes[row], panels):
            c = pcolor[key]
            for f in fsel:
                ax.plot([f] * len(by_f[f]), vals(f, key), ".", color=c, alpha=0.25, ms=4)
            ax.errorbar(fsel, med(key)[idx], yerr=std(key)[idx], fmt="o-", color=c, lw=1.8, ms=5, capsize=3, elinewidth=1.2)
            ax.axhline(0.0, color=INK2, lw=0.8, ls=":")
            ax.set(xscale="log", title=f"{title} - linear ({sub})", ylabel=f"[{unit}]", xlabel="force frequency")
            ticks(ax, fsel)
    fig.suptitle(f"Force error vs force frequency, linear error axis ({legend_txt})" if wide else
                 f"Force error vs force frequency, linear error axis\n({legend_txt})", fontsize=10, color=INK2)
    p_lin = save(fig, root, "error_vs_frequency_linear.png")

    # 2) applied and calculated peak-to-peak (last cycle) vs frequency
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(7.4, 7.0), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    pp_in = np.array([by_f[f][0]["metrics"]["pp_in_N"] for f in freqs])
    pp_m, pp_s = med("pp_cal_last_N"), std("pp_cal_last_N")
    pp_mean = np.array([vals(f, "pp_cal_last_N").mean() for f in freqs])
    a1.plot(freqs, pp_in, "o-", color=INK2, lw=1.6, ms=6, label="applied (input) peak-to-peak")
    a1.errorbar(freqs, pp_m, yerr=pp_s, fmt="s--", color=CAT[0], lw=1.6, ms=5, capsize=3, elinewidth=1.2,
                label="calculated peak-to-peak, last cycle (median, bars = +-1 std)")
    a1.plot(freqs, pp_mean, "D", mfc="none", color=CAT[1], ms=7, label="calculated, mean of the phases")
    a1.set(xscale="log", ylabel="peak-to-peak force [N]", ylim=(0, pp_in[0] * 1.1), title="Applied and calculated peak-to-peak force vs frequency")
    a1.legend(fontsize=8, loc="lower left")
    a2.errorbar(freqs, pp_m / pp_in, yerr=pp_s / pp_in, fmt="s--", color=CAT[0], lw=1.6, ms=5, capsize=3, elinewidth=1.2)
    a2.plot(freqs, pp_mean / pp_in, "D", mfc="none", color=CAT[1], ms=7)
    a2.axhline(1.0, color=INK2, lw=1, ls=":")
    a2.set(xscale="log", xlabel="force frequency", ylabel="calculated / applied", title="Ratio of the last-cycle peak-to-peak (median +- std)")
    ticks(a2)
    p_pp = save(fig, root, "peak_to_peak_vs_frequency.png")

    # 3) error vs sampling phase
    # one categorical colour AND one marker per frequency (slots 1-7 of the palette, fixed order), so no two series rely on colour alone
    fig, axes = plt.subplots(1, len(panels), figsize=(11, 4.4) if wide else (7.2, 4.6), squeeze=False)
    for ax, (key, title, unit) in zip(axes[0], panels):
        for f, c, mk in zip(freqs, SERIES, MARKERS):
            few = n_ph <= 30                     # many phases: smaller markers and thinner lines
            ax.plot([r["phase_of_ts"] for r in by_f[f]], vals(f, key), mk + "-", color=c, lw=0.9 if few else 0.5, ms=5.5 if few else 3.2,
                    alpha=0.9 if few else 0.8, label=fmt_hz(f))
        ax.set(yscale="log", xlabel="sampling start [fraction of the sampling period Ts]", ylabel=f"[{unit}]", title=title)
    axes[0][-1].legend(title="force frequency", fontsize=8, ncol=2, loc="lower left")
    p_ph = save(fig, root, "error_vs_phase.png")

    # 4) waveform examples: applied (line) and calculated (dots) force for the middle phase, normalised to the amplitude
    fig, axes = plt.subplots(len(freqs), 1, figsize=(10, 2.0 * len(freqs)))
    for ax, f in zip(np.atleast_1d(axes), freqs):
        r0 = by_f[f][len(by_f[f]) // 2]
        d = np.load(root / "runs" / r0["key"] / "samples.npz")
        tf = r0["timing"]["t_force_s"]
        win = (d["t_s"] >= tf - r0["timing"]["ts_s"]) & (d["t_s"] <= tf + r0["cycles"] / f + r0["timing"]["ts_s"])
        x = (d["t_s"][win] - tf) * f
        # applied force = solid line, calculated force = markers at the conversions (at most ~150 shown, evenly spread), drawn on top
        show = np.arange(len(x)) if len(x) <= 150 else np.linspace(0, len(x) - 1, 150).astype(int)
        ax.plot(x, d["force_in_N"][win] / amp, "-", color=INK2, lw=1.8, label="applied force", zorder=2)
        ax.plot(x[show], d["force_cal_N"][win][show] / amp, "o", color=CAT[1], ms=4.5, mec="white", mew=0.6, ls="none",
                label="calculated force from ADC-Out (conversions)", zorder=3)
        ax.set(ylabel=f"{fmt_hz(f)}\nF / A", xlim=(x[0], x[-1]))
        if f == freqs[0]:
            ax.legend(ncol=2, fontsize=8, loc="lower center", bbox_to_anchor=(0.5, 1.02))
    np.atleast_1d(axes)[-1].set_xlabel("time since the start of the force [force periods]")
    p_wave = save(fig, root, "waveform_examples.png")

    # tables
    t_setup = table([[fmt_hz(f), f"{fs / f:,.0f}", f"{by_f[f][0]['n_samples']:,}", f"{by_f[f][0]['sensor_amp_expected_V'] * 1e3:.1f}",
                      f"{np.mean([r['sensor_peak_last_cycle_V'] for r in by_f[f]]) * 1e3:.1f}",
                      f"{by_f[f][0]['sensor_amp_expected_V'] / (by_f[f][0]['lsb_mV'] * 1e-3):.1f}"] for f in freqs],
                    ["force frequency", "samples / force period", "conversions in the run", "sensor amplitude, expected [mV]",
                     "sensor peak, last cycle [mV]", "sensor amplitude [LSB]"])
    t_err = table([[fmt_hz(f), f"{np.median(vals(f, 'rms_err_pct')):.3g}", f"{sd(vals(f, 'rms_err_pct')):.2g}",
                    f"{np.median(vals(f, 'pp_err_last_pct')):.3g}", f"{sd(vals(f, 'pp_err_last_pct')):.2g}"] for f in freqs],
                  ["force frequency", "RMS median [% of A]", "RMS std [% of A]", "p-p last cycle median [%]", "p-p last cycle std [%]"])
    t_pp = table([[fmt_hz(f), f"{pp_in[i]:.4g}", f"{pp_m[i]:.4g}", f"{pp_s[i]:.2g}", f"{pp_mean[i]:.4g}", f"{pp_m[i] / pp_in[i]:.4f}",
                   f"{np.median(vals(f, 'pp_err_last_signed_pct')):+.3g}"] for i, f in enumerate(freqs)],
                 ["force frequency", "applied p-p [N]", "calculated p-p, median [N]", "std [N]", "mean [N]", "median / applied",
                  "signed p-p error, median [%]"])
    t_alg = table([[fmt_hz(f), f"{np.median(vals(f, 'pp_err_vs_sampled_input_pct')):.3g}", f"{np.median(vals(f, 'rms_err_pct')):.3g}",
                    f"{np.median(vals(f, 'rms_err_pct', 'metrics_ideal_adc')):.3g}"] for f in freqs],
                  ["force frequency", "p-p error vs the input at the conversions, median [%]", "RMS, ADC-Out with noise, median [%]",
                   "RMS, ideal ADC input (no noise, no quantization), median [%]"])
    t_ph = table([[str(r["phase_index"]), f"{r['phase_of_ts']:.4f}", f"{r['phase_offset_us']:.3f}"] for r in by_f[freqs[0]]],
                 ["phase index", "start [x Ts]", "start [us]"])
    if pzt:
        # where the ADC (noise, quantization) is the larger part of the RMS error: the ideal-ADC RMS is below 70 % of the ADC-Out RMS
        ratio = {f: np.median(vals(f, "rms_err_pct", "metrics_ideal_adc")) / np.median(vals(f, "rms_err_pct")) for f in freqs}
        adc_dominates = [f for f in freqs if ratio[f] < 0.7]
        if len(adc_dominates) == len(freqs):
            dominate_txt = (f"They dominate at all the force frequencies tested (up to {fmt_hz(freqs[-1])}), because at this sampling rate "
                            f"the sampling error is small.")
        else:
            dominate_txt = (f"They dominate at low force frequencies (up to about {fmt_hz(max(adc_dominates))}), where the sampling error "
                            f"is negligible.") if adc_dominates else "At the force frequencies tested the sampling error is the larger part."
        lsb = lambda f: by_f[f][0]["sensor_amp_expected_V"] / (by_f[f][0]["lsb_mV"] * 1e-3)  # noqa: E731
        mv = lambda f: by_f[f][0]["sensor_amp_expected_V"] * 1e3  # noqa: E731
        text = f"""# PZT Sensor – Sin Force Tests

Generated by `Qspice/Simulator/tools/basic_fixed_force_sweep.py` from {len(runs)} QSPICE runs ({len(freqs)} frequencies x {n_ph} random sampling phases).

## Setup

| | |
|---|---|
| Model | PZT (charge source d33 x dF/dt, 600 pC/N), ideal unity buffer, ideal ADC (+ noise) |
| Bias resistor | {r_bias / 1e6:g} Mohm to ground, VMID = 0 V |
| Sensor capacitance | {cp * 1e12:g} pF |
| ADC | {runs[0]['bits']}-bit, range -1.65 to +1.65 V (LSB {runs[0]['lsb_mV']:.3f} mV), sampling rate {fs / 1e3:g} kHz |
| Noise | white Gaussian noise {noise_mv:g} mV rms added to the ADC input at every conversion (before the quantization) |
| Force | {CYCLES} cycles of a sine, amplitude {amp:g} N ({2 * amp:g} N peak-to-peak) at every frequency |
| Force calculation | Leakage-compensated from the ADC-Out value |
| Sampling phase | {n_ph} random first-conversion times in [0, Ts/2) = 0 to {0.5e6 / fs:.4g} us (Ts = {1e6 / fs:.5g} us); the force start does not move |

## Force frequencies and measured peak voltages

{t_setup}

## Results

Median and standard deviation over the {n_ph} sampling phases. The peak-to-peak error compares the peak-to-peak of the calculated force of the last cycle (max - min of the conversions inside that cycle) with the input peak-to-peak 2 A, relative to 2 A (magnitude).

**Definitions used**: RMS = rms of (calculated - applied) at the conversion instants over the two cycles, in % of the force amplitude A. Peak-to-peak error of the last cycle = |max - min of the calculated force over the conversions of the last cycle - 2 A| / 2 A.

{t_err}

Applied and calculated peak-to-peak (last cycle):

{t_pp}

### Where the error comes from

The first column compares the calculated peak-to-peak (2*A) with the peak-to-peak of the applied force at the conversion instants (what the sampling could at best show). The RMS columns compare the ADC-Out value with an ideal ADC input (no noise, no quantization).

The "ideal ADC" RMS column is the error of the force calculated from the exact sensor voltage (no noise, no ADC quantization, exact Cp, R and d33), i.e. the error of the calculation method. The calculation reverses the leak exactly, but it assumes the charge arrives at a constant rate within each sampling interval Ts. In reality, the voltage at the end of the interval weights the late charge more, because the early charge has leaked longer. The calculated force is therefore ahead of the applied force by Ts²/(12 τ), which for a sine gives

RMS error ≈ 0.37 · (f / fs) · (Ts / τ)

where f/fs is the share of a force period covered by one sampling interval (the sampling error), Ts/τ is the sampling interval relative to the bias time constant R·Cp, and 0.37 = 2π/(12√2). A small constant offset, which depends on where the force starts within a sampling interval, adds to this in quadrature. The error therefore grows with force frequency and falls with the square of the sampling rate.

The non-ideal ADC includes quantization error of the {runs[0]['bits']}-bit ADC and {noise_mv:g} mV rms noise. {dominate_txt}

**Noise**: The noise of one conversion appears directly in the calculated force. In addition, the noise charge of each conversion accumulates as a random walk. A slow force needs many conversions (N ∝ 1/f), so this term grows toward low frequency.

**Quantization**: At low frequency, the signal is only a few LSB ({mv(freqs[0]):.1f} mV = {lsb(freqs[0]):.1f} LSB at {fmt_hz(freqs[0])}, {lsb(freqs[1]):.0f} LSB at {fmt_hz(freqs[1])}), so the ADC output is a coarse staircase and the running sum accumulates its rounding errors.

{t_alg}

## Figures

- `{p_err}`: Last-cycle peak-to-peak error vs force frequency, median and standard deviation over the phases (the lower bar is cut at median / 50 on the log axis where median - std is not positive)
- `{p_lin}`: The same graph with a linear error axis (frequency axis still logarithmic), so the standard deviation bars are not distorted by the log scale; the lower row zooms into 0.1 Hz to 1 kHz
- `{p_pp}`: Applied and calculated peak-to-peak (last cycle) vs frequency, and their ratio
- `{p_ph}`: Peak-to-peak error vs sampling phase for each frequency
- `{p_wave}`: Applied force (line) and the force calculated from the ADC-Out value at the conversions (markers, at most ~150 shown) of one phase, normalized to the amplitude

![error vs frequency]({p_err})
![error vs frequency, linear]({p_lin})
![peak-to-peak]({p_pp})
![phase]({p_ph})
![waveforms]({p_wave})
"""
        (root / "report.md").write_text(text, encoding="utf-8")
        from report_to_docx import build_docx
        docx = build_docx(root, file_name="PZT_Freq_Tests.docx", header=False)
        print(f"report: {root / 'report.md'}  ({len(runs)} runs, clipped conversions: {clip})\nword:   {docx}")
        return
    notes = root / "notes.md"
    text = f"""# Basic_Model: fixed force amplitude {amp:g} N, {r_bias / 1e6:g} Mohm bias, {noise_mv:g} mV rms ADC noise

Generated by `Qspice/Simulator/tools/basic_fixed_force_sweep.py` from {len(runs)} QSPICE runs ({len(freqs)} frequencies x {n_ph} random sampling phases).

## Setup (fixed)

| | |
|---|---|
| Model | Basic_Model: ideal PZT (charge source d33 x dF/dt, 600 pC/N) in parallel with Cp, ideal unity buffer, ideal ADC |
| Bias resistor | {r_bias / 1e6:g} Mohm to ground, VMID = 0 V (time constant R x Cp = {tau * 1e3:g} ms, corner {1 / (2 * math.pi * tau):,.1f} Hz) |
| Sensor capacitance | {cp * 1e12:g} pF in the circuit and in the force calculation |
| ADC | {runs[0]['bits']} bit, range -1.65 .. +1.65 V (LSB {runs[0]['lsb_mV']:.3f} mV), sampling rate {fs / 1e3:g} kHz fixed, {clip} clipped conversions |
| Noise | white Gaussian noise {noise_mv:g} mV rms added to the ADC input at every conversion (before the quantisation); own seed per case, so every run has a different noise realisation |
| Force | {CYCLES} cycles of a sine (QSPICE `SIN` source), amplitude {amp:g} N ({2 * amp:g} N peak-to-peak) at every frequency, start {FORCE_START_US:g} us after the start of the sampling frame |
| Force calculation | leakage-compensated (default of the Force tab), from the ADC-Out value, reference VMID |
| Sampling phase | {n_ph} random first-conversion times in [0, Ts/2) = 0 .. {0.5e6 / fs:.2f} us (Ts = {1e6 / fs:.3f} us){guard_txt}, the same for every frequency (seed {PHASE_SEED}); the force start does not move |
| Statistics | median and standard deviation (sample, n - 1) over the {n_ph} phases |

The sensor amplitude is not tuned: with the fixed {amp:g} N it follows from the bias high-pass, `V = d33 A / Cp x w tau / sqrt(1 + (w tau)^2)`
(d33 / Cp = 6 V/N, so at most {6 * amp:g} V). Below the corner the resistor bleeds the charge off as fast as the force creates it, so the
low frequencies give only a few LSB.

{t_setup}

## Results

Median and standard deviation over the {n_ph} sampling phases. The peak-to-peak error compares the peak-to-peak of the calculated force of the
last cycle (max - min of the conversions inside that cycle) with the input peak-to-peak 2 A, relative to 2 A (magnitude).

{t_err}

Applied and calculated peak-to-peak (last cycle):

{t_pp}

### Where the error comes from

The peak-to-peak error above is measured against the true input peak-to-peak 2 A, so it also contains the peaks that fall between two
conversions. The first column compares the calculated peak-to-peak with the peak-to-peak of the applied force *at the conversion instants*
(what the sampling could at best show). The RMS columns compare the ADC-Out value with an ideal ADC input (no noise, no quantisation), which
leaves the error of the force calculation alone.

{t_alg}

## Figures

- `{p_err}`: RMS error and last-cycle peak-to-peak error vs force frequency, median and standard deviation over the phases (the lower bar is cut at median / 50 on the log axis where median - std is not positive).
- `{p_lin}`: the same two graphs with a linear error axis (frequency axis still logarithmic), so the standard deviation bars are not distorted by the log scale; the lower row zooms into 0.1 Hz .. 1 kHz.
- `{p_pp}`: applied and calculated peak-to-peak (last cycle) vs frequency, and their ratio.
- `{p_ph}`: error vs sampling phase for each frequency.
- `{p_wave}`: applied force (line) and the force calculated from the ADC-Out value at the conversions (markers, at most ~150 shown) of one phase, normalised to the amplitude.

![error vs frequency]({p_err})
![error vs frequency, linear]({p_lin})
![peak-to-peak]({p_pp})
![phase]({p_ph})
![waveforms]({p_wave})

### Sampling phases

{t_ph}

Files: `summary.csv` (every run), `summary_by_frequency.csv` (median / std / mean / min / max over the phases), `runs/<case>/` (samples.npz, meta.json).
"""
    if notes.exists():
        text += "\n## Notes\n\n" + notes.read_text(encoding="utf-8").strip() + "\n"
    (root / "report.md").write_text(text, encoding="utf-8")
    print(f"report: {root / 'report.md'}  ({len(runs)} runs, clipped conversions: {clip})")


# ---------------------------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=None, help="result folder (default: a new results/basic_model_fixed_force_R20M_noise0p4mV_<n>ph_<date>_<time>)")
    ap.add_argument("--freqs", type=float, nargs="+", default=FREQS_HZ, help="force frequencies [Hz]")
    ap.add_argument("--n-phases", type=int, default=N_PHASES, help="random sampling phases in [0, Ts/2)")
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1), help="parallel QSPICE processes")
    ap.add_argument("--qspice", default=None, help="path of QSPICE64.exe")
    ap.add_argument("--force", action="store_true", help="re-run cases that already have a meta.json")
    ap.add_argument("--dry-run", action="store_true", help="list the cases and stop")
    ap.add_argument("--report-only", action="store_true", help="only rebuild summary.csv, figures and report.md (needs --out)")
    ap.add_argument("--no-report", action="store_true", help="do not write the report after the runs")
    ap.add_argument("--fs-hz", type=float, default=FS_HZ, help="ADC sampling rate [Hz] (default 30000)")
    ap.add_argument("--pzt-report", action="store_true",
                    help="report in the layout of PZT_Freq_Tests.docx: peak-to-peak graphs only (no RMS graphs); also writes PZT_Freq_Tests.docx")
    args = ap.parse_args()
    if args.report_only:
        if not args.out:
            ap.error("--report-only needs --out")
        build_report(Path(args.out), pzt=args.pzt_report)
        return 0

    out_root = Path(args.out) if args.out else RESULTS_DIR / (
        f"basic_model_fixed_force_R{R_BIAS_OHM / 1e6:g}M_noise{NOISE_MV:g}mV_fs{args.fs_hz / 1e3:g}kHz_{args.n_phases}ph_"
        f"{datetime.now():%Y%m%d_%H%M%S}".replace(".", "p"))
    setup = {"r_bias_ohm": R_BIAS_OHM, "cp_pF": CP_PF, "fs_hz": args.fs_hz, "bits": BITS, "noise_mV": NOISE_MV, "cycles": CYCLES, "amp_N": AMP_N,
             "phase_span_ts": PHASE_SPAN, "phase_seed": PHASE_SEED, "phase_guard_ts": PHASE_GUARD, "n_phases": args.n_phases,
             "phases_ts": draw_phases(args.n_phases)}
    setup_file = out_root / "setup.json"
    if setup_file.exists() and json.loads(setup_file.read_text(encoding="utf-8")) != setup:
        ap.error(f"{out_root} was made with other settings or phases; use another --out")
    cases = [Case(f, k, p, fs_hz=args.fs_hz) for f in args.freqs for k, p in enumerate(setup["phases_ts"])]
    todo = [c for c in cases if args.force or not (out_root / "runs" / c.key / "meta.json").exists()]
    print(f"result folder: {out_root}\n{len(cases)} cases ({len(cases) - len(todo)} already done, {len(todo)} to run)")
    print("sampling phases [x Ts]: " + ", ".join(f"{p:.4f}" for p in setup["phases_ts"]))
    model = _model()
    sp = model.adapter.bm.sensor_params(make_config(model, cases[0])[0])
    for f in args.freqs:
        print(f"  {f:>8g} Hz: amplitude {AMP_N:g} N -> sensor {sensor_amplitude(f, AMP_N, sp['cp'], sp['d33'], R_BIAS_OHM) * 1e3:.1f} mV, "
              f"{args.fs_hz / f:g} samples/period, {args.fs_hz * CYCLES / f:,.0f} conversions")
    if args.dry_run:
        return 0
    out_root.mkdir(parents=True, exist_ok=True)
    setup_file.write_text(json.dumps(setup, indent=1), encoding="utf-8")
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
                print(f"[{n}/{len(todo)}] {c.key}: rms {m['rms_err_pct']:.3g} %  p-p last {m['pp_err_last_pct']:.3g} %  "
                      f"sensor {meta['sensor_peak_last_cycle_V'] * 1e3:.0f} mV  ({meta['wall_s']} s, {(time.time() - t_start) / 60:.1f} min)", flush=True)
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
        build_report(out_root, pzt=args.pzt_report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
