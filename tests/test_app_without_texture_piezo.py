"""The app must construct and run without a texture_piezo checkout: only TouchID model use needs it."""

import pytest

from core.texture_piezo.application.inference_config import InferenceConfig, default_model_type
from core.texture_piezo.models import model_discovery
from inference import texture_piezo_adapter


def _clear_discovery_caches() -> None:
    model_discovery._catalog_by_key.cache_clear()
    model_discovery._defaults.cache_clear()


def _raise_unavailable(*_args, **_kwargs):
    raise texture_piezo_adapter.TexturePiezoUnavailableError("texture_piezo not found")


@pytest.fixture
def texture_piezo_unavailable():
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(texture_piezo_adapter, "model_catalog", _raise_unavailable)
        patch.setattr(texture_piezo_adapter, "read_default_models", _raise_unavailable)
        _clear_discovery_caches()
        yield
    _clear_discovery_caches()


def test_default_model_type_is_empty_when_texture_piezo_is_unavailable(texture_piezo_unavailable):
    assert default_model_type() == ""


def test_inference_config_constructs_when_texture_piezo_is_unavailable(texture_piezo_unavailable):
    assert InferenceConfig().model_type == ""
