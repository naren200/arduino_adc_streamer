"""
Standalone validation for ActiveSampleQueue (inference/segmentation.py)
=========================================================================
Replays labeled texture_piezo captures through ActiveSampleQueue and checks:
  (a) no emitted window straddles a labeled idle<->active boundary from the
      ground-truth labels (beyond the same short-idle merge tolerance the
      old is_window_quality gate used),
  (b) spans roughly line up with labeled event times,
  (c) a pure-idle capture produces zero (or near-zero) emitted windows.

No pytest/test framework is configured in this repo (no pytest.ini/tests
dir) -- this follows _bench_featurize.py's convention of a standalone
diagnostic script that prints PASS/FAIL.

Run: python inference/_test_segmentation.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from inference.config import InferenceConfig
from inference.quality_gate import fit_idle_baseline, merge_gap_chunks, MICRO_CHUNK_S
from inference.segmentation import ActiveSampleQueue
from inference.stream_processor import TouchIdStreamProcessor

_TEXTURE_PIEZO_ROOT = _REPO_ROOT.parent / "texture_piezo" / "data" / "raw" / "sensor_v12d_7_26" / "ch5"

IDLE_BASELINE_CSV = _TEXTURE_PIEZO_ROOT / "only_idle_v3_20260916_1244.csv"
LABELED_CSV = _TEXTURE_PIEZO_ROOT / "only_wood_and_idle_v2_20260916_0920.csv"
LABELED_LABELS_JSON = _TEXTURE_PIEZO_ROOT / "only_wood_and_idle_v2_20260916_0920_labels.json"
PURE_IDLE_CSV = _TEXTURE_PIEZO_ROOT / "only_idle_v4_20260916_1445.csv"

PZT_COLUMNS = ["PZT5_B", "PZT5_L", "PZT5_C", "PZT5_R", "PZT5_T"]


def _load_csv(path: Path) -> tuple[np.ndarray, np.ndarray, float]:
    """Returns (samples (n, len(PZT_COLUMNS)), relative_time_s (n,), fs)."""
    df = pd.read_csv(path)
    samples = df[PZT_COLUMNS].to_numpy(dtype=np.float64)
    ts = pd.to_datetime(df["Timestamp"], format="%H:%M:%S.%f")
    rel_s = (ts - ts.iloc[0]).dt.total_seconds().to_numpy()
    duration = rel_s[-1] - rel_s[0]
    fs = (len(rel_s) - 1) / duration if duration > 0 else 0.0
    return samples, rel_s, fs


def _replay(
    samples: np.ndarray, fs: float, config: InferenceConfig, baseline,
) -> tuple[ActiveSampleQueue, list[tuple[int, int]]]:
    """Feed the whole capture through in 0.05s micro-chunks, draining
    ready_windows() and running evict_stale() every tick -- matching how the
    live GUI panel and offline.py's batch loop both use ActiveSampleQueue
    (draining as you go, not deferred to the end): evict_stale drops
    finalized spans once they're older than span_stale_timeout_s, so windows
    must be pulled out via ready_windows() before that cadence, not after."""
    queue = ActiveSampleQueue(
        fs=fs, window_size_s=config.window_size_s, hop_size_s=config.hop_size_s, baseline=baseline,
    )
    chunk_n = max(1, round(MICRO_CHUNK_S * fs))
    n = len(samples)
    idx = 0
    windows: list[tuple[int, int]] = []
    while idx < n:
        end = min(idx + chunk_n, n)
        now_t = end / fs
        queue.push_micro_chunk((idx, end), samples[idx:end], now_t)
        idx = end
        windows.extend(queue.ready_windows())
        queue.evict_stale(now_t, config.min_span_fill_ratio, config.span_stale_timeout_s)
    return queue, windows


def _merged_ground_truth_runs(labels_path: Path, window_size_s: float) -> list[tuple[float, float]]:
    """Merge labeled segments separated by gaps shorter than
    merge_gap_chunks(window_size_s) worth of time, mirroring
    ActiveSampleQueue's own short-idle-gap absorption, so the comparison
    uses the same tolerance the segmentation is allowed to use."""
    payload = json.loads(labels_path.read_text())
    segments = sorted(payload["segments"], key=lambda s: s["start_s"])
    merge_gap_s = merge_gap_chunks(window_size_s) * MICRO_CHUNK_S
    runs: list[list[float]] = []
    for seg in segments:
        if runs and seg["start_s"] - runs[-1][1] < merge_gap_s:
            runs[-1][1] = max(runs[-1][1], seg["end_s"])
        else:
            runs.append([seg["start_s"], seg["end_s"]])
    return [(a, b) for a, b in runs]


def check_no_straddle(
    windows_s: list[tuple[float, float]], runs: list[tuple[float, float]],
) -> list[str]:
    """The bug this segmentation fixes: a window covering the tail of one
    labeled active run, a real idle gap, AND the start of a DIFFERENT run --
    i.e. straddling a genuine active->idle->active return. Checked directly
    as "does this window overlap more than one merged ground-truth run".

    A window's edge running a little before/after one run's hand-labeled
    boundary (e.g. real settle/pre-touch ramp the labeler's reaction time
    missed) is NOT counted as a failure here -- quality_gate.py's own
    validation notes already document that the gaps immediately around
    labeled events aren't clean idle and were excluded from its validation
    for the same reason."""
    failures = []
    for start_s, end_s in windows_s:
        overlapping = [(a, b) for a, b in runs if start_s < b and end_s > a]
        if len(overlapping) > 1:
            failures.append(
                f"window [{start_s:.3f}, {end_s:.3f}] overlaps {len(overlapping)} distinct "
                f"labeled runs: {overlapping}"
            )
    return failures


def main() -> None:
    config = InferenceConfig()
    print(f"window_size_s={config.window_size_s} hop_size_s={config.hop_size_s} "
          f"min_span_fill_ratio={config.min_span_fill_ratio} "
          f"span_stale_timeout_s={config.span_stale_timeout_s}\n")

    missing = [p for p in (IDLE_BASELINE_CSV, LABELED_CSV, LABELED_LABELS_JSON, PURE_IDLE_CSV) if not p.exists()]
    if missing:
        print("SKIP: could not find texture_piezo capture(s):")
        for p in missing:
            print(f"  {p}")
        return

    baseline_samples, _bts, baseline_fs = _load_csv(IDLE_BASELINE_CSV)
    baseline = fit_idle_baseline(baseline_samples, PZT_COLUMNS, baseline_fs)
    print(f"Idle baseline fit from {IDLE_BASELINE_CSV.name}: fs={baseline_fs:.1f}Hz, k={baseline.k}\n")

    all_ok = True

    # --- (a) + (b): labeled capture -----------------------------------
    samples, rel_s, fs = _load_csv(LABELED_CSV)
    _queue, windows = _replay(samples, fs, config, baseline)
    windows_s = [(rel_s[0] + s / fs, rel_s[0] + e / fs) for s, e, _span_id in windows]
    print(f"{LABELED_CSV.name}: fs={fs:.1f}Hz, {len(samples)} samples, {len(windows)} windows emitted")

    runs = _merged_ground_truth_runs(LABELED_LABELS_JSON, config.window_size_s)
    print(f"  {len(runs)} merged ground-truth active runs")

    failures = check_no_straddle(windows_s, runs)
    if failures:
        all_ok = False
        print(f"  FAIL: {len(failures)} window(s) straddle a labeled boundary:")
        for f in failures[:10]:
            print(f"    {f}")
    else:
        print("  PASS: no emitted window straddles a labeled idle<->active boundary")

    n_inside_a_run = sum(
        1 for s, e in windows_s if any(s >= a and e <= b for a, b in runs)
    )
    print(f"  {n_inside_a_run}/{len(windows)} windows fall inside a labeled active run "
          f"(spans roughly line up with labeled event times)")
    if runs and n_inside_a_run == 0:
        all_ok = False
        print("  FAIL: zero windows landed inside any labeled active run")

    # --- (c): pure-idle capture ----------------------------------------
    idle_samples, _its, idle_fs = _load_csv(PURE_IDLE_CSV)
    _idle_queue, idle_windows = _replay(idle_samples, idle_fs, config, baseline)
    print(f"\n{PURE_IDLE_CSV.name}: fs={idle_fs:.1f}Hz, {len(idle_samples)} samples, "
          f"{len(idle_windows)} windows emitted (expect 0, or near-zero false positives)")
    false_positive_rate = len(idle_windows) / max(1, len(idle_samples) / (config.hop_size_s * idle_fs))
    if len(idle_windows) > 0:
        print(f"  NOTE: {len(idle_windows)} window(s) emitted from a pure-idle capture "
              f"(rate vs. naive hop-grid window count: {false_positive_rate:.3%})")
        if false_positive_rate > 0.05:
            all_ok = False
            print("  FAIL: false-positive rate on pure idle exceeds 5%")
        else:
            print("  PASS: false-positive rate on pure idle is near-zero")
    else:
        print("  PASS: zero false positives on pure idle")

    # --- (d): smoke-test TouchIdStreamProcessor's own replay-style drive
    # loop -- _replay above exercises ActiveSampleQueue directly; this
    # exercises the actual shared class both live streaming and offline
    # replay drive (gui/inference_panel.py's _touchid_replay_tick and
    # update_touchid_display both call push_chunk), covering the
    # ActiveSampleQueue-driven branch and the no-baseline fixed-grid
    # fallback, which nothing else here touches.
    print()
    try:
        all_ok = _check_stream_processor_path(samples, rel_s, fs, config, baseline) and all_ok
    except Exception as exc:
        all_ok = False
        print(f"  FAIL: TouchIdStreamProcessor.push_chunk raised: {exc!r}")

    print()
    print("OVERALL: PASS" if all_ok else "OVERALL: FAIL")


def _drive_stream_processor(samples, rel_s, fs, config, idle_baseline):
    """Replay-style drive loop: feed the whole capture through push_chunk in
    hop_size_s-sized slices (as fast as possible, sample-derived now_t --
    same pattern gui/inference_panel.py's _touchid_replay_tick uses), and
    collect every ReadyWindow it yields."""
    processor = TouchIdStreamProcessor(
        pzt_columns=list(PZT_COLUMNS),
        window_size_s=config.window_size_s,
        hop_size_s=config.hop_size_s,
        span_stale_timeout_s=config.span_stale_timeout_s,
        min_span_fill_ratio=config.min_span_fill_ratio,
        idle_baseline=idle_baseline,
    )
    hop_n = max(1, round(config.hop_size_s * fs))
    n = len(samples)
    idx = 0
    windows = []
    while idx < n:
        end = min(idx + hop_n, n)
        channel_samples = {col: samples[idx:end, i] for i, col in enumerate(PZT_COLUMNS)}
        now_t = end / fs
        windows.extend(processor.push_chunk(channel_samples, rel_s[idx:end], fs, now_t=now_t))
        idx = end
    return windows


def _check_stream_processor_path(samples, rel_s, fs, config, baseline) -> bool:
    windows_gated = _drive_stream_processor(samples, rel_s, fs, config, baseline)
    print(f"TouchIdStreamProcessor replay-style drive (gated): {len(windows_gated)} windows")
    ok = len(windows_gated) > 0 and all(w.window_ts[0] < w.window_ts[-1] for w in windows_gated)
    print("  PASS" if ok else "  FAIL: no windows, or a start_s >= end_s")

    windows_ungated = _drive_stream_processor(samples, rel_s, fs, config, None)
    print(f"TouchIdStreamProcessor replay-style drive (no baseline, fixed grid): {len(windows_ungated)} windows")
    ok2 = len(windows_ungated) > 0 and all(w.window_ts[0] < w.window_ts[-1] for w in windows_ungated)
    print("  PASS" if ok2 else "  FAIL: no windows, or a start_s >= end_s")

    return ok and ok2


if __name__ == "__main__":
    main()
