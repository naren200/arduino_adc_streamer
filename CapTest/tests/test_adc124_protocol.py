"""TestBoard_ADC124 protocol: round trip, the documented example frame, CRC failure,
truncation, #NOT_OK, and identification of production / test / diagnostic firmware."""

from pathlib import Path

import numpy as np
import pytest

from captest import protocol
from captest.boards import adc124_protocol as proto
from captest.boards import get_registry
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.simbase import SimLink
from helpers124 import BOARD, driver

EXAMPLE_HEX = (Path(__file__).resolve().parents[2] / "docs" / "testing" / "results"
               / "testboard-adc124" / "capacitance-test" / "example_packet_1_0_n16.hex")


def sample_burst(n=40, pre=8, mask=0b1111):
    rng = np.random.default_rng(0)
    return proto.BurstData124(
        array=2, address=3, input_mask=mask, pre_frames=pre,
        words=rng.integers(0, 4096, n).astype(np.uint16),
        stamps=rng.integers(0, 1 << 32, n).astype(np.uint32), flags=0x45, hold_clocks=3,
        guard_ns=200, cpu_hz=600_000_000, spi_clock_hz=16_000_000, boot_id=0xDEADBEEF, park_us=200,
        start_cycles=111, lead_cycles=222, switch_begin_cycles=333, switch_end_cycles=444,
        period_min_cycles=600, period_sum_cycles=24000, period_max_cycles=700,
        transfer_errors=1, data_errors=2)


def test_round_trip():
    burst = sample_burst()
    packet = proto.encode_burst_packet(burst)
    assert len(packet) == proto.HEADER_BYTES + 6 * 40 + 2
    decoded = proto.decode_burst_packet(packet)
    for name in ("array", "address", "input_mask", "pre_frames", "flags", "hold_clocks", "guard_ns",
                 "cpu_hz", "spi_clock_hz", "boot_id", "park_us", "start_cycles", "lead_cycles",
                 "switch_begin_cycles", "switch_end_cycles", "period_min_cycles",
                 "period_sum_cycles", "period_max_cycles", "transfer_errors", "data_errors"):
        assert getattr(decoded, name) == getattr(burst, name), name
    assert np.array_equal(decoded.words, burst.words) and np.array_equal(decoded.stamps, burst.stamps)
    assert decoded.n_inputs == 4 and decoded.inputs() == [0, 1, 2, 3]
    assert proto.decode_burst_packet(proto.encode_burst_packet(sample_burst(mask=0b0101))).inputs() == [0, 2]


def test_documented_example_frame_is_stable():
    """The frame printed in TESTBOARD_ADC124_CAPTEST.md was measured on the board."""
    packet = bytes.fromhex(EXAMPLE_HEX.read_text().strip())
    assert len(packet) == 174
    assert packet[:4] == b"CB\x01\x40"                      # magic, protocol 1, header 64
    assert packet[4:8] == bytes([1, 0, 1, 1])               # array 1, address 0, mask 1, one input
    assert packet[8:12] == bytes([18, 0, 2, 0])             # 18 frames, 2 before the connection
    assert packet[16:20] == (600_000_000).to_bytes(4, "little")
    assert packet[20:24] == (16_000_000).to_bytes(4, "little")
    burst = proto.decode_burst_packet(packet)               # CRC-16/CCITT-FALSE verified
    assert burst.boot_id == 0x9B217090 and burst.guard_ns == 200 and burst.flags == 0
    assert burst.switch_end_cycles - burst.switch_begin_cycles == 367          # 611.7 ns
    assert (burst.period_min_cycles, burst.period_sum_cycles, burst.period_max_cycles) == (608, 10588, 672)
    assert burst.n_frames == 18 and burst.pre_frames == 2
    assert burst.codes[:4].tolist() == [2048, 2049, 2060, 2069]         # VMID, VMID, then the step
    assert burst.period_ns()["mean"] == pytest.approx(1038.3, abs=0.5)
    # Re-encoding the decoded frame reproduces the bytes exactly.
    assert proto.encode_burst_packet(burst) == packet


def test_crc_failure_truncation_and_bad_headers():
    packet = bytearray(proto.encode_burst_packet(sample_burst()))
    broken = bytearray(packet)
    broken[100] ^= 0x10
    with pytest.raises(proto.CrcError):
        proto.decode_burst_packet(bytes(broken))
    for cut in (10, proto.HEADER_BYTES, len(packet) - 1):
        with pytest.raises(proto.TruncatedPacketError):
            proto.decode_burst_packet(bytes(packet[:cut]))
    bad = bytearray(packet)
    bad[0] = ord("X")
    with pytest.raises(proto.FormatError):
        proto.decode_burst_packet(bytes(bad))
    huge = bytearray(packet)
    huge[8:10] = (60000).to_bytes(2, "little")
    with pytest.raises(proto.FormatError):
        proto.packet_length_from_header(bytes(huge[:proto.HEADER_BYTES]))
    # Consistent CRC but an impossible header (mask 0b0011 with 4 inputs).
    inconsistent = bytearray(packet[:-2])
    inconsistent[6] = 0b0011
    inconsistent += proto.struct.pack("<H", protocol.crc16_ccitt_false(bytes(inconsistent)))
    with pytest.raises(proto.FormatError):
        proto.decode_burst_packet(bytes(inconsistent))
    wrong_version = bytearray(packet[:-2])
    wrong_version[2] = 9
    wrong_version += proto.struct.pack("<H", protocol.crc16_ccitt_false(bytes(wrong_version)))
    with pytest.raises(proto.FormatError):
        proto.decode_burst_packet(bytes(wrong_version))


def test_command_and_reply_formats():
    assert proto.format_burst_command(1, 3, 15, 584, 200, 8) == "capburst 1 3 15 584 200 8*"
    assert proto.format_burst_command(2, 0, 1, "auto", 0, 4, 8_000_000) == "capburst 2 0 1 auto 0 4 8000000*"
    reading = proto.parse_vmid_line("# capvmid array=1 mask=3 n=16 mean_code=2050.781 min=2048 max=2051 "
                                    "in1=2050.688 in2=2050.813 in3=- in4=- data_errors=0")
    assert reading["mean_code"] == pytest.approx(2050.781) and reading["in3"] is None
    assert reading["in2"] == pytest.approx(2050.813) and reading["mask"] == 3
    with pytest.raises(proto.FormatError):
        proto.parse_vmid_line("# something else")
    status = protocol.parse_status_line(
        "# capstatus firmware=TestBoard_ADC124_CapTest board=TestBoard_ADC124 version=1.0.0 protocol=1 "
        "git=c6c4b27 build=Oct_23_2026_17:45:40 boot_id=0x9B217090 uptime_ms=9272 cpu_hz=600000000 "
        "spi_clock_hz=16000000 max_samples=4096 state=idle bursts=0 transfer_errors=0 hold_clocks=3 "
        "guard_ns=200 min_spi_hz=8000000 max_spi_hz=16000000")
    assert status.firmware == proto.FIRMWARE_NAME and status.extra["board"] == proto.BOARD_NAME
    assert status.boot_id == 0x9B217090 and status.max_samples == 4096


def make_client(board_kind="captest", **config):
    drv = driver()
    device = drv.create_simulator(VirtualClock(), None, drv.sim_config(kind=board_kind, **config))
    return CapTestClient(SimLink(device)), device


@pytest.mark.parametrize("args", [
    (1, 7, 1, 100, 200, 8),            # address 7 is VMID, not a sensor
    (1, 6, 15, 100, 200, 8),           # address 6 exists only on MUX4
    (1, 6, 7, 100, 200, 8),            # MUX1..3 have no input 6
    (3, 0, 1, 100, 200, 8),            # array 3
    (1, 0, 0, 100, 200, 8),            # empty input mask
    (1, 0, 16, 100, 200, 8),           # mask above 15
    (1, 0, 1, 8, 200, 8),              # fewer than 16 samples
    (1, 0, 1, 4090, 200, 8),           # more than 4096 frames in total
    (1, 0, 1, 100, 20000, 8),          # park time above 10 ms
    (1, 0, 1, 100, 200, 0),            # no pre-connect frames
    (1, 0, 1, 100, 200, 65),           # more than 64 pre-connect frames
])
def test_rejected_commands_raise_not_ok(args):
    client, device = make_client()
    with pytest.raises(proto.NotOkError):
        client.burst_packet(proto.format_burst_command(*args), proto.HEADER_BYTES,
                            proto.packet_length_from_header)
    assert device.bursts_served == 0


def test_spi_clock_outside_the_adc_range_is_rejected():
    client, device = make_client()
    with pytest.raises(proto.NotOkError):
        client.burst_packet(proto.format_burst_command(1, 0, 1, 100, 200, 8, 7_000_000),
                            proto.HEADER_BYTES, proto.packet_length_from_header)
    assert client.set_spi_clock(8_000_000) is True and client.set_spi_clock(20_000_000) is False


def test_corrupt_and_truncated_replies_and_recovery():
    client, device = make_client()
    command = proto.format_burst_command(1, 0, 1, 100, 200, 8)

    def fetch():
        return client.burst_packet(command, proto.HEADER_BYTES, proto.packet_length_from_header)

    device.corrupt_next_packet = True
    with pytest.raises(proto.CrcError):
        proto.decode_burst_packet(fetch())
    device.truncate_next_packet = True
    with pytest.raises(proto.TruncatedPacketError):
        proto.decode_burst_packet(fetch())
    device.clear_output()                                   # the link recovers for the next command
    assert proto.decode_burst_packet(fetch()).n_frames == 8 + 100


def test_burst_round_trip_over_the_simulated_link():
    client, device = make_client()
    packet = client.burst_packet(proto.format_burst_command(2, 3, 0b1010, 64, 200, 6), proto.HEADER_BYTES,
                                 proto.packet_length_from_header)
    burst = proto.decode_burst_packet(packet)
    assert (burst.array, burst.address, burst.inputs()) == (2, 3, [1, 3])
    assert burst.pre_frames == 6 and burst.n_frames == 6 + 64        # whole round-robin cycles
    assert burst.boot_id == device.boot_id and burst.guard_ns == 200
    assert burst.switch_end_cycles - burst.switch_begin_cycles == 367


@pytest.mark.parametrize("kind,expected_kind,detail", [
    ("captest", "captest", ""), ("production", "production", "TestBoard_ADC124"),
    ("diagnostic", "diagnostic", "ADC124_PCB_DIAGNOSTIC")])
def test_identify_adc124_firmwares(kind, expected_kind, detail):
    client, _ = make_client(kind)
    info = client.identify()
    assert info.kind == expected_kind and info.board_id == BOARD
    if detail:
        assert info.detail == detail
    if kind == "captest":
        assert info.status.firmware == proto.FIRMWARE_NAME and info.status.extra["board"] == BOARD
    else:
        assert "firmware is running" in info.label


def test_identify_silent_device_and_vmid_read():
    client, _ = make_client("silent")
    info = client.identify()
    assert info.kind == "unknown" and "no recognised" in info.label
    client, _ = make_client()
    reading = driver().vmid(client, 1)
    assert reading["mean_code"] == pytest.approx(2048.0 * 1.65 / 1.65, abs=3) and reading["in3"] is not None
    assert "VMID array 1" in driver().vmid_text(reading, 3.3)


def test_both_boards_are_identified_and_matched_from_mcu_and_capstatus():
    registry = get_registry()
    for entry in registry.enabled:
        drv = entry.driver
        for kind, expected in (("captest", "captest"), ("production", "production")):
            device = drv.create_simulator(VirtualClock(), None, drv.sim_config(kind=kind))
            info = CapTestClient(SimLink(device)).identify()
            assert (info.kind, info.board_id) == (expected, entry.id), (entry.id, kind)
    # A 7953 client talking to an ADC124 board reports that the firmware belongs to the ADC124.
    adc124 = driver().create_simulator(VirtualClock(), None, driver().sim_config())
    assert CapTestClient(SimLink(adc124)).identify().board_id == BOARD
    assert registry.match_firmware(mcu="ADC124_PCB_DIAGNOSTIC")[1] == "diagnostic"
    assert registry.match_firmware(mcu="SomethingElse") == (None, "")
