"""Golden test: TouchIdStreamProcessor emits exactly the recorded ReadyWindows
for a seeded synthetic stream, whatever the tick size.

Pins the v1 gating behavior across refactors. Regenerate the fixture only for
an intended v1 behavior change:
    python tests/test_gating_golden.py
"""

import hashlib
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from core.texture_piezo.application.stream_processor import TouchIdStreamProcessor
from core.texture_piezo.gating.v1.quality_gate import fit_idle_baseline

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gating_v1_golden.json"
PZT_COLUMNS = [f"PZT3_{c}" for c in "BLCRT"]
FS = 1538.2
SEED = 20260101
IDLE_MEAN = 2048.0
IDLE_NOISE_STD = 1.0
BURST_NOISE_STD = 60.0
WINDOW_SIZE_S = 0.5
HOP_SIZE_S = 0.1
SPAN_STALE_TIMEOUT_S = 1.0
IDLE_BASELINE_DURATION_S = 5.0
TICK_SIZES = (100, 154, 777)
# (idle seconds before the burst, burst seconds). 0.1 s gap: absorbed inline; 0.25 s gap: stripped
# but merged back by the grace window; 0.8 s and 1.0 s gaps: separate fragments. The 0.3 s bursts
# are shorter than a window (the first merges into a longer fragment, the last stands alone).
SCRIPT = ((1.0, 1.6), (0.1, 1.2), (0.8, 0.3), (0.25, 1.0), (1.0, 0.3), (1.0, 0.0))


def _noise(rng: np.random.Generator, n_samples: int, std: float) -> np.ndarray:
    return IDLE_MEAN + rng.normal(0.0, std, size=(n_samples, len(PZT_COLUMNS)))


def build_stream(rng: np.random.Generator) -> np.ndarray:
    parts = []
    for idle_s, burst_s in SCRIPT:
        parts.append(_noise(rng, round(idle_s * FS), IDLE_NOISE_STD))
        parts.append(_noise(rng, round(burst_s * FS), BURST_NOISE_STD))
    return np.concatenate(parts, axis=0)


def _array_digest(*arrays: np.ndarray) -> str:
    digest = hashlib.sha256()
    for array in arrays:
        digest.update(np.ascontiguousarray(array, dtype=np.float64).tobytes())
    return digest.hexdigest()


def _describe(window) -> dict:
    channels = window.window.channels
    return {
        "frag_id": window.frag_id,
        "n_samples": window.window.n_samples,
        "first_ts": float(window.window_ts[0]),
        "last_ts": float(window.window_ts[-1]),
        "digest": _array_digest(window.window_ts, *(channels[name] for name in sorted(channels))),
    }


def run_scenario(use_baseline: bool, tick_size: int) -> list[dict]:
    rng = np.random.default_rng(SEED)
    idle_stretch = _noise(rng, round(IDLE_BASELINE_DURATION_S * FS), IDLE_NOISE_STD)
    stream = build_stream(rng)
    baseline = fit_idle_baseline(idle_stretch, PZT_COLUMNS, FS) if use_baseline else None
    processor = TouchIdStreamProcessor(
        pzt_columns=PZT_COLUMNS, window_size_s=WINDOW_SIZE_S, hop_size_s=HOP_SIZE_S,
        span_stale_timeout_s=SPAN_STALE_TIMEOUT_S, idle_baseline=baseline,
    )
    described = []
    for start in range(0, len(stream), tick_size):
        chunk = stream[start:start + tick_size]
        samples = processor.filter_raw({col: chunk[:, i] for i, col in enumerate(PZT_COLUMNS)})
        timestamps = (start + np.arange(len(chunk))) / FS
        now_t = (start + len(chunk)) / FS
        described.extend(_describe(w) for w in processor.push_chunk(samples, timestamps, FS, now_t=now_t))
    return described


def build_golden() -> dict:
    return {
        f"{'baseline' if use_baseline else 'fixed_grid'}_tick{tick_size}": run_scenario(use_baseline, tick_size)
        for use_baseline in (True, False)
        for tick_size in TICK_SIZES
    }


GOLDEN = json.loads(FIXTURE_PATH.read_text()) if FIXTURE_PATH.exists() else {}


@pytest.mark.parametrize("scenario", sorted(GOLDEN))
def test_processor_reproduces_the_recorded_windows(scenario):
    use_baseline, tick = scenario.split("_tick")
    assert run_scenario(use_baseline == "baseline", int(tick)) == GOLDEN[scenario]


def test_fixture_covers_every_scenario_and_emits_windows():
    assert sorted(GOLDEN) == sorted(build_golden_keys())
    assert all(len(windows) > 0 for windows in GOLDEN.values())


def build_golden_keys() -> list[str]:
    return [f"{kind}_tick{tick}" for kind in ("baseline", "fixed_grid") for tick in TICK_SIZES]


if __name__ == "__main__":
    FIXTURE_PATH.parent.mkdir(exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps(build_golden(), indent=1))
