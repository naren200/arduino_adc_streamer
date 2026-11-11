"""Board driver for TestBoard_7953 (4 x ADS7953, one burst = one channel).

Behaviour is that of the first CapTest version: the driver only puts the existing
protocol, fit front end and simulator behind the BoardDriver interface.
"""

from __future__ import annotations

import dataclasses
import math
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

    # ---------------------------------------------------------- force calibration
    supports_force_calibration = True
    force_board_profile = "TestBoard_7953"

    # ------------------------------------------------------------------ Vinf mode
    supports_vinf = True
    HOLD_TAU_MULTIPLES = 12.0       # held burst length in tau of the guessed capacitance
    HOLD_MIN_SAMPLES = 256

    def hold_samples(self, settings, ref: Channel, c_total_pf: Optional[float] = None) -> int:
        """Samples of a held burst: 12 tau of the capacitance (the fitted one, 20 % more, or
        300 pF when none is known), between 256 and the firmware maximum."""
        c_pf = 1.2 * c_total_pf if c_total_pf and math.isfinite(c_total_pf) else max(settings.c_guess_pf, 300.0)
        n = math.ceil(self.HOLD_TAU_MULTIPLES * settings.r_bias(*ref) * c_pf * 1e-12
                      / settings.nominal_period_s())
        return max(self.HOLD_MIN_SAMPLES, min(protocol.MAX_SAMPLES, n))

    SETTLE_END_V = 0.003            # settle to within this distance of V_D, then as long again for the quiet tail

    def settle_frames(self, settings, ref: Channel, c_total_pf: Optional[float] = None,
                      c_share_pf: Optional[float] = None) -> int:
        """Command frames of an alternating burst long enough for the sensor to settle and then
        stay quiet for as long again. Each cycle (one sensor frame, Vmid frames) multiplies the
        distance d below V_D by exp(-T / tau) * (1 - C_share / C_total); from d = V_D to
        SETTLE_END_V takes N such cycles, and the burst is 2 N cycles. Without a fitted
        capacitance, as many frames as the firmware allows."""
        run = 1 + settings.vmid_frames
        frames = protocol.MAX_SAMPLES
        if c_total_pf and math.isfinite(c_total_pf) and c_total_pf > 0:
            share = c_share_pf / c_total_pf if c_share_pf and math.isfinite(c_share_pf) else 0.1
            share = min(max(share, 0.02), 0.5)
            tau = settings.r_bias(*ref) * c_total_pf * 1e-12
            factor = math.exp(-settings.nominal_period_s() / tau) * (1.0 - share)
            settle = math.ceil(math.log(self.SETTLE_END_V / self.vmid_volts) / math.log(factor))
            frames = 2 * settle * run
        return max(64 * run, min(protocol.MAX_SAMPLES, frames))

    def measure_alternating_hold(self, client, connection: Connection, settings, frames: int) -> Outcome:
        """capalt with one connected time (no long gap): one sensor frame, the Vmid frames,
        repeated for `frames` frames, as production runs the channels."""
        adc, channel = connection.reads[0]
        text = protocol.format_alternate_command(
            adc, channel, frames, settings.park_us, settings.gap_ns, 1, settings.vmid_frames,
            settings.first_clock_delay_ns, 0)
        try:
            packet = client.burst_packet(text, protocol.HEADER_BYTES, protocol.packet_length_from_header)
        except protocol.NotOkError as exc:
            raise protocol.NotOkError(
                "%s. Vinf mode needs CapTest firmware 1.3.0 or newer; flash it" % exc) from exc
        burst = protocol.decode_burst_packet(packet)
        return Outcome(packet, burst, burst.boot_id)

    def measure_hold(self, client, connection: Connection, settings, samples: int) -> Outcome:
        """One connection held for the whole burst (capburst) of `samples` samples."""
        return self.measure(client, connection, dataclasses.replace(settings, method="burst",
                                                                    n_samples=samples))

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
