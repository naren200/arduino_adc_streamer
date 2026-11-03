"""Byte-stream plumbing shared by the board simulators.

A simulated device is fed command bytes ('*'-terminated lines) and returns reply
bytes (text lines and binary burst packets) exactly like the serial port of the real
firmware; SimLink wraps it as a Link so the client code is identical.
"""

from __future__ import annotations

from typing import List, Optional

from .link import Link, LinkError, VirtualClock


class SimBase:
    """Command line framing, output buffer and fault-injection switches."""

    def __init__(self, clock=None):
        self.clock = clock or VirtualClock()
        self._inbox = bytearray()
        self._outbox = bytearray()
        self.connected = True
        self.commands: List[str] = []         # every command received (for tests)
        self.bursts_served = 0
        self.parked = True
        self.corrupt_next_packet = False
        self.truncate_next_packet = False
        self.fail_next_burst = False

    def disconnect(self) -> None:
        self.connected = False

    # --------------------------------------------------------------- byte I/O
    def feed(self, data: bytes) -> None:
        if not self.connected:
            raise LinkError("device disconnected")
        self._inbox += data
        while True:
            end = self._inbox.find(b"*")
            if end < 0:
                break
            line = self._inbox[:end].decode("ascii", "replace").strip()
            del self._inbox[:end + 1]
            if line:
                self.commands.append(line)
                self._handle(line)

    def take(self, size: int) -> bytes:
        out = bytes(self._outbox[:size])
        del self._outbox[:size]
        return out

    def take_line(self) -> Optional[bytes]:
        index = self._outbox.find(b"\n")
        if index < 0:
            return None
        line = bytes(self._outbox[:index])
        del self._outbox[:index + 1]
        return line.rstrip(b"\r")

    def clear_output(self) -> None:
        self._outbox.clear()

    def _say(self, text: str) -> None:
        self._outbox += text.encode("ascii") + b"\r\n"

    def _ack(self, ok: bool, arguments: str) -> None:
        self._say(("#OK" if ok else "#NOT_OK") + (" " + arguments if arguments else ""))

    def _send_packet(self, packet: bytes) -> None:
        if self.corrupt_next_packet:
            self.corrupt_next_packet = False
            packet = packet[:60] + bytes([packet[60] ^ 0xFF]) + packet[61:]
        if self.truncate_next_packet:
            self.truncate_next_packet = False
            packet = packet[:len(packet) // 2]
        self._outbox += packet

    def _handle(self, line: str) -> None:
        raise NotImplementedError


class SimLink(Link):
    """Link wrapper so client code is identical for simulator and serial port."""

    def __init__(self, device: SimBase):
        self.device = device

    def write(self, data: bytes) -> None:
        self.device.feed(data)

    def read(self, size: int, timeout: float) -> bytes:
        return self.device.take(size)

    def readline(self, timeout: float) -> Optional[bytes]:
        return self.device.take_line()

    def reset_input(self) -> None:
        self.device.clear_output()

    def close(self) -> None:
        pass
