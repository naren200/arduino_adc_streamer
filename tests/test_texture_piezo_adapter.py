"""inference/texture_piezo_adapter.py: the one door to texture_piezo's model runtimes.

Activation is checked in a fresh interpreter (it edits sys.path), the failure paths against
throw-away fake checkouts so no real sys.path is touched."""

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from inference import texture_piezo_adapter as adapter
from inference._paths import TEXTURE_PIEZO_ROOT

REPO_ROOT = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT_S = 120
REAL_TEXTURE_PIEZO = adapter.source_dir(TEXTURE_PIEZO_ROOT) / "touchid_inference" / "runtime" / "__init__.py"
needs_texture_piezo = pytest.mark.skipif(not REAL_TEXTURE_PIEZO.is_file(), reason="texture_piezo checkout not found")


def make_fake_checkout(root: Path, app_root_setting: str | None, extra_modules=()) -> Path:
    runtime = root / "src" / "touchid_inference" / "runtime"
    runtime.mkdir(parents=True)
    (root / "src" / "touchid_inference" / "__init__.py").write_text("")
    (runtime / "__init__.py").write_text("raise RuntimeError('must never be imported by a failed check')\n")
    for name in extra_modules:
        (root / "src" / f"{name}.py").write_text("")
    (root / "configs").mkdir()
    paths = f"  arduino_adc_streamer_root: {app_root_setting}\n" if app_root_setting is not None else ""
    (root / "configs" / "config.yaml").write_text(f"paths:\n{paths}" if paths else "other: 1\n")
    return root


def run_child(code: str) -> str:
    completed = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True,
                               timeout=CHILD_TIMEOUT_S, check=True)
    return completed.stdout.strip().splitlines()[-1]


@needs_texture_piezo
def test_activation_appends_only_texture_piezo_src_and_every_generic_name_resolves_there():
    code = (
        "import json, sys\n"
        "from inference import texture_piezo_adapter as a\n"
        "before = list(sys.path)\n"
        "a.runtime_api()\n"
        "added = [p for p in sys.path if p not in before]\n"
        "at_end = sys.path[-len(added):] == added\n"
        "import engine_adapter\n"
        "after = [p for p in sys.path if p not in before and p not in added]\n"
        "origins = {n: str(a._origin_of(n)) for n in a.GENERIC_MODULE_NAMES}\n"
        "print(json.dumps({'added': added, 'at_end': at_end, 'after': after, 'origins': origins,\n"
        "  'engine_root': str(engine_adapter.ENGINE_ROOT),\n"
        "  'torch_loaded': 'torch' in sys.modules}))"
    )
    report = json.loads(run_child(code))
    src = str(adapter.source_dir(TEXTURE_PIEZO_ROOT))
    assert report["added"] == [src] and report["at_end"]
    assert all(Path(origin).is_relative_to(src) for origin in report["origins"].values()), report["origins"]
    assert Path(report["engine_root"]) == adapter.APP_ROOT
    assert [Path(p) for p in report["after"]] in ([], [adapter.APP_ROOT])  # TP's engine_adapter may re-add THIS checkout
    assert not report["torch_loaded"]


def test_a_missing_checkout_fails_with_a_message_naming_the_env_var(tmp_path):
    with pytest.raises(adapter.TexturePiezoUnavailableError, match="TEXTURE_PIEZO_ROOT"):
        adapter.require_checkout(tmp_path / "nowhere")


def test_texture_piezo_configured_for_a_sibling_app_checkout_is_refused_before_anything_is_imported(tmp_path):
    sibling = tmp_path / "arduino_adc_streamer_copy"
    sibling.mkdir()
    root = make_fake_checkout(tmp_path / "texture_piezo", str(sibling))
    path_before, modules_before = list(sys.path), set(sys.modules)
    with pytest.raises(adapter.EngineRootMismatchError, match="point it at this checkout"):
        adapter._activate.__wrapped__(root)
    assert sys.path == path_before
    assert not {name for name in set(sys.modules) - modules_before if name.startswith("touchid_inference")}


def test_an_app_root_setting_that_names_this_checkout_is_accepted(tmp_path):
    root = make_fake_checkout(tmp_path / "texture_piezo", adapter.APP_ROOT.as_posix())
    adapter.require_same_app_root(root)


def test_an_unset_app_root_setting_is_refused(tmp_path):
    root = make_fake_checkout(tmp_path / "texture_piezo", None)
    with pytest.raises(adapter.EngineRootMismatchError):
        adapter.require_same_app_root(root)


def test_a_top_level_name_defined_by_both_repositories_is_refused(tmp_path):
    root = make_fake_checkout(tmp_path / "texture_piezo", None, extra_modules=("inference",))
    with pytest.raises(adapter.ModuleCollisionError, match="inference"):
        adapter.require_disjoint_names(root)


def test_the_real_texture_piezo_and_this_app_share_no_top_level_name():
    if not REAL_TEXTURE_PIEZO.is_file():
        pytest.skip("texture_piezo checkout not found")
    adapter.require_disjoint_names(TEXTURE_PIEZO_ROOT)


def test_a_generic_name_that_resolves_outside_texture_piezo_src_is_refused(tmp_path):
    root = make_fake_checkout(tmp_path / "texture_piezo", None)
    with pytest.raises(adapter.ModuleCollisionError, match="json"):
        adapter.require_generic_names_resolve_under(root, ("json",))


def test_the_guard_list_covers_the_modules_texture_piezo_runtimes_import():
    required = {"data", "model", "train", "evaluate", "datasets", "artifacts", "touchid_inference",
                "engine_adapter", "window_layout", "chunk_features", "chunk_bundle"}
    assert required <= set(adapter.GENERIC_MODULE_NAMES)


@needs_texture_piezo
def test_runtime_artifacts_carry_the_family_and_the_bundle_path():
    from core.texture_piezo.models.model_discovery import ModelArtifacts

    artifacts = ModelArtifacts(
        model_type="ann", version="v3", checkpoint="best", checkpoint_path=Path("a.pt"), model_version="v1",
        class_names=("a", "b"), bundle_id="20260101T000000Z-00000000", checkpoint_tag="best", label="ann v3 / best")
    converted = adapter.runtime_artifacts(artifacts)
    assert (converted.model_type, converted.checkpoint_path) == ("ann", Path("a.pt"))


@needs_texture_piezo
def test_a_state_dict_file_is_reported_as_not_a_bundle(tmp_path):
    import torch

    path = tmp_path / "ann_v3.bundle.pt"
    torch.save({"net.0.weight": torch.zeros(2, 2)}, path)
    with pytest.raises(adapter.NotABundleError, match="not a model bundle"):
        adapter.read_model_header(path)


@needs_texture_piezo
def test_the_real_texture_piezo_offers_a_catalog_of_plain_family_entries():
    catalog = adapter.model_catalog()
    assert catalog
    keys = [entry.key for entry in catalog]
    assert len(set(keys)) == len(keys)
    assert all(entry.display_name and entry.versions for entry in catalog)


def test_a_texture_piezo_without_model_catalog_is_refused_with_a_clear_message(monkeypatch):
    outdated_runtime = types.SimpleNamespace()
    monkeypatch.setattr(adapter, "runtime_api", lambda root=TEXTURE_PIEZO_ROOT: outdated_runtime)
    with pytest.raises(adapter.TexturePiezoUnavailableError, match="too old: missing model_catalog; update texture_piezo"):
        adapter.model_catalog()
