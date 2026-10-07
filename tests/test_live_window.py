import numpy as np
import pytest

from core.piezo_engine import channel_names as names
from core.piezo_engine.live_window import LiveWindow

RATE_HZ = 1000.0
N = 50


def _window() -> LiveWindow:
    return LiveWindow({"PZT3_B": np.arange(N, dtype=float), "PZT3_L": np.ones(N)}, RATE_HZ)


def test_engine_channel_names_cover_all_groups():
    assert names.ENGINE_CHANNEL_NAMES == set(
        names.PZT_COLUMNS + names.INTEGRATED_COLUMNS + names.SHEAR_NORMAL_COLUMNS + names.FORCE_COLUMNS)
    assert names.INTEGRATED_COLUMNS[0] == "PZT3_B_integrated"


def test_basic_properties():
    window = _window()
    assert window.n_samples == N
    assert window.duration_s == pytest.approx(N / RATE_HZ)


def test_arrays_and_mapping_are_immutable_and_decoupled_from_input():
    source = np.arange(N, dtype=float)
    window = LiveWindow({"PZT3_B": source}, RATE_HZ)
    source[0] = 99.0
    assert window.channels["PZT3_B"][0] == 0.0
    with pytest.raises(ValueError):
        window.channels["PZT3_B"][0] = 1.0
    with pytest.raises(TypeError):
        window.channels["PZT3_L"] = np.zeros(N)
    with pytest.raises(AttributeError):
        window.sample_rate_hz = 5.0


@pytest.mark.parametrize("channels", [
    {},
    {"bogus": np.zeros(N)},
    {"PZT3_B": np.zeros((N, 2))},
    {"PZT3_B": np.zeros(N, dtype=int)},
    {"PZT3_B": np.zeros(N), "PZT3_L": np.zeros(N + 1)},
    {"PZT3_B": np.zeros(0)},
])
def test_invalid_channels_raise(channels):
    with pytest.raises(ValueError):
        LiveWindow(channels, RATE_HZ)


@pytest.mark.parametrize("rate", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_sample_rate_raises(rate):
    with pytest.raises(ValueError, match="sample_rate_hz"):
        LiveWindow({"PZT3_B": np.zeros(N)}, rate)


def test_require_lists_all_missing_channels():
    window = _window()
    window.require(["PZT3_B"])
    with pytest.raises(KeyError) as excinfo:
        window.require(["PZT3_B", "shear_force_lr", "normal_force"])
    assert "shear_force_lr" in str(excinfo.value) and "normal_force" in str(excinfo.value)


def test_stack_orders_columns_by_request_and_selects_dtype():
    window = _window()
    stacked = window.stack(["PZT3_L", "PZT3_B"])
    assert stacked.shape == (N, 2) and stacked.dtype == np.float64
    np.testing.assert_array_equal(stacked[:, 0], np.ones(N))
    assert window.stack(["PZT3_B"], dtype=np.float32).dtype == np.float32
    with pytest.raises(KeyError):
        window.stack(["PZT3_R"])
    with pytest.raises(ValueError):
        window.stack(["PZT3_B"], dtype=np.int32)


def test_from_stacked_round_trip():
    order = list(names.PZT_COLUMNS + names.SHEAR_NORMAL_COLUMNS)
    matrix = np.random.default_rng(0).normal(size=(N, len(order)))
    window = LiveWindow.from_stacked(matrix, order, RATE_HZ)
    np.testing.assert_array_equal(window.stack(order), matrix)
    assert window.sample_rate_hz == RATE_HZ


def test_from_stacked_rejects_shape_mismatch_and_duplicates():
    with pytest.raises(ValueError):
        LiveWindow.from_stacked(np.zeros((N, 3)), ["PZT3_B", "PZT3_L"], RATE_HZ)
    with pytest.raises(ValueError, match="duplicate"):
        LiveWindow.from_stacked(np.zeros((N, 2)), ["PZT3_B", "PZT3_B"], RATE_HZ)


def test_channel_names_module_is_stdlib_only():
    import ast
    import sys
    from pathlib import Path

    tree = ast.parse(Path(names.__file__).read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= set(sys.stdlib_module_names)
