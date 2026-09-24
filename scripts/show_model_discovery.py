"""Print what model_discovery finds in texture_piezo/models, and why.

Every checkpoint on disk, per architecture: the version and checkpoint tag it
parses as, the sidecars resolved for it, and either OK or the reason it is not
offered in TouchID's Version/Checkpoint dropdowns.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inference import model_discovery  # noqa: E402
from inference.config import InferenceConfig, model_checkpoint_of, model_version_of  # noqa: E402


def _name(path) -> str:
    return path.name if path else "-"


def main() -> None:
    print(f"checkpoint dir: {model_discovery.CHECKPOINT_DIR}")
    print(f"artifact dirs:  {', '.join(str(d) for d in model_discovery.ARTIFACT_DIRS)}\n")

    for model_type, prefixes in model_discovery.ARCH_STEM_PREFIXES.items():
        found = model_discovery.discover(model_type)
        print(f"=== {model_type}  (stem prefixes: {', '.join(prefixes)}) — {len(found)} candidate(s)")
        for entry in found:
            artifacts = entry.artifacts
            status = "OK " if entry.is_loadable else "SKIP"
            print(f"  [{status}] {artifacts.version:>4} / {artifacts.checkpoint:<26} {artifacts.checkpoint_path.name}")
            print(f"         scaler={_name(artifacts.scaler_path)}  "
                  f"raw_norm={_name(artifacts.raw_norm_stats_path)}  "
                  f"features={_name(artifacts.feature_names_path)}")
            if not entry.is_loadable:
                print(f"         reason: {entry.error}")
        print()

    config = InferenceConfig()
    print("=== defaults resolved for a fresh InferenceConfig")
    print(f"  active model_type: {config.model_type} "
          f"-> {model_version_of(config)} / {model_checkpoint_of(config)}")
    for model_type in model_discovery.ARCH_STEM_PREFIXES:
        path = getattr(config, f"{model_type}_model_path")
        print(f"  {model_type + '_model_path':<20} {Path(path).name if path else '(none discovered)'}")
    for attr in ("scaler_path", "raw_norm_stats_path", "feature_names_path"):
        value = getattr(config, attr)
        print(f"  {attr:<20} {Path(value).name if value else '(none discovered)'}")


if __name__ == "__main__":
    main()
