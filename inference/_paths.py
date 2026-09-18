import sys
from pathlib import Path

# texture_piezo lives as a sibling checkout outside this repo so edits there
# (weights, feature code) are picked up live without vendoring/duplicating files.
TEXTURE_PIEZO_ROOT = Path.home() / "Documents" / "Github" / "texture_piezo"
TEXTURE_PIEZO_SRC = TEXTURE_PIEZO_ROOT / "src"
TEXTURE_PIEZO_MODELS = TEXTURE_PIEZO_ROOT / "models"

if str(TEXTURE_PIEZO_SRC) not in sys.path:
    sys.path.insert(0, str(TEXTURE_PIEZO_SRC))
