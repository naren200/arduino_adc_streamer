"""Command/response client for the capacitance-test firmware."""

from __future__ import annotations

import time
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
        self._pending = bytearray()      # bytes read ahead while telling chunks from text lines
        self._streaming = False

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

    # ------------------------------------------------------- continuous capture
    def capture_require(self) -> dict:
        """Ask the firmware for the capture feature (capcaps). Older firmware answers #NOT_OK:
        raises CaptureUnavailable, the burst workflows keep working."""
        from .capture import CaptureUnavailable

        try:
            ok, comments = self.command("capcaps*")
        except (protocol.ProtocolError, LinkError) as exc:
            raise CaptureUnavailable("no answer to capcaps: %s" % exc) from exc
        if not ok:
            raise CaptureUnavailable("this firmware has no continuous capture (flash a newer "
                                     "capacitance-test firmware); burst measurements still work")
        for line in comments:
            if line.startswith("# capcaps"):
                values = protocol.parse_key_values(line)
                if int(values.get("capture", 0)) != 1:
                    raise CaptureUnavailable("unsupported capture protocol %s" % values.get("capture"))
                return values
        raise CaptureUnavailable("capcaps reply has no capability line")

    def capture_configure(self, config) -> dict:
        """Send the scan configuration (begin / route... / commit); returns the commit values."""
        info: dict = {}
        for line in config.command_lines():
            ok, comments = self.command(line)
            if not ok:
                raise protocol.NotOkError("capture configuration rejected at: %s" % line.rstrip("*"))
            for comment in comments:
                if comment.startswith("# capcfg"):
                    info = protocol.parse_key_values(comment)
        return info

    def capture_start(self, max_sweeps: int = 0) -> None:
        self._pending.clear()
        ok, _ = self.command("capstart %d*" % max_sweeps)
        if not ok:
            raise protocol.NotOkError("capstart rejected")
        self._streaming = True

    def capture_stop(self) -> None:
        """Ask the board to stop; the final chunk(s) and the acknowledgement follow."""
        self.link.write(b"capstop*")

    def capture_streaming(self) -> bool:
        return self._streaming

    def resync(self, quiet_s: float = 0.25, max_s: float = 3.0) -> None:
        """Bring the line to a known state before a capture: abort anything still running,
        discard what the board still sends (stale chunks, a late acknowledgement) until the line
        is quiet, then check that the board answers and is idle. Without this a failed capture
        can leave bytes that the next command would read as its own reply."""
        self._pending.clear()
        self._streaming = False
        try:
            self.link.write(b"capabort*")
        except LinkError:
            raise
        deadline = time.monotonic() + max_s
        while time.monotonic() < deadline:
            if not self.link.read(65536, quiet_s):
                break
        self.link.reset_input()
        self._pending.clear()
        ok, comments = self.command("capstat*", timeout=1.0)
        state = ""
        for line in comments:
            if line.startswith("# capstat"):
                state = protocol.parse_key_values(line).get("state", "")
        if not ok and not state:
            return                      # firmware without capture commands: nothing can be running
        if not ok or state != "idle":
            raise protocol.ProtocolError("the board is not idle after capabort (state=%s)" % (state or "?"))

    def capture_status(self) -> dict:
        ok, comments = self.command("capstat*")
        if not ok:
            raise protocol.NotOkError("capstat rejected")
        for line in comments:
            if line.startswith("# capstat"):
                return protocol.parse_key_values(line)
        raise protocol.FormatError("capstat reply has no status line")

    def _take(self, size: int, timeout: float) -> bool:
        while len(self._pending) < size:
            part = self.link.read(size - len(self._pending), timeout)
            if not part:
                return False
            self._pending += part
        return True

    def capture_items(self, timeout: float):
        """Yield ('chunk', bytes), ('line', text) and ('ack', ok) items that arrive within
        ``timeout`` of each other. Chunks and text lines may interleave, so each item is
        framed: 'CK' starts a binary chunk of header-given length, anything else is a line."""
        from .capture import CHUNK_MAGIC, HEADER_BYTES, chunk_length_from_header

        while True:
            if not self._take(2, timeout):
                return
            if bytes(self._pending[:2]) == CHUNK_MAGIC:
                if not self._take(HEADER_BYTES, self.burst_timeout):
                    raise protocol.TruncatedPacketError("capture chunk header incomplete")
                total = chunk_length_from_header(bytes(self._pending[:HEADER_BYTES]))
                if not self._take(total, self.burst_timeout):
                    raise protocol.TruncatedPacketError("capture chunk incomplete")
                data = bytes(self._pending[:total])
                del self._pending[:total]
                yield "chunk", data
                continue
            head = bytes(self._pending)
            self._pending.clear()
            if b"\n" in head:
                line_bytes, _, rest = head.partition(b"\n")
                self._pending += rest
            else:
                tail = self.link.readline(timeout)
                if tail is None:
                    self._pending += head      # keep the partial line for the next call
                    return
                line_bytes = head + tail
            line = line_bytes.decode("ascii", "replace").strip()
            if line.startswith("#OK") or line.startswith("#NOT_OK"):
                self._streaming = False
                yield "ack", line.startswith("#OK")
            elif line:
                yield "line", line
