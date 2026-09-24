"""
Architecture Registry
======================
Single source of truth mapping a `model_type` key ("ann", "cnn", "quad",
"penta", ...) to everything the inference pipeline needs to discover, build,
and run that texture_piezo architecture. Before this existed, config.py and
classifier.py each had their own ann/cnn if-elif chain that grew by one
branch per architecture; adding CNNQuadBranchV3/V4 and CNNPentaBranchV1 as a
third and fourth branch would have meant touching both files' conditionals
plus pipeline.py's window_channels gating. Now adding an architecture is one
ArchSpec entry here.

Quad/Penta checkpoints don't get a per-version scaler_<version>.pkl or
raw_norm_stats_<version>.npz sidecar the way ANN/CNN do: their fusion-feature
normalization is a BatchNorm1d baked into the model's own state_dict (see
CNNQuadBranchV4.feat_norm in texture_piezo/src/model.py), and their raw-signal
normalization is one shared global scale fit once across quadbranch_v3/v4/
pentabranch_v1 alike (texture_piezo's data_v3.raw_norm.adc_scale_stats /
integrated_scale_stats in configs/config.yaml) rather than a per-version fit.
So these two need no sidecar at all -- only the checkpoint .pt file plus a
matching `<key>branch_<version>` entry in texture_piezo's config.yaml (for
fusion_feat_names/dropout). Nothing here declares that, though: model_discovery
decides whether a checkpoint is offerable by loading it, so an architecture
that needs no sidecars simply never fails for want of one.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import torch
import yaml

from ._paths import TEXTURE_PIEZO_ROOT, TEXTURE_PIEZO_SRC

sys.path.insert(0, str(TEXTURE_PIEZO_SRC))
from clip_windowing_utils_v1 import build_feature_names  # noqa: E402
from model import ANN, CNN1D, CNNPentaBranchV1, CNNQuadBranchV3, CNNQuadBranchV4  # noqa: E402
import data as data_mod  # noqa: E402

_TRAIN_CONFIG_PATH = TEXTURE_PIEZO_ROOT / "configs" / "config.yaml"
_FEATURE_NAMES_PATH = TEXTURE_PIEZO_ROOT / "data" / "processed" / "clip_feature_names_v2.json"

# Shared across every quad/penta version to date (fit once on quad_train_idx
# in 07_model_v4.ipynb, reused verbatim by quadbranch_v3/v4 and pentabranch_v1
# -- see that notebook's signal-group build cell). If a future version fits
# its own scale, give it a per-version override here rather than assuming
# this constant still applies.
_QUAD_N_FFT = 64
_QUAD_HOP_LENGTH = 16


def _load_train_config() -> dict:
    with open(_TRAIN_CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def _load_feature_names() -> list[str]:
    import json
    with open(_FEATURE_NAMES_PATH) as f:
        return json.load(f)


def _resolve_trained_feature_names(config, version: str) -> list[str]:
    """The exact, ordered hand-crafted feature names this checkpoint was
    trained on -- texture_piezo is the single source of truth for this, not
    any number hardcoded here.

    texture_piezo's feature set grows over time (e.g. commit 31184e6 appended
    integrated-signal peak features), so a checkpoint's expected input width
    drifts away from the live build_feature_names() output as soon as a new
    feature is added after that checkpoint was trained. If texture_piezo saves
    a `clip_feature_names_{prefix}_{version}.json` manifest next to a
    checkpoint (the named/ordered feature list frozen at training time), that
    manifest is authoritative and used to both size the model and select the
    matching named columns out of whatever the live feature builder produces
    now -- so this keeps working automatically no matter how many features
    get added later, as long as names aren't renamed/removed.

    Falls back to the live build_feature_names() (today's dim) when no
    manifest exists -- correct only for checkpoints trained against the
    feature set currently on disk; older checkpoints predating this
    convention will still raise a clear shape/name error at load time.

    The manifest is located by model_discovery, not by rebuilding a filename
    here: texture_piezo has written manifests under at least three
    conventions (`clip_feature_names_texture_ann_v3.json`,
    `clip_feature_names_v2.json`, `ann_v3b_feature_names.json`), so matching
    one literal pattern silently fell back to the live feature list for every
    checkpoint saved under the other two.
    """
    import json

    manifest_path = getattr(config, "feature_names_path", "")
    if manifest_path and Path(manifest_path).exists():
        with open(manifest_path) as f:
            return json.load(f)
    return build_feature_names()


# A checkpoint file is either a bare state_dict or a dict wrapping one
# alongside training metadata (ann_v3b_best.pt is {model_state, in_dim,
# names}). When the wrapper carries the feature names it was trained on, that
# beats any manifest resolved off the filesystem -- it cannot drift from the
# weights it ships with.
_STATE_DICT_KEYS = ("model_state", "state_dict", "model")
_EMBEDDED_NAMES_KEYS = ("names", "feature_names")


def _load_checkpoint(path) -> tuple[dict, list[str] | None]:
    """-> (state_dict, feature names the checkpoint embeds, or None)."""
    payload = torch.load(path, map_location="cpu")
    for key in _STATE_DICT_KEYS:
        inner = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(inner, dict):
            names = next(
                (payload[name_key] for name_key in _EMBEDDED_NAMES_KEYS
                 if isinstance(payload.get(name_key), list)),
                None,
            )
            return inner, names
    return payload, None


@dataclass
class ArchSpec:
    key: str  # "ann" | "cnn" | "quad" | "penta"
    # Filename parsing and artifact resolution live in model_discovery, keyed
    # off ARCH_STEM_PREFIXES -- an ArchSpec no longer carries its own regex or
    # a requires_sidecars flag, because whether a checkpoint is usable is
    # decided by whether `load` succeeds, not by which files happen to exist.
    # (config, version) -> a loaded runtime object exposing .predict_proba(...)
    load: Callable[["InferenceConfig", str], "ArchitectureRuntime"]  # noqa: F821


class ArchitectureRuntime:
    """Common interface every loaded architecture exposes to TextureClassifier.

    window_channels is (n_samples, n_pzt + 3): the raw PZT channels in
    pzt_columns order, followed by [shear_lr, shear_tb, normal] -- the same
    layout pipeline.classify_window already builds for every architecture.
    Architectures that don't need it (ANN) simply ignore the argument.
    """

    def predict_proba(
        self, feature_vector: np.ndarray, window_channels: np.ndarray | None,
        class_names: list[str], window_integrated: np.ndarray | None = None,
    ) -> dict[str, float]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# ANN
# ---------------------------------------------------------------------------

class _AnnRuntime(ArchitectureRuntime):
    def __init__(self, config, version):
        import joblib
        self.scaler = joblib.load(config.scaler_path)
        state_dict, embedded_names = _load_checkpoint(config.ann_model_path)
        self.feature_names = embedded_names or _resolve_trained_feature_names(config, version)
        # A scaler fitted on a different width than the feature names means
        # select_features would hand transform() the wrong columns and produce
        # confident nonsense rather than an error. Fail loudly instead.
        if self.scaler.n_features_in_ != len(self.feature_names):
            raise ValueError(
                f"scaler expects {self.scaler.n_features_in_} features but the feature names "
                f"resolved for {version} number {len(self.feature_names)}"
            )
        self.model = ANN(in_dim=len(self.feature_names), num_classes=len(config.class_names),
                          hidden_dims=[64, 32], dropout=0.3)
        self.model.load_state_dict(state_dict)
        self.model.eval()

    def predict_proba(self, feature_vector, window_channels, class_names, window_integrated=None):
        # feature_vector is always the full, current-order build_feature_names()
        # output; select_features maps it down to whatever named subset (in
        # whatever order) this checkpoint's manifest says it was trained on.
        selected = data_mod.select_features(
            feature_vector.reshape(1, -1), build_feature_names(), self.feature_names)
        scaled = self.scaler.transform(selected).astype(np.float32)
        with torch.no_grad():
            logits = self.model(torch.from_numpy(scaled))
            probs = torch.softmax(logits, dim=1).numpy()[0]
        return dict(zip(class_names, probs.tolist()))


def _load_ann(config, version):
    return _AnnRuntime(config, version)


# ---------------------------------------------------------------------------
# CNN1D
# ---------------------------------------------------------------------------

_RAW_NORM_CLIP_RANGE = (-50.0, 50.0)  # matches texture_piezo's fit_idle_norm_stats default


class _CnnRuntime(ArchitectureRuntime):
    def __init__(self, config, version):
        import joblib
        self.scaler = joblib.load(config.scaler_path)
        state_dict, embedded_names = _load_checkpoint(config.cnn_model_path)
        self.feature_names = embedded_names or _resolve_trained_feature_names(config, version)
        if self.scaler.n_features_in_ != len(self.feature_names):
            raise ValueError(
                f"scaler expects {self.scaler.n_features_in_} features but the feature names "
                f"resolved for {version} number {len(self.feature_names)}"
            )
        self.model = CNN1D(n_raw_channels=8, n_feat=len(self.feature_names),
                            num_classes=len(config.class_names), dropout=0.4)
        self.model.load_state_dict(state_dict)
        self.model.eval()
        raw_stats = np.load(config.raw_norm_stats_path)
        self.raw_mean = raw_stats["mean"].astype(np.float64)
        self.raw_std = raw_stats["std"].astype(np.float64)
        self.raw_eps = float(raw_stats["eps"])

    def _normalize_raw(self, window_channels):
        clip_lo, clip_hi = _RAW_NORM_CLIP_RANGE
        normed = (window_channels.astype(np.float64) - self.raw_mean) / (self.raw_std + self.raw_eps)
        normed = np.clip(normed, clip_lo, clip_hi).astype(np.float32)
        return normed.T[np.newaxis, :, :]  # (n_samples, 8) -> (1, 8, n_samples)

    def predict_proba(self, feature_vector, window_channels, class_names, window_integrated=None):
        if window_channels is None:
            raise ValueError("window_channels is required for model_type='cnn'")
        selected = data_mod.select_features(
            feature_vector.reshape(1, -1), build_feature_names(), self.feature_names)
        scaled_feat = self.scaler.transform(selected).astype(np.float32)
        x_raw = self._normalize_raw(window_channels)
        with torch.no_grad():
            logits = self.model(torch.from_numpy(x_raw), torch.from_numpy(scaled_feat))
            probs = torch.softmax(logits, dim=1).numpy()[0]
        return dict(zip(class_names, probs.tolist()))


def _load_cnn(config, version):
    return _CnnRuntime(config, version)


# ---------------------------------------------------------------------------
# Quad / Penta shared plumbing
# ---------------------------------------------------------------------------

class _QuadPentaRuntime(ArchitectureRuntime):
    """Shared input-construction for CNNQuadBranchV3/V4 and CNNPentaBranchV1 --
    they differ only in model class + forward signature (Penta adds x_raw_full),
    not in how the raw window becomes grid/frame tensors."""

    def __init__(self, model: torch.nn.Module, fusion_feat_names: list[str], is_penta: bool):
        self.model = model.eval()
        self.fusion_feat_names = fusion_feat_names
        self.is_penta = is_penta
        train_cfg = _load_train_config()
        raw_norm = train_cfg["data_v3"]["raw_norm"]
        self.adc_scale_stats = raw_norm["adc_scale_stats"]
        self.integrated_scale_stats = raw_norm["integrated_scale_stats"]
        self.raw_fixed_len = train_cfg["data_v3"]["raw_fixed_len"]
        self.feature_names = _load_feature_names() if fusion_feat_names else []

    def _build_signal_groups(self, window_channels: np.ndarray, window_integrated: np.ndarray | None = None):
        # window_channels: (n_samples, n_pzt + 3) = [pzt(5), shear_lr, shear_tb, normal]
        # Resample (not pad/truncate -- see texture_piezo/src/data.py
        # pad_or_truncate_raw) to the exact fixed length the model was
        # trained on (07_model_v4.ipynb / 04_model_no_SIFT_complexity_v3.ipynb),
        # so STFT framing and every normalization stat below sees the shape
        # the model expects. Every sample of the output is real interpolated
        # signal now, not a zero-padded tail, so valid_lengths is the full
        # raw_fixed_len -- masking it down to n_samples like the old
        # pad/truncate behavior did would wrongly exclude real resampled
        # signal from its own per-window normalization stats.
        padded, _stats = data_mod.pad_or_truncate_raw([window_channels], self.raw_fixed_len)
        valid_lengths = np.array([self.raw_fixed_len])

        adc = padded[:, :, :5]  # (1, raw_fixed_len, 5)
        shear_normal = padded[:, :, 5:8]  # (1, raw_fixed_len, 3)

        if window_integrated is not None:
            # Whole-file continuous integration (see inference/offline.py),
            # sliced to this window and resampled the same way adc is above --
            # matches training's dd.load_and_integrate_calibration_csv, which
            # integrates once over the full file before windowing. Computing
            # integration fresh per-window (the branch below) restarts the
            # causal-median baseline and bounded windowed sum at the window
            # boundary, producing an artificial startup ramp training never
            # saw (08_analysis_v1.ipynb finding, 2026-09-10).
            integrated, _stats = data_mod.pad_or_truncate_raw([window_integrated], self.raw_fixed_len)
        else:
            # Defensive fallback only -- as of the CausalDerivedChannels fix
            # (2026-09-16), both callers (inference/offline.py and
            # gui/inference_panel.py's live TouchID path) always pass
            # window_integrated from a persistent, whole-session/whole-
            # snapshot CausalDerivedChannels instance, so this branch should
            # no longer be hit in the current codebase. Left in place rather
            # than removed in case a future caller of predict_proba forgets
            # to supply window_integrated; restarting integration at this
            # window's own boundary here would silently reproduce the
            # original idle-noise-misclassification bug for that caller only.
            integrated = data_mod.integrate_channels(adc, channel_idx=range(5), valid_lengths=valid_lengths)

        adc_scaled = data_mod.apply_raw_norm_per_window_global_scale(
            adc, self.adc_scale_stats, valid_lengths=valid_lengths)
        integ_scaled = data_mod.apply_raw_norm_per_window_global_scale(
            integrated, self.integrated_scale_stats, valid_lengths=valid_lengths)
        shear_scaled = data_mod.normalize_raw_per_window(
            shear_normal, channel_groups=[[0, 1, 2]], valid_lengths=valid_lengths)

        x_grid_raw = data_mod.frame_pzt_grid(adc_scaled, n_fft=_QUAD_N_FFT, hop_length=_QUAD_HOP_LENGTH)
        x_grid_integ = data_mod.frame_pzt_grid(integ_scaled, n_fft=_QUAD_N_FFT, hop_length=_QUAD_HOP_LENGTH)
        x_shear_frames = data_mod.frame_raw_signal(shear_scaled, n_fft=_QUAD_N_FFT, hop_length=_QUAD_HOP_LENGTH)

        x_raw_full = None
        if self.is_penta:
            x_raw_full = np.concatenate([adc_scaled, integ_scaled, shear_scaled], axis=-1).astype(np.float32)

        return x_grid_raw, x_grid_integ, x_shear_frames, x_raw_full

    def _build_feat(self, feature_vector: np.ndarray) -> np.ndarray:
        if not self.fusion_feat_names:
            return np.zeros((1, 0), dtype=np.float32)
        selected = data_mod.select_features(
            feature_vector.reshape(1, -1), self.feature_names, self.fusion_feat_names)
        return selected.astype(np.float32)

    def predict_proba(self, feature_vector, window_channels, class_names, window_integrated=None):
        if window_channels is None:
            raise ValueError(f"window_channels is required for model_type={'penta' if self.is_penta else 'quad'!r}")
        x_grid_raw, x_grid_integ, x_shear_frames, x_raw_full = self._build_signal_groups(
            window_channels, window_integrated=window_integrated)
        x_feat = self._build_feat(feature_vector)

        args = [torch.from_numpy(x_grid_raw), torch.from_numpy(x_grid_integ), torch.from_numpy(x_shear_frames)]
        if self.is_penta:
            args.append(torch.from_numpy(x_raw_full))
        args.append(torch.from_numpy(x_feat))

        with torch.no_grad():
            logits = self.model(*args)
            probs = torch.softmax(logits, dim=1).numpy()[0]
        return dict(zip(class_names, probs.tolist()))


def _load_quad_or_penta(config, version, model_cls, is_penta: bool):
    train_cfg = _load_train_config()
    prefix = "penta" if is_penta else "quad"
    version_cfg = train_cfg.get(f"{prefix}branch_{version}")
    if version_cfg is None:
        raise ValueError(
            f"No '{prefix}branch_{version}' entry in texture_piezo/configs/config.yaml "
            f"-- needed for dropout/fusion_feat_names."
        )
    fusion_feat_names = version_cfg.get("fusion_feat_names", [])
    # Use the path the caller selected, not a rebuilt default name -- rebuilding
    # it here ignored the checkpoint tag entirely, so picking
    # texture_pentabranch_v1_best_working_09_17_2026 in the GUI silently loaded
    # the untagged texture_pentabranch_v1.pt instead.
    checkpoint_path = getattr(config, f"{prefix}_model_path")

    kwargs = dict(
        n_fft=_QUAD_N_FFT, n_feat=len(fusion_feat_names),
        num_classes=len(config.class_names), dropout=version_cfg.get("dropout", 0.4),
    )
    if is_penta:
        kwargs["n_raw_channels"] = 13
        model = model_cls(**kwargs)
    else:
        model = model_cls(**kwargs)
    model.load_state_dict(torch.load(checkpoint_path, map_location="cpu"))

    return _QuadPentaRuntime(model, fusion_feat_names, is_penta)


_QUAD_MODEL_CLS_BY_VERSION = {
    # v3 (Option B, factorized Conv2d+Conv1d per-frame) and v4 (Option A,
    # single Conv3d stack per-frame) are different architectures that happen
    # to share the same fusion head shape -- a v3 checkpoint's state_dict
    # only matches CNNQuadBranchV3, not V4, so the class must be picked per
    # version rather than assumed. Add new versions' class here as they're
    # trained; texture_quadbranch_<version>.pt with no entry here will raise
    # instead of silently loading into the wrong architecture.
    "v3": CNNQuadBranchV3,
    "v4": CNNQuadBranchV4,
}


def _load_quad(config, version):
    model_cls = _QUAD_MODEL_CLS_BY_VERSION.get(version)
    if model_cls is None:
        raise ValueError(
            f"Unknown quadbranch version {version!r} -- add it to "
            f"_QUAD_MODEL_CLS_BY_VERSION in architectures.py with the matching model class."
        )
    return _load_quad_or_penta(config, version, model_cls, is_penta=False)


def _load_penta(config, version):
    return _load_quad_or_penta(config, version, CNNPentaBranchV1, is_penta=True)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Checkpoint naming, version parsing and tag priority all live in
# model_discovery (ARCH_STEM_PREFIXES / CHECKPOINT_TAGS); re-exported here so
# existing importers of these two names keep working.
from .model_discovery import CHECKPOINT_DEFAULT, CHECKPOINT_TAGS  # noqa: E402,F401

ARCH_REGISTRY: dict[str, ArchSpec] = {
    "ann": ArchSpec(key="ann", load=_load_ann),
    "cnn": ArchSpec(key="cnn", load=_load_cnn),
    "quad": ArchSpec(key="quad", load=_load_quad),
    "penta": ArchSpec(key="penta", load=_load_penta),
}
