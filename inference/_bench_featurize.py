"""
Latency benchmark for the derived-channels + featurize step of the live
TouchID pipeline.

Standalone script — no live ADC connection or GUI required.

Generates synthetic 5-channel ADC windows at sample counts corresponding
to a range of live sample rates (fs), times one CausalDerivedChannels.process()
chunk call + featurize_window end-to-end over repeated calls, and compares
the mean+p95 latency against the InferenceConfig hop budget (and a tighter
plausible GUI-spinbox minimum) to flag whether the pipeline can keep up
with the hop cadence.

Run: python inference/_bench_featurize.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

# Allow `python inference/_bench_featurize.py` (run directly, not as a
# module) by putting the repo root on sys.path so `inference.*` resolves.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from inference._paths import TEXTURE_PIEZO_SRC
from inference.config import InferenceConfig

sys.path.insert(0, str(TEXTURE_PIEZO_SRC))
from causal_derived_channels import CausalDerivedChannels  # noqa: E402
from clip_windowing_utils_v1 import extract_window_features  # noqa: E402

N_REPEATS = 100
LIVE_BASELINE_WINDOW_SAMPLES = 100

# fs values spanning the observed live range (per prior investigation
# noted in the task 10 spec), plus InferenceConfig's own window sizing
# as the "typical" case.
FS_VALUES_HZ = [68.0, 150.0, 400.0, 800.0, 1200.0, 1600.0]

# A tighter plausible GUI spinbox minimum, in case task 09's spinbox
# allows finer control than the InferenceConfig default hop. No
# inference_panel.py exists yet in this repo, so this is a conservative
# guess rather than a value read from the real spinbox range.
TIGHT_HOP_CANDIDATE_S = 0.05


def _make_synthetic_window(n_samples: int, n_channels: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    # Realistic-ish ADC counts: slowly varying signal plus noise, not pure
    # white noise, so downstream filtering/wavelet code does real work.
    t = np.linspace(0, 1, n_samples)
    base = 2048 + 200 * np.sin(2 * np.pi * 3 * t)[:, None]
    noise = rng.normal(0, 30, size=(n_samples, n_channels))
    return (base + noise).astype(np.float64)


def _time_pipeline(fs: float, pzt_columns: list[str], n_repeats: int) -> list[float]:
    n_samples = max(int(round(InferenceConfig().window_size_s * fs)), 8)
    durations_ms = []
    for i in range(n_repeats):
        window_adc = _make_synthetic_window(n_samples, len(pzt_columns), seed=i)
        # Fresh CausalDerivedChannels per repeat -- this bench measures one
        # hop's worth of streaming work (process() on one new chunk +
        # featurize), not the cost of accumulated state, since real live
        # usage keeps one persistent instance across the whole session.
        channels = CausalDerivedChannels(pzt_columns=pzt_columns)
        start = time.perf_counter()
        chunk_by_column = {col: window_adc[:, j] for j, col in enumerate(pzt_columns)}
        derived = channels.process(chunk_by_column)
        window_integrated = np.column_stack([derived["integrated"][col] for col in pzt_columns])
        extract_window_features(
            window_adc, window_integrated, derived["shear_lr"], derived["shear_tb"], derived["normal"], fs,
        )
        end = time.perf_counter()
        durations_ms.append((end - start) * 1000.0)
    return durations_ms


def _mean_p95(durations_ms: list[float]) -> tuple[float, float]:
    arr = np.array(durations_ms)
    return float(np.mean(arr)), float(np.percentile(arr, 95))


def main() -> None:
    config = InferenceConfig()
    pzt_columns = config.pzt_columns
    hop_budget_ms = config.hop_size_s * 1000.0
    tight_hop_budget_ms = TIGHT_HOP_CANDIDATE_S * 1000.0

    print(f"InferenceConfig.window_size_s = {config.window_size_s}s")
    print(f"InferenceConfig.hop_size_s (reference hop budget) = {config.hop_size_s}s "
          f"({hop_budget_ms:.1f} ms)")
    print(f"Tighter plausible spinbox minimum = {TIGHT_HOP_CANDIDATE_S}s "
          f"({tight_hop_budget_ms:.1f} ms)")
    print(f"N repeats per fs = {N_REPEATS}\n")

    header = f"{'fs (Hz)':>10} | {'n_samples':>9} | {'mean (ms)':>10} | {'p95 (ms)':>9} | {'mean+p95 (ms)':>13} | flags"
    print(header)
    print("-" * len(header))

    any_hop_violation = False
    any_tight_violation = False

    for fs in FS_VALUES_HZ:
        n_samples = max(int(round(config.window_size_s * fs)), 8)
        durations_ms = _time_pipeline(fs, pzt_columns, N_REPEATS)
        mean_ms, p95_ms = _mean_p95(durations_ms)
        combined_ms = mean_ms + p95_ms

        flags = []
        if combined_ms > hop_budget_ms:
            flags.append("EXCEEDS hop_size_s budget")
            any_hop_violation = True
        if combined_ms > tight_hop_budget_ms:
            flags.append("EXCEEDS tight spinbox-min budget")
            any_tight_violation = True
        flag_str = "; ".join(flags) if flags else "OK"

        print(f"{fs:>10.1f} | {n_samples:>9d} | {mean_ms:>10.3f} | {p95_ms:>9.3f} | "
              f"{combined_ms:>13.3f} | {flag_str}")

    print()
    if any_hop_violation:
        print("FAIL: mean+p95 latency exceeds InferenceConfig.hop_size_s "
              f"({hop_budget_ms:.1f} ms) at one or more tested fs values. "
              "Feature extraction may fall behind the hop cadence on the UI/timer "
              "thread; consider moving it to a worker QThread.")
    else:
        print("PASS: mean+p95 latency stays within InferenceConfig.hop_size_s "
              f"({hop_budget_ms:.1f} ms) at all tested fs values.")

    if any_tight_violation:
        print(f"NOTE: mean+p95 latency exceeds the tighter {TIGHT_HOP_CANDIDATE_S}s "
              f"({tight_hop_budget_ms:.1f} ms) candidate spinbox minimum at one or more "
              "tested fs values. If task 09's GUI spinbox allows hops this small, its "
              "minimum should be raised, or feature extraction should move off the "
              "UI/timer thread into a worker QThread.")
    else:
        print(f"mean+p95 latency also stays within the tighter {TIGHT_HOP_CANDIDATE_S}s "
              f"({tight_hop_budget_ms:.1f} ms) candidate spinbox minimum at all tested fs values.")


if __name__ == "__main__":
    main()
