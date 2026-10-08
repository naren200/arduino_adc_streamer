"""
Model Discovery
================
Discovers usable texture_piezo models by READING THE FILES, never by guessing from names.

Every `.pt` file directly inside texture_piezo's `models/` and `models/_bundles/` is opened and identified by
its content (texture_piezo's `read_model_header`, reached only through the adapter): a self-describing bundle
carries its family, code version, weights version, checkpoint tag, a unique `bundle_id` (creation time + weights
hash) and class names in its header. The file name, its suffix and the folder it sits in play no part, so renaming
a bundle changes nothing. The scan is flat: `models/_archive` and the other sub-folders are never entered.

  - A file whose content is not a bundle (a bare state dict, an older checkpoint without the current header) is not
    offered. It is reported as skipped with the reason "not a model bundle; re-export from its notebook"
    (`skipped_files()`).
  - EVERY distinct bundle is listed. Only a byte-identical copy (or a file with an already-seen `bundle_id`, i.e.
    the same weights written at the same moment) is collapsed: it is skipped as "duplicate of <first file>" (first
    by sorted path, `models/` before `models/_bundles/`). Retrained weights are never collapsed, whatever their labels.
  - A bundle is selected by `(model_family, weights_version, checkpoint)` = `ModelArtifacts.model_type / .version /
    .checkpoint`. `checkpoint` is the header's `checkpoint_tag` for the NEWEST bundle (by `bundle_id`) carrying that
    family, weights version and tag, and `"<tag>@<bundle_id>"` for each older bundle of the same triple, so every
    bundle has a selectable key and a saved selection of the plain tag always means "the latest of that tag".
    `ModelArtifacts.label` is the stable human label (family, weights version, tag, created date).
  - The class names a model predicts over come from its bundle (`ModelArtifacts.class_names`).

The offered-versions list is validated by actually loading each candidate (`probe`), not by checking that
a header parses: a bundle whose weights do not fit its declared code version is read but not offerable.

Default models (tracked data, read through the adapter)
-------------------------------------------------------
texture_piezo's `configs/default_models.json` names the app's family and, per family, the (weights_version,
checkpoint_tag) its selection starts on:

    {"app_family": "penta", "models": {"penta": {"weights_version": "v1", "checkpoint_tag": "default"}, ...}}

A pin resolves to the newest LOADABLE bundle (by `bundle_id`) with that weights version and tag. A family with no
entry (chunk) starts on its newest loadable bundle, silently. When the file is absent or invalid, or a pin names a
bundle that is missing or does not load, the affected default falls back, deterministically, to the NEWEST loadable
bundle of that family by `bundle_id` timestamp and one WARNING `model_default_fallback` event says why. When the app
family itself has nothing loadable, the app opens on the first family of the catalog's order that does (with that
family's own default). `default_for_family()` is the per-family rule and seeds each family's selection;
`default_model()` is the app's.
"""

from __future__ import annotations

import hashlib
import logging
import pickle
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from inference import texture_piezo_adapter
from inference._paths import TEXTURE_PIEZO_MODELS

# Folders scanned (flat, in this order) for model files.
MODEL_DIRS = (TEXTURE_PIEZO_MODELS, TEXTURE_PIEZO_MODELS / "_bundles")
# None reads texture_piezo's own configs/default_models.json (through the adapter); a test points it elsewhere.
DEFAULT_MODELS_PATH: Path | None = None
MODEL_FILE_GLOB = "*.pt"
BUNDLE_ID_SEPARATOR = "@"

# The checkpoint tag a bundle with a single checkpoint carries; it lists first within its version.
CHECKPOINT_DEFAULT = "default"

NOT_A_BUNDLE_REASON = "not a model bundle; re-export from its notebook"

# Failures an unloadable bundle legitimately raises: texture_piezo's typed errors (all ValueError),
# torch/pickle decoding of a damaged file, and an unreadable path. Anything else (ImportError, AttributeError,
# TypeError, NameError ...) is a programming error and must surface, not read as "SKIP".
EXPECTED_MODEL_LOAD_FAILURES = (ValueError, OSError, RuntimeError, EOFError, pickle.UnpicklingError)

# RuntimeError subclasses that signal a broken environment, not a bad checkpoint: they must
# surface instead of reading as "SKIP".
ENVIRONMENT_FAILURES = (texture_piezo_adapter.TexturePiezoUnavailableError, NotImplementedError)

PROBE_LOG_EVENT = "model_discovery_probe"
PROBE_OK = "ok"
PROBE_SKIPPED = "skipped"
CATALOG_UNAVAILABLE_LOG_EVENT = "model_catalog_unavailable"
FILE_SKIPPED_LOG_EVENT = "model_file_skipped"
DEFAULT_FALLBACK_LOG_EVENT = "model_default_fallback"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelArtifacts:
    """One selectable model: the bundle file and the identity its header declares."""

    model_type: str  # header model_family
    version: str  # header weights_version
    checkpoint: str  # the selection key: header checkpoint_tag, or "<tag>@<bundle_id>" for an older bundle of the same tag
    checkpoint_path: Path
    model_version: str  # header model_version: the code version that builds the network
    class_names: tuple[str, ...]
    bundle_id: str  # header bundle_id: unique per written bundle, sorts by creation time
    checkpoint_tag: str  # header checkpoint_tag
    label: str  # stable human label: family, weights version, tag, created date


@dataclass(frozen=True)
class DiscoveredModel:
    artifacts: ModelArtifacts
    error: str | None  # None means it probe-loaded successfully

    @property
    def is_loadable(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class SkippedFile:
    """A model-folder file that is not offered, and why."""

    path: Path
    reason: str


@dataclass(frozen=True)
class DefaultModel:
    """The default model and where it came from. `artifacts` is None when nothing is loadable."""

    artifacts: ModelArtifacts | None
    is_fallback: bool
    reason: str | None  # why the pointer was not used; None when it was


@lru_cache(maxsize=None)
def _catalog_by_key() -> dict:
    """The catalog entries keyed by family; empty (and logged) when texture_piezo is unavailable,
    so importing and constructing the app's config never requires it."""
    try:
        return {entry.key: entry for entry in texture_piezo_adapter.model_catalog()}
    except texture_piezo_adapter.TexturePiezoUnavailableError as exc:
        logger.warning(CATALOG_UNAVAILABLE_LOG_EVENT, extra={"reason": str(exc)})
        return {}


def family_keys() -> tuple[str, ...]:
    return tuple(_catalog_by_key())


def display_name_of(model_type: str) -> str:
    return _catalog_by_key()[model_type].display_name


def _natural_key(text: str) -> tuple:
    """Digit runs compare as numbers (v2 < v10); the parts alternate str / int, so keys always compare."""
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text))


def version_sort_key(version: str) -> tuple:
    return _natural_key(version)


def _artifacts_order(artifacts: ModelArtifacts) -> tuple:
    return (
        _natural_key(artifacts.version), artifacts.checkpoint_tag != CHECKPOINT_DEFAULT,
        _natural_key(artifacts.checkpoint_tag),
    )


def _newest_first(models):
    return sorted(models, key=lambda model: model.artifacts.bundle_id, reverse=True)


def _model_files() -> list[Path]:
    return [path for directory in MODEL_DIRS if directory.is_dir() for path in sorted(directory.glob(MODEL_FILE_GLOB))]


def _log_skipped_file(path: Path, reason: str) -> None:
    logger.info(FILE_SKIPPED_LOG_EVENT, extra={"path": str(path), "reason": reason})


def _first_line(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"


def _artifacts_of(path: Path, header) -> ModelArtifacts:
    return ModelArtifacts(
        model_type=header.model_family, version=header.weights_version, checkpoint=header.checkpoint_tag,
        checkpoint_path=path, model_version=header.model_version, class_names=tuple(header.class_names),
        bundle_id=header.bundle_id, checkpoint_tag=header.checkpoint_tag, label=header.label)


def _read_artifacts(path: Path) -> tuple[ModelArtifacts | None, str | None]:
    """-> (artifacts, None) for a readable bundle of a family with a runtime, else (None, why not)."""
    try:
        header = texture_piezo_adapter.read_model_header(path)
    except texture_piezo_adapter.NotABundleError:
        return None, NOT_A_BUNDLE_REASON
    except ENVIRONMENT_FAILURES:
        raise
    except EXPECTED_MODEL_LOAD_FAILURES as exc:
        return None, f"invalid bundle: {_first_line(exc)}"
    if header.model_family not in _catalog_by_key():
        return None, f"no runtime for model family {header.model_family!r}"
    return _artifacts_of(path, header), None


def _log_probe(artifacts: ModelArtifacts, outcome: str, reason: str | None) -> None:
    logger.info(PROBE_LOG_EVENT, extra={
        "family": artifacts.model_type, "version": artifacts.version,
        "checkpoint": artifacts.checkpoint, "outcome": outcome, "reason": reason,
    })


def load_artifacts_runtime(artifacts: ModelArtifacts):
    """The one place discovered artifacts become a runtime, shared by the probe and the app's real load.
    The class order is the bundle's own."""
    return texture_piezo_adapter.load_runtime(
        artifacts.model_type, texture_piezo_adapter.runtime_artifacts(artifacts), artifacts.class_names)


def _probe(artifacts: ModelArtifacts) -> str | None:
    """None when the bundle loads, else why it is not offerable. Only
    EXPECTED_MODEL_LOAD_FAILURES count as "not offerable"; anything else propagates."""
    try:
        load_artifacts_runtime(artifacts)
    except ENVIRONMENT_FAILURES:
        raise
    except EXPECTED_MODEL_LOAD_FAILURES as exc:
        reason = _first_line(exc)
        _log_probe(artifacts, PROBE_SKIPPED, reason)
        return reason
    _log_probe(artifacts, PROBE_OK, None)
    return None


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _with_selection_keys(candidates: list[ModelArtifacts]) -> list[ModelArtifacts]:
    """Give every candidate its selectable `checkpoint` key: the plain tag for the newest bundle of a
    (family, weights version, tag), `<tag>@<bundle_id>` for each older one."""
    newest: dict[tuple[str, str, str], str] = {}
    for artifacts in candidates:
        group = (artifacts.model_type, artifacts.version, artifacts.checkpoint_tag)
        newest[group] = max(newest.get(group, ""), artifacts.bundle_id)
    return [
        artifacts if artifacts.bundle_id == newest[(artifacts.model_type, artifacts.version, artifacts.checkpoint_tag)]
        else replace(artifacts, checkpoint=f"{artifacts.checkpoint_tag}{BUNDLE_ID_SEPARATOR}{artifacts.bundle_id}")
        for artifacts in candidates
    ]


def _twin_of(artifacts: ModelArtifacts, seen: dict[str, Path]) -> Path | None:
    """The earlier file this one repeats: byte-identical content, or the same bundle_id (same weights, same moment)."""
    return seen.get(_file_digest(artifacts.checkpoint_path)) or seen.get(artifacts.bundle_id)


def _scan_files() -> tuple[tuple[DiscoveredModel, ...], tuple[SkippedFile, ...]]:
    candidates: list[ModelArtifacts] = []
    seen: dict[str, Path] = {}
    skipped: list[SkippedFile] = []
    for path in _model_files():
        artifacts, reason = _read_artifacts(path)
        twin = _twin_of(artifacts, seen) if artifacts is not None else None
        if twin is not None:
            reason = f"duplicate of {twin.name}"
        if reason is not None:
            skipped.append(SkippedFile(path, reason))
            _log_skipped_file(path, reason)
        else:
            seen.setdefault(_file_digest(path), path)
            seen.setdefault(artifacts.bundle_id, path)
            candidates.append(artifacts)
    models = tuple(DiscoveredModel(artifacts, _probe(artifacts)) for artifacts in _with_selection_keys(candidates))
    return models, tuple(skipped)


@lru_cache(maxsize=None)
def _scan() -> tuple[tuple[DiscoveredModel, ...], tuple[SkippedFile, ...]]:
    """Every model file read once. Nothing is read while the catalog is empty (texture_piezo unavailable)."""
    return _scan_files() if _catalog_by_key() else ((), ())


def discover(model_type: str) -> tuple[DiscoveredModel, ...]:
    """Every bundle on disk for `model_type`, each tagged with whether it actually loads, in stable order.
    Cached -- call `refresh()` after adding files at runtime."""
    found = [model for model in _scan()[0] if model.artifacts.model_type == model_type]
    return tuple(sorted(_newest_first(found), key=lambda model: _artifacts_order(model.artifacts)))


def skipped_files() -> tuple[SkippedFile, ...]:
    """Model-folder files that are not offered (not a bundle, invalid, no runtime, duplicate), with reasons."""
    return _scan()[1]


def refresh() -> None:
    """Drop the discovery caches so newly added model files and a changed default pointer are picked up."""
    _scan.cache_clear()
    _catalog_by_key.cache_clear()
    _defaults.cache_clear()


def loadable(model_type: str) -> list[DiscoveredModel]:
    return [found for found in discover(model_type) if found.is_loadable]


def artifacts_for(model_type: str, version: str, checkpoint: str = CHECKPOINT_DEFAULT) -> ModelArtifacts | None:
    for found in discover(model_type):
        if found.artifacts.version == version and found.artifacts.checkpoint == checkpoint:
            return found.artifacts
    return None


def _read_default_models() -> tuple[object | None, str | None]:
    """-> (texture_piezo's DefaultModels, None), or (None, why there is no usable file)."""
    try:
        return texture_piezo_adapter.read_default_models(DEFAULT_MODELS_PATH), None
    except FileNotFoundError:
        return None, "default models file (configs/default_models.json) not found"
    except (OSError, ValueError) as exc:
        return None, f"default models file is unusable ({_first_line(exc)})"


def _newest_loadable(model_type: str) -> ModelArtifacts | None:
    usable = _newest_first(loadable(model_type))
    return usable[0].artifacts if usable else None


def _pinned_artifacts(model_type: str, pointer) -> ModelArtifacts | None:
    """The newest loadable bundle of `model_type` carrying the pin's weights version and checkpoint tag."""
    matching = [
        found for found in _newest_first(loadable(model_type))
        if (found.artifacts.version, found.artifacts.checkpoint_tag) == (pointer.weights_version, pointer.checkpoint_tag)
    ]
    return matching[0].artifacts if matching else None


def _log_fallback(model_type: str | None, reason: str, fallback: ModelArtifacts | None) -> None:
    logger.warning(DEFAULT_FALLBACK_LOG_EVENT, extra={
        "family": model_type, "reason": reason,
        "fallback_family": fallback.model_type if fallback else None,
        "fallback_version": fallback.version if fallback else None,
        "fallback_checkpoint": fallback.checkpoint if fallback else None,
    })


def _family_default(model_type: str, defaults, file_problem: str | None) -> DefaultModel:
    pointer = defaults.by_family.get(model_type) if defaults is not None else None
    if pointer is not None and (pinned := _pinned_artifacts(model_type, pointer)) is not None:
        return DefaultModel(pinned, is_fallback=False, reason=None)
    newest = _newest_loadable(model_type)
    if pointer is None and file_problem is None:
        return DefaultModel(newest, is_fallback=False, reason=None)  # an unpinned family: its newest export, by design
    reason = file_problem or f"default model {model_type}/{pointer.weights_version}/{pointer.checkpoint_tag} is not a loadable model"
    if file_problem is None:
        _log_fallback(model_type, reason, newest)
    return DefaultModel(newest, is_fallback=True, reason=reason)


def _app_default(by_family: dict[str, DefaultModel], defaults, file_problem: str | None) -> DefaultModel:
    app_family = defaults.app_family if defaults is not None else None
    if app_family in by_family and by_family[app_family].artifacts is not None:
        return by_family[app_family]
    reason = file_problem or f"app family {app_family!r} has no loadable model"
    first = next((default for default in by_family.values() if default.artifacts is not None), None)
    return DefaultModel(first.artifacts if first else None, is_fallback=True, reason=reason)


@lru_cache(maxsize=None)
def _defaults() -> tuple[dict[str, DefaultModel], DefaultModel]:
    """Every family's default and the app's, resolved once per refresh; each fallback is logged once."""
    defaults, file_problem = _read_default_models()
    if file_problem is not None:
        _log_fallback(None, file_problem, None)
    by_family = {family: _family_default(family, defaults, file_problem) for family in family_keys()}
    app = _app_default(by_family, defaults, file_problem)
    if file_problem is None and app.is_fallback:
        _log_fallback(None, app.reason, app.artifacts)
    return by_family, app


def default_for_family(model_type: str) -> ModelArtifacts | None:
    """The default selection for `model_type` (see the module docstring); None when it has nothing loadable."""
    default = _defaults()[0].get(model_type)
    return default.artifacts if default else None


def default_model() -> DefaultModel:
    """The app's default model, with whether and why it is a fallback."""
    return _defaults()[1]
