"""Simulated continuous capture of the TestBoard_7953 (speaks capture protocol 1).

The physics is the simulator's own charge-sharing model (sim.py), one visit per route and
sweep: the floating sensor leaks, force-generated charge adds to it, the connection shares it
with the Vmid-held node (C_D), and R_bias pulls the connected node toward Vmid for the
connected time Tc; the sample is taken at the release frame and the Vmid frame resets the
node. The recorded time stamps are real 32-bit cycle counters (they wrap).

Test hooks: ``force(t)`` (N versus device time since capstart), ``weights`` (share of the
generated charge per route), ``d33_true`` (C/N), and fault switches for the capture tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import capture
from .settings import key

SWEEPS_PER_CHUNK = 8
MAX_CHUNK_BYTES = 2048           # one USB frame of the patched Teensy core (TX_SIZE)
RING_SWEEPS = 128


@dataclass
class ScanFaults:
    drop_chunk_seq: Optional[int] = None        # the chunk with this sequence number never arrives
    corrupt_chunk_seq: Optional[int] = None     # one byte of this chunk is flipped
    overrun_after_sweep: Optional[int] = None   # from this sweep on, `overrun_count` sweeps are lost
    overrun_count: int = 0
    tag_error_at_sweep: Optional[int] = None    # route 0 returns a wrong tag in this sweep
    reset_failure_at_sweep: Optional[int] = None  # the Vmid word of route 0 is off by 40 mV
    clip_route: Optional[Tuple[int, int]] = None   # the sensor word of this route is railed
    clip_from_s: float = 0.0
    reboot_at_chunk: Optional[int] = None
    disconnect_at_chunk: Optional[int] = None
    wrap_start: Optional[int] = None            # initial cycle counter (default random)


class ScanSimulator:
    def __init__(self, device):
        self.dev = device
        self.faults = ScanFaults()
        self.force: Callable[[float], float] = lambda t: 0.0
        self.weights: Dict[Tuple[int, int], float] = {}
        self.d33_true = 20e-12
        self.reset()

    def reset(self) -> None:
        self.cfg: Optional[dict] = None
        self.staging: Optional[dict] = None
        self.streaming = False
        self.session = 0
        self.seq = 0
        self.sweep = 0
        self.max_sweeps = 0
        self.dropped = 0
        self.cycles0 = 0
        self.t_dev = 0.0                        # device time of the next sweep start
        self.sessions_started = 0
        self.force_prev = 0.0
        self.final_sent = False

    # ----------------------------------------------------------------- commands
    def handle(self, command: str, arguments: str) -> bool:
        """True when the command belongs to the capture feature (reply already queued)."""
        dev = self.dev
        if command not in ("capcaps", "capcfg", "capstart", "capstop", "capstat"):
            return False
        if dev._version() < (1, 4, 0):
            dev._ack(False, arguments)
            return True
        tokens = arguments.split()
        if command == "capcaps":
            dev._say("# capcaps capture=1 chunk=1 max_routes=%d sweeps_per_chunk=%d ring_sweeps=%d "
                     "max_chunk_bytes=%d"
                     % (capture.MAX_ROUTES, SWEEPS_PER_CHUNK, RING_SWEEPS, MAX_CHUNK_BYTES))
            dev._ack(True, arguments)
        elif command == "capcfg":
            self._configure_reply(tokens, arguments)
        elif command == "capstart":
            self._start(tokens, arguments)
        elif command == "capstop":
            self._stop(arguments)
        else:
            dev._say("# capstat state=%s session=0x%X sweeps=%d chunks=%d dropped=%d"
                     % ("running" if self.streaming else "idle", self.session, self.sweep,
                        self.seq, self.dropped))
            dev._ack(True, arguments)
        return True

    def _configure_reply(self, tokens, arguments) -> bool:
        """capcfg begin | route | commit; replies (and acknowledges) itself."""
        dev = self.dev
        if self.streaming:
            dev._ack(False, arguments)
            return False
        try:
            sub = tokens[0]
            if sub == "begin":
                gap, fcd, lag, vmid = (int(x) for x in tokens[1:5])
                if vmid != 1 or not (0 <= lag <= 1_000_000 and 0 <= gap <= 100_000 and 0 <= fcd <= 1000):
                    raise ValueError
                self.staging = {"gap_ns": gap, "fcd_ns": fcd, "lag_us": lag, "routes": []}
            elif sub == "route":
                adc, ch = int(tokens[1]), int(tokens[2])
                from .settings import routed_channels, ADC_NUMBERS
                if self.staging is None or adc not in ADC_NUMBERS or ch not in routed_channels(adc):
                    raise ValueError
                if len(self.staging["routes"]) >= capture.MAX_ROUTES or (adc, ch) in self.staging["routes"]:
                    raise ValueError
                self.staging["routes"].append((adc, ch))
            elif sub == "commit":
                routes = self.staging["routes"]
                seen, prev = set(), None
                for adc, _ in routes:
                    if adc != prev:
                        if adc in seen:
                            raise ValueError
                        seen.add(adc)
                        prev = adc
                if not routes:
                    raise ValueError
                period_ns = 16e9 / dev.spi_clock_hz + self.staging["gap_ns"]
                if (period_ns < 1000 or period_ns < self.staging["fcd_ns"] + 13 * 1e9 / dev.spi_clock_hz + 325):
                    raise ValueError
                self.cfg = dict(self.staging)
                blocks = len(seen)
                frames = 2 * len(routes) + 2 * blocks
                dev._say("# capcfg routes=%d blocks=%d frames_per_sweep=%d sweep_ns=%d"
                         % (len(routes), blocks, frames, int(frames * period_ns)))
            else:
                raise ValueError
        except (ValueError, IndexError, TypeError):
            dev._ack(False, arguments)
            return False
        dev._ack(True, arguments)
        return False

    def _start(self, tokens, arguments) -> None:
        dev = self.dev
        try:
            max_sweeps = int(tokens[0]) if tokens else 0
        except ValueError:
            return dev._ack(False, arguments)
        if self.cfg is None or self.streaming or max_sweeps < 0:
            return dev._ack(False, arguments)
        self.sessions_started += 1
        self.session = (0xC0DE0000 + self.sessions_started * 7919) & 0xFFFFFFFF
        self.seq = self.sweep = self.dropped = 0
        self.max_sweeps = max_sweeps
        self.t_dev = 0.0
        self.final_sent = False
        self.cycles0 = (self.faults.wrap_start if self.faults.wrap_start is not None
                        else int(dev._rng.integers(0, 1 << 32)))
        self.force_prev = float(self.force(0.0))
        self._prepare()
        self.per_chunk = max(1, min(SWEEPS_PER_CHUNK, (MAX_CHUNK_BYTES - capture.HEADER_BYTES - 2)
                                    // (len(self.cfg["routes"]) * capture.RECORD_BYTES)))
        self.streaming = True
        dev.parked = False
        dev._ack(True, arguments)

    def _stop(self, arguments) -> None:
        dev = self.dev
        if self.streaming:
            self._emit_chunk(0, final=True, stopped=True)
        self.streaming = False
        dev.parked = True
        dev._ack(True, arguments)

    # ------------------------------------------------------------------ physics
    def _prepare(self) -> None:
        """Per-route constants and state arrays of the configured scan."""
        dev = self.dev
        routes = self.cfg["routes"]
        keys = [key(a, c) for a, c in routes]
        self.c_s = np.array([dev.c_sensor[k] for k in keys])
        self.c_d = np.array([dev.c_d(a) for a, _ in routes])
        self.tau = np.array([dev.r_actual[k] for k in keys]) * (self.c_s + self.c_d)
        self.channels = np.array([c for _, c in routes], dtype=np.int64)
        self.w = np.array([self.weights.get(r, 1.0 / len(routes)) for r in routes])
        v_d = dev._v_d()
        self.v_s = np.array([dev.config.v_s_overrides.get(k, v_d) for k in keys])
        self.t_last = np.zeros(len(routes))
        self.first_sweep = True
        # frame slot of each route inside its ADC block (frames 2r+1 connect, 2r+2 release)
        slot = np.zeros(len(routes), dtype=np.int64)
        block_len = {}
        start = {}
        order = []
        for i, (adc, _) in enumerate(routes):
            if adc not in start:
                start[adc] = i
                order.append(adc)
            slot[i] = i - start[adc]
        for adc in order:
            block_len[adc] = sum(1 for a, _ in routes if a == adc)
        self.slot = slot
        offsets = np.zeros(len(routes))
        frames_before = 0
        for adc in order:
            idx = [i for i, (a, _) in enumerate(routes) if a == adc]
            offsets[idx] = frames_before
            frames_before += 2 * len(idx) + 2
        self.block_offset_frames = offsets
        self.frames_per_sweep = frames_before

    def _sweep(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """One sweep: (ts_conn, ts_rel, sensor words, vmid words) for every route."""
        dev, cfg = self.dev, self.dev.config
        n = len(self.cfg["routes"])
        clock_t = 1.0 / dev.spi_clock_hz
        period = 16 * clock_t + self.cfg["gap_ns"] * 1e-9 + cfg.overhead_ns * 1e-9
        jitter = dev._rng.normal(0.0, cfg.frame_jitter_ns * 1e-9, size=(2, n))
        base = self.t_dev + self.block_offset_frames * period
        conn = base + (2 * self.slot + 1) * period + jitter[0]
        rel = base + (2 * self.slot + 2) * period + jitter[1]
        sweep_end = self.t_dev + self.frames_per_sweep * period + self.cfg["lag_us"] * 1e-6
        v_d = dev._v_d()
        f_now = float(self.force(float(rel.mean())))
        d_force = f_now - self.force_prev
        self.force_prev = f_now
        if self.first_sweep:
            self.t_last = conn.copy()
            self.first_sweep = False
        off = np.maximum(conn - self.t_last, 0.0)
        if cfg.leak_tau_s < 1e5:
            keep = np.exp(-off / cfg.leak_tau_s)
            v_s = self.v_s * keep + v_d * (1.0 - keep)
        else:
            v_s = self.v_s.copy()
        v_s = v_s + self.d33_true * self.w * d_force / self.c_s
        c_tot = self.c_s + self.c_d
        v0 = (self.c_d * v_d + self.c_s * v_s) / c_tot
        tc = rel - conn
        u = cfg.vmid - (cfg.vmid - v0) * np.exp(-tc / self.tau)
        self.v_s, self.t_last = u, rel.copy()
        codes_s = dev._adc_noise_code(u)
        codes_v = dev._adc_noise_code(np.full(n, v_d))
        sensor = ((self.channels << 12) | codes_s).astype(np.uint16)
        vmid = ((15 << 12) | codes_v).astype(np.uint16)
        f = self.faults
        routes = self.cfg["routes"]
        if f.tag_error_at_sweep == self.sweep:
            sensor[0] = (int(sensor[0]) & 0x0FFF) | (7 << 12)
        if f.reset_failure_at_sweep == self.sweep:
            vmid[0] = (15 << 12) | min(4095, (int(vmid[0]) & 0x0FFF) + int(0.04 / cfg.vref * 4096))
        if f.clip_route in routes and float(rel.mean()) >= f.clip_from_s:
            sensor[routes.index(f.clip_route)] = (f.clip_route[1] << 12) | 4095
        cycles = np.rint(np.stack([conn, rel], axis=-1) * cfg.cpu_hz).astype(np.int64) + self.cycles0
        ts = (cycles & 0xFFFFFFFF).astype(np.uint32)
        self.t_dev = sweep_end
        return ts[:, 0], ts[:, 1], sensor, vmid

    # ------------------------------------------------------------------- pumping
    def pump(self) -> None:
        """Called whenever the host reads: produce chunks until some bytes are waiting."""
        dev = self.dev
        guard = 0
        while self.streaming and len(dev._outbox) < 2 and guard < 8:
            guard += 1
            if self.max_sweeps and self.sweep >= self.max_sweeps:
                self._emit_chunk(0, final=True)
                self.streaming = False
                dev.parked = True
                break
            take = self.per_chunk
            if self.max_sweeps:
                take = min(take, self.max_sweeps - self.sweep)
            self._emit_chunk(take)

    def _emit_chunk(self, n_sweeps: int, final: bool = False, stopped: bool = False) -> None:
        dev, f = self.dev, self.faults
        n_routes = len(self.cfg["routes"])
        first = self.sweep
        rows = []
        lost = 0
        for _ in range(n_sweeps):
            data = self._sweep()
            if f.overrun_after_sweep is not None and self.sweep >= f.overrun_after_sweep and f.overrun_count > 0:
                f.overrun_count -= 1
                lost += 1
                self.dropped += 1
            else:
                rows.append(data)
            self.sweep += 1
        flags = (capture.FLAG_OVERRUN if lost else 0) | (capture.FLAG_FINAL if final else 0) \
            | (capture.FLAG_STOPPED if stopped else 0)
        if rows:
            ts_conn = np.stack([r[0] for r in rows])
            ts_rel = np.stack([r[1] for r in rows])
            sensor = np.stack([r[2] for r in rows])
            vmid = np.stack([r[3] for r in rows])
        else:
            ts_conn = ts_rel = np.zeros((0, n_routes), dtype=np.uint32)
            sensor = vmid = np.zeros((0, n_routes), dtype=np.uint16)
        chunk_first = first          # the next chunk starts after the dropped sweeps: a visible gap
        boot = dev.boot_id
        if f.reboot_at_chunk is not None and self.seq >= f.reboot_at_chunk:
            boot = (boot ^ 0x5A5A) & 0xFFFFFFFF
        chunk = capture.Chunk(self.session, boot, self.seq, chunk_first, n_routes, flags,
                              self.dropped, dev.config.cpu_hz, ts_conn, ts_rel, sensor, vmid)
        data = capture.encode_chunk(chunk)
        seq = self.seq
        self.seq += 1
        if f.disconnect_at_chunk is not None and seq >= f.disconnect_at_chunk:
            dev.disconnect()
            return
        if f.drop_chunk_seq == seq:
            return
        if f.corrupt_chunk_seq == seq:
            data = data[:40] + bytes([data[40] ^ 0xFF]) + data[41:]
        dev._outbox += data
