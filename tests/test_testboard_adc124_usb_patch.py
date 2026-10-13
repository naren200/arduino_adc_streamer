"""Verify both the audited core repair and its compiled ADC124 ARM writer."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

from Arduino_Sketches.TestBoard_ADC124.scripts.usb_core_patch import patch_usb_serial


def test_unknown_core_is_rejected():
    with pytest.raises(RuntimeError, match="differs from audited"):
        patch_usb_serial("unknown framework")


def test_guard_and_flush_repair_does_not_modify_installed_core():
    path = Path.home() / ".platformio/packages/framework-arduinoteensy/cores/teensy4/usb_serial.c"
    if not path.exists():
        pytest.skip("Pinned Teensy core not installed")
    original = path.read_bytes()
    repaired = patch_usb_serial(original.decode())
    assert repaired.count('tx_noautoflush = 1;\n\tasm volatile("" ::: "memory");') == 4
    flush = repaired.split("void usb_serial_flush_output(void)", 1)[1]
    assert flush.index("tx_noautoflush = 1") < flush.index("if (tx_available == 0)")
    assert "usb_serial_try_write_frame" in repaired
    assert path.read_bytes() == original


@pytest.mark.parametrize("environment", ["teensy41", "teensy41_profile"])
def test_compiled_arm_writer_never_waits_or_emits_partial_frames(environment):
    if not importlib.util.find_spec("unicorn") or not importlib.util.find_spec("elftools"):
        pytest.skip("Use uv run --with unicorn --with pyelftools to execute ARM verification")
    project = Path(__file__).resolve().parents[1] / "Arduino_Sketches/TestBoard_ADC124"
    elf = project / ".pio/build" / environment / "firmware.elf"
    if not elf.exists():
        pytest.skip("Build both ADC124 firmware environments first")
    checked = subprocess.run([sys.executable, str(project / "benchmarks/verify_live_usb_elf.py"), "--elf", str(elf)],
                             capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "live USB ARM scenarios" in checked.stdout
