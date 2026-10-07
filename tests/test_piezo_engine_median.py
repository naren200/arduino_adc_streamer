"""CausalMedianN: parity with the GUI ingest filter, the legacy median-3, and chunk invariance."""

import math

import numpy as np
import pytest

from core.piezo_engine.median import (
    CausalMedianN,
    MEDIAN_WINDOW_MAX_SAMPLES,
    MEDIAN_WINDOW_MIN_SAMPLES,
    normalize_median_window,
    validate_median_window,
)
from data_processing.pzt_blip_filter import PztBlipFilterMixin

LEGACY_LEADING_EDGE_SAMPLES = 2
ADC_FULL_SCALE = 4096


class _MixinHost(PztBlipFilterMixin):
    def __init__(self, window_samples: int) -> None:
        self._init_pzt_blip_filter_state()
        self.pzt_blip_filter_window_samples = window_samples


class _LegacyCausalMedian3:
    """The removed core ``_CausalMedian3`` (scalar, selection-based), kept as an independent reference."""

    def __init__(self) -> None:
        self._history: list[float] = []

    def push(self, value: float) -> float:
        value = float(value)
        history = self._history
        if not history:
            filtered = value
        elif len(history) == 1:
            filtered = (history[0] + value) / 2.0
        else:
            x, y, z = history[0], history[1], value
            if x > y:
                x, y = y, x
            if y > z:
                y, z = z, y
            if x > y:
                x, y = y, x
            filtered = y
        self._history = (history + [value])[-2:]
        return filtered


def _random_columns(rng, n_samples: int, n_columns: int = 3) -> np.ndarray:
    return rng.integers(0, ADC_FULL_SCALE, size=(n_samples, n_columns)).astype(np.float32)


def _random_chunk_sizes(rng, total: int, window: int) -> list[int]:
    sizes: list[int] = []
    while sum(sizes) < total:
        sizes.append(int(rng.choice([1, 1, 2, window - 1, window, window + 1, 17, 64])))
    sizes[-1] -= sum(sizes) - total
    return [s for s in sizes if s > 0]


def _run_chunked(stage, data: np.ndarray, sizes: list[int]) -> np.ndarray:
    out, start = [], 0
    for size in sizes:
        out.append(stage.process(data[start:start + size]))
        start += size
    return np.concatenate(out, axis=0)


@pytest.mark.parametrize("window", [3, 5, 7])
def test_bit_identical_to_gui_mixin_on_float32_values(window):
    """The mixin works in float32, the engine in float64. An odd-count median is an
    exact window element, so engine(float64(x)) == float64(mixin(float32 x)) exactly."""
    rng = np.random.default_rng(window)
    data = _random_columns(rng, 600)
    sizes = _random_chunk_sizes(rng, len(data), window)
    host = _MixinHost(window)
    mixin_out, start = [], 0
    for size in sizes:
        mixin_out.append(host._pzt_blip_filtered_columns(data[start:start + size]))
        start += size
    engine_out = _run_chunked(CausalMedianN(window, n_columns=3), data.astype(np.float64), sizes)
    assert np.array_equal(np.concatenate(mixin_out).astype(np.float64), engine_out)


def test_matches_legacy_median3_after_first_two_samples():
    rng = np.random.default_rng(11)
    values = _random_columns(rng, 500, 1)[:, 0].astype(np.float64)
    legacy = _LegacyCausalMedian3()
    expected = np.array([legacy.push(v) for v in values])
    got = CausalMedianN(3).process(values)
    assert np.array_equal(got[LEGACY_LEADING_EDGE_SAMPLES:], expected[LEGACY_LEADING_EDGE_SAMPLES:])


def test_first_window_minus_one_samples_pass_through_raw():
    values = np.array([9.0, 1.0, 5.0, 100.0, 5.0])
    got = CausalMedianN(5).process(values)
    assert np.array_equal(got[:4], values[:4])
    assert got[4] == 5.0


@pytest.mark.parametrize("window", [3, 5, 15])
def test_chunk_invariance_one_call_vs_many(window):
    rng = np.random.default_rng(100 + window)
    data = rng.normal(size=(400, 2))
    whole = CausalMedianN(window, n_columns=2).process(data)
    chunked = _run_chunked(CausalMedianN(window, n_columns=2), data, _random_chunk_sizes(rng, 400, window))
    assert np.array_equal(whole, chunked)


def test_reset_restarts_leading_edge():
    stage = CausalMedianN(3)
    stage.process(np.array([1.0, 2.0, 3.0]))
    stage.reset()
    assert np.array_equal(stage.process(np.array([7.0, 1.0])), np.array([7.0, 1.0]))


def test_nan_poisons_its_window_like_numpy_median():
    out = CausalMedianN(3).process(np.array([1.0, 2.0, np.nan, 4.0, 5.0, 6.0]))
    assert math.isnan(out[2]) and math.isnan(out[3]) and math.isnan(out[4]) and out[5] == 5.0


@pytest.mark.parametrize("bad", [2, 4, 1, 17, 0])
def test_validate_rejects_what_normalize_would_alter(bad):
    with pytest.raises(ValueError):
        validate_median_window(bad)
    with pytest.raises(ValueError):
        CausalMedianN(bad)


def test_normalize_clamps_and_forces_odd():
    assert normalize_median_window(2) == MEDIAN_WINDOW_MIN_SAMPLES
    assert normalize_median_window(4) == 5
    assert normalize_median_window(99) == MEDIAN_WINDOW_MAX_SAMPLES


def test_wrong_column_count_raises():
    with pytest.raises(ValueError):
        CausalMedianN(3, n_columns=2).process(np.zeros((4, 3)))
