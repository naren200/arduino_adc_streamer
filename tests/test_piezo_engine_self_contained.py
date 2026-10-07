"""core.piezo_engine must import nothing outside itself except numpy/numba/stdlib."""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_DIR = REPO_ROOT / "core" / "piezo_engine"
FORBIDDEN_PREFIXES = ("PyQt6", "constants", "data_processing", "gui", "inference", "config", "pyqtgraph")

_PROBE = """
import importlib, json, pkgutil, sys
import core.piezo_engine as pkg
for info in pkgutil.iter_modules(pkg.__path__):
    importlib.import_module("core.piezo_engine." + info.name)
print(json.dumps(sorted({name.split(".")[0] for name in sys.modules})))
"""


def test_engine_imports_pull_in_no_app_modules():
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    top_level = set(json.loads(result.stdout.strip().splitlines()[-1]))
    assert not top_level.intersection(FORBIDDEN_PREFIXES)


def test_engine_sources_have_no_app_imports():
    offenders = []
    for path in ENGINE_DIR.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("from ", "import ")) and any(
                stripped.split()[1].startswith(prefix) for prefix in FORBIDDEN_PREFIXES
            ):
                offenders.append(f"{path.name}: {stripped}")
    assert not offenders
