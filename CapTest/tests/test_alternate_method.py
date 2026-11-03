"""Vmid between samples (capalt, CapTest firmware 1.3.0): the TestBoard_7953 default method."""

import pytest

from captest import capfit, protocol, runner
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings
from captest.sim import SimConfig, SimLink, SimulatedDevice


def make_session(version="1.3.0", **config):
    clock = VirtualClock()
    settings = Settings()
    device = SimulatedDevice(SimConfig(firmware_version=version, **config), clock=clock,
                             r_bias_table=settings.bias)
    session = runner.MeasurementSession(CapTestClient(SimLink(device)), settings, clock, simulated=True)
    return session, device


def test_default_method_is_vmid_between_samples_with_one_vmid_frame():
    settings = Settings.for_board("TestBoard_7953")
    assert settings.method == "alternate" and settings.vmid_frames == 1 and settings.gap_long_ns == 0
    assert Settings.from_dict({"gap_ns": 200}).method == "alternate"     # files saved before the setting


def test_automatic_long_gap_follows_the_guessed_tau():
    settings = Settings()
    assert settings.long_gap_ns(4, 0) == 7500           # 249 kOhm x 100 pF x 0.3
    assert settings.long_gap_ns(1, 0) == 30000          # 1 MOhm
    settings.c_guess_pf = 1000.0
    assert settings.long_gap_ns(1, 0) == 40000          # limited
    settings.gap_long_ns = 9000
    assert settings.long_gap_ns(1, 0) == 9000


@pytest.mark.parametrize("ref, c_pzt", [((1, 0), 70.0), ((2, 7), 240.0), ((4, 1), 30.0)])
def test_alternate_bursts_recover_c_total_and_the_shared_node(ref, c_pzt):
    session, device = make_session(c_pzt_overrides={"%d:%d" % ref: c_pzt})
    result = session.measure_channel(ref, bursts=3)
    truth = device.true_values(*ref)
    assert not result.failed and result.flags == []
    assert result.fit_used == capfit.FIT_ALT
    assert result.aggregate.mean["c_total"] == pytest.approx(truth["c_total"], rel=0.03)
    assert result.aggregate.mean["c_d"] == pytest.approx(truth["c_d"], abs=1e-12)
    command = [c for c in device.commands if c.startswith("capalt")][0].split()
    assert command[6:8] == ["1", "1"] and int(command[9]) == session.settings.long_gap_ns(*ref)


def test_the_start_voltage_does_not_bias_the_result():
    session, device = make_session(v_s_overrides={"2:5": 0.6})
    charged = session.measure_channel((2, 5)).aggregate.mean["c_total"]
    assert charged == pytest.approx(device.true_values(2, 5)["c_total"], rel=0.03)


def test_pattern_is_read_from_the_tags_and_reanalysis_follows_the_packet(tmp_path):
    session, _ = make_session()
    session.settings.vmid_frames = 2
    session.measure_channel((2, 0))
    burst = protocol.decode_burst_packet(session.raw[0].packet)
    assert burst.flags & protocol.FLAG_ALTERNATE and protocol.alternate_pattern(burst) == (1, 2)
    before = session.results[(2, 0)].aggregate.mean["c_total"]
    session.save_raw(tmp_path / "raw.json")
    other = Settings(method="burst")                     # the setting does not change saved data
    again = runner.reanalyze(tmp_path / "raw.json", other)
    assert again[0].fit_used == capfit.FIT_ALT
    assert again[0].aggregate.mean["c_total"] == pytest.approx(before)


def test_one_connected_time_cannot_separate_tau_and_is_reported():
    session, device = make_session()
    client = session.client
    client.set_spi_clock(20_000_000)
    packet = client.burst_packet("capalt 2 0 64 200 200 1 1 25*", protocol.HEADER_BYTES,
                                 protocol.packet_length_from_header)
    m = capfit.analyze_alternate(protocol.decode_burst_packet(packet), 470e3)
    assert capfit.FIT_FAILED in m.flags and not m.ok


def test_older_firmware_gets_a_clear_error():
    session, _ = make_session(version="1.2.0")
    logged = []
    session.on_log = logged.append
    assert session.run_manual([(2, 0)])[0].failed
    assert any("1.3.0" in line for line in logged)
    with pytest.raises(protocol.NotOkError, match="1.3.0"):
        session.driver.measure(session.client, session.driver.single((2, 0)), session.settings)


def test_sensor_side_values_stay_empty():
    session, _ = make_session()
    mean = session.measure_channel((2, 5)).aggregate.mean
    assert mean["c_d"] > 0
    assert mean["c_sensor_side"] != mean["c_sensor_side"] and mean["c_pzt"] != mean["c_pzt"]   # NaN
