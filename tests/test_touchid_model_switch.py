"""Switching the TouchID model (as the GUI's type, version and checkpoint combos do) must load EVERY model discovery
offers, starting from the default model: a family's helper files must follow the selection."""

import pytest

from core.texture_piezo.application.inference_config import InferenceConfig, set_model_version
from core.texture_piezo.models import model_discovery
from core.texture_piezo.models.classifier import TextureClassifier

LOADABLE_MODELS = [
    found.artifacts for model_type in model_discovery.family_keys() for found in model_discovery.loadable(model_type)
]


def _model_id(artifacts):
    return f"{artifacts.model_type}-{artifacts.version}-{artifacts.checkpoint}"


def test_discovery_offers_a_model_of_every_family_the_catalog_lists():
    assert {artifacts.model_type for artifacts in LOADABLE_MODELS} == set(model_discovery.family_keys())


@pytest.mark.parametrize("model_type", model_discovery.family_keys())
def test_switching_the_type_from_the_default_model_loads_that_family(model_type):
    config = InferenceConfig()
    config.model_type = model_type
    classifier = TextureClassifier(config)
    assert classifier.model_type == model_type
    assert classifier.required_channels


@pytest.mark.parametrize("artifacts", LOADABLE_MODELS, ids=_model_id)
def test_every_discovered_model_can_be_selected_and_loaded(artifacts):
    config = InferenceConfig()
    config.model_type = artifacts.model_type
    set_model_version(config, artifacts.version, artifacts.checkpoint)
    classifier = TextureClassifier(config)
    assert classifier.model_type == artifacts.model_type
    assert classifier.class_names == tuple(artifacts.class_names) and classifier.required_channels
