"""Is the per-frame path of TestBoard_7953 gated by the SPI clock?

The frame period T is held the same at two SPI clocks by choosing the CS-high gap, so
only the clocked part of the frame changes (16 clocks: 800 ns at 20 MHz, 1600 ns at
10 MHz). From 1/tau = 1/(R * C_total) + (C_f / C_total) / T:

  - C_f proportional to the clocked time: a path that conducts while the clock runs;
  - C_f the same at both clocks: a fixed charge share at one event of the frame.

C_total should not change in either case. Conditions are shuffled per pass; the board is
left parked on Vmid at 20 MHz.

Run from CapTest/:  python tools/spi_clock_7953.py --port COM3
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from captest import protocol                        # noqa: E402
from captest.client import CapTestClient            # noqa: E402
from captest.link import SerialLink                 # noqa: E402
from captest.settings import Settings, parse_channel_spec  # noqa: E402
from gap_sweep_7953 import PARK_US, analyse, line_fit  # noqa: E402

DEFAULT_CHANNELS = "1:0,1:2,1:7,2:2,2:7,2:12,3:0,3:2,3:5,3:7,4:0,4:2,4:7,4:12,4:13"
DEFAULT_PERIODS = "2500,5000,10000,20000"
DEFAULT_CLOCKS = "20000000,10000000"
FINAL_CLOCK_HZ = 20_000_000
PRIMING_BURSTS = 2


def burst(client, channel, samples, gap, first_clock_delay_ns):
    text = protocol.format_burst_command(channel[0], channel[1], samples, PARK_US, gap, first_clock_delay_ns)
    return client.burst_packet(text, protocol.HEADER_BYTES, protocol.packet_length_from_header)


def calibrate_gap(client, channel, clock_hz, period_ns, first_clock_delay_ns, say) -> int:
    """The gap that gives `period_ns` at this clock, from the measured frame period."""
    gap = max(int(period_ns - 16e9 / clock_hz - 400), 0)
    measured = float("nan")
    for _ in range(4):
        packet = burst(client, channel, 64, gap, first_clock_delay_ns)
        measured = protocol.decode_burst_packet(packet).period_ns()["mean"]
        if abs(measured - period_ns) < 3.0:
            break
        gap = max(int(round(gap + period_ns - measured)), 0)
        time.sleep(0.05)
    say("clock %.0f MHz, T %d ns: gap %d ns, measured %.1f ns" % (clock_hz / 1e6, period_ns, gap, measured))
    return gap


def summarise(rows, r_bias_of, clocks):
    lines, summary = [], []
    periods = sorted({r["target_ns"] for r in rows})
    for channel in sorted({(r["adc"], r["channel"]) for r in rows}):
        r_bias = r_bias_of(channel)
        ours = [r for r in rows if (r["adc"], r["channel"]) == channel and not r["priming"]]
        entry = {"adc": channel[0], "channel": channel[1], "r_bias": r_bias, "clocks": {}, "periods": []}
        lines.append("ADC%d:%d  R_bias %.0f k" % (channel[0], channel[1], r_bias / 1e3))
        lines.append("   T_ns    " + "   ".join("tau @%2.0f MHz (sd)" % (c / 1e6) for c in clocks)
                     + "   paired diff % (sem)   V_inf offset mV   rms   first/V_D")
        for period in periods:
            group = {c: sorted((r for r in ours if r["target_ns"] == period and r["clock_hz"] == c),
                               key=lambda r: r["pass"]) for c in clocks}
            tau = {c: np.array([r["tau_us"] for r in group[c]]) for c in clocks}
            value = lambda c, n: float(np.nanmean([r[n] for r in group[c]]))   # noqa: E731
            base = {r["pass"]: r["tau_us"] for r in group[clocks[0]]}
            diffs = np.array([100.0 * (r["tau_us"] / base[r["pass"]] - 1.0) for r in group[clocks[1]]
                              if r["pass"] in base])
            sem = float(np.nanstd(diffs, ddof=1) / np.sqrt(len(diffs))) if len(diffs) > 1 else float("nan")
            offset = {c: 1e3 * float(np.nanmean([r["v_d"] - r["vinf"] for r in group[c]])) for c in clocks}
            lines.append("  %6d  " % period
                         + "   ".join("%7.2f (%5.2f)   " % (np.nanmean(tau[c]), np.nanstd(tau[c], ddof=1)) for c in clocks)
                         + "  %+6.2f (%4.2f)      %5.1f %5.1f   %5.2f %5.2f   %.3f %.3f"
                         % (np.nanmean(diffs), sem, offset[clocks[0]], offset[clocks[1]],
                            value(clocks[0], "rms_lsb"), value(clocks[1], "rms_lsb"),
                            value(clocks[0], "r0"), value(clocks[1], "r0")))
            entry["periods"].append({"target_ns": period, "paired_diff_pct": float(np.nanmean(diffs)),
                                     "paired_sem_pct": sem,
                                     **{"tau_%d_us" % c: float(np.nanmean(tau[c])) for c in clocks},
                                     **{"period_%d_ns" % c: value(c, "period_ns") for c in clocks},
                                     **{"offset_%d_mv" % c: offset[c] for c in clocks}})
        for clock in clocks:
            points = [(float(np.nanmean([r["period_ns"] for r in ours if r["target_ns"] == p and r["clock_hz"] == clock])),
                       float(np.nanmean([r["tau_us"] for r in ours if r["target_ns"] == p and r["clock_hz"] == clock])))
                      for p in periods]
            fit = line_fit([p[0] for p in points], [p[1] for p in points])
            if fit and fit[1] > 0:
                slope, intercept = fit
                c_total = 1e6 / (r_bias * intercept)
                predicted = intercept + slope * 1e3 / np.array([p[0] for p in points])
                misfit = float(np.max(np.abs(predicted * np.array([p[1] for p in points]) - 1.0)) * 100.0)
                entry["clocks"][str(clock)] = {"c_total_pf": c_total, "c_frame_pf": slope * c_total,
                                               "misfit_pct": misfit}
                lines.append("   %2.0f MHz: C_total %.1f pF, per-frame capacitance %.2f pF, worst tau misfit %.1f %%"
                             % (clock / 1e6, c_total, slope * c_total, misfit))
            else:
                entry["clocks"][str(clock)] = {"c_total_pf": float("nan"), "c_frame_pf": float("nan"),
                                               "misfit_pct": float("nan")}
                lines.append("   %2.0f MHz: no usable line fit" % (clock / 1e6))
        summary.append(entry)
    return lines, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--channels", default=DEFAULT_CHANNELS)
    parser.add_argument("--periods", default=DEFAULT_PERIODS, help="frame periods in ns")
    parser.add_argument("--clocks", default=DEFAULT_CLOCKS, help="SPI clocks in Hz, the first is the reference")
    parser.add_argument("--samples", type=int, default=512)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--wait-ms", type=float, default=500.0, help="time between bursts of a channel")
    parser.add_argument("--seed", type=int, default=7953)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    settings = Settings.for_board("TestBoard_7953")
    channels = parse_channel_spec(args.channels)
    periods = [int(p) for p in args.periods.split(",")]
    clocks = [int(c) for c in args.clocks.split(",")]
    out = Path(args.out) if args.out else (Path(__file__).resolve().parents[1] / "Results" / "TestBoard_7953"
                                           / time.strftime("%Y-%m-%d_spi_clock"))
    out.mkdir(parents=True, exist_ok=True)

    link = SerialLink(args.port)
    client = CapTestClient(link)
    rows, raw, log = [], [], []

    def say(text):
        print(text, flush=True)
        log.append(text)

    def set_clock(clock_hz):
        if not client.set_spi_clock(clock_hz):
            raise protocol.ProtocolError("spiclock %d rejected" % clock_hz)

    fcd = settings.first_clock_delay_ns
    try:
        info = client.identify()
        say(info.label)
        if info.kind != "captest" or info.board_id != "TestBoard_7953":
            say("Not the TestBoard_7953 capacitance-test firmware: nothing measured.")
            return 2
        gaps = {}
        for clock in clocks:
            set_clock(clock)
            for period in periods:
                gaps[(clock, period)] = calibrate_gap(client, channels[0], clock, period, fcd, say)

        rng = random.Random(args.seed)
        plan = [(channel, clocks[0], periods[0], -1) for _ in range(PRIMING_BURSTS) for channel in channels]
        for repeat in range(args.repeats):
            conditions = [(clock, period) for clock in clocks for period in periods]
            rng.shuffle(conditions)
            for clock, period in conditions:
                plan.extend((channel, clock, period, repeat) for channel in channels)
        say("%d bursts" % len(plan))

        last = {}
        current = None
        started = time.monotonic()
        for index, (channel, clock, period, repeat) in enumerate(plan):
            if clock != current:
                set_clock(clock)
                current = clock
            since = last.get(channel)
            if since is not None:
                time.sleep(max(0.0, args.wait_ms / 1000.0 - (time.monotonic() - since)))
            gap = gaps[(clock, period)]
            try:
                packet = burst(client, channel, args.samples, gap, fcd)
            except protocol.ProtocolError as exc:
                say("ADC%d:%d %d Hz T %d failed: %s" % (channel[0], channel[1], clock, period, exc))
                last[channel] = time.monotonic()
                continue
            since_s = None if since is None else time.monotonic() - since
            last[channel] = time.monotonic()
            decoded = protocol.decode_burst_packet(packet)
            if decoded.spi_clock_hz != clock:
                say("Packet reports %d Hz, expected %d Hz: stopped." % (decoded.spi_clock_hz, clock))
                return 3
            row = {"index": index, "pass": repeat, "adc": channel[0], "channel": channel[1], "clock_hz": clock,
                   "target_ns": period, "gap_ns": gap, "priming": repeat < 0, "since_previous_s": since_s,
                   "packet_flags": decoded.flags,
                   **analyse(decoded, settings.r_bias(*channel), settings.vref)}
            rows.append(row)
            raw.append({"index": index, "pass": repeat, "adc": channel[0], "channel": channel[1], "clock_hz": clock,
                        "target_ns": period, "gap_ns": gap, "priming": repeat < 0, "since_previous_s": since_s,
                        "packet_b64": base64.b64encode(packet).decode("ascii")})
            if (index + 1) % 60 == 0 or index + 1 == len(plan):
                say("%d / %d bursts, %.0f s" % (index + 1, len(plan), time.monotonic() - started))
    finally:
        try:
            say("spiclock %d: %s" % (FINAL_CLOCK_HZ, client.set_spi_clock(FINAL_CLOCK_HZ)))
            say("capabort: %s" % client.abort())
        except Exception as exc:   # the board state must be reported
            say("restoring the board failed: %s" % exc)
        link.close()

    lines, summary = summarise(rows, lambda c: settings.r_bias(*c), clocks)
    for line in lines:
        say(line)
    say("Tag errors: %d, flagged packets: %d, in %d bursts"
        % (sum(r["tag_errors"] for r in rows), sum(1 for r in rows if r["packet_flags"]), len(rows)))
    (out / "raw_bursts.json").write_text(json.dumps(
        {"format": "captest-spi-clock-1", "board": "TestBoard_7953", "samples": args.samples,
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
