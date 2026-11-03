"""Measurement output goes to CapTest/Results/<board id>/ and is never tracked by git."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from captest import settings

ROOT = Path(__file__).resolve().parents[2]


def test_default_results_folder_is_per_board(monkeypatch):
    monkeypatch.delenv("CAPTEST_RESULTS_DIR", raising=False)
    path = settings.results_dir("TestBoard_ADC124")
    assert path == ROOT / "CapTest" / "Results" / "TestBoard_ADC124"
    assert settings.resolve_output("run.json", "TestBoard_7953") == ROOT / "CapTest" / "Results" / "TestBoard_7953" / "run.json"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_results_are_git_ignored():
    for name in ("run.json", "run.csv", "run_raw.json"):
        probe = f"CapTest/Results/TestBoard_7953/{name}"
        assert subprocess.run(["git", "check-ignore", "-q", probe], cwd=ROOT).returncode == 0, probe
