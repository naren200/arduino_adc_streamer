"""Structured gating events, the config contract and the optional tuning capabilities."""

import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from core.texture_piezo.application.gating_strategy import GateKTunable, GatingParams, OnsetSkipTunable
from core.texture_piezo.application.stream_processor import TouchIdStreamProcessor
from core.texture_piezo.application.v1_gating import V1Gating
from core.texture_piezo.application.v2_gating import V2Gating
from gating_v2_support import make_v2_config
from test_gating_v2_strategy import _baseline, _chunk, _config, _gating, _run, _signals

V2_LOGGER = "texture_piezo.gating.v2"
V1_LOGGER = "texture_piezo.gating.v1"
RANGE_FIELDS = {"range_id", "start_idx", "end_idx", "start_ts", "end_ts"}
WINDOW_FIELDS = {"range_ids", "n_ranges", "n_seams", "good_duration_s", "first_sweep_ts", "last_sweep_ts"}


def _events(caplog, name: str = V2_LOGGER) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == name]


def _named(records, message: str) -> list[logging.LogRecord]:
    return [record for record in records if record.getMessage() == message]


def _fields(record: logging.LogRecord, names: set[str]) -> dict:
    return {name: getattr(record, name) for name in names}


def test_range_and_window_events_carry_their_fields(caplog):
    caplog.set_level(logging.DEBUG, logger=V2_LOGGER)
    windows = _run(_gating(_config()), _signals(), tick=100)
    records = _events(caplog)

    opened = _named(records, "range_opened")
    assert [r.range_id for r in opened] == [0, 1]
    assert all(set(_fields(r, RANGE_FIELDS)) == RANGE_FIELDS for r in opened)
    assert _named(records, "range_extended") and _named(records, "range_merged")
    first_window = _named(records, "window_emitted")[0]
    assert _fields(first_window, {"range_ids", "n_ranges", "n_seams"}) == {
        "range_ids": (0, 1), "n_ranges": 2, "n_seams": 1,
    }
    assert first_window.good_duration_s == pytest.approx(windows[0].window.duration_s)
    assert first_window.first_sweep_ts == windows[0].window_ts[0]
    assert first_window.last_sweep_ts == windows[0].window_ts[-1]
    assert len(_named(records, "window_emitted")) == len(windows)
    assert set(_fields(first_window, WINDOW_FIELDS)) == WINDOW_FIELDS


def test_extended_is_reported_for_a_range_that_grows_across_ticks(caplog):
    caplog.set_level(logging.DEBUG, logger=V2_LOGGER)
    _run(_gating(_config()), _signals(bursts=((1000, 1800),)), tick=100)
    extended = _named(_events(caplog), "range_extended")
    assert extended and {r.range_id for r in extended} == {0}
    assert [r.end_idx for r in extended] == sorted(r.end_idx for r in extended)


def test_range_expired_names_the_dropped_range(caplog):
    caplog.set_level(logging.DEBUG, logger=V2_LOGGER)
    _run(_gating(_config(expiry_s=0.8)), _signals(bursts=((1000, 1200),), n_samples=3000), tick=100)
    expired = _named(_events(caplog), "range_expired")
    assert [r.range_id for r in expired] == [0]
    assert expired[0].end_idx == 1200  # the head may already have been cut shorter by earlier expiries


def test_waiting_for_baseline_is_announced_once(caplog):
    caplog.set_level(logging.DEBUG, logger=V2_LOGGER)
    gating = _gating(_config(idle_gate_enabled=True, strength_gate_enabled=False), baseline=None)
    _run(gating, _signals(), tick=100)
    assert len(_named(_events(caplog), "gate_waiting_for_baseline")) == 1
    gating.reset()
    _run(gating, _signals(), tick=100)
    assert len(_named(_events(caplog), "gate_waiting_for_baseline")) == 2


def test_no_waiting_event_when_the_baseline_is_present(caplog):
    caplog.set_level(logging.DEBUG, logger=V2_LOGGER)
    _run(_gating(_config(idle_gate_enabled=True), baseline=_baseline()), _signals(), tick=100)
    assert not _named(_events(caplog), "gate_waiting_for_baseline")


def test_nothing_is_logged_when_debug_is_off(caplog):
    caplog.set_level(logging.INFO, logger=V2_LOGGER)
    gating = _gating(_config(idle_gate_enabled=True, strength_gate_enabled=False))
    assert _run(gating, _signals(), tick=100)
    assert _events(caplog) == []


def test_v1_emits_window_emitted_with_frag_id_and_sample_count(caplog):
    caplog.set_level(logging.DEBUG, logger=V1_LOGGER)
    gating = V1Gating(GatingParams(
        pzt_columns=("PZT3_B",), required_channels=("PZT3_B",), window_size_s=0.1, hop_size_s=0.05,
        idle_baseline=None, onset_skip_s=0.0, v1_options=_v1_options(),
    ))
    windows = gating.push(_chunk(*_signals(), 0, 200))
    emitted = _named(_events(caplog, V1_LOGGER), "window_emitted")
    assert len(emitted) == len(windows) > 0
    assert (emitted[0].frag_id, emitted[0].n_samples) == (None, windows[0].window.n_samples)


def _v1_options():
    from core.texture_piezo.application.v1_gating import V1GatingOptions
    return V1GatingOptions(span_stale_timeout_s=1.0)


# --- config contract --------------------------------------------------------------------------


def test_config_fields_have_no_defaults():
    from dataclasses import MISSING, fields

    from core.texture_piezo.gating.v2.config import GatingV2Config

    assert all(field.default is MISSING for field in fields(GatingV2Config))


def test_sustain_min_is_stored_as_an_int_and_an_integral_float_is_converted():
    config = make_v2_config(sustain_min=2.0, burst_min=3)
    assert config.sustain_min == 2 and isinstance(config.sustain_min, int)
    assert config.burst_min == 3.0 and isinstance(config.burst_min, float)


@pytest.mark.parametrize("bad", [1.5, True, -1, float("nan"), "2"])
def test_sustain_min_must_be_a_non_negative_integer(bad):
    with pytest.raises(ValueError, match="sustain_min"):
        make_v2_config(sustain_min=bad)


@pytest.mark.parametrize("bad", [-0.1, True, float("inf"), "1"])
def test_burst_min_must_be_a_non_negative_finite_number(bad):
    with pytest.raises(ValueError, match="burst_min"):
        make_v2_config(burst_min=bad)


# --- optional tuning capabilities -------------------------------------------------------------


def test_both_strategies_implement_both_capabilities():
    for strategy in (V1Gating, V2Gating):
        assert issubclass(strategy, OnsetSkipTunable) and issubclass(strategy, GateKTunable)


def test_the_processor_tunes_only_what_the_strategy_supports():
    class MinimalStrategy:
        def push(self, chunk):
            return []

        def reset(self):
            pass

    class OnsetOnlyStrategy(MinimalStrategy):
        def __init__(self):
            self.onset_skip_s = None

        def set_onset_skip_s(self, onset_skip_s):
            self.onset_skip_s = onset_skip_s

    processor = TouchIdStreamProcessor(
        pzt_columns=["PZT3_B", "PZT3_L", "PZT3_C", "PZT3_R", "PZT3_T"], window_size_s=0.5, hop_size_s=0.1,
        span_stale_timeout_s=1.0, idle_baseline=None,
    )
    processor.gating = MinimalStrategy()
    processor.set_onset_skip_s(0.2)
    processor.set_gate_k(3.0)
    processor.gating = OnsetOnlyStrategy()
    processor.set_onset_skip_s(0.3)
    processor.set_gate_k(3.0)
    assert processor.gating.onset_skip_s == 0.3
