"""Helpers for nested configuration dictionaries addressed by dotted paths ("mux.ileak_s_off_nA")."""
from __future__ import annotations

import copy
import math
import re
from typing import Any

_MISSING = object()


def get_path(cfg: dict, path: str, default: Any = _MISSING) -> Any:
    node: Any = cfg
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif default is _MISSING:
            raise KeyError(path)
        else:
            return default
    return node


def set_path(cfg: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def apply_overrides(cfg: dict, overrides: dict[str, Any]) -> dict:
    """Return a deep copy of cfg with {dotted.path: value} overrides applied."""
    out = copy.deepcopy(cfg)
    for path, value in overrides.items():
        set_path(out, path, copy.deepcopy(value))
    return out


_SI = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
_NUM = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([fpnuµmkKMGT]|meg|MEG)?\s*$")


def parse_number(text: str) -> float:
    """Parse '1e10', '470k', '1.5n', '2meg' (case matters only for m/M). Raises ValueError."""
    m = _NUM.match(text)
    if not m:
        raise ValueError(f"not a number: {text!r}")
    value = float(m.group(1))
    suffix = m.group(2)
    if suffix:
        value *= 1e6 if suffix.lower() == "meg" else _SI[suffix]
    if not math.isfinite(value):
        raise ValueError("not finite")
    return value


def format_number(value: float | int) -> str:
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if value == 0:
        return "0"
    a = abs(value)
    if 1e-3 <= a < 1e6:
        return f"{value:.10g}"
    return f"{value:.6g}".replace("e+0", "e").replace("e+", "e").replace("e-0", "e-")   # 1.6e+07 -> 1.6e7
