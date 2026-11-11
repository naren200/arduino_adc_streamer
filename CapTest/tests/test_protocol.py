"""Protocol: encode/decode round trip, CRC failure, truncation, #NOT_OK, identification."""

import numpy as np
import pytest

from captest import protocol
from captest.client import CapTestClient
from captest.link import VirtualClock
from captest.sim import SimConfig, SimLink, SimulatedDevice


def sample_burst(n=40):
    rng = np.random.default_rng(0)
    return protocol.BurstData(
        adc=3, channel=7, n_samples=n,
        words=((7 << 12) | rng.integers(0, 4096, n + 2)).astype(np.uint16),
        timestamps=(rng.integers(0, 1 << 32, n + 2)).astype(np.uint32),
        vmid_words=((15 << 12) | rng.integers(2000, 2100, 9)).astype(np.uint16),
        cpu_hz=600_000_000, spi_clock_hz=20_000_000, gap_ns=200,
        first_clock_delay_ns=25, boot_id=0xDEADBEEF, park_us=200, park_frames=77,
        period_min_cycles=640, period_sum_cycles=26000, period_max_cycles=700,
        timeout_errors=1, tag_errors=2, flags=5)


def test_crc_known_vector():
    # CRC-16/CCITT-FALSE check value for "123456789".
    assert protocol.crc16_ccitt_false(b"123456789") == 0x29B1


def test_round_trip():
    burst = sample_burst()
    packet = protocol.encode_burst_packet(burst)
    assert len(packet) == protocol.HEADER_BYTES + 2 * 9 + 6 * 42 + 2
    decoded = protocol.decode_burst_packet(packet)
    for name in ("adc", "channel", "n_samples", "cpu_hz", "spi_clock_hz", "gap_ns",
                 "first_clock_delay_ns", "boot_id", "park_us", "park_frames",
                 "period_min_cycles", "period_sum_cycles", "period_max_cycles",
                 "timeout_errors", "tag_errors", "flags"):
        assert getattr(decoded, name) == getattr(burst, name), name
    assert np.array_equal(decoded.words, burst.words)
    assert np.array_equal(decoded.timestamps, burst.timestamps)
    assert np.array_equal(decoded.vmid_words, burst.vmid_words)


def test_documented_example_frame_is_stable():
    """The byte layout in TESTBOARD_7953_CAPTEST.md (little-endian, CRC-16/CCITT-FALSE)."""
    burst = protocol.BurstData(
        adc=2, channel=5, n_samples=2,
        words=np.array([0xF800, 0xF801, 0x5400, 0x5401], np.uint16),
        timestamps=np.array([1000, 1640, 2280, 2920], np.uint32),
        vmid_words=np.array([0xF7FF, 0xF800], np.uint16),
        cpu_hz=600_000_000, spi_clock_hz=20_000_000, gap_ns=200, first_clock_delay_ns=25,
        boot_id=0x12345678, park_us=200, park_frames=100, period_min_cycles=640,
        period_sum_cycles=1920, period_max_cycles=640)
    packet = protocol.encode_burst_packet(burst)
    assert packet[:4] == b"CB\x01\x38"
    assert packet[4] == 2 and packet[5] == 5
    assert packet[6:8] == b"\x02\x00" and packet[8:10] == b"\x04\x00"
    assert packet[10] == 2
    assert packet[12:16] == (600_000_000).to_bytes(4, "little")
    assert packet[28:32] == bytes.fromhex("78563412")
    assert len(packet) == 56 + 4 + 8 + 16 + 2
    assert protocol.decode_burst_packet(packet).boot_id == 0x12345678


def test_crc_failure():
    packet = bytearray(protocol.encode_burst_packet(sample_burst()))
    packet[80] ^= 0x10
    with pytest.raises(protocol.CrcError):
        protocol.decode_burst_packet(bytes(packet))


def test_truncated():
    packet = protocol.encode_burst_packet(sample_burst())
    for cut in (10, protocol.HEADER_BYTES, len(packet) - 1):
        with pytest.raises(protocol.TruncatedPacketError):
            protocol.decode_burst_packet(packet[:cut])


def test_bad_magic_and_implausible_length():
    packet = bytearray(protocol.encode_burst_packet(sample_burst()))
    packet[0] = ord("X")
    with pytest.raises(protocol.FormatError):
        protocol.decode_burst_packet(bytes(packet))
    packet = bytearray(protocol.encode_burst_packet(sample_burst()))
    packet[8:10] = (60000).to_bytes(2, "little")
    with pytest.raises(protocol.FormatError):
        protocol.packet_length_from_header(bytes(packet[:56]))


def test_status_line_parse():
    status = protocol.parse_status_line(
        "# capstatus firmware=TestBoard_7953_CapTest version=1.0.0 protocol=1 git=abc1234-dirty "
        "build=Oct_23_2026_10:00:00 boot_id=0xCAFE uptime_ms=1234 cpu_hz=600000000 "
        "spi_clock_hz=20000000 max_samples=2048 min_period_ns=1000")
    assert status.firmware == protocol.FIRMWARE_NAME and status.boot_id == 0xCAFE
    assert status.uptime_ms == 1234 and status.extra == {"min_period_ns": "1000"}
    with pytest.raises(protocol.FormatError):
        protocol.parse_status_line("# something else")


def make_client(**config):
    device = SimulatedDevice(SimConfig(**config), clock=VirtualClock())
    return CapTestClient(SimLink(device)), device


def test_client_burst_round_trip_over_simulated_link():
    client, device = make_client()
    burst = client.burst(2, 3, 200, 200, 200, 25)
    assert burst.adc == 2 and burst.channel == 3 and burst.n_frames == 202
    assert burst.boot_id == device.boot_id


@pytest.mark.parametrize("command,args", [
    ("channel15", (1, 15, 100, 200, 200)),       # Vmid is not a sensor channel
    ("unrouted_adc1", (1, 10, 100, 200, 200)),   # ADC1 routes only 0..9
    ("unrouted_adc4", (4, 15, 100, 200, 200)),
    ("period", (1, 0, 100, 200, 100)),           # 16 clocks + 100 ns < 1.0 us
    ("bad_adc", (5, 0, 100, 200, 200)),
    ("too_many", (1, 0, 5000, 200, 200)),
])
def test_rejections_raise_not_ok(command, args):
    client, _ = make_client()
    with pytest.raises(protocol.NotOkError):
        client.burst(*args)


def test_truncated_and_corrupt_replies():
    client, device = make_client()
    device.corrupt_next_packet = True
    with pytest.raises(protocol.CrcError):
        client.burst(1, 0, 100, 200, 200)
    device.truncate_next_packet = True
    with pytest.raises(protocol.TruncatedPacketError):
        client.burst(1, 0, 100, 200, 200)
    # The link recovers for the next command.
    device.clear_output()
    assert client.burst(1, 0, 100, 200, 200).n_frames == 102


def test_identify_captest_production_and_silent():
    client, _ = make_client()
    info = client.identify()
    assert info.kind == "captest" and info.status.firmware == protocol.FIRMWARE_NAME
    client, _ = make_client(kind="production")
    info = client.identify()
    assert info.kind == "production" and "production firmware" in info.label
    client, _ = make_client(kind="silent")
    info = client.identify()
    assert info.kind == "unknown" and "no recognised" in info.label


def test_vmid_reading_near_midscale():
    client, _ = make_client()
    reading = client.vmid(1)
    assert reading["mean_code"] == pytest.approx(2048, abs=3)
    assert reading["tag_errors"] == 0
