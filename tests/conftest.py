"""Keep diagnostics written by tests out of the real ~/.adc_streamer directory."""
import pytest


@pytest.fixture(autouse=True)
def isolated_stall_dump_path(tmp_path_factory, monkeypatch):
    import data_processing.stall_watchdog as stall_watchdog
    target = tmp_path_factory.getbasetemp() / 'stall_dumps' / 'stalls.log'
    monkeypatch.setattr(stall_watchdog, 'dump_path', lambda: target)
