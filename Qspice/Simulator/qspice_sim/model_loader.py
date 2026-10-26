"""Discover and load simulator models.

A model is a folder containing ``simulator_model.json`` (manifest: parameter forms, plot groups, nodes), a default
configuration (``config.json`` by default) and an adapter module (``simulator_adapter.py``) exposing

    build(cfg)   -> {"netlist": str, "t_stop": float, "samples": [{"t": s, "label": str}], "warnings": [str]}
    summary(cfg) -> [(label, text), ...]        optional
    field_hints(cfg) -> {path: text}            optional, placeholder text of empty nullable fields

See ../README.md for the full contract.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adc_model import AdcParams

MANIFEST_NAME = "simulator_model.json"
FIELD_TYPES = {"float", "int", "bool", "choice", "intlist"}


class ModelError(Exception):
    """The model folder is missing something or the manifest is inconsistent."""


@dataclass
class Model:
    root: Path
    manifest: dict
    defaults: dict
    adapter: Any
    problems: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.root.name

    @property
    def name(self) -> str:
        return self.manifest.get("name", self.id)

    @property
    def description(self) -> str:
        return self.manifest.get("description", "")

    @property
    def scenarios(self) -> dict[str, dict]:
        return self.defaults.get("scenarios", {})

    def default_config(self) -> dict:
        return copy.deepcopy(self.defaults)

    def build(self, cfg: dict) -> dict:
        result = self.adapter.build(copy.deepcopy(cfg))
        for key in ("netlist", "t_stop"):
            if key not in result:
                raise ModelError(f"adapter.build() result is missing '{key}'")
        result.setdefault("samples", [])
        result.setdefault("warnings", [])
        return result

    def summary(self, cfg: dict) -> list[tuple[str, str]]:
        fn = getattr(self.adapter, "summary", None)
        return list(fn(copy.deepcopy(cfg))) if fn else []

    def field_hints(self, cfg: dict) -> dict[str, str]:
        """Optional adapter.field_hints(cfg) -> {path: text}: shown as the placeholder of an empty (nullable) field."""
        fn = getattr(self.adapter, "field_hints", None)
        if not fn:
            return {}
        try:
            return dict(fn(copy.deepcopy(cfg)))
        except Exception:  # a hint is a convenience; invalid parameters are reported elsewhere
            return {}

    def node_info(self) -> dict[str, dict]:
        return {n["name"].lower(): n for n in self.manifest.get("nodes", [])}

    def plot_groups(self) -> list[dict]:
        groups = list(self.manifest.get("plot_groups", []))
        if not any(g["id"] == "other" for g in groups):
            groups.append({"id": "other", "title": "Other nodes [V]"})
        return groups

    # ----- derived nodes (computed in Python from the sample node, not QSPICE nodes) -----
    @property
    def adc_out(self) -> dict | None:
        """Manifest block ``adc_out``: the ADC input node, the ADC parameters in the configuration and the channel count."""
        return self.manifest.get("adc_out")

    @property
    def channels(self) -> list[int]:
        spec = self.adc_out
        return list(range(int(spec.get("channels", 0)))) if spec else []

    def derived_nodes(self) -> list[dict]:
        """[{name, label, group, kind, channel, sources}] in tree order."""
        spec = self.adc_out
        if not spec:
            return []
        out = []
        for k in self.channels:
            out.append({"name": f"adcout_ch{k}", "label": f"ADC-Out ch{k}", "group": spec.get("group", "adc"), "kind": "adcout",
                        "channel": k, "sources": [spec["node"]]})
        return out

    def derived_info(self) -> dict[str, dict]:
        return {d["name"]: d for d in self.derived_nodes()}

    def adc_params(self, run_cfg: dict, live_cfg: dict | None = None) -> AdcParams:
        """ADC parameters of a result: reference and VMID from the configuration the run used (they shape the circuit),
        the resolution from the live configuration (changing it only re-derives the ADC output, it needs no new run)."""
        spec = self.adc_out
        live = run_cfg if live_cfg is None else live_cfg
        return AdcParams(bits=int(_get(live, spec["bits_path"])), vref=float(_get(run_cfg, spec["vref_path"])),
                         vmid=float(_get(run_cfg, spec["vmid_path"])))

    def all_paths(self) -> list[str]:
        paths = []
        for tab in ("components", "system", "simulation"):
            for grp in self.manifest.get(tab, []):
                paths += [it["path"] for it in grp["items"]]
        paths += [t["path"] for t in self.manifest.get("tables", [])]
        return paths


def _get(cfg: dict, path: str) -> Any:
    node: Any = cfg
    for part in path.split("."):
        node = node[part]
    return node


def validate_manifest(manifest: dict, defaults: dict) -> list[str]:
    problems: list[str] = []
    for tab in ("components", "system", "simulation"):
        for grp in manifest.get(tab, []):
            for it in grp.get("items", []):
                if it.get("type", "float") not in FIELD_TYPES:
                    problems.append(f"{it.get('path')}: unknown field type {it.get('type')!r}")
                try:
                    _get(defaults, it["path"])
                except (KeyError, TypeError):
                    problems.append(f"{it['path']}: not present in the default configuration")
    for tbl in manifest.get("tables", []):
        try:
            if not isinstance(_get(defaults, tbl["path"]), list):
                problems.append(f"{tbl['path']}: table path is not a list")
        except (KeyError, TypeError):
            problems.append(f"{tbl['path']}: not present in the default configuration")
    groups = {g["id"] for g in manifest.get("plot_groups", [])} | {"other"}
    for n in manifest.get("nodes", []):
        if n.get("group", "other") not in groups:
            problems.append(f"node {n['name']}: unknown plot group {n.get('group')!r}")
    problems += _validate_adc_out(manifest, defaults, groups)
    return problems


def _validate_adc_out(manifest: dict, defaults: dict, groups: set[str]) -> list[str]:
    spec = manifest.get("adc_out")
    if spec is None:
        return []
    problems: list[str] = []
    for key in ("node", "bits_path", "vref_path", "vmid_path", "channels"):
        if key not in spec:
            problems.append(f"adc_out: '{key}' is missing")
    for key in ("bits_path", "vref_path", "vmid_path"):
        if key in spec:
            try:
                _get(defaults, spec[key])
            except (KeyError, TypeError):
                problems.append(f"adc_out.{key}: {spec[key]} is not present in the default configuration")
    try:
        bits = _get(defaults, spec.get("bits_path", ""))
        if isinstance(bits, bool) or not isinstance(bits, int) or bits < 2:
            problems.append(f"adc_out.bits_path: {spec['bits_path']} must be an integer of at least 2")
    except (KeyError, TypeError):
        pass
    if not isinstance(spec.get("channels", 1), int) or spec.get("channels", 1) < 1:
        problems.append("adc_out.channels must be a positive integer (number of sensor channels)")
    if spec.get("group", "adc") not in groups:
        problems.append(f"adc_out: unknown plot group {spec.get('group')!r}")
    if spec.get("node") and spec["node"].lower() not in {n["name"].lower() for n in manifest.get("nodes", [])}:
        problems.append(f"adc_out.node: {spec['node']} is not a node of the manifest")
    real = {n["name"].lower() for n in manifest.get("nodes", [])}
    for d in _derived_names(spec):
        if d in real:
            problems.append(f"derived node {d} has the same name as a real node")
    return problems


def _derived_names(spec: dict) -> list[str]:
    return [f"adcout_ch{k}" for k in range(int(spec.get("channels", 0)) if isinstance(spec.get("channels", 0), int) else 0)]


def _load_adapter(root: Path, filename: str):
    path = root / filename
    if not path.is_file():
        raise ModelError(f"adapter file not found: {path}")
    name = "qspice_adapter_" + re.sub(r"\W", "_", str(root.resolve()))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    if not hasattr(module, "build"):
        raise ModelError(f"{filename} has no build(cfg) function")
    return module


def load_model(root: Path | str) -> Model:
    root = Path(root)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ModelError(f"{MANIFEST_NAME} not found in {root}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ModelError(f"{manifest_path}: {exc}") from exc
    cfg_path = root / manifest.get("config", "config.json")
    if not cfg_path.is_file():
        raise ModelError(f"default configuration not found: {cfg_path}")
    defaults = json.loads(cfg_path.read_text(encoding="utf-8"))
    adapter = _load_adapter(root, manifest.get("adapter", "simulator_adapter.py"))
    return Model(root, manifest, defaults, adapter, validate_manifest(manifest, defaults))


def discover(search_roots: list[Path]) -> list[Model]:
    """Load every sub-folder of the given roots that contains a manifest. Broken models are skipped, not fatal."""
    models: list[Model] = []
    for base in search_roots:
        if not base.is_dir():
            continue
        for sub in sorted(p for p in base.iterdir() if p.is_dir() and (p / MANIFEST_NAME).is_file()):
            try:
                models.append(load_model(sub))
            except Exception as exc:  # a broken model must not take the application down
                print(f"[qspice_sim] skipping model {sub.name}: {exc}", file=sys.stderr)
    return models
