"""Headless end-to-end replay of a real capture through the TouchID path for every real-checkpoint model family (pinned ann/quad/penta, newest loadable chunk).

The capture goes through the same steps as ``InferencePanel``'s replay (snapshot loader ->
TouchIdStreamProcessor on the loaded model's EngineConfig and required channels -> LiveWindow
per ready window -> texture_piezo runtime). Real checkpoints for ann / quad / penta / chunk.

Checked per family:
  (i)   it runs and classifies windows;
  (ii)  every LiveWindow equals the whole-file engine output sliced at the same samples;
  (iii) the probabilities the app returns equal an independently loaded texture_piezo runtime
        fed the same LiveWindow (wiring check).
"""

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

import data_processing.analysis_workbench as workbench
from core.piezo_engine.pipeline import (
    INPUT_CONDITIONING_METADATA_KEY, PiezoEnginePipeline, input_conditioning_from_record,
)
from core.texture_piezo.application.inference_config import InferenceConfig, pzt_columns_for_sensor, set_model_version
from core.texture_piezo.application.live_channels import named_engine_channels
from core.texture_piezo.application.live_model import engine_config_for_model, required_channels_for_model
from core.texture_piezo.application.stream_processor import TouchIdStreamProcessor
from core.texture_piezo.gating.quality_gate import fit_idle_baseline
from core.texture_piezo.models import model_discovery
from core.texture_piezo.models.classifier import TextureClassifier
from core.texture_piezo.models.model_discovery import ModelArtifacts
from inference import texture_piezo_adapter as adapter
from inference._paths import TEXTURE_PIEZO_ROOT

CAPTURE_STEM = "only_cardboard_and_idle_v1_20260914_1635"
CAPTURE_DIR = TEXTURE_PIEZO_ROOT / "data" / "raw" / "sensor_v12d_7_26" / "ch5"
SENSOR_NUMBER = "5"
SPOT_CHECK_STRIDE = 7
RECORDED_PREDICTIONS = 5
PROBABILITY_TOLERANCE = 1e-9
RESULTS_ENV_VAR = "C1_E2E_RESULTS"

PINNED_CHECKPOINTS = {
    "ann-v3b": ("ann", "v3b", "best"),
    "ann-v5": ("ann", "v5", "partial_finetune"),
    "quad-v4": ("quad", "v4", "default"),
    "penta-v1": ("penta", "v1", "default"),
}
NEWEST_LOADABLE_CHUNK_LABEL = "chunk-newest"
CHUNK_FAMILY = "chunk"
REPLAY_LABELS = [*PINNED_CHECKPOINTS, NEWEST_LOADABLE_CHUNK_LABEL]

pytestmark = pytest.mark.skipif(
    not (CAPTURE_DIR / f"{CAPTURE_STEM}.csv").is_file(), reason="texture_piezo raw capture not available",
)

_RECORDED: dict = {}


@pytest.fixture(scope="module")
def snapshot():
    return workbench.load_exported_csv_snapshot(
        CAPTURE_DIR / f"{CAPTURE_STEM}.csv", CAPTURE_DIR / f"{CAPTURE_STEM}_metadata.json")


@pytest.fixture(scope="module")
def idle_baseline(snapshot):
    labels = json.loads((CAPTURE_DIR / f"{CAPTURE_STEM}_labels.json").read_text(encoding="utf-8"))
    columns = pzt_columns_for_sensor(SENSOR_NUMBER)
    indices = [snapshot.channel_labels.index(column) for column in columns]
    chunks = []
    for segment in labels["segments"]:
        if segment["class"] == "baseline":
            mask = (snapshot.timestamps_s >= segment["start_s"]) & (snapshot.timestamps_s < segment["end_s"])
            chunks.append(snapshot.data[mask][:, indices])
    return fit_idle_baseline(np.concatenate(chunks), columns, float(snapshot.sample_rate_hz), k=InferenceConfig().idle_gate_k)


def pinned_artifacts(model_type, version, checkpoint) -> ModelArtifacts:
    """A missing expected checkpoint fails (never skips): a skip would silently drop live-path coverage."""
    artifacts = model_discovery.artifacts_for(model_type, version, checkpoint)
    if artifacts is None or not artifacts.checkpoint_path.is_file():
        pytest.fail(f"expected {model_type} {version}/{checkpoint} checkpoint is missing from the models folder")
    return artifacts


def newest_loadable_artifacts(model_type) -> ModelArtifacts:
    loadable = model_discovery.loadable(model_type)
    if not loadable:
        pytest.fail(f"no loadable {model_type} model found in the models folder")
    return max(loadable, key=lambda found: found.artifacts.bundle_id).artifacts


def artifacts_for_label(label) -> ModelArtifacts:
    if label == NEWEST_LOADABLE_CHUNK_LABEL:
        return newest_loadable_artifacts(CHUNK_FAMILY)
    return pinned_artifacts(*PINNED_CHECKPOINTS[label])


def replay_windows(snapshot, idle_baseline, runtime):
    """Every LiveWindow the replay of ``snapshot`` produces for ``runtime``'s config and channels."""
    columns = pzt_columns_for_sensor(SENSOR_NUMBER)
    indices = [snapshot.channel_labels.index(column) for column in columns]
    fs = float(snapshot.sample_rate_hz)
    config = InferenceConfig()
    processor = TouchIdStreamProcessor(
        pzt_columns=columns, window_size_s=config.window_size_s, hop_size_s=config.hop_size_s,
        span_stale_timeout_s=config.span_stale_timeout_s, idle_baseline=idle_baseline,
        onset_skip_s=config.onset_skip_s,
        input_conditioning=input_conditioning_from_record(snapshot.metadata.get(INPUT_CONDITIONING_METADATA_KEY)),
        engine_config=engine_config_for_model(runtime), required_channels=required_channels_for_model(runtime),
    )
    hop_n = max(1, round(config.hop_size_s * fs))
    ready = []
    for start in range(0, snapshot.sweep_count, hop_n):
        end = min(start + hop_n, snapshot.sweep_count)
        chunk = {c: snapshot.data[start:end, i].astype(np.float64) for c, i in zip(columns, indices)}
        timestamps = np.asarray(snapshot.timestamps_s[start:end], dtype=np.float64)
        ready += processor.push_chunk(processor.filter_raw(chunk), timestamps, fs, now_t=end / fs)
    return processor, ready


def offline_reference(snapshot, processor):
    """The whole-file engine output by engine channel name, and the timestamps it covers."""
    columns = pzt_columns_for_sensor(SENSOR_NUMBER)
    indices = [snapshot.channel_labels.index(column) for column in columns]
    counts = {c: snapshot.data[:, i].astype(np.float64) for c, i in zip(columns, indices)}
    pipeline = PiezoEnginePipeline(
        columns, processor.engine_config,
        input_conditioning=input_conditioning_from_record(snapshot.metadata.get(INPUT_CONDITIONING_METADATA_KEY)),
    )
    result = pipeline.process(counts, sample_rate_hz=float(snapshot.sample_rate_hz))
    derived = {"integrated": dict(result.integrated), "shear_jerk_lr": result.shear_jerk_lr,
               "shear_jerk_tb": result.shear_jerk_tb, "normal_jerk": result.normal_jerk}
    if processor.engine_config.compute_force:
        derived.update(shear_force_lr=result.shear_force_lr, shear_force_tb=result.shear_force_tb,
                       normal_force=result.normal_force)
    channels = named_engine_channels(columns, result.raw, derived, processor.required_channels)
    return channels, np.asarray(snapshot.timestamps_s, dtype=np.float64)[result.dropped_leading.total:]


def assert_live_equals_offline(ready, channels, trimmed_ts):
    assert ready
    for window in ready:
        start = int(np.searchsorted(trimmed_ts, window.window_ts[0]))
        assert trimmed_ts[start] == window.window_ts[0]
        for name, values in window.window.channels.items():
            reference = channels[name][start:start + window.window.n_samples]
            assert values.dtype == reference.dtype == np.float64
            np.testing.assert_array_equal(values, reference)


def check_family(label, snapshot, idle_baseline, artifacts, class_names):
    runtime = adapter.load_runtime(artifacts.model_type, adapter.runtime_artifacts(artifacts), class_names)
    processor, ready = replay_windows(snapshot, idle_baseline, runtime)
    assert ready, "the replay produced no windows"
    channels, trimmed_ts = offline_reference(snapshot, processor)
    assert_live_equals_offline(ready, channels, trimmed_ts)

    direct = adapter.load_runtime(artifacts.model_type, adapter.runtime_artifacts(artifacts), class_names)
    predictions = []
    for index in range(0, len(ready), SPOT_CHECK_STRIDE):
        window = ready[index].window
        probabilities = runtime.predict_proba(window, class_names)
        reference = direct.predict_proba(window, class_names)
        assert list(probabilities) == list(class_names)
        assert sum(probabilities.values()) == pytest.approx(1.0, abs=1e-5)
        assert max(abs(probabilities[name] - reference[name]) for name in class_names) <= PROBABILITY_TOLERANCE
        predictions.append((float(ready[index].window_ts[0]), max(probabilities, key=probabilities.get),
                            round(max(probabilities.values()), 4)))
    _RECORDED[label] = {"n_windows": len(ready), "required_channels": list(runtime.required_channels),
                        "first_predictions": predictions[:RECORDED_PREDICTIONS]}
    dump_path = os.environ.get(RESULTS_ENV_VAR)
    if dump_path:
        Path(dump_path).write_text(json.dumps(_RECORDED, indent=1), encoding="utf-8")


@pytest.mark.parametrize("label", REPLAY_LABELS)
def test_real_checkpoint_replay(label, snapshot, idle_baseline):
    artifacts = artifacts_for_label(label)
    check_family(label, snapshot, idle_baseline, artifacts, artifacts.class_names)


def test_texture_classifier_wraps_the_registry_runtime_for_a_real_checkpoint(snapshot, idle_baseline):
    artifacts = pinned_artifacts(*PINNED_CHECKPOINTS["penta-v1"])
    config = InferenceConfig(model_type="penta")
    model_discovery.refresh()
    set_model_version(config, artifacts.version, artifacts.checkpoint)
    classifier = TextureClassifier(config)
    _processor, ready = replay_windows(snapshot, idle_baseline, classifier)
    probabilities = classifier.predict_proba(ready[len(ready) // 2].window)
    assert list(probabilities) == list(classifier.class_names) == list(artifacts.class_names)
    assert classifier.engine_config is None and not classifier.expected_ingest_blip_filter
