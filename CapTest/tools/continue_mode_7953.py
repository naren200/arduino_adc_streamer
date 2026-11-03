"""Does re-commanding the channel cause the frame effect on TestBoard_7953?

Every channel is measured at every gap twice: with the channel command repeated each
frame (hold_mode 0, the normal burst) and with the ADS7953 "continue" command after the
connection (hold_mode 1, SLAS605C Table 7). Conditions are shuffled per pass, so each
pass gives one paired comparison at the same sensor history.

Run from CapTest/:  python tools/continue_mode_7953.py --port COM3
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
DEFAULT_GAPS = "200,1000,5000,20000"
MIN_VERSION = (1, 1, 0)


def command(channel, samples, gap, hold, first_clock_delay_ns) -> str:
    return protocol.format_burst_command(channel[0], channel[1], samples, PARK_US, gap,
                                         first_clock_delay_ns, hold)


def summarise(rows, r_bias_of):
    lines, summary = [], []
    gaps = sorted({r["gap_ns"] for r in rows})
    for channel in sorted({(r["adc"], r["channel"]) for r in rows}):
        r_bias = r_bias_of(channel)
        ours = [r for r in rows if (r["adc"], r["channel"]) == channel and not r["priming"]]
        entry = {"adc": channel[0], "channel": channel[1], "r_bias": r_bias, "modes": {}}
        lines.append("ADC%d:%d  R_bias %.0f k" % (channel[0], channel[1], r_bias / 1e3))
        lines.append("   gap_ns   tau normal (sd)   tau continue (sd)   paired diff % (sem)   "
                     "V_inf offset mV n/c   rms n/c   first/V_D n/c")
        for gap in gaps:
            group = {m: sorted((r for r in ours if r["gap_ns"] == gap and r["hold"] == m),
                               key=lambda r: r["pass"]) for m in (0, 1)}
            tau = {m: np.array([r["tau_us"] for r in group[m]]) for m in (0, 1)}
            value = lambda m, name: float(np.nanmean([r[name] for r in group[m]]))   # noqa: E731
            offset = {m: 1e3 * float(np.nanmean([r["v_d"] - r["vinf"] for r in group[m]])) for m in (0, 1)}
            paired = {r["pass"]: r["tau_us"] for r in group[0]}
            diffs = np.array([100.0 * (r["tau_us"] / paired[r["pass"]] - 1.0) for r in group[1]
                              if r["pass"] in paired])
            sem = float(np.nanstd(diffs, ddof=1) / np.sqrt(len(diffs))) if len(diffs) > 1 else float("nan")
            lines.append("  %7d  %7.2f (%5.2f)   %7.2f (%5.2f)    %+6.2f (%4.2f)        %5.1f %5.1f     "
                         "%5.2f %5.2f   %.3f %.3f"
                         % (gap, np.nanmean(tau[0]), np.nanstd(tau[0], ddof=1), np.nanmean(tau[1]),
                            np.nanstd(tau[1], ddof=1), np.nanmean(diffs), sem, offset[0], offset[1],
                            value(0, "rms_lsb"), value(1, "rms_lsb"), value(0, "r0"), value(1, "r0")))
            entry.setdefault("gaps", []).append(
                {"gap_ns": gap, "tau_normal_us": float(np.nanmean(tau[0])),
                 "tau_continue_us": float(np.nanmean(tau[1])), "paired_diff_pct": float(np.nanmean(diffs)),
                 "paired_sem_pct": sem, "offset_normal_mv": offset[0], "offset_continue_mv": offset[1],
                 "rms_normal": value(0, "rms_lsb"), "rms_continue": value(1, "rms_lsb"),
                 "first_normal": value(0, "r0"), "first_continue": value(1, "r0")})
        for mode, name in ((0, "normal"), (1, "continue")):
            periods, taus = [], []
            for gap in gaps:
                group = [r for r in ours if r["gap_ns"] == gap and r["hold"] == mode]
                periods.append(float(np.nanmean([r["period_ns"] for r in group])))
                taus.append(float(np.nanmean([r["tau_us"] for r in group])))
            fit = line_fit(periods, taus)
            if fit and fit[1] > 0:
                slope, intercept = fit
                c_total = 1e6 / (r_bias * intercept)
                entry["modes"][name] = {"c_total_pf": c_total, "c_frame_pf": slope * c_total}
                lines.append("   %-8s C_total %.1f pF, per-frame capacitance %.2f pF"
                             % (name, c_total, slope * c_total))
            else:
                entry["modes"][name] = {"c_total_pf": float("nan"), "c_frame_pf": float("nan")}
                lines.append("   %-8s no usable line fit" % name)
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
                                           / time.strftime("%Y-%m-%d_continue_mode"))
    out.mkdir(parents=True, exist_ok=True)

    link = SerialLink(args.port)
    client = CapTestClient(link)
    rows, raw, log = [], [], []

    def say(text):
        print(text, flush=True)
        log.append(text)

    try:
        info = client.identify()
        say(info.label)
        status = client.status()
        version = tuple(int(x) for x in status.version.split("."))
        if info.kind != "captest" or info.board_id != "TestBoard_7953" or version < MIN_VERSION:
            say("Needs TestBoard_7953 CapTest firmware 1.1.0 or newer: nothing measured.")
            return 2
        if not client.set_spi_clock(settings.spi_clock_hz):
            say("spiclock rejected")
            return 2
        for adc in sorted({c[0] for c in channels}):
            reading = client.vmid(adc)
            say("Vmid ADC%d: mean code %.2f, min %d, max %d, tag errors %d"
                % (adc, reading["mean_code"], reading["min"], reading["max"], reading["tag_errors"]))

        rng = random.Random(args.seed)
        plan = [(channel, gaps[0], 0, -1) for channel in channels]
        for repeat in range(args.repeats):
            conditions = [(gap, hold) for gap in gaps for hold in (0, 1)]
            rng.shuffle(conditions)
            for gap, hold in conditions:
                plan.extend((channel, gap, hold, repeat) for channel in channels)
        say("%d bursts" % len(plan))

        last = {}
        started = time.monotonic()
        for index, (channel, gap, hold, repeat) in enumerate(plan):
            since = last.get(channel)
            if since is not None:
                time.sleep(max(0.0, args.wait_ms / 1000.0 - (time.monotonic() - since)))
            priming = repeat < 0
            try:
                text = command(channel, args.samples, gap, hold, settings.first_clock_delay_ns)
                packet = client.burst_packet(text, protocol.HEADER_BYTES, protocol.packet_length_from_header)
            except protocol.ProtocolError as exc:
                say("ADC%d:%d gap %d hold %d failed: %s" % (channel[0], channel[1], gap, hold, exc))
                last[channel] = time.monotonic()
                continue
            since_s = None if since is None else time.monotonic() - since
            last[channel] = time.monotonic()
            burst = protocol.decode_burst_packet(packet)
            if bool(burst.flags & protocol.FLAG_CONTINUE_MODE) != bool(hold):
                say("ADC%d:%d: the packet does not echo hold %d: stopped." % (channel[0], channel[1], hold))
                return 3
            row = {"index": index, "pass": repeat, "adc": channel[0], "channel": channel[1], "gap_ns": gap,
                   "hold": hold, "priming": priming, "since_previous_s": since_s,
                   **analyse(burst, settings.r_bias(*channel), settings.vref)}
            rows.append(row)
            raw.append({"index": index, "pass": repeat, "adc": channel[0], "channel": channel[1],
                        "gap_ns": gap, "hold": hold, "priming": priming, "since_previous_s": since_s,
                        "packet_b64": base64.b64encode(packet).decode("ascii")})
            if (index + 1) % 60 == 0 or index + 1 == len(plan):
                say("%d / %d bursts, %.0f s" % (index + 1, len(plan), time.monotonic() - started))
    finally:
        try:
            say("capabort: %s" % client.abort())
        except Exception as exc:   # the board state must be reported
            say("capabort failed: %s" % exc)
        link.close()

    lines, summary = summarise(rows, lambda c: settings.r_bias(*c))
    for line in lines:
        say(line)
    say("Tag errors: %d, in %d bursts" % (sum(r["tag_errors"] for r in rows), len(rows)))
    (out / "raw_bursts.json").write_text(json.dumps(
        {"format": "captest-continue-mode-1", "board": "TestBoard_7953", "samples": args.samples,
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
