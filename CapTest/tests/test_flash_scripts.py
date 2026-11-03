"""Switching scripts: --dry-run output only (nothing is uploaded)."""

import os
import shutil
import subprocess

import pytest

from captest import flasher


def run_dry(kind, port="COM9"):
    command = flasher.script_command(kind, port, dry_run=True)
    return subprocess.run(command, capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(os.name != "nt" or not (shutil.which("pwsh") or shutil.which("powershell")),
                    reason="PowerShell scripts need Windows PowerShell")
@pytest.mark.parametrize("kind,env,project", [
    ("test", "teensy41_captest", "TestBoard_7953_CapTest"),
    ("production", "teensy41", "TestBoard_7953"),
])
def test_powershell_dry_run(kind, env, project):
    result = run_dry(kind)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "dry run: nothing executed" in out
    assert "-e " + env + " -t upload --upload-port COM9" in out
    assert project in out and "nothing else is erased" in out
    assert "teensy@6.0.0" in out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("script,env", [("flash_test.sh", "teensy41_captest"),
                                        ("flash_production.sh", "teensy41")])
def test_bash_dry_run(script, env):
    path = flasher.SCRIPT_DIR / script
    result = subprocess.run(["bash", str(path), "--port", "COM9", "--dry-run"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "-e %s -t upload --upload-port COM9" % env in result.stdout
    assert "dry run: nothing executed" in result.stdout


def test_script_command_builds_both_platforms(monkeypatch):
    monkeypatch.setattr(flasher.os, "name", "posix")
    command = flasher.script_command("production", "/dev/ttyACM0", dry_run=True)
    assert command[0] == "bash" and command[1].endswith("flash_production.sh")
    assert command[2:] == ["--port", "/dev/ttyACM0", "--yes", "--dry-run"]
    monkeypatch.setattr(flasher.os, "name", "nt")
    command = flasher.script_command("test", "COM3")
    assert command[-3:] == ["-Port", "COM3", "-Yes"] or "-Yes" in command
    assert command[-2] != "-DryRun"


def test_describe_names_port_and_replaced_firmware():
    text = flasher.describe("test", "COM3")
    assert "COM3" in text and "production" in text
    text = flasher.describe("production", "COM3")
    assert "capacitance-test" in text


def test_run_script_streams_dry_run_output():
    lines = []
    code = flasher.run_script("test", "COM9", lines.append, dry_run=True)
    assert code == 0
    assert any("dry run" in line for line in lines)
