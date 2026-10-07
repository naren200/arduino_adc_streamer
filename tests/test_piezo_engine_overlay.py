"""Engine overlay primitives (``TrailingWindowSum``, ``JerkOverlayStage``): window sums, chunk invariance,
warmup dropping and the running-median baseline."""

import numpy as np
import pytest

from core.piezo_engine.force_stage import ExpandingMedianCentering, TrailingMeanStage, TrailingWindowSum
from core.piezo_engine.overlay import JerkOverlayStage
from core.piezo_engine.shear_constants import SHEAR_SENSOR_POSITIONS


def _bits(values: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(values, dtype=np.float64).view(np.int64)


def _assert_bit_equal(actual: np.ndarray, expected: np.ndarray, message: str) -> None:
    assert actual.shape == expected.shape, message
    assert np.array_equal(_bits(actual), _bits(expected)), message


@pytest.mark.parametrize("window", [1, 6, 30])
def test_trailing_mean_stage_is_the_window_sum_over_the_covered_sample_count(window):
    values = np.random.default_rng(1).normal(size=500)
    sums, counts = TrailingWindowSum(window).push(values)
    _assert_bit_equal(TrailingMeanStage(window).push(values), sums / counts, "mean")
    assert counts[0] == 1 and counts[-1] == window


def test_window_sum_state_carries_across_chunks_bit_exactly():
    values = np.random.default_rng(2).normal(size=3000)
    whole, _ = TrailingWindowSum(30).push(values)
    stage, parts, start = TrailingWindowSum(30), [], 0
    for size in (1, 1, 2, 28, 31, 500, 1, 2436):
        parts.append(stage.push(values[start:start + size])[0])
        start += size
    _assert_bit_equal(np.concatenate(parts), whole, "chunked window sum")


def test_window_sum_known_values_window_two():
    # centered series 0, 0, 10, 10 (median of 0,0,10,10 prefixes -> 0, 0, 0, 5 -> centered 0, 0, 10, 5)
    centered = ExpandingMedianCentering(positions=("C",)).push({"C": np.array([0.0, 0.0, 10.0, 10.0])})["C"]
    sums, _counts = TrailingWindowSum(2).push(centered)
    np.testing.assert_allclose(sums[1:], [0.0, 10.0, 15.0])


# ---------------------------------------------------------------- JerkOverlayStage

def _volts(n_samples: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    return {position: 1.0 + 0.05 * rng.normal(size=n_samples) for position in SHEAR_SENSOR_POSITIONS}


def _push_in_chunks(stage: JerkOverlayStage, volts: dict, sizes: list[int]) -> list:
    results, start = [], 0
    for size in sizes:
        results.append(stage.push({p: v[start:start + size] for p, v in volts.items()}))
        start += size
    return results


def test_overlay_is_chunk_invariant_and_drops_exactly_window_minus_one_leading_samples():
    n_samples, window = 700, 30
    volts = _volts(n_samples, 3)
    whole = _push_in_chunks(JerkOverlayStage(window), volts, [n_samples])[0]
    assert whole.dropped_leading == window - 1 and whole.n_samples == n_samples - (window - 1)
    chunked = _push_in_chunks(JerkOverlayStage(window), volts, [1] * 40 + [5, 100, 1, 554])
    assert sum(r.dropped_leading for r in chunked) == window - 1
    for name in ("shear_jerk_lr", "shear_jerk_tb", "normal_jerk"):
        _assert_bit_equal(np.concatenate([getattr(r, name) for r in chunked]), getattr(whole, name), name)
    for position in SHEAR_SENSOR_POSITIONS:
        _assert_bit_equal(
            np.concatenate([r.baseline_removed[position] for r in chunked]), whole.baseline_removed[position], position)


def test_overlay_shorter_than_the_window_is_empty_not_partial():
    result = _push_in_chunks(JerkOverlayStage(5), _volts(2, 4), [2])[0]
    assert result.n_samples == 0 and result.dropped_leading == 2
    assert all(values.size == 0 for values in result.baseline_removed.values())


def test_overlay_window_must_be_positive_and_reset_restarts_the_stream():
    with pytest.raises(ValueError):
        JerkOverlayStage(0)
    volts = _volts(300, 5)
    stage = JerkOverlayStage(10)
    first = _push_in_chunks(stage, volts, [300])[0]
    stage.reset()
    again = _push_in_chunks(stage, volts, [300])[0]
    _assert_bit_equal(again.normal_jerk, first.normal_jerk, "after reset")


def test_overlay_baseline_removed_is_volts_minus_the_running_median():
    volts = _volts(120, 6)
    result = _push_in_chunks(JerkOverlayStage(1), volts, [120])[0]
    for position, values in volts.items():
        running = np.array([np.median(values[:i + 1]) for i in range(len(values))])
        _assert_bit_equal(result.baseline_removed[position], values - running, position)


def test_overlay_is_a_window_sum_not_the_engine_windowed_mean_jerk():
    """The overlay scales with the window (a SUM of centered samples); the engine's jerk feature is a windowed
    MEAN minus the current median. They are different signals -- proven numerically in P2b_report.md."""
    n_samples = 400
    volts = {position: np.full(n_samples, 1.0) for position in SHEAR_SENSOR_POSITIONS}
    volts["C"] = volts["C"].copy()
    volts["C"][100:] += 0.01  # a step the running median absorbs only slowly
    narrow = _push_in_chunks(JerkOverlayStage(5), volts, [n_samples])[0]
    wide = _push_in_chunks(JerkOverlayStage(50), volts, [n_samples])[0]
    assert np.abs(wide.normal_jerk).max() > 5 * np.abs(narrow.normal_jerk).max()
