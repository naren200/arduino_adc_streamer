"""Summary, figures and report.md of the sampling sweep (see sampling_sweep.py).

    python Qspice/Simulator/tools/sampling_sweep_report.py [results/sampling_sweep]

Reads runs/*/meta.json (+ samples.csv / waveform.npz for the example plots), writes summary.csv, figures/*.png and report.md into the
result folder. An optional notes.md in that folder is appended to the report as "Notes" (hand-written interpretation).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402
import numpy as np  # noqa: E402

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "results" / "sampling_sweep"

# palette: categorical slots 1-5 of the data-viz reference palette (light surface) for frequencies / methods, the blue ramp for ordered rates
CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
RAMP = ["#9ec5f4", "#86b6ef", "#5598e7", "#3987e5", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e3e2de"
METHODS = [("metrics", "full compensation (default)"),
           ("metrics_no_leak_current", "leakage currents = 0"),
           ("metrics_no_compensation", "no compensation (ideal charge model)")]

plt.rcParams.update({"font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
                     "axes.titlecolor": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.spines.top": False,
                     "axes.spines.right": False, "legend.frameon": False, "figure.dpi": 110, "savefig.dpi": 130,
                     "axes.prop_cycle": plt.cycler(color=CAT), "figure.constrained_layout.use": True})


def load_runs(root: Path) -> list[dict]:
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((root / "runs").glob("*/meta.json"))]
    if not runs:
        raise SystemExit(f"no runs found in {root / 'runs'}")
    return sorted(runs, key=lambda r: (r["freq_hz"], r["fs_hz"], r["phase_T"]))


def fmt_hz(f: float) -> str:
    return f"{f / 1e3:g} kHz" if f >= 1000 else f"{f:g} Hz"


def write_summary(root: Path, runs: list[dict]) -> None:
    cols = ["freq_hz", "fs_hz", "oversampling", "phase_T", "phase_deg", "grid_phase", "n_samples", "n_window"]
    per = ["rms_err_pct", "max_err_pct", "mean_err_N", "peak_ratio", "fit_gain", "fit_phase_deg", "end_err_N"]
    with (root / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols + [f"{k.replace('metrics', 'm') if k != 'metrics' else 'm'}_{p}" for k, _ in METHODS for p in per]
                   + [f"m_ideal_adc_{p}" for p in per])
        for r in runs:
            w.writerow([r[c] if c in r else r["metrics"][c] for c in cols]
                       + [r[k][p] for k, _ in METHODS for p in per] + [r["metrics_ideal_adc"][p] for p in per])


def table(rows: list[list[str]], header: list[str]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] + ["---:"] * (len(header) - 1)) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


class Agg:
    """runs grouped by (frequency, sampling rate); statistics over the phases."""

    def __init__(self, runs: list[dict]):
        self.runs = runs
        self.freqs = sorted({r["freq_hz"] for r in runs})
        self.rates = sorted({r["fs_hz"] for r in runs})
        self.g: dict[tuple[float, float], list[dict]] = {}
        for r in runs:
            self.g.setdefault((r["freq_hz"], r["fs_hz"]), []).append(r)

    def vals(self, f: float, fs: float, key: str = "rms_err_pct", method: str = "metrics") -> np.ndarray:
        return np.array([r[method][key] for r in self.g.get((f, fs), [])])

    def stat(self, f: float, fs: float, how: str, key: str = "rms_err_pct", method: str = "metrics") -> float:
        v = self.vals(f, fs, key, method)
        return float("nan") if not len(v) else float({"mean": np.mean, "min": np.min, "max": np.max}[how](v))


def save(fig, root: Path, name: str) -> str:
    (root / "figures").mkdir(exist_ok=True)
    fig.savefig(root / "figures" / name, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return f"figures/{name}"


# ------------------------------------------------------------------------------------------------ figures
def fig_error_vs_freq(a: Agg, root: Path) -> str:
    fig, ax = plt.subplots(figsize=(7, 4.2))
    colors = dict(zip(a.rates, (RAMP[i] for i in np.linspace(0, len(RAMP) - 1, len(a.rates)).astype(int))))
    for fs in a.rates:
        fr = [f for f in a.freqs if (f, fs) in a.g]
        ax.plot(fr, [a.stat(f, fs, "mean") for f in fr], "o-", color=colors[fs], lw=1.6, ms=5, label=f"{fs / 1e3:g} kHz")
    ax.set(xscale="log", yscale="log", xlabel="input frequency [Hz]", ylabel="RMS error of the calculated force [% of 0.2 N]",
           title="Force error vs input frequency (mean over the 5 phases, one line per sampling rate)")
    ax.legend(title="sampling rate", ncol=2, fontsize=8)
    return save(fig, root, "error_vs_frequency.png")


def fig_error_vs_fs(a: Agg, root: Path) -> str:
    fig, ax = plt.subplots(figsize=(7, 4.2))
    for i, f in enumerate(a.freqs):
        fr = [fs for fs in a.rates if (f, fs) in a.g]
        m = np.array([a.stat(f, fs, "mean") for fs in fr])
        lo = np.array([a.stat(f, fs, "min") for fs in fr])
        hi = np.array([a.stat(f, fs, "max") for fs in fr])
        x = np.array(fr) / 1e3
        ax.fill_between(x, lo, hi, color=CAT[i], alpha=0.15, lw=0)
        ax.plot(x, m, "o-", color=CAT[i], lw=1.6, ms=5, label=fmt_hz(f))
    ax.set(xscale="log", yscale="log", xlabel="sampling rate [kHz]", ylabel="RMS error of the calculated force [% of 0.2 N]",
           title="Force error vs sampling rate (line = mean, band = best..worst phase)")
    ax.set_xticks([5, 10, 20, 30, 50, 60], ["5", "10", "20", "30", "50", "60"])
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.legend(title="input frequency")
    return save(fig, root, "error_vs_sampling_rate.png")


def fig_heatmaps(a: Agg, root: Path) -> str:
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6))
    for ax, how, key, title in [(axes[0], "mean", "rms_err_pct", "RMS error, mean over phases [%]"),
                                (axes[1], "max", "max_err_pct", "Peak error, worst phase [%]")]:
        z = np.array([[a.stat(f, fs, how, key) for fs in a.rates] for f in a.freqs])
        ax.grid(False)
        im = ax.imshow(np.log10(z), cmap="Blues", aspect="auto")
        ax.set_xticks(range(len(a.rates)), [f"{fs / 1e3:g}" for fs in a.rates])
        ax.set_yticks(range(len(a.freqs)), [fmt_hz(f) for f in a.freqs])
        ax.set(xlabel="sampling rate [kHz]", title=title)
        for i in range(z.shape[0]):
            for j in range(z.shape[1]):
                if np.isfinite(z[i, j]):
                    dark = np.log10(z[i, j]) > np.nanmean(np.log10(z)) + 0.2
                    ax.text(j, i, f"{z[i, j]:.3g}", ha="center", va="center", fontsize=8, color="white" if dark else INK)
        for s in ax.spines.values():
            s.set_visible(False)
    fig.suptitle("Blank = below the Nyquist rate (not run). Colour = log10 of the value.", fontsize=9, color=INK2)
    return save(fig, root, "heatmaps.png")


def fig_phase(a: Agg, root: Path) -> str:
    fig, axes = plt.subplots(1, len(a.freqs), figsize=(3.0 * len(a.freqs), 3.4), sharey=False)
    for ax, f in zip(axes, a.freqs):
        rates = [fs for fs in a.rates if (f, fs) in a.g]
        cols = [RAMP[i] for i in np.linspace(0, len(RAMP) - 1, len(a.rates)).astype(int)]
        for fs in rates:
            rs = sorted(a.g[(f, fs)], key=lambda r: r["phase_T"])
            ax.plot([r["phase_deg"] for r in rs], [r["metrics"]["rms_err_pct"] for r in rs], "o-", ms=3.5, lw=1.4,
                    color=cols[a.rates.index(fs)], label=f"{fs / 1e3:g}k")
        ax.set(title=fmt_hz(f), xlabel="phase [deg of the sine period]", xticks=[0, 22.5, 45, 67.5, 90])
        ax.tick_params(axis="x", labelsize=7)
        ax.legend(fontsize=7, ncol=2, title="fs", title_fontsize=7)
    axes[0].set_ylabel("RMS error [% of 0.2 N]")
    fig.suptitle("Dependence on the sampling phase (0 .. T/4)", color=INK)
    return save(fig, root, "phase_dependence.png")


def fig_peak(a: Agg, root: Path) -> str:
    """Largest calculated |F| divided by the true amplitude: what a peak detector on the samples would report."""
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), sharey=True)
    for ax, f in zip(axes, [10_000.0, 20_000.0]):
        rates = [fs for fs in a.rates if (f, fs) in a.g]
        for i, fs in enumerate(rates):
            rs = sorted(a.g[(f, fs)], key=lambda r: r["phase_T"])
            ax.plot([r["phase_deg"] for r in rs], [r["metrics"]["peak_ratio"] for r in rs], "o-", color=RAMP[min(8, 2 + 2 * i)],
                    lw=1.6, ms=5, label=f"{fs / 1e3:g} kHz")
        ax.axhline(1.0, color=INK2, lw=0.8, ls=":")
        ax.set(title=f"{fmt_hz(f)} input", xlabel="phase [deg of the sine period]", xticks=[0, 22.5, 45, 67.5, 90])
        ax.legend(title="sampling rate")
    axes[0].set_ylabel("peak of the calculated force / 0.2 N")
    return save(fig, root, "peak_capture.png")


def fig_methods(a: Agg, root: Path) -> str:
    """Leakage test: the three force calculations on the same ADC data."""
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.0))
    ax = axes[0]
    x = np.arange(len(a.freqs))
    w = 0.27
    for k, (m, label) in enumerate(METHODS):
        y = [np.mean([r[m]["rms_err_pct"] for r in a.runs if r["freq_hz"] == f]) for f in a.freqs]
        bars = ax.bar(x + (k - 1) * w, y, w - 0.03, color=CAT[k], label=label)
        for b, v in zip(bars, y):
            ax.text(b.get_x() + b.get_width() / 2, v * 1.12, f"{v:.3g}", ha="center", fontsize=6.5, color=INK2)
    ax.set(yscale="log", xticks=x, xticklabels=[fmt_hz(f) for f in a.freqs], xlabel="input frequency",
           ylabel="RMS error [% of 0.2 N], mean over rates and phases", title="Effect of the leakage removal")
    ax.set_ylim(top=ax.get_ylim()[1] * 2.5)
    ax.legend(loc="upper right", fontsize=8)
    ax = axes[1]
    f = 1000.0
    fs = 30e3 if (f, 30e3) in a.g else a.rates[-1]
    case = next(r for r in a.g[(f, fs)] if r["phase_T"] == 0.0)
    d = np.genfromtxt(root / "runs" / case["key"] / "samples.csv", delimiter=",", names=True)
    ax.plot(d["t_s"] * 1e3, d["force_in_N"], color=INK, lw=1.4, label="input force")
    for k, col in enumerate(["force_cal_N", "force_cal_no_leak_current_N", "force_cal_no_compensation_N"]):
        ax.plot(d["t_s"] * 1e3, d[col], color=CAT[k], lw=1.0, ls="-" if k == 0 else "--", label=METHODS[k][1])
    ax.set(xlabel="time [ms]", ylabel="force [N]", title=f"Example: {fmt_hz(f)} sine sampled at {fmt_hz(fs)}")
    ax.legend(fontsize=8, loc="lower left")
    return save(fig, root, "leakage_removal.png")


def fig_methods_vs_freq_lines(a: Agg, root: Path) -> str:
    """Error of the three calculations vs sampling rate, one panel per frequency."""
    fig, axes = plt.subplots(1, len(a.freqs), figsize=(3.0 * len(a.freqs), 3.4))
    for ax, f in zip(axes, a.freqs):
        fr = [fs for fs in a.rates if (f, fs) in a.g]
        for k, (m, label) in enumerate(METHODS):
            ax.plot(np.array(fr) / 1e3, [a.stat(f, fs, "mean", "rms_err_pct", m) for fs in fr], "o-", color=CAT[k], lw=1.5, ms=4,
                    label=label)
        ax.set(yscale="log", title=fmt_hz(f), xlabel="sampling rate [kHz]")
    axes[0].set_ylabel("RMS error [% of 0.2 N]")
    for ax in axes:
        ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    h, lab = axes[0].get_legend_handles_labels()
    fig.legend(h, lab, loc="outside lower center", ncol=3, fontsize=8)
    return save(fig, root, "leakage_removal_vs_rate.png")


def fig_signals(a: Agg, root: Path) -> str:
    """The saved signals of one case: sensor output at the mux input, ADC output, input and calculated force."""
    f, fs = (10_000.0, 50_000.0) if (10_000.0, 50_000.0) in a.g else (a.freqs[-1], a.rates[-1])
    case = next(r for r in a.g[(f, fs)] if r["phase_T"] == 0.0)
    d = np.genfromtxt(root / "runs" / case["key"] / "samples.csv", delimiter=",", names=True)
    w = np.load(root / "runs" / case["key"] / "waveform.npz")
    tm = case["timing"]
    t0, t1 = tm["t_force_s"] - 2 * tm["ts_s"], tm["t_force_end_s"] + 3 * tm["ts_s"]
    sel = (w["t"] >= t0) & (w["t"] <= t1)
    ss = (d["t_s"] >= t0) & (d["t_s"] <= t1)
    fig, axes = plt.subplots(3, 1, figsize=(8, 7), sharex=True)
    axes[0].plot(w["t"][sel] * 1e3, w["sensor_s0"][sel], color=CAT[0], lw=1.2, label="sensor output (mux input S0)")
    axes[0].plot(w["t"][sel] * 1e3, w["adc_in"][sel], color=CAT[1], lw=1.0, label="ADC input (buffer output)")
    axes[0].set_ylabel("voltage [V]")
    axes[0].legend(fontsize=8, loc="upper right")
    axes[1].step(d["t_s"][ss] * 1e3, d["adc_out_V"][ss], where="post", color=CAT[2], lw=1.2, label="ADC output (12 bit, held)")
    axes[1].plot(d["t_s"][ss] * 1e3, d["adc_out_V"][ss], "o", color=CAT[2], ms=3)
    axes[1].set_ylabel("voltage [V]")
    axes[1].legend(fontsize=8, loc="upper right")
    axes[2].plot(w["t"][sel] * 1e3, w["force_in"][sel], color=INK, lw=1.4, label="input force")
    axes[2].plot(d["t_s"][ss] * 1e3, d["force_cal_N"][ss], "o-", color=CAT[3], lw=1.0, ms=3.5, label="calculated force (at the conversions)")
    axes[2].set(xlabel="time [ms]", ylabel="force [N]")
    axes[2].legend(fontsize=8, loc="upper right")
    fig.suptitle(f"Saved signals of one case: {fmt_hz(f)} sine, {fmt_hz(fs)} sampling, phase 0", color=INK)
    return save(fig, root, "signals_example.png")


def fig_waveforms(a: Agg, root: Path) -> str:
    picks = [(1000.0, 5e3, 0.0), (1000.0, 30e3, 0.0), (10_000.0, 20e3, 0.0), (10_000.0, 20e3, 0.25),
             (20_000.0, 40e3, 0.0), (20_000.0, 60e3, 0.0)]
    picks = [p for p in picks if (p[0], p[1]) in a.g]
    fig, axes = plt.subplots(2, 3, figsize=(11, 5.6))
    for ax, (f, fs, ph) in zip(axes.flat, picks):
        case = next(r for r in a.g[(f, fs)] if abs(r["phase_T"] - ph) < 1e-6)
        d = np.genfromtxt(root / "runs" / case["key"] / "samples.csv", delimiter=",", names=True)
        w = np.load(root / "runs" / case["key"] / "waveform.npz")
        tm = case["timing"]
        t0, t1 = tm["t_force_s"] - tm["period_s"] * 0.2, tm["t_force_end_s"] + tm["period_s"] * 0.2
        sel, ss = (w["t"] >= t0) & (w["t"] <= t1), (d["t_s"] >= t0) & (d["t_s"] <= t1)
        sc = 1e6 if f >= 10_000 else 1e3
        ax.plot(w["t"][sel] * sc, w["force_in"][sel], color=INK, lw=1.2, label="input")
        ax.plot(d["t_s"][ss] * sc, d["force_cal_N"][ss], "o-", color=CAT[1], lw=0.9, ms=3.5, label="calculated")
        ax.set(title=f"{fmt_hz(f)} @ {fmt_hz(fs)}, phase {ph * 360:g}°  (rms {case['metrics']['rms_err_pct']:.3g} %)",
               xlabel="time [us]" if sc == 1e6 else "time [ms]")
        ax.title.set_fontsize(8.5)
    axes[0, 0].set_ylabel("force [N]")
    axes[1, 0].set_ylabel("force [N]")
    axes[0, 1].legend(fontsize=8, loc="lower right")
    return save(fig, root, "waveform_examples.png")


# ------------------------------------------------------------------------------------------------ report
def observations(a: Agg) -> list[str]:
    out = []
    worst = max(a.runs, key=lambda r: r["metrics"]["rms_err_pct"])
    best = min(a.runs, key=lambda r: r["metrics"]["rms_err_pct"])
    out.append(f"Worst case: {fmt_hz(worst['freq_hz'])} sampled at {fmt_hz(worst['fs_hz'])}, phase {worst['phase_deg']:g}°, "
               f"RMS error {worst['metrics']['rms_err_pct']:.1f} % of the amplitude ({worst['metrics']['max_err_pct']:.1f} % peak). "
               f"Best case: {fmt_hz(best['freq_hz'])} at {fmt_hz(best['fs_hz'])}, phase {best['phase_deg']:g}°, "
               f"{best['metrics']['rms_err_pct']:.2f} % ({best['metrics']['max_err_pct']:.2f} % peak).")
    for thr in (5.0, 2.0):
        parts = []
        for f in a.freqs:
            ok = [fs for fs in a.rates if (f, fs) in a.g and a.stat(f, fs, "max") < thr]
            parts.append(f"{fmt_hz(f)}: " + (f"{fmt_hz(min(ok))}" if ok else "not reached"))
        out.append(f"Lowest tested sampling rate with the RMS error below {thr:g} % at every phase: " + "; ".join(parts) + ".")
    spread = max(((a.stat(f, fs, "max") / a.stat(f, fs, "min"), f, fs) for (f, fs) in a.g if a.stat(f, fs, "max") >= 5.0), key=lambda x: x[0])
    out.append(f"Largest phase sensitivity among the cases with a worst-phase RMS error of 5 % or more: {fmt_hz(spread[1])} at {fmt_hz(spread[2])}, "
               f"worst phase / best phase = {spread[0]:.1f}.")
    for f in a.freqs:
        rs = [r for r in a.runs if r["freq_hz"] == f and abs(r["fs_hz"] - 2 * f) < 1e-6]
        if rs:
            pk = [r["metrics"]["peak_ratio"] for r in rs]
            out.append(f"At exactly the Nyquist rate ({fmt_hz(2 * f)} for {fmt_hz(f)}) the largest calculated force is {min(pk):.2f} .. {max(pk):.2f} "
                       f"of the true 0.2 N depending on the phase, and the amplitude/phase fit is undetermined (two samples per period).")
    for f in (a.freqs[0], a.freqs[-1]):
        m = [np.mean([r[k]["rms_err_pct"] for r in a.runs if r["freq_hz"] == f]) for k, _ in METHODS]
        out.append(f"Leakage test at {fmt_hz(f)}: mean RMS error {m[0]:.3g} % with full compensation, {m[1]:.3g} % with the leakage currents set to 0, "
                   f"{m[2]:.3g} % without any compensation.")
    return out


def build_report(root: Path) -> None:
    root = Path(root)
    runs = load_runs(root)
    a = Agg(runs)
    write_summary(root, runs)
    figs = {"freq": fig_error_vs_freq(a, root), "fs": fig_error_vs_fs(a, root), "heat": fig_heatmaps(a, root), "phase": fig_phase(a, root),
            "peak": fig_peak(a, root), "methods": fig_methods(a, root), "methods_rate": fig_methods_vs_freq_lines(a, root),
            "signals": fig_signals(a, root), "wave": fig_waveforms(a, root)}
    r0 = runs[0]
    rate_hdr = [f"{fs / 1e3:g} kHz" for fs in a.rates]

    def grid(how: str, key: str = "rms_err_pct", method: str = "metrics", digits: str = ".3g") -> str:
        rows = []
        for f in a.freqs:
            rows.append([fmt_hz(f)] + [("" if (f, fs) not in a.g else format(a.stat(f, fs, how, key, method), digits)) for fs in a.rates])
        return table(rows, ["input \\ sampling"] + rate_hdr)

    lines = [
        "# Sampling sweep: TestBoard_ADC124, one channel, sine force",
        "",
        f"{len(runs)} QSPICE runs of the TestBoard_ADC124 model through the Simulator's own model, ADC and force-calculation code "
        "(`tools/sampling_sweep.py`). Every number below is a metric of the calculated force at the ADC conversion instants.",
        "",
        "## System (fixed)",
        "",
        "| | |",
        "|---|---|",
        "| Model | TestBoard_ADC124, 1 mux (`n_mux` 1), channel 0 only, `repeat` 1 |",
        "| Sampling | continuous; no VMID visit between samples, no park on VMID (`park_on_vmid` false): the mux connects channel 0 once and never switches again |",
        f"| Sensor capacitance | {r0['cp_pF']:g} pF override in the circuit and in the force calculation (Force tab value `cp_pF`) |",
        f"| Input force | {r0['cycles']} cycles of a sine, amplitude {r0['amp_N']} N (open-circuit swing 0.8 V) |",
        f"| ADC | {r0['bits']} bit, 0 .. 3.3 V, LSB {r0['lsb_mV']:.3f} mV, no noise |",
        "| Force calculation | leakage- and charge-sharing-compensated model of the Force tab (defaults), `F = cumsum(dF)`, starting from 0 at VMID |",
        f"| Solver | QSPICE, maximum step T/20000 limited to 5 ns .. 1 us ({sum(r['wall_s'] for r in runs) / 60:.1f} min of QSPICE time in total) |",
        "",
        "## Test matrix",
        "",
        table([[fmt_hz(f), ", ".join(f"{fs / 1e3:g}" for fs in a.rates if (f, fs) in a.g), str(len(a.g.get((f, a.rates[0]), [])) or len(next(v for k, v in a.g.items() if k[0] == f)))]
               for f in a.freqs], ["input frequency", "sampling rates [kHz] (all >= 2 f)", "phases"]),
        "",
        f"Phases: 0, T/16, T/8, 3T/16, T/4 of the sine period T (0°, 22.5°, 45°, 67.5°, 90°). The sampling grid is the same in all runs; the force starts "
        f"3 samples + (phase x T mod Ts) after the first conversion, which is the same as starting the sampling later by phase x T: only the part of the "
        f"delay inside one sampling period moves the samples on the sine (`grid_phase` in `summary.csv`, as a fraction of Ts), whole sampling periods "
        f"would only lengthen the zero-force lead-in (see Notes).",
        "",
        "## Metrics",
        "",
        "- **RMS error** and **peak error**: `F_cal - F_in` at the conversion instants inside the force window (start of the sine to the end of "
        "the third cycle), as % of the 0.2 N amplitude. `F_in` is the force node of the circuit at the same instant.",
        "- **Peak ratio**: largest `|F_cal|` in the window / 0.2 N, what a peak detector on the samples would report.",
        "- **Fit gain / phase**: least-squares `a sin + b cos + c` at the known frequency (undetermined, left empty, at exactly 2 samples per period).",
        "- **End error**: `F_cal - F_in` at the last conversion (after the force ended): the accumulated drift, since `F` is a running sum.",
        "- **Leakage test**: the same ADC values are processed three ways: (1) full compensation (default), (2) `i_conn` = `i_float` = 0, "
        "i.e. the constant leakage currents removed from the model while the bias-resistor decay and charge sharing are still compensated, "
        "(3) no compensation at all (the ideal charge model, Force tab checkbox off).",
        "",
        "## Summary",
        "",
        *[f"- {o}" for o in observations(a)],
        "",
        "### RMS error vs input frequency",
        "",
        f"![error vs frequency]({figs['freq']})",
        "",
        "### RMS error vs sampling rate",
        "",
        f"![error vs sampling rate]({figs['fs']})",
        "",
        "### Tables (calculated force, full compensation)",
        "",
        "RMS error [% of 0.2 N], mean over the phases:", "", grid("mean"), "",
        "Peak error [% of 0.2 N], worst phase:", "", grid("max", "max_err_pct"), "",
        "Spread of the RMS error over the phases, worst / best:", "",
        table([[fmt_hz(f)] + [("" if (f, fs) not in a.g else f"{a.stat(f, fs, 'max') / a.stat(f, fs, 'min'):.2f}") for fs in a.rates]
               for f in a.freqs], ["input \\ sampling"] + rate_hdr), "",
        "Peak ratio (largest calculated force / 0.2 N), mean over the phases:", "", grid("mean", "peak_ratio", digits=".3f"), "",
        "Error at the last conversion, after the force ended [mN], mean over the phases:", "",
        table([[fmt_hz(f)] + [("" if (f, fs) not in a.g else f"{a.stat(f, fs, 'mean', 'end_err_N') * 1e3:.2f}") for fs in a.rates]
               for f in a.freqs], ["input \\ sampling"] + rate_hdr), "",
        f"![heatmaps]({figs['heat']})",
        "",
        "## Phase (sampling start)",
        "",
        f"![phase dependence]({figs['phase']})",
        "",
        "At exactly the Nyquist rate the phase decides whether the samples fall on the peaks or on the zero crossings:",
        "",
        f"![peak capture]({figs['peak']})",
        "",
        "## Leakage removal vs no removal",
        "",
        "Mean RMS error [% of 0.2 N] over all rates and phases, and the factor to the default:",
        "",
        table([[fmt_hz(f)] + [format(np.mean([r[m]["rms_err_pct"] for r in runs if r["freq_hz"] == f]), ".3g") for m, _ in METHODS]
               + [f"{np.mean([r['metrics_no_leak_current']['rms_err_pct'] for r in runs if r['freq_hz'] == f]) / np.mean([r['metrics']['rms_err_pct'] for r in runs if r['freq_hz'] == f]):.1f}x",
                  f"{np.mean([r['metrics_no_compensation']['rms_err_pct'] for r in runs if r['freq_hz'] == f]) / np.mean([r['metrics']['rms_err_pct'] for r in runs if r['freq_hz'] == f]):.1f}x"]
               for f in a.freqs], ["input"] + [lbl for _, lbl in METHODS] + ["currents=0 / full", "none / full"]),
        "",
        f"![leakage removal]({figs['methods']})",
        "",
        f"![leakage removal vs sampling rate]({figs['methods_rate']})",
        "",
        "Net constant leakage of the connected node in this model: drain leakage 1 nA minus 6 off channels x 1 nA = -5 nA (it charges the node); "
        "the floating sensor sees 1 nA. Leaving the 5 nA out of the calculation makes `F` drift at about 5 nA / d33 = 8 N/s while the sensor is connected.",
        "",
        "## Example waveforms",
        "",
        f"![waveform examples]({figs['wave']})",
        "",
        f"![saved signals]({figs['signals']})",
        "",
        "## Files",
        "",
        "- `summary.csv`: one row per run with all metrics (full compensation, leakage currents = 0, no compensation, ideal ADC value).",
        "- `runs/<case>/samples.csv`: per conversion `t_s, adc_in_V, adc_code, adc_out_V, sensor_s0_V, force_in_N, force_cal_N, "
        "force_cal_ideal_adc_N, force_cal_no_leak_current_N, force_cal_no_compensation_N`.",
        "- `runs/<case>/waveform.npz`: QSPICE time points `t`, `sensor_s0` (channel 0 mux input), `adc_in` (buffer output), `force_in`.",
        "- `runs/<case>/meta.json`: parameters, timing (first conversion, force start/end), all metrics.",
        "",
    ]
    notes = root / "notes.md"
    if notes.exists():
        lines += ["## Notes", "", notes.read_text(encoding="utf-8").strip(), ""]
    (root / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"report: {root / 'report.md'} ({len(runs)} runs, {len(figs)} figures)")


if __name__ == "__main__":
    build_report(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR)
