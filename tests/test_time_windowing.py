import unittest

import numpy as np

from core.texture_piezo.application.buffer import RollingBuffer
from core.texture_piezo.application.stream_processor import TouchIdStreamProcessor
from core.texture_piezo.gating.quality_gate import IdleBaseline
from core.texture_piezo.gating.segmentation import WARMUP_SAMPLES, ActiveSampleQueue
from core.texture_piezo.gating.time_window import hop_start_index, padded_start_index, window_end_index

WINDOW_SIZE_S = 0.1
HOP_SIZE_S = 0.05
ONSET_SKIP_S = 0.02
CHUNK_N = 50
N_CHANNELS = 5
PZT_COLUMNS = [f'PZT3_{c}' for c in 'BLCRT']


def _baseline():
    return IdleBaseline(
        pzt_columns=PZT_COLUMNS, mean=np.zeros(N_CHANNELS), std=np.ones(N_CHANNELS) * 0.1,
        fs=1000.0, k=5.0, captured_duration_s=5.0,
    )


def _emit_windows(sweep_ts: np.ndarray) -> list[tuple[int, int, int]]:
    """Drive an always-active stream whose sample i has timestamp sweep_ts[i]
    through a fresh queue, one micro-chunk at a time, collecting every window."""
    queue = ActiveSampleQueue(
        window_size_s=WINDOW_SIZE_S, hop_size_s=HOP_SIZE_S, baseline=_baseline(), onset_skip_s=ONSET_SKIP_S,
    )
    active_chunk = np.ones((CHUNK_N, N_CHANNELS)) * 10.0
    windows = []
    idx = WARMUP_SAMPLES
    while idx + CHUNK_N <= len(sweep_ts):
        queue.push_micro_chunk((idx, idx + CHUNK_N), active_chunk, sweep_ts[idx], now_t=sweep_ts[idx])
        idx += CHUNK_N
        windows.extend(queue.ready_windows(sweep_ts[:idx], 0))
    return windows


def _legacy_count_windows(fs: float, total_n: int) -> list[tuple[int, int]]:
    """What the old round(duration * fs) arithmetic emitted for one fragment
    opening at WARMUP_SAMPLES in an always-active stream of total_n samples."""
    window_n, hop_n = round(WINDOW_SIZE_S * fs), round(HOP_SIZE_S * fs)
    start, end = WARMUP_SAMPLES + round(ONSET_SKIP_S * fs), total_n
    windows = []
    while end - start >= window_n:
        windows.append((start, start + window_n))
        start += hop_n
    return windows


class UniformRateEquivalenceTests(unittest.TestCase):
    def test_windows_match_legacy_sample_count_windows_at_constant_rate(self):
        fs, total_n = 1000.0, 1000
        sweep_ts = np.arange(total_n) / fs

        got = [(start, end) for start, end, _ in _emit_windows(sweep_ts)]

        legacy = _legacy_count_windows(fs, total_n)
        # Time-based completeness waits for the first sample past a window's
        # boundary, so the very last legacy window can arrive one sample later.
        self.assertGreaterEqual(len(got), len(legacy) - 1)
        self.assertEqual(got, legacy[:len(got)])


class VaryingRateDurationTests(unittest.TestCase):
    def test_every_window_spans_its_real_duration_when_rate_halves_mid_stream(self):
        fast_dt, slow_dt = 0.001, 0.002
        sweep_ts = np.concatenate([
            np.arange(600) * fast_dt,
            600 * fast_dt + np.arange(1, 601) * slow_dt,
        ])

        windows = _emit_windows(sweep_ts)

        self.assertGreater(len(windows), 4)
        for start, end, _ in windows:
            real_span_s = sweep_ts[end - 1] - sweep_ts[start]
            self.assertLess(real_span_s, WINDOW_SIZE_S)
            self.assertGreaterEqual(real_span_s, WINDOW_SIZE_S - slow_dt - 1e-9)
        starts_s = [sweep_ts[start] for start, _, _ in windows]
        for previous, following in zip(starts_s, starts_s[1:]):
            self.assertAlmostEqual(following - previous, HOP_SIZE_S, delta=slow_dt + 1e-9)


class RollingBufferTimeWindowTests(unittest.TestCase):
    def test_window_holds_the_requested_duration_whatever_the_sample_spacing(self):
        buffer = RollingBuffer(n_channels=1, window_size_s=WINDOW_SIZE_S, hop_size_s=HOP_SIZE_S)
        sweep_ts = np.concatenate([np.arange(100) * 0.001, 0.1 + np.arange(1, 101) * 0.002])
        buffer.push({'a': np.arange(len(sweep_ts), dtype=float)}, sweep_ts)

        window_adc, window_ts = buffer.get_window()

        self.assertEqual(window_adc.shape, (len(window_ts), 1))
        self.assertLess(window_ts[-1] - window_ts[0], WINDOW_SIZE_S)
        self.assertGreaterEqual(window_ts[-1] - window_ts[0], WINDOW_SIZE_S - 0.002 - 1e-9)

    def test_no_window_until_a_sample_past_the_boundary_arrives(self):
        buffer = RollingBuffer(n_channels=1, window_size_s=WINDOW_SIZE_S, hop_size_s=HOP_SIZE_S)
        buffer.push({'a': np.zeros(100)}, np.arange(100) * 0.001)

        self.assertIsNone(buffer.get_window())


class TimeWindowHelperTests(unittest.TestCase):
    def test_hop_always_advances_even_with_duplicate_timestamps(self):
        timestamps = np.zeros(10)

        self.assertEqual(hop_start_index(timestamps, 3, hop_size_s=0.0), 4)

    def test_window_end_is_none_until_boundary_sample_exists(self):
        timestamps = np.arange(100) * 0.001

        self.assertIsNone(window_end_index(timestamps, 0, WINDOW_SIZE_S))
        self.assertEqual(window_end_index(np.arange(101) * 0.001, 0, WINDOW_SIZE_S), 100)


class PaddedStartIndexTests(unittest.TestCase):
    FS = 1000.0
    WINDOW_N = 100

    def test_matches_legacy_sample_count_when_history_is_ample(self):
        timestamps = np.arange(500) / self.FS

        self.assertEqual(padded_start_index(timestamps, 300, WINDOW_SIZE_S), 300 - self.WINDOW_N)

    def test_exactly_one_window_of_history_is_enough(self):
        timestamps = np.arange(self.WINDOW_N) / self.FS

        self.assertEqual(padded_start_index(timestamps, self.WINDOW_N, WINDOW_SIZE_S), 0)

    def test_history_shorter_than_a_window_is_insufficient(self):
        timestamps = np.arange(60) / self.FS

        self.assertIsNone(padded_start_index(timestamps, 60, WINDOW_SIZE_S))


class TimelineBreakTests(unittest.TestCase):
    def test_timestamps_restarting_near_zero_reset_windowing_instead_of_raising(self):
        processor = TouchIdStreamProcessor(
            pzt_columns=PZT_COLUMNS, window_size_s=WINDOW_SIZE_S, hop_size_s=HOP_SIZE_S,
            span_stale_timeout_s=1.0, idle_baseline=_baseline(),
        )
        fs, n = 1000.0, 3000
        samples = {col: np.ones(n) for col in PZT_COLUMNS}
        processor.push_chunk(processor.filter_raw(samples), 5.0 + np.arange(n) / fs, fs, now_t=5.0)
        self.assertGreater(len(processor._store._store_ts), 0)

        processor.push_chunk(processor.filter_raw(samples), np.arange(n) / fs, fs, now_t=0.0)

        self.assertLess(processor._store._store_ts[-1], 5.0)
        self.assertTrue(np.all(np.diff(processor._store._store_ts) >= 0))


if __name__ == '__main__':
    unittest.main()
