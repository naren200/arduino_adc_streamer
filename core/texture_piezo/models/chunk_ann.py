"""
Multi-scale chunk ANN
======================
Inference-only port of texture_piezo_v4's MultiScaleChunkANN. The network is
described entirely by its checkpoint bundle -- which chunk sizes ("scales") it
was trained on, the window length, encoder/head widths, and one fitted
StandardScaler (mean/scale) per scale -- so any scale combination that has
been exported loads and runs through the same code. Nothing here names 20, 25
or a scale count.

Bundle format (written by texture_piezo_v4/notebooks/_v4_export_chunk_bundles.py):
    model_state, scales, window_len, feat_dim, encoder_hidden, head_hidden,
    dropout, num_classes, concat_max_chunks,
    scaler_mean: {str(scale): list[float]}, scaler_scale: {str(scale): list[float]}
Only tensors, lists, dicts and numbers -- no pickled sklearn objects.

Window layout fed to `ChunkPredictor.predict_proba` is the live 8-column one
used by every other runtime: [pzt(5), shear_jerk_lr, shear_jerk_tb, normal_jerk],
already resampled to the bundle's window_len. The chunk features read only
those columns (the integrated block of v4's 13-column layout is unused).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import torch
from torch import nn

from inference._paths import TEXTURE_PIEZO_SRC  # noqa: F401 -- puts texture_piezo/src on sys.path
from clip_windowing_utils_v1 import CHANNEL_LABELS  # noqa: E402

N_PZT = len(CHANNEL_LABELS)
N_WINDOW_COLUMNS = N_PZT + 3
N_CHUNK_FEATURES = 28
DEFAULT_CONCAT_MAX_CHUNKS = 5
# Channel C (center PZT) reports its jerk abs-valued, matching
# clip_windowing_utils_v1's per-window jerk_d1 convention.
_ABS_JERK_LABEL = "C"
_POOL_EPS = 1e-5


# ---------------------------------------------------------------------------
# Chunk features
# ---------------------------------------------------------------------------

def _mean_var(x: np.ndarray, axis: int) -> np.ndarray:
    return np.concatenate([x.mean(axis=axis), x.var(axis=axis)], axis=-1)


def _pzt_jerk(chunks: np.ndarray) -> np.ndarray:
    d1 = np.diff(chunks, axis=1).copy()
    d1[..., CHANNEL_LABELS.index(_ABS_JERK_LABEL)] = np.abs(d1[..., CHANNEL_LABELS.index(_ABS_JERK_LABEL)])
    return d1


def _series_mean_var(series: np.ndarray) -> np.ndarray:
    return np.stack([series.mean(axis=1), series.var(axis=1)], axis=-1)


def compute_chunk_features(window: np.ndarray, chunk_size: int) -> np.ndarray:
    """(window_len, 8) -> (n_chunks, 28), column order of v4's CHUNK_FEATURE_NAMES:
    PZT mean, PZT var, PZT jerk mean, PZT jerk var, then mean/var of
    shear_jerk_lr, shear_jerk_tb, net shear magnitude, normal_jerk."""
    window_len, n_columns = window.shape
    if n_columns != N_WINDOW_COLUMNS:
        raise ValueError(f"expected {N_WINDOW_COLUMNS} window columns, got {n_columns}")
    if window_len % chunk_size != 0:
        raise ValueError(f"window_len={window_len} is not a multiple of chunk_size={chunk_size}")
    n_chunks = window_len // chunk_size
    chunks = window.astype(np.float32).reshape(n_chunks, chunk_size, n_columns)

    pzt = chunks[:, :, :N_PZT]
    shear_lr, shear_tb, normal = chunks[:, :, N_PZT], chunks[:, :, N_PZT + 1], chunks[:, :, N_PZT + 2]
    net_shear = np.sqrt(shear_lr ** 2 + shear_tb ** 2)

    feats = np.concatenate([
        _mean_var(pzt, axis=1),
        _mean_var(_pzt_jerk(pzt), axis=1),
        _series_mean_var(shear_lr), _series_mean_var(shear_tb),
        _series_mean_var(net_shear), _series_mean_var(normal),
    ], axis=-1)
    return feats.astype(np.float32)


# ---------------------------------------------------------------------------
# Network (mirrors texture_piezo_v4/src/model.py so state_dicts load strictly)
# ---------------------------------------------------------------------------

class MeanMaxStdPool1d(nn.Module):
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        std = torch.sqrt(x.var(dim=2, unbiased=False) + _POOL_EPS)
        return torch.cat([x.mean(dim=2), x.amax(dim=2), std], dim=1)


class ChunkEncoder(nn.Module):
    def __init__(self, feat_dim: int, hidden: int, dropout: float):
        super().__init__()
        self.hidden = hidden
        self.net = nn.Sequential(
            nn.Linear(feat_dim, hidden), nn.BatchNorm1d(hidden), nn.ReLU(), nn.Dropout(dropout),
            nn.Linear(hidden, hidden), nn.BatchNorm1d(hidden), nn.ReLU(), nn.Dropout(dropout),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        b, n_chunks, feat_dim = x.shape
        return self.net(x.reshape(b * n_chunks, feat_dim)).reshape(b, n_chunks, self.hidden)


class MultiScaleChunkANN(nn.Module):
    def __init__(self, scales: list[int], window_len: int, feat_dim: int, encoder_hidden: int,
                 head_hidden: int, num_classes: int, dropout: float, concat_max_chunks: int):
        super().__init__()
        if not scales:
            raise ValueError("MultiScaleChunkANN requires at least one scale")
        self.scales = list(scales)
        self.encoders = nn.ModuleDict({str(s): ChunkEncoder(feat_dim, encoder_hidden, dropout) for s in self.scales})
        n_chunks = {s: window_len // s for s in self.scales}
        self.use_concat = {s: n_chunks[s] <= concat_max_chunks for s in self.scales}
        self.pools = nn.ModuleDict({str(s): MeanMaxStdPool1d() for s in self.scales if not self.use_concat[s]})
        fusion_dim = sum(n_chunks[s] * encoder_hidden if self.use_concat[s] else 3 * encoder_hidden
                         for s in self.scales)
        self.head = nn.Sequential(
            nn.Linear(fusion_dim, head_hidden), nn.ReLU(), nn.Dropout(dropout),
            nn.Linear(head_hidden, num_classes),
        )

    def forward(self, *chunk_tensors: torch.Tensor) -> torch.Tensor:
        if len(chunk_tensors) != len(self.scales):
            raise ValueError(f"expected {len(self.scales)} chunk tensors for scales {self.scales}, got {len(chunk_tensors)}")
        parts = []
        for scale, x in zip(self.scales, chunk_tensors):
            encoded = self.encoders[str(scale)](x)
            if self.use_concat[scale]:
                parts.append(encoded.reshape(encoded.shape[0], -1))
            else:
                parts.append(self.pools[str(scale)](encoded.permute(0, 2, 1)))
        return self.head(torch.cat(parts, dim=1))


# ---------------------------------------------------------------------------
# Bundle -> predictor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChunkBundle:
    """Validated contents of one exported checkpoint."""

    model_state: dict
    scales: tuple[int, ...]
    window_len: int
    feat_dim: int
    encoder_hidden: int
    head_hidden: int
    dropout: float
    num_classes: int
    concat_max_chunks: int
    scaler_mean: dict[int, np.ndarray]
    scaler_scale: dict[int, np.ndarray]


def _infer_concat_max_chunks(payload: dict) -> int:
    """Bundles written before concat_max_chunks was recorded: recover it from
    the head's input width, the only place the concat/pool decision shows up."""
    fusion_dim = payload["model_state"]["head.0.weight"].shape[1]
    hidden, window_len = payload["encoder_hidden"], payload["window_len"]
    for limit in range(window_len + 1):
        expected = sum(
            (window_len // s) * hidden if window_len // s <= limit else 3 * hidden for s in payload["scales"])
        if expected == fusion_dim:
            return limit
    raise ValueError("head input width matches no concat/pool split of the checkpoint's scales")


def parse_bundle(payload: dict, expected_num_classes: int) -> ChunkBundle:
    """Fail loudly on anything that would make inference silently wrong --
    this is the gate model_discovery's probe relies on."""
    required = ("model_state", "scales", "window_len", "feat_dim", "encoder_hidden", "head_hidden",
                "dropout", "scaler_mean", "scaler_scale")
    missing = [key for key in required if not isinstance(payload, dict) or key not in payload]
    if missing:
        raise ValueError(f"not a chunk-ANN bundle (missing {', '.join(missing)})")

    scales = tuple(int(s) for s in payload["scales"])
    window_len, feat_dim = int(payload["window_len"]), int(payload["feat_dim"])
    if feat_dim != N_CHUNK_FEATURES:
        raise ValueError(f"bundle feat_dim={feat_dim}, runtime computes {N_CHUNK_FEATURES} chunk features")
    # A 1-sample chunk has no first difference, so the jerk features are undefined.
    bad_scales = [s for s in scales if s < 2 or window_len % s != 0]
    if bad_scales:
        raise ValueError(f"scales {bad_scales} are not >= 2 divisors of window_len={window_len}")

    mean = {int(k): np.asarray(v, dtype=np.float32) for k, v in payload["scaler_mean"].items()}
    scale = {int(k): np.asarray(v, dtype=np.float32) for k, v in payload["scaler_scale"].items()}
    for s in scales:
        if s not in mean or s not in scale:
            raise ValueError(f"no scaler for scale {s}")
        if mean[s].shape != (feat_dim,) or scale[s].shape != (feat_dim,):
            raise ValueError(f"scaler for scale {s} is not {feat_dim}-wide")

    num_classes = int(payload.get("num_classes", payload["model_state"]["head.3.weight"].shape[0]))
    if num_classes != expected_num_classes:
        raise ValueError(f"bundle has {num_classes} classes, config has {expected_num_classes}")

    return ChunkBundle(
        model_state=payload["model_state"], scales=scales, window_len=window_len, feat_dim=feat_dim,
        encoder_hidden=int(payload["encoder_hidden"]), head_hidden=int(payload["head_hidden"]),
        dropout=float(payload["dropout"]), num_classes=num_classes,
        concat_max_chunks=int(payload.get("concat_max_chunks", _infer_concat_max_chunks(payload))),
        scaler_mean=mean, scaler_scale=scale,
    )


class ChunkPredictor:
    """One loaded bundle: window (window_len, 8) -> class probabilities."""

    def __init__(self, bundle: ChunkBundle):
        self.bundle = bundle
        self.window_len = bundle.window_len
        self.model = MultiScaleChunkANN(
            list(bundle.scales), bundle.window_len, bundle.feat_dim, bundle.encoder_hidden,
            bundle.head_hidden, bundle.num_classes, bundle.dropout, bundle.concat_max_chunks)
        self.model.load_state_dict(bundle.model_state)
        self.model.eval()

    def _scaled_chunks(self, window: np.ndarray, scale: int) -> torch.Tensor:
        feats = compute_chunk_features(window, scale)
        scaled = (feats - self.bundle.scaler_mean[scale]) / self.bundle.scaler_scale[scale]
        return torch.from_numpy(scaled.astype(np.float32)[np.newaxis])

    def predict_proba(self, window: np.ndarray) -> np.ndarray:
        if window.shape[0] != self.window_len:
            raise ValueError(f"window has {window.shape[0]} samples, bundle expects {self.window_len}")
        tensors = [self._scaled_chunks(window, scale) for scale in self.bundle.scales]
        with torch.no_grad():
            return torch.softmax(self.model(*tensors), dim=1).numpy()[0]
