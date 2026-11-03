"""Optional: extracted C_total versus the QSPICE TestBoard_ADC124 model (skipped without QSPICE)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import validate_qspice_adc124  # noqa: E402


@pytest.mark.skipif(not validate_qspice_adc124.qspice_available(), reason="QSPICE64.exe not installed")
def test_extraction_matches_the_adc124_qspice_model():
    assert validate_qspice_adc124.main(["--words", "420"]) == 0
