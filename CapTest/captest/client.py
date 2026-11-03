"""Command/response client for the capacitance-test firmware."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from . import protocol
from .link import Link, LinkError


@dataclass
class FirmwareInfo:
    kind: str                       # "captest" | "production" | "diagnostic" | "unknown"
    status: Optional[protocol.DeviceStatus] = None
    detail: str = ""
    board_id: str = ""              # registry board the attached firmware belongs to ("" = unknown)
    mcu: str = ""                   # answer of mcu* (without the '# ')

    @property
    def label(self) -> str:
        if self.kind == "captest" and self.status:
            return "capacitance-test firmware %s (git %s, boot 0x%X)" % (
                self.status.version, self.status.git, self.status.boot_id)
        if self.kind == "production":
            return "production firmware is running (no capacitance-test commands)"
        if self.kind == "diagnostic":
            return "PCB-diagnostic firmware is running (no capacitance-test commands)"
        return "no recognised firmware reply (%s)" % (self.detail or "no answer")


class CapTestClient:
    def __init__(self, link: Link, command_timeout: float = 2.0, burst_timeout: float = 5.0):
        self.link = link
        self.command_timeout = command_timeout
        self.burst_timeout = burst_timeout

    # ------------------------------------------------------------------ basic
    def command(self, text: str, timeout: Optional[float] = None) -> Tuple[bool, List[str]]:
        """Send one '*'-terminated command; return (ok, comment lines).

        Raises ProtocolError when no #OK / #NOT_OK arrives in time."""
        if not text.endswith("*"):
            text += "*"
        self.link.write(text.encode("ascii"))
        return self._read_reply(timeout or self.command_timeout)

    def _read_reply(self, timeout: float) -> Tuple[bool, List[str]]:
        comments: List[str] = []
        while True:
            raw = self.link.readline(timeout)
            if raw is None:
                raise protocol.ProtocolError("no reply within %.1f s" % timeout)
            line = raw.decode("ascii", "replace").strip()
            if line.startswith("#OK"):
                return True, comments
            if line.startswith("#NOT_OK"):
                return False, comments
            if line.startswith("#"):
                comments.append(line)

    # ----------------------------------------------------------------- queries
    def identify(self) -> FirmwareInfo:
        """Tell a capacitance-test firmware from a production / diagnostic firmware and
        say which registry board it belongs to (FirmwareInfo.board_id)."""
        from .boards import get_registry

        registry = get_registry()
        try:
            self.link.reset_input()
            ok, comments = self.command("capstatus*", timeout=1.0)
        except (protocol.ProtocolError, LinkError) as exc:
            return FirmwareInfo("unknown", detail=str(exc))
        if ok:
            for line in comments:
                if "firmware=" in line:
                    try:
                        status = protocol.parse_status_line(line)
                    except protocol.ProtocolError:
                        continue
                    entry, kind = registry.match_firmware(firmware=status.firmware)
                    if entry is not None:
                        return FirmwareInfo("captest", status, board_id=entry.id,
                                            mcu=status.firmware)
        # capstatus unknown: the production firmware answers #NOT_OK; ask who it is.
        mcu = ""
        try:
            ok, comments = self.command("mcu*", timeout=1.0)
            if ok:
                for line in comments:
                    if line.startswith("# "):
                        mcu = line[2:].strip()
                        break
        except (protocol.ProtocolError, LinkError) as exc:
            return FirmwareInfo("unknown", detail=str(exc))
        entry, kind = registry.match_firmware(mcu=mcu)
        if entry is not None and kind in ("production", "diagnostic"):
            return FirmwareInfo(kind, detail=mcu, board_id=entry.id, mcu=mcu)
        try:
            ok, comments = self.command("status*", timeout=1.0)
        except (protocol.ProtocolError, LinkError) as exc:
            return FirmwareInfo("unknown", detail=str(exc), mcu=mcu)
        if ok and any("Current mode" in line for line in comments):
            return FirmwareInfo("production", detail="status reply: " + "; ".join(comments[:2]),
                                board_id="TestBoard_7953", mcu=mcu)
        return FirmwareInfo("unknown", detail="unexpected status reply" + (
            " (mcu %s)" % mcu if mcu else ""), mcu=mcu)

    def status(self) -> protocol.DeviceStatus:
        ok, comments = self.command("capstatus*")
        if not ok:
            raise protocol.NotOkError("capstatus rejected")
        for line in comments:
            if "firmware=" in line:
                return protocol.parse_status_line(line)
        raise protocol.FormatError("capstatus reply has no status line")

    def vmid(self, adc: int, count: int = 16) -> dict:
        ok, comments = self.command("capvmid %d %d*" % (adc, count))
        if not ok:
            raise protocol.NotOkError("capvmid rejected")
        for line in comments:
            if line.startswith("# capvmid"):
                values = protocol.parse_key_values(line)
                return {"adc": int(values["adc"]), "n": int(values["n"]),
                        "mean_code": float(values["mean_code"]),
                        "min": int(values["min"]), "max": int(values["max"]),
                        "tag_errors": int(values["tag_errors"])}
        raise protocol.FormatError("capvmid reply has no data line")

    def abort(self) -> bool:
        ok, _ = self.command("capabort*")
        return ok

    def set_spi_clock(self, hz: int) -> bool:
        ok, _ = self.command("spiclock %d*" % hz)
        return ok

    # ------------------------------------------------------------------- burst
    def burst_packet(self, text: str, header_bytes: int, length_from_header) -> bytes:
        """Send a burst command and return the raw packet that follows the ack.

        An incomplete burst is acknowledged #NOT_OK but still sends its packet; a
        rejected command sends none (NotOkError)."""
        self.link.write(text.encode("ascii"))
        ok, _ = self._read_reply(self.burst_timeout)
        # The packet follows the ack immediately, so a rejection need not wait long.
        header = self.link.read(header_bytes, self.burst_timeout if ok else 0.5)
        if len(header) < header_bytes:
            if not ok:
                raise protocol.NotOkError("capburst rejected: %s" % text.rstrip("*"))
            raise protocol.TruncatedPacketError("burst header incomplete (%d bytes)" % len(header))
        total = length_from_header(header)
        rest = self.link.read(total - header_bytes, self.burst_timeout)
        return header + rest

    def burst(self, adc: int, channel: int, samples, park_us: int, gap_ns: int,
              first_clock_delay_ns: Optional[int] = None) -> protocol.BurstData:
        """TestBoard_7953 burst (the ADC124 driver builds its own command)."""
        text = protocol.format_burst_command(adc, channel, samples, park_us, gap_ns,
                                             first_clock_delay_ns)
        packet = self.burst_packet(text, protocol.HEADER_BYTES, protocol.packet_length_from_header)
        return protocol.decode_burst_packet(packet)
