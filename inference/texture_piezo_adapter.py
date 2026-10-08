"""The ONLY module of this repository that makes texture_piezo importable.

texture_piezo owns every model, every feature and every model runtime; this app
owns gating, windowing, segmentation, buffering, discovery and the GUI. The two
meet in ``touchid_inference.runtime``: the app hands it a ``LiveWindow`` of named
engine channels and gets class probabilities back.

Nothing here runs at import time. The first call that needs texture_piezo
(``load_runtime`` / ``runtime_api``) resolves the
checkout, runs every check below, and only then appends ``<root>/src`` to
``sys.path`` (appended, so this repo's own top-level names always win a clash):

1. the checkout exists and contains ``touchid_inference.runtime``;
2. texture_piezo's own ``paths.arduino_adc_streamer_root`` names THIS checkout.
   texture_piezo's ``engine_adapter`` activates that path when imported and raises
   if ``core`` already resolved elsewhere; checking first turns that into a clear
   message and guarantees no second app root is ever put on ``sys.path``;
3. no top-level module of texture_piezo shares a name with one of this repo, and
   its generic names (``data``, ``model``, ``train``, ...) really resolve under
   ``<root>/src`` -- they are imported lazily by the runtimes, so their origin is
   checked, not their presence in ``sys.modules``.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

import yaml

from inference._paths import TEXTURE_PIEZO_ROOT, TEXTURE_PIEZO_ROOT_ENV_VAR

APP_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR_NAME = "src"
CONFIG_RELATIVE_PATH = Path("configs") / "config.yaml"
APP_ROOT_CONFIG_KEY = "arduino_adc_streamer_root"
RUNTIME_PACKAGE = "touchid_inference.runtime"
CATALOG_ATTRIBUTE = "model_catalog"
_RUNTIME_SENTINEL = Path("touchid_inference") / "runtime" / "__init__.py"

# Generic top-level names texture_piezo code imports; each must resolve under <root>/src.
GENERIC_MODULE_NAMES = (
    "data", "model", "train", "evaluate", "datasets", "artifacts", "engine_adapter", "touchid_inference",
    "window_layout", "chunk_features", "chunk_bundle",
)


class TexturePiezoUnavailableError(RuntimeError):
    """texture_piezo cannot be located or does not offer the model runtime package."""


class EngineRootMismatchError(TexturePiezoUnavailableError):
    """texture_piezo is configured to use a different app checkout than the running one."""


class ModuleCollisionError(TexturePiezoUnavailableError):
    """A top-level module name exists in both repositories, or resolves to the wrong place."""


def source_dir(root: Path) -> Path:
    return Path(root) / SOURCE_DIR_NAME


def require_checkout(root: Path) -> None:
    if not (source_dir(root) / _RUNTIME_SENTINEL).is_file():
        raise TexturePiezoUnavailableError(
            f"texture_piezo not found at {root} (no {SOURCE_DIR_NAME}/{_RUNTIME_SENTINEL.as_posix()}); "
            f"set the {TEXTURE_PIEZO_ROOT_ENV_VAR} environment variable to its checkout"
        )


def configured_app_root(root: Path) -> Path | None:
    """The app checkout texture_piezo's own config points at, or None when unset."""
    config = yaml.safe_load((Path(root) / CONFIG_RELATIVE_PATH).read_text(encoding="utf-8")) or {}
    configured = (config.get("paths") or {}).get(APP_ROOT_CONFIG_KEY)
    if not configured:
        return None
    path = Path(configured)
    return (path if path.is_absolute() else Path(root) / path).resolve()


def require_same_app_root(root: Path, app_root: Path = APP_ROOT) -> None:
    configured = configured_app_root(root)
    if configured is None or configured != Path(app_root).resolve():
        raise EngineRootMismatchError(
            f"texture_piezo's configs/config.yaml paths.{APP_ROOT_CONFIG_KEY} is {configured}, but this app runs "
            f"from {Path(app_root).resolve()}; point it at this checkout. Importing the runtime would otherwise "
            "put a second engine checkout on sys.path or fail inside texture_piezo's engine_adapter"
        )


def _is_importable_directory(entry: Path) -> bool:
    """A regular package, or a namespace package (this app's ``inference`` has no __init__)."""
    return entry.is_dir() and ((entry / "__init__.py").is_file() or any(entry.glob("*.py")))


def top_level_names(directory: Path) -> set[str]:
    """Names importable from ``directory``: packages and plain modules at its top level."""
    return {
        entry.stem if entry.is_file() else entry.name
        for entry in Path(directory).iterdir()
        if entry.suffix == ".py" or _is_importable_directory(entry)
    }


def require_disjoint_names(root: Path, app_root: Path = APP_ROOT) -> None:
    shared = sorted(top_level_names(source_dir(root)) & top_level_names(app_root))
    if shared:
        raise ModuleCollisionError(
            f"top-level names defined by both texture_piezo/src and this app: {shared}; one would shadow the other"
        )


def _origin_of(name: str) -> Path | None:
    module = sys.modules.get(name)
    origin = getattr(module, "__file__", None) if module is not None else None
    if origin is None:
        spec = importlib.util.find_spec(name)
        origin = spec.origin if spec is not None else None
    return Path(origin).resolve() if origin else None


def require_generic_names_resolve_under(root: Path, names: Sequence[str] = GENERIC_MODULE_NAMES) -> None:
    src = source_dir(root).resolve()
    wrong = {}
    for name in names:
        origin = _origin_of(name)
        if origin is None or src not in origin.parents:
            wrong[name] = str(origin)
    if wrong:
        raise ModuleCollisionError(f"modules that must come from {src} resolve elsewhere: {wrong}")


def _append_source_dir(root: Path) -> None:
    entry = str(source_dir(root))
    if entry not in sys.path:
        sys.path.append(entry)


@lru_cache(maxsize=None)
def _activate(root: Path):
    """Run every check, put texture_piezo on ``sys.path`` once, return its runtime package."""
    require_checkout(root)
    require_same_app_root(root)
    require_disjoint_names(root)
    _append_source_dir(root)
    runtime = importlib.import_module(RUNTIME_PACKAGE)
    require_generic_names_resolve_under(root)
    return runtime


def runtime_api(root: Path = TEXTURE_PIEZO_ROOT):
    """texture_piezo's ``touchid_inference.runtime`` package (activating it on first use)."""
    return _activate(Path(root))


def model_catalog(root: Path = TEXTURE_PIEZO_ROOT):
    """texture_piezo's ``CatalogEntry`` tuple: every model family with a runtime, as plain data."""
    runtime = runtime_api(root)
    if not hasattr(runtime, CATALOG_ATTRIBUTE):
        raise TexturePiezoUnavailableError(
            f"texture_piezo checkout is too old: missing {CATALOG_ATTRIBUTE}; update texture_piezo"
        )
    return getattr(runtime, CATALOG_ATTRIBUTE)()


class NotABundleError(ValueError):
    """The file's content is not a texture_piezo model bundle (re-export it from its notebook)."""


def read_model_header(path: Path, root: Path = TEXTURE_PIEZO_ROOT):
    """texture_piezo's ``ModelHeader`` for the checkpoint at ``path``, read from the file's content.

    Fields: model_family, model_version (code version), weights_version, checkpoint_tag, bundle_id (unique per
    written bundle), class_names, metrics, eval_protocol; plus ``label`` (family, weights version, tag, created date). Raises ``NotABundleError`` for a file that is not a bundle; a file that claims the schema
    but is inconsistent raises texture_piezo's own ValueError subclass."""
    runtime = runtime_api(root)
    try:
        return runtime.read_model_header(path)
    except runtime.NotABundleError as error:
        raise NotABundleError(str(error)) from error


def read_default_models(path: Path | None = None, root: Path = TEXTURE_PIEZO_ROOT):
    """texture_piezo's ``DefaultModels`` (``app_family`` and ``by_family`` -> ``ModelPointer``) from its tracked
    ``configs/default_models.json`` (or ``path``). FileNotFoundError / OSError when unreadable, ValueError when invalid."""
    reader = runtime_api(root).read_default_models
    return reader() if path is None else reader(path)


def runtime_artifacts(artifacts, root: Path = TEXTURE_PIEZO_ROOT):
    """texture_piezo ``RuntimeArtifacts`` for an app ``ModelArtifacts``: the family and the one bundle file."""
    return runtime_api(root).RuntimeArtifacts(model_type=artifacts.model_type, checkpoint_path=artifacts.checkpoint_path)


def load_runtime(model_type: str, artifacts, class_names: Sequence[str]):
    """Load the texture_piezo runtime for ``model_type``.

    ``artifacts`` is a texture_piezo ``RuntimeArtifacts`` (see ``runtime_artifacts``).
    ``class_names`` is the ordered label list; pass the same ordered tuple to every
    ``predict_proba`` call.
    """
    return runtime_api().load_runtime(model_type, artifacts, tuple(class_names))

