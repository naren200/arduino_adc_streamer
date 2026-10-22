"""compute_gate_replay: the Analysis tab's gate preview is the TouchID replay itself (same ReplayDriver)."""

import numpy as np
import pytest

from core.texture_piezo.application.gating_trace import COMBINED_LANE, STRENGTH_LANE
from core.texture_piezo.application.stream_processor import DerivedChannelPipeline
from data_processing.gate_replay import compute_gate_replay, label_overlap_line, result_line
from test_replay_driver import FS, PZT_COLUMNS, make_baseline, make_driver, make_setup, make_snapshot


def test_window_ranges_equal_those_of_a_touchid_replay_over_the_same_snapshot_and_setup():
    snapshot, setup = make_snapshot(), make_setup()

    result = compute_gate_replay(snapshot, setup)

    replayed = make_driver(snapshot, setup).run_to_end()
    assert len(replayed) > 1
    assert [window.ranges for window in result.trace.windows] == [window.ranges for window in replayed]


def test_the_result_carries_the_setup_the_strategys_description_and_the_replays_own_normal_jerk():
    snapshot, setup = make_snapshot(), make_setup()

    result = compute_gate_replay(snapshot, setup)

    assert result.setup is setup
    assert "Strength:" in result.logic_text
    assert len(result.normal_jerk) == len(result.timestamps_s) < snapshot.sweep_count
    live = DerivedChannelPipeline(list(PZT_COLUMNS), setup.engine_config, setup.input_conditioning)
    columns = {name: snapshot.data[:, index].astype(np.float64) for index, name in enumerate(PZT_COLUMNS)}
    _raw, derived, _timestamps = live.process(live.filter_raw(columns), snapshot.timestamps_s, FS)
    np.testing.assert_allclose(result.normal_jerk, derived["normal_jerk"])


def test_a_v2_replay_reports_its_lanes_and_burst_scores_and_accepts_only_what_windows_used():
    result = compute_gate_replay(make_snapshot(), make_setup())

    assert {lane.name for lane in result.trace.lanes} >= {STRENGTH_LANE, COMBINED_LANE}
    assert result.burst is not None and result.burst.burst_min == result.setup.gating_v2_config.burst_min
    strength = result.lane_spans_s(STRENGTH_LANE)
    for start, end in result.accepted_spans_s():
        assert any(good_start <= start and end <= good_end for good_start, good_end in strength)


def test_a_v1_replay_reports_windows_only_and_has_no_burst_scores():
    setup = make_setup(gating_version="v1", gating_v2_config=None, idle_baseline=make_baseline())

    result = compute_gate_replay(make_snapshot(), setup)

    assert result.trace.lanes == () and result.burst is None
    assert result.trace.windows and result.accepted_spans_s()


def test_a_source_missing_a_configured_channel_is_refused():
    snapshot = make_snapshot()
    snapshot.channel_labels = list(PZT_COLUMNS[:-1]) + ["other"]

    with pytest.raises(ValueError, match="channel"):
        compute_gate_replay(snapshot, make_setup())


def test_result_and_label_overlap_lines():
    result = compute_gate_replay(make_snapshot(), make_setup())
    assert result_line(result).startswith("Result:") and "windows" in result_line(result)
    line = label_overlap_line([(0.0, 2.0)], [(1.0, 3.0)])
    assert "2.0 s labelled, 50% of it accepted" in line and "1.0 s (50%)" in line
