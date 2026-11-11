"""The guided scope test, on the simulated TestBoard_7953 with scripted answers."""

import importlib.util
import json
from pathlib import Path

import pytest

from captest import protocol
from captest.boards import load_driver
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings
from captest.simbase import SimLink

_path = Path(__file__).resolve().parents[1] / "tools" / "scope_test_7953.py"
_spec = importlib.util.spec_from_file_location("scope_test_7953", _path)
scope = importlib.util.module_from_spec(_spec)
import sys  # noqa: E402

sys.modules["scope_test_7953"] = scope
_spec.loader.exec_module(scope)

SETUP_STEPS = len(scope.setup_steps(1, 1.25))
SMALL, LARGE = (1, 0), (1, 2)


class Recording(SimLink):
    def __init__(self, device):
        super().__init__(device)
        self.sent = []

    def write(self, data):
        self.sent.append(data.decode("ascii"))
        super().write(data)


def make_guide(tmp_path, answers, version="1.1.0"):
    settings = Settings.for_board("TestBoard_7953")
    driver = load_driver("TestBoard_7953")
    device = driver.create_simulator(VirtualClock(), settings.bias, driver.sim_config(firmware_version=version))
    link = Recording(device)
    script = iter(answers)
    prompts, rests = [], []

    def ask(prompt):
        prompts.append(prompt)
        return next(script)

    guide = scope.Guide(CapTestClient(link), settings, scope.build_shots(SMALL, LARGE, 120.0),
                        {SMALL: 72.0, LARGE: 255.0}, tmp_path / "out", ask=ask, say=lambda text: None,
                        sleep=rests.append)
    return guide, link, prompts, rests


def bursts(link):
    return [text for text in link.sent if text.startswith("capburst")]


def test_nothing_fires_during_setup_and_quit_leaves_the_board_parked(tmp_path):
    guide, link, _, _ = make_guide(tmp_path, ["", "q"])
    assert guide.run() == 0
    assert bursts(link) == []
    assert link.sent[-1].startswith("capabort")


def test_full_run_fires_each_shot_once_after_the_prompt(tmp_path):
    shot_answers = ["", "", "main note"]          # fire, next, note
    answers = [""] * SETUP_STEPS + shot_answers * 5 + ["n"]
    guide, link, prompts, rests = make_guide(tmp_path, answers)
    assert guide.run() == 0
    sent = bursts(link)
    assert len(sent) == 5
    assert sent[0].split()[1:3] == ["1", "0"] and sent[0].split()[5] == "200"
    assert sent[1].split()[5] == "5000"
    assert sent[3].split()[1:3] == ["1", "2"]
    assert sent[4].rstrip("*\n").split()[-1] == "1"        # continue mode only on its own shot
    assert all(len(text.split()) == 7 for text in sent[:4])
    assert 120.0 in rests
    assert link.sent[-1].startswith("capabort")

    saved = json.loads((tmp_path / "out" / "scope_test.json").read_text(encoding="utf-8"))
    assert [s["note"] for s in saved["shots"]] == ["main note"] * 5
    assert saved["shots"][1]["numbers"]["period_us"] > 5.0
    assert protocol.decode_burst_packet(__import__("base64").b64decode(saved["shots"][4]["packet_b64"])).flags \
        & protocol.FLAG_CONTINUE_MODE


def test_repeat_fires_again_and_keeps_both_attempts(tmp_path):
    answers = [""] * SETUP_STEPS + ["", "r", "", "", "note", "q"]
    guide, link, _, _ = make_guide(tmp_path, answers)
    guide.run()
    assert len(bursts(link)) == 2
    saved = json.loads((tmp_path / "out" / "scope_test.json").read_text(encoding="utf-8"))
    assert [s["kept"] for s in saved["shots"]] == [False, True]


def test_old_firmware_skips_only_the_continue_shot(tmp_path):
    answers = [""] * SETUP_STEPS + ["", "", ""] * 4 + ["n"]
    guide, link, _, _ = make_guide(tmp_path, answers, version="1.0.0")
    assert guide.run() == 0
    assert len(bursts(link)) == 4


def test_production_or_wrong_firmware_fires_nothing(tmp_path, monkeypatch):
    guide, link, _, _ = make_guide(tmp_path, [])
    class Info:
        label, kind, board_id = "production", "production", "TestBoard_7953"
    monkeypatch.setattr(guide.client, "identify", lambda: Info)
    assert guide.run() == 2
    assert bursts(link) == []


def test_interrupt_still_parks_and_saves(tmp_path):
    guide, link, _, _ = make_guide(tmp_path, [""])     # the script runs out: StopIteration is not expected
    guide.ask_user = lambda prompt: (_ for _ in ()).throw(KeyboardInterrupt())
    assert guide.run() == 1
    assert link.sent[-1].startswith("capabort")
    assert (tmp_path / "out" / "scope_test.log").is_file()


def test_spi_clock_option_is_sent_before_the_shots(tmp_path):
    guide, link, _, _ = make_guide(tmp_path, [""] * SETUP_STEPS + ["", "", "", "q"])
    guide.spi_clock_hz = 10_000_000
    assert guide.run() == 0
    assert any("10000000" in text for text in link.sent[:bursts_index(link)])
    assert len(bursts(link)) == 1


def test_large_gap_option_changes_only_the_large_shot():
    shots = scope.build_shots(SMALL, LARGE, 120.0, 200)
    assert [s.gap_ns for s in shots if s.channel == LARGE] == [200]
    assert shots[1].gap_ns == 5000


def bursts_index(link):
    return next(i for i, text in enumerate(link.sent) if text.startswith("capburst"))


def test_prediction_matches_the_measured_curve():
    assert scope.c_frame_pf(0.1) == pytest.approx(6.3)
    assert scope.c_frame_pf(0.9) == pytest.approx(3.4)
    assert scope.c_frame_pf(1.15) == pytest.approx(2.6)


def test_sim_accepts_and_rejects_hold_mode():
    settings = Settings.for_board("TestBoard_7953")
    driver = load_driver("TestBoard_7953")
    client = CapTestClient(SimLink(driver.create_simulator(VirtualClock(), settings.bias, driver.sim_config())))
    client.set_spi_clock(20_000_000)
    packet = client.burst_packet("capburst 1 0 16 200 200 25 1*", protocol.HEADER_BYTES,
                                 protocol.packet_length_from_header)
    assert protocol.decode_burst_packet(packet).flags & protocol.FLAG_CONTINUE_MODE
    with pytest.raises(protocol.ProtocolError):
        client.burst_packet("capburst 1 0 16 200 200 25 2*", protocol.HEADER_BYTES,
                            protocol.packet_length_from_header)


def make_alternate_guide(tmp_path, answers, version="1.2.0"):
    settings = Settings.for_board("TestBoard_7953")
    driver = load_driver("TestBoard_7953")
    clock = VirtualClock()
    device = driver.create_simulator(clock, settings.bias, driver.sim_config(firmware_version=version))
    link = Recording(device)
    script = iter(answers)
    c_total = {ch: device.true_values(*ch)["c_total"] * 1e12 for ch in (SMALL, LARGE)}
    guide = scope.Guide(CapTestClient(link), settings, scope.build_alternate_shots(SMALL, LARGE, 3.0),
                        c_total, tmp_path / "out", ask=lambda prompt: next(script), say=lambda text: None,
                        sleep=clock.sleep)
    return guide, link, device


def test_alternate_set_recovers_the_shared_node_capacitance(tmp_path):
    shots = len(scope.build_alternate_shots(SMALL, LARGE, 3.0))
    answers = [""] * SETUP_STEPS + ["", "", ""] * shots + ["n"]
    guide, link, device = make_alternate_guide(tmp_path, answers)
    assert guide.run() == 0
    sent = [text for text in link.sent if text.startswith("capalt")]
    assert len(sent) == shots and not bursts(link)
    c_d_pf = device.true_values(*SMALL)["c_d"] * 1e12
    for result in guide.results:
        numbers = result["numbers"]
        assert numbers["tags_ok"] and numbers["tag_errors"] == 0
        assert numbers["c_share_pf"] == pytest.approx(c_d_pf, abs=1.0)
        assert abs(numbers["vmid_after_mv"]) < 5.0
    combined = scope.combine_alternate(guide.results)[SMALL + (1,)]
    truth = device.true_values(*SMALL)
    assert combined["tau_us"] == pytest.approx(truth["tau"] * 1e6, rel=0.05)
    assert combined["c_share"] == pytest.approx(truth["ratio"], abs=0.02)
    packet = __import__("base64").b64decode(guide.results[0]["packet_b64"])
    assert protocol.decode_burst_packet(packet).flags & protocol.FLAG_ALTERNATE


def test_old_firmware_skips_the_alternate_shots(tmp_path):
    guide, link, _ = make_alternate_guide(tmp_path, [""] * SETUP_STEPS + ["n"], version="1.1.0")
    assert guide.run() == 0
    assert not [text for text in link.sent if text.startswith("capalt")]


def test_alternate_command_and_its_limits():
    assert protocol.format_alternate_command(2, 5, 384, 200, 200, 1, 2, 25) == "capalt 2 5 384 200 200 1 2 25*"
    assert list(protocol.alternate_commands_sensor(7, 1, 2)) == [True, False, False, True, False, False, True]
    settings = Settings.for_board("TestBoard_7953")
    driver = load_driver("TestBoard_7953")
    client = CapTestClient(SimLink(driver.create_simulator(VirtualClock(), settings.bias,
                                                           driver.sim_config(firmware_version="1.2.0"))))
    packet = client.burst_packet("capalt 1 0 16 200 200 3 2*", protocol.HEADER_BYTES,
                                 protocol.packet_length_from_header)
    burst = protocol.decode_burst_packet(packet)
    tags = list(burst.tags[protocol.LEAD_FRAMES:])
    assert tags == [0, 0, 0, 15, 15] * 3 + [0]
    for bad in ("capalt 1 0 16 200 200 0 2*", "capalt 1 0 16 200 200 1 9*", "capalt 1 15 16 200 200 1 2*"):
        with pytest.raises(protocol.ProtocolError):
            client.burst_packet(bad, protocol.HEADER_BYTES, protocol.packet_length_from_header)
