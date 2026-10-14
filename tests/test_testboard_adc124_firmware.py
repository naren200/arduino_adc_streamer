"""Host simulation of the production ADC124 application; optional compiler dependency.

Run explicitly with: uv run --with ziglang pytest tests/test_testboard_adc124_firmware.py
The regular Python suite skips this test if no native compiler is installed.
"""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("profiling", [False, True])
def test_production_adc_driver_pipeline_gpio_and_error_recovery(tmp_path, profiling):
    project = Path(__file__).resolve().parents[1] / "Arduino_Sketches" / "TestBoard_ADC124"
    if importlib.util.find_spec("ziglang"):
        compiler = [sys.executable, "-m", "ziglang", "c++"]
        if sys.platform == "win32":
            compiler += ["-target", "x86_64-windows-gnu"]
    elif shutil.which("g++"):
        compiler = [shutil.which("g++")]
    elif shutil.which("clang++"):
        compiler = [shutil.which("clang++")]
    else:
        pytest.skip("Install a native C++ compiler or use uv run --with ziglang pytest")
    executable = tmp_path / ("driver_test.exe" if sys.platform == "win32" else "driver_test")
    sources = [project / "src" / f"{name}.cpp" for name in
               ("Adc124s101", "SpiController", "UsbSerialController", "ApiProtocol")]
    command = compiler + ["-std=c++17", "-O1", "-UNDEBUG", "-D__IMXRT1062__",
        f"-DTESTBOARD_PROFILE={int(profiling)}", "-I", str(project / "test" / "host"),
        "-I", str(project / "include"), str(project / "test" / "host" / "driver_test.cpp"),
        *map(str, sources), "-o", str(executable)]
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=180)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    tested = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert tested.returncode == 0, tested.stdout + tested.stderr
    assert "708 ADC124 digital acquisition cases and USB/failure checks passed" in tested.stdout
    if profiling:
        from Arduino_Sketches.TestBoard_ADC124.benchmarks.testboard_adc124_benchmark import parse_status
        from Arduino_Sketches.TestBoard_ADC124.benchmarks.firmware_profile import decode_profile
        status = tested.stdout.split("PROFILE_STATUS_BEGIN\n", 1)[1].split("PROFILE_STATUS_END", 1)[0]
        profile = decode_profile(parse_status(status.splitlines()))
        assert profile["attempts"] == 4 and profile["written"] == 2 and profile["discarded"] == 2
        assert profile["aborted"] == 0
        assert profile["phases"]["run_prepare"]["count"] == 1
        assert profile["phases"]["validate"]["count"] == 4
        assert profile["phases"]["session"]["count"] == 4
