"""Print what model_discovery finds in texture_piezo/models, and why.

Every model file the discovery reads, per architecture: the identity its header declares (weights version,
checkpoint tag, bundle id, code version, class names) and either OK or the reason it is not offered in TouchID's
Version/Checkpoint dropdowns; then the files that are not bundles (or are otherwise skipped) with the reason,
and the default model with where it came from.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.texture_piezo.models import model_discovery  # noqa: E402
from core.texture_piezo.application.inference_config import InferenceConfig, model_checkpoint_of, model_version_of  # noqa: E402


def print_family_summary() -> None:
    print("=== summary per family")
    for model_type in model_discovery.family_keys():
        ok_count = len(model_discovery.loadable(model_type))
        skip_count = len(model_discovery.discover(model_type)) - ok_count
        default = model_discovery.default_for_family(model_type)
        default_text = f"{default.version} / {default.checkpoint}" if default else "(none)"
        print(f"  {model_type:<6} OK={ok_count:<3} SKIP={skip_count:<3} default={default_text}")
    print()


def print_skipped_files() -> None:
    skipped = model_discovery.skipped_files()
    print(f"=== files not offered ({len(skipped)})")
    for entry in skipped:
        print(f"  [SKIP] {entry.path.name:<56} {entry.reason}")
    print()


def print_default_model() -> None:
    default = model_discovery.default_model()
    print("=== default model (configs/default_models.json)")
    if default.artifacts is None:
        print(f"  none loadable ({default.reason})")
    else:
        artifacts = default.artifacts
        origin = f"FALLBACK, {default.reason}" if default.is_fallback else "from configs/default_models.json"
        print(f"  {artifacts.label}  [{origin}]")
    print()


def main() -> None:
    print(f"scanned folders (flat): {', '.join(str(d) for d in model_discovery.MODEL_DIRS)}\n")

    for model_type in model_discovery.family_keys():
        found = model_discovery.discover(model_type)
        print(f"=== {model_type} - {len(found)} candidate(s)")
        for entry in found:
            artifacts = entry.artifacts
            status = "OK " if entry.is_loadable else "SKIP"
            print(f"  [{status}] {artifacts.label}")
            print(f"         key={artifacts.version} / {artifacts.checkpoint}  code={artifacts.model_version}  "
                  f"id={artifacts.bundle_id}  file={artifacts.checkpoint_path.name}")
            print(f"         classes={', '.join(artifacts.class_names)}")
            if not entry.is_loadable:
                print(f"         reason: {entry.error}")
        print()

    print_skipped_files()
    print_family_summary()
    print_default_model()
    config = InferenceConfig()
    print("=== defaults resolved for a fresh InferenceConfig")
    print(f"  active model_type: {config.model_type} "
          f"-> {model_version_of(config)} / {model_checkpoint_of(config)}")
    for model_type, selection in config.selections.items():
        print(f"  {model_type + ' selection':<20} {selection.version} / {selection.checkpoint}")


if __name__ == "__main__":
    main()
