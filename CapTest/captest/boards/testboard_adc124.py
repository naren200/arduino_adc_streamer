"""Board driver for TestBoard_ADC124 (2 x ADC124S101 behind GPIO-switched muxes).

One burst connects input X of all four muxes of one array (one address) and converts
one to four of the mux outputs round-robin. The firmware switches the mux inside the
burst and time-stamps the edge, so the connection instant is measured, not inferred.
Time model and assumptions: docs/architecture/firmware/TESTBOARD_ADC124_CAPTEST.md.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .. import capfit
from ..settings import Settings
from . import adc124_protocol as proto
from .base import BoardDriver, Channel, Connection, GridGroup, Outcome

FRAME_GAP_NS = 37.0      # measured LPSPI inter-frame gap at 16 MHz (nominal period only)


def _numbers(text: str) -> List[int]:
    return [int(x) for x in re.findall(r"\d+", text)]


class DriverADC124(BoardDriver):
    def __init__(self, descriptor: dict, entry=None):
        super().__init__(descriptor, entry)
        ch = descriptor["channels"]
        self.arrays: List[int] = [int(a) for a in ch["arrays"]]
        self.muxes: List[int] = [int(m) for m in ch["muxes"]]
        self.inputs: Dict[int, List[int]] = {int(m): [int(i) for i in v]
                                             for m, v in ch["inputs"].items()}
        self.vmid_address = int(ch["vmid_address"])
        # ADC input IN(n) (1..4) that mux m (1..4) is assumed to feed.
        self.mux_adc_input: Dict[int, int] = {m: int(a) for m, a in
                                              zip(self.muxes, ch["mux_to_adc_input"])}
        self._bias_conflicts: List[str] = []

    # ------------------------------------------------------------- channel model
    def valid(self, ref: Sequence[int]) -> bool:
        """Exactly the firmware's validSensorRoute()."""
        if len(ref) != 3:
            return False
        array, mux, inp = ref
        return array in self.arrays and mux in self.muxes and inp in self.inputs[mux]

    def groups(self) -> List[GridGroup]:
        width = max(max(v) for v in self.inputs.values()) + 1
        result = []
        for array in self.arrays:
            grid, channels = [], []
            for mux in self.muxes:
                row: List[Optional[Channel]] = []
                for inp in range(width):
                    ref = (array, mux, inp)
                    if self.valid(ref):
                        row.append(ref)
                        channels.append(ref)
                    else:
                        row.append(None)
                grid.append(row)
            result.append(GridGroup(array, "Array %d" % array, channels, grid,
                                    ["MUX%d" % m for m in self.muxes],
                                    [str(i) for i in range(width)]))
        return result

    def parse_spec(self, text: str) -> List[Channel]:
        """'1:1:0,1:4:6,2:2:0'; each field may be '*' or a range '0-3' ('1:*:*' = array 1)."""
        result: List[Channel] = []
        for part in text.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            fields = [f.strip() for f in part.split(":")]
            if len(fields) != 3:
                raise ValueError("expected array:mux:input, got %r" % part)
            choices = []
            for name, field, universe in (("array", fields[0], self.arrays),
                                          ("mux", fields[1], self.muxes),
                                          ("input", fields[2], sorted({i for v in self.inputs.values()
                                                                       for i in v}))):
                if field == "*":
                    choices.append(list(universe))
                elif "-" in field:
                    low, high = (int(s) for s in field.split("-", 1))
                    if low > high:
                        raise ValueError("bad range %r" % part)
                    choices.append(list(range(low, high + 1)))
                else:
                    choices.append([int(field)])
            wildcard = any(f == "*" or "-" in f for f in fields)
            for array in choices[0]:
                for mux in choices[1]:
                    for inp in choices[2]:
                        ref = (array, mux, inp)
                        if not self.valid(ref):
                            if wildcard:
                                continue          # '1:*:*' skips the unpopulated inputs
                            raise ValueError("no sensor channel %d:%d:%d (input 6 exists only on "
                                             "MUX4; inputs 0..5 elsewhere; 7 is VMID)" % ref)
                        if ref not in result:
                            result.append(ref)
        return result

    def shared_node(self, ref: Channel):
        return (ref[0], ref[1])

    def chart_group(self, ref: Channel) -> str:
        return "A%d MUX%d" % (ref[0], ref[1])

    # ------------------------------------------------------------------ defaults
    def _firmware_table(self) -> List[List[int]]:
        """kBiasResistorOhms from the firmware header when it can be read."""
        header = self._repo_path(self.descriptor["bias"].get("firmware_header"))
        if header is not None and header.is_file():
            text = header.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"kBiasResistorOhms\s*\[[^\]]*\]\s*\[[^\]]*\]\s*=\s*\{(.*?)\};",
                              text, re.DOTALL)
            if match:
                numbers = _numbers(match.group(1))
                if len(numbers) == 8:
                    return [numbers[0:4], numbers[4:8]]
        return [list(r) for r in self.descriptor["bias"]["firmware_table"]]

    def _repo_path(self, relative: Optional[str]):
        from pathlib import Path

        if not relative:
            return None
        root = self.entry.repo_root if self.entry is not None else Path(__file__).resolve().parents[3]
        return root / relative

    def default_bias_table(self) -> Dict[str, float]:
        """Per channel R_bias: the routes JSON, else the firmware table; any disagreement
        between the two is kept in bias_conflicts()."""
        firmware = self._firmware_table()
        table: Dict[str, float] = {}
        from_json: Dict[str, float] = {}
        path = self._repo_path(self.descriptor["bias"].get("routes_file"))
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            from_json = {str(k): float(v) for k, v in loaded["bias_resistors_ohms"].items()}
        except (OSError, ValueError, KeyError, AttributeError):
            pass
        conflicts = []
        for ref in self.channels():
            name = self.key(ref)
            table_value = float(firmware[ref[0] - 1][ref[1] - 1])
            if name in from_json:
                table[name] = from_json[name]
                if abs(from_json[name] - table_value) > 0.5:
                    conflicts.append("%s: routes JSON %.0f ohm, firmware table %.0f ohm"
                                     % (name, from_json[name], table_value))
            else:
                table[name] = table_value
        self._bias_conflicts = conflicts
        return table

    def bias_conflicts(self) -> List[str]:
        if not self._bias_conflicts:
            self.default_bias_table()
        return list(self._bias_conflicts)

    # ------------------------------------------------------------------ planning
    def touches(self, array: int, address: int) -> Tuple[Channel, ...]:
        """Every sensor connected by a burst at (array, address): input X of all muxes."""
        return tuple((array, mux, address) for mux in self.muxes
                     if self.valid((array, mux, address)))

    def _connection(self, array: int, address: int, reads: Sequence[Channel]) -> Connection:
        reads = tuple(sorted(reads, key=lambda r: r[1]))
        label = ("array %d address %d (MUX %s)"
                 % (array, address, ",".join(str(r[1]) for r in reads)))
        return Connection(reads, self.touches(array, address), label, (array, address))

    def single(self, channel: Channel) -> Connection:
        channel = tuple(channel)
        return Connection((channel,), self.touches(channel[0], channel[2]),
                          self.display(channel), (channel[0], channel[2]))

    def plan(self, channels, settings, per_address: Optional[bool] = None) -> List[Connection]:
        if per_address is None:
            per_address = settings.per_address
        unique: List[Channel] = []
        for ref in channels:
            ref = tuple(ref)
            if ref not in unique:
                unique.append(ref)
        if per_address:
            groups: Dict[Tuple[int, int], List[Channel]] = {}
            for ref in sorted(unique, key=lambda r: (r[0], r[2], r[1])):
                groups.setdefault((ref[0], ref[2]), []).append(ref)
            return [self._connection(a, x, refs) for (a, x), refs in groups.items()]
        # One channel per connection. Mux-major order: consecutive connections use
        # different addresses, so the sensors a neighbouring mux just connected have
        # the most time to discharge before they are read.
        return [self.single(r) for r in sorted(unique, key=lambda r: (r[0], r[1], r[2]))]

    # -------------------------------------------------------------- device actions
    def frame_period_s(self, settings) -> float:
        return proto.FRAME_CLOCKS / settings.spi_clock_hz + FRAME_GAP_NS * 1e-9

    def pre_frames_for(self, settings, n_inputs: int) -> int:
        return -(-max(int(settings.pre_samples), 1) // n_inputs) * n_inputs

    def post_frames_for(self, settings, connection: Connection) -> int:
        """Frames after the connection: N per channel x the channels read."""
        m = len(connection.reads)
        if settings.n_samples:
            per_channel = int(settings.n_samples)
        else:
            r_max = max(settings.r_bias(*c) for c in connection.reads)
            frames = 6.0 * r_max * settings.c_guess_pf * 1e-12 / self.frame_period_s(settings)
            per_channel = max(32, int(np.ceil(frames / m)) + 1)
        limit = (proto.MAX_FRAMES - self.pre_frames_for(settings, m)) // m
        return max(1, min(per_channel, limit)) * m

    def adc_mask(self, reads: Sequence[Channel]) -> int:
        mask = 0
        for ref in reads:
            mask |= 1 << (self.mux_adc_input[ref[1]] - 1)
        return mask

    def measure(self, client, connection: Connection, settings) -> Outcome:
        array, address = connection.params
        text = proto.format_burst_command(
            array, address, self.adc_mask(connection.reads),
            self.post_frames_for(settings, connection), settings.park_us, settings.pre_samples,
            settings.spi_clock_hz)
        packet = client.burst_packet(text, proto.HEADER_BYTES, proto.packet_length_from_header)
        decoded = proto.decode_burst_packet(packet)
        return Outcome(packet, decoded, decoded.boot_id)

    def decode(self, packet: bytes):
        return proto.decode_burst_packet(packet)

    # ---------------------------------------------------------------- time model
    @staticmethod
    def frame_line(burst: proto.BurstData124):
        """Robust line through the pop stamps: (offset a, period T, kept mask).

        stamps are seconds after start_cycles; the pops carry a positive polling
        latency, so outliers are late pops (interrupt stalls) and are rejected."""
        rel = ((burst.stamps.astype(np.int64) - int(burst.start_cycles)) & 0xFFFFFFFF
               ).astype(np.float64) / burst.cpu_hz
        k = np.arange(len(rel), dtype=np.float64)
        keep = np.ones(len(rel), dtype=bool)
        a, period = 0.0, 0.0
        for _ in range(4):
            if keep.sum() < 3:
                break
            period, a = np.polyfit(k[keep], rel[keep], 1)
            residual = rel - (a + period * k)
            centre = np.median(residual[keep])
            spread = np.median(np.abs(residual[keep] - centre)) * 1.4826
            new = np.abs(residual - centre) < max(4.0 * spread, 40e-9)
            if np.array_equal(new, keep):
                break
            keep = new
        return float(a), float(period), keep

    def timing(self, burst: proto.BurstData124, settings) -> Dict[str, object]:
        """Sample instants of every returned frame and the connection instants (seconds
        after start_cycles), from the stamps and the assumed constants."""
        a, period, keep = self.frame_line(burst)
        k = np.arange(burst.n_frames, dtype=np.float64)
        f_spi = float(burst.spi_clock_hz)
        sample = (a + period * k - settings.pop_latency_ns * 1e-9
                  - (proto.FRAME_CLOCKS - settings.hold_clocks) / f_spi)
        info: Dict[str, object] = {"sample": sample, "period": period, "kept": keep}
        if burst.switch_end_cycles:
            mask = 0xFFFFFFFF
            disable = ((int(burst.switch_begin_cycles) - int(burst.start_cycles)) & mask) / burst.cpu_hz
            enable = (((int(burst.switch_end_cycles) - int(burst.start_cycles)) & mask)
                      / burst.cpu_hz - burst.guard_ns * 1e-9)
            info.update(disable=disable, enable=enable,
                        connect=enable + settings.enable_delay_ns * 1e-9)
        return info

    # ------------------------------------------------------------------ analysis
    def analyze(self, decoded: proto.BurstData124, refs, settings, since_ms
                ) -> Dict[Channel, capfit.Measurement]:
        burst = decoded
        timing = self.timing(burst, settings)
        slots = burst.inputs()
        out: Dict[Channel, capfit.Measurement] = {}
        for ref in refs:
            ref = tuple(ref)
            out[ref] = self._analyze_channel(burst, timing, slots, ref, settings, since_ms.get(ref))
        return out

    def _burst_flags(self, burst: proto.BurstData124, timing, settings) -> List[str]:
        flags: List[str] = []
        min_hz, max_hz, _ = self.spi_range
        if (burst.flags & (proto.FLAG_INCOMPLETE | proto.FLAG_ABORTED) or burst.transfer_errors
                or "connect" not in timing or burst.n_frames <= burst.pre_frames):
            flags.append(capfit.INCOMPLETE)
        nominal_ns = proto.FRAME_CLOCKS / burst.spi_clock_hz * 1e9
        if burst.flags & proto.FLAG_PERIOD_SHORT or timing["period"] * 1e9 < 0.98 * nominal_ns:
            flags.append(capfit.PERIOD_SHORT)
        if not min_hz <= burst.spi_clock_hz <= max_hz:
            flags.append(capfit.CLOCK_OUT_OF_SPEC)
        if burst.flags & proto.FLAG_SWITCH_SLOW:
            flags.append(capfit.SWITCH_JITTER)
        if burst.flags & proto.FLAG_STAMP_GAP or (~timing["kept"]).sum() > 0.1 * burst.n_frames:
            flags.append(capfit.STAMP_GAP)
        return flags

    def _analyze_channel(self, burst: proto.BurstData124, timing, slots: List[int], ref: Channel,
                         settings: Settings, since_ms: Optional[float]) -> capfit.Measurement:
        r_bias = settings.r_bias(*ref)
        lsb_volts = settings.vref / 4096.0
        adc_input = self.mux_adc_input[ref[1]] - 1
        flags = self._burst_flags(burst, timing, settings)
        if adc_input not in slots or ref[0] != burst.array or ref[2] != burst.address:
            flags.append(capfit.INCOMPLETE)
            slot = None
        else:
            slot = slots.index(adc_input)
        empty = np.zeros(0)
        period = burst.period_ns()
        if slot is None or "connect" not in timing:
            record = capfit.BurstRecord(ref, empty, empty, flags=flags, period_ns=period,
                                        label=self.display(ref))
            return capfit.analyze_record(record, r_bias, settings.analysis(), since_ms,
                                         settings.discharge_ms)
        m = len(slots)
        frames = np.arange(slot, burst.n_frames, m)
        times = timing["sample"][frames]
        words = burst.words[frames]
        good = (words & 0xF000) == 0
        volts = capfit.code_to_volts(words & 0x0FFF, settings.vref)
        if not good.all():
            flags.append(capfit.DATA_ERRORS)
        disable, connect = timing["disable"], timing["connect"]
        pre = good & (times < disable)
        post = good & (times >= connect)
        v_d = float("nan")
        pre_idx = np.flatnonzero(pre)
        if len(pre_idx):
            take = pre_idx[-max(1, int(settings.baseline_samples)):]
            v_d = float(np.mean(volts[take]))
        post_idx = np.flatnonzero(post)[max(int(settings.discard_k), 0):]
        t = times[post_idx] - connect
        v = volts[post_idx]
        info = {"disable_us": disable * 1e6, "connect_us": connect * 1e6,
                "open_window_ns": (connect - disable) * 1e9,
                "frame_period_ns": timing["period"] * 1e9, "post_samples": float(len(post_idx)),
                "pre_samples": float(len(pre_idx))}
        record = capfit.BurstRecord(
            ref, t, v, v_d, pre_t=times[pre_idx] - connect, pre_v=volts[pre_idx], flags=flags,
            period_ns=period, label=self.display(ref), connect_info=info)
        measurement = capfit.analyze_record(record, r_bias, settings.analysis(), since_ms,
                                            settings.discharge_ms)
        if np.isfinite(measurement.v0) and not -0.05 < measurement.v0 < settings.vref + 0.05:
            measurement.flags.append(capfit.V0_OUT_OF_RANGE)
        if np.isfinite(measurement.tau) and len(t) and t[-1] < 4.0 * measurement.tau:
            measurement.flags.append(capfit.SHORT_WINDOW)
        return measurement

    # --------------------------------------------------------------------- VMID
    def vmid(self, client, target: int) -> dict:
        ok, comments = client.command("capvmid %d 0*" % target)
        if not ok:
            raise proto.NotOkError("capvmid rejected")
        for line in comments:
            if line.startswith("# capvmid"):
                return proto.parse_vmid_line(line)
        raise proto.FormatError("capvmid reply has no data line")

    def vmid_text(self, reading: dict, vref: float) -> str:
        volts = reading["mean_code"] / 4096.0 * vref
        per_input = " ".join("IN%d %s" % (i, "-" if reading["in%d" % i] is None
                                          else "%.1f" % reading["in%d" % i]) for i in range(1, 5))
        return ("VMID array %d: mean code %.2f (%.4f V), min %d, max %d, data errors %d  [%s]"
                % (reading["array"], reading["mean_code"], volts, reading["min"], reading["max"],
                   reading["data_errors"], per_input))

    # --------------------------------------------------------------- simulation
    def sim_config(self, **kw):
        from .adc124_sim import SimConfig124

        return SimConfig124(**kw)

    def create_simulator(self, clock=None, bias_table=None, config=None):
        from .adc124_sim import SimulatedDevice124

        return SimulatedDevice124(config or self.sim_config(), clock=clock,
                                  r_bias_table=bias_table, driver=self)


def create_driver(descriptor: dict, entry=None) -> DriverADC124:
    return DriverADC124(descriptor, entry)
