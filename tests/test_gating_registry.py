import pytest

from core.texture_piezo.application.gating_registry import get_gating
from core.texture_piezo.application.gating_strategy import GatingParams
from core.texture_piezo.application.v1_gating import V1Gating, V1GatingOptions
from core.texture_piezo.application.v2_gating import V2Gating


def _params_without_v2_config() -> GatingParams:
    return GatingParams(
        pzt_columns=("PZT3_B",), required_channels=("PZT3_B",), window_size_s=0.5, hop_size_s=0.1,
        idle_baseline=None, onset_skip_s=0.0, v1_options=V1GatingOptions(span_stale_timeout_s=1.0),
    )


def test_v1_resolves_to_the_v1_strategy():
    assert get_gating("v1") is V1Gating


def test_v2_resolves_to_the_v2_strategy():
    assert get_gating("v2") is V2Gating


def test_v2_cannot_be_built_without_its_config():
    with pytest.raises(ValueError, match="v2_config"):
        get_gating("v2")(_params_without_v2_config())


def test_an_unknown_version_names_the_available_ones():
    with pytest.raises(ValueError, match=r"unknown gating version 'v9'.*\['v1', 'v2'\]"):
        get_gating("v9")


def _processor_kwargs() -> dict:
    return dict(pzt_columns=["PZT3_B", "PZT3_L", "PZT3_C", "PZT3_R", "PZT3_T"], window_size_s=0.5, hop_size_s=0.1,
                span_stale_timeout_s=1.0, idle_baseline=None)


def test_a_v2_config_with_v1_gating_is_refused_not_ignored():
    from core.texture_piezo.application.stream_processor import TouchIdStreamProcessor
    from core.texture_piezo.gating.v2.config import GatingV2Config

    config = GatingV2Config(idle_gate_enabled=False, strength_gate_enabled=True, concat_enabled=True, burst_min=1.0, sustain_min=1,
        block_s=0.04, sustain_blocks=2, expiry_s=3.0,
    )
    with pytest.raises(ValueError, match="v2_config"):
        TouchIdStreamProcessor(**_processor_kwargs(), gating_version="v1", gating_v2_config=config)


def test_v2_gating_without_a_config_is_refused_by_the_processor():
    from core.texture_piezo.application.stream_processor import TouchIdStreamProcessor

    with pytest.raises(ValueError, match="v2_config"):
        TouchIdStreamProcessor(**_processor_kwargs(), gating_version="v2")
