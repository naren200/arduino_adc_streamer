"""Test-only explicit GatingV2Config values (the config itself has no defaults by design)."""

from core.texture_piezo.gating.v2.config import GatingV2Config

TEST_BLOCK_S = 0.04
TEST_SUSTAIN_BLOCKS = 2
TEST_EXPIRY_S = 3.0


def make_v2_config(**overrides) -> GatingV2Config:
    values = dict(
        idle_gate_enabled=False, strength_gate_enabled=True, concat_enabled=True, burst_min=10.0, sustain_min=1,
        block_s=TEST_BLOCK_S, sustain_blocks=TEST_SUSTAIN_BLOCKS, expiry_s=TEST_EXPIRY_S,
    )
    return GatingV2Config(**{**values, **overrides})
