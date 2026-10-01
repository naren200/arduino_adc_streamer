"""Vectorized per-chunk replacement for looping ShearDetector.detect() +
NormalForceCalculator.compute() once per sample.

Ported from texture_piezo/src/shear_normal_utils_v1.py's
compute_shear_normal_batch, rewired to this repo's constants.shear position
labels/constants in place of texture_piezo's own module-local copies (same
values, ``C``/``L``/``R``/``T``/``B`` and friends -- see
core/piezo_engine/shear_detector.py, which already made this same swap).
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from constants.shear import (
    NORMAL_FORCE_SENSOR_COUNT,
    SHEAR_POSITION_BOTTOM,
    SHEAR_POSITION_CENTER,
    SHEAR_POSITION_LEFT,
    SHEAR_POSITION_RIGHT,
    SHEAR_POSITION_TOP,
    SHEAR_ZERO_VALUE,
)


def compute_shear_normal_batch(
    centered_by_position: Mapping[str, np.ndarray],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorized, bit-identical replacement for looping
    ShearDetector.detect() + NormalForceCalculator.compute() once per
    sample, returning only (shear_lr, shear_tb, total_force) -- the sole
    fields CausalDerivedChannels.process() actually reads. Does NOT replace
    detect()/compute() themselves (GUI/live per-sample callers still use
    those, unchanged) and does NOT compute x_mm/y_mm/force_type/etc, since
    nothing here consumes them.

    Safe to vectorize: unlike the causal-median/bounded-sum steps upstream,
    detect()/compute() are pure per-sample functions with no cross-sample
    state -- each output depends only on that sample's 5 centered values.

    Every branch here must reproduce detect()/compute()'s float arithmetic
    in the exact same operation order (see ShearDetector/NormalForceCalculator
    in shear_detector.py/normal_force_calculator.py for the reference
    sequence); ported unchanged from texture_piezo's
    shear_normal_utils_v1.compute_shear_normal_batch, which was verified
    against a per-sample loop over both real data and an adversarial
    synthetic array covering every branch (residual_C == 0.0 outer-tie
    cases, the 1e-12 baseline-offset epsilon, -0.0 sign handling).
    """
    c = np.asarray(centered_by_position[SHEAR_POSITION_CENTER], dtype=np.float64)
    left = np.asarray(centered_by_position[SHEAR_POSITION_LEFT], dtype=np.float64)
    right = np.asarray(centered_by_position[SHEAR_POSITION_RIGHT], dtype=np.float64)
    top = np.asarray(centered_by_position[SHEAR_POSITION_TOP], dtype=np.float64)
    bottom = np.asarray(centered_by_position[SHEAR_POSITION_BOTTOM], dtype=np.float64)

    # ── ShearDetector.detect() ──────────────────────────────────────────
    lr_pair = (left != SHEAR_ZERO_VALUE) & (right != SHEAR_ZERO_VALUE) & (np.sign(left) != np.sign(right))
    tb_pair = (top != SHEAR_ZERO_VALUE) & (bottom != SHEAR_ZERO_VALUE) & (np.sign(top) != np.sign(bottom))

    b_lr = np.where(lr_pair, np.copysign(np.minimum(np.abs(left), np.abs(right)), right), SHEAR_ZERO_VALUE)
    b_tb = np.where(tb_pair, np.copysign(np.minimum(np.abs(top), np.abs(bottom)), top), SHEAR_ZERO_VALUE)

    # strain_vector: C=0, L=-b_lr, R=b_lr, T=b_tb, B=-b_tb ; residual = calibrated - strain_vector
    residual_c = c
    residual_l = left + b_lr
    residual_r = right - b_lr
    residual_t = top + b_tb
    residual_b = bottom - b_tb

    # ── NormalForceCalculator.compute() ─────────────────────────────────
    # force_type: direct from center when nonzero, else inferred from outers.
    direct_compression = residual_c > SHEAR_ZERO_VALUE
    direct_tension = residual_c < SHEAR_ZERO_VALUE
    center_zero = ~direct_compression & ~direct_tension

    outer_pos_count = (
        (left > SHEAR_ZERO_VALUE).astype(np.int64) + (right > SHEAR_ZERO_VALUE) + (top > SHEAR_ZERO_VALUE) + (bottom > SHEAR_ZERO_VALUE)
    )
    outer_neg_count = (
        (left < SHEAR_ZERO_VALUE).astype(np.int64) + (right < SHEAR_ZERO_VALUE) + (top < SHEAR_ZERO_VALUE) + (bottom < SHEAR_ZERO_VALUE)
    )
    infer_compression = outer_pos_count > outer_neg_count
    infer_tension = outer_neg_count > outer_pos_count
    infer_tie = ~infer_compression & ~infer_tension

    # Zero-filled terms for values that don't pass the filter reproduce
    # sum(filtered_list) bit-for-bit (x + 0.0 == x for finite floats), so
    # this matches the original's list-comprehension-then-sum exactly.
    pos_mag = (
        np.where(left > SHEAR_ZERO_VALUE, left, SHEAR_ZERO_VALUE)
        + np.where(right > SHEAR_ZERO_VALUE, right, SHEAR_ZERO_VALUE)
        + np.where(top > SHEAR_ZERO_VALUE, top, SHEAR_ZERO_VALUE)
        + np.where(bottom > SHEAR_ZERO_VALUE, bottom, SHEAR_ZERO_VALUE)
    )
    neg_mag = (
        np.where(left < SHEAR_ZERO_VALUE, -left, SHEAR_ZERO_VALUE)
        + np.where(right < SHEAR_ZERO_VALUE, -right, SHEAR_ZERO_VALUE)
        + np.where(top < SHEAR_ZERO_VALUE, -top, SHEAR_ZERO_VALUE)
        + np.where(bottom < SHEAR_ZERO_VALUE, -bottom, SHEAR_ZERO_VALUE)
    )
    tie_compression = infer_tie & (pos_mag > neg_mag)
    tie_tension = infer_tie & (neg_mag > pos_mag)

    is_compression = direct_compression | (center_zero & (infer_compression | tie_compression))
    is_tension = direct_tension | (center_zero & (infer_tension | tie_tension))

    outer_min = np.minimum(np.minimum(left, right), np.minimum(top, bottom))
    outer_max = np.maximum(np.maximum(left, right), np.maximum(top, bottom))
    baseline_offset = np.where(
        is_compression, np.maximum(SHEAR_ZERO_VALUE, outer_min),
        np.where(is_tension, np.minimum(SHEAR_ZERO_VALUE, outer_max), SHEAR_ZERO_VALUE),
    )

    normalized_c = residual_c - baseline_offset
    normalized_l = residual_l - baseline_offset
    normalized_r = residual_r - baseline_offset
    normalized_t = residual_t - baseline_offset
    normalized_b = residual_b - baseline_offset
    baseline_force = float(NORMAL_FORCE_SENSOR_COUNT) * baseline_offset
    # Chained left-to-right in SENSOR_POSITIONS order (C, L, R, T, B), then
    # + baseline_force -- reproduces sum(normalized.values()) + baseline_force
    # bit-for-bit; do not use np.sum/np.add.reduce here (pairwise summation
    # reorders and can round differently for this exact-equality contract).
    total_force = normalized_c + normalized_l + normalized_r + normalized_t + normalized_b + baseline_force

    return b_lr, b_tb, total_force
