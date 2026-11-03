"""Optional: extracted C_total versus the QSPICE TestBoard_7953 model (skipped without QSPICE)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import validate_qspice  # noqa: E402


@pytest.mark.skipif(not validate_qspice.qspice_available(), reason="QSPICE64.exe not installed")
def test_extraction_matches_qspice_model():
    assert validate_qspice.main(["--samples", "400"]) == 0
