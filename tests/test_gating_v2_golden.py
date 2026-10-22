"""Golden test: TouchIdStreamProcessor with gating v2 emits exactly the recorded windows for a
seeded synthetic stream, whatever the tick size.

Pins the v2 gating behavior across refactors. Regenerate the fixture only for an intended v2
behavior change, and review the printed ranges by hand before committing it:
    python tests/test_gating_v2_golden.py
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
from gating_v2_support import make_v2_config

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gating_v2_golden.json"
PZT_COLUMNS = [f"PZT3_{c}" for c in "BLCRT"]
FS = 1538.2
SEED = 20260102
IDLE_MEAN = 2048.0
IDLE_NOISE_STD = 1.0
BURST_NOISE_STD = 60.0
BURST_MIN = 0.2  # idle blocks score ~0.02, burst blocks ~1 (measured on this noise)
SUSTAIN_MIN = 1
WINDOW_SIZE_S = 0.5
HOP_SIZE_S = 0.1
SPAN_STALE_TIMEOUT_S = 1.0
IDLE_BASELINE_DURATION_S = 5.0
TICK_SIZES = (100, 154, 777)
# (idle seconds before the burst, burst seconds): a 0.3 s burst alone is shorter than a window
# (only concatenation can use it), the 0.25 s gap lets concatenation bridge two bursts.
SCRIPT = ((1.0, 1.6), (0.3, 0.3), (0.25, 0.3), (1.0, 0.7), (1.0, 0.0))
SCENARIOS = {
    "strength_concat": dict(idle_gate_enabled=False, strength_gate_enabled=True, concat_enabled=True),
    "strength_single_range": dict(idle_gate_enabled=False, strength_gate_enabled=True, concat_enabled=False),
    "strength_and_idle_concat": dict(idle_gate_enabled=True, strength_gate_enabled=True, concat_enabled=True),
}


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
        "ranges": [list(r) for r in window.ranges],
        "seam_idx": list(window.window.seam_idx),
        "n_samples": window.window.n_samples,
        "digest": _array_digest(window.window_ts, *(channels[name] for name in sorted(channels))),
    }


def run_scenario(scenario: str, tick_size: int) -> list[dict]:
    rng = np.random.default_rng(SEED)
    idle_stretch = _noise(rng, round(IDLE_BASELINE_DURATION_S * FS), IDLE_NOISE_STD)
    stream = build_stream(rng)
    config = make_v2_config(burst_min=BURST_MIN, sustain_min=SUSTAIN_MIN, **SCENARIOS[scenario])
    processor = TouchIdStreamProcessor(
        pzt_columns=PZT_COLUMNS, window_size_s=WINDOW_SIZE_S, hop_size_s=HOP_SIZE_S,
        span_stale_timeout_s=SPAN_STALE_TIMEOUT_S, idle_baseline=fit_idle_baseline(idle_stretch, PZT_COLUMNS, FS),
        gating_version="v2", gating_v2_config=config,
    )
    described = []
    for start in range(0, len(stream), tick_size):
        chunk = stream[start:start + tick_size]
        samples = processor.filter_raw({col: chunk[:, i] for i, col in enumerate(PZT_COLUMNS)})
        timestamps = (start + np.arange(len(chunk))) / FS
        described.extend(_describe(w) for w in processor.push_chunk(samples, timestamps, FS, now_t=0.0))
    return described


def build_golden() -> dict:
    return {
        f"{scenario}_tick{tick_size}": run_scenario(scenario, tick_size)
        for scenario in SCENARIOS
        for tick_size in TICK_SIZES
    }


GOLDEN = json.loads(FIXTURE_PATH.read_text()) if FIXTURE_PATH.exists() else {}


@pytest.mark.parametrize("scenario", sorted(GOLDEN))
def test_processor_reproduces_the_recorded_windows(scenario):
    name, tick = scenario.rsplit("_tick", 1)
    assert run_scenario(name, int(tick)) == GOLDEN[scenario]


def test_fixture_covers_every_scenario_and_emits_windows():
    assert sorted(GOLDEN) == sorted(f"{name}_tick{tick}" for name in SCENARIOS for tick in TICK_SIZES)
    assert all(len(windows) > 0 for windows in GOLDEN.values())


def test_tick_size_does_not_change_the_windows():
    for name in SCENARIOS:
        by_tick = [GOLDEN[f"{name}_tick{tick}"] for tick in TICK_SIZES]
        assert by_tick[0] == by_tick[1] == by_tick[2]


if __name__ == "__main__":
    FIXTURE_PATH.parent.mkdir(exist_ok=True)
    golden = build_golden()
    FIXTURE_PATH.write_text(json.dumps(golden, indent=1))
    for key, windows in golden.items():
        print(key, [(w["frag_id"], w["ranges"], w["seam_idx"]) for w in windows])
