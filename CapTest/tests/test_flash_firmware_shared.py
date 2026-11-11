"""Shared flash script (CapTest/scripts/flash_firmware.*) and the per-firmware wrappers:
--dry-run output only, nothing is uploaded."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from captest import flasher
from captest.boards import get_registry

REPO = Path(flasher.__file__).resolve().parents[2]
SHARED = REPO / "CapTest" / "scripts"
BOARDS = [(e.id, e.firmware) for e in get_registry().enabled]
HAS_PWSH = os.name == "nt" and (shutil.which("pwsh") or shutil.which("powershell"))
HAS_BASH = shutil.which("bash") is not None


def run(command):
    return subprocess.run(command, capture_output=True, text=True, timeout=120)


def ps_command(script, *args):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    return [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args]


def check_dry_run(out, fw, kind):
    project = fw["test_project" if kind == "test" else "production_project"].split("/")[-1]
    env = fw["test_env" if kind == "test" else "production_env"]
    assert "dry run: nothing executed" in out and "nothing else is erased" in out
    assert "-e %s -t upload --upload-port COM9" % env in out
    assert project in out.replace("\\", "/") and "teensy@6.0.0" in out
    if kind == "test":
        assert fw["firmware_name"] in out
    else:
        assert "PRODUCTION" in out


def test_no_board_can_flash_a_diagnostic_or_profile_environment():
    for board, fw in BOARDS:
        for key in ("test_env", "production_env"):
            assert not re.search(r"pcbdiag|profile", fw[key]), (board, key)


@pytest.mark.skipif(not HAS_PWSH, reason="PowerShell scripts need Windows PowerShell")
@pytest.mark.parametrize("kind", ["test", "production"])
@pytest.mark.parametrize("board,fw", BOARDS)
def test_shared_powershell_dry_run(board, fw, kind):
    result = run(ps_command(SHARED / "flash_firmware.ps1", "-Board", board, "-Firmware", kind,
                            "-Port", "COM9", "-DryRun"))
    assert result.returncode == 0, result.stdout + result.stderr
    check_dry_run(result.stdout, fw, kind)


@pytest.mark.skipif(not HAS_BASH, reason="bash not available")
@pytest.mark.parametrize("kind", ["test", "production"])
@pytest.mark.parametrize("board,fw", BOARDS)
def test_shared_bash_dry_run(board, fw, kind):
    result = run(["bash", str(SHARED / "flash_firmware.sh"), "--board", board, "--firmware", kind,
                  "--port", "COM9", "--dry-run"])
    assert result.returncode == 0, result.stdout + result.stderr
    check_dry_run(result.stdout, fw, kind)


@pytest.mark.skipif(not HAS_PWSH, reason="PowerShell scripts need Windows PowerShell")
@pytest.mark.parametrize("kind", ["test", "production"])
@pytest.mark.parametrize("board,fw", BOARDS)
def test_per_firmware_powershell_wrappers(board, fw, kind):
    stem = REPO / fw["flash_scripts"][kind]
    result = run(ps_command(stem.with_suffix(".ps1"), "-Port", "COM9", "-DryRun"))
    assert result.returncode == 0, result.stdout + result.stderr
    check_dry_run(result.stdout, fw, kind)


@pytest.mark.skipif(not HAS_BASH, reason="bash not available")
@pytest.mark.parametrize("kind", ["test", "production"])
@pytest.mark.parametrize("board,fw", BOARDS)
def test_per_firmware_bash_wrappers(board, fw, kind):
    stem = REPO / fw["flash_scripts"][kind]
    result = run(["bash", str(stem.with_suffix(".sh")), "--port", "COM9", "--dry-run"])
    assert result.returncode == 0, result.stdout + result.stderr
    check_dry_run(result.stdout, fw, kind)


@pytest.mark.skipif(not HAS_BASH, reason="bash not available")
def test_unknown_board_and_bad_arguments_fail_clearly():
    result = run(["bash", str(SHARED / "flash_firmware.sh"), "--board", "Nope", "--firmware", "test",
                  "--dry-run", "--port", "COM9"])
    assert result.returncode == 2 and "Unknown board 'Nope'" in result.stderr
    assert "TestBoard_ADC124" in result.stderr                               # lists the known boards
    assert run(["bash", str(SHARED / "flash_firmware.sh"), "--board", "TestBoard_ADC124"]).returncode == 2
    assert run(["bash", str(SHARED / "flash_firmware.sh"), "--board", "TestBoard_ADC124",
                "--firmware", "diag"]).returncode == 2


@pytest.fixture
def mini_repo(tmp_path):
    """A copy of the shared scripts next to a boards.json whose test env is the diagnostic one."""
    (tmp_path / "CapTest" / "scripts").mkdir(parents=True)
    for name in ("flash_firmware.ps1", "flash_firmware.sh"):
        shutil.copy(SHARED / name, tmp_path / "CapTest" / "scripts" / name)
    (tmp_path / "Arduino_Sketches" / "X").mkdir(parents=True)
    (tmp_path / "Arduino_Sketches" / "X" / "platformio.ini").write_text(
        "[env:teensy41_pcbdiag]\nplatform = teensy@6.0.0\n[env:teensy41]\nplatform = teensy@6.0.0\n")
    entry = {"id": "X", "firmware": {"test_project": "Arduino_Sketches/X", "test_env": "teensy41_pcbdiag",
                                     "firmware_name": "X_CapTest", "production_project": "Arduino_Sketches/X",
                                     "production_env": "teensy41_profile", "production_mcu": "X"}}
    (tmp_path / "CapTest" / "boards.json").write_text(json.dumps({"schema": 1, "boards": [entry]}))
    return tmp_path


@pytest.mark.skipif(not HAS_BASH, reason="bash not available")
@pytest.mark.parametrize("kind", ["test", "production"])
def test_diagnostic_and_profile_environments_are_refused_by_bash(mini_repo, kind):
    result = run(["bash", str(mini_repo / "CapTest" / "scripts" / "flash_firmware.sh"), "--board", "X",
                  "--firmware", kind, "--port", "COM9", "--dry-run"])
    assert result.returncode == 2 and "Refusing to flash" in result.stderr


@pytest.mark.skipif(not HAS_PWSH, reason="PowerShell scripts need Windows PowerShell")
@pytest.mark.parametrize("kind", ["test", "production"])
def test_diagnostic_and_profile_environments_are_refused_by_powershell(mini_repo, kind):
    result = run(ps_command(mini_repo / "CapTest" / "scripts" / "flash_firmware.ps1", "-Board", "X",
                            "-Firmware", kind, "-Port", "COM9", "-DryRun"))
    assert result.returncode != 0 and "Refusing to flash" in (result.stdout + result.stderr)


def test_flasher_builds_commands_for_a_board_on_both_platforms(monkeypatch):
    monkeypatch.setattr(flasher.os, "name", "posix")
    command = flasher.script_command("test", "/dev/ttyACM0", dry_run=True, board="TestBoard_ADC124")
    assert command[0] == "bash" and command[1].endswith("flash_firmware.sh")
    assert command[2:] == ["--board", "TestBoard_ADC124", "--firmware", "test", "--port", "/dev/ttyACM0",
                           "--yes", "--dry-run"]
    monkeypatch.setattr(flasher.os, "name", "nt")
    command = flasher.script_command("production", "COM3", board="TestBoard_ADC124")
    assert command[command.index("-Board") + 1] == "TestBoard_ADC124"
    assert command[command.index("-Firmware") + 1] == "production"
    assert command[command.index("-Port") + 1] == "COM3" and "-Yes" in command and "-DryRun" not in command
    with pytest.raises(KeyError):
        flasher.script_command("test", None, board="Nope")


def test_describe_names_the_boards_firmware():
    text = flasher.describe("test", "COM3", "TestBoard_ADC124")
    assert "COM3" in text and "TestBoard_ADC124_CapTest" in text and "production TestBoard_ADC124" in text
    text = flasher.describe("production", "COM3", "TestBoard_ADC124")
    assert "production TestBoard_ADC124 firmware" in text and "TestBoard_ADC124_CapTest" in text


@pytest.mark.parametrize("board,fw", BOARDS)
def test_run_script_streams_dry_run_output(board, fw):
    lines = []
    code = flasher.run_script("test", "COM9", lines.append, dry_run=True, board=board)
    assert code == 0 and any("dry run" in line for line in lines)
    assert any(fw["firmware_name"] in line for line in lines)
