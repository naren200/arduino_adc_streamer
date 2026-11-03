"""Board driver for TestBoard_7953 (4 x ADS7953, one burst = one channel).

Behaviour is that of the first CapTest version: the driver only puts the existing
protocol, fit front end and simulator behind the BoardDriver interface.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .. import capfit, protocol
from ..settings import (ADC_NUMBERS, ROUTED_CHANNELS, default_bias_table, key,
                        parse_channel_spec, routed_channels)
from .base import BoardDriver, Channel, Connection, GridGroup, Outcome


class Driver7953(BoardDriver):
    def __init__(self, descriptor: dict, entry=None):
        super().__init__(descriptor, entry)
        channels = descriptor["channels"]
        self.routed = {int(k): int(v) for k, v in channels["routed"].items()}
        self.grid_columns = int(channels.get("grid_columns", 8))

    # ------------------------------------------------------------- channel model
    def groups(self) -> List[GridGroup]:
        result = []
        for adc in sorted(self.routed):
            channels = [(adc, c) for c in range(self.routed[adc])]
            columns = self.grid_columns
            rows = [channels[i:i + columns] for i in range(0, len(channels), columns)]
            grid = [row + [None] * (columns - len(row)) for row in rows]
            result.append(GridGroup(adc, "ADC%d" % adc, channels, grid,
                                    [str(r * columns) for r in range(len(rows))],
                                    [str(c) for c in range(columns)]))
        return result

    def parse_spec(self, text: str) -> List[Channel]:
        return parse_channel_spec(text)

    def auto_channels(self, group_ids: Optional[Sequence[int]] = None) -> List[Channel]:
        return self.channels(group_ids if group_ids is not None else ADC_NUMBERS)

    # ------------------------------------------------------------------ defaults
    def default_bias_table(self) -> Dict[str, float]:
        return default_bias_table()

    # ------------------------------------------------------------------- planning
    def single(self, channel: Channel) -> Connection:
        channel = tuple(channel)
        return Connection((channel,), (channel,), self.display(channel))

    def plan(self, channels, settings, per_address: Optional[bool] = None) -> List[Connection]:
        return [self.single(c) for c in channels]

    # ------------------------------------------------------------ device actions
    def measure(self, client, connection: Connection, settings) -> Outcome:
        adc, channel = connection.reads[0]
        samples = settings.resolve_samples(adc, channel)
        if settings.method == "alternate":
            text = protocol.format_alternate_command(
                adc, channel, samples, settings.park_us, settings.gap_ns, 1, settings.vmid_frames,
                settings.first_clock_delay_ns, settings.long_gap_ns(adc, channel))
            try:
                packet = client.burst_packet(text, protocol.HEADER_BYTES, protocol.packet_length_from_header)
            except protocol.NotOkError as exc:
                raise protocol.NotOkError(
                    "%s. Vmid between samples needs CapTest firmware 1.3.0 or newer; flash it, or set "
                    "Method to 'one connection per burst'" % exc) from exc
            burst = protocol.decode_burst_packet(packet)
            return Outcome(packet, burst, burst.boot_id)
        burst = client.burst(adc, channel, samples, settings.park_us, settings.gap_ns,
                             settings.first_clock_delay_ns)
        return Outcome(protocol.encode_burst_packet(burst), burst, burst.boot_id)

    def decode(self, packet: bytes):
        return protocol.decode_burst_packet(packet)

    def analyze(self, decoded, refs, settings, since_ms) -> Dict[Channel, capfit.Measurement]:
        ref = tuple(refs[0])
        # The packet says how it was taken, so re-analysis follows the data, not the settings.
        analyze = (capfit.analyze_alternate if decoded.flags & protocol.FLAG_ALTERNATE
                   else capfit.analyze_burst)
        measurement = analyze(
            decoded, settings.r_bias(*ref), settings.analysis(),
            since_last_ms=since_ms.get(ref), discharge_ms=settings.discharge_ms)
        measurement.label = self.display(ref)
        return {ref: measurement}

    def vmid(self, client, target: int) -> dict:
        return client.vmid(target)

    def vmid_text(self, reading: dict, vref: float) -> str:
        volts = reading["mean_code"] / 4096.0 * vref
        return ("VMID ADC%d: mean code %.2f (%.4f V), min %d, max %d, tag errors %d"
                % (reading["adc"], reading["mean_code"], volts, reading["min"],
                   reading["max"], reading["tag_errors"]))

    # ---------------------------------------------------------------- simulation
    def sim_config(self, **kw):
        from ..sim import SimConfig

        return SimConfig(**kw)

    def create_simulator(self, clock=None, bias_table=None, config=None):
        from ..sim import SimulatedDevice

        return SimulatedDevice(config or self.sim_config(), clock=clock, r_bias_table=bias_table)


def create_driver(descriptor: dict, entry=None) -> Driver7953:
    return Driver7953(descriptor, entry)
