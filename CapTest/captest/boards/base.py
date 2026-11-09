"""Board driver interface: everything that differs between boards.

The application core (session, fit, results, exports, GUI) is board-neutral and talks
to a BoardDriver. A driver is a small module (`create_driver(descriptor, entry)`) plus a
descriptor JSON (CapTest/boards/<id>.json). To add a board see CapTest/README.md.

Vocabulary
  channel   one sensor channel, identified by a tuple of ints (the *ref*): (adc, channel)
            for TestBoard_7953, (array, mux, input) for TestBoard_ADC124.
  connection  one burst: the channels that are READ and the sensors it TOUCHES (all
            sensors the burst connects, also those nobody reads: they are no longer
            "fresh" afterwards and need their discharge time).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

Channel = Tuple[int, ...]


@dataclass(frozen=True)
class Connection:
    """One burst: channels read (ordered) and sensors connected (superset of reads)."""

    reads: Tuple[Channel, ...]
    touches: Tuple[Channel, ...]
    label: str = ""
    params: Tuple = ()           # board specific, hashable (e.g. (array, address))


@dataclass
class GridGroup:
    """A group of channels drawn as one grid (7953: one ADC, ADC124: one array)."""

    id: int
    title: str
    channels: List[Channel]
    grid: List[List[Optional[Channel]]]      # rows of cells; None = no channel there
    row_labels: List[str] = field(default_factory=list)
    col_labels: List[str] = field(default_factory=list)


@dataclass
class Outcome:
    """What a burst returned: the raw packet (for storage / re-analysis) and its decoding."""

    packet: bytes
    decoded: object
    boot_id: int = 0


class BoardDriver:
    """Base class; see the module docstring. Subclasses implement every method that
    raises NotImplementedError."""

    #: set by the registry loader
    entry = None
    #: Vinf mode (measured settling voltage of a held sensor, next to the calculated one)
    supports_vinf = False
    #: Force data export and the Force Calibration tab (needs continuous capture and a
    #: sensor-library layout of PZT packages). Only drivers that implement both set it.
    supports_force_calibration = False
    #: Sensor-library board profile used to resolve the PZT groups ("" = none)
    force_board_profile = ""

    def __init__(self, descriptor: dict, entry=None):
        self.descriptor = descriptor
        self.entry = entry
        self.id: str = descriptor["id"]
        self.name: str = descriptor.get("name", self.id)

    # ------------------------------------------------------------ descriptor data
    @property
    def vref(self) -> float:
        return float(self.descriptor["adc"]["vref_volts"])

    @property
    def vmid_volts(self) -> float:
        return float(self.descriptor["vmid_volts"])

    @property
    def spi_range(self) -> Tuple[int, int, int]:
        spi = self.descriptor["spi"]
        return int(spi["min_hz"]), int(spi["max_hz"]), int(spi["default_hz"])

    @property
    def settings_fields(self) -> List[str]:
        return list(self.descriptor["settings_fields"])

    @property
    def firmware_name(self) -> str:
        return self.entry.firmware["firmware_name"] if self.entry else ""

    @property
    def production_mcu(self) -> str:
        return self.entry.firmware["production_mcu"] if self.entry else ""

    @property
    def syntax(self) -> str:
        return self.descriptor["channels"]["syntax"]

    @property
    def example_spec(self) -> str:
        return self.descriptor["channels"]["example"]

    @property
    def discharge_check(self) -> dict:
        return self.descriptor["discharge_check"]

    @property
    def connection_model(self) -> str:
        return self.descriptor["connection_model"]

    @property
    def touches_text(self) -> str:
        return self.descriptor.get("touches", "")

    def id_columns(self) -> List[Tuple[str, str, str]]:
        return [tuple(c) for c in self.descriptor["channels"]["id_columns"]]

    # ------------------------------------------------------------- channel model
    def groups(self) -> List[GridGroup]:
        raise NotImplementedError

    def channels(self, group_ids: Optional[Sequence[int]] = None) -> List[Channel]:
        wanted = None if group_ids is None else set(group_ids)
        return [c for g in self.groups() if wanted is None or g.id in wanted for c in g.channels]

    def is_channel(self, ref: Channel) -> bool:
        return tuple(ref) in set(self.channels())

    def parse_spec(self, text: str) -> List[Channel]:
        raise NotImplementedError

    def format_spec(self, channels: Sequence[Channel]) -> str:
        return ",".join(self.key(c) for c in channels)

    def key(self, ref: Channel) -> str:
        return ":".join(str(int(x)) for x in ref)

    def display(self, ref: Channel) -> str:
        return self.descriptor["channels"]["display"].format(*ref)

    def cell_label(self, ref: Channel) -> str:
        return str(ref[-1])

    def identity(self, ref: Channel) -> Dict[str, int]:
        return {col[1]: int(v) for col, v in zip(self.id_columns(), ref)}

    def group_of(self, ref: Channel) -> int:
        return int(ref[0])

    def shared_node(self, ref: Channel):
        """Key of the mux-output node a channel shares with others (they share C_D)."""
        return ref[0]

    def chart_group(self, ref: Channel) -> str:
        """Label of the group a bar chart colours / separates by (7953: ADC, ADC124: mux)."""
        return str(ref[0])

    # ---------------------------------------------------------------- defaults
    def default_bias_table(self) -> Dict[str, float]:
        raise NotImplementedError

    def bias_conflicts(self) -> List[str]:
        """Human readable disagreements between the bias-resistor sources (empty = none)."""
        return []

    # ---------------------------------------------------------------- planning
    def single(self, channel: Channel) -> Connection:
        """A connection that reads exactly `channel`."""
        raise NotImplementedError

    def plan(self, channels: Sequence[Channel], settings, per_address: Optional[bool] = None
             ) -> List[Connection]:
        """Ordered connections that read `channels` (given order for the 7953; address
        order for the ADC124)."""
        raise NotImplementedError

    def auto_channels(self, group_ids: Optional[Sequence[int]] = None) -> List[Channel]:
        return self.channels(group_ids)

    # --------------------------------------------------------- device interaction
    def setup_device(self, client, settings) -> bool:
        """Push settings the firmware keeps (SPI clock). False when rejected."""
        return client.set_spi_clock(settings.spi_clock_hz) is not False

    def measure(self, client, connection: Connection, settings) -> Outcome:
        raise NotImplementedError

    def decode(self, packet: bytes):
        raise NotImplementedError

    def analyze(self, decoded, refs: Sequence[Channel], settings,
                since_ms: Dict[Channel, Optional[float]]):
        """Measurements (dict ref -> capfit.Measurement) for `refs` read by `decoded`."""
        raise NotImplementedError

    def vmid(self, client, target: int) -> dict:
        raise NotImplementedError

    def vmid_text(self, reading: dict, vref: float) -> str:
        raise NotImplementedError

    def discharge_samples_hint(self, settings, ref: Channel) -> int:
        return 0

    # --------------------------------------------------------------- simulation
    def sim_config(self, **kw):
        raise NotImplementedError

    def create_simulator(self, clock=None, bias_table=None, config=None):
        raise NotImplementedError
