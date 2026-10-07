"""Import-boundary rules between the engine, live gating and the texture_piezo repo."""

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATING_DIR = REPO_ROOT / "core" / "texture_piezo" / "gating"
ENGINE_PACKAGE = "core.piezo_engine"
THIRD_PARTY_ALLOWED = {"numpy"}


def _imported_modules(path: Path) -> list[str]:
    modules = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules.append(node.module)
    return modules


def _is_allowed(module: str) -> bool:
    top_level = module.split(".")[0]
    return (
        top_level in sys.stdlib_module_names
        or top_level in THIRD_PARTY_ALLOWED
        or module == ENGINE_PACKAGE
        or module.startswith(ENGINE_PACKAGE + ".")
    )


def test_gating_imports_only_engine_stdlib_and_numpy():
    offenders = [
        f"{path.name}: {module}"
        for path in GATING_DIR.glob("*.py")
        for module in _imported_modules(path)
        if not _is_allowed(module)
    ]
    assert not offenders


ADAPTER = REPO_ROOT / "inference" / "texture_piezo_adapter.py"
EXCLUDED_TOP_LEVEL_DIRS = {"Legacy", "tests", "token_dont_read_ai", ".venv", "venv", "node_modules", ".git"}
# Top-level modules of texture_piezo/src: importing any of them outside the adapter is a violation.
TEXTURE_PIEZO_MODULES = {
    "touchid_inference", "engine_adapter", "data", "model", "train", "evaluate", "datasets", "artifacts",
    "window_layout", "chunk_features", "chunk_bundle", "chunk_cache", "clip_windowing_utils_v1",
    "clip_windowing_utils_v2", "drag_detection_utils_v1", "partial_window_utils", "export_chunk_bundle",
}
TEXTURE_PIEZO_NAMES = (
    "touchid_inference", "clip_windowing_utils", "chunk_features", "extract_window_features",
    "put_texture_piezo_src_on_path", "TEXTURE_PIEZO_SRC",
)
SYS_PATH_MUTATORS = {"insert", "append", "extend"}


def _application_sources() -> list[Path]:
    return [
        path for path in REPO_ROOT.rglob("*.py")
        if path != ADAPTER
        and not set(path.relative_to(REPO_ROOT).parts[:-1]) & EXCLUDED_TOP_LEVEL_DIRS
        and "__pycache__" not in path.parts
    ]


def _is_sys_path_mutation(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in SYS_PATH_MUTATORS and ast.unparse(node.func.value) == "sys.path"
    )


def _texture_piezo_path_edits(path: Path) -> list[str]:
    return [
        f"{path.name}: {ast.unparse(node)}"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if _is_sys_path_mutation(node) and "texture_piezo" in ast.unparse(node).lower()
    ]


def test_aa_reaches_texture_piezo_only_through_the_adapter():
    offenders = []
    for path in _application_sources():
        offenders += [f"{path.name}: imports {module}" for module in _imported_modules(path)
                      if module.split(".")[0] in TEXTURE_PIEZO_MODULES]
        offenders += _texture_piezo_path_edits(path)
    assert not offenders


def test_the_adapter_is_the_one_module_that_edits_sys_path_for_texture_piezo():
    adapter_text = ADAPTER.read_text(encoding="utf-8")
    assert "sys.path.append" in adapter_text and "importlib.import_module" in adapter_text


def test_no_application_module_outside_the_adapter_names_a_texture_piezo_module():
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}: {name}"
        for path in _application_sources()
        for name in TEXTURE_PIEZO_NAMES
        if re.search(rf"(?<![A-Za-z0-9_]){name}(?![A-Za-z0-9_])", path.read_text(encoding="utf-8"))
    ]
    assert not offenders
