import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from core.texture_piezo.application.stream_processor import TouchIdStreamProcessor
from core.texture_piezo.gating.quality_gate import IdleBaseline

import data as data_mod  # noqa: E402  (sys.path wired by core.texture_piezo.application.stream_processor's import above)

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


def _warm_up(processor: TouchIdStreamProcessor, fs: float, t: float = 0.0) -> float:
    """Push push_chunk's full leading-sample drop (max of
    CausalDerivedChannels' own moving-sum window-fill warmup and the
    session's capture-start settle trim -- see push_chunk's docstring)
    through the processor with idle-valued samples before a test's real
    assertions, so those samples' own ready-window count isn't silently
    short by the drop that push_chunk applies. Returns the timestamp to
    resume pushing from."""
    n = max(
        data_mod.total_warmup_sample_count(fs, processor.derived_channels.jerk_window_samples),
        data_mod.capture_start_settle_sample_count(fs),
    )
    processor.push_chunk(processor.filter_raw(_chunk(n, 0.0)), _timestamps(t, n, fs), fs, now_t=t)
    return t + n / fs


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
            idle_baseline=None,
        )

    def test_no_window_until_hop_worth_of_samples_pushed(self):
        processor = self._processor()
        fs = 1000.0
        t = _warm_up(processor, fs)
        # Next push (10 samples at 1000Hz = 0.01s) is far short of even one
        # hop_size_s (0.05s) -- must yield nothing yet.
        ready = processor.push_chunk(processor.filter_raw(_chunk(10, 5.0)), _timestamps(t, 10, fs), fs, now_t=t)
        self.assertEqual(ready, [])

    def test_window_size_worth_of_samples_yields_one_window_with_frag_id_none(self):
        processor = self._processor()
        fs = 1000.0
        ready = []
        t = _warm_up(processor, fs)
        # 0.1s window_size_s at 1000Hz = 100 samples -- push enough hops to
        # cross that threshold.
        for _ in range(3):
            ready = processor.push_chunk(processor.filter_raw(_chunk(50, 5.0)), _timestamps(t, 50, fs), fs, now_t=t)
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
        t = _warm_up(processor, fs)
        for _ in range(6):
            got = processor.push_chunk(processor.filter_raw(_chunk(50, 0.0)), _timestamps(t, 50, fs), fs, now_t=t)
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
            idle_baseline=baseline or _make_baseline(),
        )

    def test_pure_idle_signal_yields_no_windows(self):
        processor = self._processor()
        fs = 1000.0
        ready = []
        t = _warm_up(processor, fs)
        for _ in range(10):
            got = processor.push_chunk(processor.filter_raw(_chunk(50, 0.0)), _timestamps(t, 50, fs), fs, now_t=t)
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
        t = _warm_up(processor, fs, t)
        for _ in range(10):
            got = processor.push_chunk(processor.filter_raw(_chunk(50, 10.0)), _timestamps(t, 50, fs), fs, now_t=t)
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
        t = _warm_up(processor, fs)
        for _ in range(10):
            got = processor.push_chunk(processor.filter_raw(_chunk(50, 10.0)), _timestamps(t, 50, fs), fs, now_t=t)
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
            idle_baseline=baseline,
        )
        fs = 1000.0
        t = _warm_up(processor, fs)
        windows = []
        for _ in range(20):
            windows.extend(processor.push_chunk(processor.filter_raw(_chunk(25, 10.0)), _timestamps(t, 25, fs), fs, now_t=t))
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


class ChunkInvarianceTests(unittest.TestCase):
    """Feeding the identical total raw samples through one large push_chunk()
    call vs many small sequential push_chunk() calls must leave the
    continuous sample store (the 6 parallel raw/derived/timestamp arrays
    ActiveSampleQueue's absolute indices reference) byte-identical --
    otherwise live (many small hop-sized pushes) and replay/offline (which
    could legally choose a different chunking) would silently diverge."""

    def _processor(self, baseline):
        return TouchIdStreamProcessor(
            pzt_columns=PZT_COLUMNS,
            window_size_s=0.1,
            hop_size_s=0.05,
            span_stale_timeout_s=1.0,
            idle_baseline=baseline,
        )

    def _per_column_signal(self, rng, total_n: int) -> dict:
        """Give every PZT column its own distinct noise draw, straddling
        zero with opposite signs on opposite sides (PZT_COLUMNS is
        [B, L, C, R, T] -- see compute_shear_normal_batch in
        core/piezo_engine/batch.py: shear requires sign(left) != sign(right)
        and sign(top) != sign(bottom), so same-sign columns make
        shear_jerk_lr/tb identically zero regardless of magnitude). Not the
        same value broadcast to all columns (see _chunk) -- an all-equal
        signal would make comparing the differential channels below
        vacuously pass through an arbitrarily broken refactor."""
        offset_by_col = {"PZT3_B": -4.0, "PZT3_L": -2.0, "PZT3_C": 0.0, "PZT3_R": 2.0, "PZT3_T": 4.0}
        return {
            col: rng.normal(offset_by_col[col], 0.5, size=total_n)
            for col in PZT_COLUMNS
        }

    def _push_as_chunks(self, processor, fs, ts_all, raw_by_col: dict, chunk_sizes) -> None:
        """Feed raw_by_col into processor.push_chunk split into successive
        pieces of chunk_sizes (which must sum to len(ts_all)). ts_all is
        built ONCE by the caller and sliced here (never recomputed per
        chunk), so the split and whole runs see byte-identical timestamps
        regardless of how the chunking regroups the pushes."""
        n = len(ts_all)
        assert sum(chunk_sizes) == n
        pos = 0
        for size in chunk_sizes:
            samples = {col: values[pos:pos + size].copy() for col, values in raw_by_col.items()}
            chunk_ts = ts_all[pos:pos + size]
            processor.push_chunk(processor.filter_raw(samples), chunk_ts, fs, now_t=float(chunk_ts[0]))
            pos += size

    def _assert_stores_equal(self, proc_whole, proc_split) -> None:
        np.testing.assert_array_equal(proc_whole._store._store_raw, proc_split._store._store_raw)
        np.testing.assert_array_equal(proc_whole._store._store_integrated, proc_split._store._store_integrated)
        np.testing.assert_array_equal(proc_whole._store._store_shear_jerk_lr, proc_split._store._store_shear_jerk_lr)
        np.testing.assert_array_equal(proc_whole._store._store_shear_jerk_tb, proc_split._store._store_shear_jerk_tb)
        np.testing.assert_array_equal(proc_whole._store._store_normal_jerk, proc_split._store._store_normal_jerk)
        np.testing.assert_array_equal(proc_whole._store._store_ts, proc_split._store._store_ts)
        self.assertEqual(proc_whole._store._store_base_abs, proc_split._store._store_base_abs)
        self.assertEqual(proc_whole._store._store_next_abs, proc_split._store._store_next_abs)
        # _chunk_cursor_abs is deliberately NOT compared here: it is reset to
        # _store_next_abs at whatever moment _ensure_active_queue first
        # constructs the ActiveSampleQueue (see stream_processor.py), which
        # depends on how many samples had already been appended at that
        # point -- itself a function of how the warmup-spanning chunk
        # boundaries fell. That shifts the micro-chunk grid's alignment
        # between the whole and split runs (both still process every
        # available micro-chunk, just offset differently), which legitimately
        # changes ActiveSampleQueue's segmentation without indicating any
        # bug in the store itself.

    def test_store_arrays_identical_for_one_big_chunk_vs_many_small_chunks(self):
        baseline = _make_baseline()
        fs = 1000.0
        rng = np.random.default_rng(0)
        total_n = 400  # well under span_stale_timeout_s=1.0s at 1000Hz, so
        # expiry-driven _trim_store differences can't leak into this
        # comparison (queue.expire runs once per push_chunk call, so a
        # split run with many more calls legitimately expires more often --
        # see the class docstring on why ReadyWindows themselves aren't
        # compared here).
        raw_by_col = self._per_column_signal(rng, total_n)

        proc_whole = self._processor(baseline)
        t0 = _warm_up(proc_whole, fs)
        ts_all = _timestamps(t0, total_n, fs)
        self._push_as_chunks(proc_whole, fs, ts_all, raw_by_col, [total_n])

        proc_split = self._processor(baseline)
        t0_split = _warm_up(proc_split, fs)
        self.assertEqual(t0, t0_split)
        self._push_as_chunks(proc_split, fs, ts_all, raw_by_col, [7, 3, 40, 1, 100, 9, 90, 50, 100])

        # Guard against the comparisons above going vacuous: the differential
        # channels must actually carry signal for this test to mean anything.
        self.assertGreater(np.abs(proc_whole._store._store_shear_jerk_lr).max(), 0)
        self.assertGreater(np.abs(proc_whole._store._store_shear_jerk_tb).max(), 0)

        self._assert_stores_equal(proc_whole, proc_split)

    def test_store_arrays_identical_when_warmup_itself_spans_the_chunk_boundary(self):
        """Same as above, but skips _warm_up -- the leading-sample warmup
        drop (push_chunk's samples_seen_before-relative cutoff, see
        _drop_leading_warmup_samples) happens inside a single push for the
        whole run and is itself split across several small pushes for the
        split run. This is the exact logic moving into DerivedChannelPipeline
        in the refactor, so it needs its own chunk-invariance coverage."""
        baseline = _make_baseline()
        fs = 1000.0
        rng = np.random.default_rng(1)
        warmup_n = max(
            data_mod.total_warmup_sample_count(
                fs, TouchIdStreamProcessor(
                    pzt_columns=PZT_COLUMNS, window_size_s=0.1, hop_size_s=0.05,
                    span_stale_timeout_s=1.0, idle_baseline=baseline,
                ).derived_channels.jerk_window_samples,
            ),
            data_mod.capture_start_settle_sample_count(fs),
        )
        total_n = warmup_n + 150
        raw_by_col = self._per_column_signal(rng, total_n)
        ts_all = _timestamps(0.0, total_n, fs)

        proc_whole = self._processor(baseline)
        self._push_as_chunks(proc_whole, fs, ts_all, raw_by_col, [total_n])

        proc_split = self._processor(baseline)
        # Deliberately crosses the warmup boundary mid-chunk at several
        # points rather than landing exactly on it.
        sizes = [11, 17, max(1, warmup_n - 25), 25, 3, 40, 1]
        sizes.append(total_n - sum(sizes))
        self._push_as_chunks(proc_split, fs, ts_all, raw_by_col, sizes)

        self.assertGreater(np.abs(proc_whole._store._store_shear_jerk_lr).max(), 0)
        self.assertGreater(np.abs(proc_whole._store._store_shear_jerk_tb).max(), 0)

        self._assert_stores_equal(proc_whole, proc_split)


class FilterRawTests(unittest.TestCase):
    """TouchIdStreamProcessor.filter_raw despikes raw channel_samples via the
    same causal median-3 primitive texture_piezo's offline path uses."""

    def _processor(self):
        return TouchIdStreamProcessor(
            pzt_columns=PZT_COLUMNS,
            window_size_s=0.1,
            hop_size_s=0.05,
            span_stale_timeout_s=1.0,
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


class PushChunkRequiresFilterRawTests(unittest.TestCase):
    """Regression test for the ordering bug: push_chunk's capture-start
    settle trim only removes leading samples, it does not despike them --
    the despiking has to have already happened via filter_raw(). Pins the
    fail-fast guard (stream_processor.py's _raw_filtered_this_tick marker)
    so a future edit that drops or reorders a filter_raw() call in a real
    caller (gui/inference_panel.py) fails loudly here instead of silently
    letting an unfiltered blip through, the way it already did once for the
    Analysis CSV path."""

    def _processor(self):
        return TouchIdStreamProcessor(
            pzt_columns=PZT_COLUMNS,
            window_size_s=0.1,
            hop_size_s=0.05,
            span_stale_timeout_s=1.0,
            idle_baseline=None,
        )

    def test_push_chunk_without_filter_raw_raises(self):
        processor = self._processor()
        fs = 1000.0
        with self.assertRaises(RuntimeError):
            processor.push_chunk(_chunk(10, 5.0), _timestamps(0.0, 10, fs), fs, now_t=0.0)

    def test_push_chunk_after_filter_raw_succeeds(self):
        processor = self._processor()
        fs = 1000.0
        filtered = processor.filter_raw(_chunk(10, 5.0))
        processor.push_chunk(filtered, _timestamps(0.0, 10, fs), fs, now_t=0.0)  # must not raise

    def test_filter_raw_must_be_called_again_for_the_next_chunk(self):
        """The marker is per-tick, not sticky -- calling filter_raw once
        does not license two push_chunk calls."""
        processor = self._processor()
        fs = 1000.0
        filtered = processor.filter_raw(_chunk(10, 5.0))
        processor.push_chunk(filtered, _timestamps(0.0, 10, fs), fs, now_t=0.0)
        with self.assertRaises(RuntimeError):
            processor.push_chunk(filtered, _timestamps(0.01, 10, fs), fs, now_t=0.01)


if __name__ == '__main__':
    unittest.main()
