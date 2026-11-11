"""PZT package groups resolved from the sensor library (board-neutral input, 7953 routes).

A group is one PZT package: five sensor positions (B, L, C, R, T) wired to five inputs of one
physical ADC. The mapping is read from the board-matching entry of
sensors_library/sensor_configurations.json at runtime; nothing is hard-coded here. The
file's global ``selected_name`` is ignored: the entry is chosen by ``board_profile``.

Group identity is ``A<array>_<PZT name>`` (e.g. ``A1_PZT1``); a bare PZT name is ambiguous
because both arrays use the same package names.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

Channel = Tuple[int, ...]

VMID_INPUT = 15                       # reserved for Vmid on every 7953 mux
DEFAULT_POSITIONS = ("B", "L", "C", "R", "T")
SENSOR_COUNT_PER_GROUP = 5


class GroupMappingError(ValueError):
    pass


def default_library_path() -> Path:
    return Path(__file__).resolve().parents[2] / "sensors_library" / "sensor_configurations.json"


@dataclass(frozen=True)
class SensorRoute:
    position: str                     # B, L, C, R, T
    adc: int
    input: int

    @property
    def ref(self) -> Channel:
        return (self.adc, self.input)


@dataclass(frozen=True)
class PztGroup:
    array: int
    pzt: str
    adc: int
    mux: int
    routes: Tuple[SensorRoute, ...]

    @property
    def group_id(self) -> str:
        return "A%d_%s" % (self.array, self.pzt)

    @property
    def refs(self) -> Tuple[Channel, ...]:
        return tuple(r.ref for r in self.routes)

    def to_json(self) -> dict:
        return {"group_id": self.group_id, "array": self.array, "pzt": self.pzt,
                "adc": self.adc, "mux": self.mux,
                "routes": [{"position": r.position, "adc": r.adc, "input": r.input}
                           for r in self.routes]}


@dataclass
class GroupMap:
    board_profile: str
    library_name: str
    library_hash: str                 # SHA-256 of the library file the entry came from
    layout_hash: str                  # SHA-256 of the canonical entry (the part used here)
    positions: Tuple[str, ...]
    reverse_polarity: bool
    groups: Dict[str, PztGroup] = field(default_factory=dict)

    def ordered(self) -> List[PztGroup]:
        return sorted(self.groups.values(), key=lambda g: (g.array, g.pzt))

    def get(self, group_id: str) -> PztGroup:
        try:
            return self.groups[group_id]
        except KeyError:
            raise GroupMappingError("unknown group %r (known: %s)"
                                    % (group_id, ", ".join(sorted(self.groups)))) from None

    def arrays(self) -> List[int]:
        return sorted({g.array for g in self.groups.values()})

    def group_of(self, ref: Channel) -> Optional[PztGroup]:
        for g in self.groups.values():
            if tuple(ref) in g.refs:
                return g
        return None

    def all_refs(self) -> List[Channel]:
        return [r for g in self.ordered() for r in g.refs]

    def identity(self) -> dict:
        return {"board_profile": self.board_profile, "library_name": self.library_name,
                "library_hash": self.library_hash, "layout_hash": self.layout_hash,
                "positions": list(self.positions), "reverse_polarity": self.reverse_polarity}


def _canonical_hash(entry: dict) -> str:
    text = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve(board_profile: str, library_path=None, *, library_data: Optional[dict] = None,
            layout_name: Optional[str] = None) -> GroupMap:
    """Resolve and validate the PZT groups of ``board_profile``.

    ``library_data`` (a parsed library) may be given instead of reading the file, e.g. by
    tests or to use a frozen copy. ``layout_name`` selects an entry by name when more than
    one entry shares the board profile.
    """
    path = Path(library_path) if library_path is not None else default_library_path()
    if library_data is None:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise GroupMappingError("cannot read sensor library %s: %s" % (path, exc)) from exc
        try:
            library_data = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise GroupMappingError("sensor library %s is not valid JSON: %s" % (path, exc)) from exc
        library_hash = hashlib.sha256(raw).hexdigest()
    else:
        library_hash = _canonical_hash(library_data)

    entries = [c for c in library_data.get("configurations", [])
               if isinstance(c, dict) and c.get("board_profile") == board_profile]
    if layout_name is not None:
        entries = [c for c in entries if c.get("name") == layout_name]
    if not entries:
        raise GroupMappingError("no sensor-library entry for board profile %r%s"
                                % (board_profile, "" if layout_name is None
                                   else " and layout %r" % layout_name))
    if len(entries) > 1:
        raise GroupMappingError("board profile %r has %d library entries (%s): choose one by name"
                                % (board_profile, len(entries),
                                   ", ".join(str(e.get("name")) for e in entries)))
    return _from_entry(entries[0], board_profile, library_hash)


def _from_entry(entry: dict, board_profile: str, library_hash: str) -> GroupMap:
    positions = tuple(entry.get("channel_sensor_map") or ())
    if not positions or len(set(positions)) != len(positions):
        raise GroupMappingError("channel_sensor_map must list distinct sensor positions")
    per_sensor = int((entry.get("channel_layout") or {}).get("channels_per_sensor", len(positions)))
    if per_sensor != len(positions):
        raise GroupMappingError("channels_per_sensor (%d) does not match the %d positions"
                                % (per_sensor, len(positions)))
    arrays = entry.get("arrays")
    mux_mapping = entry.get("mux_mapping")
    if not isinstance(arrays, dict) or not arrays:
        raise GroupMappingError("library entry has no 'arrays'")
    if not isinstance(mux_mapping, dict) or not mux_mapping:
        raise GroupMappingError("library entry has no 'mux_mapping'")
    cells = (entry.get("array_layout") or {}).get("cells") or []
    layout_names = [n for row in cells for n in row if n]
    for name in layout_names:
        if name not in mux_mapping:
            raise GroupMappingError("array_layout names %s which has no mux_mapping" % name)

    used: Dict[Channel, str] = {}
    groups: Dict[str, PztGroup] = {}
    for array_key in sorted(arrays, key=lambda k: int(k)):
        array = int(array_key)
        lanes = list((arrays[array_key] or {}).get("adc_lanes") or [])
        if not lanes or any(not isinstance(a, int) or a < 1 for a in lanes):
            raise GroupMappingError("array %d has invalid adc_lanes %r" % (array, lanes))
        for pzt, mapping in mux_mapping.items():
            mux = int(mapping.get("mux", 0))
            channels = list(mapping.get("channels") or [])
            if not 1 <= mux <= len(lanes):
                raise GroupMappingError("%s: mux %d is not a lane of array %d" % (pzt, mux, array))
            if len(channels) != len(positions):
                raise GroupMappingError("%s: %d inputs for %d sensor positions"
                                        % (pzt, len(channels), len(positions)))
            if len(set(channels)) != len(channels):
                raise GroupMappingError("%s: duplicate inputs %r" % (pzt, channels))
            for c in channels:
                if not isinstance(c, int) or not 0 <= c <= 15:
                    raise GroupMappingError("%s: input %r is outside 0..15" % (pzt, c))
                if c == VMID_INPUT:
                    raise GroupMappingError("%s: input %d is reserved for Vmid" % (pzt, VMID_INPUT))
            adc = int(lanes[mux - 1])
            routes = tuple(SensorRoute(p, adc, int(c)) for p, c in zip(positions, channels))
            for r in routes:
                if r.ref in used:
                    raise GroupMappingError("%s of array %d overlaps %s on ADC%d input %d"
                                            % (pzt, array, used[r.ref], r.adc, r.input))
                used[r.ref] = "A%d_%s" % (array, pzt)
            group = PztGroup(array, str(pzt), adc, mux, routes)
            groups[group.group_id] = group
    return GroupMap(board_profile=board_profile, library_name=str(entry.get("name", "")),
                    library_hash=library_hash, layout_hash=_canonical_hash(entry),
                    positions=positions, reverse_polarity=bool(entry.get("reverse_polarity", False)),
                    groups=groups)
