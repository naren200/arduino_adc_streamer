import os
from pathlib import Path

# texture_piezo lives as a sibling checkout outside this repo. These constants
# are plain locations: ROOT/MODELS say where trained checkpoints and
# training-side artifacts are found on disk. Importing this module has no
# sys.path side effect; only inference/texture_piezo_adapter.py makes
# texture_piezo importable.
TEXTURE_PIEZO_ROOT_ENV_VAR = "TEXTURE_PIEZO_ROOT"
_DEFAULT_TEXTURE_PIEZO_ROOT = Path.home() / "Documents" / "Github" / "texture_piezo"

TEXTURE_PIEZO_ROOT = Path(os.environ.get(TEXTURE_PIEZO_ROOT_ENV_VAR) or _DEFAULT_TEXTURE_PIEZO_ROOT)
TEXTURE_PIEZO_MODELS = TEXTURE_PIEZO_ROOT / "models"
