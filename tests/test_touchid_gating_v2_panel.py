"""TouchID tab <-> gating v2: the loaded model picks the gating strategy, a v2 model's training
parameters seed an editable settings group, edits rebuild the processor, and a window made of
several stretches of the stream is painted one region per stretch."""

import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from core.piezo_engine.live_window import LiveWindow
from core.texture_piezo.application import inference_config
from core.texture_piezo.application.gating_strategy import ReadyWindow
from core.texture_piezo.application.mode import TouchIdMode
from core.texture_piezo.gating.v1.quality_gate import IdleBaseline
from core.texture_piezo.application.inference_config import InferenceConfig, load_touchid_settings, save_touchid_settings
from core.texture_piezo.application.live_channels import DEFAULT_CHANNELS
from core.texture_piezo.application.stream_processor import LIVE_ENGINE_CONFIG
from core.texture_piezo.models.classifier import TextureClassifier
from core.texture_piezo.application.v1_gating import V1Gating
from core.texture_piezo.application.v2_gating import V2Gating
from core.texture_piezo.gating.v2.config import GatingV2Config
from inference import texture_piezo_adapter
from core.piezo_engine.pipeline import RAW_INPUT
from test_inference_panel import TouchIdHarness
from test_replay_driver import make_driver, make_snapshot

TRAINED_PARAMS = {
    "version": "v2", "idle_gate_enabled": False, "strength_gate_enabled": True, "concat_enabled": True,
    "burst_min": 50.0, "sustain_min": 1.0, "block_s": 0.04, "sustain_blocks": 2, "expiry_s": 3.0,
}
TRAINED_CONFIG = GatingV2Config(
    idle_gate_enabled=False, strength_gate_enabled=True, concat_enabled=True,
    burst_min=50.0, sustain_min=1, block_s=0.04, sustain_blocks=2, expiry_s=3.0,
)


def fake_model(gating_version="v2", params=None):
    """A loaded model as the tab sees it (TextureClassifier's surface): gating version and, for v2, its training params."""
    return SimpleNamespace(
        engine_config=LIVE_ENGINE_CONFIG.to_dict(), required_channels=DEFAULT_CHANNELS,
        expected_ingest_blip_filter=True, gating_version=gating_version,
        gating_v2_params=TRAINED_PARAMS if gating_version == "v2" and params is None else params,
    )


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def harness():
    with patch("gui.inference_panel.save_touchid_settings") as save, patch("gui.inference_panel.save_idle_baseline"):
        host = TouchIdHarness()
        host.log = []
        host.log_status = host.log.append
        host.saved = save
        host.touchid_idle_baseline = None
        host.touchid_config.gating_v2_overrides = {}
        yield host


def load(host, model):
    """Swap the loaded model the way a reload does, after its classifier is built."""
    with patch("gui.inference_panel.TextureClassifier", return_value=model):
        host._touchid_reload_model()


def live_config(host):
    return host.touchid_processor.gating_v2_config


def test_a_model_without_gating_v2_gets_v1_gating_and_no_v2_group(harness):
    load(harness, fake_model("v1"))
    assert harness.touchid_processor.gating_version == "v1"
    assert isinstance(harness.touchid_processor.gating, V1Gating)
    assert live_config(harness) is None
    assert harness.touchid_v2_group.isHidden()


def test_no_model_loaded_keeps_v1_gating(harness):
    harness.touchid_classifier = None
    assert harness._touchid_new_processor().gating_version == "v1"


def test_a_v2_model_gets_v2_gating_built_from_its_bundle_params(harness):
    load(harness, fake_model())
    assert harness.touchid_processor.gating_version == "v2"
    assert isinstance(harness.touchid_processor.gating, V2Gating)
    assert live_config(harness) == TRAINED_CONFIG
    assert not harness.touchid_v2_group.isHidden()
    assert harness.touchid_v2_editors["burst_min"].value() == 50.0
    assert harness.touchid_v2_editors["strength_gate_enabled"].isChecked()
    assert not harness.touchid_v2_editors["idle_gate_enabled"].isChecked()


def test_switching_family_rebuilds_the_strategy_and_carries_the_engine_state(harness):
    load(harness, fake_model("v1"))
    with patch("core.texture_piezo.application.stream_processor.TouchIdStreamProcessor.adopt_engine_state_from") as adopt:
        load(harness, fake_model())
        assert isinstance(harness.touchid_processor.gating, V2Gating)
        load(harness, fake_model("v1"))
    assert isinstance(harness.touchid_processor.gating, V1Gating)
    assert adopt.call_count == 2
    assert harness.touchid_v2_group.isHidden()


def test_reloading_an_unchanged_v2_model_keeps_the_running_processor(harness):
    load(harness, fake_model())
    running = harness.touchid_processor
    load(harness, fake_model())
    assert harness.touchid_processor is running


def test_a_matching_config_shows_no_difference_indicator(harness):
    load(harness, fake_model())
    assert harness.touchid_v2_differs_label.text() == ""
    assert not harness.touchid_v2_reset_btn.isEnabled()


def test_editing_a_field_rebuilds_the_processor_and_flags_the_difference(harness):
    load(harness, fake_model())
    before = harness.touchid_processor
    harness.touchid_v2_editors["burst_min"].setValue(80.0)
    assert harness.touchid_processor is not before
    assert live_config(harness).burst_min == 80.0
    assert harness.touchid_config.gating_v2_overrides == {"burst_min": 80.0}
    assert "burst_min=80.0 (trained 50.0)" in harness.touchid_v2_differs_label.text()
    assert "DIFFERS FROM TRAINING" in harness.touchid_v2_differs_label.text()
    assert harness.touchid_v2_reset_btn.isEnabled()
    harness.saved.assert_called()


def test_a_checkbox_edit_changes_that_flag_only(harness):
    load(harness, fake_model())
    harness.touchid_v2_editors["concat_enabled"].setChecked(False)
    assert live_config(harness).concat_enabled is False
    assert live_config(harness).burst_min == TRAINED_CONFIG.burst_min
    assert harness.touchid_config.gating_v2_overrides == {"concat_enabled": False}


def test_editing_a_field_back_to_the_training_value_clears_the_difference(harness):
    load(harness, fake_model())
    harness.touchid_v2_editors["burst_min"].setValue(80.0)
    harness.touchid_v2_editors["burst_min"].setValue(50.0)
    assert harness.touchid_config.gating_v2_overrides == {}
    assert harness.touchid_v2_differs_label.text() == ""


def test_reset_restores_the_training_values(harness):
    load(harness, fake_model())
    harness.touchid_v2_editors["sustain_blocks"].setValue(4)
    harness.touchid_v2_editors["idle_gate_enabled"].setChecked(True)
    harness.touchid_v2_reset_btn.click()
    assert live_config(harness) == TRAINED_CONFIG
    assert harness.touchid_v2_editors["sustain_blocks"].value() == 2
    assert not harness.touchid_v2_editors["idle_gate_enabled"].isChecked()
    assert harness.touchid_v2_differs_label.text() == ""


def test_a_setting_the_gating_refuses_is_rolled_back_and_reported(harness):
    load(harness, fake_model())
    running = harness.touchid_processor
    harness.touchid_v2_editors["expiry_s"].setValue(0.2)  # below the 0.5 s window: no window could accumulate
    assert harness.touchid_processor is running
    assert harness.touchid_config.gating_v2_overrides == {}
    assert harness.touchid_v2_editors["expiry_s"].value() == 3.0
    assert "refused" in harness.log[-1]


def test_a_persisted_override_is_applied_to_the_next_v2_model(harness):
    harness.touchid_config.gating_v2_overrides = {"burst_min": 80.0}
    load(harness, fake_model())
    assert live_config(harness).burst_min == 80.0
    assert harness.touchid_v2_editors["burst_min"].value() == 80.0


def test_idle_gate_without_a_baseline_shows_the_status_until_one_is_captured(harness):
    load(harness, fake_model())
    harness.touchid_v2_editors["idle_gate_enabled"].setChecked(True)
    assert harness.touchid_v2_status_label.text() == "idle baseline needed"

    harness.touchid_idle_capture_samples = [np.random.randn(200, 5) * 2 + 500]
    harness._finish_touchid_idle_capture(1000.0)
    assert harness.touchid_processor.idle_baseline is harness.touchid_idle_baseline
    assert harness.touchid_v2_status_label.text() == ""


def test_no_status_when_the_idle_gate_is_off(harness):
    load(harness, fake_model())
    assert harness.touchid_v2_status_label.text() == ""


def test_overrides_survive_a_save_and_load_of_the_settings(tmp_path):
    path = tmp_path / "settings.json"
    config = InferenceConfig()
    config.gating_v2_overrides = {"burst_min": 80.0, "concat_enabled": False}
    with patch.object(inference_config, "_get_last_touchid_settings_path", return_value=path):
        save_touchid_settings(config)
        restored = load_touchid_settings(InferenceConfig())
    assert restored.gating_v2_overrides == {"burst_min": 80.0, "concat_enabled": False}


@pytest.mark.parametrize("spin_name, attribute, refused_value", [
    ("touchid_window_spin", "window_size_s", 4.0),  # beyond the 3 s expiry_s: no window could ever fit
])
def test_a_window_size_the_v2_gating_refuses_is_rolled_back_and_reported(harness, spin_name, attribute, refused_value):
    load(harness, fake_model())
    running, previous = harness.touchid_processor, getattr(harness.touchid_config, attribute)
    getattr(harness, spin_name).setValue(refused_value)
    assert harness.touchid_processor is running
    assert getattr(harness.touchid_config, attribute) == previous
    assert getattr(harness, spin_name).value() == previous
    assert "refused" in harness.log[-1]


def region_spans(host):
    return [tuple(region.getRegion()) for _end, region in host.touchid_stream_inference_regions]


def test_a_single_range_window_paints_one_region(harness):
    harness.touchid_stream_display_t0 = 0.0
    harness._touchid_set_inference_region(np.arange(5) * 0.1, span_id=0)
    assert region_spans(harness) == [pytest.approx((0.0, 0.4))]


def test_a_multi_range_window_paints_one_region_per_range(harness):
    harness.touchid_stream_display_t0 = 0.0
    window_ts = np.array([1.0, 1.1, 1.2, 2.0, 2.1, 5.0])
    harness._touchid_set_inference_region(window_ts, span_id=0, seam_idx=(3, 5))
    assert region_spans(harness) == [pytest.approx(span) for span in ((1.0, 1.2), (2.0, 2.1), (5.0, 5.0))]


def test_ready_windows_are_painted_from_their_seams(harness):
    harness.touchid_stream_display_t0 = 0.0
    harness.touchid_worker_busy = True  # paint only; submission is not under test
    channels = {name: np.zeros(4) for name in DEFAULT_CHANNELS}
    window = LiveWindow(channels, 1000.0, seam_idx=(2,))
    ready = ReadyWindow(window=window, window_ts=np.array([0.0, 0.001, 0.5, 0.501]), frag_id=3, ranges=((0, 2), (500, 502)))
    harness._touchid_handle_ready_windows([ready])
    assert region_spans(harness) == [pytest.approx((0.0, 0.001)), pytest.approx((0.5, 0.501))]


def test_adapter_reads_the_gating_version_and_bundle_params_off_the_runtime():
    bundle = SimpleNamespace(gating_params=SimpleNamespace(to_bundle_dict=lambda: TRAINED_PARAMS))
    chunk = SimpleNamespace(GATING_VERSION="v2", bundle=bundle)
    assert texture_piezo_adapter.gating_version_of(chunk) == "v2"
    assert texture_piezo_adapter.gating_v2_params_of(chunk) == TRAINED_PARAMS
    assert texture_piezo_adapter.gating_version_of(SimpleNamespace(GATING_VERSION="v1")) == "v1"


def test_the_classifier_exposes_v2_params_only_for_a_v2_runtime():
    bundle = SimpleNamespace(gating_params=SimpleNamespace(to_bundle_dict=lambda: TRAINED_PARAMS))
    with patch("core.texture_piezo.models.classifier.TextureClassifier.__init__", return_value=None):
        classifier = TextureClassifier(None)
    classifier._runtime = SimpleNamespace(GATING_VERSION="v2", bundle=bundle)
    assert classifier.gating_v2_params == TRAINED_PARAMS
    classifier._runtime = SimpleNamespace(GATING_VERSION="v1")
    assert classifier.gating_v2_params is None


IDLE_GATED_PARAMS = {**TRAINED_PARAMS, "idle_gate_enabled": True}
NO_BASELINE_PROMPT = "No idle baseline exists. Continue without the idle gate?"


def arm_replay_source(host):
    """An Analysis snapshot carrying the host's PZT columns, so a run passes every precondition up to the baseline."""
    columns = list(host.touchid_config.pzt_columns)
    host.analysis_snapshot = SimpleNamespace(
        sample_rate_hz=1000.0, channel_labels=columns, sweep_count=200, data=np.zeros((200, len(columns))),
        timestamps_s=np.arange(200) / 1000.0, metadata={},
    )
    host.analysis_label_segments = []


def run_on_source(host, answer):
    """Click Run on Analysis Source, answering the no-baseline question; returns the question calls."""
    with patch("gui.inference_panel.QMessageBox.question", return_value=answer) as question:
        host.on_touchid_run_on_source_clicked()
    timer = getattr(host, "_touchid_replay_timer", None)
    if timer is not None:
        timer.stop()
    return question


def test_replay_without_baseline_asks_before_running_an_idle_gated_model(harness):
    load(harness, fake_model(params=IDLE_GATED_PARAMS))
    arm_replay_source(harness)
    question = run_on_source(harness, QMessageBox.StandardButton.No)
    assert question.call_args.args[2] == NO_BASELINE_PROMPT


def test_declining_the_no_baseline_question_changes_nothing(harness):
    load(harness, fake_model(params=IDLE_GATED_PARAMS))
    arm_replay_source(harness)
    processor = harness.touchid_processor
    run_on_source(harness, QMessageBox.StandardButton.No)
    assert harness.touchid_mode == TouchIdMode.NORMAL
    assert harness.touchid_processor is processor
    assert harness.touchid_config.gating_v2_overrides == {}
    assert harness.touchid_v2_editors["idle_gate_enabled"].isChecked()


def test_accepting_the_no_baseline_question_turns_the_idle_gate_off_and_persists_it(harness):
    load(harness, fake_model(params=IDLE_GATED_PARAMS))
    arm_replay_source(harness)
    run_on_source(harness, QMessageBox.StandardButton.Yes)
    assert harness.touchid_mode == TouchIdMode.REPLAYING
    assert harness.touchid_config.gating_v2_overrides == {"idle_gate_enabled": False}
    assert not harness.touchid_v2_editors["idle_gate_enabled"].isChecked()
    assert live_config(harness).idle_gate_enabled is False
    harness.saved.assert_called()


def test_a_v2_model_without_an_idle_gate_is_not_asked(harness):
    load(harness, fake_model())
    arm_replay_source(harness)
    question = run_on_source(harness, QMessageBox.StandardButton.No)
    question.assert_not_called()
    assert harness.touchid_mode == TouchIdMode.REPLAYING


def test_an_existing_baseline_is_not_asked_about(harness):
    load(harness, fake_model(params=IDLE_GATED_PARAMS))
    arm_replay_source(harness)
    harness.touchid_idle_baseline = IdleBaseline(
        pzt_columns=list(harness.touchid_config.pzt_columns), mean=[0.0] * 5, std=[1.0] * 5, fs=1000.0)
    question = run_on_source(harness, QMessageBox.StandardButton.No)
    question.assert_not_called()
    assert harness.touchid_mode == TouchIdMode.REPLAYING


def test_v1_replay_without_baseline_asks_and_runs_on_the_fixed_grid_when_accepted(harness):
    load(harness, fake_model("v1"))
    arm_replay_source(harness)
    question = run_on_source(harness, QMessageBox.StandardButton.Yes)
    question.assert_called_once()
    assert harness.touchid_mode == TouchIdMode.REPLAYING
    assert harness.touchid_config.gating_v2_overrides == {}


def test_v1_replay_without_baseline_aborts_when_declined(harness):
    load(harness, fake_model("v1"))
    arm_replay_source(harness)
    run_on_source(harness, QMessageBox.StandardButton.No)
    assert harness.touchid_mode == TouchIdMode.NORMAL


def test_replay_ticks_submit_every_emitted_window_in_order(harness):
    params = {**TRAINED_PARAMS, "burst_min": 1.0, "sustain_min": 3, "sustain_blocks": 7, "expiry_s": 1.0}
    load(harness, fake_model(params=params))
    harness.touchid_config.window_size_s, harness.touchid_config.hop_size_s = 0.1, 0.05
    harness._rebuild_touchid_buffers()
    snapshot = make_snapshot()
    snapshot.channel_labels = list(harness.touchid_config.pzt_columns)
    harness.analysis_snapshot, harness.analysis_label_segments = snapshot, []
    submitted, finished = [], []
    harness._touchid_submit_window = submitted.append
    harness._touchid_finish_replay = lambda *args, **kwargs: finished.append(True)
    run_on_source(harness, QMessageBox.StandardButton.No)
    while not finished:
        harness._touchid_replay_tick()
    expected = make_driver(snapshot, harness._touchid_replay_setup(input_conditioning=RAW_INPUT)).run_to_end()
    assert len(submitted) == len(expected) > 1
    assert [w.window_ts[0] for w in submitted] == [w.window_ts[0] for w in expected]
