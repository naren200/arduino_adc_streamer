"""Emulated TestBoard_ADC124 (production, diagnostic or CapTest firmware) for tests and
the "Simulation mode" of the application.

The device speaks the real wire protocol (bytes in, bytes out). Physics per burst:
a connection at (array, address) joins input X of all four muxes of the array. Each
mux has its own output node D (capacitance C_D, bias resistor R_bias to VMID); a
connected sensor shares its charge with it at once and the bias resistor then pulls
the combined node to VMID with tau = R_bias x (C_D + C_sensor). Every sensor that was
connected (read or not) leaks while it is floating again. The ADC converts the mux
outputs round-robin; frame times, pop latency, GPIO switch timing and ADC noise are
generated like the firmware reports them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..simbase import SimBase
from . import adc124_protocol as proto

BOOT_ID_BASE = 0x124CA000
GUARD_NS = 200
SWITCH_CYCLES = 367               # measured: switchAddress() = 611.7 ns at 600 MHz
SWITCH_START_NS = 53.3            # measured: switch begins after the last pre-connect pop


@dataclass
class SimConfig124:
    c_pzt_pf: float = 70.0             # mean sensor-side capacitance (PZT + trace + pin)
    c_pzt_spread: float = 0.15         # +/- fractional spread between channels
    c_d_pf: float = 12.0               # mean mux-output node capacitance
    c_d_spread: float = 0.10           # +/- spread between the eight mux nodes
    r_bias_tolerance: float = 0.01     # actual R_bias = nominal * (1 +/- tol)
    vmid: float = 1.65
    vmid_pedestal_v: float = 0.0       # charge injection: the D node sits this far below VMID
    v_s_start: float = 0.0             # sensor voltage at power-up
    leak_tau_s: float = 0.005          # sensor discharge time constant when floating
    noise_lsb: float = 1.0
    vref: float = 3.3
    gap_ns: float = 37.0               # LPSPI inter-frame gap
    frame_jitter_ns: float = 2.0
    pop_latency_ns: float = 80.0       # true mean latency between frame end and its time stamp
    pop_jitter_ns: float = 25.0
    stall_probability: float = 0.0     # chance of a late pop (interrupt) per frame
    true_hold_clocks: float = 3.3      # the real ADC holds a little later than the 3 assumed
    true_enable_delay_ns: float = 30.0
    cpu_hz: int = 600_000_000
    spi_clock_hz: int = 16_000_000
    kind: str = "captest"              # captest | production | diagnostic | silent
    seed: int = 124
    c_pzt_overrides: Dict[str, float] = field(default_factory=dict)   # "array:mux:input" -> pF
    c_d_overrides: Dict[str, float] = field(default_factory=dict)     # "array:mux" -> pF
    v_s_overrides: Dict[str, float] = field(default_factory=dict)     # "array:mux:input" -> volts
    frame_pattern_lsb: float = 0.0     # alternating-frame offset (fault injection)
    fast_fraction: float = 0.0         # share of the step carried by a fast component
    fast_tau_ratio: float = 0.4        # tau_fast / tau_slow
    max_frames: int = proto.MAX_FRAMES
    firmware_version: str = "1.0.0"

    def create_device(self, clock=None, r_bias_table=None) -> "SimulatedDevice124":
        return SimulatedDevice124(self, clock=clock, r_bias_table=r_bias_table)


class SimulatedDevice124(SimBase):
    def __init__(self, config: Optional[SimConfig124] = None, clock=None, r_bias_table=None,
                 driver=None):
        super().__init__(clock)
        from . import load_driver

        self.driver = driver or load_driver("TestBoard_ADC124")
        self.config = config or SimConfig124()
        self.nominal_bias = self.driver.default_bias_table()
        self.nominal_bias.update(r_bias_table or {})
        self._rng = np.random.default_rng(self.config.seed)
        self.last_array = 0
        self.spi_clock_hz = self.config.spi_clock_hz
        self._draw_hardware()
        self.power_cycle()

    # ----------------------------------------------------------------- hardware
    def _draw_hardware(self) -> None:
        rng = np.random.default_rng(self.config.seed + 1)
        cfg = self.config
        self.c_sensor: Dict[Tuple[int, ...], float] = {}
        self.r_actual: Dict[Tuple[int, ...], float] = {}
        self._c_d: Dict[Tuple[int, int], float] = {}
        for array in self.driver.arrays:
            for mux in self.driver.muxes:
                name = "%d:%d" % (array, mux)
                spread = 1.0 + cfg.c_d_spread * rng.uniform(-1, 1)
                self._c_d[(array, mux)] = cfg.c_d_overrides.get(name, cfg.c_d_pf * spread) * 1e-12
        for ref in self.driver.channels():
            name = self.driver.key(ref)
            spread = 1.0 + cfg.c_pzt_spread * rng.uniform(-1, 1)
            self.c_sensor[ref] = cfg.c_pzt_overrides.get(name, cfg.c_pzt_pf * spread) * 1e-12
            self.r_actual[ref] = self.nominal_bias[name] * (
                1.0 + cfg.r_bias_tolerance * rng.uniform(-1, 1))

    def c_d(self, ref) -> float:
        return self._c_d[(ref[0], ref[1])]

    def true_values(self, *ref) -> Dict[str, float]:
        """Ground truth for comparisons in tests: true_values(array, mux, input)."""
        ref = tuple(ref)
        c_total = self.c_sensor[ref] + self.c_d(ref)
        return {"c_total": c_total, "c_d": self.c_d(ref), "c_sensor": self.c_sensor[ref],
                "ratio": self.c_d(ref) / c_total, "r_bias": self.r_actual[ref],
                "tau": self.r_actual[ref] * c_total}

    def power_cycle(self) -> None:
        """Reboot: new boot id, uptime 0, every sensor uncharged again."""
        self.boot_time = self.clock.now()
        self._boot_count = getattr(self, "_boot_count", 0) + 1
        self.boot_id = (BOOT_ID_BASE + self._boot_count * 0x1F3) & 0xFFFFFFFF
        self._sensor_state: Dict[Tuple[int, ...], Tuple[float, float]] = {}
        self._inbox.clear()
        self._outbox.clear()
        self.parked = True

    # ----------------------------------------------------------------- commands
    def _handle(self, line: str) -> None:
        parts = line.split(None, 1)
        command = parts[0].lower()
        arguments = parts[1].strip() if len(parts) > 1 else ""
        cfg = self.config
        if cfg.kind == "silent":
            return
        if cfg.kind in ("production", "diagnostic"):
            if command == "mcu":
                self._say("# TestBoard_ADC124" if cfg.kind == "production" else "# ADC124_PCB_DIAGNOSTIC")
                self._ack(True, arguments)
            elif command == "status" and cfg.kind == "production":
                self._say("# board=TestBoard_ADC124")
                self._say("# protocol_version=3")
                self._say("# running=false")
                self._ack(True, arguments)
            else:
                self._ack(False, arguments)
            return
        if command in ("capstatus", "status"):
            uptime_ms = int((self.clock.now() - self.boot_time) * 1000)
            self._say(
                "# capstatus firmware=%s board=%s version=%s protocol=%d git=simulated "
                "build=Oct_23_2026_00:00:00 boot_id=0x%X uptime_ms=%d cpu_hz=%d spi_clock_hz=%d "
                "max_samples=%d state=idle bursts=%d transfer_errors=0 hold_clocks=3 guard_ns=%d "
                "min_spi_hz=8000000 max_spi_hz=16000000"
                % (proto.FIRMWARE_NAME, proto.BOARD_NAME, cfg.firmware_version,
                   proto.PROTOCOL_VERSION, self.boot_id, uptime_ms, cfg.cpu_hz, self.spi_clock_hz,
                   cfg.max_frames, self.bursts_served, GUARD_NS))
            self._ack(True, arguments)
        elif command == "capvmid":
            self._cmd_vmid(arguments)
        elif command == "capburst":
            self._cmd_burst(arguments)
        elif command in ("capabort", "stop"):
            self.parked = True
            self._ack(True, arguments)
        elif command == "spiclock":
            try:
                hz = int(arguments)
            except ValueError:
                return self._ack(False, arguments)
            ok = 8_000_000 <= hz <= 16_000_000
            if ok:
                self.spi_clock_hz = hz
            self._ack(ok, arguments)
        elif command == "mcu":
            self._say("# " + proto.FIRMWARE_NAME)
            self._ack(True, arguments)
        else:
            self._ack(False, arguments)

    def _adc_codes(self, volts) -> np.ndarray:
        cfg = self.config
        codes = np.asarray(volts, dtype=np.float64) / cfg.vref * 4096.0
        codes = codes + self._rng.normal(0.0, cfg.noise_lsb, size=np.shape(codes))
        return np.clip(np.round(codes), 0, 4095).astype(np.int64)

    def _cmd_vmid(self, arguments: str) -> None:
        tokens = arguments.split()
        try:
            array = int(tokens[0])
            selected = int(tokens[1])
            count = int(tokens[2]) if len(tokens) > 2 else 16
        except (ValueError, IndexError):
            return self._ack(False, arguments)
        if array not in self.driver.arrays or not 0 <= selected <= 4 or not 1 <= count <= 64:
            return self._ack(False, arguments)
        mask = 0x0F if selected == 0 else 1 << (selected - 1)
        per_input = {}
        everything = []
        for i in range(4):
            if mask & (1 << i):
                codes = self._adc_codes(np.full(count, self.config.vmid))
                per_input[i] = float(codes.mean())
                everything.extend(codes.tolist())
        self.parked = True
        text = ("# capvmid array=%d mask=%d n=%d mean_code=%.3f min=%d max=%d"
                % (array, mask, count, float(np.mean(everything)), min(everything), max(everything)))
        for i in range(4):
            text += " in%d=%s" % (i + 1, "%.3f" % per_input[i] if i in per_input else "-")
        self._say(text + " data_errors=0")
        self._ack(True, arguments)

    def _valid_connection(self, array: int, address: int, mask: int) -> bool:
        if array not in self.driver.arrays or not 0 <= address <= 6 or not 1 <= mask <= 15:
            return False
        for mux, adc_input in self.driver.mux_adc_input.items():
            if mask & (1 << (adc_input - 1)) and not self.driver.valid((array, mux, address)):
                return False
        return True

    def _cmd_burst(self, arguments: str) -> None:
        cfg = self.config
        tokens = arguments.split()
        try:
            if not 6 <= len(tokens) <= 7:
                raise ValueError
            array, address, mask = int(tokens[0]), int(tokens[1]), int(tokens[2])
            auto = tokens[3] == "auto"
            post = 0 if auto else int(tokens[3])
            park_us, pre = int(tokens[4]), int(tokens[5])
            clock = int(tokens[6]) if len(tokens) == 7 else 0
        except ValueError:
            return self._ack(False, arguments)
        if (self.fail_next_burst or not self._valid_connection(array, address, mask)
                or park_us > proto.MAX_PARK_US or not 1 <= pre <= proto.MAX_PRE_FRAMES
                or (clock and not 8_000_000 <= clock <= 16_000_000)):
            self.fail_next_burst = False
            return self._ack(False, arguments)
        if clock:
            self.spi_clock_hz = clock
        m = bin(mask).count("1")
        pre_frames = -(-pre // m) * m
        if auto:
            r_max = max(self.nominal_bias[self.driver.key((array, mux, address))]
                        for mux, a in self.driver.mux_adc_input.items() if mask & (1 << (a - 1)))
            frame_ns = 16e9 / self.spi_clock_hz
            post = int(min(2048, 6 * r_max * 0.1 / frame_ns + 1))
        post = -(-post // m) * m
        if post < proto.MIN_POST_FRAMES or pre_frames + post > cfg.max_frames:
            return self._ack(False, arguments)
        burst = self._generate_burst(array, address, mask, m, pre_frames, post, park_us)
        self._ack(True, arguments)
        self._send_packet(proto.encode_burst_packet(burst))

    # ------------------------------------------------------------------ physics
    def _decay(self, dt: np.ndarray, tau: float) -> np.ndarray:
        f = self.config.fast_fraction
        dt = np.asarray(dt, dtype=np.float64)
        if f <= 0:
            return np.exp(-dt / tau)
        return (1.0 - f) * np.exp(-dt / tau) + f * np.exp(-dt / (tau * self.config.fast_tau_ratio))

    def _generate_burst(self, array: int, address: int, mask: int, m: int, pre_frames: int,
                        post_frames: int, park_us: int) -> proto.BurstData124:
        cfg = self.config
        rng = self._rng
        n = pre_frames + post_frames
        f_spi = float(self.spi_clock_hz)
        frame_s = 16.0 / f_spi
        period = frame_s + cfg.gap_ns * 1e-9
        # True frame starts: the lead frame first (it is not returned), then frame k.
        starts = 0.15e-6 + period * np.arange(n + 1) + np.concatenate(
            [[0.0], np.cumsum(rng.normal(0.0, cfg.frame_jitter_ns * 1e-9, size=n))])
        ends = starts + frame_s
        latency = np.maximum(rng.normal(cfg.pop_latency_ns, cfg.pop_jitter_ns, size=n + 1), 5.0) * 1e-9
        if cfg.stall_probability:
            stalled = rng.random(n + 1) < cfg.stall_probability
            latency = latency + stalled * rng.uniform(1.5e-6, 6e-6, size=n + 1)
        pops = ends + latency
        for j in range(1, len(pops)):
            # After a stall the backlog in the RX FIFO is popped back to back (about 120 ns apart).
            pops[j] = max(pops[j], pops[j - 1] + 120e-9)
        start_cycles = int(rng.integers(0, 1 << 32))

        def cycles(seconds):
            return (np.rint(np.asarray(seconds) * cfg.cpu_hz).astype(np.int64) + start_cycles) & 0xFFFFFFFF

        stamps = cycles(pops[1:]).astype(np.uint32)
        lead = int(cycles(pops[0]))
        sw0_s = pops[pre_frames] + SWITCH_START_NS * 1e-9        # pop of returned frame pre-1
        sw0_c = int(cycles(sw0_s))
        sw1_c = (sw0_c + SWITCH_CYCLES) & 0xFFFFFFFF
        t_disable = sw0_s + 3e-9
        t_enable = sw0_s + SWITCH_CYCLES / cfg.cpu_hz - GUARD_NS * 1e-9
        t_connect = t_enable + cfg.true_enable_delay_ns * 1e-9
        sample_t = starts[1:] + cfg.true_hold_clocks / f_spi

        # Sensors connected by this burst: input X of all four muxes.
        now = self.clock.now()
        v_d = cfg.vmid - cfg.vmid_pedestal_v
        node: Dict[int, dict] = {}
        for mux in self.driver.muxes:
            ref = (array, mux, address)
            if not self.driver.valid(ref):
                continue
            name = self.driver.key(ref)
            v_start, t_rel = self._sensor_state.get(
                ref, (cfg.v_s_overrides.get(name, cfg.v_s_start), self.boot_time))
            v_s = v_start * math.exp(-max(now - t_rel, 0.0) / cfg.leak_tau_s)
            c_s, c_d = self.c_sensor[ref], self.c_d(ref)
            c_tot = c_s + c_d
            node[mux] = {"ref": ref, "tau": self.r_actual[ref] * c_tot,
                         "v0": (c_d * v_d + c_s * v_s) / c_tot}

        inputs = [i for i in range(4) if mask & (1 << i)]
        adc_to_mux = {a - 1: mux for mux, a in self.driver.mux_adc_input.items()}
        volts = np.empty(n)
        for k in range(n):
            mux = adc_to_mux[inputs[k % m]]
            if sample_t[k] < t_connect or mux not in node:
                volts[k] = cfg.vmid
            else:
                info = node[mux]
                volts[k] = cfg.vmid - (cfg.vmid - info["v0"]) * float(
                    self._decay(sample_t[k] - t_connect, info["tau"]))
        codes = self._adc_codes(volts)
        if cfg.frame_pattern_lsb:
            codes = np.clip(codes + np.where(np.arange(n) % 2 == 0, 1, -1)
                            * int(cfg.frame_pattern_lsb), 0, 4095)
        words = codes.astype(np.uint16)

        # Every connected sensor floats again at the end of the burst and leaks from there.
        t_end = pops[-1]
        for info in node.values():
            v_end = cfg.vmid - (cfg.vmid - info["v0"]) * float(self._decay(t_end - t_connect, info["tau"]))
            self._sensor_state[info["ref"]] = (v_end, now + t_end + 3 * period)
        self.parked = True
        self.last_array = array
        self.bursts_served += 1

        deltas = np.diff(stamps.astype(np.int64) & 0xFFFFFFFF)
        deltas = np.where(deltas < 0, deltas + (1 << 32), deltas)
        flags = 0
        if deltas.sum() / max(n - 1, 1) / cfg.cpu_hz < 0.98 * frame_s:
            flags |= proto.FLAG_PERIOD_SHORT
        if deltas.max() * (n - 1) > 3 * deltas.sum():
            flags |= proto.FLAG_STAMP_GAP
        return proto.BurstData124(
            array=array, address=address, input_mask=mask, pre_frames=pre_frames, words=words,
            stamps=stamps, flags=flags, hold_clocks=3, guard_ns=GUARD_NS, cpu_hz=cfg.cpu_hz,
            spi_clock_hz=self.spi_clock_hz, boot_id=self.boot_id, park_us=park_us,
            start_cycles=start_cycles, lead_cycles=lead, switch_begin_cycles=sw0_c,
            switch_end_cycles=sw1_c, period_min_cycles=int(deltas.min()),
            period_sum_cycles=int(deltas.sum()), period_max_cycles=int(deltas.max()),
            transfer_errors=0, data_errors=0)
