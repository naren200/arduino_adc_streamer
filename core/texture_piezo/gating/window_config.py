"""
Inference Config
=================
Ported from texture_piezo's former window-config module for
arduino_adc_streamer standalone inference.

Independent config surface for texture_piezo's inference-stage logic
(quality_gate.py, segmentation.py, window_padding.py, and callers like
partial_window_utils.fast_forward_extract_windows).

Deliberately NOT read from configs/config.yaml -- that file's
data.window_size_s/hop_size_s govern TRAINING/VALIDATION clip windowing
(in the training repository) and are unrelated to how the live/offline
inference pipeline windows a stream. The two are coincidentally similar in
places (window=0.5s matches training here) but are not meant to be coupled;
changing one must not silently change the other.

Values below mirror the arduino_adc_streamer GUI's current settings
(inference/config.py's InferenceConfig defaults there), captured as the
"final combination for inference only" at the time this pipeline was moved
into texture_piezo.
"""

from __future__ import annotations

from .quality_gate import DEFAULT_K as DEFAULT_IDLE_GATE_K
from .quality_gate import IDLE_CAPTURE_DURATION_S as IDLE_BASELINE_DURATION_S

WINDOW_SIZE_S = 0.5
HOP_SIZE_S = 0.1
SMOOTHING_WINDOW_N = 10
CONFIDENCE_THRESHOLD = 0.75

# How many seconds of a freshly-opened fragment's leading edge to discard
# before any window may start there -- a fresh idle->active transition
# carries a brief mechanical settling/transient on the raw PZT ADC signal
# that isn't representative of steady contact, and the first window drawn
# from a fragment must never be seeded from it. Does NOT apply to a
# merge-grace resume (segmentation.ActiveSampleQueue) -- that's the same
# touch continuing, not a fresh onset.
#
# Measured empirically (not analytically derived) from raw-signal settling
# time on labeled non-idle segment onsets in
# data/raw/sensor_v12b_v11z4/test_/only_{leather,cardboard,wood,tile}_and_idle_v2/v3*.csv
# (15 onsets across 4 files/materials): the labeled start_s in *_labels.json
# is not sample-accurate (rig control-loop/label latency), so the true
# signal rise was located near each labeled onset first, then a short
# rolling-window std was swept forward from that rise looking for
# convergence to the segment's own steady-state std. Only 5/15 onsets
# converged within a 64-sample search (the rest never settled within that
# search on this noisy raw ADC signal) -- convergent cases: 0, 0, 8, 8, 32
# samples (median 8, max 32) at ~1538 Hz. Inconclusive/noisy overall, so per
# convention this rounds UP to the observed max (32 samples) rather than the
# median, as a conservative choice, instead of manufacturing false precision
# from a small, noisy sample.
ONSET_SKIP_S = 32 / 1538.2
