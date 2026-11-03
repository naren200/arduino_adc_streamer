"""The guided ADC124 scope test, on the simulated TestBoard_ADC124 with scripted answers."""

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from captest.boards import adc124_protocol as proto
from captest.boards import load_driver
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings
from captest.simbase import SimLink

_path = Path(__file__).resolve().parents[1] / "tools" / "scope_test_adc124.py"
_spec = importlib.util.spec_from_file_location("scope_test_adc124", _path)
scope = importlib.util.module_from_spec(_spec)
sys.modules["scope_test_adc124"] = scope
_spec.loader.exec_module(scope)

CHANNEL, OTHER = (2, 1, 0), (2, 2, 0)


class Recording(SimLink):
    def __init__(self, device):
        super().__init__(device)
        self.sent = []

    def write(self, data):
        self.sent.append(data.decode("ascii"))
        super().write(data)


def make_guide(tmp_path, answer, shots=None, **config):
    settings = Settings.for_board("TestBoard_ADC124")
    driver = load_driver("TestBoard_ADC124")
    clock = VirtualClock()
    device = driver.create_simulator(clock, settings.bias, driver.sim_config(**config))
    link = Recording(device)
    prompts, rests = [], []

    def ask(prompt):
        prompts.append(prompt)
        return answer(prompt)

    def sleep(seconds):                 # the simulated sensors only recharge and leak as the clock runs
        rests.append(seconds)
        clock.sleep(seconds)

    guide = scope.Guide(CapTestClient(link), driver, settings, shots or scope.build_shots(CHANNEL, OTHER),
                        tmp_path / "out", ask=ask, say=lambda text: None, sleep=sleep)
    return guide, link, prompts, rests


def bursts(link):
    return [text for text in link.sent if text.startswith("capburst")]


def scripted(level="", timing="", skip_shots=()):
    """Answers by prompt: setup steps and shots go on, readings get `level` / `timing`."""
    state = {"shots": 0}

    def answer(prompt):
        if prompt.startswith("Optional shot"):
            return "y"
        if prompt.startswith("Press SINGLE"):
            return ""
        if prompt.startswith("  ") and "Enter when done" in prompt:
            return ""
        if prompt.startswith("Note what"):
            return "note"
        if prompt.startswith("Save the screen"):
            return ""
        if " (predicted " in prompt:
            return timing if prompt.rstrip().endswith("ns): ") else level
        raise AssertionError("unexpected prompt %r" % prompt)

    return answer


def test_nothing_fires_during_setup_and_quit_leaves_the_board_parked(tmp_path):
    guide, link, _, _ = make_guide(tmp_path, lambda prompt: "q")
    assert guide.run() == 0
    # Only the sizing bursts (one per distinct channel) were sent before the setup.
    assert len(bursts(link)) == 2
    assert link.sent[-1].startswith("capabort")


def test_sizing_bursts_come_before_the_setup_and_read_one_mux_each(tmp_path):
    guide, link, prompts, _ = make_guide(tmp_path, lambda prompt: "q")
    guide.run()
    first, second = (text.split() for text in bursts(link))
    assert first[1:4] == ["2", "0", "1"] and second[1:4] == ["2", "0", "2"]      # mask: IN1, IN2
    assert prompts == ["  Enter when done, q to quit: "]


def test_full_run_fires_each_shot_once_after_the_prompt(tmp_path):
    guide, link, _, rests = make_guide(tmp_path, scripted())
    assert guide.run() == 0
    # Two sizing bursts plus five shots (edge, detail, overview, other mux, D node).
    assert len(bursts(link)) == 2 + 5
    saved = json.loads((tmp_path / "out" / "scope_test.json").read_text(encoding="utf-8"))
    assert [s["shot"] for s in saved["shots"]] == [
        "Connection edge", "First microseconds", "Full charge-up", "Other mux, other R_bias",
        "Mux output D node (optional)"]
    assert all(s["note"] == "note" for s in saved["shots"])
    assert len(rests) == 2 + 5 and set(rests) == {1.0}
    assert link.sent[-1].startswith("capabort")
    for shot in saved["shots"]:
        proto.decode_burst_packet(base64.b64decode(shot["packet_b64"]))


def test_readings_equal_to_the_prediction_all_agree(tmp_path):
    """A perfect scope reads back exactly what the calculation predicts."""
    guide, _, _, _ = make_guide(tmp_path, lambda p: "q")
    guide.preflight()
    guide.sleep(1.0)
    n = guide.fire_burst(CHANNEL, guide.c_guess(CHANNEL))["numbers"]
    assert n["fit_ok"] and n["tau_us"] > 5.0
    for kind in (scope.EDGE, scope.DETAIL, scope.OVERVIEW):
        readings = scope.build_readings(kind, n)
        assert readings[0].key == "parked"
        for reading in readings:
            assert scope.compare(reading, reading.predicted, n["step_mv"])["ok"]


def test_timing_readings_give_the_implied_enable_delay(tmp_path):
    answers = {"window": "411", "connect": "448"}

    def answer(prompt):
        for key, label in (("window", "EN disable edge to EN enable edge"),
                           ("connect", "EN disable edge to the start of the node step")):
            if label in prompt:
                return answers[key]
        return scripted()(prompt)

    shots = [s for s in scope.build_shots(CHANNEL, None) if s.kind == scope.EDGE]
    guide, _, _, _ = make_guide(tmp_path, answer, shots)
    assert guide.run() == 0
    saved = json.loads((tmp_path / "out" / "scope_test.json").read_text(encoding="utf-8"))
    assert saved["shots"][0]["implied_enable_delay_ns"] == pytest.approx(37.0)
    assert {r["key"] for r in saved["shots"][0]["readings"]} >= {"window", "connect"}


def test_a_wrong_level_is_flagged(tmp_path):
    guide, _, _, _ = make_guide(tmp_path, lambda p: "q")
    guide.preflight()
    guide.sleep(1.0)
    n = guide.fire_burst(CHANNEL, guide.c_guess(CHANNEL))["numbers"]
    reading = scope.build_readings(scope.OVERVIEW, n)[1]
    far = scope.compare(reading, reading.predicted + 0.2, n["step_mv"])
    assert not far["ok"] and far["diff"] == pytest.approx(200.0)


def test_repeat_fires_again_and_keeps_both_attempts(tmp_path):
    answers = iter(["r", ""])

    def answer(prompt):
        if prompt.startswith("Save the screen"):
            return next(answers)
        return scripted()(prompt)

    shots = [s for s in scope.build_shots(CHANNEL, None) if s.kind == scope.OVERVIEW and not s.optional]
    guide, link, _, _ = make_guide(tmp_path, answer, shots)
    assert guide.run() == 0
    assert len(bursts(link)) == 1 + 2
    saved = json.loads((tmp_path / "out" / "scope_test.json").read_text(encoding="utf-8"))
    assert [s["kept"] for s in saved["shots"]] == [False, True]


def test_burst_sizes_follow_the_sizing_burst(tmp_path):
    guide, link, _, _ = make_guide(tmp_path, scripted())
    guide.run()
    frames = [int(text.split()[4]) for text in bursts(link)]
    assert frames[2] < frames[0] * 3              # shots use 7 tau of the measured C, not a fixed guess
    assert all(16 <= count <= proto.MAX_FRAMES for count in frames)


def test_production_or_wrong_firmware_fires_nothing(tmp_path, monkeypatch):
    guide, link, _, _ = make_guide(tmp_path, lambda p: "")

    class Info:
        label, kind, board_id = "production", "production", "TestBoard_ADC124"

    monkeypatch.setattr(guide.client, "identify", lambda: Info)
    assert guide.run() == 2
    assert bursts(link) == []


def test_interrupt_still_parks_and_saves(tmp_path):
    guide, link, _, _ = make_guide(tmp_path, lambda p: "q")
    guide.ask_user = lambda prompt: (_ for _ in ()).throw(KeyboardInterrupt())
    assert guide.run() == 1
    assert link.sent[-1].startswith("capabort")
    assert (tmp_path / "out" / "scope_test.log").is_file()


def test_a_sensor_without_a_step_is_reported_not_crashed(tmp_path):
    # A sensor that sits at VMID and does not leak: the burst shows no step at all.
    guide, _, _, _ = make_guide(tmp_path, lambda p: "q", v_s_overrides={"2:1:0": 1.65}, noise_lsb=0.0,
                                leak_tau_s=1e12)
    guide.preflight()
    assert "no usable exponential" in guide.size_text(guide.sizing[CHANNEL])
    assert scope.build_readings(scope.OVERVIEW, guide.sizing[CHANNEL])[:1][0].key == "parked"


def test_other_channel_picks_another_bias_resistor():
    driver = load_driver("TestBoard_ADC124")
    settings = Settings.for_board("TestBoard_ADC124")
    assert scope.other_channel(driver, settings, (2, 1, 0)) == (2, 2, 0)      # 1 M against 470 k
    assert scope.other_channel(driver, settings, (1, 1, 3)) == (1, 2, 3)


@pytest.mark.parametrize("text,volts", [("1.234", 1.234), ("1234mV", 1.234), ("1.234 V", 1.234),
                                        ("1234", 1.234), ("", None), ("abc", None)])
def test_level_parsing(text, volts):
    assert scope.parse_level_v(text) == (pytest.approx(volts) if volts is not None else None)


@pytest.mark.parametrize("text,ns", [("436", 436.0), ("436ns", 436.0), ("0.44us", 440.0), ("x", None)])
def test_time_parsing(text, ns):
    assert scope.parse_time_ns(text) == (pytest.approx(ns) if ns is not None else None)


def test_scales_are_one_two_five():
    assert scope.nice_up(0.8 * 17.8e-6) == pytest.approx(20e-6)
    assert scope.format_per_div(20e-6) == "20 us/div"
    assert scope.format_per_div(5e-9) == "5 ns/div"
    assert scope.volts_per_div(0.9) == "200 mV/div"
    assert scope.volts_per_div(0.005) == "10 mV/div"
    assert scope.volts_per_div(6.6) == "2 V/div"


def test_setup_names_the_trigger_edge_per_array():
    driver = load_driver("TestBoard_ADC124")
    one = " ".join(scope.setup_steps(driver, (1, 1, 0), 1.65, 0.05))
    two = " ".join(scope.setup_steps(driver, (2, 1, 0), 1.65, -0.8))
    assert "falling edge" in one and "pin 0" in one and "AIN1" in one
    assert "rising edge" in two and "pin 32" in two and "above" in two
