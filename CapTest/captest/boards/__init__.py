"""Board registry: CapTest/boards.json -> validated entries -> board drivers.

A broken entry is kept in the list as *disabled with the reason* instead of breaking
the others (the GUI shows it greyed out). Paths: `descriptor` is relative to the
folder of boards.json, every other path to the repository root (its parent), never to
the working directory.
"""

from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .base import BoardDriver, Channel, Connection, GridGroup, Outcome  # noqa: F401

DEFAULT_REGISTRY = Path(__file__).resolve().parents[2] / "boards.json"
DEFAULT_BOARD_ID = "TestBoard_7953"

REQUIRED_ENTRY_KEYS = ("id", "name", "descriptor", "driver", "firmware", "docs")
REQUIRED_FIRMWARE_KEYS = ("test_project", "test_env", "firmware_name", "production_project",
                          "production_env", "production_mcu", "flash_scripts")
REQUIRED_DESCRIPTOR_KEYS = ("schema", "id", "name", "adc", "vmid_volts", "spi", "channels",
                            "connection_model", "defaults", "settings_fields", "discharge_check")


@dataclass
class BoardEntry:
    id: str
    name: str
    raw: dict
    registry_dir: Path
    repo_root: Path
    enabled: bool = True
    error: str = ""
    descriptor: Optional[dict] = None
    _driver: Optional[BoardDriver] = field(default=None, repr=False)

    @property
    def firmware(self) -> dict:
        return self.raw.get("firmware", {})

    @property
    def docs(self) -> Dict[str, Path]:
        return {k: self.repo_root / v for k, v in self.raw.get("docs", {}).items()}

    def project_dir(self, which: str) -> Path:
        return self.repo_root / self.firmware["%s_project" % which]

    @property
    def driver(self) -> BoardDriver:
        if not self.enabled:
            raise RuntimeError("board %s is disabled: %s" % (self.id, self.error))
        if self._driver is None:
            module = importlib.import_module(self.raw["driver"])
            self._driver = module.create_driver(self.descriptor, self)
        return self._driver


def _has_env(platformio_ini: Path, env: str) -> bool:
    try:
        text = platformio_ini.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return re.search(r"^\s*\[env:%s\]" % re.escape(env), text, re.MULTILINE) is not None


def validate_entry(entry: BoardEntry) -> None:
    """Fill entry.enabled / entry.error / entry.descriptor (never raises)."""
    raw = entry.raw
    try:
        for key in REQUIRED_ENTRY_KEYS:
            if key not in raw:
                raise ValueError("missing key %r" % key)
        firmware = raw["firmware"]
        for key in REQUIRED_FIRMWARE_KEYS:
            if key not in firmware:
                raise ValueError("firmware: missing key %r" % key)
        descriptor_path = entry.registry_dir / raw["descriptor"]
        if not descriptor_path.is_file():
            raise ValueError("descriptor %s not found" % descriptor_path)
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError("descriptor %s is not valid JSON: %s" % (descriptor_path.name, exc))
        for key in REQUIRED_DESCRIPTOR_KEYS:
            if key not in descriptor:
                raise ValueError("descriptor: missing key %r" % key)
        if descriptor["id"] != entry.id:
            raise ValueError("descriptor id %r differs from the entry id" % descriptor["id"])
        for which in ("test", "production"):
            project = entry.repo_root / firmware["%s_project" % which]
            if not project.is_dir():
                raise ValueError("%s firmware project %s not found" % (which, project))
            env = firmware["%s_env" % which]
            if not _has_env(project / "platformio.ini", env):
                raise ValueError("%s has no [env:%s]" % (project / "platformio.ini", env))
        for kind, stem in firmware["flash_scripts"].items():
            base = entry.repo_root / stem
            if not (base.with_suffix(".ps1").is_file() and base.with_suffix(".sh").is_file()):
                raise ValueError("flash script %s(.ps1/.sh) not found" % base)
        for kind, path in raw["docs"].items():
            if not (entry.repo_root / path).is_file():
                raise ValueError("doc %s (%s) not found" % (kind, path))
        try:
            module = importlib.import_module(raw["driver"])
        except Exception as exc:
            raise ValueError("driver %s does not load: %s" % (raw["driver"], exc))
        if not hasattr(module, "create_driver"):
            raise ValueError("driver %s has no create_driver()" % raw["driver"])
        entry.descriptor = descriptor
        entry.enabled, entry.error = True, ""
    except ValueError as exc:
        entry.enabled, entry.error = False, str(exc)


class Registry:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else DEFAULT_REGISTRY
        self.registry_dir = self.path.parent
        self.repo_root = self.registry_dir.parent
        self.entries: List[BoardEntry] = []
        self.load_error = ""
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            boards = data["boards"]
            if data.get("schema") != 1 or not isinstance(boards, list):
                raise ValueError("unsupported registry schema")
        except (OSError, ValueError, KeyError) as exc:
            self.load_error = "cannot read %s: %s" % (self.path, exc)
            return
        seen = set()
        for index, raw in enumerate(boards):
            board_id = str(raw.get("id", "")) if isinstance(raw, dict) else ""
            entry = BoardEntry(board_id or "entry#%d" % index,
                               str(raw.get("name", board_id)) if isinstance(raw, dict) else "",
                               raw if isinstance(raw, dict) else {}, self.registry_dir,
                               self.repo_root)
            if not isinstance(raw, dict):
                entry.enabled, entry.error = False, "entry is not an object"
            elif board_id in seen:
                entry.enabled, entry.error = False, "duplicate board id %r" % board_id
            else:
                seen.add(board_id)
                validate_entry(entry)
            self.entries.append(entry)

    @property
    def enabled(self) -> List[BoardEntry]:
        return [e for e in self.entries if e.enabled]

    def ids(self) -> List[str]:
        return [e.id for e in self.entries]

    def get(self, board_id: str) -> Optional[BoardEntry]:
        for entry in self.entries:
            if entry.id == board_id:
                return entry
        return None

    def driver(self, board_id: str) -> BoardDriver:
        entry = self.get(board_id)
        if entry is None:
            raise KeyError("unknown board %r (known: %s)" % (board_id, ", ".join(self.ids())))
        return entry.driver

    def first_enabled(self) -> Optional[BoardEntry]:
        return self.enabled[0] if self.enabled else None

    def match_firmware(self, mcu: str = "", firmware: str = "", board: str = ""):
        """Which board does an attached firmware belong to?

        Returns (entry, kind) with kind 'captest' | 'production' | 'diagnostic', or
        (None, '') when nothing matches. Disabled entries are not matched."""
        for entry in self.enabled:
            fw = entry.firmware
            if firmware and firmware == fw["firmware_name"]:
                return entry, "captest"
        for entry in self.enabled:
            fw = entry.firmware
            if mcu and mcu == fw["firmware_name"]:
                return entry, "captest"
            if mcu and mcu == fw["production_mcu"]:
                return entry, "production"
            if mcu and mcu == fw.get("diagnostic_mcu"):
                return entry, "diagnostic"
        return None, ""


_DEFAULT: Optional[Registry] = None


def get_registry(reload: bool = False) -> Registry:
    global _DEFAULT
    if _DEFAULT is None or reload:
        _DEFAULT = Registry()
    return _DEFAULT


def load_driver(board_id: Optional[str] = None) -> BoardDriver:
    registry = get_registry()
    return registry.driver(board_id or DEFAULT_BOARD_ID)
