import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from inference.stream_processor import TouchIdStreamProcessor
from touchid_inference.quality_gate import IdleBaseline

PZT_COLUMNS = [f"PZT3_{c}" for c in "BLCRT"]


def _make_baseline(k: float = 8.0) -> IdleBaseline:
    return IdleBaseline(
        pzt_columns=list(PZT_COLUMNS),
        mean=[0.0] * len(PZT_COLUMNS),
        std=[0.1] * len(PZT_COLUMNS),
        fs=1000.0,
        k=k,
        captured_duration_s=5.0,
    )


def _chunk(n: int, value: float) -> dict:
    """n samples of `value` on every channel, e.g. a flat idle or active chunk."""
    return {col: np.full(n, value, dtype=np.float64) for col in PZT_COLUMNS}


def _timestamps(start: float, n: int, fs: float) -> np.ndarray:
    return start + np.arange(n) / fs


class FixedGridFallbackTests(unittest.TestCase):
    """No idle_baseline -- push_chunk must fall back to the plain fixed
    window_size_s/hop_size_s grid, classifying (i.e. yielding) every window
    unconditionally regardless of signal content."""

    def _processor(self):
        return TouchIdStreamProcessor(
            pzt_columns=PZT_COLUMNS,
            window_size_s=0.1,
            hop_size_s=0.05,
            span_stale_timeout_s=1.0,
            min_span_fill_ratio=0.08,
            idle_baseline=None,
        )

    def test_no_window_until_hop_worth_of_samples_pushed(self):
        processor = self._processor()
        fs = 1000.0
        # First push (10 samples at 1000Hz = 0.01s) is far short of even one
        # hop_size_s (0.05s) -- must yield nothing yet.
        ready = processor.push_chunk(_chunk(10, 5.0), _timestamps(0.0, 10, fs), fs, now_t=0.0)
        self.assertEqual(ready, [])

    def test_window_size_worth_of_samples_yields_one_window_with_frag_id_none(self):
        processor = self._processor()
        fs = 1000.0
        ready = []
        t = 0.0
        # 0.1s window_size_s at 1000Hz = 100 samples -- push enough hops to
        # cross that threshold.
        for _ in range(3):
            ready = processor.push_chunk(_chunk(50, 5.0), _timestamps(t, 50, fs), fs, now_t=t)
            t += 50 / fs
        self.assertEqual(len(ready), 1)
        window = ready[0]
        self.assertIsNone(window.frag_id)
        self.assertEqual(len(window.window_adc), 100)
        self.assertEqual(len(window.window_ts), 100)

    def test_every_window_is_classified_unconditionally_even_when_flat_idle(self):
        """No baseline means the idle-fraction gate is a no-op -- a
        perfectly flat (zero-signal) chunk must still produce windows,
        unlike the ActiveSampleQueue path below."""
        processor = self._processor()
        fs = 1000.0
        ready = []
        t = 0.0
        for _ in range(6):
            got = processor.push_chunk(_chunk(50, 0.0), _timestamps(t, 50, fs), fs, now_t=t)
            ready.extend(got)
            t += 50 / fs
        self.assertGreaterEqual(len(ready), 1)


class ActiveSampleQueuePathTests(unittest.TestCase):
    """idle_baseline present -- push_chunk drives ActiveSampleQueue
    segmentation instead of the fixed grid, stripping idle out of a
    fragment before windowing rather than rejecting a window after."""

    def _processor(self, baseline=None):
        return TouchIdStreamProcessor(
            pzt_columns=PZT_COLUMNS,
            window_size_s=0.1,
            hop_size_s=0.05,
            span_stale_timeout_s=1.0,
            min_span_fill_ratio=0.08,
            idle_baseline=baseline or _make_baseline(),
        )

    def test_pure_idle_signal_yields_no_windows(self):
        processor = self._processor()
        fs = 1000.0
        ready = []
        t = 0.0
        for _ in range(10):
            got = processor.push_chunk(_chunk(50, 0.0), _timestamps(t, 50, fs), fs, now_t=t)
            ready.extend(got)
            t += 50 / fs
        self.assertEqual(ready, [])

    def test_sustained_active_signal_yields_windows_with_a_frag_id(self):
        processor = self._processor()
        fs = 1000.0
        ready = []
        t = 0.0
        # Far above the idle band (mean=0, std=0.1, k=8 -> band is +-0.8) --
        # unambiguously active.
        for _ in range(10):
            got = processor.push_chunk(_chunk(50, 10.0), _timestamps(t, 50, fs), fs, now_t=t)
            ready.extend(got)
            t += 50 / fs
        self.assertGreater(len(ready), 0)
        for window in ready:
            self.assertIsNotNone(window.frag_id)
            self.assertEqual(len(window.window_adc), 100)  # window_size_s=0.1 @ 1000Hz

    def test_now_t_is_never_read_from_wall_clock(self):
        """A caller-supplied now_t far in the "past" (e.g. a replay's
        sample-derived clock starting near 0) must be honored as-is --
        segmentation must depend only on the now_t argument, never on
        time.monotonic(), so live and replay can drive the identical
        processor class with their own appropriate clocks."""
        processor = self._processor()
        fs = 1000.0
        ready = []
        # now_t deliberately stays far below any real wall-clock value.
        t = 0.0
        for _ in range(10):
            got = processor.push_chunk(_chunk(50, 10.0), _timestamps(t, 50, fs), fs, now_t=t)
            ready.extend(got)
            t += 50 / fs
        self.assertGreater(len(ready), 0)


class SameHopCadenceReproducibilityTests(unittest.TestCase):
    """Two processors fed the identical sequence of hop-sized chunks (the
    only cadence either live streaming or replay actually uses -- both push
    exactly one hop_size_s slice per push_chunk call, see
    gui/inference_panel.py's update_touchid_display and
    _touchid_replay_tick) must yield identical windows. This is what makes
    it valid for replay to reuse the same TouchIdStreamProcessor class as
    live: given the same input chunked the same way, the output is
    deterministic (CausalDerivedChannels' own chunk-invariance -- feeding
    it different-sized chunks of the SAME total input -- is verified
    separately against a real capture, not here)."""

    def _run(self, baseline):
        processor = TouchIdStreamProcessor(
            pzt_columns=PZT_COLUMNS,
            window_size_s=0.1,
            hop_size_s=0.05,
            span_stale_timeout_s=1.0,
            min_span_fill_ratio=0.08,
            idle_baseline=baseline,
        )
        fs = 1000.0
        t = 0.0
        windows = []
        for _ in range(20):
            windows.extend(processor.push_chunk(_chunk(25, 10.0), _timestamps(t, 25, fs), fs, now_t=t))
            t += 25 / fs
        return windows

    def test_same_hop_cadence_reproduces_identical_windows(self):
        baseline = _make_baseline()
        windows_a = self._run(baseline)
        windows_b = self._run(baseline)
        self.assertEqual(len(windows_a), len(windows_b))
        for wa, wb in zip(windows_a, windows_b):
            np.testing.assert_array_equal(wa.window_adc, wb.window_adc)
            np.testing.assert_array_equal(wa.window_ts, wb.window_ts)
            self.assertEqual(wa.frag_id, wb.frag_id)


class FilterRawTests(unittest.TestCase):
    """TouchIdStreamProcessor.filter_raw despikes raw channel_samples via the
    same causal median-3 primitive texture_piezo's offline path uses."""

    def _processor(self):
        return TouchIdStreamProcessor(
            pzt_columns=PZT_COLUMNS,
            window_size_s=0.1,
            hop_size_s=0.05,
            span_stale_timeout_s=1.0,
            min_span_fill_ratio=0.08,
            idle_baseline=None,
        )

    def test_isolated_spike_is_removed(self):
        processor = self._processor()
        values = np.array([10.0, 10.0, 10.0, 500.0, 10.0, 10.0, 10.0])
        samples = {col: values.copy() for col in PZT_COLUMNS}
        filtered = processor.filter_raw(samples)
        for col in PZT_COLUMNS:
            self.assertNotEqual(filtered[col][3], 500.0)
            self.assertEqual(filtered[col][3], 10.0)

    def test_chunk_invariant_regardless_of_split(self):
        rng = np.random.default_rng(0)
        raw = rng.normal(2048, 40, size=100)
        raw[37] += 400  # isolated spike

        proc_whole = self._processor()
        whole = proc_whole.filter_raw({col: raw for col in PZT_COLUMNS})

        proc_split = self._processor()
        split_parts = {col: [] for col in PZT_COLUMNS}
        for start, end in [(0, 13), (13, 50), (50, 61), (61, 100)]:
            chunk = {col: raw[start:end] for col in PZT_COLUMNS}
            filtered_chunk = proc_split.filter_raw(chunk)
            for col in PZT_COLUMNS:
                split_parts[col].append(filtered_chunk[col])

        for col in PZT_COLUMNS:
            split_joined = np.concatenate(split_parts[col])
            np.testing.assert_array_equal(whole[col], split_joined)


if __name__ == '__main__':
    unittest.main()
