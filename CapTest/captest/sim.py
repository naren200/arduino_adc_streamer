"""Emulated TestBoard_7953_CapTest device speaking the real wire protocol.

The simulator is a byte-stream device: commands go in through ``feed`` and the
replies (text lines and binary burst packets) are read back, exactly like the
serial port of the firmware. Physics: each sensor node (PZT + trace + mux pin)
holds a charge that leaks away between connections; connecting it to the shared
MXO node (parked at Vmid) shares charge, then the bias resistor pulls the
combined node to Vmid with tau = R_bias * (C_D + C_sensor).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import protocol
from .link import VirtualClock
from .simscan import ScanSimulator
from .simbase import SimBase, SimLink  # noqa: F401  (SimLink is re-exported)
from .settings import ADC_NUMBERS, default_bias_table, key, routed_channels

BOOT_ID_BASE = 0x7953C000


@dataclass
class SimConfig:
    c_pzt_pf: float = 70.0             # mean sensor-side capacitance (PZT + trace + pin)
    c_pzt_spread: float = 0.15         # +/- fractional spread between channels
    c_d_pf: float = 12.0               # shared MXO node capacitance
    r_bias_tolerance: float = 0.01     # actual R_bias = nominal * (1 +/- tol)
    vmid: float = 1.25
    vmid_pedestal_v: float = 0.0       # charge-injection pedestal: V_D = vmid - pedestal
    v_s_start: float = 0.0             # sensor voltage at power-up
    leak_tau_s: float = 0.005          # sensor discharge time constant when floating
    noise_lsb: float = 1.0
    vref: float = 2.5
    frame_jitter_ns: float = 3.0
    overhead_ns: float = 100.0         # per-frame software overhead beyond 16T + gap
    true_first_clock_delay_ns: float = 25.0
    cpu_hz: int = 600_000_000
    spi_clock_hz: int = 20_000_000
    kind: str = "captest"              # captest | production | silent
    seed: int = 7953
    c_pzt_overrides: Dict[str, float] = field(default_factory=dict)   # "adc:ch" -> pF
    c_d_overrides: Dict[int, float] = field(default_factory=dict)     # adc -> pF
    frame_pattern_lsb: float = 0.0     # optional alternating-frame offset (fault injection)
    fast_fraction: float = 0.0         # share of the step carried by a fast component (0 = one exponential)
    fast_tau_ratio: float = 0.4        # tau_fast / tau_slow
    v_s_overrides: Dict[str, float] = field(default_factory=dict)     # "adc:ch" -> sensor start voltage
    max_samples: int = protocol.MAX_SAMPLES
    firmware_version: str = "1.4.0"

    def create_device(self, clock=None, r_bias_table=None) -> "SimulatedDevice":
        return SimulatedDevice(self, clock=clock, r_bias_table=r_bias_table)


class SimulatedDevice(SimBase):
    def __init__(self, config: Optional[SimConfig] = None, clock=None, r_bias_table=None):
        super().__init__(clock)
        self.config = config or SimConfig()
        self.nominal_bias = default_bias_table()
        self.nominal_bias.update(r_bias_table or {})
        self._rng = np.random.default_rng(self.config.seed)
        self.last_adc = 0
        self.spi_clock_hz = self.config.spi_clock_hz
        self.scan = ScanSimulator(self)
        self._draw_hardware()
        self.power_cycle()

    # --------------------------------------------------------------- hardware
    def _draw_hardware(self) -> None:
        rng = np.random.default_rng(self.config.seed + 1)
        cfg = self.config
        self.c_sensor: Dict[str, float] = {}
        self.r_actual: Dict[str, float] = {}
        for adc in ADC_NUMBERS:
            for ch in routed_channels(adc):
                k = key(adc, ch)
                spread = 1.0 + cfg.c_pzt_spread * rng.uniform(-1, 1)
                self.c_sensor[k] = cfg.c_pzt_overrides.get(k, cfg.c_pzt_pf * spread) * 1e-12
                nominal = self.nominal_bias[k]
                self.r_actual[k] = nominal * (1.0 + cfg.r_bias_tolerance * rng.uniform(-1, 1))

    def c_d(self, adc: int) -> float:
        return self.config.c_d_overrides.get(adc, self.config.c_d_pf) * 1e-12

    def true_values(self, adc: int, channel: int) -> Dict[str, float]:
        """Ground truth for comparisons in tests."""
        k = key(adc, channel)
        c_total = self.c_sensor[k] + self.c_d(adc)
        return {"c_total": c_total, "c_d": self.c_d(adc), "c_sensor": self.c_sensor[k],
                "ratio": self.c_d(adc) / c_total, "r_bias": self.r_actual[k],
                "tau": self.r_actual[k] * c_total}

    def power_cycle(self) -> None:
        """Reboot: new boot id, uptime 0, every sensor uncharged again."""
        self.boot_time = self.clock.now()
        self._boot_count = getattr(self, "_boot_count", 0) + 1
        self.boot_id = (BOOT_ID_BASE + self._boot_count * 0x1F3) & 0xFFFFFFFF
        self._sensor_state: Dict[str, Tuple[float, float]] = {}
        if hasattr(self, "scan"):
            self.scan.reset()
        self._inbox.clear()
        self._outbox.clear()
        self.parked = True

    # --------------------------------------------------------------- commands
    def _handle(self, line: str) -> None:
        parts = line.split(None, 1)
        command = parts[0].lower()
        arguments = parts[1].strip() if len(parts) > 1 else ""
        cfg = self.config
        if cfg.kind == "silent":
            return
        if cfg.kind == "production":
            # Production firmware: normal status reply, unknown commands #NOT_OK.
            if command == "status":
                self._say("# Current mode: PZT")
                self._say("# array=both scanorder=interleaved adcseq=manual")
                self._ack(True, arguments)
            elif command == "mcu":
                self._say("# TestBoard_7953")
                self._ack(True, arguments)
            else:
                self._ack(False, arguments)
            return
        if command in ("capstatus", "status"):
            uptime_ms = int((self.clock.now() - self.boot_time) * 1000)
            self._say(
                "# capstatus firmware=%s version=%s protocol=%d git=simulated "
                "build=Oct_23_2026_00:00:00 boot_id=0x%X uptime_ms=%d cpu_hz=%d "
                "spi_clock_hz=%d max_samples=%d min_period_ns=1000"
                % (protocol.FIRMWARE_NAME, cfg.firmware_version, protocol.PROTOCOL_VERSION,
                   self.boot_id, uptime_ms, cfg.cpu_hz, self.spi_clock_hz, cfg.max_samples))
            self._ack(True, arguments)
        elif command == "capvmid":
            self._cmd_vmid(arguments)
        elif command == "capburst":
            self._cmd_burst(arguments)
        elif command == "capalt" and self._version() >= (1, 2, 0):
            self._cmd_alternate(arguments)
        elif self.scan.handle(command, arguments):
            pass
        elif command in ("capabort", "stop"):
            if self.scan.streaming:
                self.scan.streaming = False        # abort ends a running capture, then parks
            self.parked = True
            self._ack(True, arguments)
        elif command == "spiclock":
            try:
                hz = int(arguments)
            except ValueError:
                return self._ack(False, arguments)
            ok = 100_000 <= hz <= 20_000_000
            if ok:
                self.spi_clock_hz = hz
            self._ack(ok, arguments)
        elif command == "mcu":
            self._say("# TestBoard_7953")
            self._ack(True, arguments)
        else:
            self._ack(False, arguments)

    def _adc_noise_code(self, volts: np.ndarray) -> np.ndarray:
        cfg = self.config
        codes = volts / cfg.vref * 4096.0
        codes = codes + self._rng.normal(0.0, cfg.noise_lsb, size=np.shape(codes))
        return np.clip(np.round(codes), 0, 4095).astype(np.int64)

    def _v_d(self) -> float:
        return self.config.vmid - self.config.vmid_pedestal_v

    def _cmd_vmid(self, arguments: str) -> None:
        tokens = arguments.split()
        try:
            adc = int(tokens[0])
            count = int(tokens[1]) if len(tokens) > 1 else 16
        except (ValueError, IndexError):
            return self._ack(False, arguments)
        if adc not in ADC_NUMBERS or not 1 <= count <= 64:
            return self._ack(False, arguments)
        codes = self._adc_noise_code(np.full(count, self._v_d()))
        self.parked = True
        self._say("# capvmid adc=%d n=%d mean_code=%.3f min=%d max=%d tag_errors=0"
                  % (adc, count, codes.mean(), codes.min(), codes.max()))
        self._ack(True, arguments)

    def _cmd_burst(self, arguments: str) -> None:
        cfg = self.config
        tokens = arguments.split()
        try:
            if not 5 <= len(tokens) <= 7:
                raise ValueError
            adc, channel = int(tokens[0]), int(tokens[1])
            park_us, gap_ns = int(tokens[3]), int(tokens[4])
            fcd_ns = int(tokens[5]) if len(tokens) >= 6 else 25
            hold_mode = int(tokens[6]) if len(tokens) == 7 else 0
            if hold_mode not in (0, 1):
                raise ValueError
            auto = tokens[2] == "auto"
            n = 0 if auto else int(tokens[2])
        except ValueError:
            return self._ack(False, arguments)
        if adc not in ADC_NUMBERS or channel not in routed_channels(adc) or self.fail_next_burst:
            self.fail_next_burst = False
            return self._ack(False, arguments)
        clock_t = 1e9 / self.spi_clock_hz
        nominal_ns = 16 * clock_t + gap_ns
        if auto:
            r = self.nominal_bias[key(adc, channel)]
            n = int(min(1024, max(32, 6 * r * 100e-12 * 1e9 / nominal_ns + 1)))
        if (not 8 <= n <= cfg.max_samples or nominal_ns < 1000
                or nominal_ns < fcd_ns + 13 * clock_t + 325
                or park_us > 10000 or gap_ns > 100000 or fcd_ns > 1000
                or park_us + (n + 12) * nominal_ns / 1000.0 > 20000):
            return self._ack(False, arguments)
        burst = self._generate_burst(adc, channel, n, park_us, gap_ns, fcd_ns)
        if hold_mode:
            burst.flags |= protocol.FLAG_CONTINUE_MODE
        self._ack(True, arguments)
        self._send_packet(protocol.encode_burst_packet(burst))

    def _pump(self) -> None:
        self.scan.pump()

    def _version(self) -> Tuple[int, ...]:
        return tuple(int(part) for part in self.config.firmware_version.split("."))

    def _cmd_alternate(self, arguments: str) -> None:
        cfg = self.config
        tokens = arguments.split()
        try:
            max_tokens = 9 if self._version() >= (1, 3, 0) else 8
            if not 7 <= len(tokens) <= max_tokens:
                raise ValueError
            adc, channel, n, park_us, gap_ns, sensor_run, vmid_run = (int(t) for t in tokens[:7])
            fcd_ns = int(tokens[7]) if len(tokens) >= 8 else 25
            gap_long_ns = int(tokens[8]) if len(tokens) == 9 else 0
        except ValueError:
            return self._ack(False, arguments)
        runs_ok = all(1 <= run <= protocol.MAX_RUN_FRAMES for run in (sensor_run, vmid_run))
        if adc not in ADC_NUMBERS or channel not in routed_channels(adc) or not runs_ok:
            return self._ack(False, arguments)
        clock_t = 1e9 / self.spi_clock_hz
        nominal_ns = 16 * clock_t + gap_ns
        longest_ns = 16 * clock_t + max(gap_ns, gap_long_ns)
        if (not 8 <= n <= cfg.max_samples or nominal_ns < 1000
                or nominal_ns < fcd_ns + 13 * clock_t + 325
                or park_us > 10000 or gap_ns > 100000 or fcd_ns > 1000
                or gap_long_ns > 100000 or (gap_long_ns and gap_long_ns < gap_ns)
                or park_us + (n + 12) * longest_ns / 1000.0 > 20000):
            return self._ack(False, arguments)
        burst = self._generate_alternate(adc, channel, n, park_us, gap_ns, fcd_ns, sensor_run, vmid_run,
                                         gap_long_ns)
        self._ack(True, arguments)
        self._send_packet(protocol.encode_burst_packet(burst))

    # ----------------------------------------------------------------- physics
    def _decay(self, dt: float, tau: float) -> float:
        """Normalised remaining step: one exponential, or fast + slow components."""
        f = self.config.fast_fraction
        if f <= 0:
            return math.exp(-dt / tau)
        return (1.0 - f) * math.exp(-dt / tau) + f * math.exp(-dt / (tau * self.config.fast_tau_ratio))

    def _frame_times(self, frames: int, gap_ns: int, extra_s: Optional[np.ndarray] = None):
        """Nominal period, CS-fall times, cycle counts and timestamps of one burst.
        extra_s: per-frame additional period (capalt's long gap)."""
        cfg = self.config
        clock_t = 1.0 / self.spi_clock_hz
        period_s = 16 * clock_t + gap_ns * 1e-9 + cfg.overhead_ns * 1e-9
        jitter = self._rng.normal(0.0, cfg.frame_jitter_ns * 1e-9, size=frames)
        periods = np.maximum(period_s + jitter, 16 * clock_t + 50e-9)
        if extra_s is not None:
            periods = periods + extra_s
        t_frames = np.concatenate([[0.0], np.cumsum(periods[:-1])])   # CS fall of frame j
        cycles = np.rint(t_frames * cfg.cpu_hz).astype(np.int64)
        start = int(self._rng.integers(0, 1 << 32))
        timestamps = ((cycles + start) & 0xFFFFFFFF).astype(np.uint32)
        return period_s, t_frames, cycles, timestamps

    def _sensor_voltage_now(self, k: str) -> float:
        cfg = self.config
        v_start, t_rel = self._sensor_state.get(
            k, (cfg.v_s_overrides.get(k, cfg.v_s_start), self.boot_time))
        return v_start * math.exp(-max(self.clock.now() - t_rel, 0.0) / cfg.leak_tau_s)

    def _generate_burst(self, adc, channel, n, park_us, gap_ns, fcd_ns) -> protocol.BurstData:
        cfg = self.config
        k = key(adc, channel)
        clock_t = 1.0 / self.spi_clock_hz
        frames = n + protocol.LEAD_FRAMES
        period_s, t_frames, cycles, timestamps = self._frame_times(frames, gap_ns)

        # Sensor state at connection time.
        now = self.clock.now()
        v_s = self._sensor_voltage_now(k)
        c_s, c_d = self.c_sensor[k], self.c_d(adc)
        c_tot = c_s + c_d
        v_d = self._v_d()
        v0 = (c_d * v_d + c_s * v_s) / c_tot
        tau = self.r_actual[k] * c_tot
        t_c = t_frames[1] + cfg.true_first_clock_delay_ns * 1e-9 + 1.5 * clock_t

        volts = np.empty(frames)
        tags = np.empty(frames, dtype=np.int64)
        for j in range(frames):
            if j < protocol.LEAD_FRAMES:
                volts[j], tags[j] = v_d, protocol.VMID_CHANNEL
            else:
                dt = max(t_frames[j] - t_c, 0.0)
                volts[j] = cfg.vmid - (cfg.vmid - v0) * self._decay(dt, tau)
                tags[j] = channel
        codes = self._adc_noise_code(volts)
        if cfg.frame_pattern_lsb:
            codes = codes + np.where(np.arange(frames) % 2 == 0, 1, -1) * int(cfg.frame_pattern_lsb)
            codes = np.clip(codes, 0, 4095)
        words = ((tags << 12) | codes).astype(np.uint16)

        # Leave the sensor floating at its end voltage; it leaks from release on.
        t_end = t_frames[-1]
        v_end = cfg.vmid - (cfg.vmid - v0) * self._decay(t_end - t_c, tau)
        self._sensor_state[k] = (v_end, now + t_end + 3 * period_s)
        tag_errors = int(np.sum(tags[protocol.LEAD_FRAMES:] != channel))
        return self._burst_data(adc, channel, n, park_us, gap_ns, fcd_ns, period_s, cycles, timestamps,
                                words, tag_errors)

    def _generate_alternate(self, adc, channel, n, park_us, gap_ns, fcd_ns,
                            sensor_run, vmid_run, gap_long_ns=0) -> protocol.BurstData:
        """capalt: the mux follows the commands one frame late (it switches at the 2nd SCLK
        of the next frame) and each word is sampled at CS fall, so word j is the input
        commanded in frame j - 2. On a sensor connection the node, held at V_D by the
        Vmid input, shares charge with the sensor; while connected R_bias pulls both
        toward Vmid; while on Vmid the sensor floats and leaks."""
        cfg = self.config
        k = key(adc, channel)
        clock_t = 1.0 / self.spi_clock_hz
        frames = n + protocol.LEAD_FRAMES
        extra = None
        if gap_long_ns:
            # Odd cycles: the frames of phases 1..S (sensor connected) use the long gap.
            index = np.arange(frames)
            cycle, phase = index // (sensor_run + vmid_run), index % (sensor_run + vmid_run)
            stretched = (cycle % 2 == 1) & (phase >= 1) & (phase <= sensor_run) & (index < n)
            extra = np.where(stretched, (gap_long_ns - gap_ns) * 1e-9, 0.0)
        period_s, t_frames, cycles, timestamps = self._frame_times(frames, gap_ns, extra)
        sensor_cmd = np.zeros(frames, dtype=bool)
        sensor_cmd[:n] = protocol.alternate_commands_sensor(n, sensor_run, vmid_run)
        t_select = cfg.true_first_clock_delay_ns * 1e-9 + 1.5 * clock_t

        v_d = self._v_d()
        c_s, c_d = self.c_sensor[k], self.c_d(adc)
        c_tot = c_s + c_d
        tau = self.r_actual[k] * c_tot
        v_s, v_node, connected = self._sensor_voltage_now(k), v_d, False

        def evolve(dt):
            nonlocal v_s, v_node
            if connected:
                v_node = cfg.vmid - (cfg.vmid - v_node) * math.exp(-dt / tau)
                v_s = v_node
            else:
                v_s *= math.exp(-dt / cfg.leak_tau_s)

        volts = np.empty(frames)
        tags = np.empty(frames, dtype=np.int64)
        for j in range(frames):
            volts[j] = v_node
            tags[j] = channel if connected else protocol.VMID_CHANNEL
            evolve(t_select)
            want = j >= 1 and sensor_cmd[j - 1]
            if want and not connected:
                v_node = (c_d * v_node + c_s * v_s) / c_tot
            elif connected and not want:
                v_node = v_d
            connected = want
            evolve((t_frames[j + 1] - t_frames[j] if j + 1 < frames else period_s) - t_select)
        words = ((tags << 12) | self._adc_noise_code(volts)).astype(np.uint16)

        self._sensor_state[k] = (v_s, self.clock.now() + t_frames[-1] + 3 * period_s)
        expected = np.where(sensor_cmd[:n], channel, protocol.VMID_CHANNEL)
        tag_errors = int(np.sum(tags[protocol.LEAD_FRAMES:] != expected))
        burst = self._burst_data(adc, channel, n, park_us, gap_ns, fcd_ns, period_s, cycles, timestamps,
                                 words, tag_errors)
        burst.flags |= protocol.FLAG_ALTERNATE
        return burst

    def _burst_data(self, adc, channel, n, park_us, gap_ns, fcd_ns, period_s, cycles, timestamps,
                    words, tag_errors) -> protocol.BurstData:
        cfg = self.config
        clock_t = 1.0 / self.spi_clock_hz
        v_d = self._v_d()
        self.parked = True
        self.last_adc = adc
        self.bursts_served += 1

        n_vmid = min(max(int(park_us / (period_s * 1e6)) + 4, 4), protocol.MAX_VMID_WORDS)
        vmid_words = ((protocol.VMID_CHANNEL << 12) | self._adc_noise_code(np.full(n_vmid, v_d))).astype(np.uint16)
        deltas = np.diff(cycles)
        flags = 0
        min_ns = deltas.min() * 1e9 / cfg.cpu_hz
        if min_ns < 1000:
            flags |= protocol.FLAG_PERIOD_TOO_SHORT
        if min_ns < fcd_ns + 13 * clock_t * 1e9 + 325:
            flags |= protocol.FLAG_ACQUISITION_SHORT
        return protocol.BurstData(
            adc=adc, channel=channel, n_samples=n, words=words, timestamps=timestamps,
            vmid_words=vmid_words, cpu_hz=cfg.cpu_hz, spi_clock_hz=self.spi_clock_hz,
            gap_ns=gap_ns, first_clock_delay_ns=fcd_ns, boot_id=self.boot_id,
            park_us=park_us, park_frames=int(park_us / (period_s * 1e6)) + 4,
            period_min_cycles=int(deltas.min()), period_sum_cycles=int(deltas.sum()),
            period_max_cycles=int(deltas.max()), timeout_errors=0, tag_errors=tag_errors,
            flags=flags)
