import unittest
from types import SimpleNamespace

import numpy as np
import torch

from core.texture_piezo.models import model_discovery
from core.texture_piezo.models.architectures import ARCH_REGISTRY, _ChunkRuntime
from core.texture_piezo.models.chunk_ann import (
    N_CHUNK_FEATURES,
    ChunkPredictor,
    MultiScaleChunkANN,
    compute_chunk_features,
    parse_bundle,
)

WINDOW_LEN = 100
ENCODER_HIDDEN = 4
HEAD_HIDDEN = 8
NUM_CLASSES = 6
CONCAT_MAX_CHUNKS = 5
CLASS_NAMES = ["a", "b", "c", "d", "e", "f"]
SCALE_COMBINATIONS = [(5,), (20,), (50,), (5, 20), (10, 25), (4, 20, 50)]


def make_bundle(scales, include_concat_max_chunks=True, seed=0):
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    net = MultiScaleChunkANN(list(scales), WINDOW_LEN, N_CHUNK_FEATURES, ENCODER_HIDDEN, HEAD_HIDDEN,
                             NUM_CLASSES, 0.3, CONCAT_MAX_CHUNKS)
    # Non-trivial BatchNorm running stats, so eval-mode output depends on them.
    for module in net.modules():
        if isinstance(module, torch.nn.BatchNorm1d):
            module.running_mean.normal_()
            module.running_var.uniform_(0.5, 2.0)
    bundle = {
        "model_state": net.state_dict(), "scales": list(scales), "window_len": WINDOW_LEN,
        "feat_dim": N_CHUNK_FEATURES, "encoder_hidden": ENCODER_HIDDEN, "head_hidden": HEAD_HIDDEN,
        "dropout": 0.3, "num_classes": NUM_CLASSES,
        "scaler_mean": {str(s): rng.normal(size=N_CHUNK_FEATURES).tolist() for s in scales},
        "scaler_scale": {str(s): rng.uniform(0.5, 2.0, N_CHUNK_FEATURES).tolist() for s in scales},
    }
    if include_concat_max_chunks:
        bundle["concat_max_chunks"] = CONCAT_MAX_CHUNKS
    return net, bundle


def make_window(seed=1):
    return np.random.default_rng(seed).normal(size=(WINDOW_LEN, 8)).astype(np.float32)


class ChunkFeatureTests(unittest.TestCase):
    def test_shape_is_n_chunks_by_28(self):
        self.assertEqual(compute_chunk_features(make_window(), 20).shape, (5, N_CHUNK_FEATURES))

    def test_known_values_for_one_chunk(self):
        window = np.zeros((4, 8), dtype=np.float32)
        window[:, 0] = [1, 2, 3, 4]            # PZT B: mean 2.5, var 1.25, jerk all +1
        window[:, 2] = [4, 3, 2, 1]            # PZT C: jerk all -1, abs-valued to +1
        window[:, 5] = 3.0                      # shear_jerk_lr
        window[:, 6] = 4.0                      # shear_jerk_tb -> net shear 5
        feats = compute_chunk_features(window, 4)[0]
        self.assertAlmostEqual(float(feats[0]), 2.5)           # B mean
        self.assertAlmostEqual(float(feats[5]), 1.25)          # B var
        self.assertAlmostEqual(float(feats[10]), 1.0)          # B jerk mean
        self.assertAlmostEqual(float(feats[12]), 1.0)          # C jerk mean, abs-valued
        self.assertAlmostEqual(float(feats[20]), 3.0)          # shear_jerk_lr mean
        self.assertAlmostEqual(float(feats[24]), 5.0)          # net shear mean

    def test_rejects_scale_that_does_not_divide_window(self):
        with self.assertRaises(ValueError):
            compute_chunk_features(make_window(), 30)

    def test_rejects_wrong_column_count(self):
        with self.assertRaises(ValueError):
            compute_chunk_features(np.zeros((100, 13), dtype=np.float32), 20)


class ChunkPredictorTests(unittest.TestCase):
    def test_any_scale_combination_matches_a_direct_forward_pass(self):
        for scales in SCALE_COMBINATIONS:
            with self.subTest(scales=scales):
                net, payload = make_bundle(scales)
                bundle = parse_bundle(payload, NUM_CLASSES)
                window = make_window()
                tensors = []
                for scale in scales:
                    feats = compute_chunk_features(window, scale)
                    scaled = (feats - np.asarray(payload["scaler_mean"][str(scale)], dtype=np.float32)) \
                        / np.asarray(payload["scaler_scale"][str(scale)], dtype=np.float32)
                    tensors.append(torch.from_numpy(scaled.astype(np.float32)[None]))
                net.eval()
                with torch.no_grad():
                    expected = torch.softmax(net(*tensors), dim=1).numpy()[0]
                probs = ChunkPredictor(bundle).predict_proba(window)
                np.testing.assert_allclose(probs, expected, atol=1e-6)
                self.assertAlmostEqual(float(probs.sum()), 1.0, places=5)

    def test_wrong_window_length_is_rejected(self):
        _, payload = make_bundle((20,))
        predictor = ChunkPredictor(parse_bundle(payload, NUM_CLASSES))
        with self.assertRaises(ValueError):
            predictor.predict_proba(np.zeros((WINDOW_LEN + 1, 8), dtype=np.float32))


class BundleValidationTests(unittest.TestCase):
    def test_concat_max_chunks_is_recovered_when_not_recorded(self):
        for scales in [(5,), (20,), (5, 20), (20, 50)]:
            with self.subTest(scales=scales):
                _, payload = make_bundle(scales, include_concat_max_chunks=False)
                recovered = parse_bundle(payload, NUM_CLASSES).concat_max_chunks
                # Any limit that reproduces the same concat/pool split is equivalent.
                net = MultiScaleChunkANN(list(scales), WINDOW_LEN, N_CHUNK_FEATURES, ENCODER_HIDDEN,
                                         HEAD_HIDDEN, NUM_CLASSES, 0.3, recovered)
                net.load_state_dict(payload["model_state"])

    def test_missing_scaler_for_a_scale_is_rejected(self):
        _, payload = make_bundle((5, 20))
        del payload["scaler_mean"]["20"]
        with self.assertRaisesRegex(ValueError, "no scaler for scale 20"):
            parse_bundle(payload, NUM_CLASSES)

    def test_scale_that_does_not_divide_window_is_rejected(self):
        _, payload = make_bundle((20,))
        payload["scales"] = [7]
        with self.assertRaisesRegex(ValueError, "divisors"):
            parse_bundle(payload, NUM_CLASSES)

    def test_class_count_mismatch_is_rejected(self):
        _, payload = make_bundle((20,))
        with self.assertRaisesRegex(ValueError, "classes"):
            parse_bundle(payload, NUM_CLASSES + 1)

    def test_non_bundle_payload_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not a chunk-ANN bundle"):
            parse_bundle({"model_state": {}}, NUM_CLASSES)


class FakeTorchLoader:
    def __init__(self, payload):
        self.payload = payload

    def load(self, path):
        return self.payload


class ChunkRuntimeTests(unittest.TestCase):
    def test_runtime_resamples_to_bundle_window_len_and_returns_named_probs(self):
        _, payload = make_bundle((5, 20))
        config = SimpleNamespace(chunk_model_path="unused.pt", class_names=CLASS_NAMES)
        runtime = _ChunkRuntime(config, torch_loader=FakeTorchLoader(payload))
        live_window = make_window()[:73]  # live windows are not window_len samples long
        probs = runtime.predict_proba(None, live_window, CLASS_NAMES)
        self.assertEqual(list(probs), CLASS_NAMES)
        self.assertAlmostEqual(sum(probs.values()), 1.0, places=5)

    def test_runtime_requires_window_channels(self):
        _, payload = make_bundle((20,))
        config = SimpleNamespace(chunk_model_path="unused.pt", class_names=CLASS_NAMES)
        runtime = _ChunkRuntime(config, torch_loader=FakeTorchLoader(payload))
        with self.assertRaises(ValueError):
            runtime.predict_proba(None, None, CLASS_NAMES)


class ChunkRegistrationTests(unittest.TestCase):
    def test_chunk_is_a_registered_architecture(self):
        self.assertIn("chunk", ARCH_REGISTRY)
        self.assertIn("chunk", model_discovery.ARCH_STEM_PREFIXES)
        self.assertEqual(model_discovery.ARCH_SIDECAR_ROLES["chunk"], ())

    def test_scale_tagged_filename_parses_to_version_and_tag(self):
        match = model_discovery._checkpoint_regex("chunk").match("texture_chunkann_v4_scales20_25")
        self.assertEqual((match.group(1), match.group(2)), ("v4", "scales20_25"))


if __name__ == "__main__":
    unittest.main()
