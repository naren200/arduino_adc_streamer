import unittest

import numpy as np

from data_processing.pzt_blip_filter import PztBlipFilterMixin


class PztBlipFilterHarness(PztBlipFilterMixin):
    def __init__(self, *, pzt_rs=False, channels=None, repeat=1):
        self.config = {
            'channels': list(channels or [0, 1, 2]),
            'repeat': repeat,
        }
        self._pzt_rs = pzt_rs
        self._init_pzt_blip_filter_state()

    def is_array_pzt1_mode(self):
        return not self._pzt_rs

    def is_array_pzt_rs_mode(self):
        return self._pzt_rs

    def get_channels_for_arduino_command(self):
        return self.config['channels']

    def get_array_selected_sensor_groups(self):
        if not self._pzt_rs:
            return []
        return [{'sensor_id': 'PZT1'}]

    # Minimal stand-in for PztGhostRemovalMixin._get_pzt_ghost_groups: treat
    # every column as one PZT group in PZT1 mode, matching the 2-mux layout
    # for the small channel counts used in these tests.
    def _get_pzt_ghost_groups(self, samples_per_sweep):
        width = int(samples_per_sweep)
        if self._pzt_rs:
            return []
        return [list(range(width))] if width else []


def causal_median3_reference(values: np.ndarray) -> np.ndarray:
    """Independent, non-vectorized reference implementation for one column."""
    out = np.empty_like(values, dtype=np.float64)
    history: list[float] = []
    for i, value in enumerate(values):
        history_i = (history + [value])[-3:]
        if len(history_i) < 3:
            out[i] = value
        else:
            out[i] = float(np.median(history_i))
        history = (history + [value])[-2:]
    return out


class PztBlipFilterTests(unittest.TestCase):
    def test_isolated_spike_is_rejected(self):
        harness = PztBlipFilterHarness(channels=[0])
        values = np.array([0.0, 0.0, 5.0, 0.0, 0.0], dtype=np.float32)
        block = values.reshape(-1, 1)
        archive = block.copy()

        filtered_block, filtered_archive = harness.prepare_pzt_blip_filter_blocks(block, archive)

        # First two samples have no full window yet and pass through raw;
        # the spike at index 2 is outvoted by its neighbours from index 2 on.
        expected = causal_median3_reference(values).reshape(-1, 1).astype(np.float32)
        np.testing.assert_allclose(filtered_block, expected)
        np.testing.assert_allclose(filtered_archive, expected)

    def test_state_carries_across_blocks(self):
        harness = PztBlipFilterHarness(channels=[0])
        values = np.array([1.0, 1.0, 1.0, 9.0, 1.0, 1.0, 1.0], dtype=np.float32)
        expected = causal_median3_reference(values).astype(np.float32)

        got = np.empty_like(values)
        for start in range(0, len(values), 2):
            chunk = values[start:start + 2].reshape(-1, 1)
            filtered_chunk, _ = harness.prepare_pzt_blip_filter_blocks(chunk, chunk.copy())
            got[start:start + len(filtered_chunk)] = filtered_chunk.reshape(-1)

        np.testing.assert_allclose(got, expected)

    def test_rs_only_mode_is_never_filtered(self):
        harness = PztBlipFilterHarness(pzt_rs=True)
        block = np.array([[0.0, 0.0, 5.0, 0.0]], dtype=np.float32)
        archive = block.copy()

        filtered_block, filtered_archive = harness.prepare_pzt_blip_filter_blocks(block, archive)

        np.testing.assert_array_equal(filtered_block, block)
        np.testing.assert_array_equal(filtered_archive, archive)

    def test_new_capture_resets_history(self):
        harness = PztBlipFilterHarness(channels=[0])
        first = np.array([0.0, 0.0, 0.0], dtype=np.float32).reshape(-1, 1)
        harness.prepare_pzt_blip_filter_blocks(first, first.copy())

        harness.begin_pzt_blip_filter_capture()

        second = np.array([9.0, 0.0, 0.0], dtype=np.float32).reshape(-1, 1)
        filtered_block, _ = harness.prepare_pzt_blip_filter_blocks(second, second.copy())

        # With history cleared, the leading 9.0 has no carried-over neighbour
        # from the previous capture and is treated as the start of a new
        # window (unfiltered) rather than smoothed away by stale history.
        expected = causal_median3_reference(second.reshape(-1)).reshape(-1, 1).astype(np.float32)
        np.testing.assert_allclose(filtered_block, expected)


if __name__ == '__main__':
    unittest.main()
