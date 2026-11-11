"""Hermetic test settings: no test reads or writes the user's real settings or ~/.adc_streamer diagnostics."""

from unittest.mock import patch

import pytest

from core.texture_piezo.application import inference_config

SETTINGS_FILE_NAME = "last_used_touchid_settings.json"


@pytest.fixture(autouse=True)
def isolated_touchid_settings_file(tmp_path):
    with patch.object(inference_config, "_get_last_touchid_settings_path", return_value=tmp_path / SETTINGS_FILE_NAME):
        yield


@pytest.fixture(autouse=True)
def isolated_stall_dump_path(tmp_path_factory, monkeypatch):
    import data_processing.stall_watchdog as stall_watchdog
    target = tmp_path_factory.getbasetemp() / 'stall_dumps' / 'stalls.log'
    monkeypatch.setattr(stall_watchdog, 'dump_path', lambda: target)
