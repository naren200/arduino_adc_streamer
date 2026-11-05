"""Peak-to-peak graph set of a basic_fixed_force_sweep.py result folder, each graph in two versions, and its Word file.

    python Qspice/Simulator/tools/pzt_graphs.py results/basic_model_fixed_force_R20M_noise0p4mV_fs100kHz_100ph_<date>_<time>

Reads summary.csv of the folder and writes
    figures/g_<graph>_full.png     all frequencies (0.1 Hz .. 10 kHz)
    figures/g_<graph>_to5kHz.png   the same graph without the frequencies above 5 kHz
    graphs_report.md, PZT_Freq_Tests_Graphs.docx   (the figures with captions, the table of the dB ratio)

Graphs: 1. ratio of the peak-to-peak (calculated / applied) in dB, 2. applied and calculated peak-to-peak in N, 3. peak-to-peak error (log
axis), 4. peak-to-peak error (linear axis, with a zoom to 1 kHz), 5. peak-to-peak error vs sampling phase. All values are those of the last
force cycle. Graphs 1-4: the blue line is the median over the sampling phases, the orange dots are the individual phases. Errors are numbers,
not percent. dB = 20 log10(calculated peak-to-peak / applied peak-to-peak), 1.00 = 0 dB.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sampling_sweep_report import CAT, INK2, table  # noqa: E402  (also sets the matplotlib style)
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402

SERIES = CAT + ["#008300", "#6250d6"]          # categorical slots 1-7 (blue, orange, aqua, yellow, magenta, green, violet), fixed per frequency
MARKERS = "osv^DPX"
BLUE, ORANGE = CAT[0], CAT[1]
VERSIONS = [("full", None, "all frequencies, 0.1 Hz to 10 kHz"), ("to5kHz", 5000.0, "up to 5 kHz, without 10 kHz")]   # in the Word captions
ERR_LABEL = "peak-to-peak error [fraction of the input p-p]"


def fmt_hz(f: float) -> str:
    return f"{f / 1e3:g} kHz" if f >= 1000 else f"{f:g} Hz"


def load(root: Path) -> dict:
    """{freq: {'phase', 'pp_cal', 'err', 'pp_in'}} from summary.csv (err = |p-p error| as a fraction)."""
    rows = list(csv.DictReader((root / "summary.csv").open(encoding="utf-8")))
    out: dict = {}
    for r in rows:
        d = out.setdefault(float(r["freq_hz"]), {"phase": [], "pp_cal": [], "err": [], "pp_in": 2 * float(r["amp_N"])})
        d["phase"].append(float(r["phase_of_ts"]))
        d["pp_cal"].append(float(r["pp_cal_last_N"]))
        d["err"].append(float(r["pp_err_last_pct"]) / 100.0)
    return {f: {k: (np.array(v) if isinstance(v, list) else v) for k, v in d.items()} for f, d in sorted(out.items())}


def sd(v) -> float:
    return float(np.std(v, ddof=1)) if len(v) > 1 else 0.0


def ticks(ax, freqs):
    ax.set_xticks(freqs, [fmt_hz(f) for f in freqs], rotation=30, ha="right")
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())


def save(fig, root: Path, name: str) -> str:
    (root / "figures").mkdir(exist_ok=True)
    fig.savefig(root / "figures" / name, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return f"figures/{name}"


def median_and_phases(ax, data, freqs, values, label_median="median", label_dots="each sampling phase"):
    """The common look of graphs 1-4: orange dots = every sampling phase, blue dashed line with squares = median over the phases."""
    for f in freqs:
        v = values(f)
        ax.plot([f] * len(v), v, ".", color=ORANGE, alpha=0.35, ms=5, zorder=2)
    med = np.array([np.median(values(f)) for f in freqs])
    ax.plot(freqs, med, "s--", color=BLUE, lw=1.6, ms=5.5, zorder=3)
    ax.plot([], [], ".", color=ORANGE, ms=7, label=label_dots)
    ax.plot([], [], "s--", color=BLUE, lw=1.6, ms=5.5, label=label_median)
    return med


# ---------------------------------------------------------------------------------------------- graphs
def g_ratio_db(data, freqs):
    fig, ax = plt.subplots(figsize=(7.4, 4.8))
    median_and_phases(ax, data, freqs, lambda f: 20 * np.log10(data[f]["pp_cal"] / data[f]["pp_in"]))
    ax.axhline(0.0, color=INK2, lw=1, ls=":")
    ax.set(xscale="log", xlabel="force frequency", ylabel="calculated / applied peak-to-peak [dB]", title="Ratio of the peak-to-peak")
    ticks(ax, freqs)
    ax.legend(fontsize=8, loc="lower left")
    return fig


def g_pp_n(data, freqs):
    fig, ax = plt.subplots(figsize=(7.4, 4.8))
    pp_in = data[freqs[0]]["pp_in"]
    ax.plot(freqs, [pp_in] * len(freqs), "o-", color=INK2, lw=1.6, ms=6, label="applied (input) peak-to-peak", zorder=1)
    median_and_phases(ax, data, freqs, lambda f: data[f]["pp_cal"], "calculated, median", "calculated, each sampling phase")
    ax.set(xscale="log", xlabel="force frequency", ylabel="peak-to-peak force [N]", ylim=(0, pp_in * 1.1),
           title="Applied and calculated peak-to-peak force")
    ticks(ax, freqs)
    ax.legend(fontsize=8, loc="lower left")
    return fig


def g_err_log(data, freqs):
    fig, ax = plt.subplots(figsize=(7.4, 4.8))
    median_and_phases(ax, data, freqs, lambda f: data[f]["err"])
    ax.set(yscale="log", xscale="log", xlabel="force frequency", ylabel=ERR_LABEL, title="Peak-to-peak error")
    ticks(ax, freqs)
    ax.legend(fontsize=8, loc="upper left")
    return fig


def g_err_linear(data, freqs):
    low = [f for f in freqs if f <= 1000.0]
    fig, axes = plt.subplots(2, 1, figsize=(7.4, 8.6))
    for ax, fs, title in ((axes[0], freqs, "Peak-to-peak error - linear axis"), (axes[1], low, "Peak-to-peak error - linear axis, zoom")):
        median_and_phases(ax, data, fs, lambda f: data[f]["err"])
        ax.axhline(0.0, color=INK2, lw=0.8, ls=":")
        ax.set(xscale="log", xlabel="force frequency", ylabel=ERR_LABEL, title=title)
        ticks(ax, fs)
        ax.legend(fontsize=8, loc="upper left")
    return fig


def g_err_phase(data, freqs):
    fig, ax = plt.subplots(figsize=(7.6, 4.8))
    for f, c, mk in zip(freqs, SERIES, MARKERS):     # colour and marker are fixed per frequency, whatever the version
        ax.plot(data[f]["phase"], data[f]["err"], mk + "-", color=c, lw=0.5, ms=3.4, alpha=0.8, label=fmt_hz(f))
    ax.set(yscale="log", xlabel="sampling start [fraction of the sampling period Ts]", ylabel=ERR_LABEL,
           title="Peak-to-peak error vs sampling phase")
    ax.legend(title="force frequency", fontsize=8, ncol=2, loc="lower left")
    return fig


GRAPHS = [
    ("ratio_dB", g_ratio_db, "Ratio of the peak-to-peak",
     "Calculated / applied peak-to-peak in dB (1.00 = 0 dB): blue = median over the sampling phases, orange dots = each sampling phase"),
    ("pp_N", g_pp_n, "Applied and calculated peak-to-peak",
     "Applied and calculated peak-to-peak force in N: blue = median over the sampling phases, orange dots = each sampling phase"),
    ("err_log", g_err_log, "Peak-to-peak error, log axis",
     "Peak-to-peak error as a number, log axis: blue = median over the sampling phases, orange dots = each sampling phase"),
    ("err_linear", g_err_linear, "Peak-to-peak error, linear axis",
     "Peak-to-peak error as a number, linear axis (frequency axis still logarithmic), below it a zoom to 0.1 Hz to 1 kHz: blue = median, orange dots = each sampling phase"),
    ("err_phase", g_err_phase, "Peak-to-peak error vs sampling phase",
     "Peak-to-peak error as a number for each sampling phase, one line per force frequency"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="result folder of basic_fixed_force_sweep.py (needs summary.csv)")
    ap.add_argument("--no-docx", action="store_true", help="figures and graphs_report.md only")
    args = ap.parse_args()
    root = Path(args.folder)
    data = load(root)
    all_freqs = list(data)
    n_ph = len(data[all_freqs[0]]["phase"])

    figs: dict[tuple[str, str], str] = {}
    for key, fn, _, _ in GRAPHS:
        for ver, fmax, _ in VERSIONS:
            freqs = [f for f in all_freqs if fmax is None or f <= fmax]
            figs[(key, ver)] = save(fn(data, freqs), root, f"g_{key}_{ver}.png")

    # the numbers behind graph 1
    rows = []
    for f in all_freqs:
        ratio = data[f]["pp_cal"] / data[f]["pp_in"]
        db = 20 * np.log10(ratio)
        rows.append([fmt_hz(f), f"{np.median(ratio):.5f}", f"{np.median(db):+.4f}", f"{sd(db):.4f}"])
    t_db = table(rows, ["force frequency", "median ratio calculated / applied", "median [dB]", "std over the phases [dB]"])

    parts = [f"""# PZT Sensor – Sin Force Tests: peak-to-peak graphs

Generated by `Qspice/Simulator/tools/pzt_graphs.py` from the {sum(len(d['phase']) for d in data.values())} QSPICE runs of the folder ({len(all_freqs)} frequencies x {n_ph} random sampling phases).

Every graph is shown twice: for all frequencies (0.1 Hz to 10 kHz) and up to 5 kHz (without 10 kHz). All peak-to-peak values are those of the last force cycle. The blue line is the median over the {n_ph} sampling phases and the orange dots are the individual phases. Errors are numbers, not percent: 0.01 = 1 % of the input peak-to-peak. The ratio of the peak-to-peak values is given in dB = 20 log10(calculated / applied), so 1.00 = 0 dB.

## Ratio of the peak-to-peak

{t_db}
"""]
    for i, (key, _, title, desc) in enumerate(GRAPHS):
        if i:
            parts.append(f"## {title}\n")
        for ver, _, sub in VERSIONS:
            parts.append(f"- `{figs[(key, ver)]}`: {desc} - {sub}\n")
        parts.append("\n".join(f"![{title}, {ver}]({figs[(key, ver)]})" for ver, _, _ in VERSIONS) + "\n")
    (root / "graphs_report.md").write_text("\n".join(parts), encoding="utf-8")
    print(f"figures: {len(figs)} in {root / 'figures'}\nreport:  {root / 'graphs_report.md'}")
    if not args.no_docx:
        from report_to_docx import build_docx
        print(f"word:    {build_docx(root, file_name='PZT_Freq_Tests_Graphs.docx', header=False, md_file='graphs_report.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
