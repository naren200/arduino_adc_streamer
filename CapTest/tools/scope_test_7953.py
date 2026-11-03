"""Guided oscilloscope test of the per-frame charge transfer on TestBoard_7953.

Walks through the probe and scope setup, then fires one burst per shot when you press
Enter, so a single-shot capture on the scope catches the connection. It answers: does the
node jump at one point of every frame, and at which point?

Run from CapTest/:  uv run python tools/scope_test_7953.py --port COM3
Answers, your notes and the raw packets are saved after every shot. The board is left
parked on Vmid.

--set alternate (firmware 1.2.0, capalt) switches between the sensor and Vmid during the
burst, as production does with Vmid between channels. Each return to Vmid resets the node
to a known voltage, so the cycle-to-cycle approach of the sensor to Vmid gives the shared
node capacitance directly: r = (1 - C_share/C_total) * exp(-S*T/tau) per cycle of S sensor
frames. Shots with different S*T on one channel give tau and C_share without C_total.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from captest import capfit, protocol                # noqa: E402
from captest.client import CapTestClient            # noqa: E402
from captest.link import LinkError, SerialLink      # noqa: E402
from captest.settings import Settings, parse_channel_spec  # noqa: E402

PARK_US = 200
DEFAULT_CLOCK_HZ = 20_000_000
TRIGGER_LEVEL_V = 1.0
CS_PIN = {1: 9, 2: 24, 3: 32, 4: 33}
SCK_PIN = {1: 13, 2: 13, 3: 27, 4: 27}
# C_f against node voltage, rested bursts of 2026-10-30, default gap: (upper edge V, pF).
C_FRAME_CURVE = ((0.2, 6.3), (0.4, 5.5), (0.6, 4.8), (0.8, 4.1), (1.0, 3.4), (1.1, 2.9), (9.9, 2.6))
TAU_WARNING = 0.10          # a larger change than this suggests the probe loads the node
FIT_MIN_DISTANCE_V = 0.03   # alternate fit: use cycles still this far from Vmid (about 50 LSB)

BUFFER = "buffer output"
MXO = "MXO"


@dataclass
class Shot:
    name: str
    channel: tuple
    gap_ns: int
    samples: int
    timebase: str
    volts_per_div: str
    look_for: str
    hold_mode: Optional[int] = None
    rest_s: float = 1.0
    probe: str = BUFFER
    expected_tau_us: Optional[float] = None
    optional: bool = False
    pattern: Optional[Tuple[int, int]] = None    # capalt: (sensor frames, Vmid frames) per cycle


def c_frame_pf(volts: float) -> float:
    for edge, value in C_FRAME_CURVE:
        if volts < edge:
            return value
    return C_FRAME_CURVE[-1][1]


def build_shots(small, large, rest_large_s, large_gap_ns=5000) -> List[Shot]:
    default_channels = small == (1, 0) and large == (1, 2)

    def tau(value):
        return value if default_channels else None

    return [
        Shot("Overview, small sensor", small, 200, 256, "20 us/div", "200 mV/div",
             "A fast drop from Vmid to a low level at the connection, then a rise back toward "
             "Vmid. This shot only confirms the trigger and probe work.",
             expected_tau_us=tau(18.3)),
        Shot("Frame detail, long gap (the main shot)", small, 5000, 64, "2 us/div", "100 mV/div",
             "Frames are about 6.2 us apart, so a jump stands out from the slow R_bias slope. Look at "
             "the first 3-4 frames. Does the level jump once per frame, and does it stay at the new "
             "level? Zoom in on one jump and note where it falls against CS and SCLK: CS falling, the "
             "2nd SCLK falling edge (mux selects), the 14th SCLK rising edge (acquisition starts), or "
             "CS rising.",
             expected_tau_us=tau(43.4)),
        Shot("Frame detail, default gap", small, 200, 64, "500 ns/div", "100 mV/div",
             "The same event at the normal 1.37 us frame. Is the jump at the same place in the frame "
             "as in the long-gap shot?",
             expected_tau_us=tau(18.3)),
        Shot("Large sensor, rested", large, large_gap_ns, 64, "2 us/div", "50 mV/div",
             "The same jump, but smaller: the same charge goes into a larger capacitance. Compare its "
             "size with the small sensor against the prediction printed after the shot. A first sample/V_D "
             "above about 0.2 means the sensor had not rested long enough; raise --rest-large-s.",
             rest_s=rest_large_s, expected_tau_us=None),
        Shot("Continue mode", small, 5000, 64, "2 us/div", "100 mV/div",
             "The channel is not re-commanded here. Compare with the main shot: same jump, same place "
             "in the frame?",
             hold_mode=1, expected_tau_us=tau(43.4)),
        Shot("MXO directly (optional)", small, 5000, 64, "2 us/div", "100 mV/div",
             "Do this one only if the buffer output left you unsure whether a jump is real. Use the "
             "lowest-capacitance probe you have, because the probe adds to C_total and tau will be "
             "longer. The buffer isolates MXO from the ADC kick, so a jump here is on the sensor node.",
             probe=MXO, optional=True),
    ]


def build_alternate_shots(small, large, rest_large_s) -> List[Shot]:
    """capalt shots. Production uses one to three sensor frames, then two Vmid frames."""
    return [
        Shot("Production pattern (1 sensor, 2 Vmid), small sensor", small, 200, 384, "2 us/div", "200 mV/div",
             "At each connection the node drops from Vmid to a shared level, rises for one frame, then "
             "returns to Vmid for two frames. The drops shrink cycle by cycle as the sensor charges. The "
             "first drop's depth is set by C_share/C_total. After the drop, does the node return all the "
             "way to Vmid within the two Vmid frames?",
             pattern=(1, 2)),
        Shot("Production pattern, long gap", small, 5000, 192, "10 us/div", "200 mV/div",
             "The same cycle with 6.2 us frames, so R_bias carries a larger part of each step. Sharing "
             "happens once per cycle; the R_bias part grows with the connected time. The tool combines "
             "this shot with the first to separate the two.",
             pattern=(1, 2)),
        Shot("Three sensor frames (channelrepeat 3)", small, 200, 400, "2 us/div", "200 mV/div",
             "Three frames per connection. The drop is the same kind; the second and third frames keep "
             "rising (R_bias and the per-frame charge).",
             pattern=(3, 2)),
        Shot("One Vmid frame", small, 200, 256, "2 us/div", "200 mV/div",
             "Only one Vmid frame between connections. Is one frame enough for the node to get back to "
             "Vmid? Compare the Vmid samples printed after the shot with the production pattern.",
             pattern=(1, 1)),
        Shot("Large sensor, production pattern", large, 200, 600, "5 us/div", "100 mV/div",
             "The same cycle on the large sensor: smaller drops (the same node on a larger sensor) and "
             "many more cycles to reach Vmid.",
             rest_s=rest_large_s, pattern=(1, 2)),
    ]


def setup_steps(adc: int, vmid_volts: float) -> List[str]:
    return [
        "Power the board and leave USB connected. Use 10x probes and connect each ground clip to "
        "board ground close to the signal, with the shortest lead you have.",
        "CH1 on the %s of ADC%d's OPA365 buffer (the side with the 270 pF to ground). Do not probe "
        "MXO yet: a probe there adds its capacitance to C_total." % (BUFFER, adc),
        "CH2 on ADC%d chip select (Teensy pin %d). CH3, if you have it, on the SPI clock (Teensy "
        "pin %d)." % (adc, CS_PIN[adc], SCK_PIN[adc]),
        "CH1: DC coupled, full bandwidth, 200 mV/div. CH1 should now sit steady at about %.3f V "
        "(the ADC reads that as Vmid). If it does not, you are on the wrong node." % vmid_volts,
        "CH2 and CH3: DC coupled, 2 V/div. They should be idle: CS high, clock low.",
        "Trigger: source CH1, falling edge, level %.1f V, normal mode. CS is not the trigger because "
        "it toggles during the park too; CH1 falls only at the connection." % TRIGGER_LEVEL_V,
        "Use the deepest memory or highest sample rate available: you need at least 50 MS/s to see "
        "inside a frame. Place the trigger about 10 %% from the left of the screen.",
    ]


READING_THE_TRACE = [
    "How to read the trace at the buffer output:",
    "  - A spike right after CS falls or at acquisition that recovers to the same curve is ADC kick "
    "absorbed by the 270 pF. That is not the transfer.",
    "  - A jump that stays at the new level, once per frame, is the charge transfer.",
    "  - Only a smooth rise means no sudden transfer at this node, which is also a useful result. "
    "Note it and check MXO in the optional shot.",
]


def analyse(burst: protocol.BurstData, r_bias: float, c_total_pf: float, vref: float, frames: int = 6) -> dict:
    """The samples the ADC saw and the step the 2026-10-30 model predicts per frame."""
    words = (burst.words[protocol.LEAD_FRAMES:] & 0x0FFF).astype(np.float64)
    volts = words * vref / 4096.0
    stamps = protocol.timestamps_to_seconds(burst.timestamps, burst.cpu_hz, origin_index=1)
    period = float(np.median(np.diff(stamps[protocol.LEAD_FRAMES:])))
    target = float(np.mean(volts[-max(8, len(volts) // 10):]))
    vmid = burst.vmid_words[-8:] & 0x0FFF
    v_d = float(np.mean(vmid) * vref / 4096.0) if len(vmid) else float("nan")
    rc = 1.0 - np.exp(-period / (r_bias * c_total_pf * 1e-12))
    rows = []
    for k in range(min(frames, len(volts) - 1)):
        distance = target - float(volts[k])
        transfer = distance * c_frame_pf(float(volts[k])) / c_total_pf
        rows.append({"frame": k, "volts": float(volts[k]), "step_mv": float(volts[k + 1] - volts[k]) * 1e3,
                     "rc_mv": distance * rc * 1e3, "transfer_mv": transfer * 1e3})
    fit = capfit.analyze_burst(burst, r_bias, capfit.AnalysisSettings(vref=vref, discard_k=0, fit_model=capfit.FIT_ONE))
    tau_us = fit.tau_1exp * 1e6
    return {"period_us": period * 1e6, "first_over_vd": float(volts[0] / v_d) if v_d else float("nan"),
            "target_v": target, "tau_us": tau_us, "tag_errors": burst.tag_errors, "rows": rows}


def analyse_alternate(burst: protocol.BurstData, pattern: Tuple[int, int], r_bias: float, c_total_pf: float,
                      vref: float, cycles: int = 6) -> dict:
    """Per-cycle approach of the sensor to Vmid in a capalt burst.

    U_k is the last sensor sample of cycle k (just before the mux returns to Vmid). With the
    node reset to V_D between connections, U_{k+1} = r U_k + (1 - r) V_inf and
    r = (1 - c) exp(-S T / tau), where c = C_share / C_total and S T the connected time."""
    sensor_run, vmid_run = pattern
    words = burst.words[protocol.LEAD_FRAMES:]
    volts = (words & 0x0FFF).astype(np.float64) * vref / 4096.0
    is_sensor = protocol.alternate_commands_sensor(len(words), sensor_run, vmid_run)
    stamps = protocol.timestamps_to_seconds(burst.timestamps, burst.cpu_hz)
    period = float(np.median(np.diff(stamps)))
    vmid = burst.vmid_words[-8:] & 0x0FFF
    v_d = float(np.mean(vmid) * vref / 4096.0) if len(vmid) else float("nan")

    cycle_len = sensor_run + vmid_run
    n_cycles = len(words) // cycle_len
    last = np.array([volts[k * cycle_len + sensor_run - 1] for k in range(n_cycles)])
    first = np.array([volts[k * cycle_len] for k in range(n_cycles)])
    # The first Vmid sample after each connection: how far the node still is from V_D.
    after = np.array([volts[k * cycle_len + sensor_run] for k in range(n_cycles)])
    rows = [{"cycle": k, "first_v": float(first[k]), "last_v": float(last[k]),
             "step_mv": float(last[k + 1] - last[k]) * 1e3, "vmid_after_mv": float(after[k] - v_d) * 1e3}
            for k in range(min(cycles, n_cycles - 1))]

    use = np.flatnonzero(v_d - last[:-1] > FIT_MIN_DISTANCE_V)
    r = v_inf = c_share = float("nan")
    if len(use) >= 3:
        slope, intercept = np.polyfit(last[use], last[use + 1], 1)
        r = float(slope)
        v_inf = float(intercept / (1.0 - slope)) if slope != 1.0 else float("nan")
        tau = r_bias * c_total_pf * 1e-12
        c_share = 1.0 - r * float(np.exp(sensor_run * period / tau))
    tag_ok = burst.tag_errors == 0 and bool(np.all((words >> 12)[is_sensor] == burst.channel))
    return {"period_us": period * 1e6, "v_d": v_d, "first_over_vd": float(first[0] / v_d) if v_d else float("nan"),
            "r": r, "v_inf": v_inf, "c_share": c_share, "c_share_pf": c_share * c_total_pf,
            "connected_us": sensor_run * period * 1e6, "fit_cycles": int(len(use)),
            "vmid_after_mv": float(np.median(after[use] - v_d) * 1e3) if len(use) else float("nan"),
            "tag_errors": burst.tag_errors, "tags_ok": tag_ok, "rows": rows}


def combine_alternate(results: List[dict]) -> Dict[tuple, dict]:
    """tau and C_share from kept alternate shots of one channel with different connected times:
    ln r = ln(1 - c) - S T / tau, a straight line in S T. Only shots with the same number of
    sensor frames S are combined (the gap changes T): every extra sensor frame adds the
    per-frame charge, so c depends on S (on the board, S = 3 gave 17 pF against 10 pF)."""
    by_channel: Dict[tuple, List[Tuple[float, float]]] = {}
    for result in results:
        numbers = result["numbers"]
        if result.get("pattern") and result["kept"] and np.isfinite(numbers.get("r", np.nan)) and numbers["r"] > 0:
            by_channel.setdefault((result["adc"], result["channel"], result["pattern"][0]), []).append(
                (numbers["connected_us"], float(np.log(numbers["r"]))))
    combined = {}
    for channel, points in by_channel.items():
        x = np.array([p[0] for p in points])
        if len(points) < 2 or x.max() < 1.2 * x.min():
            continue
        slope, intercept = np.polyfit(x, [p[1] for p in points], 1)
        if slope >= 0:
            continue
        combined[channel] = {"tau_us": float(-1.0 / slope), "c_share": float(1.0 - np.exp(intercept)),
                             "shots": len(points)}
    return combined


class Guide:
    def __init__(self, client, settings, shots: List[Shot], c_total_pf: dict, out: Path,
                 ask: Callable[[str], str] = input, say: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep, spi_clock_hz: int = DEFAULT_CLOCK_HZ):
        self.spi_clock_hz = spi_clock_hz
        self.client, self.settings, self.shots = client, settings, shots
        self.c_total_pf, self.out = c_total_pf, out
        self.ask_user, self.print, self.sleep = ask, say, sleep
        self.log: List[str] = []
        self.results: List[dict] = []

    def say(self, text: str = "") -> None:
        self.print(text)
        self.log.append(text)

    def ask(self, prompt: str) -> str:
        answer = self.ask_user(prompt).strip()
        self.log.append("%s%s" % (prompt, answer))
        return answer

    def save(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "scope_test.json").write_text(json.dumps(
            {"format": "captest-scope-test-1", "board": "TestBoard_7953", "shots": self.results}, indent=2),
            encoding="utf-8")
        (self.out / "scope_test.log").write_text("\n".join(self.log) + "\n", encoding="utf-8")

    def preflight(self) -> Optional[dict]:
        info = self.client.identify()
        self.say(info.label)
        if info.kind != "captest" or info.board_id != "TestBoard_7953":
            self.say("This needs the TestBoard_7953 capacitance-test firmware: nothing was fired.")
            return None
        if not self.client.set_spi_clock(self.spi_clock_hz):
            self.say("spiclock rejected: nothing was fired.")
            return None
        status = self.client.status()
        version = tuple(int(x) for x in status.version.split("."))
        self.supports_continue = version >= (1, 1, 0)
        self.supports_alternate = version >= (1, 2, 0)
        adc = self.shots[0].channel[0]
        reading = self.client.vmid(adc)
        self.client.abort()
        return {"adc": adc, "vmid_volts": reading["mean_code"] * self.settings.vref / 4096.0}

    def run_setup(self, adc: int, vmid_volts: float) -> bool:
        self.say("\nThe board is parked on Vmid. Nothing fires until you say so.\n")
        steps = setup_steps(adc, vmid_volts)
        for number, step in enumerate(steps, 1):
            self.say("Setup %d of %d: %s" % (number, len(steps), step))
            if self.ask("  Enter when done, q to quit: ").lower() == "q":
                return False
        self.say("")
        for line in READING_THE_TRACE:
            self.say(line)
        return True

    def fire(self, shot: Shot) -> dict:
        if shot.pattern:
            text = protocol.format_alternate_command(shot.channel[0], shot.channel[1], shot.samples, PARK_US,
                                                     shot.gap_ns, shot.pattern[0], shot.pattern[1],
                                                     self.settings.first_clock_delay_ns)
        else:
            hold = 1 if shot.hold_mode else None
            text = protocol.format_burst_command(shot.channel[0], shot.channel[1], shot.samples, PARK_US,
                                                 shot.gap_ns, self.settings.first_clock_delay_ns, hold)
        packet = self.client.burst_packet(text, protocol.HEADER_BYTES, protocol.packet_length_from_header)
        burst = protocol.decode_burst_packet(packet)
        r_bias, c_total = self.settings.r_bias(*shot.channel), self.c_total_pf[shot.channel]
        if shot.pattern:
            numbers = analyse_alternate(burst, shot.pattern, r_bias, c_total, self.settings.vref)
        else:
            numbers = analyse(burst, r_bias, c_total, self.settings.vref)
        return {"packet": packet, "numbers": numbers}

    def report_alternate(self, shot: Shot, numbers: dict) -> None:
        self.say("\nThe ADC saw: frame period %.2f us, V_D %.3f V, first sample/V_D %.3f, tag errors %d"
                 % (numbers["period_us"], numbers["v_d"], numbers["first_over_vd"], numbers["tag_errors"]))
        self.say("  cycle  first V  last V  step mV  first Vmid sample - V_D, mV")
        for row in numbers["rows"]:
            self.say("  %5d  %.3f    %.3f  %7.1f  %8.1f"
                     % (row["cycle"], row["first_v"], row["last_v"], row["step_mv"], row["vmid_after_mv"]))
        if not np.isfinite(numbers["r"]):
            self.say("  Too few cycles far enough from Vmid for the per-cycle fit.")
            return
        self.say("  Per cycle (%d cycles): r %.4f, fitted target %.3f V (extrapolated, not the measured "
                 "settling level; V_D %.3f V), connected %.2f us"
                 % (numbers["fit_cycles"], numbers["r"], numbers["v_inf"], numbers["v_d"], numbers["connected_us"]))
        self.say("  With tau = R_bias x %.0f pF: C_share/C_total %.3f, C_share %.1f pF"
                 % (self.c_total_pf[shot.channel], numbers["c_share"], numbers["c_share_pf"]))
        self.say("  First Vmid sample after a connection: %+.1f mV from V_D (median)" % numbers["vmid_after_mv"])

    def report(self, shot: Shot, numbers: dict) -> None:
        if shot.pattern:
            self.report_alternate(shot, numbers)
            return
        self.say("\nThe ADC saw: frame period %.2f us, first sample/V_D %.3f, rough tau %.1f us, tag errors %d"
                 % (numbers["period_us"], numbers["first_over_vd"], numbers["tau_us"], numbers["tag_errors"]))
        self.say("  frame     V     step mV   R_bias part mV   predicted jump mV")
        for row in numbers["rows"]:
            self.say("  %5d  %.3f  %7.1f   %12.1f   %17.1f"
                     % (row["frame"], row["volts"], row["step_mv"], row["rc_mv"], row["transfer_mv"]))
        # The expected values were measured at the default clock, with its frame period.
        if shot.expected_tau_us and self.spi_clock_hz == DEFAULT_CLOCK_HZ and np.isfinite(numbers["tau_us"]):
            change = numbers["tau_us"] / shot.expected_tau_us - 1.0
            if abs(change) > TAU_WARNING:
                self.say("  Note: tau differs by %+.0f %% from the unprobed run (%.1f us). The probe may be "
                         "loading the sensor node, or the sensor state differs. Check the probe point."
                         % (change * 100.0, shot.expected_tau_us))

    def report_combined(self) -> None:
        for (adc, channel, sensor_run), fit in sorted(combine_alternate(self.results).items()):
            r_bias = self.settings.r_bias(adc, channel)
            c_total_pf = fit["tau_us"] * 1e-6 / r_bias * 1e12
            self.say("ADC%d:%d from %d alternate shots with %d sensor frame(s): tau %.1f us, C_total %.1f pF "
                     "(R_bias), C_share/C_total %.3f, C_share %.1f pF"
                     % (adc, channel, fit["shots"], sensor_run, fit["tau_us"], c_total_pf, fit["c_share"],
                        fit["c_share"] * c_total_pf))

    def run_shot(self, number: int, shot: Shot) -> bool:
        """Returns False when the user quits."""
        self.say("\n=== Shot %d of %d: %s ===" % (number, len(self.shots), shot.name))
        if shot.hold_mode and not self.supports_continue:
            self.say("Skipped: continue mode needs CapTest firmware 1.1.0 or newer.")
            return True
        if shot.pattern and not self.supports_alternate:
            self.say("Skipped: switching to Vmid between samples needs CapTest firmware 1.2.0 or newer.")
            return True
        if shot.optional and self.ask("Optional shot. Run it? [y/N]: ").lower() != "y":
            return True
        self.say("Channel ADC%d:%d, gap %d ns, %d samples." % (shot.channel[0], shot.channel[1], shot.gap_ns,
                                                              shot.samples))
        if shot.pattern:
            self.say("Pattern: %d sensor frame(s), then %d Vmid frame(s), repeated." % shot.pattern)
        self.say("Probe: CH1 on the %s." % shot.probe)
        self.say("Scope: CH1 %s, timebase %s." % (shot.volts_per_div, shot.timebase))
        self.say("Look for: %s" % shot.look_for)
        attempt = 0
        while True:
            attempt += 1
            if shot.rest_s > 1.0:
                self.say("Letting the sensor rest for %.0f s, with nothing connected to it..." % shot.rest_s)
            self.sleep(shot.rest_s)
            answer = self.ask("Press SINGLE on the scope, wait for it to arm, then Enter to fire (s skip, q quit): ")
            if answer.lower() == "q":
                return False
            if answer.lower() == "s":
                return True
            try:
                fired = self.fire(shot)
            except protocol.ProtocolError as exc:
                self.say("The burst failed: %s" % exc)
                answer = self.ask("r retry, n next shot, q quit: ").lower()
                if answer == "q":
                    return False
                if answer == "n":
                    return True
                continue
            self.report(shot, fired["numbers"])
            answer = self.ask("Did the scope capture it? Save the screen, then: Enter next, r repeat, q quit: ").lower()
            note = "" if answer == "r" else self.ask("Note what you saw (Enter for none): ")
            self.results.append({"shot": shot.name, "attempt": attempt, "adc": shot.channel[0],
                                 "channel": shot.channel[1], "gap_ns": shot.gap_ns, "samples": shot.samples,
                                 "hold_mode": shot.hold_mode or 0, "pattern": list(shot.pattern or ()),
                                 "probe": shot.probe, "note": note,
                                 "numbers": fired["numbers"], "kept": answer != "r",
                                 "packet_b64": base64.b64encode(fired["packet"]).decode("ascii")})
            self.save()
            if answer == "q":
                return False
            if answer != "r":
                return True

    def run(self) -> int:
        try:
            ready = self.preflight()
            if ready is None:
                return 2
            if not self.run_setup(ready["adc"], ready["vmid_volts"]):
                self.say("Stopped before any burst was fired.")
                return 0
            for number, shot in enumerate(self.shots, 1):
                if not self.run_shot(number, shot):
                    self.say("\nStopped.")
                    break
            else:
                self.say("\nAll shots done.")
            self.report_combined()
            return 0
        except (KeyboardInterrupt, EOFError):
            self.say("\nInterrupted.")
            return 1
        finally:
            try:
                self.say("Board parked on Vmid: %s" % self.client.abort())
            except Exception as exc:   # the board state must be reported
                self.say("Parking the board failed: %s" % exc)
            self.save()
            self.say("Saved to %s" % self.out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--small", default="1:0", help="small sensor channel, adc:channel")
    parser.add_argument("--large", default="1:2", help="large sensor channel, same ADC")
    parser.add_argument("--small-pf", type=float, default=72.0, help="C_total of the small sensor")
    parser.add_argument("--large-pf", type=float, default=255.0, help="C_total of the large sensor")
    parser.add_argument("--rest-large-s", type=float, default=5.0,
                        help="rest before the large-sensor shot; 2:7 needs 3 s, 500 ms does not discharge it")
    parser.add_argument("--large-gap-ns", type=int, default=5000, help="CS gap of the large-sensor shot")
    parser.add_argument("--spi-hz", type=int, default=DEFAULT_CLOCK_HZ, help="SPI clock for every shot")
    parser.add_argument("--set", choices=("frames", "alternate"), default="frames",
                        help="frames: the per-frame charge shots; alternate: switch between the sensor and "
                             "Vmid during the burst (firmware 1.2.0)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    small, large = parse_channel_spec(args.small)[0], parse_channel_spec(args.large)[0]
    if small[0] != large[0]:
        parser.error("--small and --large must be on the same ADC: one probe sits on one buffer")
    suffix = "_scope_test_alternate" if args.set == "alternate" else "_scope_test"
    out = Path(args.out) if args.out else (Path(__file__).resolve().parents[1] / "Results" / "TestBoard_7953"
                                           / (time.strftime("%Y-%m-%d") + suffix))
    try:
        link = SerialLink(args.port)
    except (LinkError, OSError) as exc:
        print("Cannot open %s: %s\nClose any program using the port (the CapTest GUI, a serial monitor) and "
              "run this again." % (args.port, exc))
        return 2
    try:
        shots = (build_alternate_shots(small, large, args.rest_large_s) if args.set == "alternate"
                 else build_shots(small, large, args.rest_large_s, args.large_gap_ns))
        guide = Guide(CapTestClient(link), Settings.for_board("TestBoard_7953"), shots,
                      {small: args.small_pf, large: args.large_pf}, out, spi_clock_hz=args.spi_hz)
        return guide.run()
    finally:
        link.close()


if __name__ == "__main__":
    raise SystemExit(main())
