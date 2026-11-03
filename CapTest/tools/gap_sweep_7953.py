"""Frame-period sweep on TestBoard_7953: is the charge-up an RC or locked to the frames?

Same channels, same sample count, only the CS-high gap (so the frame period T) changes:

  - a real RC keeps tau constant in microseconds, whatever T is;
  - a charge transfer once per frame keeps the step per frame `a` constant, so tau grows
    with T.

Both together follow 1/tau = 1/(R * C_total) + (C_D / C_total) / T, so the line of 1/tau
against 1/T separates them: the intercept is the RC part, the slope the per-frame part.

Run from CapTest/:  python tools/gap_sweep_7953.py --port COM3
Writes raw packets, one row per burst and the summary to --out. The burst is the normal
capburst; the board is left parked on Vmid. The first burst of each channel only primes
the sensor to a known history and is left out of the summary.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import random
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from captest import capfit, protocol                # noqa: E402
from captest.client import CapTestClient            # noqa: E402
from captest.link import SerialLink                 # noqa: E402
from captest.settings import Settings, parse_channel_spec  # noqa: E402

DEFAULT_CHANNELS = "1:0,1:2,2:0,2:2,3:0,3:2,4:0,4:13"
DEFAULT_GAPS = "200,1000,5000,20000"
PARK_US = 200


def analyse(burst: protocol.BurstData, r_bias: float, vref: float) -> dict:
    """Numbers of one burst. No samples are discarded: with long frames the first ones
    carry most of the curve."""
    settings = capfit.AnalysisSettings(vref=vref, discard_k=0, fit_model=capfit.FIT_ONE)
    m = capfit.analyze_burst(burst, r_bias, settings)
    words = (burst.words[protocol.LEAD_FRAMES:] & 0x0FFF).astype(np.float64)
    volts = words * vref / 4096.0
    stamps = protocol.timestamps_to_seconds(burst.timestamps, burst.cpu_hz, origin_index=1)
    period = float(np.median(np.diff(stamps[protocol.LEAD_FRAMES:])))
    target = float(np.mean(volts[-max(8, len(volts) // 10):]))
    # Fraction of the remaining distance covered per frame, over the first three steps.
    step = float((volts[3] - volts[0]) / np.sum(target - volts[:3]))
    early_tau = -period / np.log(1.0 - step) if 0.0 < step < 1.0 else float("nan")
    return {"period_ns": period * 1e9, "tau_us": m.tau_1exp * 1e6, "rms_lsb": m.rms_1exp,
            "tau2_us": m.tau_2exp * 1e6, "v0": m.v0_1exp, "vinf": m.vinf, "v_d": m.v_d,
            "first_v": float(volts[0]), "r0": float(volts[0] / m.v_d), "step_per_frame": step,
            "early_tau_us": early_tau * 1e6, "target_v": target,
            "t_end_us": float(m.t[-1] * 1e6) if len(m.t) else float("nan"),
            "tag_errors": burst.tag_errors, "flags": " ".join(m.flags)}


def line_fit(period_ns, tau_us):
    """Slope and intercept of 1/tau [1/us] against 1/T [1/us]; None without enough points."""
    x = 1e3 / np.asarray(period_ns, dtype=np.float64)
    y = 1.0 / np.asarray(tau_us, dtype=np.float64)
    good = np.isfinite(x) & np.isfinite(y)
    if good.sum() < 3:
        return None
    slope, intercept = np.polyfit(x[good], y[good], 1)
    return float(slope), float(intercept)


def summarise(rows, r_bias_of):
    lines = []
    summary = []
    for channel in sorted({(r["adc"], r["channel"]) for r in rows}):
        r_bias = r_bias_of(channel)
        ours = [r for r in rows if (r["adc"], r["channel"]) == channel and not r["priming"]]
        points = []
        lines.append("ADC%d:%d  R_bias %.0f k" % (channel[0], channel[1], r_bias / 1e3))
        lines.append("   gap_ns   T_ns  tau_us (sd)   early_tau_us  step/frame  first/V_D   V_inf    rms")
        for gap in sorted({r["gap_ns"] for r in ours}):
            group = [r for r in ours if r["gap_ns"] == gap]
            mean = lambda name: float(np.nanmean([g[name] for g in group]))   # noqa: E731
            sd = float(np.nanstd([g["tau_us"] for g in group], ddof=1)) if len(group) > 1 else 0.0
            points.append((mean("period_ns"), mean("tau_us"), mean("early_tau_us"),
                           mean("step_per_frame"), mean("r0")))
            lines.append("  %7d %6.0f  %7.2f (%.2f)  %8.2f    %8.4f   %8.4f  %7.4f  %5.2f"
                         % (gap, mean("period_ns"), mean("tau_us"), sd, mean("early_tau_us"),
                            mean("step_per_frame"), mean("r0"), mean("vinf"), mean("rms_lsb")))
        entry = {"adc": channel[0], "channel": channel[1], "r_bias": r_bias, "points": points}
        fit = line_fit([p[0] for p in points], [p[1] for p in points])
        if fit:
            slope, intercept = fit
            entry.update(slope=slope, intercept=intercept)
            # tau [us] / R [ohm] * 1e6 = C [pF]
            rc_c = 1e6 / (r_bias * intercept) if intercept > 0 else float("nan")
            predicted = intercept + slope * 1e3 / np.array([p[0] for p in points])
            misfit = float(np.max(np.abs(predicted * np.array([p[1] for p in points]) - 1.0)) * 100.0)
            lines.append("   1/tau line: per-frame fraction %.4f, RC part %.1f us -> C_total %.1f pF, "
                         "per-frame capacitance %.2f pF, worst tau misfit %.1f %%"
                         % (slope, 1.0 / intercept if intercept else float("nan"), rc_c, slope * rc_c, misfit))
            entry.update(c_total_pf=rc_c, c_frame_pf=slope * rc_c, misfit_pct=misfit)
        lines.append("   tau(T max) / tau(T min) = %.2f for a T ratio of %.2f"
                     % (points[-1][1] / points[0][1], points[-1][0] / points[0][0]))
        summary.append(entry)
    return lines, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--channels", default=DEFAULT_CHANNELS)
    parser.add_argument("--gaps", default=DEFAULT_GAPS, help="CS-high gaps in ns")
    parser.add_argument("--samples", type=int, default=512)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--wait-ms", type=float, default=500.0, help="time between bursts of a channel")
    parser.add_argument("--seed", type=int, default=7953)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    settings = Settings.for_board("TestBoard_7953")
    channels = parse_channel_spec(args.channels)
    gaps = [int(g) for g in args.gaps.split(",")]
    out = Path(args.out) if args.out else (Path(__file__).resolve().parents[1] / "Results" / "TestBoard_7953"
                                           / time.strftime("%Y-%m-%d_gap_sweep"))
    out.mkdir(parents=True, exist_ok=True)

    link = SerialLink(args.port)
    client = CapTestClient(link)
    rows, raw = [], []
    log = []

    def say(text):
        print(text, flush=True)
        log.append(text)

    try:
        info = client.identify()
        say(info.label)
        if info.kind != "captest" or info.board_id != "TestBoard_7953":
            say("Not the TestBoard_7953 capacitance-test firmware: nothing measured.")
            return 2
        if not client.set_spi_clock(settings.spi_clock_hz):
            say("spiclock rejected")
            return 2
        for adc in sorted({c[0] for c in channels}):
            reading = client.vmid(adc)
            say("Vmid ADC%d: mean code %.2f, min %d, max %d, tag errors %d"
                % (adc, reading["mean_code"], reading["min"], reading["max"], reading["tag_errors"]))

        rng = random.Random(args.seed)
        plan = []
        for channel in channels:
            plan.append((channel, gaps[0], True))
        for _ in range(args.repeats):
            # Pass-major with the gaps shuffled per pass, so drift does not follow the gap.
            order = list(gaps)
            rng.shuffle(order)
            for gap in order:
                plan.extend((channel, gap, False) for channel in channels)
        say("%d bursts, about %.0f s" % (len(plan), len(plan) * args.wait_ms / 1000.0 / len(channels)))

        last = {}
        started = time.monotonic()
        for index, (channel, gap, priming) in enumerate(plan):
            since = last.get(channel)
            if since is not None:
                time.sleep(max(0.0, args.wait_ms / 1000.0 - (time.monotonic() - since)))
            try:
                text = protocol.format_burst_command(channel[0], channel[1], args.samples, PARK_US, gap,
                                                     settings.first_clock_delay_ns)
                packet = client.burst_packet(text, protocol.HEADER_BYTES, protocol.packet_length_from_header)
            except protocol.ProtocolError as exc:
                say("ADC%d:%d gap %d failed: %s" % (channel[0], channel[1], gap, exc))
                last[channel] = time.monotonic()
                continue
            since_s = None if since is None else time.monotonic() - since
            last[channel] = time.monotonic()
            burst = protocol.decode_burst_packet(packet)
            row = {"index": index, "adc": channel[0], "channel": channel[1], "gap_ns": gap,
                   "priming": priming, "since_previous_s": since_s,
                   **analyse(burst, settings.r_bias(*channel), settings.vref)}
            rows.append(row)
            raw.append({"index": index, "adc": channel[0], "channel": channel[1], "gap_ns": gap,
                        "priming": priming, "since_previous_s": since_s,
                        "packet_b64": base64.b64encode(packet).decode("ascii")})
            if (index + 1) % 20 == 0 or index + 1 == len(plan):
                say("%d / %d bursts, %.0f s" % (index + 1, len(plan), time.monotonic() - started))
    finally:
        try:
            say("capabort: %s" % client.abort())
        except (protocol.ProtocolError, Exception) as exc:   # the board state must be reported
            say("capabort failed: %s" % exc)
        link.close()

    lines, summary = summarise(rows, lambda c: settings.r_bias(*c))
    for line in lines:
        say(line)
    (out / "raw_bursts.json").write_text(json.dumps(
        {"format": "captest-gap-sweep-1", "board": "TestBoard_7953", "samples": args.samples,
         "park_us": PARK_US, "bursts": raw}), encoding="utf-8")
    with open(out / "bursts.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (out / "run.log").write_text("\n".join(log) + "\n", encoding="utf-8")
    say("Saved to %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
