"""Vinf mode (TestBoard_7953): measured settling voltage of a held sensor against the calculated one."""

import numpy as np
import pytest

from captest import capfit, cli, protocol, runner
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings
from captest.sim import SimConfig, SimLink, SimulatedDevice


def make_session(**config):
    clock = VirtualClock()
    settings = Settings()
    device = SimulatedDevice(SimConfig(**config), clock=clock, r_bias_table=settings.bias)
    session = runner.MeasurementSession(CapTestClient(SimLink(device)), settings, clock, simulated=True)
    return session, device


def test_each_channel_gets_three_levels():
    session, device = make_session()
    seen = []
    session.on_vinf = seen.append
    readings = session.run_vinf([(1, 0), (2, 5)], bursts=2)
    assert [r.ref for r in readings] == [(1, 0), (2, 5)] and [r.ref for r in seen] == [(1, 0), (2, 5)]
    for r in readings:
        assert r.alt_bursts == 2 and r.bursts == 2
        for level in (r.v_alt, r.v_held, r.v_calc, r.v_d):
            assert np.isfinite(level)
        assert r.flags == []
        # The simulator has no per-frame charge, so every level sits at Vmid (within a few mV).
        assert abs(r.v_alt - r.v_d) < 0.005 and abs(r.v_held - r.v_d) < 0.005 and abs(r.v_calc - r.v_d) < 0.005
        assert np.isfinite(r.v_alt_sd) and r.alt_spread_lsb < 3
    # per repeat: the fit burst (two connected times), the alternating burst (one), the held burst.
    kinds = [c.split() for c in device.commands if c.startswith(("capalt", "capburst"))]
    assert [k[0] for k in kinds] == ["capalt", "capalt", "capburst"] * 4
    assert kinds[0][-1] != "0" and kinds[1][-1] == "0"           # the alternating burst has no long gap
    assert device.commands[-1] == "capabort"
    # Burst lengths follow the capacitance: bounded by the firmware, at least 64 cycles.
    frames = [int(k[3]) for k in kinds if k[0] == "capalt" and k[-1] == "0"]
    assert min(frames) >= 128 and max(frames) <= protocol.MAX_SAMPLES
    held = {int(k[3]) for k in kinds if k[0] == "capburst"}
    assert max(held) <= protocol.MAX_SAMPLES and min(held) >= 256
    assert session.vinf_readings.keys() == {(1, 0), (2, 5)}


def test_the_alternating_level_is_the_median_of_the_settled_tail():
    session, device = make_session()
    client = CapTestClient(SimLink(device))
    text = protocol.format_alternate_command(2, 5, 400, 200, 200, 1, 1, 25, 0)
    packet = client.burst_packet(text, protocol.HEADER_BYTES, protocol.packet_length_from_header)
    burst = protocol.decode_burst_packet(packet)
    base = capfit.analyze_vinf_alternating(burst, 470e3, Settings().analysis())
    assert base.alt_bursts == 1 and base.alt_cycles == 200 and capfit.VINF_ALT_NOT_SETTLED not in base.flags
    # Move every sensor sample of the tail by -10 LSB: the median follows, the Vmid readings do not.
    words = burst.words.copy()
    lead = protocol.LEAD_FRAMES
    for i in range(200, 400, 2):
        words[lead + i] = (words[lead + i] & 0xF000) | ((words[lead + i] & 0x0FFF) - 10)
    burst.words = words
    moved = capfit.analyze_vinf_alternating(burst, 470e3, Settings().analysis())
    assert moved.v_alt == pytest.approx(base.v_alt - 10 * 2.5 / 4096, abs=0.0008)
    assert moved.offset_alt_mv == pytest.approx(base.offset_alt_mv - 6.1, abs=1.0)


def test_an_alternating_burst_that_ends_early_is_flagged_and_a_failed_reset_too():
    session, device = make_session(c_pzt_overrides={"1:0": 260.0})
    client = CapTestClient(SimLink(device))

    def alternating(frames):
        text = protocol.format_alternate_command(1, 0, frames, 200, 200, 1, 1, 25, 0)
        packet = client.burst_packet(text, protocol.HEADER_BYTES, protocol.packet_length_from_header)
        return protocol.decode_burst_packet(packet)

    short = capfit.analyze_vinf_alternating(alternating(120), 1e6, Settings().analysis())
    assert capfit.VINF_ALT_NOT_SETTLED in short.flags
    long = alternating(1600)
    assert capfit.VINF_ALT_NOT_SETTLED not in capfit.analyze_vinf_alternating(long, 1e6, Settings().analysis()).flags
    words = long.words.copy()
    lead = protocol.LEAD_FRAMES
    for i in range(1201, 1600, 2):                           # the Vmid readings in the tail
        words[lead + i] = (words[lead + i] & 0xF000) | ((words[lead + i] & 0x0FFF) - 160)
    long.words = words
    assert capfit.VMID_RESET_FAILED in capfit.analyze_vinf_alternating(long, 1e6, Settings().analysis()).flags


def test_a_sensor_still_charging_is_flagged_not_settled():
    session, device = make_session(c_pzt_overrides={"1:0": 260.0})
    short = CapTestClient(SimLink(device)).burst(1, 0, 300, 200, 200, 25)        # about 3 tau of 260 pF at 1 MOhm
    reading = capfit.analyze_vinf(short, 1e6, Settings().analysis())
    assert capfit.VINF_NOT_SETTLED in reading.flags and reading.bursts == 1
    long = CapTestClient(SimLink(device)).burst(1, 0, 2048, 200, 200, 25)
    assert capfit.VINF_NOT_SETTLED not in capfit.analyze_vinf(long, 1e6, Settings().analysis()).flags


def test_an_offset_of_the_settling_level_is_measured():
    # The simulator settles at V_D; shift the tail of a recorded burst and the median follows it.
    session, device = make_session()
    burst = CapTestClient(SimLink(device)).burst(2, 5, 1200, 200, 200, 25)
    words = burst.words.copy()
    keep = words[protocol.LEAD_FRAMES:] & 0x0FFF
    words[protocol.LEAD_FRAMES:] = (words[protocol.LEAD_FRAMES:] & 0xF000) | (keep - 10).clip(0)
    burst.words = words
    base = capfit.analyze_vinf(burst, 470e3, Settings().analysis())
    assert base.v_held == pytest.approx(base.v_d - 10 * 2.5 / 4096, abs=0.001)
    assert base.offset_held_mv == pytest.approx(-6.1, abs=1.0)


def test_boards_without_the_mode_refuse_it():
    from helpers124 import make_session as make124

    session, _, _ = make124()
    assert not session.driver.supports_vinf
    with pytest.raises(ValueError, match="no Vinf mode"):
        session.run_vinf([(1, 1, 0)])


def test_stop_parks_the_board_and_keeps_finished_channels():
    session, device = make_session()
    calls = []
    session.on_vinf = lambda r: (calls.append(r), setattr(session, "should_stop", lambda: True))
    readings = session.run_vinf([(1, 0), (1, 1), (1, 2)])
    assert len(readings) == 1 and device.commands[-1] == "capabort"


def test_csv_and_cli(tmp_path, capsys):
    session, _ = make_session()
    readings = session.run_vinf([(2, 5)])
    runner.write_vinf_csv(readings, session.driver, tmp_path / "v.csv")
    header, row = (tmp_path / "v.csv").read_text().splitlines()
    for name in ("adc", "channel", "v_alt_v", "v_held_v", "v_calc_v", "alt_minus_vmid_mv",
                 "alt_minus_calc_mv", "held_minus_calc_mv", "flags"):
        assert name in header.split(",")
    assert row.startswith("2,5,")
    code = cli.main(["--simulate", "--vinf", "--channels", "2:5,2:6", "--csv", str(tmp_path / "c.csv")])
    out = capsys.readouterr().out
    assert code == 0 and "2:5" in out and "alt-VD" in out and (tmp_path / "c.csv").exists()
    assert cli.main(["--simulate", "--vinf"]) == 2
