"""A saved TouchID model selection that no longer resolves must fall back to the default
model, visibly, instead of raising at startup."""

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from core.texture_piezo.application import inference_config
from core.texture_piezo.application.inference_config import (
    MODEL_FALLBACK_LOG_EVENT, InferenceConfig, load_touchid_settings, model_checkpoint_of, model_version_of,
)
from core.texture_piezo.models.model_discovery import DiscoveredModel, ModelArtifacts

DEFAULT_PENTA = ("v1", "default")
CHUNK_VERSION = "v3"
CHUNK_TAG = "scales20_25"
UNKNOWN = "does_not_exist"


def _artifacts(model_type, version, checkpoint):
    return ModelArtifacts(
        model_type=model_type, version=version, checkpoint=checkpoint,
        checkpoint_path=Path(f"{model_type}_{version}_{checkpoint}.pt"),
        model_version="v1", class_names=("a", "b"), bundle_id="20260101T000000Z-00000000", checkpoint_tag=checkpoint,
        label=f"{model_type} {version} / {checkpoint}",
    )


FAKE_MODELS = {
    "penta": (DiscoveredModel(_artifacts("penta", *DEFAULT_PENTA), None),),
    "chunk": (DiscoveredModel(_artifacts("chunk", CHUNK_VERSION, CHUNK_TAG), None),),
}


@pytest.fixture(autouse=True)
def fake_discovery_and_settings_file(tmp_path):
    settings_path = tmp_path / "settings.json"
    defaults_path = tmp_path / "default_models.json"
    defaults_path.write_text(json.dumps({"app_family": "penta", "models": {
        "penta": {"weights_version": DEFAULT_PENTA[0], "checkpoint_tag": DEFAULT_PENTA[1]}}}), encoding="utf-8")
    inference_config.model_discovery.refresh()  # resolved defaults are cached: drop those real discovery produced
    with patch.object(inference_config.model_discovery, "DEFAULT_MODELS_PATH", defaults_path), \
            patch.object(inference_config.model_discovery, "discover", lambda model_type: FAKE_MODELS.get(model_type, ())), \
            patch.object(inference_config, "_get_last_touchid_settings_path", return_value=settings_path):
        yield settings_path
    inference_config.model_discovery.refresh()


def load_with_saved(settings_path, **saved):
    settings_path.write_text(json.dumps({"version": 1, "touchid_settings": saved}), encoding="utf-8")
    return load_touchid_settings(InferenceConfig())


def assert_default_penta(config):
    assert config.model_type == "penta"
    assert (model_version_of(config), model_checkpoint_of(config)) == DEFAULT_PENTA


def test_valid_selection_is_restored_without_a_fallback(fake_discovery_and_settings_file, caplog):
    with caplog.at_level(logging.WARNING):
        config = load_with_saved(fake_discovery_and_settings_file, model_type="chunk",
                                 model_version=CHUNK_VERSION, model_checkpoint=CHUNK_TAG)
    assert config.model_type == "chunk"
    assert (model_version_of(config), model_checkpoint_of(config)) == (CHUNK_VERSION, CHUNK_TAG)
    assert config.model_fallback_message == ""
    assert not caplog.records


def test_unknown_type_keeps_the_default_type_and_reports(fake_discovery_and_settings_file, caplog):
    with caplog.at_level(logging.WARNING):
        config = load_with_saved(fake_discovery_and_settings_file, model_type=UNKNOWN,
                                 model_version="v1", model_checkpoint="default")
    assert_default_penta(config)
    assert UNKNOWN in config.model_fallback_message
    assert caplog.records[0].saved_type == UNKNOWN


def test_unknown_version_keeps_the_default_model_of_the_saved_type(fake_discovery_and_settings_file, caplog):
    with caplog.at_level(logging.WARNING):
        config = load_with_saved(fake_discovery_and_settings_file, model_type="chunk",
                                 model_version="v99", model_checkpoint=CHUNK_TAG)
    assert config.model_type == "chunk"
    assert (model_version_of(config), model_checkpoint_of(config)) == (CHUNK_VERSION, CHUNK_TAG)
    assert "v99" in config.model_fallback_message
    record = caplog.records[0]
    assert (record.getMessage(), record.levelno) == (MODEL_FALLBACK_LOG_EVENT, logging.WARNING)
    assert (record.saved_version, record.fallback_version) == ("v99", CHUNK_VERSION)


def test_unknown_checkpoint_tag_keeps_the_default_model_and_reports(fake_discovery_and_settings_file, caplog):
    with caplog.at_level(logging.WARNING):
        config = load_with_saved(fake_discovery_and_settings_file, model_type="chunk",
                                 model_version=CHUNK_VERSION, model_checkpoint=UNKNOWN)
    assert (model_version_of(config), model_checkpoint_of(config)) == (CHUNK_VERSION, CHUNK_TAG)
    assert UNKNOWN in config.model_fallback_message
    assert caplog.records[0].saved_checkpoint == UNKNOWN


def test_an_unreadable_settings_file_is_logged_and_leaves_the_defaults(fake_discovery_and_settings_file, caplog):
    from gui.inference_panel import SETTINGS_UNREADABLE_LOG_EVENT, InferencePanelMixin

    fake_discovery_and_settings_file.write_text("{not json", encoding="utf-8")
    config = InferenceConfig()
    with caplog.at_level(logging.WARNING):
        assert InferencePanelMixin._touchid_config_with_saved_settings(config) is config
    assert any(r.getMessage() == SETTINGS_UNREADABLE_LOG_EVENT for r in caplog.records)


def test_an_unexpected_error_while_loading_settings_propagates(fake_discovery_and_settings_file):
    from gui.inference_panel import InferencePanelMixin

    fake_discovery_and_settings_file.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(AttributeError):
        InferencePanelMixin._touchid_config_with_saved_settings(InferenceConfig())
