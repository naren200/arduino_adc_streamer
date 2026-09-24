"""
Model Discovery
================
Filesystem-driven discovery of usable texture_piezo checkpoints. Nothing here
knows any specific filename: an architecture declares only its stem prefixes
(e.g. "ann" is written as both `texture_ann_v3.pt` and `ann_v3b_best.pt`), and
every artifact a checkpoint needs -- scaler, raw-norm stats, frozen
feature-name manifest -- is located by matching the checkpoint's own version
token against the artifact folders.

The offered-versions list is validated by actually loading each candidate
(`probe`), not by checking that files exist. Presence proved too weak: the
models folder currently holds `texture_ann_v2.pt` (131 input features) whose
only resolvable feature-name manifest lists 296, and `texture_cnn2d_v3.pt`,
a 2-D architecture that the CNN1D runtime cannot load at all. Both satisfy a
presence check and neither can run, so both used to appear in the GUI's
version dropdown and fail when selected. Loading all 14 checkpoints in the
folder takes ~0.03s, so probing is cheap enough to be the gate.

Resolution rules, in one place:
  - A checkpoint stem is `<arch prefix>_<version>[_<tag>]`, version `v\\d+` plus
    an optional single letter (v2, v2b, v3). No tag means CHECKPOINT_DEFAULT.
  - A sidecar belongs to a version if the version appears as a whole
    underscore-separated segment of its stem -- so `scaler_v2b.pkl` belongs to
    v2b and never to v2 -- and its stem contains that artifact role's keywords.
  - Ties are broken toward the least-decorated name (fewest segments), so
    `clip_feature_names_v2.json` wins over `clip_feature_names_v2_option2_final.json`.
  - Anything with "deprecated" in its name is ignored entirely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from ._paths import TEXTURE_PIEZO_MODELS, TEXTURE_PIEZO_ROOT

# Folders scanned for checkpoints and their sidecars. Checkpoints only ever
# live in the first; manifests are written to either by different notebooks.
CHECKPOINT_DIR = TEXTURE_PIEZO_MODELS
ARTIFACT_DIRS = (TEXTURE_PIEZO_MODELS, TEXTURE_PIEZO_ROOT / "data" / "processed")

DEFAULT_CLASS_NAMES = ("bumpy_wood", "cardboard", "leather", "tile", "tiona", "idle")

CHECKPOINT_DEFAULT = "default"
CHECKPOINT_TAGS = ("best", "final", "last")
_VERSION_PATTERN = r"(v\d+[a-zA-Z]?)"
_EXCLUDED_MARKER = "deprecated"

# model_type -> the checkpoint stem prefixes that architecture is saved under.
# Multiple prefixes per architecture is the normal case, not an exception:
# texture_piezo's training notebooks have used more than one naming convention
# over time and both sets of files are still in the folder. A prefix is listed
# here only if the architecture's loader can actually consume it -- notably
# `texture_cnn2d` is absent, because those checkpoints are a 2-D architecture
# with no runtime in architectures.py.
ARCH_STEM_PREFIXES: dict[str, tuple[str, ...]] = {
    "ann": ("texture_ann", "ann"),
    "cnn": ("texture_cnn",),
    "quad": ("texture_quadbranch",),
    "penta": ("texture_pentabranch",),
}

# model_type -> the sidecar roles that architecture's runtime actually reads.
# Scoping matters: version tokens are shared across architectures, so an
# unscoped search happily hands penta v1 the unrelated ann_attention_v1_scaler.pkl
# and quad v3 the ANN's scaler_v3.pkl. Quad/penta read neither (their
# normalization is baked into the checkpoint or globally shared), so resolving
# those roles for them can only produce a wrong answer, never a useful one.
ARCH_SIDECAR_ROLES: dict[str, tuple[str, ...]] = {
    "ann": ("scaler_path", "feature_names_path"),
    "cnn": ("scaler_path", "raw_norm_stats_path", "feature_names_path"),
    "quad": (),
    "penta": (),
}

# Artifact role -> (file suffix, keywords every candidate stem must contain).
# "raw" distinguishes raw_norm_stats_*.npz from spec_norm_stats_*.npz, which
# belongs to the 2-D spectrogram models rather than to CNN1D.
_SIDECAR_RULES = {
    "scaler_path": (".pkl", ("scaler",)),
    "raw_norm_stats_path": (".npz", ("raw", "norm")),
    "feature_names_path": (".json", ("feature", "names")),
}


@dataclass(frozen=True)
class ModelArtifacts:
    """Every path one selectable checkpoint needs, resolved from disk."""

    model_type: str
    version: str
    checkpoint: str
    checkpoint_path: Path
    scaler_path: Path | None
    raw_norm_stats_path: Path | None
    feature_names_path: Path | None


@dataclass(frozen=True)
class DiscoveredModel:
    artifacts: ModelArtifacts
    error: str | None  # None means it probe-loaded successfully

    @property
    def is_loadable(self) -> bool:
        return self.error is None


def _segments(stem: str) -> list[str]:
    return stem.lower().split("_")


def _is_excluded(path: Path) -> bool:
    return _EXCLUDED_MARKER in path.stem.lower()


@lru_cache(maxsize=None)
def _checkpoint_regex(model_type: str) -> re.Pattern:
    prefixes = "|".join(re.escape(p) for p in ARCH_STEM_PREFIXES[model_type])
    return re.compile(rf"^(?:{prefixes})_{_VERSION_PATTERN}(?:_(.+))?$")


def _find_sidecar(version: str, role: str) -> Path | None:
    suffix, keywords = _SIDECAR_RULES[role]
    candidates = [
        path
        for directory in ARTIFACT_DIRS
        for path in directory.glob(f"*{suffix}")
        if not _is_excluded(path)
        and version.lower() in _segments(path.stem)
        and all(keyword in path.stem.lower() for keyword in keywords)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda path: (len(_segments(path.stem)), path.name))


def _resolve_artifacts(model_type: str, version: str, checkpoint: str, checkpoint_path: Path) -> ModelArtifacts:
    roles = ARCH_SIDECAR_ROLES[model_type]
    resolved = {role: (_find_sidecar(version, role) if role in roles else None) for role in _SIDECAR_RULES}
    return ModelArtifacts(
        model_type=model_type,
        version=version,
        checkpoint=checkpoint,
        checkpoint_path=checkpoint_path,
        **resolved,
    )


def _candidates(model_type: str) -> list[ModelArtifacts]:
    regex = _checkpoint_regex(model_type)
    found = []
    for path in CHECKPOINT_DIR.glob("*.pt"):
        if _is_excluded(path):
            continue
        match = regex.match(path.stem)
        if match:
            found.append(_resolve_artifacts(model_type, match.group(1), match.group(2) or CHECKPOINT_DEFAULT, path))
    return found


def probe_config(artifacts: ModelArtifacts) -> SimpleNamespace:
    """The minimal duck-typed config an ArchSpec.load() reads.

    Deliberately not an InferenceConfig: InferenceConfig's own defaults call
    back into discovery, so building one here to probe a candidate would
    recurse.
    """
    config = SimpleNamespace(
        class_names=list(DEFAULT_CLASS_NAMES),
        scaler_path=str(artifacts.scaler_path) if artifacts.scaler_path else "",
        raw_norm_stats_path=str(artifacts.raw_norm_stats_path) if artifacts.raw_norm_stats_path else "",
        feature_names_path=str(artifacts.feature_names_path) if artifacts.feature_names_path else "",
    )
    setattr(config, f"{artifacts.model_type}_model_path", str(artifacts.checkpoint_path))
    return config


def _probe(artifacts: ModelArtifacts) -> str | None:
    from .architectures import ARCH_REGISTRY

    try:
        ARCH_REGISTRY[artifacts.model_type].load(probe_config(artifacts), artifacts.version)
    except Exception as exc:  # noqa: BLE001 -- any failure means "not offerable"
        reason = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        missing = [role for role in ARCH_SIDECAR_ROLES[artifacts.model_type] if getattr(artifacts, role) is None]
        return f"{reason} (no {', '.join(missing)} found for {artifacts.version})" if missing else reason
    return None


def version_sort_key(version: str) -> tuple[int, str]:
    """Natural order, so v10 sorts after v2 rather than lexicographically before it."""
    match = re.match(r"^v(\d+)([a-zA-Z]?)$", version)
    return (int(match.group(1)), match.group(2)) if match else (0, version)


@lru_cache(maxsize=None)
def discover(model_type: str) -> tuple[DiscoveredModel, ...]:
    """Every checkpoint on disk for `model_type`, each tagged with whether it
    actually loads. Cached -- call `refresh()` after adding files at runtime."""
    if model_type not in ARCH_STEM_PREFIXES:
        return ()
    discovered = [DiscoveredModel(artifacts, _probe(artifacts)) for artifacts in _candidates(model_type)]
    checkpoint_priority = (CHECKPOINT_DEFAULT,) + CHECKPOINT_TAGS
    return tuple(sorted(discovered, key=lambda found: (
        version_sort_key(found.artifacts.version),
        checkpoint_priority.index(found.artifacts.checkpoint)
        if found.artifacts.checkpoint in checkpoint_priority else len(checkpoint_priority),
        found.artifacts.checkpoint,
    )))


def refresh() -> None:
    """Drop the discovery cache so newly added checkpoint files are picked up."""
    discover.cache_clear()
    _checkpoint_regex.cache_clear()


def loadable(model_type: str) -> list[DiscoveredModel]:
    return [found for found in discover(model_type) if found.is_loadable]


def artifacts_for(model_type: str, version: str, checkpoint: str = CHECKPOINT_DEFAULT) -> ModelArtifacts | None:
    for found in discover(model_type):
        if found.artifacts.version == version and found.artifacts.checkpoint == checkpoint:
            return found.artifacts
    return None


def newest_artifacts(model_type: str) -> ModelArtifacts | None:
    """The default selection for `model_type`: newest loadable version, and
    within it the untagged checkpoint if there is one."""
    usable = loadable(model_type)
    if not usable:
        return None
    # Only an UNTAGGED checkpoint can become the default. A tag marks a
    # variant you opt into, not a release: ann_v5_partial_finetune.pt is a
    # newer version number than texture_ann_v3.pt but is the catastrophic-
    # forgetting fine-tune (idle F1 0.000), and "newest version wins" alone
    # would have silently promoted it. To make a new checkpoint the default,
    # save it untagged (texture_ann_v6.pt); everything else stays selectable
    # but must be chosen deliberately.
    candidates = [f for f in usable if f.artifacts.checkpoint == CHECKPOINT_DEFAULT] or usable
    # Within the highest version number, prefer the unlettered variant: a
    # letter suffix marks a variant of that version (v3b is an 84-feature
    # retrain of v3), not a successor, so v3b must not demote v3.
    newest_number = max(version_sort_key(f.artifacts.version)[0] for f in candidates)
    in_newest = [f for f in candidates if version_sort_key(f.artifacts.version)[0] == newest_number]
    unlettered = [f for f in in_newest if not version_sort_key(f.artifacts.version)[1]]
    return (unlettered or in_newest)[0].artifacts
