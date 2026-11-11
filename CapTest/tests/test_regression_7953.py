"""Move/generalisation regression gate for TestBoard_7953.

The numbers below were recorded from the application BEFORE it was moved out of
Arduino_Sketches/TestBoard_7953_CapTest and generalised to several boards (seeded
simulated automatic run on ADC1). Packets must stay bit-identical; results must stay
numerically identical to the recording (the fit is deterministic for a given numpy)."""

import base64
import hashlib
import json

import pytest

from captest import cli, runner
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.settings import Settings
from captest.sim import SimConfig, SimLink, SimulatedDevice

ADC1_PACKETS_SHA256 = "117a8cf4dedf5e6d9c866789e8cba64330c5d73934f26d09eb0587c6dbfa59c6"
FIRST_CHANNELS = {   # (adc, channel): (C_total pF, C_D/C_total) of the recorded run
    (1, 0): (90.5504, 0.13219), (1, 1): (89.8406, 0.13226), (1, 2): (73.256, 0.16472)}
# C_D/C_total and the values derived from it are no longer exported for the single connection
# (plan 4.3): the fit still gives the recorded ratio on the measurement itself.


def adc1_run():
    clock = VirtualClock()
    settings = Settings(method="burst")   # the recorded run used one connection per burst
    device = SimulatedDevice(SimConfig(seed=7953), clock=clock, r_bias_table=settings.bias)
    session = runner.MeasurementSession(CapTestClient(SimLink(device)), settings, clock, simulated=True)
    session.run_auto([1])
    return session


def test_simulated_7953_packets_are_bit_identical_to_the_first_version():
    session = adc1_run()
    digest = hashlib.sha256()
    for burst in session.raw:
        digest.update(burst.packet)
    assert len(session.raw) == 10 and digest.hexdigest() == ADC1_PACKETS_SHA256


def test_simulated_7953_results_match_the_recording():
    session = adc1_run()
    for ref, (c_total, ratio) in FIRST_CHANNELS.items():
        result = session.results[ref]
        assert result.aggregate.mean["c_total"] * 1e12 == pytest.approx(c_total, abs=2e-4)
        assert result.measurements[0].ratio == pytest.approx(ratio, abs=2e-5)
        row = result.row()
        assert row["ratio"] != row["ratio"] and row["c_d_pf"] != row["c_d_pf"]      # NaN: not valid
        assert row["c_total_method"] == "tau_over_r" and "FRAME_DEPENDENT" in row["flags"]


def test_7953_files_keep_the_first_version_formats(tmp_path):
    out, raw = tmp_path / "r.json", tmp_path / "raw.json"
    assert cli.main(["--simulate", "--auto", "--adc", "1", "--out", str(out), "--raw-out", str(raw)]) == 0
    results = json.loads(out.read_text())
    assert results["format"] == "captest-results-1" and "board" not in results
    assert "board_id" not in results["settings"] and "per_address" not in results["settings"]
    assert list(results["rows"][0])[:3] == ["adc", "channel", "tau_us"]
    saved = json.loads(raw.read_text())
    assert saved["format"] == "captest-raw-1" and set(saved["bursts"][0]) == {
        "adc", "channel", "fresh", "since_previous_s", "wall_time", "packet_b64"}
    base64.b64decode(saved["bursts"][0]["packet_b64"])
    # Files written by the first version load unchanged.
    settings, bursts = runner.load_raw(raw)
    assert settings.board_id == "TestBoard_7953" and len(bursts) == 10 and bursts[0].ref == (1, 0)
