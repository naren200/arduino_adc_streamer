"""core/ (and the GUI panel module) must not import the training repository at import time.

Checked in a fresh interpreter, BY PATH: texture_piezo's modules have generic names
(`data`, `model`), so a module-name check would prove nothing. texture_piezo is reached only
lazily, on the first model load, through inference/texture_piezo_adapter.py.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent

_PROBE = r"""
import importlib, json, pkgutil, sys
from pathlib import Path

sys.path.insert(0, {tests_dir!r})
import core.texture_piezo as pkg
for info in pkgutil.walk_packages(pkg.__path__, "core.texture_piezo."):
    importlib.import_module(info.name)
import core.piezo_engine as engine_pkg
for info in pkgutil.iter_modules(engine_pkg.__path__):
    importlib.import_module("core.piezo_engine." + info.name)
import gui.inference_panel

training_root = Path.home().joinpath("Documents", "Github", "texture_piezo").resolve()
loaded = [m.__file__ for m in list(sys.modules.values()) if getattr(m, "__file__", None)]
offenders = [f for f in loaded if training_root in Path(f).resolve().parents]
on_path = [p for p in sys.path if training_root in Path(p or ".").resolve().parents or Path(p or ".").resolve() == training_root]
print(json.dumps({{"offenders": offenders, "on_path": on_path}}))
"""

def _run_probe() -> dict:
    import json
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    code = _PROBE.format(tests_dir=str(TESTS_DIR))
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_importing_core_and_the_gui_panel_loads_nothing_from_the_training_repo():
    report = _run_probe()
    assert report["offenders"] == [] and report["on_path"] == []


def test_no_core_source_imports_the_training_repo_paths():
    offenders = []
    for path in (REPO_ROOT / "core").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "TEXTURE_PIEZO_SRC" in text or "put_texture_piezo_src_on_path" in text:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []
