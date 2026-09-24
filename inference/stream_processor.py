"""
TouchID Stream Processor
========================
GUI-free extraction of the buffering/derivation/segmentation logic that
turns newly-arrived raw PZT sweeps into classifier-ready windows -- the
single source of truth for BOTH live streaming (gui/inference_panel.py,
fed one hop's worth of sweeps per QTimer tick, now_t=time.monotonic()) and
offline replay (fed the same size chunks as fast as possible, now_t derived
from the snapshot's own sample clock -- see push_chunk's docstring for why
the two need different clocks).

Owns:
  - CausalDerivedChannels (the "integrated"/shear/normal causal derivation --
    see texture_piezo/src/causal_derived_channels.py's module docstring for
    why feeding it in small incremental chunks or fewer/larger ones produces
    IDENTICAL numbers, which is what makes sharing this class between live
    and replay valid in the first place).
  - The RollingBuffer pair (fixed-grid fallback path, used only when no idle
    baseline has been captured yet -- see push_chunk).
  - The continuous raw+derived sample store + its trim/slice logic, feeding
    ActiveSampleQueue's index-only bookkeeping (used once a baseline exists).
  - The ActiveSampleQueue lifecycle itself (lazy construction once fs is
    known, micro-chunking, fragment expiry).

Does NOT own: classification (the caller submits ReadyWindows to whatever
worker/synchronous path it likes), the "which of this tick's windows to
submit" policy (live submits only the newest and drops the rest under load;
replay must submit and wait for every one -- both are caller policy, not
processor policy), or any GUI/plotting state.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import numpy as np

from ._paths import TEXTURE_PIEZO_SRC
from .buffer import RollingBuffer

sys.path.insert(0, str(TEXTURE_PIEZO_SRC))
from causal_derived_channels import CausalDerivedChannels  # noqa: E402
import data as data_mod  # noqa: E402
from touchid_inference.quality_gate import IdleBaseline, MICRO_CHUNK_S  # noqa: E402
from touchid_inference.segmentation import ActiveSampleQueue  # noqa: E402


@dataclass
class ReadyWindow:
    """One classifier-ready window, plus the bookkeeping the caller needs to
    paint it and submit it. frag_id is None in the fixed-grid fallback branch
    (no fragment concept there -- see _touchid_region_color_for_span's
    docstring in gui/inference_panel.py for how callers use this)."""

    window_adc: np.ndarray
    window_integrated: np.ndarray
    window_shear_lr: np.ndarray
    window_shear_tb: np.ndarray
    window_normal: np.ndarray
    window_ts: np.ndarray
    frag_id: int | None


class TouchIdStreamProcessor:
    """Feed it successive, causally-ordered chunks of raw PZT sweeps via
    push_chunk(); it returns whatever ReadyWindows became available this
    tick (zero, one, or several)."""

    def __init__(
        self,
        pzt_columns: list[str],
        window_size_s: float,
        hop_size_s: float,
        span_stale_timeout_s: float,
        min_span_fill_ratio: float,
        idle_baseline: IdleBaseline | None,
    ) -> None:
        self.pzt_columns = list(pzt_columns)
        self.window_size_s = float(window_size_s)
        self.hop_size_s = float(hop_size_s)
        self.span_stale_timeout_s = float(span_stale_timeout_s)
        self.min_span_fill_ratio = float(min_span_fill_ratio)
        self.idle_baseline = idle_baseline

        # Persistent streaming state for the "integrated"/shear/normal
        # derived channels -- one instance for this processor's whole
        # lifetime, .process()'d on each newly-pushed chunk so its bounded
        # windowed sums and unbounded causal medians carry forward
        # continuously instead of restarting every window.
        self.derived_channels = CausalDerivedChannels(pzt_columns=self.pzt_columns)

        # Persistent per-channel causal median-3 despike state -- the same
        # single shared implementation texture_piezo's offline
        # load_calibration_csv path uses (via the batch causal_median_filter_3
        # wrapper), so live/replay raw is despiked identically to offline raw.
        self._raw_filter = {col: data_mod._CausalMedian3() for col in self.pzt_columns}

        n_pzt = len(self.pzt_columns)
        # Fixed-grid fallback path (used only while idle_baseline is None).
        # touchid_derived_buffer is a second RollingBuffer, pushed in
        # lockstep with touchid_buffer on the same hop cadence, so
        # get_window() on both together yields perfectly aligned raw-ADC and
        # derived-channel slices for one window.
        self._buffer = RollingBuffer(n_channels=n_pzt, window_size_s=self.window_size_s, hop_size_s=self.hop_size_s)
        self._derived_buffer = RollingBuffer(
            n_channels=n_pzt + 3, window_size_s=self.window_size_s, hop_size_s=self.hop_size_s,
        )

        self._store_reset()

    def _store_reset(self) -> None:
        """(Re)initialize the continuous raw+derived sample store (used only
        once an idle baseline exists) and drop the ActiveSampleQueue built
        on top of it -- it's rebuilt lazily (see _ensure_active_queue) once a
        measured fs is available again."""
        n_pzt = len(self.pzt_columns)
        self._store_raw = np.empty((0, n_pzt))
        self._store_integrated = np.empty((0, n_pzt))
        self._store_shear_lr = np.empty(0)
        self._store_shear_tb = np.empty(0)
        self._store_normal = np.empty(0)
        self._store_ts = np.empty(0)
        self._store_base_abs = 0  # abs index of store[0]
        self._store_next_abs = 0  # abs index just past the last appended sample
        self._chunk_cursor_abs = 0  # abs index up to which micro-chunks have been pushed
        self.active_queue: ActiveSampleQueue | None = None

    def _ensure_active_queue(self, fs: float) -> None:
        """Lazily build the ActiveSampleQueue on the first tick a measured fs
        is available -- it can't be constructed in __init__ since fs isn't
        known until streaming has actually started."""
        if self.active_queue is not None or self.idle_baseline is None:
            return
        self.active_queue = ActiveSampleQueue(
            fs=fs,
            window_size_s=self.window_size_s,
            hop_size_s=self.hop_size_s,
            baseline=self.idle_baseline,
        )
        self._chunk_cursor_abs = self._store_next_abs

    def _append_to_store(self, channel_samples: dict, derived: dict, timestamps: np.ndarray) -> None:
        """Append this tick's newly-pushed raw+derived samples to the
        continuous store, in lockstep, at the running absolute index
        ActiveSampleQueue's yielded (start_idx, end_idx) pairs reference."""
        pzt_columns = self.pzt_columns
        raw = np.stack([channel_samples[col] for col in pzt_columns], axis=1)
        integrated = np.stack([derived['integrated'][col] for col in pzt_columns], axis=1)
        self._store_raw = np.concatenate([self._store_raw, raw], axis=0)
        self._store_integrated = np.concatenate([self._store_integrated, integrated], axis=0)
        self._store_shear_lr = np.concatenate([self._store_shear_lr, derived['shear_lr']])
        self._store_shear_tb = np.concatenate([self._store_shear_tb, derived['shear_tb']])
        self._store_normal = np.concatenate([self._store_normal, derived['normal']])
        self._store_ts = np.concatenate([self._store_ts, np.asarray(timestamps, dtype=np.float64)])
        self._store_next_abs += len(raw)

    def _trim_store(self) -> None:
        """Drop the front of the continuous store once no live span
        (finalized or open, per active_queue.oldest_referenced_idx)
        references it anymore, keeping a window_size_s + span_stale_timeout_s
        safety margin so a still-growing open span never has its start index
        trimmed out from under it."""
        queue = self.active_queue
        if queue is None:
            return
        margin_n = round((self.window_size_s + self.span_stale_timeout_s) * queue.fs)
        oldest_referenced = queue.oldest_referenced_idx()
        safe_abs = self._chunk_cursor_abs if oldest_referenced is None else min(
            oldest_referenced, self._chunk_cursor_abs
        )
        trim_to_abs = max(self._store_base_abs, safe_abs - margin_n)
        trim_n = trim_to_abs - self._store_base_abs
        if trim_n <= 0:
            return
        self._store_raw = self._store_raw[trim_n:]
        self._store_integrated = self._store_integrated[trim_n:]
        self._store_shear_lr = self._store_shear_lr[trim_n:]
        self._store_shear_tb = self._store_shear_tb[trim_n:]
        self._store_normal = self._store_normal[trim_n:]
        self._store_ts = self._store_ts[trim_n:]
        self._store_base_abs = trim_to_abs

    def _slice_store(self, start_abs: int, end_abs: int):
        """Slice the continuous store at an absolute (start_idx, end_idx)
        pair from ActiveSampleQueue -- returns (window_adc, window_integrated,
        window_shear_lr, window_shear_tb, window_normal, window_ts), or None
        if the range has already been trimmed out (shouldn't happen given
        _trim_store's safety margin, but guarded rather than slicing
        garbage)."""
        if start_abs < self._store_base_abs:
            return None
        start_i = start_abs - self._store_base_abs
        end_i = end_abs - self._store_base_abs
        return (
            self._store_raw[start_i:end_i],
            self._store_integrated[start_i:end_i],
            self._store_shear_lr[start_i:end_i],
            self._store_shear_tb[start_i:end_i],
            self._store_normal[start_i:end_i],
            self._store_ts[start_i:end_i],
        )

    def filter_raw(self, channel_samples: dict) -> dict:
        """Causal median-3 despike -- same _CausalMedian3 primitive/algorithm
        texture_piezo's offline load_calibration_csv path uses, so live and
        offline raw are despiked identically. Must be called ONCE per tick,
        before channel_samples is used for anything else (the Signal Stream
        plot, idle-baseline accumulation, AND push_chunk) -- callers must not
        filter twice."""
        return {
            col: np.array([self._raw_filter[col].push(v) for v in np.asarray(channel_samples[col]).reshape(-1)])
            for col in self.pzt_columns
        }

    def push_chunk(
        self, channel_samples: dict, timestamps: np.ndarray, fs: float, now_t: float,
    ) -> list[ReadyWindow]:
        """Advance the derived-channel causal state by exactly this newly-
        pushed chunk, then feed it into whichever windowing branch is active,
        returning every ReadyWindow that became available this call.

        now_t is caller-supplied, never read from the wall clock in here:
        live streaming passes time.monotonic() (hops really do arrive
        ~hop_size_s apart in real time, so ActiveSampleQueue.expire's
        staleness timeout means what it says). Replay must NOT pass
        wall-clock time -- it runs the whole capture as fast as possible, so
        wall-clock would barely advance between chunks and every span would
        look far younger than span_stale_timeout_s regardless of how much
        (sample-time) history it actually spans, silently disabling eviction.
        Replay instead passes a sample-derived clock (e.g.
        end_sample_index / fs, matching the snapshot's own timestamps_s) so
        expire sees the same real-time deltas the algorithm would have
        seen live for that exact recording, reproducing identical
        segmentation decisions.
        """
        derived = self.derived_channels.process(channel_samples)

        if self.idle_baseline is None:
            return self._push_chunk_fixed_grid(channel_samples, derived, timestamps, fs)
        return self._push_chunk_active_queue(channel_samples, derived, timestamps, fs, now_t)

    def _push_chunk_fixed_grid(
        self, channel_samples: dict, derived: dict, timestamps: np.ndarray, fs: float,
    ) -> list[ReadyWindow]:
        """No baseline captured yet -- classify every window on the plain
        fixed window_size_s/hop_size_s grid unconditionally (matches
        is_window_quality's old no-op-without-a-baseline behavior)."""
        pzt_columns = self.pzt_columns
        derived_channel_samples = {f'integrated_{col}': derived['integrated'][col] for col in pzt_columns}
        derived_channel_samples['shear_lr'] = derived['shear_lr']
        derived_channel_samples['shear_tb'] = derived['shear_tb']
        derived_channel_samples['normal'] = derived['normal']
        self._buffer.push(channel_samples, timestamps)
        self._derived_buffer.push(derived_channel_samples, timestamps)

        window = self._buffer.get_window(fs=fs)
        if window is None:
            return []
        window_adc, window_ts = window
        derived_window = self._derived_buffer.get_window(fs=fs)
        if derived_window is None:
            return []
        derived_window_adc, _derived_ts = derived_window
        n_pzt = len(pzt_columns)
        window_integrated = derived_window_adc[:, :n_pzt]
        window_shear_lr = derived_window_adc[:, n_pzt]
        window_shear_tb = derived_window_adc[:, n_pzt + 1]
        window_normal = derived_window_adc[:, n_pzt + 2]

        return [ReadyWindow(
            window_adc=window_adc,
            window_integrated=window_integrated,
            window_shear_lr=window_shear_lr,
            window_shear_tb=window_shear_tb,
            window_normal=window_normal,
            window_ts=window_ts,
            frag_id=None,
        )]

    def _push_chunk_active_queue(
        self, channel_samples: dict, derived: dict, timestamps: np.ndarray, fs: float, now_t: float,
    ) -> list[ReadyWindow]:
        """Baseline present: sample-accurate ActiveSampleQueue segmentation
        (inference/segmentation.py) -- feed this tick's newly-derived samples
        into the continuous store, then chunk/segment off of it instead of a
        fixed hop grid.

        Chunks any newly-appended continuous-store samples into 0.05s
        micro-chunks, feeds them into the queue, drains whatever windows it
        yields, and expires fragments too old to still matter. Idle is now
        stripped out of a fragment before windowing (not rejected after), so
        a returned window cannot straddle a genuine idle gap by
        construction -- there is no post-hoc idle-fraction rejection step
        anymore."""
        self._append_to_store(channel_samples, derived, timestamps)
        self._ensure_active_queue(fs)
        queue = self.active_queue
        if queue is None:
            return []

        chunk_n = max(1, round(MICRO_CHUNK_S * fs))

        while self._chunk_cursor_abs + chunk_n <= self._store_next_abs:
            start_abs = self._chunk_cursor_abs
            end_abs = start_abs + chunk_n
            start_i = start_abs - self._store_base_abs
            end_i = end_abs - self._store_base_abs
            queue.push_micro_chunk((start_abs, end_abs), self._store_raw[start_i:end_i], now_t)
            self._chunk_cursor_abs = end_abs

        windows = queue.ready_windows(store_base_abs=self._store_base_abs)
        queue.expire(now_t)

        ready: list[ReadyWindow] = []
        for start_abs, end_abs, frag_id in windows:
            sliced = self._slice_store(start_abs, end_abs)
            if sliced is None:
                continue
            window_adc = sliced[0]
            window_integrated, window_shear_lr, window_shear_tb, window_normal, window_ts = sliced[1:]
            ready.append(ReadyWindow(
                window_adc=window_adc,
                window_integrated=window_integrated,
                window_shear_lr=window_shear_lr,
                window_shear_tb=window_shear_tb,
                window_normal=window_normal,
                window_ts=window_ts,
                frag_id=frag_id,
            ))

        self._trim_store()
        return ready
