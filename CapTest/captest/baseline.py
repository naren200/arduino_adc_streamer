"""Board baseline: C_total of every input measured with no sensor connected.

The routing of the PCB (traces, back-plane segments, the MXO path to the op-amp) adds a few to
about 20 pF to every reading, and by different amounts per ADC and channel. A baseline is a
capacitance run with all sensors unplugged; with "Subtract board baseline" on, each result also
reports C_net = C_total - baseline of the same input.

The subtraction is plain: it assumes the board capacitance and the sensor add. It is only made
between values obtained the same way (c_total_method); a baseline from the Vmid-between-samples
method is never subtracted from a one-connection result.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .settings import Channel, Settings, settings_dir

BASELINE_FORMAT = "captest-baseline-1"
# Settings that change what C_total means for the Vmid-between-samples method; a baseline taken
# with other values is still applied but reported.
COMPARED_SETTINGS = ("method", "gap_ns", "gap_long_ns", "vmid_frames", "fit_model", "discard_k")


class BaselineError(ValueError):
    """A baseline file that cannot be used."""


@dataclass
class BaselineEntry:
    c_total_pf: float
    c_total_sd_pf: float = float("nan")      # sd over the bursts of the run
    c_share_pf: float = float("nan")
    method: str = ""                         # c_total_method of the run ("" = not recorded)
    bursts: int = 0
    flags: str = ""


@dataclass
class Baseline:
    board_id: str
    entries: Dict[Channel, BaselineEntry] = field(default_factory=dict)
    created: str = ""
    source: str = ""
    settings: Dict[str, object] = field(default_factory=dict)

    def entry(self, ref: Channel) -> Optional[BaselineEntry]:
        return self.entries.get(tuple(ref))

    def methods(self) -> List[str]:
        return sorted({e.method for e in self.entries.values() if e.method})

    def mismatches(self, settings: Settings) -> List[str]:
        """Settings that differ from the ones the baseline was measured with."""
        notes = []
        for name in COMPARED_SETTINGS:
            if name in self.settings and self.settings[name] != getattr(settings, name, None):
                notes.append("%s was %r, is %r" % (name, self.settings[name], getattr(settings, name, None)))
        return notes

    def summary(self) -> str:
        methods = ", ".join(self.methods()) or "method not recorded"
        flagged = sum(1 for e in self.entries.values() if e.flags)
        text = "%d inputs, %s%s (%s)" % (
            len(self.entries), self.created[:10] or "undated", ", %d flagged" % flagged if flagged else "",
            methods)
        return text

    # ------------------------------------------------------------------ file
    def to_json(self) -> dict:
        return {
            "format": BASELINE_FORMAT, "board": self.board_id, "created": self.created,
            "source": self.source, "settings": self.settings,
            "entries": {":".join(str(int(x)) for x in ref): {
                "c_total_pf": _num(e.c_total_pf), "c_total_sd_pf": _num(e.c_total_sd_pf),
                "c_share_pf": _num(e.c_share_pf), "method": e.method, "bursts": e.bursts,
                "flags": e.flags} for ref, e in sorted(self.entries.items())},
        }

    @classmethod
    def from_json(cls, data: dict) -> "Baseline":
        if data.get("format") != BASELINE_FORMAT:
            raise BaselineError("not a captest baseline file")
        entries: Dict[Channel, BaselineEntry] = {}
        for name, item in data.get("entries", {}).items():
            try:
                ref = tuple(int(x) for x in name.split(":"))
                entries[ref] = BaselineEntry(
                    _float(item["c_total_pf"]), _float(item.get("c_total_sd_pf")),
                    _float(item.get("c_share_pf")), str(item.get("method", "")),
                    int(item.get("bursts", 0)), str(item.get("flags", "")))
            except (KeyError, ValueError, TypeError) as exc:
                raise BaselineError("bad baseline entry %r: %s" % (name, exc)) from exc
        return cls(str(data.get("board", "")), entries, str(data.get("created", "")),
                   str(data.get("source", "")), dict(data.get("settings", {})))


def _num(value: float):
    return None if value is None or not math.isfinite(value) else float(value)


def _float(value) -> float:
    return float("nan") if value is None else float(value)


def baseline_path(board_id: str) -> Path:
    return settings_dir() / ("%s_baseline.json" % board_id)


def _setting_values(settings: Settings) -> Dict[str, object]:
    return {name: getattr(settings, name) for name in COMPARED_SETTINGS if hasattr(settings, name)}


def from_results(results: Sequence, settings: Settings, source: str = "") -> Baseline:
    """Baseline from the results of a run with no sensors connected. Failed channels (no usable
    burst) are left out; flagged ones are kept and marked."""
    entries: Dict[Channel, BaselineEntry] = {}
    for result in results:
        if result.failed:
            continue
        mean, std = result.aggregate.mean, result.aggregate.std
        total = mean["c_total"] * 1e12
        if not math.isfinite(total):
            continue
        entries[tuple(result.ref)] = BaselineEntry(
            total, std["c_total"] * 1e12, mean["c_share"] * 1e12, result.c_total_method,
            result.aggregate.count, " ".join(result.flags))
    return Baseline(settings.board_id, entries, datetime.now().isoformat(timespec="seconds"),
                    source, _setting_values(settings))


def from_results_file(path) -> Baseline:
    """Baseline from a saved results JSON (CapTest results export) of a run with no sensors."""
    from .boards import load_driver

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "captest-results-1":
        raise BaselineError("%s is not a captest results file" % path)
    board_id = data.get("board", "TestBoard_7953")
    settings = Settings.from_dict(dict(data.get("settings", {}), board_id=board_id))
    id_names = [column[1] for column in load_driver(board_id).id_columns()]
    entries: Dict[Channel, BaselineEntry] = {}
    for row in data.get("rows", []):
        total = row.get("c_total_pf")
        if total is None or not row.get("bursts_used"):
            continue
        ref = tuple(int(row[name]) for name in id_names)
        entries[ref] = BaselineEntry(
            float(total), _float(row.get("c_total_std_pf")), _float(row.get("c_share_pf")),
            str(row.get("c_total_method") or ""), int(row.get("bursts_used") or 0),
            str(row.get("flags") or ""))
    if not entries:
        raise BaselineError("%s has no usable rows" % path)
    return Baseline(board_id, entries, datetime.now().isoformat(timespec="seconds"),
                    Path(path).name, _setting_values(settings))


def save(baseline: Baseline, path=None) -> Path:
    path = Path(path) if path else baseline_path(baseline.board_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline.to_json(), indent=2), encoding="utf-8")
    return path


def load(board_id: str, path=None) -> Optional[Baseline]:
    """The saved baseline of a board; None when there is none. A file that cannot be read, or
    that belongs to another board, raises BaselineError."""
    path = Path(path) if path else baseline_path(board_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BaselineError("cannot read %s: %s" % (path, exc)) from exc
    try:
        baseline = Baseline.from_json(json.loads(text))
    except json.JSONDecodeError as exc:
        raise BaselineError("%s is not valid JSON: %s" % (path, exc)) from exc
    if baseline.board_id != board_id:
        raise BaselineError("%s is a baseline of %s, not of %s" % (path, baseline.board_id, board_id))
    return baseline


def attach(result, baseline: Optional[Baseline]) -> None:
    """Give a ChannelResult the baseline entry of its input (None clears it)."""
    if baseline is None:
        result.baseline, result.baseline_note = None, ""
        return
    entry = baseline.entry(result.ref)
    result.baseline = entry
    result.baseline_note = "" if entry is not None else "no baseline for this input"
