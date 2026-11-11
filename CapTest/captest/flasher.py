"""Run the firmware switching scripts (GUI-free; the GUI streams their output)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

from .link import list_serial_ports

# Legacy per-firmware wrappers of TestBoard_7953 (kept working); every board also has the
# shared script that reads boards.json.
SCRIPT_DIR = (Path(__file__).resolve().parents[2] / "Arduino_Sketches" / "TestBoard_7953_CapTest"
              / "scripts")
SHARED_DIR = Path(__file__).resolve().parents[1] / "scripts"
KINDS = {
    "test": ("flash_test", "capacitance-test firmware (TestBoard_7953_CapTest)"),
    "production": ("flash_production", "production TestBoard_7953 firmware"),
}


def _kind_text(kind: str, board=None) -> str:
    """'capacitance-test firmware (X)' / 'production X firmware' for a board (entry or id)."""
    if board is None:
        return KINDS[kind][1]
    entry = _entry(board)
    if kind == "test":
        return "capacitance-test firmware (%s)" % entry.firmware["firmware_name"]
    return "production %s firmware" % entry.id


def _entry(board):
    if hasattr(board, "firmware"):
        return board
    from .boards import get_registry

    entry = get_registry().get(str(board))
    if entry is None:
        raise KeyError("unknown board %r" % (board,))
    return entry


def script_command(kind: str, port: Optional[str] = None, dry_run: bool = False,
                   yes: bool = True, board=None) -> List[str]:
    """Command line that runs the PowerShell (Windows) or bash script.

    Without `board` the TestBoard_7953 wrapper scripts are used (as in the first version);
    with a board (id or registry entry) the shared flash_firmware script is used."""
    if board is None:
        stem, directory, extra_ps, extra_sh = KINDS[kind][0], SCRIPT_DIR, [], []
        script = lambda ext: str(directory / (stem + ext))   # noqa: E731
    else:
        entry = _entry(board)
        extra_ps = ["-Board", entry.id, "-Firmware", kind]
        extra_sh = ["--board", entry.id, "--firmware", kind]
        script = lambda ext: str(SHARED_DIR / ("flash_firmware" + ext))   # noqa: E731
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        command = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script(".ps1")]
        command += extra_ps
        if port:
            command += ["-Port", port]
        if yes:
            command.append("-Yes")
        if dry_run:
            command.append("-DryRun")
    else:
        command = ["bash", script(".sh")] + extra_sh
        if port:
            command += ["--port", port]
        if yes:
            command.append("--yes")
        if dry_run:
            command.append("--dry-run")
    return command


def describe(kind: str, port: Optional[str], board=None) -> str:
    """Text for the confirmation dialog: what will be replaced, on which port."""
    replaced = _kind_text("production" if kind == "test" else "test", board)
    return ("Flash the %s to the Teensy on %s?\n\nThis replaces whatever firmware is "
            "running now (normally the %s). Nothing else is erased; neither firmware "
            "stores settings on the board, so switching back and forth loses nothing."
            % (_kind_text(kind, board), port or "the auto-detected port", replaced))


def detect_teensy_ports() -> List[str]:
    return [p["device"] for p in list_serial_ports() if p["teensy"]]


def run_script(kind: str, port: Optional[str], on_line: Callable[[str], None],
               dry_run: bool = False, board=None) -> int:
    """Run a switching script, streaming its output line by line."""
    command = script_command(kind, port, dry_run=dry_run, yes=True, board=board)
    on_line("$ " + " ".join(command))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors="replace", bufsize=1)
    assert process.stdout is not None
    for line in process.stdout:
        on_line(line.rstrip("\r\n"))
    return process.wait()


def wait_for_teensy(timeout_s: float = 30.0, preferred: Optional[str] = None,
                    poll_s: float = 0.5) -> Optional[str]:
    """Wait for the Teensy to re-enumerate after an upload; returns its port."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ports = detect_teensy_ports()
        if preferred in ports:
            return preferred
        if ports:
            return ports[0]
        time.sleep(poll_s)
    return None
