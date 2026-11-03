"""Byte-stream links to a device (real serial port or the simulator) and clocks."""

from __future__ import annotations

import time
from typing import List, Optional


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class VirtualClock:
    """Deterministic clock: sleep() advances time instantly (tests, fast CLI sim)."""

    def __init__(self, start: float = 1000.0):
        self._now = start

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += seconds

    def advance(self, seconds: float) -> None:
        self._now += seconds


class LinkError(Exception):
    pass


class Link:
    """Minimal interface used by the client."""

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def read(self, size: int, timeout: float) -> bytes:
        """Return up to `size` bytes, fewer only when the timeout expired."""
        raise NotImplementedError

    def readline(self, timeout: float) -> Optional[bytes]:
        """Return one line without its terminator, or None on timeout."""
        raise NotImplementedError

    def reset_input(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class SerialLink(Link):
    def __init__(self, port: str, baud: int = 460800):
        import serial  # pyserial

        try:
            self._serial = serial.Serial(port, baud, timeout=0.05, write_timeout=2.0)
        except serial.SerialException as exc:
            raise LinkError(str(exc)) from exc
        self._buffer = bytearray()

    def write(self, data: bytes) -> None:
        try:
            self._serial.write(data)
            self._serial.flush()
        except Exception as exc:
            raise LinkError(str(exc)) from exc

    def _fill(self, deadline: float, want: int) -> None:
        while len(self._buffer) < want and time.monotonic() < deadline:
            try:
                chunk = self._serial.read(max(1, min(self._serial.in_waiting, 65536)))
            except Exception as exc:
                raise LinkError(str(exc)) from exc
            if chunk:
                self._buffer += chunk

    def read(self, size: int, timeout: float) -> bytes:
        self._fill(time.monotonic() + timeout, size)
        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out

    def readline(self, timeout: float) -> Optional[bytes]:
        deadline = time.monotonic() + timeout
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                line = bytes(self._buffer[:index])
                del self._buffer[:index + 1]
                return line.rstrip(b"\r")
            if time.monotonic() >= deadline:
                return None
            self._fill(deadline, len(self._buffer) + 1)

    def reset_input(self) -> None:
        try:
            self._serial.reset_input_buffer()
        except Exception as exc:
            raise LinkError(str(exc)) from exc
        self._buffer.clear()

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:
            pass


def list_serial_ports() -> List[dict]:
    """Serial ports with a Teensy flag (PJRC VID 0x16C0)."""
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    ports = []
    for info in list_ports.comports():
        ports.append({"device": info.device, "description": info.description,
                      "teensy": info.vid == 0x16C0})
    return ports
