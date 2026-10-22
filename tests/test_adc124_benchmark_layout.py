"""Relocated tools resolve firmware/data roots without touching the board."""
import importlib
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
BENCHMARKS = REPO / 'Arduino_Sketches/TestBoard_ADC124/benchmarks'


def test_relocated_tools_share_repository_and_firmware_roots(monkeypatch):
    monkeypatch.syspath_prepend(str(BENCHMARKS))
    for name in ('capture_adc124_settling', 'benchmark_adc124_scan_modes', 'analyze_adc124_scan_modes', 'run_adc124_mux_profiles'):
        module = importlib.import_module(name)
        assert module.ROOT == REPO
        assert Path(module.__file__).parent == BENCHMARKS
    assert importlib.import_module('benchmark_adc124_scan_modes').FW == BENCHMARKS.parent


def test_quick_dry_run_never_opens_serial_or_creates_output(monkeypatch, tmp_path, capsys):
    monkeypatch.syspath_prepend(str(BENCHMARKS))
    module = importlib.import_module('benchmark_adc124_lookahead_quick')
    def forbidden(*args, **kwargs):
        raise AssertionError('Dry-run touched hardware or snapshots')
    monkeypatch.setattr(module, 'board_status', forbidden)
    monkeypatch.setattr(module, 'upload_image', forbidden)
    monkeypatch.setattr(module, 'snapshot', forbidden)
    output = tmp_path/'capture'
    assert module.main([str(output), '--baseline', '--dry-run']) == 0
    assert not output.exists()
    assert '28 new settings; 24 baseline settings' in capsys.readouterr().out


def test_quick_cli_runs_outside_repository_without_scratch_files(tmp_path):
    result = subprocess.run([sys.executable, str(BENCHMARKS/'benchmark_adc124_lookahead_quick.py'),
                             '--dry-run'], cwd=tmp_path, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '28 new settings; 0 baseline settings' in result.stdout
    assert not list(tmp_path.iterdir())
    result = subprocess.run([sys.executable, str(BENCHMARKS/'analyze_adc124_lookahead_quick.py'),
                             '--help'], cwd=tmp_path, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Completed quick-benchmark raw root' in result.stdout
