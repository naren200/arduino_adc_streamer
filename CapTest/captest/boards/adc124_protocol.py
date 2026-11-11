"""Wire protocol of the TestBoard_ADC124_CapTest firmware (GUI- and serial-free).

Burst packet layout: docs/architecture/firmware/TESTBOARD_ADC124_CAPTEST.md (little-endian).
CRC, exceptions and the capstatus line parser are shared with the other boards.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

from ..protocol import (CrcError, FormatError, NotOkError, ProtocolError,  # noqa: F401
                        TruncatedPacketError, crc16_ccitt_false, parse_key_values)

PROTOCOL_VERSION = 1
MAGIC = b"CB"
HEADER_BYTES = 64
MAX_FRAMES = 4096
MAX_PRE_FRAMES = 64
MIN_POST_FRAMES = 16
MAX_VMID_READS = 64
MAX_PARK_US = 10000
FIRMWARE_NAME = "TestBoard_ADC124_CapTest"
BOARD_NAME = "TestBoard_ADC124"
FRAME_CLOCKS = 16

FLAG_INCOMPLETE = 1 << 0
FLAG_ABORTED = 1 << 1
FLAG_NO_SWITCH = 1 << 2
FLAG_PERIOD_SHORT = 1 << 3
FLAG_SWITCH_SLOW = 1 << 4
FLAG_DATA_ERRORS = 1 << 5
FLAG_STAMP_GAP = 1 << 6

_HEADER = struct.Struct("<2sBBBBBBHHBBHIIIIIIIIIIIHH")
assert _HEADER.size == HEADER_BYTES


@dataclass
class BurstData124:
    """One decoded burst. Returned frame k converts input inputs()[k % n_inputs]."""

    array: int
    address: int                 # 7 = parked read (no connection)
    input_mask: int              # bit i = ADC input IN(i+1)
    pre_frames: int
    words: np.ndarray            # uint16 response words, frame order
    stamps: np.ndarray           # uint32 cycle counter at the pop of each frame
    flags: int = 0
    hold_clocks: int = 3
    guard_ns: int = 200
    cpu_hz: int = 600_000_000
    spi_clock_hz: int = 16_000_000
    boot_id: int = 0
    park_us: int = 0
    start_cycles: int = 0
    lead_cycles: int = 0
    switch_begin_cycles: int = 0
    switch_end_cycles: int = 0
    period_min_cycles: int = 0
    period_sum_cycles: int = 0
    period_max_cycles: int = 0
    transfer_errors: int = 0
    data_errors: int = 0
    version: int = PROTOCOL_VERSION

    @property
    def n_frames(self) -> int:
        return int(len(self.words))

    @property
    def n_inputs(self) -> int:
        return bin(self.input_mask & 0x0F).count("1")

    def inputs(self) -> List[int]:
        """ADC input indices 0..3 (IN1..IN4) in conversion order."""
        return [i for i in range(4) if self.input_mask & (1 << i)]

    @property
    def codes(self) -> np.ndarray:
        return (self.words & 0x0FFF).astype(np.int64)

    def period_ns(self) -> Dict[str, float]:
        """min/mean/max time between consecutive result pops, ns (min/max include the
        polling jitter; the mean is the frame period)."""
        frames = max(self.n_frames - 1, 1)
        to_ns = 1e9 / self.cpu_hz
        return {"min": self.period_min_cycles * to_ns,
                "mean": self.period_sum_cycles / frames * to_ns,
                "max": self.period_max_cycles * to_ns}


def encode_burst_packet(burst: BurstData124) -> bytes:
    n = len(burst.words)
    header = _HEADER.pack(
        MAGIC, burst.version, HEADER_BYTES, burst.array, burst.address, burst.input_mask,
        burst.n_inputs, n, burst.pre_frames, burst.flags, burst.hold_clocks, burst.guard_ns,
        burst.cpu_hz, burst.spi_clock_hz, burst.boot_id, burst.park_us, burst.start_cycles,
        burst.lead_cycles, burst.switch_begin_cycles, burst.switch_end_cycles,
        burst.period_min_cycles, burst.period_sum_cycles, burst.period_max_cycles,
        burst.transfer_errors, burst.data_errors)
    payload = (header + np.asarray(burst.words, "<u2").tobytes()
               + np.asarray(burst.stamps, "<u4").tobytes())
    return payload + struct.pack("<H", crc16_ccitt_false(payload))


def packet_length_from_header(header: bytes) -> int:
    """Total packet length (incl. CRC) given the first HEADER_BYTES bytes."""
    if len(header) < HEADER_BYTES:
        raise TruncatedPacketError("header incomplete")
    if header[:2] != MAGIC:
        raise FormatError("bad magic %r" % header[:2])
    n_frames = struct.unpack_from("<H", header, 8)[0]
    if n_frames > MAX_FRAMES:
        raise FormatError("implausible length n_frames=%d" % n_frames)
    return HEADER_BYTES + 6 * n_frames + 2


def decode_burst_packet(packet: bytes) -> BurstData124:
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
    (_, version, header_len, array, address, mask, inputs, n, pre, flags, hold, guard, cpu_hz,
     spi, boot, park, start, lead, sw0, sw1, p_min, p_sum, p_max, terr, derr
     ) = _HEADER.unpack_from(packet, 0)
    if version != PROTOCOL_VERSION:
        raise FormatError("unsupported protocol version %d" % version)
    if header_len != HEADER_BYTES:
        raise FormatError("unexpected header length %d" % header_len)
    if inputs != bin(mask & 0x0F).count("1") or not 1 <= inputs <= 4 or pre > n:
        raise FormatError("inconsistent header (mask 0x%X, inputs %d, pre %d, frames %d)"
                          % (mask, inputs, pre, n))
    words = np.frombuffer(packet, "<u2", n, HEADER_BYTES).astype(np.uint16)
    stamps = np.frombuffer(packet, "<u4", n, HEADER_BYTES + 2 * n).astype(np.uint32)
    return BurstData124(
        array=array, address=address, input_mask=mask, pre_frames=pre, words=words,
        stamps=stamps, flags=flags, hold_clocks=hold, guard_ns=guard, cpu_hz=cpu_hz,
        spi_clock_hz=spi, boot_id=boot, park_us=park, start_cycles=start, lead_cycles=lead,
        switch_begin_cycles=sw0, switch_end_cycles=sw1, period_min_cycles=p_min,
        period_sum_cycles=p_sum, period_max_cycles=p_max, transfer_errors=terr,
        data_errors=derr, version=version)


def format_burst_command(array: int, address: int, input_mask: int, samples, park_us: int,
                         pre_samples: int, spi_clock_hz: Optional[int] = None) -> str:
    text = "capburst %d %d %d %s %d %d" % (array, address, input_mask, samples, park_us,
                                           pre_samples)
    if spi_clock_hz:
        text += " %d" % spi_clock_hz
    return text + "*"


def parse_vmid_line(line: str) -> dict:
    """'# capvmid array=1 mask=15 n=16 mean_code=2050.8 ... in1=.. data_errors=0'."""
    values = parse_key_values(line)
    if "mean_code" not in values:
        raise FormatError("not a capvmid line: %r" % line)
    result = {"array": int(values["array"]), "mask": int(values["mask"]), "n": int(values["n"]),
              "mean_code": float(values["mean_code"]), "min": int(values["min"]),
              "max": int(values["max"]), "data_errors": int(values.get("data_errors", 0))}
    result["tag_errors"] = result["data_errors"]
    for i in range(1, 5):
        text = values.get("in%d" % i, "-")
        result["in%d" % i] = None if text == "-" else float(text)
    return result
