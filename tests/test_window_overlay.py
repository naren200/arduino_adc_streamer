import numpy as np

from gui.window_overlay import first_hop_tiles, window_id_at, window_stretches

TS = np.array([0.0, 1.0, 2.0, 10.0, 11.0, 12.0])  # seam at index 3 (idle gap 2 -> 10)


def test_window_stretches_split_at_seams():
    assert window_stretches(TS, (3,)) == [(0.0, 2.0), (10.0, 12.0)]


def test_first_hop_within_one_stretch_ends_at_next_sample():
    assert first_hop_tiles(TS, (3,), 2) == [(0.0, 2.0)]


def test_first_hop_crossing_a_seam_gives_one_tile_per_stretch():
    assert first_hop_tiles(TS, (3,), 4) == [(0.0, 2.0), (10.0, 11.0)]


def test_hop_larger_than_window_covers_everything():
    assert first_hop_tiles(TS, (3,), 99) == [(0.0, 2.0), (10.0, 12.0)]


def test_window_id_at_finds_tile_and_misses_idle():
    predictions = [
        {"start_s": 0.0, "end_s": 2.0, "window_id": 0},
        {"start_s": 10.0, "end_s": 11.0, "window_id": 1},
    ]
    assert window_id_at(predictions, 10.5) == 1
    assert window_id_at(predictions, 5.0) is None
