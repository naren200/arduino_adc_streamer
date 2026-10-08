"""Models are found by READING FILE HEADERS: the file name, suffix and folder play no part; EVERY distinct bundle is
listed (only a byte-identical copy collapses); a non-bundle is reported, not offered; class names come from the
bundle; the defaults are tracked data (configs/default_models.json) with a deterministic, logged fallback to the
newest loadable bundle."""

import json
import logging
import shutil
from datetime import datetime, timezone

import pytest
import torch

from core.texture_piezo.application.inference_config import InferenceConfig, ModelSelection
from core.texture_piezo.models import model_discovery
from core.texture_piezo.models.classifier import TextureClassifier
from inference import texture_piezo_adapter
from inference._paths import TEXTURE_PIEZO_MODELS

REAL_BUNDLES = TEXTURE_PIEZO_MODELS / "_bundles"
DEFAULTS_FILE = "default_models.json"
OLD, NEW = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc)
PENTA_V1, ANN_V3, ANN_V6, CHUNK = ("penta", "v1", "default"), ("ann", "v3", "default"), ("ann", "v6", "m1"), ("chunk", "v1", "rerun")


def _newest_real_bundle(family, version, tag):
    """The real bundle with this header identity (the files' names are not an interface)."""
    found = []
    for path in REAL_BUNDLES.glob("*.pt") if REAL_BUNDLES.is_dir() else ():
        try:
            header = texture_piezo_adapter.read_model_header(path)
        except (ValueError, OSError):
            continue
        if (header.model_family, header.weights_version, header.checkpoint_tag) == (family, version, tag):
            found.append((header.bundle_id, path))
    return max(found)[1] if found else None


pytestmark = pytest.mark.skipif(_newest_real_bundle(*PENTA_V1) is None, reason="texture_piezo bundles not available")


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    """An empty stand-in for texture_piezo's models/ (with its _bundles/ folder); nothing is scanned elsewhere."""
    root = tmp_path / "models"
    (root / "_bundles").mkdir(parents=True)
    monkeypatch.setattr(model_discovery, "MODEL_DIRS", (root, root / "_bundles"))
    monkeypatch.setattr(model_discovery, "DEFAULT_MODELS_PATH", root / DEFAULTS_FILE)
    model_discovery.refresh()
    yield root
    monkeypatch.undo()
    model_discovery.refresh()


def copy_bundle(identity, destination):
    shutil.copy(_newest_real_bundle(*identity), destination)
    return destination


def restamped_copy(identity, destination, *, created_at, tag=None, scale_weights_by=None):
    """A copy of a real bundle with a new creation time (and optionally another tag and retrained weights)."""
    from bundle_identity import make_bundle_id

    payload = torch.load(_newest_real_bundle(*identity), weights_only=True)
    if scale_weights_by is not None:
        payload["model_state"] = {name: tensor * scale_weights_by if tensor.is_floating_point() else tensor
                                  for name, tensor in payload["model_state"].items()}
    payload["bundle_id"] = make_bundle_id(payload["model_state"], created_at)
    payload["checkpoint_tag"] = tag or payload["checkpoint_tag"]
    torch.save(payload, destination)
    return payload["bundle_id"]


def write_defaults(root, app_family, **pins):
    record = {"app_family": app_family,
              "models": {family: {"weights_version": version, "checkpoint_tag": tag} for family, (version, tag) in pins.items()}}
    (root / DEFAULTS_FILE).write_text(json.dumps(record), encoding="utf-8")


def keys(model_type):
    return [(found.artifacts.version, found.artifacts.checkpoint) for found in model_discovery.discover(model_type)]


def test_a_bundle_is_identified_by_its_content_whatever_it_is_renamed_to(models_dir):
    copy_bundle(PENTA_V1, models_dir / "zz_whatever.pt")
    (found,) = model_discovery.discover("penta")
    assert found.is_loadable and found.artifacts.checkpoint_path.name == "zz_whatever.pt"
    assert (found.artifacts.model_type, found.artifacts.version, found.artifacts.checkpoint) == ("penta", "v1", "default")
    assert found.artifacts.model_version == "v1" and found.artifacts.bundle_id and "penta v1 / default" in found.artifacts.label


def test_a_bundle_is_found_in_either_scanned_folder_and_never_in_a_subfolder(models_dir):
    copy_bundle(PENTA_V1, models_dir / "_bundles" / "a.pt")
    (models_dir / "_archive").mkdir()
    copy_bundle(ANN_V3, models_dir / "_archive" / "b.pt")
    assert keys("penta") == [("v1", "default")] and keys("ann") == []


def test_a_misnamed_non_bundle_is_skipped_with_the_reexport_reason(models_dir):
    torch.save({"net.0.weight": torch.zeros(2, 2)}, models_dir / "ann_v9.bundle.pt")
    copy_bundle(PENTA_V1, models_dir / "ann_v3.pt")
    assert keys("ann") == [] and keys("penta") == [("v1", "default")]
    (skipped,) = model_discovery.skipped_files()
    assert skipped.path.name == "ann_v9.bundle.pt" and skipped.reason == "not a model bundle; re-export from its notebook"


def test_a_bundle_whose_weights_do_not_fit_its_declared_code_is_listed_but_not_offered(models_dir):
    from bundle_identity import make_bundle_id

    payload = torch.load(_newest_real_bundle(*PENTA_V1), weights_only=True)
    payload["model_state"] = {name: tensor[:1] for name, tensor in payload["model_state"].items() if tensor.ndim}
    payload["bundle_id"] = make_bundle_id(payload["model_state"])
    torch.save(payload, models_dir / "broken.pt")
    (found,) = model_discovery.discover("penta")
    assert not found.is_loadable and found.error
    assert model_discovery.loadable("penta") == []


def test_a_byte_identical_copy_is_collapsed_and_reported_as_a_duplicate(models_dir):
    copy_bundle(PENTA_V1, models_dir / "a.pt")
    copy_bundle(PENTA_V1, models_dir / "_bundles" / "b.pt")
    assert [found.artifacts.checkpoint_path.name for found in model_discovery.discover("penta")] == ["a.pt"]
    (skipped,) = model_discovery.skipped_files()
    assert skipped.reason == "duplicate of a.pt"


def test_retrained_weights_under_the_same_labels_are_both_listed_and_both_selectable(models_dir):
    older_id = restamped_copy(ANN_V3, models_dir / "old.pt", created_at=OLD)
    newer_id = restamped_copy(ANN_V3, models_dir / "new.pt", created_at=NEW, scale_weights_by=1.01)
    found = model_discovery.discover("ann")
    assert len(found) == 2 and all(model.is_loadable for model in found) and model_discovery.skipped_files() == ()
    assert {model.artifacts.bundle_id for model in found} == {older_id, newer_id}
    assert model_discovery.artifacts_for("ann", "v3", "default").bundle_id == newer_id  # the plain tag is the latest
    assert model_discovery.artifacts_for("ann", "v3", f"default@{older_id}").bundle_id == older_id
    assert model_discovery.default_for_family("ann").bundle_id == newer_id


def test_two_chunk_exports_with_different_weights_both_appear(models_dir):
    restamped_copy(CHUNK, models_dir / "a.pt", created_at=OLD)
    restamped_copy(CHUNK, models_dir / "b.pt", created_at=NEW, scale_weights_by=1.01)
    assert len(model_discovery.discover("chunk")) == 2 and model_discovery.skipped_files() == ()


def test_the_same_weights_written_at_the_same_moment_collapse_even_when_a_label_differs(models_dir):
    restamped_copy(ANN_V3, models_dir / "a.pt", created_at=OLD)
    restamped_copy(ANN_V3, models_dir / "b.pt", created_at=OLD, tag="other")
    assert len(model_discovery.discover("ann")) == 1
    (skipped,) = model_discovery.skipped_files()
    assert skipped.reason == "duplicate of a.pt"


def test_class_names_come_from_the_bundle_in_the_order_it_stores(models_dir):
    payload = torch.load(_newest_real_bundle(*ANN_V3), weights_only=True)
    permuted = list(reversed(payload["class_names"]))
    torch.save({**payload, "class_names": permuted}, models_dir / "permuted.pt")
    (found,) = model_discovery.discover("ann")
    assert found.is_loadable and found.artifacts.class_names == tuple(permuted)

    config = InferenceConfig(model_type="ann", selections={"ann": ModelSelection("v3", "default")})
    assert config.class_names == permuted
    classifier = TextureClassifier(config)
    assert classifier.class_names == tuple(permuted)


def test_without_a_selected_model_there_are_no_class_names(models_dir):
    assert InferenceConfig(model_type="ann", selections={}).class_names == []


def test_the_defaults_file_pins_each_family_and_names_the_app_family(models_dir):
    restamped_copy(ANN_V3, models_dir / "a3.pt", created_at=OLD)
    restamped_copy(ANN_V6, models_dir / "a6.pt", created_at=NEW)
    copy_bundle(PENTA_V1, models_dir / "p.pt")
    write_defaults(models_dir, "penta", penta=("v1", "default"), ann=("v3", "default"))
    default = model_discovery.default_model()
    assert not default.is_fallback and default.reason is None and default.artifacts.checkpoint_path.name == "p.pt"
    assert model_discovery.default_for_family("ann").checkpoint_path.name == "a3.pt"  # pinned, though v6 is newer
    assert InferenceConfig().model_type == "penta"
    assert InferenceConfig().selections["ann"] == ModelSelection("v3", "default")


def test_an_unpinned_family_starts_on_its_newest_export_without_a_warning(models_dir, caplog):
    restamped_copy(CHUNK, models_dir / "old.pt", created_at=OLD)
    newer_id = restamped_copy(CHUNK, models_dir / "new.pt", created_at=NEW, tag="rerun2", scale_weights_by=1.01)
    copy_bundle(PENTA_V1, models_dir / "p.pt")
    write_defaults(models_dir, "penta", penta=("v1", "default"))
    with caplog.at_level(logging.WARNING, logger=model_discovery.logger.name):
        assert model_discovery.default_for_family("chunk").bundle_id == newer_id
    assert not caplog.records


@pytest.mark.parametrize("defaults", [
    None,
    "{not json",
    json.dumps({"app_family": "penta"}),
])
def test_an_absent_or_unusable_defaults_file_falls_back_to_the_newest_loadable_and_warns_once(models_dir, caplog, defaults):
    copy_bundle(PENTA_V1, models_dir / "p.pt")
    restamped_copy(ANN_V6, models_dir / "a6.pt", created_at=NEW)
    restamped_copy(ANN_V3, models_dir / "a3.pt", created_at=OLD)
    if defaults is not None:
        (models_dir / DEFAULTS_FILE).write_text(defaults, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=model_discovery.logger.name):
        default = model_discovery.default_model()
        model_discovery.default_model()
        model_discovery.default_for_family("ann")
    # the app family is unknown: the first catalog family with a loadable model (ann), its NEWEST bundle (v6, by timestamp)
    assert default.is_fallback and default.reason
    assert (default.artifacts.model_type, default.artifacts.version, default.artifacts.checkpoint) == ("ann", "v6", "m1")
    warnings = [record for record in caplog.records if record.getMessage() == model_discovery.DEFAULT_FALLBACK_LOG_EVENT]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING and warnings[0].reason == default.reason


def test_a_pin_that_names_no_loadable_bundle_falls_back_to_that_familys_newest_and_warns(models_dir, caplog):
    restamped_copy(ANN_V3, models_dir / "a3.pt", created_at=OLD)
    restamped_copy(ANN_V6, models_dir / "a6.pt", created_at=NEW)
    copy_bundle(PENTA_V1, models_dir / "p.pt")
    write_defaults(models_dir, "penta", penta=("v1", "default"), ann=("v3", "no_such_tag"))
    with caplog.at_level(logging.WARNING, logger=model_discovery.logger.name):
        fallback = model_discovery.default_for_family("ann")
    assert (fallback.version, fallback.checkpoint) == ("v6", "m1")
    (warning,) = [record for record in caplog.records if record.getMessage() == model_discovery.DEFAULT_FALLBACK_LOG_EVENT]
    assert warning.family == "ann" and "ann/v3/no_such_tag" in warning.reason
    assert not model_discovery.default_model().is_fallback  # the app family itself resolved


def test_an_app_family_with_nothing_loadable_opens_on_the_first_family_that_has_a_model(models_dir, caplog):
    restamped_copy(ANN_V3, models_dir / "a3.pt", created_at=OLD)
    write_defaults(models_dir, "penta", penta=("v1", "default"), ann=("v3", "default"))
    with caplog.at_level(logging.WARNING, logger=model_discovery.logger.name):
        default = model_discovery.default_model()
    assert default.is_fallback and default.artifacts.model_type == "ann" and "penta" in default.reason


def test_a_family_pinned_in_the_file_still_seeds_every_selection_without_a_warning(models_dir, caplog):
    copy_bundle(PENTA_V1, models_dir / "p.pt")
    restamped_copy(ANN_V6, models_dir / "a6.pt", created_at=NEW)
    restamped_copy(ANN_V3, models_dir / "a3.pt", created_at=OLD)
    write_defaults(models_dir, "penta", penta=("v1", "default"), ann=("v3", "default"))
    with caplog.at_level(logging.WARNING, logger=model_discovery.logger.name):
        selections = InferenceConfig().selections
    assert selections["ann"] == ModelSelection("v3", "default") and selections["penta"] == ModelSelection("v1", "default")
    assert not caplog.records


def test_the_fallback_with_nothing_loadable_is_reported_not_raised(models_dir, caplog):
    with caplog.at_level(logging.WARNING, logger=model_discovery.logger.name):
        default = model_discovery.default_model()
    assert default.artifacts is None and default.is_fallback
    assert any(record.getMessage() == model_discovery.DEFAULT_FALLBACK_LOG_EVENT for record in caplog.records)
