"""Wire protocol of the TestBoard_7953_CapTest firmware (GUI- and serial-free).

Binary burst packet layout is documented in
docs/architecture/firmware/TESTBOARD_7953_CAPTEST.md (little-endian).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, Optional

import numpy as np

PROTOCOL_VERSION = 1
MAGIC = b"CB"
HEADER_BYTES = 56
MAX_VMID_WORDS = 16
MAX_SAMPLES = 2048
LEAD_FRAMES = 2
VMID_CHANNEL = 15
FIRMWARE_NAME = "TestBoard_7953_CapTest"

FLAG_PERIOD_TOO_SHORT = 1 << 0
FLAG_ACQUISITION_SHORT = 1 << 1
FLAG_INCOMPLETE = 1 << 2
FLAG_TAG_ERRORS = 1 << 3
FLAG_CONTINUE_MODE = 1 << 4
FLAG_ALTERNATE = 1 << 5
MAX_RUN_FRAMES = 8

_HEADER = struct.Struct("<2sBBBBHHBBIIIIIIIIIIHH")
assert _HEADER.size == HEADER_BYTES


class ProtocolError(Exception):
    """Base class for malformed device replies."""


class TruncatedPacketError(ProtocolError):
    pass


class CrcError(ProtocolError):
    pass


class FormatError(ProtocolError):
    pass


class NotOkError(ProtocolError):
    """The device answered #NOT_OK."""


def crc16_ccitt_false(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xorout."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass
class BurstData:
    """One decoded burst. Frame index j: 0 = command frame k, 1 = frame k+1
    (the mux switches during it), j >= 2 return samples (sample i = frame 2+i)."""

    adc: int
    channel: int
    n_samples: int
    words: np.ndarray          # uint16, one response word per recorded frame
    timestamps: np.ndarray     # uint32 cycle counter at each frame start
    vmid_words: np.ndarray     # uint16, last park results (chronological)
    cpu_hz: int
    spi_clock_hz: int
    gap_ns: int
    first_clock_delay_ns: int
    boot_id: int
    park_us: int
    park_frames: int
    period_min_cycles: int
    period_sum_cycles: int
    period_max_cycles: int
    timeout_errors: int = 0
    tag_errors: int = 0
    flags: int = 0
    version: int = PROTOCOL_VERSION

    @property
    def n_frames(self) -> int:
        return int(len(self.words))

    @property
    def codes(self) -> np.ndarray:
        return (self.words & 0x0FFF).astype(np.int64)

    @property
    def tags(self) -> np.ndarray:
        return (self.words >> 12).astype(np.int64)

    def period_ns(self) -> Dict[str, float]:
        """min/mean/max frame period in ns from the firmware's cycle statistics."""
        frames = max(self.n_frames - 1, 1)
        to_ns = 1e9 / self.cpu_hz
        return {
            "min": self.period_min_cycles * to_ns,
            "mean": self.period_sum_cycles / frames * to_ns,
            "max": self.period_max_cycles * to_ns,
        }


def encode_burst_packet(burst: BurstData) -> bytes:
    n_frames = len(burst.words)
    n_vmid = len(burst.vmid_words)
    header = _HEADER.pack(
        MAGIC, burst.version, HEADER_BYTES, burst.adc, burst.channel,
        burst.n_samples, n_frames, n_vmid, burst.flags, burst.cpu_hz,
        burst.spi_clock_hz, burst.gap_ns, burst.first_clock_delay_ns,
        burst.boot_id, burst.park_us, burst.park_frames,
        burst.period_min_cycles, burst.period_sum_cycles, burst.period_max_cycles,
        burst.timeout_errors, burst.tag_errors)
    body = (np.asarray(burst.vmid_words, "<u2").tobytes()
            + np.asarray(burst.words, "<u2").tobytes()
            + np.asarray(burst.timestamps, "<u4").tobytes())
    payload = header + body
    return payload + struct.pack("<H", crc16_ccitt_false(payload))


def packet_length_from_header(header: bytes) -> int:
    """Total packet length (incl. CRC) given the first HEADER_BYTES bytes."""
    if len(header) < HEADER_BYTES:
        raise TruncatedPacketError("header incomplete")
    if header[:2] != MAGIC:
        raise FormatError("bad magic %r" % header[:2])
    n_frames = struct.unpack_from("<H", header, 8)[0]
    n_vmid = header[10]
    if n_frames > MAX_SAMPLES + LEAD_FRAMES or n_vmid > MAX_VMID_WORDS:
        raise FormatError("implausible lengths n_frames=%d n_vmid=%d" % (n_frames, n_vmid))
    return HEADER_BYTES + 2 * n_vmid + 6 * n_frames + 2


def decode_burst_packet(packet: bytes) -> BurstData:
    if len(packet) < HEADER_BYTES + 2:
        raise TruncatedPacketError("packet shorter than a header")
    total = packet_length_from_header(packet[:HEADER_BYTES])
    if len(packet) < total:
        raise TruncatedPacketError("need %d bytes, have %d" % (total, len(packet)))
    packet = packet[:total]
    expected = struct.unpack_from("<H", packet, total - 2)[0]
    actual = crc16_ccitt_false(packet[:-2])
    if expected != actual:
        raise CrcError("CRC mismatch: packet 0x%04X, computed 0x%04X" % (expected, actual))
    (_, version, header_len, adc, channel, n_samples, n_frames, n_vmid, flags,
     cpu_hz, spi_clock_hz, gap_ns, fcd_ns, boot_id, park_us, park_frames,
     p_min, p_sum, p_max, timeouts, tag_errors) = _HEADER.unpack_from(packet, 0)
    if version != PROTOCOL_VERSION:
        raise FormatError("unsupported protocol version %d" % version)
    if header_len != HEADER_BYTES:
        raise FormatError("unexpected header length %d" % header_len)
    offset = HEADER_BYTES
    vmid = np.frombuffer(packet, "<u2", n_vmid, offset).astype(np.uint16)
    offset += 2 * n_vmid
    words = np.frombuffer(packet, "<u2", n_frames, offset).astype(np.uint16)
    offset += 2 * n_frames
    stamps = np.frombuffer(packet, "<u4", n_frames, offset).astype(np.uint32)
    return BurstData(
        adc=adc, channel=channel, n_samples=n_samples, words=words,
        timestamps=stamps, vmid_words=vmid, cpu_hz=cpu_hz,
        spi_clock_hz=spi_clock_hz, gap_ns=gap_ns, first_clock_delay_ns=fcd_ns,
        boot_id=boot_id, park_us=park_us, park_frames=park_frames,
        period_min_cycles=p_min, period_sum_cycles=p_sum, period_max_cycles=p_max,
        timeout_errors=timeouts, tag_errors=tag_errors, flags=flags, version=version)


# ---------------------------------------------------------------- text lines

@dataclass
class DeviceStatus:
    firmware: str
    version: str = ""
    protocol: int = 0
    git: str = ""
    build: str = ""
    boot_id: int = 0
    uptime_ms: int = 0
    cpu_hz: int = 0
    spi_clock_hz: int = 0
    max_samples: int = 0
    extra: Dict[str, str] = field(default_factory=dict)


def parse_key_values(line: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for token in line.split():
        if "=" in token:
            key, value = token.split("=", 1)
            result[key] = value
    return result


def parse_status_line(line: str) -> DeviceStatus:
    """Parse '# capstatus firmware=... boot_id=0x...' into a DeviceStatus."""
    values = parse_key_values(line)
    if "firmware" not in values:
        raise FormatError("not a capstatus line: %r" % line)

    def number(key: str) -> int:
        text = values.get(key, "0")
        return int(text, 16) if text.lower().startswith("0x") else int(text)

    known = {"firmware", "version", "protocol", "git", "build", "boot_id",
             "uptime_ms", "cpu_hz", "spi_clock_hz", "max_samples"}
    return DeviceStatus(
        firmware=values["firmware"], version=values.get("version", ""),
        protocol=number("protocol"), git=values.get("git", ""),
        build=values.get("build", ""), boot_id=number("boot_id"),
        uptime_ms=number("uptime_ms"), cpu_hz=number("cpu_hz"),
        spi_clock_hz=number("spi_clock_hz"), max_samples=number("max_samples"),
        extra={k: v for k, v in values.items() if k not in known})


def format_burst_command(adc: int, channel: int, samples, park_us: int, gap_ns: int,
                         first_clock_delay_ns: Optional[int] = None,
                         hold_mode: Optional[int] = None) -> str:
    """hold_mode (firmware 1.1.0): 1 sends "continue" after the connection, 0 re-commands
    the channel every frame. It is positional, so it needs first_clock_delay_ns."""
    if hold_mode is not None and first_clock_delay_ns is None:
        raise ValueError("hold_mode needs first_clock_delay_ns")
    text = "capburst %d %d %s %d %d" % (adc, channel, samples, park_us, gap_ns)
    if first_clock_delay_ns is not None:
        text += " %d" % first_clock_delay_ns
    if hold_mode is not None:
        text += " %d" % hold_mode
    return text + "*"


def format_alternate_command(adc: int, channel: int, frames: int, park_us: int, gap_ns: int,
                             sensor_run: int, vmid_run: int,
                             first_clock_delay_ns: Optional[int] = None,
                             gap_long_ns: Optional[int] = None) -> str:
    """capalt (firmware 1.2.0): after the park, repeat sensor_run commands of the channel
    and vmid_run commands of Vmid, like production with Vmid between channels.
    gap_long_ns (firmware 1.3.0) stretches the connected frames of every second cycle, so
    one burst holds two connected times. It is positional, so it needs first_clock_delay_ns."""
    if gap_long_ns is not None and first_clock_delay_ns is None:
        raise ValueError("gap_long_ns needs first_clock_delay_ns")
    text = "capalt %d %d %d %d %d %d %d" % (adc, channel, frames, park_us, gap_ns, sensor_run, vmid_run)
    if first_clock_delay_ns is not None:
        text += " %d" % first_clock_delay_ns
    if gap_long_ns is not None:
        text += " %d" % gap_long_ns
    return text + "*"


def alternate_pattern(burst: "BurstData") -> Optional[tuple]:
    """(sensor_run, vmid_run) of a capalt burst, read from the returned channel tags
    (the packet does not carry the pattern). None when the tags show no alternation."""
    tags = burst.tags[LEAD_FRAMES:LEAD_FRAMES + burst.n_samples]
    if len(tags) < 4 or tags[0] != burst.channel:
        return None
    runs = []
    value, length = tags[0], 0
    for tag in tags:
        if tag == value:
            length += 1
        else:
            runs.append((int(value), length))
            value, length = tag, 1
        if len(runs) == 2:
            break
    if len(runs) < 2 or runs[1][0] != VMID_CHANNEL:
        return None
    return runs[0][1], runs[1][1]


def alternate_commands_sensor(n_samples: int, sensor_run: int, vmid_run: int) -> np.ndarray:
    """True for each command frame of a capalt burst that selects the sensor. The returned
    word of frame j >= LEAD_FRAMES is the sample of command j - LEAD_FRAMES."""
    frame = np.arange(n_samples)
    return frame % (sensor_run + vmid_run) < sensor_run


def timestamps_to_seconds(timestamps: np.ndarray, cpu_hz: float, origin_index: int = 0) -> np.ndarray:
    """Unwrap uint32 cycle counts into seconds relative to timestamps[origin_index]."""
    deltas = (timestamps.astype(np.int64) - int(timestamps[origin_index])) & 0xFFFFFFFF
    # A burst is far shorter than half the 32-bit range; values above it are
    # frames before the origin.
    deltas = np.where(deltas >= 1 << 31, deltas - (1 << 32), deltas)
    return deltas.astype(np.float64) / float(cpu_hz)
