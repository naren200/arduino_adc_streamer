"""Measurement settings, R_bias table and channel selection parsing."""

from __future__ import annotations

import json
import os

import numpy as np
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import capfit

Channel = Tuple[int, ...]   # board channel id: (adc, channel) for the 7953, (array, mux, input) for the ADC124
DEFAULT_BOARD_ID = "TestBoard_7953"

BIAS_JSON = (Path(__file__).resolve().parents[2] / "Arduino_Sketches" / "TestBoard_7953" / "benchmarks"
             / "testboard_7953_bias_resistors.json")
DEFAULT_ADC_BIAS = {1: 1_000_000.0, 2: 470_000.0, 3: 470_000.0, 4: 249_000.0}
ROUTED_CHANNELS = {1: 10, 2: 15, 3: 10, 4: 15}   # number of routed sensor inputs per ADC
ADC_NUMBERS = (1, 2, 3, 4)


def routed_channels(adc: int) -> List[int]:
    return list(range(ROUTED_CHANNELS[adc]))


def all_channels(adcs: Iterable[int] = ADC_NUMBERS) -> List[Channel]:
    return [(a, c) for a in adcs for c in routed_channels(a)]


def key(adc: int, channel: int) -> str:
    return "%d:%d" % (adc, channel)


def default_bias_table() -> Dict[str, float]:
    """Per ADC:channel bias resistors: the benchmark JSON when present."""
    table: Dict[str, float] = {}
    try:
        loaded = json.loads(BIAS_JSON.read_text(encoding="utf-8"))
        table = {str(k): float(v) for k, v in loaded.items()}
    except (OSError, ValueError):
        pass
    for adc, ohms in DEFAULT_ADC_BIAS.items():
        for channel in routed_channels(adc):
            table.setdefault(key(adc, channel), ohms)
    return table


def parse_channel_spec(text: str) -> List[Channel]:
    """Parse '2:0,2:5,4:1-3' (also '1:*' for every routed channel of ADC 1)."""
    result: List[Channel] = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError("expected adc:channel, got %r" % part)
        adc_text, ch_text = (s.strip() for s in part.split(":", 1))
        adc = int(adc_text)
        if adc not in ADC_NUMBERS:
            raise ValueError("ADC must be 1..4, got %d" % adc)
        if ch_text == "*":
            channels = routed_channels(adc)
        elif "-" in ch_text:
            low, high = (int(s) for s in ch_text.split("-", 1))
            if low > high:
                raise ValueError("bad range %r" % part)
            channels = list(range(low, high + 1))
        else:
            channels = [int(ch_text)]
        for channel in channels:
            if channel not in routed_channels(adc):
                raise ValueError("ADC%d has no routed channel %d" % (adc, channel))
            if (adc, channel) not in result:
                result.append((adc, channel))
    return result


def format_channel_spec(channels: Sequence[Channel]) -> str:
    return ",".join(key(a, c) for a, c in channels)


# Settings that only exist for boards other than the 7953 are left out of the JSON of a
# 7953 session, so its saved files stay byte-identical to those of the first version.
BOARD_EXTRA_FIELDS = ("board_id", "pre_samples", "hold_clocks", "enable_delay_ns",
                      "pop_latency_ns", "baseline_samples", "per_address")


@dataclass
class Settings:
    bias: Dict[str, float] = field(default_factory=default_bias_table)
    bias_tolerance: float = 0.01          # fractional, shown next to C_total
    n_samples: Optional[int] = None       # None = auto
    c_guess_pf: float = 100.0             # used to size an automatic burst
    park_us: int = 200
    gap_ns: int = 200
    first_clock_delay_ns: int = 25
    spi_clock_hz: int = 20_000_000
    bursts: int = 1
    discharge_ms: float = 300.0
    discard_k: int = 2
    vref: float = 2.5
    v_s0: float = 0.0
    c_parasitic_pf: float = 5.0
    fit_model: str = capfit.FIT_TWO        # "1exp" or "2exp"
    # TestBoard_7953 acquisition method. "alternate": Vmid between samples (capalt, firmware
    # 1.3.0): vmid_frames Vmid frames after each sensor frame, and every second cycle keeps
    # the sensor connected with gap_long_ns, so one burst gives tau and C_share. "burst": one
    # connection held for the whole burst (capburst). Other boards ignore these three.
    method: str = "alternate"
    vmid_frames: int = 1
    gap_long_ns: int = 0                   # 0 = automatic, see long_gap_ns
    # Used by boards whose mux is switched by GPIO (TestBoard_ADC124).
    board_id: str = DEFAULT_BOARD_ID
    pre_samples: int = 8                   # frames taken before the connection (the V_D baseline)
    hold_clocks: float = 3.0               # ADC hold instant, SCLK periods after the frame start
    enable_delay_ns: float = 25.0          # mux turn-on delay after the enable edge
    pop_latency_ns: float = 100.0          # polling latency between frame end and its time stamp
    baseline_samples: int = 4              # last pre-connect samples per input averaged into V_D
    per_address: bool = True               # one burst per address serves all four muxes

    def r_bias(self, *ref: int) -> float:
        """R_bias of a channel: r_bias(adc, channel) or r_bias(array, mux, input)."""
        name = ":".join(str(int(x)) for x in ref)
        if name in self.bias:
            return float(self.bias[name])
        return float(DEFAULT_ADC_BIAS[ref[0]])

    @classmethod
    def for_board(cls, board_id: str) -> "Settings":
        """Defaults of a board (descriptor 'defaults', its bias-resistor table)."""
        from .boards import load_driver

        driver = load_driver(board_id)
        names = {f.name for f in fields(cls)}
        values = {k: v for k, v in driver.descriptor["defaults"].items() if k in names}
        return cls(board_id=board_id, bias=driver.default_bias_table(), **values)

    def nominal_period_s(self) -> float:
        # Clock time + CS-high gap + ~100 ns of software/first-clock overhead.
        return 16.0 / self.spi_clock_hz + (self.gap_ns + 100) * 1e-9

    def resolve_samples(self, adc: int, channel: int) -> int:
        if self.n_samples:
            return int(self.n_samples)
        if self.method == "alternate":
            return self.alternate_frames(adc, channel)
        return capfit.recommended_samples(
            self.r_bias(adc, channel), self.c_guess_pf * 1e-12, self.nominal_period_s())

    def long_gap_ns(self, adc: int, channel: int) -> int:
        """Gap of the stretched cycles: the setting, or (0 = automatic) 0.3 x the guessed tau,
        4-40 us. tau comes from the difference between the two connected times, so it must be a
        fair part of tau; much longer and a few long cycles take the sensor to Vmid."""
        if self.gap_long_ns:
            return int(self.gap_long_ns)
        tau_ns = self.r_bias(adc, channel) * self.c_guess_pf * 1e-12 * 1e9
        return int(round(min(max(0.3 * tau_ns, 4000.0), 40000.0), -2))

    def alternate_frames(self, adc: int, channel: int, c_share_pf: float = 10.0,
                         end_distance: float = 0.02, cap: int = 1024) -> int:
        """Command frames of an automatic capalt burst: enough cycle pairs (one short, one
        long) for the guessed C to come within `end_distance` of Vmid, twice over, and at
        least 16 pairs; never longer than the firmware's 20 ms interrupt-masked limit."""
        c_guess = self.c_guess_pf * 1e-12
        tau = self.r_bias(adc, channel) * c_guess
        share = min(c_share_pf / self.c_guess_pf, 0.9)
        t_short = self.nominal_period_s()
        t_long = t_short + (self.long_gap_ns(adc, channel) - self.gap_ns) * 1e-9
        per_pair = 2.0 * np.log(1.0 - share) - (t_short + t_long) / tau
        pairs = int(np.ceil(2.0 * np.log(end_distance / 1.25) / per_pair)) + 2
        frames = max(16, pairs) * 2 * (1 + self.vmid_frames)
        longest_us = (16.0 / self.spi_clock_hz + max(self.gap_ns, self.long_gap_ns(adc, channel)) * 1e-9) * 1e6
        cap = min(cap, int((20000 - self.park_us) / longest_us) - 12)
        return max(8, min(cap, frames))

    def analysis(self) -> capfit.AnalysisSettings:
        return capfit.AnalysisSettings(
            vref=self.vref, discard_k=self.discard_k, v_s0=self.v_s0,
            c_parasitic_pf=self.c_parasitic_pf, r_bias_tolerance=self.bias_tolerance,
            fit_model=self.fit_model if self.fit_model in capfit.FIT_MODELS else capfit.FIT_TWO)

    def to_json(self) -> str:
        data = asdict(self)
        if self.board_id == DEFAULT_BOARD_ID:
            for name in BOARD_EXTRA_FIELDS:
                data.pop(name)
        return json.dumps(data, indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        board_id = data.get("board_id", DEFAULT_BOARD_ID)
        if board_id == DEFAULT_BOARD_ID:
            settings = cls(**{k: v for k, v in data.items() if k in known and k != "bias"})
        else:
            settings = cls.for_board(board_id)
            for name, value in data.items():
                if name in known and name not in ("bias", "board_id"):
                    setattr(settings, name, value)
        if "bias" in data:
            settings.bias.update({str(k): float(v) for k, v in data["bias"].items()})
        return settings

    def save(self, path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path) -> "Settings":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ------------------------------------------------------- per-board files on disk
CAPTEST_DIR = Path(__file__).resolve().parents[1]


def settings_dir() -> Path:
    """Folder of the per-board saved settings (CAPTEST_SETTINGS_DIR overrides, for tests)."""
    override = os.environ.get("CAPTEST_SETTINGS_DIR")
    return Path(override) if override else CAPTEST_DIR / "settings"


def results_dir(board_id: str) -> Path:
    """Default output folder of a board: CapTest/Results/<board id>/ (created on demand;
    CAPTEST_RESULTS_DIR overrides the CapTest/Results part)."""
    override = os.environ.get("CAPTEST_RESULTS_DIR")
    path = (Path(override) if override else CAPTEST_DIR / "Results") / board_id
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def resolve_output(path, board_id: str) -> Path:
    """A bare file name goes to the board's results folder, anything with a folder is kept."""
    path = Path(path)
    if path.parent == Path("."):
        return results_dir(board_id) / path.name
    return path


def save_board_settings(settings: Settings) -> Path:
    folder = settings_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("%s.json" % settings.board_id)
    settings.save(path)
    return path


def load_board_settings(board_id: str) -> Optional[Settings]:
    path = settings_dir() / ("%s.json" % board_id)
    try:
        settings = Settings.load(path)
    except (OSError, ValueError, KeyError):
        return None
    return settings if settings.board_id == board_id else None


def save_last_board(board_id: str) -> None:
    folder = settings_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "last_board.json").write_text(json.dumps({"board_id": board_id}), encoding="utf-8")


def last_board_id() -> Optional[str]:
    try:
        return json.loads((settings_dir() / "last_board.json").read_text(encoding="utf-8")).get("board_id")
    except (OSError, ValueError):
        return None
