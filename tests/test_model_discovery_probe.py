"""Discovery treats only expected load failures as "not offerable"; programming errors surface."""

import logging
import pickle
from pathlib import Path
from unittest.mock import patch

import pytest

from inference import texture_piezo_adapter
from core.texture_piezo.models import model_discovery
from core.texture_piezo.models.model_discovery import PROBE_LOG_EVENT, ModelArtifacts

FAMILY = "penta"


def artifacts_for_probe() -> ModelArtifacts:
    return ModelArtifacts(model_type=FAMILY, version="v1", checkpoint="default", checkpoint_path=Path("m.pt"),
                          model_version="v1", class_names=("a", "b"), bundle_id="20260101T000000Z-00000000",
                          checkpoint_tag="default", label="penta v1 / default")


@pytest.fixture(autouse=True)
def runtime_artifacts_stub():
    with patch.object(model_discovery.texture_piezo_adapter, "runtime_artifacts", lambda artifacts: artifacts):
        yield


def probe_raising(exc: Exception):
    with patch.object(model_discovery.texture_piezo_adapter, "load_runtime", side_effect=exc):
        return model_discovery._probe(artifacts_for_probe())


@pytest.mark.parametrize("failure", [
    ValueError("bad bundle"), OSError("locked"), RuntimeError("state dict mismatch"), EOFError("truncated"),
    pickle.UnpicklingError("not a pickle"),
])
def test_an_expected_load_failure_makes_the_checkpoint_not_offerable(failure):
    assert probe_raising(failure).startswith(type(failure).__name__)


@pytest.mark.parametrize("bug", [
    AttributeError("'NoneType' object has no attribute 'x'"), ImportError("no module"),
    NameError("undefined"), TypeError("wrong arguments"),
])
def test_a_programming_error_inside_a_runtime_load_is_not_hidden_as_skip(bug):
    with pytest.raises(type(bug)):
        probe_raising(bug)


@pytest.mark.parametrize("failure", [NotImplementedError("runtime missing"),
                                     texture_piezo_adapter.TexturePiezoUnavailableError("no checkout")])
def test_a_broken_environment_is_not_hidden_as_skip(failure):
    with pytest.raises(type(failure)):
        probe_raising(failure)


def test_each_probe_emits_one_structured_event(caplog):
    with caplog.at_level(logging.INFO, logger=model_discovery.logger.name):
        probe_raising(ValueError("bad bundle"))
    record = next(r for r in caplog.records if r.getMessage() == PROBE_LOG_EVENT)
    assert (record.family, record.version, record.checkpoint) == (FAMILY, "v1", "default")
    assert record.outcome == model_discovery.PROBE_SKIPPED and "bad bundle" in record.reason


def test_without_texture_piezo_the_catalog_is_empty_and_the_config_still_builds(caplog):
    from core.texture_piezo.application.inference_config import InferenceConfig

    unavailable = model_discovery.texture_piezo_adapter.TexturePiezoUnavailableError("texture_piezo not found")
    model_discovery.refresh()
    try:
        with patch.object(model_discovery.texture_piezo_adapter, "model_catalog", side_effect=unavailable),                 caplog.at_level(logging.WARNING, logger=model_discovery.logger.name):
            config = InferenceConfig()
            assert model_discovery.family_keys() == () and model_discovery.discover(FAMILY) == ()
        assert config.selections == {} and config.model_type == "" and config.class_names == []
        assert any(r.getMessage() == model_discovery.CATALOG_UNAVAILABLE_LOG_EVENT for r in caplog.records)
    finally:
        model_discovery.refresh()
