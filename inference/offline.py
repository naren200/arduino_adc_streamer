"""
Offline (CSV) Inference
========================
Runs the TouchID model over an already-loaded Analysis CSV snapshot, one
window at a time, mirroring the live per-hop pipeline in
InferencePanelMixin.update_touchid_display -- but iterating windows up
front over snapshot.data instead of pulling them off a RollingBuffer fed
by the serial stream.

Windowing is driven by inference/segmentation.py's ActiveSampleQueue (fed
the whole capture's worth of 0.05s micro-chunks in one pass, since this path
already has the entire snapshot loaded) instead of a fixed hop_size_s grid --
see segmentation.py's module docstring for why: a rigid grid chops real
touch events across window boundaries and silently discards whole windows
that straddle a return-to-idle.
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from ._paths import TEXTURE_PIEZO_SRC
from .classifier import TextureClassifier
from .config import InferenceConfig
from .pipeline import classify_window
from .quality_gate import MAX_WINDOW_IDLE_FRACTION, MICRO_CHUNK_S, IdleBaseline, window_idle_fraction
from .segmentation import ActiveSampleQueue

sys.path.insert(0, str(TEXTURE_PIEZO_SRC))
from causal_derived_channels import CausalDerivedChannels  # noqa: E402
from drag_detection_utils_v1 import remove_transient_blips  # noqa: E402


def run_inference_on_snapshot(
    snapshot,
    classifier: TextureClassifier,
    config: InferenceConfig,
    idle_baseline: IdleBaseline | None = None,
) -> list[dict]:
    """
    Run the same preprocess/featurize/predict pipeline as live TouchID over
    snapshot.data.

    idle_baseline, if given, drives segmentation.ActiveSampleQueue over the
    whole capture: only window_size_s-length windows it actually yields (each
    a contiguous real-signal span that never straddles a genuine
    return-to-idle) are classified -- idle stretches simply produce no
    result entries, rather than an explicit class="idle" placeholder.
    idle_baseline=None falls back to the plain fixed window_size_s/hop_size_s
    grid, classifying every window unconditionally (matches the live path's
    behavior when no baseline has been captured yet).

    Returns a list of {"start_s", "end_s", "class", "confidence", "probs"}
    dicts, one per window, in chronological order.
    """
    fs = float(snapshot.sample_rate_hz)
    if fs <= 0:
        raise ValueError("Snapshot has no valid sample rate; cannot run offline inference.")

    try:
        channel_indices = [snapshot.channel_labels.index(col) for col in config.pzt_columns]
    except ValueError as exc:
        raise ValueError(f"Snapshot is missing a configured PZT column: {exc}") from exc

    window_n = round(config.window_size_s * fs)
    hop_n = round(config.hop_size_s * fs)
    if window_n <= 0 or hop_n <= 0:
        raise ValueError("window_size_s/hop_size_s must be positive.")

    total_sweeps = snapshot.sweep_count
    if total_sweeps < window_n:
        raise ValueError(
            f"Loaded CSV has only {total_sweeps} sweeps, fewer than one window "
            f"({window_n} sweeps at {fs:.2f} Hz)."
        )

    # mode="offline" (whole-array median baseline) instead of "live" (causal
    # rolling median) -- this path already has the entire capture loaded, so
    # there's no reason to fake a live stream's trailing-window baseline. Using
    # "offline" here matches training's load_calibration_csv (also
    # mode="offline" by default), removing one source of train/inference skew.
    # Still applied once over the whole capture up front rather than per-window
    # slice, for the same reason as before: consistent baseline across windows.
    pzt_frame = pd.DataFrame(
        snapshot.data[:, channel_indices].astype(np.float64), columns=config.pzt_columns,
    )
    # remove_transient_blips is currently a no-op: texture_piezo's
    # BLIP_VOLT_THRESH=10.0 exceeds ADC_VREF_VOLTAGE=3.3, so the deviation
    # check can never trip (confirmed: 0/30.8M samples flagged across
    # data/raw/sensor_v12d_7_26/ch5). Left uncalled rather than deleted --
    # officially expected to be fixed later in texture_piezo, at which
    # point this call should be restored.
    # pzt_frame = remove_transient_blips(pzt_frame, columns=config.pzt_columns, mode="offline")
    cleaned_pzt = pzt_frame.to_numpy(dtype=np.float64)

    # Whole-file continuous causal-median-baseline + bounded-windowed-sum
    # integration AND shear/normal, matching training's
    # dd.load_and_integrate_calibration_csv (both derived once over the full
    # file, before any windowing) -- both training and this offline path now
    # share the exact same CausalDerivedChannels class, so there's no
    # separate "offline" derivation formula anymore. Computing this per-
    # window instead (the previous behavior for shear/normal here, and still
    # the live streaming fallback) restarts the causal baseline and windowed
    # sum at every window boundary, producing an artificial startup ramp not
    # present in training data -- see
    # texture_piezo/src/drag_detection_utils_v1.py's
    # load_and_integrate_calibration_csv docstring (08_analysis_v1.ipynb
    # finding, 2026-09-10). Since this path already has the whole file
    # loaded, there's no reason to pay that cost here.
    channels = CausalDerivedChannels(pzt_columns=config.pzt_columns)
    chunk_by_column = {col: cleaned_pzt[:, i] for i, col in enumerate(config.pzt_columns)}
    derived = channels.process(chunk_by_column)
    integrated_full = np.column_stack([derived["integrated"][col] for col in config.pzt_columns])
    shear_lr_full = derived["shear_lr"]
    shear_tb_full = derived["shear_tb"]
    normal_full = derived["normal"]

    def classify_at(start: int, end: int) -> dict:
        window_adc = cleaned_pzt[start:end]
        window_integrated = integrated_full[start:end]
        window_shear_lr = shear_lr_full[start:end]
        window_shear_tb = shear_tb_full[start:end]
        window_normal = normal_full[start:end]

        start_s = float(snapshot.timestamps_s[start]) if snapshot.timestamps_s.size else start / fs
        end_s = float(snapshot.timestamps_s[end - 1]) if snapshot.timestamps_s.size else (end - 1) / fs

        probs = classify_window(
            window_adc, window_integrated, window_shear_lr, window_shear_tb, window_normal,
            fs, classifier,
        )
        top_class, top_conf = max(probs.items(), key=lambda kv: kv[1])

        return {
            "start_s": start_s,
            "end_s": end_s,
            "class": top_class,
            "confidence": float(top_conf),
            "probs": probs,
        }

    if idle_baseline is None:
        # No baseline captured yet -- fall back to the plain fixed hop grid,
        # classifying every window unconditionally (matches the live path's
        # behavior when touchid_idle_baseline is None: the gate is a no-op).
        results = []
        start = 0
        last_start = -1
        while start + window_n <= total_sweeps:
            results.append(classify_at(start, start + window_n))
            last_start = start
            start += hop_n
        tail_start = total_sweeps - window_n
        if tail_start > last_start:
            results.append(classify_at(tail_start, tail_start + window_n))
        return results

    # Sample-accurate segmentation: feed the whole capture's worth of 0.05s
    # micro-chunks through ActiveSampleQueue in one pass (this path already
    # has the entire snapshot loaded, so there's no live streaming cadence to
    # match), then classify only the windows it actually yields -- each one
    # guaranteed to be window_size_s of contiguous real signal that never
    # straddles a genuine return-to-idle. See segmentation.py.
    queue = ActiveSampleQueue(
        fs=fs, window_size_s=config.window_size_s, hop_size_s=config.hop_size_s, baseline=idle_baseline,
    )
    chunk_n = max(1, round(MICRO_CHUNK_S * fs))
    idx = 0
    windows: list[tuple[int, int, int]] = []
    # Drain ready_windows() and evict_stale() every tick (not deferred to the
    # end) -- evict_stale drops finalized spans once older than
    # span_stale_timeout_s, so windows must be pulled out before that
    # cadence, matching how the live GUI panel uses this same class.
    while idx < total_sweeps:
        end = min(idx + chunk_n, total_sweeps)
        now_t = end / fs
        queue.push_micro_chunk((idx, end), cleaned_pzt[idx:end], now_t)
        idx = end
        # store_base_abs=0 -- the whole capture is loaded up front here, so
        # padding (see window_padding.py) can always reach back to sample 0.
        windows.extend(queue.ready_windows(store_base_abs=0))
        queue.evict_stale(now_t, config.min_span_fill_ratio, config.span_stale_timeout_s)

    # A merged span can fuse several genuinely separate touch events when the
    # idle gap between them is shorter than merge_gap_chunks(window_size_s) --
    # reject any individual window straddling one of those gaps (>30% idle
    # micro-chunks) even though the span itself was accepted, rather than
    # feeding a mixed-signal window into the classifier.
    accepted = [
        (start, end, span_id) for start, end, span_id in windows
        if window_idle_fraction(cleaned_pzt[start:end], idle_baseline, fs) <= MAX_WINDOW_IDLE_FRACTION
    ]
    return [classify_at(start, end) for start, end, _span_id in accepted]
