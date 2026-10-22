"""Small SPICE-netlist utilities: replace .save lines, discover nodes, read the .tran statement."""
from __future__ import annotations

import re

_TWO_NODE = set("RCLVIBD")
_FOUR_NODE = set("EGSM")


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("+") and lines:
            lines[-1] += " " + raw[1:].strip()
        else:
            lines.append(raw.rstrip())
    return lines


def strip_save(text: str) -> str:
    """Remove every .save line (and its + continuations)."""
    out: list[str] = []
    skipping = False
    for raw in text.splitlines():
        low = raw.strip().lower()
        if low.startswith(".save"):
            skipping = True
            continue
        if skipping and raw.startswith("+"):
            continue
        skipping = False
        out.append(raw)
    return "\n".join(out) + "\n"


def set_save(text: str, nodes: list[str]) -> str:
    """Replace all .save lines by a single .save of V(node) for the given nodes (placed before .end)."""
    lines = strip_save(text).splitlines()
    line = (".save " + " ".join(f"V({n})" for n in nodes)) if nodes else ""
    for i in range(len(lines) - 1, -1, -1):
        low = lines[i].strip().lower()
        if low.startswith(".end") and not low.startswith(".ends"):
            if line:
                lines.insert(i, line)
            break
    else:
        if line:
            lines.append(line)
    return "\n".join(lines) + "\n"


def discover_nodes(text: str) -> list[str]:
    """Top-level node names used by the netlist (subcircuit internals excluded), ground excluded."""
    nodes: set[str] = set()
    depth = 0
    for line in _logical_lines(text):
        s = line.strip()
        if not s or s.startswith("*"):
            continue
        low = s.lower()
        if low.startswith(".subckt"):
            depth += 1
            continue
        if low.startswith(".ends"):
            depth = max(0, depth - 1)
            continue
        if depth or s.startswith("."):
            continue
        tokens = [t for t in s.split() if "=" not in t]
        kind = tokens[0][0].upper()
        if kind in _TWO_NODE:
            found = tokens[1:3]
        elif kind in _FOUR_NODE:
            found = tokens[1:5]
        elif kind in "FHW":
            found = tokens[1:3]
        elif kind in "QJ":
            found = tokens[1:4]
        elif kind == "X":
            found = tokens[1:-1]
        else:
            found = []
        for n in found:
            n = n.lower()
            if n not in ("0", "gnd") and not re.fullmatch(r"[-+]?[\d.]+", n):
                nodes.add(n)
    return sorted(nodes)


_SPICE_NUM = re.compile(r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)(meg|[fpnuµmkgt])?[a-z]*$", re.IGNORECASE)
_SPICE_SUFFIX = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "meg": 1e6, "g": 1e9, "t": 1e12}


def spice_number(token: str) -> float:
    """SPICE number with engineering suffix (m = milli, meg = mega, case-insensitive). Raises ValueError."""
    m = _SPICE_NUM.match(token.strip())
    if not m:
        raise ValueError(token)
    value = float(m.group(1))
    if m.group(2):
        value *= _SPICE_SUFFIX[m.group(2).lower()]
    return value


def parse_tran(text: str) -> tuple[float | None, float | None]:
    """(tstop, tmax) of the first .tran line; None when absent."""
    for line in _logical_lines(text):
        s = line.strip()
        if s.lower().startswith(".tran"):
            vals = []
            for tok in s.split()[1:]:
                try:
                    vals.append(spice_number(tok))
                except ValueError:
                    break
            if len(vals) >= 2:
                tmax = vals[3] if len(vals) >= 4 and vals[3] > 0 else None
                return vals[1], tmax
    return None, None
