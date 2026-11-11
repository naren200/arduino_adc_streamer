"""Guided oscilloscope test of the charge-up curve on TestBoard_ADC124.

Fires one burst per shot when you press Enter, so a single-shot capture on the scope catches
the connection. After each burst it prints what the ADC measured and therefore what the node
must look like on the scope (levels at given times, the open window of the mux, the
connection delay), asks for your cursor readings and prints the differences. It answers:
does the scope see the curve the calculation says, and are the timing assumptions right?

Run from CapTest/:  python tools/scope_test_adc124.py --port COM4
Answers, readings and the raw packets are saved after every shot. The board is left parked
on VMID.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from captest import capfit, protocol                       # noqa: E402
from captest.boards import load_driver                     # noqa: E402
from captest.client import CapTestClient                   # noqa: E402
from captest.link import LinkError, SerialLink             # noqa: E402
from captest.settings import Settings                      # noqa: E402

BOARD_ID = "TestBoard_ADC124"
GUARD_NS = 200            # break-before-make guard of the production switchAddress()
READ_STEP_US = 0.3               # time after the connection at which the step level is read
TRIGGER_LEVEL_V = 1.65           # half of the 3.3 V logic level
# Teensy pins, Arduino_Sketches/TestBoard_ADC124/include/ConfigurableParameters.h (index = array).
CS_PIN = {1: 9, 2: 24}
EN_PIN = {1: 0, 2: 32}
A0_PIN = {1: 1, 2: 29}
SCK_PIN = 13
EN_ACTIVE_HIGH = {1: True, 2: False}      # TMUX1108 on array 1, TMUX1308A on array 2

EDGE, DETAIL, OVERVIEW = "edge", "detail", "overview"
BUFFER = "buffer output (ADC input)"
MUX_OUT = "mux output D node"


@dataclass
class Shot:
    name: str
    channel: tuple
    kind: str
    look_for: str
    timebase: Optional[str] = None       # None: derived from the time constant of the sizing burst
    probe: str = BUFFER
    optional: bool = False


@dataclass
class Reading:
    key: str
    label: str
    unit: str                            # "V" for a level, "ns" for a time
    predicted: float
    at_us: Optional[float] = None        # levels: time after the connection


# ----------------------------------------------------------------------- number helpers
def nice_up(value: float) -> float:
    """Smallest 1-2-5 number that is not below `value`."""
    exponent = math.floor(math.log10(value))
    for mantissa in (1, 2, 5, 10):
        if mantissa * 10.0 ** exponent >= value * (1.0 - 1e-9):
            return mantissa * 10.0 ** exponent
    raise AssertionError("unreachable")


def format_per_div(seconds: float) -> str:
    for scale, unit in ((1e-3, "ms"), (1e-6, "us"), (1e-9, "ns")):
        if seconds >= scale * 0.999:
            return "%g %s/div" % (round(seconds / scale, 6), unit)
    return "%g ns/div" % (seconds * 1e9)


def volts_per_div(step_v: float) -> str:
    """A scale that shows the whole step in about 6 divisions (never below 10 mV/div)."""
    per_div = max(nice_up(abs(step_v) / 6.0), 0.01) if step_v else 0.01
    return "%g mV/div" % round(per_div * 1e3, 3) if per_div < 1.0 else "%g V/div" % per_div


def parse_level_v(text: str) -> Optional[float]:
    """'1.234', '1.234V' or '1234mV' -> volts. A bare number below 20 is volts, above it mV."""
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    try:
        if text.endswith("mv"):
            return float(text[:-2]) / 1e3
        if text.endswith("v"):
            return float(text[:-1])
        value = float(text)
    except ValueError:
        return None
    return value if abs(value) <= 20.0 else value / 1e3


def parse_time_ns(text: str) -> Optional[float]:
    """'436', '436ns' or '0.44us' -> ns (a bare number is ns)."""
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    try:
        if text.endswith("us"):
            return float(text[:-2]) * 1e3
        if text.endswith("ns"):
            return float(text[:-2])
        return float(text)
    except ValueError:
        return None


def _num(value) -> Optional[float]:
    value = float(value)
    return value if math.isfinite(value) else None


def model_level(numbers: dict, t_s: float) -> float:
    return numbers["vinf"] + (numbers["v0"] - numbers["vinf"]) * math.exp(-t_s / (numbers["tau_us"] * 1e-6))


# ------------------------------------------------------------------------------ analysis
def analyse(driver, decoded, ref, settings: Settings) -> dict:
    """What the ADC saw, and the node behaviour the calculation implies."""
    m = driver.analyze(decoded, [ref], settings, {ref: None})[ref]
    # The first samples after the connection are discarded by the fit (discard_k); the scope
    # compares against them, so read them from a second pass that keeps everything.
    early = driver.analyze(decoded, [ref], replace(settings, discard_k=0), {ref: None})[ref]
    timing = driver.timing(decoded, settings)
    # A flat burst still "fits" (with a meaningless tau) and only raises LOW_AMPLITUDE.
    fit_ok = (all(np.isfinite(x) for x in (m.tau, m.v0, m.vinf, m.v_d))
              and capfit.LOW_AMPLITUDE not in m.flags and capfit.FIT_FAILED not in m.flags)
    numbers = {
        "fit_ok": bool(fit_ok), "flags": list(m.flags),
        "parked_v": _num(m.v_d), "v0": _num(m.v0), "vinf": _num(m.vinf),
        "step_mv": _num((m.v0 - m.v_d) * 1e3) if fit_ok else None,
        "tau_us": _num(m.tau * 1e6), "c_total_pf": _num(m.c_total * 1e12),
        "period_ns": _num(timing["period"] * 1e9),
        "window_ns": None, "connect_ns": None, "post_s": None,
        "first_samples": [(float(t * 1e6), float(v)) for t, v in zip(early.t[:4], early.v[:4])],
    }
    if "connect" in timing:
        numbers["window_ns"] = _num((timing["enable"] - timing["disable"]) * 1e9)
        numbers["connect_ns"] = _num((timing["connect"] - timing["disable"]) * 1e9)
        numbers["post_s"] = _num(timing["sample"][-1] - timing["connect"])
    return numbers


def build_readings(kind: str, n: dict) -> List[Reading]:
    readings: List[Reading] = []
    if n["parked_v"] is not None:
        readings.append(Reading("parked", "level before the connection", "V", n["parked_v"]))
    if kind == EDGE:
        if n["fit_ok"]:
            readings.append(Reading("step", "level just after the step", "V",
                                    model_level(n, READ_STEP_US * 1e-6), READ_STEP_US))
        if n["window_ns"] is not None:
            readings.append(Reading("window", "EN disable edge to EN enable edge", "ns", n["window_ns"]))
            readings.append(Reading("connect", "EN disable edge to the start of the node step", "ns",
                                    n["connect_ns"]))
    elif kind == DETAIL:
        for k, (t_us, volts) in enumerate(n["first_samples"][:3]):
            readings.append(Reading("sample%d" % k, "level where the ADC sampled (post-connect frame %d)" % k,
                                    "V", volts, t_us))
        if n["period_ns"]:
            readings.append(Reading("period", "spacing of the ADC kick pulses on CH1", "ns", n["period_ns"]))
    elif n["fit_ok"]:
        for factor, label in ((0.25, "0.25 tau"), (1.0, "1 tau"), (3.0, "3 tau")):
            t_s = factor * n["tau_us"] * 1e-6
            readings.append(Reading("t_%g" % factor, "level at %s" % label, "V", model_level(n, t_s), t_s * 1e6))
    return readings


def compare(reading: Reading, scope_value: float, step_mv: Optional[float]) -> dict:
    if reading.unit == "V":
        diff = (scope_value - reading.predicted) * 1e3
        tolerance = max(5.0, 0.1 * abs(step_mv or 0.0))
    else:
        diff = scope_value - reading.predicted
        tolerance = max(20.0, 0.1 * reading.predicted)
    return {"key": reading.key, "label": reading.label, "unit": reading.unit, "at_us": reading.at_us,
            "predicted": reading.predicted, "scope": scope_value, "diff": diff, "tolerance": tolerance,
            "ok": abs(diff) <= tolerance}


def shot_text(label: str, c: dict) -> str:
    unit = "mV" if c["unit"] == "V" else "ns"
    pred = "%.4f V" % c["predicted"] if c["unit"] == "V" else "%.1f ns" % c["predicted"]
    scope = "%.4f V" % c["scope"] if c["unit"] == "V" else "%.1f ns" % c["scope"]
    return "  %-46s %-11s %-11s %+8.1f %s  %s" % (label, pred, scope, c["diff"], unit,
                                                  "ok" if c["ok"] else "CHECK (beyond %.0f %s)" % (c["tolerance"], unit))


# --------------------------------------------------------------------------- shot plan
def other_channel(driver, settings: Settings, channel: tuple) -> Optional[tuple]:
    """Same array and address on a mux with a different R_bias: a second time constant."""
    bias = settings.r_bias(*channel)
    for mux in driver.muxes:
        ref = (channel[0], mux, channel[2])
        if driver.valid(ref) and abs(settings.r_bias(*ref) - bias) > 1.0:
            return ref
    return None


def build_shots(channel: tuple, other: Optional[tuple]) -> List[Shot]:
    shots = [
        Shot("Connection edge", channel, EDGE,
             "Zoom on the connection. After the EN disable edge (trigger, t = 0) the address pin changes "
             "after about %d ns, then EN enables again, then the node steps. Read the time from the "
             "disable edge to the enable edge, and to where the step starts (10 %% of the step)."
             % GUARD_NS, timebase="100 ns/div"),
        Shot("First microseconds", channel, DETAIL,
             "The step and the frame-rate kick of the ADC input (one pulse per frame, recovering to the "
             "same curve). That pulse is not the signal. Read the flat part of the trace between kicks "
             "at the times listed after the burst, and the spacing of the kicks.", timebase="1 us/div"),
        Shot("Full charge-up", channel, OVERVIEW,
             "The node steps at the connection and relaxes toward its final level with the time "
             "constant tau. After the burst ends the board re-parks on VMID and the trace changes "
             "again: ignore everything after that."),
    ]
    if other is not None:
        shots.append(Shot("Other mux, other R_bias", other, OVERVIEW,
                          "Same address, so the same connection instant, on a mux with the other bias "
                          "resistor. Move CH1 to that mux's ADC input. Tau should scale with R_bias. If "
                          "the trace does not match the ADC numbers, the MUX to ADC-input wiring "
                          "assumed by the firmware (MUX n -> IN n) is wrong."))
    shots.append(Shot("Mux output D node (optional)", channel, OVERVIEW,
                      "Do this one only if the buffer output left you unsure. The probe adds capacitance "
                      "to the node. The ADC reads the node with the probe attached, so the predictions "
                      "already include it: compare tau and C_total with the buffer-output shot.",
                      probe=MUX_OUT, optional=True))
    return shots


def adc_input(driver, channel: tuple) -> int:
    return driver.mux_adc_input[channel[1]]


def setup_steps(driver, channel: tuple, parked_v: float, step_v: Optional[float]) -> List[str]:
    array, mux, _ = channel
    edge = "falling" if EN_ACTIVE_HIGH[array] else "rising"
    polarity = "high" if EN_ACTIVE_HIGH[array] else "low"
    direction = ""
    if step_v is not None:
        direction = ("The sizing burst stepped %s, so put the parked level about 2 divisions %s the centre "
                     "of the screen." % (("UP", "below") if step_v >= 0 else ("DOWN", "above")))
    return [
        "Power the board and leave USB connected. Use 10x probes and connect each ground clip to board "
        "ground close to the signal, with the shortest lead you have.",
        "CH1 on AIN%d: the ADC input of array %d (the OPA365 buffer output after its small resistor and "
        "capacitor). That is the node the ADC reads for MUX%d. Do not probe the D node yet: a probe there "
        "adds its capacitance to C_total." % (adc_input(driver, channel), array, mux),
        "CH2 on the mux enable pin EN (Teensy pin %d). Array %d's EN is active %s, so its disable edge is "
        "%s. EN does not toggle while the bank sits parked on VMID, so it moves first at the connection."
        % (EN_PIN[array], array, polarity, edge),
        "CH3, if you have it, on the mux address pin A0 (Teensy pin %d). CH4, if you have it, on the SPI "
        "clock (Teensy pin %d; chip select is pin %d)." % (A0_PIN[array], SCK_PIN, CS_PIN[array]),
        "CH1: DC coupled, full bandwidth. It should sit steady at about %.3f V (VMID). If it does not, you "
        "are on the wrong node. Start at 200 mV/div; each shot tells you a scale after the sizing burst. "
        "Keep the parked level on screen in every shot: a trace that starts off screen can read wrong "
        "levels after the overload recovers. %s" % (parked_v, direction),
        "CH2, CH3 and CH4: DC coupled, 2 V/div. They should be idle (EN inactive, address lines high, "
        "clock low).",
        "Trigger: source CH2 (EN), %s edge, level %.2f V, normal mode. Place the trigger about 10 %% from "
        "the left of the screen." % (edge, TRIGGER_LEVEL_V),
        "Use the deepest memory or the highest sample rate available: the 100 ns/div shot needs at least "
        "1 GS/s, and the full charge-up needs enough memory to keep that resolution over several tau.",
    ]


# ---------------------------------------------------------------------------- the guide
class Guide:
    def __init__(self, client, driver, settings: Settings, shots: List[Shot], out: Path,
                 ask: Callable[[str], str] = input, say: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep, rest_s: float = 1.0):
        self.client, self.driver, self.settings, self.shots = client, driver, settings, shots
        self.out, self.rest_s = out, rest_s
        self.ask_user, self.print, self.sleep = ask, say, sleep
        self.log: List[str] = []
        self.results: List[dict] = []
        self.sizing: Dict[tuple, Optional[dict]] = {}

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
            {"format": "captest-scope-test-adc124-1", "board": BOARD_ID,
             "spi_clock_hz": self.settings.spi_clock_hz, "shots": self.results}, indent=2), encoding="utf-8")
        (self.out / "scope_test.log").write_text("\n".join(self.log) + "\n", encoding="utf-8")

    # ---------------------------------------------------------------------- bursts
    def fire_burst(self, channel: tuple, c_guess_pf: float) -> dict:
        settings = replace(self.settings, n_samples=None, c_guess_pf=c_guess_pf)
        outcome = self.driver.measure(self.client, self.driver.single(channel), settings)
        return {"packet": outcome.packet, "numbers": analyse(self.driver, outcome.decoded, channel, settings)}

    def c_guess(self, channel: tuple) -> float:
        """Covers about 7 tau of what the sizing burst measured."""
        size = self.sizing.get(channel)
        if size and size["c_total_pf"]:
            return 1.2 * size["c_total_pf"]
        return self.settings.c_guess_pf

    # -------------------------------------------------------------------- preflight
    def preflight(self) -> Optional[dict]:
        info = self.client.identify()
        self.say(info.label)
        if info.kind != "captest" or info.board_id != BOARD_ID:
            self.say("This needs the TestBoard_ADC124 capacitance-test firmware: nothing was fired.")
            return None
        if not self.driver.setup_device(self.client, self.settings):
            self.say("spiclock rejected: nothing was fired.")
            return None
        first = self.shots[0].channel
        reading = self.driver.vmid(self.client, first[0])
        self.client.abort()
        self.say(self.driver.vmid_text(reading, self.settings.vref))
        parked_v = reading["in%d" % adc_input(self.driver, first)] or reading["mean_code"]
        parked_v = parked_v * self.settings.vref / 4096.0
        self.say("\nSizing: one burst per channel to learn the time constant and step (the scope is not "
                 "needed for these; the step of later shots depends on how long each sensor rested).")
        for shot in self.shots:
            if shot.channel in self.sizing:
                continue
            self.sleep(self.rest_s)
            try:
                size = self.fire_burst(shot.channel, self.settings.c_guess_pf)["numbers"]
            except protocol.ProtocolError as exc:
                self.say("  %s: sizing burst failed (%s)" % (self.driver.display(shot.channel), exc))
                self.sizing[shot.channel] = None
                continue
            self.sizing[shot.channel] = size
            self.say("  %s: %s" % (self.driver.display(shot.channel), self.size_text(size)))
        return {"channel": first, "parked_v": parked_v}

    @staticmethod
    def size_text(n: dict) -> str:
        if not n["fit_ok"]:
            return "no usable exponential (flags %s): the sensor sat at VMID, so there is no step to see" % (
                ", ".join(n["flags"]) or "none")
        return "step %+.0f mV, tau %.1f us, C_total %.1f pF" % (n["step_mv"], n["tau_us"], n["c_total_pf"])

    def run_setup(self, ready: dict) -> bool:
        size = self.sizing.get(ready["channel"])
        step_v = size["step_mv"] / 1e3 if size and size["fit_ok"] else None
        self.say("\nThe board is parked on VMID. Nothing fires until you say so.\n")
        steps = setup_steps(self.driver, ready["channel"], ready["parked_v"], step_v)
        for number, step in enumerate(steps, 1):
            self.say("Setup %d of %d: %s" % (number, len(steps), step))
            if self.ask("  Enter when done, q to quit: ").lower() == "q":
                return False
        return True

    # ----------------------------------------------------------------------- shots
    def scope_settings(self, shot: Shot) -> str:
        size = self.sizing.get(shot.channel)
        if size and size["fit_ok"]:
            tau_s = size["tau_us"] * 1e-6
            timebase = shot.timebase or format_per_div(nice_up(0.8 * tau_s))
            vdiv = volts_per_div(size["step_mv"] / 1e3)
            return "CH1 %s, timebase %s (sized from the sizing burst: tau %.1f us, step %+.0f mV)" % (
                vdiv, timebase, size["tau_us"], size["step_mv"])
        timebase = shot.timebase or "set by eye from the first shot"
        return "CH1 200 mV/div, timebase %s (the sizing burst gave no step to size from)" % timebase

    def report(self, shot: Shot, n: dict) -> None:
        self.say("\nThe ADC saw: parked level %s, flags %s" % (
            "%.4f V" % n["parked_v"] if n["parked_v"] is not None else "n/a", ", ".join(n["flags"]) or "none"))
        if n["fit_ok"]:
            self.say("  step %+.1f mV to %.4f V, final level %.4f V, tau %.1f us, C_total %.1f pF, "
                     "frame period %.0f ns" % (n["step_mv"], n["v0"], n["vinf"], n["tau_us"], n["c_total_pf"],
                                               n["period_ns"]))
            if abs(n["step_mv"]) < 20.0:
                self.say("  The step is under 20 mV: the cursors cannot tell the curves apart. Use a "
                         "channel that steps further (an open input on array 2 does) or let the sensor "
                         "rest differently before the next shot.")
        else:
            self.say("  No usable exponential: nothing to compare except the timing.")
        if shot.kind == EDGE and n["window_ns"] is not None:
            self.say("  Timing from the EN disable edge (trigger, t = 0): the address pins change at about "
                     "+%d ns, EN enables at +%.1f ns, the node starts to step at about +%.1f ns (assumes a "
                     "%.0f ns mux turn-on delay)." % (GUARD_NS, n["window_ns"], n["connect_ns"],
                                                       self.settings.enable_delay_ns))
        if n["post_s"] is not None and shot.kind != EDGE:
            self.say("  The burst lasts %.0f us after the connection." % (n["post_s"] * 1e6))

    def read_scope(self, readings: List[Reading], step_mv: Optional[float]) -> List[dict]:
        if not readings:
            return []
        self.say("\nPlace the cursors on the stored trace (t = 0 is the start of the step; for the "
                 "levels, 'after the connection' is measured from that step) and read:")
        self.say("  Levels: volts or millivolts (bare numbers below 20 are volts). Times: ns or us. "
                 "Enter skips a reading.")
        compared = []
        for reading in readings:
            where = "" if reading.at_us is None else " at +%.2f us" % reading.at_us
            predicted = ("%.4f V" % reading.predicted) if reading.unit == "V" else "%.1f ns" % reading.predicted
            prompt = "  %s%s (predicted %s): " % (reading.label, where, predicted)
            text = self.ask(prompt)
            value = parse_level_v(text) if reading.unit == "V" else parse_time_ns(text)
            if value is None:
                if text:
                    self.say("  Could not read %r: skipped." % text)
                continue
            compared.append(compare(reading, value, step_mv))
        if compared:
            self.say("\n  %-46s %-11s %-11s %s" % ("reading", "predicted", "scope", "difference"))
            for c in compared:
                self.say(shot_text(c["label"] + ("" if c["at_us"] is None else " @%.2f us" % c["at_us"]), c))
        return compared

    def implied_delay(self, compared: List[dict]) -> Optional[float]:
        by_key = {c["key"]: c for c in compared}
        if "window" in by_key and "connect" in by_key:
            return by_key["connect"]["scope"] - by_key["window"]["scope"]
        return None

    def run_shot(self, number: int, shot: Shot) -> bool:
        """Returns False when the user quits."""
        self.say("\n=== Shot %d of %d: %s ===" % (number, len(self.shots), shot.name))
        if shot.optional and self.ask("Optional shot. Run it? [y/N]: ").lower() != "y":
            return True
        self.say("Channel %s, probe: CH1 on the %s." % (self.driver.display(shot.channel), shot.probe))
        if shot.probe == BUFFER:
            self.say("CH1 on AIN%d." % adc_input(self.driver, shot.channel))
        self.say("Scope: %s." % self.scope_settings(shot))
        self.say("Look for: %s" % shot.look_for)
        attempt = 0
        while True:
            attempt += 1
            self.sleep(self.rest_s)
            answer = self.ask("Press SINGLE on the scope, wait for it to arm, then Enter to fire (s skip, q quit): ")
            if answer.lower() == "q":
                return False
            if answer.lower() == "s":
                return True
            try:
                fired = self.fire_burst(shot.channel, self.c_guess(shot.channel))
            except protocol.ProtocolError as exc:
                self.say("The burst failed: %s" % exc)
                answer = self.ask("r retry, n next shot, q quit: ").lower()
                if answer == "q":
                    return False
                if answer == "n":
                    return True
                continue
            numbers = fired["numbers"]
            self.report(shot, numbers)
            compared = self.read_scope(build_readings(shot.kind, numbers), numbers["step_mv"])
            delay = self.implied_delay(compared)
            if delay is not None:
                self.say("\n  Implied mux turn-on delay (step start minus open window): %.0f ns; the setting "
                         "enable_delay_ns is %.0f ns." % (delay, self.settings.enable_delay_ns))
            answer = self.ask("Save the screen, then: Enter next, r repeat, q quit: ").lower()
            note = "" if answer == "r" else self.ask("Note what you saw (Enter for none): ")
            self.results.append({
                "shot": shot.name, "attempt": attempt, "channel": list(shot.channel), "probe": shot.probe,
                "note": note, "kept": answer != "r", "numbers": numbers, "readings": compared,
                "implied_enable_delay_ns": delay,
                "packet_b64": base64.b64encode(fired["packet"]).decode("ascii")})
            self.save()
            if answer == "q":
                return False
            if answer != "r":
                return True

    def summary(self) -> None:
        kept = [r for r in self.results if r["kept"]]
        bad = [(r["shot"], c) for r in kept for c in r["readings"] if not c["ok"]]
        total = sum(len(r["readings"]) for r in kept)
        if total:
            self.say("\n%d of %d readings agree with the ADC-based calculation." % (total - len(bad), total))
            for name, c in bad:
                self.say("  CHECK %s: %s, scope %+.1f %s from the prediction (limit %.0f)"
                         % (name, c["label"], c["diff"], "mV" if c["unit"] == "V" else "ns", c["tolerance"]))
        by_channel = {}
        for r in kept:
            if r["numbers"]["c_total_pf"]:
                by_channel.setdefault((tuple(r["channel"]), r["probe"]), r["numbers"]["c_total_pf"])
        for (channel, probe), c_total in list(by_channel.items()):
            if probe == MUX_OUT and (channel, BUFFER) in by_channel:
                self.say("Probe on the D node: C_total %.1f pF against %.1f pF at the buffer output, so the "
                         "probe adds about %.1f pF." % (c_total, by_channel[(channel, BUFFER)],
                                                        c_total - by_channel[(channel, BUFFER)]))

    def run(self) -> int:
        try:
            ready = self.preflight()
            if ready is None:
                return 2
            if not self.run_setup(ready):
                self.say("Stopped before any scope shot was fired.")
                return 0
            for number, shot in enumerate(self.shots, 1):
                if not self.run_shot(number, shot):
                    self.say("\nStopped.")
                    break
            else:
                self.say("\nAll shots done.")
            self.summary()
            return 0
        except (KeyboardInterrupt, EOFError):
            self.say("\nInterrupted.")
            return 1
        finally:
            try:
                self.say("Board parked on VMID: %s" % self.client.abort())
            except Exception as exc:   # the board state must be reported
                self.say("Parking the board failed: %s" % exc)
            self.save()
            self.say("Saved to %s" % self.out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", default="COM4")
    parser.add_argument("--channel", default="2:1:0",
                        help="array:mux:input to probe. 2:1:0 is an open input of array 2 (about 1 V step, "
                             "tau 18 us); array 1 has the sensors (about 50 mV step)")
    parser.add_argument("--other", default="auto",
                        help="second channel for the R_bias shot, array:mux:input on the same address; "
                             "'auto' picks a mux with another bias resistor, 'none' skips the shot")
    parser.add_argument("--spi-hz", type=int, default=16_000_000, help="SPI clock for every burst (8..16 MHz)")
    parser.add_argument("--rest-s", type=float, default=1.0, help="rest before every burst")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    driver = load_driver(BOARD_ID)
    settings = Settings.for_board(BOARD_ID)
    settings.fit_model = capfit.FIT_ONE
    settings.spi_clock_hz = args.spi_hz
    try:
        channel = driver.parse_spec(args.channel)[0]
        other = (None if args.other == "none" else
                 other_channel(driver, settings, channel) if args.other == "auto" else
                 driver.parse_spec(args.other)[0])
    except ValueError as exc:
        parser.error(str(exc))
    if other is not None and (other[0], other[2]) != (channel[0], channel[2]):
        parser.error("--other must use the same array and input (address) as --channel")
    out = Path(args.out) if args.out else (Path(__file__).resolve().parents[1] / "Results" / BOARD_ID
                                           / time.strftime("%Y-%m-%d_scope_test"))
    try:
        link = SerialLink(args.port)
    except (LinkError, OSError) as exc:
        print("Cannot open %s: %s\nClose any program using the port (the CapTest GUI, a serial monitor) and "
              "run this again." % (args.port, exc))
        return 2
    try:
        guide = Guide(CapTestClient(link), driver, settings, build_shots(channel, other), out,
                      rest_s=args.rest_s)
        return guide.run()
    finally:
        link.close()


if __name__ == "__main__":
    raise SystemExit(main())
