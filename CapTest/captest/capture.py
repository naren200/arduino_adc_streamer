"""Continuous multi-channel capture (CapTest firmware capture protocol 1), GUI- and serial-free.

Firmware side (``capcaps``/``capcfg``/``capstart``/``capstop``/``capstat``) streams *chunks*:
bounded binary packets of whole sweeps. One sweep visits every configured route once, ADC
by ADC, in the production-like manual sequence (sensor frame, one Vmid frame per route, two
flush frames at the end of each ADC block so the converter is parked on Vmid)::

    header (36 B)  magic 'CK', version, header bytes, session, boot id, chunk seq,
                   first sweep, n sweeps, n routes, flags, reserved, dropped sweeps, cpu_hz
    records        sweeps x routes x 12 B: ts_conn u32 (frame k+1), ts_rel u32 (frame k+2),
                   sensor word u16 (response of frame k+2), vmid word u16 (response of k+3)
    crc16          CRC-16/CCITT-FALSE over header + records

Frame k is the command frame of the sensor; the mux connects it at CS fall of frame k+1 and
the Vmid command releases it at frame k+2, so ``Tc = ts_rel - ts_conn`` is the connected
time and ``ts_rel`` the sampling time (the convention of ``capfit.analyze_alternate``).
Cycle counters are 32 bit and wrap; the host extends them monotonically.

Nothing here hides a gap: a missing/corrupt chunk, dropped sweeps, a changed session or
boot, a wrong tag or a failed Vmid reset is recorded as a *problem* and the capture is
invalid for calibration.
"""

from __future__ import annotations

import hashlib
import struct
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from . import protocol
from .link import LinkError

CHUNK_MAGIC = b"CK"
CHUNK_VERSION = 1
HEADER_BYTES = 36
RECORD_BYTES = 12
MAX_ROUTES = 64
MAX_SWEEPS_PER_CHUNK = 64
STOP_DRAIN_S = 4.0              # longest wait for the final chunk and the capstop acknowledgement
MERGE_EVERY = 64               # chunks joined into one piece for cheap snapshots

_HEADER = struct.Struct("<2sBBIIIIHHHHII")
assert _HEADER.size == HEADER_BYTES

FLAG_OVERRUN = 1 << 0          # sweeps were dropped before this chunk (ring full)
FLAG_FINAL = 1 << 1            # last chunk of the session
FLAG_TAG_ERRORS = 1 << 2       # the firmware saw a wrong tag in this chunk
FLAG_RESET_BAD = 1 << 3        # the firmware saw a failed Vmid reset
FLAG_STOPPED = 1 << 4          # ended by capstop (otherwise by max sweeps or an error)
FLAG_ERROR = 1 << 5            # transfer error: the session ended abnormally

# Problem kinds
GAP = "GAP"
OVERRUN = "OVERRUN"
CRC = "CRC"
SESSION = "SESSION"
TAG = "TAG_ERROR"
RESET = "RESET_FAILED"
TIME = "TIME_ORDER"
LINK = "LINK"
FIRMWARE = "FIRMWARE_ERROR"

RESET_TOLERANCE_V = 0.005      # same tolerance as capfit.ALT_RESET_TOLERANCE_V


class CaptureError(Exception):
    pass


class CaptureUnavailable(CaptureError):
    """The attached firmware does not offer the capture feature."""


@dataclass(frozen=True)
class ScanConfig:
    """What the board visits and how fast. All routes of both arrays by default."""

    routes: Tuple[Tuple[int, int], ...]      # (adc, channel), grouped by ADC in visit order
    gap_ns: int = 200
    first_clock_delay_ns: int = 25
    lag_us: int = 0                          # idle time added after every sweep
    spi_clock_hz: int = 20_000_000
    vmid_frames: int = 1

    def __post_init__(self) -> None:
        if not self.routes or len(self.routes) > MAX_ROUTES:
            raise CaptureError("1..%d routes are needed" % MAX_ROUTES)
        if len(set(self.routes)) != len(self.routes):
            raise CaptureError("duplicate routes")
        seen, previous = set(), None
        for adc, _ in self.routes:               # one contiguous block per ADC
            if adc != previous:
                if adc in seen:
                    raise CaptureError("routes must be grouped by ADC (one block each)")
                seen.add(adc)
                previous = adc
        if self.vmid_frames != 1:
            raise CaptureError("capture protocol 1 resets with exactly one Vmid frame per route")
        if not 0 <= self.lag_us <= 1_000_000:
            raise CaptureError("lag must be 0..1000000 us")

    @classmethod
    def for_routes(cls, routes: Sequence[Tuple[int, int]], **kw) -> "ScanConfig":
        """Routes sorted into one block per ADC, keeping each ADC's given channel order."""
        order: List[int] = []
        for adc, _ in routes:
            if adc not in order:
                order.append(adc)
        grouped = tuple((a, c) for adc in sorted(order) for a, c in routes if a == adc)
        return cls(grouped, **kw)

    def command_lines(self) -> List[str]:
        lines = ["capcfg begin %d %d %d %d*" % (self.gap_ns, self.first_clock_delay_ns,
                                                self.lag_us, self.vmid_frames)]
        lines += ["capcfg route %d %d*" % r for r in self.routes]
        lines.append("capcfg commit*")
        return lines

    def adcs(self) -> List[int]:
        out: List[int] = []
        for adc, _ in self.routes:
            if adc not in out:
                out.append(adc)
        return out

    def signature(self) -> str:
        digest = hashlib.sha256(repr(self.routes).encode()).hexdigest()[:10]
        return ("capture=1;routes=%d:%s;gap_ns=%d;first_clock_delay_ns=%d;lag_us=%d;"
                "spi_clock_hz=%d;vmid_frames=%d"
                % (len(self.routes), digest, self.gap_ns, self.first_clock_delay_ns,
                   self.lag_us, self.spi_clock_hz, self.vmid_frames))

    def to_json(self) -> dict:
        return {"routes": [list(r) for r in self.routes], "gap_ns": self.gap_ns,
                "first_clock_delay_ns": self.first_clock_delay_ns, "lag_us": self.lag_us,
                "spi_clock_hz": self.spi_clock_hz, "vmid_frames": self.vmid_frames,
                "signature": self.signature()}


def schedule_mismatches(manifest_signature: str, config: ScanConfig) -> List[str]:
    """Timing parameters shared with the coefficient fit (``force_export.acquisition_signature``)
    that differ in this capture. The lag, the route set and the long gap legitimately differ."""
    parts = dict(p.split("=", 1) for p in manifest_signature.split(";") if "=" in p)
    compare = {"gap_ns": config.gap_ns, "first_clock_delay_ns": config.first_clock_delay_ns,
               "spi_clock_hz": config.spi_clock_hz, "vmid_frames": config.vmid_frames}
    return ["%s: coefficients fitted at %s, capture uses %s" % (k, parts.get(k), v)
            for k, v in compare.items() if k in parts and str(parts[k]) != str(v)]


# --------------------------------------------------------------------------- chunk codec
@dataclass
class Chunk:
    session: int
    boot_id: int
    seq: int
    first_sweep: int
    n_routes: int
    flags: int
    dropped_total: int
    cpu_hz: int
    ts_conn: np.ndarray          # (n_sweeps, n_routes) uint32
    ts_rel: np.ndarray
    sensor: np.ndarray           # uint16 words
    vmid: np.ndarray

    @property
    def n_sweeps(self) -> int:
        return int(self.ts_conn.shape[0])


def crc16(data: bytes) -> int:
    return protocol.crc16_ccitt_false(data)


def encode_chunk(chunk: Chunk) -> bytes:
    n_sweeps, n_routes = chunk.ts_conn.shape
    header = _HEADER.pack(CHUNK_MAGIC, CHUNK_VERSION, HEADER_BYTES, chunk.session, chunk.boot_id,
                          chunk.seq, chunk.first_sweep, n_sweeps, n_routes, chunk.flags, 0,
                          chunk.dropped_total, chunk.cpu_hz)
    rec = np.empty((n_sweeps, n_routes), dtype=[("a", "<u4"), ("b", "<u4"), ("c", "<u2"), ("d", "<u2")])
    rec["a"], rec["b"], rec["c"], rec["d"] = chunk.ts_conn, chunk.ts_rel, chunk.sensor, chunk.vmid
    payload = header + rec.tobytes()
    return payload + struct.pack("<H", crc16(payload))


def chunk_length_from_header(header: bytes) -> int:
    """Total chunk length from its first HEADER_BYTES bytes."""
    if len(header) < HEADER_BYTES:
        raise protocol.TruncatedPacketError("chunk header incomplete (%d bytes)" % len(header))
    fields = _HEADER.unpack(header[:HEADER_BYTES])
    if fields[0] != CHUNK_MAGIC:
        raise protocol.FormatError("bad chunk magic %r" % fields[0])
    if fields[1] != CHUNK_VERSION or fields[2] != HEADER_BYTES:
        raise protocol.FormatError("unsupported chunk version %d/%d" % (fields[1], fields[2]))
    n_sweeps, n_routes = fields[7], fields[8]
    if not (1 <= n_routes <= MAX_ROUTES and n_sweeps <= MAX_SWEEPS_PER_CHUNK):
        raise protocol.FormatError("implausible chunk size %dx%d" % (n_sweeps, n_routes))
    return HEADER_BYTES + n_sweeps * n_routes * RECORD_BYTES + 2


def decode_chunk(data: bytes) -> Chunk:
    total = chunk_length_from_header(data)
    if len(data) < total:
        raise protocol.TruncatedPacketError("chunk truncated (%d of %d bytes)" % (len(data), total))
    body, stored = data[:total - 2], struct.unpack("<H", data[total - 2:total])[0]
    if crc16(body) != stored:
        raise protocol.CrcError("chunk CRC mismatch")
    (_, _, _, session, boot, seq, first, n_sweeps, n_routes, flags, _, dropped, cpu_hz
     ) = _HEADER.unpack(body[:HEADER_BYTES])
    rec = np.frombuffer(body[HEADER_BYTES:], dtype=[("a", "<u4"), ("b", "<u4"), ("c", "<u2"), ("d", "<u2")])
    shape = (n_sweeps, n_routes)
    return Chunk(session, boot, seq, first, n_routes, flags, dropped, cpu_hz,
                 rec["a"].reshape(shape).astype(np.uint32), rec["b"].reshape(shape).astype(np.uint32),
                 rec["c"].reshape(shape).astype(np.uint16), rec["d"].reshape(shape).astype(np.uint16))


# ----------------------------------------------------------------------- assembler
@dataclass
class Problem:
    kind: str
    detail: str
    sweep: Optional[int] = None

    def __str__(self) -> str:
        return "%s: %s" % (self.kind, self.detail)


@dataclass
class CaptureResult:
    """Everything the calibration needs from one capture."""

    config: ScanConfig
    session: int
    boot_id: int
    cpu_hz: int
    sweeps: int
    duration_s: float
    problems: List[Problem]
    samples: Dict[Tuple[int, int], dict]         # kept routes: t, volts, tc, valid, vmid_volts
    vd_by_adc: Dict[int, dict]                   # mean/std/min/max of the Vmid readings (V)
    tag_errors: Dict[Tuple[int, int], int]
    reset_failures: Dict[Tuple[int, int], int]
    revisit_us: Dict[Tuple[int, int], float]     # median same-sensor revisit interval
    tc_us: Dict[Tuple[int, int], float]          # median connected time
    raw_file: str = ""
    raw_sha256: str = ""

    @property
    def valid(self) -> bool:
        return not self.problems

    def reasons(self) -> List[str]:
        return [str(p) for p in self.problems]


class CaptureAssembler:
    """Validates chunks in order and accumulates the kept routes' samples."""

    def __init__(self, config: ScanConfig, span_volts: float = 2.5, bits: int = 12,
                 keep_routes: Optional[Sequence[Tuple[int, int]]] = None, expected_boot: Optional[int] = None):
        self.config = config
        self.span = span_volts
        self.bits = bits
        self.keep = tuple(keep_routes) if keep_routes is not None else config.routes
        missing = [r for r in self.keep if r not in config.routes]
        if missing:
            raise CaptureError("kept routes %s are not in the scan" % missing)
        self._cols = [config.routes.index(r) for r in self.keep]
        self._adc_cols: Dict[int, List[int]] = {}
        for i, (adc, _) in enumerate(config.routes):
            self._adc_cols.setdefault(adc, []).append(i)
        self.problems: List[Problem] = []
        self.session: Optional[int] = None
        self.boot_id: Optional[int] = expected_boot
        self.cpu_hz = 0
        self._next_seq = 0
        self._next_sweep = 0
        self._last_dropped = 0
        self._prev64: Optional[int] = None
        self._origin: Optional[int] = None
        self.finished = False
        self.sweeps = 0
        self._t: List[np.ndarray] = []
        self._tc: List[np.ndarray] = []
        self._v: List[np.ndarray] = []
        self._ok: List[np.ndarray] = []
        self._vm: List[np.ndarray] = []
        self._vmid_all: Dict[int, List[np.ndarray]] = {a: [] for a in self._adc_cols}
        self._all_t_rel: List[np.ndarray] = []      # (n_sweeps, n_routes) seconds, for revisit stats
        self._tc_all: List[np.ndarray] = []
        self._vd_ref: Dict[int, float] = {}
        self.tag_errors = {r: 0 for r in config.routes}
        self.reset_failures = {r: 0 for r in config.routes}
        self.latest_time_s = 0.0
        self._since_merge = 0
        self.lock = threading.RLock()     # a worker feeds chunks while the GUI takes snapshots

    # ------------------------------------------------------------------ helpers
    def _problem(self, kind: str, detail: str, sweep: Optional[int] = None) -> None:
        self.problems.append(Problem(kind, detail, sweep))

    def _unwrap(self, ts: np.ndarray) -> np.ndarray:
        """32-bit cycle counters (flattened in time order) -> monotonic int64 cycles."""
        flat = ts.astype(np.int64).reshape(-1)
        if self._prev64 is None:
            diffs = np.diff(flat) & 0xFFFFFFFF
            start = int(flat[0])
            out = np.concatenate([[start], start + np.cumsum(diffs)])
        else:
            prev = self._prev64 & 0xFFFFFFFF
            first = (int(flat[0]) - prev) & 0xFFFFFFFF
            diffs = np.concatenate([[first], np.diff(flat) & 0xFFFFFFFF])
            out = self._prev64 + np.cumsum(diffs)
        if np.any(np.diff(out) > 0x7FFFFFFF) or (self._prev64 is not None and out[0] - self._prev64 > 0x7FFFFFFF):
            self._problem(TIME, "cycle counter went backwards or jumped by more than half a wrap")
        self._prev64 = int(out[-1])
        return out

    # --------------------------------------------------------------------- feed
    def feed_bytes(self, data: bytes) -> Optional[Chunk]:
        try:
            chunk = decode_chunk(data)
        except protocol.CrcError:
            self._problem(CRC, "chunk %d failed its CRC and was discarded" % self._next_seq)
            self._next_seq += 1          # the chunk existed; its sweeps are now a gap
            return None
        except protocol.ProtocolError as exc:
            self._problem(CRC, "undecodable chunk: %s" % exc)
            return None
        self.feed(chunk)
        return chunk

    def feed(self, chunk: Chunk) -> None:
        with self.lock:
            self._feed(chunk)

    def _feed(self, chunk: Chunk) -> None:
        if chunk.n_routes != len(self.config.routes):
            self._problem(FIRMWARE, "chunk has %d routes, the scan has %d"
                          % (chunk.n_routes, len(self.config.routes)))
            return
        if self.session is None:
            self.session = chunk.session
            self.cpu_hz = chunk.cpu_hz
            if self.boot_id is None:
                self.boot_id = chunk.boot_id
        if chunk.session != self.session:
            self._problem(SESSION, "capture session changed (restart): 0x%X -> 0x%X"
                          % (self.session, chunk.session))
            return
        if chunk.boot_id != self.boot_id:
            self._problem(SESSION, "board rebooted during the capture (boot 0x%X -> 0x%X)"
                          % (self.boot_id, chunk.boot_id))
            return
        if chunk.seq != self._next_seq:
            self._problem(GAP, "chunk %d arrived where %d was expected (lost chunks)"
                          % (chunk.seq, self._next_seq), chunk.first_sweep)
        self._next_seq = chunk.seq + 1
        if chunk.first_sweep != self._next_sweep:
            self._problem(GAP, "sweep %d follows sweep %d: %d sweeps missing"
                          % (chunk.first_sweep, self._next_sweep - 1,
                             chunk.first_sweep - self._next_sweep), chunk.first_sweep)
        self._next_sweep = chunk.first_sweep + chunk.n_sweeps
        if chunk.flags & FLAG_OVERRUN or chunk.dropped_total > self._last_dropped:
            self._problem(OVERRUN, "the board dropped data: its sweep buffer filled up because the "
                          "host could not read fast enough", chunk.first_sweep)
        self._last_dropped = chunk.dropped_total
        if chunk.flags & FLAG_ERROR:
            self._problem(FIRMWARE, "the board reported a transfer error", chunk.first_sweep)
        if chunk.flags & FLAG_FINAL:
            self.finished = True
        if chunk.n_sweeps == 0:
            return
        self._add_records(chunk)

    def _add_records(self, c: Chunk) -> None:
        n_sw, n_rt = c.ts_conn.shape
        order = np.stack([c.ts_conn, c.ts_rel], axis=-1)            # (sweep, route, 2) time order
        cycles = self._unwrap(order).reshape(n_sw, n_rt, 2)
        if self._origin is None:
            self._origin = int(cycles[0, 0, 0])
        conn = (cycles[:, :, 0] - self._origin) / float(self.cpu_hz)
        rel = (cycles[:, :, 1] - self._origin) / float(self.cpu_hz)
        tc = rel - conn
        sensor_code = (c.sensor & 0x0FFF).astype(np.float64)
        sensor_tag = (c.sensor >> 12).astype(np.int64)
        vmid_code = (c.vmid & 0x0FFF).astype(np.float64)
        vmid_tag = (c.vmid >> 12).astype(np.int64)
        lsb = self.span / float(1 << self.bits)
        volts = sensor_code * lsb
        vmid_v = vmid_code * lsb
        channels = np.array([ch for _, ch in self.config.routes])
        tag_ok = (sensor_tag == channels[None, :]) & (vmid_tag == protocol.VMID_CHANNEL)
        # Vmid reference per ADC: its median over the first sweeps seen (self-referenced), so
        # a reset that did not return the node to Vmid shows as a deviation.
        reset_ok = np.ones_like(tag_ok)
        for adc, cols in self._adc_cols.items():
            block = vmid_v[:, cols]
            if adc not in self._vd_ref:
                self._vd_ref[adc] = float(np.median(block))
            reset_ok[:, cols] = np.abs(block - self._vd_ref[adc]) <= RESET_TOLERANCE_V
            self._vmid_all[adc].append(block.reshape(-1))
        for i, route in enumerate(self.config.routes):
            bad_tags = int((~tag_ok[:, i]).sum())
            bad_reset = int((~reset_ok[:, i]).sum())
            if bad_tags:
                self.tag_errors[route] += bad_tags
            if bad_reset:
                self.reset_failures[route] += bad_reset
        if (~tag_ok).any():
            self._problem(TAG, "%d wrong channel tags in sweeps %d-%d"
                          % (int((~tag_ok).sum()), c.first_sweep, c.first_sweep + n_sw - 1),
                          c.first_sweep)
        if (~reset_ok).any():
            self._problem(RESET, "%d visits where the Vmid reset did not return the node to Vmid"
                          % int((~reset_ok).sum()), c.first_sweep)
        valid = tag_ok & reset_ok
        cols = self._cols
        self._t.append(rel[:, cols].T.copy())            # (kept route, sweeps)
        self._tc.append(tc[:, cols].T.copy())
        self._v.append(volts[:, cols].T.copy())
        self._ok.append(valid[:, cols].T.copy())
        self._vm.append(vmid_v[:, cols].T.copy())
        self._all_t_rel.append(rel)
        self._tc_all.append(tc)
        self.sweeps += n_sw
        self.latest_time_s = float(rel[-1, -1])
        self._since_merge += 1
        if self._since_merge >= MERGE_EVERY:
            self._merge_tail()

    def _merge_tail(self) -> None:
        """Chunks hold only a few sweeps each; joining the newest pieces keeps the piece lists
        short, so a snapshot never has to concatenate thousands of tiny arrays."""
        n, self._since_merge = self._since_merge, 0
        if n < 2:
            return
        for pieces, axis in ((self._t, 1), (self._tc, 1), (self._v, 1), (self._ok, 1), (self._vm, 1),
                             (self._all_t_rel, 0), (self._tc_all, 0)):
            tail = pieces[-n:]
            pieces[-n:] = [np.concatenate(tail, axis=axis)]
        for pieces in self._vmid_all.values():
            tail = pieces[-n:]
            pieces[-n:] = [np.concatenate(tail)]

    # ------------------------------------------------------------------- snapshots
    def live_samples(self) -> Tuple[Dict[Tuple[int, int], dict], int]:
        """The kept routes' samples so far and the sweep count: the cheap snapshot for live
        plots (no per-route statistics). Pieces are merged, so repeated calls stay fast."""
        with self.lock:
            if not self._t:
                return {}, 0
            self._merge_tail()                    # also joins the statistics' pieces
            t, tc, v, ok, vm = (np.concatenate(p, axis=1) for p in
                                (self._t, self._tc, self._v, self._ok, self._vm))
            # keep the joined arrays as the only pieces: the next snapshot joins just the news
            self._t, self._tc, self._v, self._ok, self._vm = [t], [tc], [v], [ok], [vm]
            samples = {ref: {"t": t[i], "volts": v[i], "tc": tc[i], "valid": ok[i],
                             "vmid_volts": vm[i]} for i, ref in enumerate(self.keep)}
            return samples, self.sweeps

    # ------------------------------------------------------------------- result
    def result(self, raw_file: str = "", raw_sha256: str = "", *, include_timing=True) -> CaptureResult:
        with self.lock:
            return self._result(raw_file, raw_sha256, include_timing=include_timing)

    def _result(self, raw_file: str = "", raw_sha256: str = "", *, include_timing=True) -> CaptureResult:
        samples: Dict[Tuple[int, int], dict] = {}
        t = None
        if self._t:
            t = np.concatenate(self._t, axis=1)
            tc = np.concatenate(self._tc, axis=1)
            v = np.concatenate(self._v, axis=1)
            ok = np.concatenate(self._ok, axis=1)
            vm = np.concatenate(self._vm, axis=1)
            for i, ref in enumerate(self.keep):
                samples[ref] = {"t": t[i], "volts": v[i], "tc": tc[i], "valid": ok[i],
                                "vmid_volts": vm[i]}
        vd = {}
        for adc, parts in self._vmid_all.items():
            if parts:
                values = np.concatenate(parts)
                vd[adc] = {"mean_v": float(values.mean()), "std_v": float(values.std()),
                           "min_v": float(values.min()), "max_v": float(values.max()),
                           "reference_v": self._vd_ref.get(adc)}
        revisit, tcs = {}, {}
        if include_timing and self._all_t_rel:
            rel = np.concatenate(self._all_t_rel, axis=0)
            tc_all = np.concatenate(self._tc_all, axis=0)
            for i, route in enumerate(self.config.routes):
                if len(rel) > 1:
                    revisit[route] = float(np.median(np.diff(rel[:, i])) * 1e6)
                tcs[route] = float(np.median(tc_all[:, i]) * 1e6)
        duration = float(t.max() - t.min()) if self._t else 0.0
        return CaptureResult(self.config, self.session or 0, self.boot_id or 0, self.cpu_hz,
                             self.sweeps, duration, list(self.problems), samples, vd,
                             dict(self.tag_errors), dict(self.reset_failures), revisit, tcs,
                             raw_file, raw_sha256)


# ------------------------------------------------------------------------- recorder
class ChunkRecorder:
    """Disk-backed raw recording: every received chunk (as sent, with its CRC) is appended to
    one file as it arrives, so long holds never depend on memory and the file replays through
    the same decoder. A short header line identifies the file."""

    MAGIC = b"CAPTEST-CAPTURE-1\n"

    def __init__(self, path, config: ScanConfig, meta: Optional[dict] = None):
        import json

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "xb")
        head = json.dumps({"config": config.to_json(), "meta": meta or {}}).encode("utf-8")
        self._handle.write(self.MAGIC + struct.pack("<I", len(head)) + head)
        self.chunks = 0
        self.closed = False

    def write(self, chunk_bytes: bytes) -> None:
        self._handle.write(chunk_bytes)
        self.chunks += 1

    def close(self) -> str:
        if not self.closed:
            self._handle.flush()
            self._handle.close()
            self.closed = True
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def snapshot_hash(self):
        """Identify an immutable recorded prefix while the same acquisition continues."""
        if not self.closed:
            self._handle.flush()
        data = self.path.read_bytes()
        return len(data), hashlib.sha256(data).hexdigest()


def replay_file(path, keep_routes=None, span_volts: float = 2.5) -> CaptureResult:
    """Rebuild a CaptureResult from a recorded file (offline replay)."""
    import json

    data = Path(path).read_bytes()
    if not data.startswith(ChunkRecorder.MAGIC):
        raise CaptureError("not a CapTest capture file")
    pos = len(ChunkRecorder.MAGIC)
    (n,) = struct.unpack("<I", data[pos:pos + 4])
    head = json.loads(data[pos + 4:pos + 4 + n].decode("utf-8"))
    cfg = head["config"]
    config = ScanConfig(tuple(tuple(r) for r in cfg["routes"]), cfg["gap_ns"],
                        cfg["first_clock_delay_ns"], cfg["lag_us"], cfg["spi_clock_hz"],
                        cfg["vmid_frames"])
    asm = CaptureAssembler(config, span_volts, keep_routes=keep_routes)
    pos += 4 + n
    while pos < len(data):
        try:
            total = chunk_length_from_header(data[pos:pos + HEADER_BYTES])
        except protocol.ProtocolError as exc:
            asm._problem(CRC, "recording damaged at byte %d: %s" % (pos, exc))
            break
        asm.feed_bytes(data[pos:pos + total])
        pos += total
    if not asm.finished:
        asm._problem(GAP, "the recording ended without its final chunk")
    return asm.result(str(path), hashlib.sha256(data).hexdigest())


# ---------------------------------------------------------------------------- session
class CaptureSession:
    """Runs one capture on a client: configure, start, read chunks, stop, always park.

    ``keep_routes`` (the analysed package) are kept in memory; every chunk is recorded to disk
    when ``record_path`` is given. The caller keeps reading with :meth:`poll` so the board's
    buffer never fills; plotting must never delay :meth:`poll`.
    """

    def __init__(self, client, config: ScanConfig, keep_routes: Sequence[Tuple[int, int]],
                 record_path=None, span_volts: float = 2.5, meta: Optional[dict] = None,
                 poll_timeout: float = 0.2, max_chunks_per_poll: int = 32):
        self.client = client
        self.max_chunks_per_poll = max_chunks_per_poll
        self.config = config
        self.assembler = CaptureAssembler(config, span_volts, keep_routes=keep_routes)
        self.recorder = ChunkRecorder(record_path, config, meta) if record_path else None
        self.poll_timeout = poll_timeout
        self.running = False
        self.stopping = False
        self.closed = False

    def start(self, max_sweeps: int = 0) -> None:
        if self.closed:
            raise CaptureError("session already finished")
        self.client.resync()                      # nothing stale from an earlier capture
        self.client.capture_require()
        self.client.capture_configure(self.config)
        self.client.capture_start(max_sweeps)
        self.running = True

    @property
    def device_time_s(self) -> float:
        """Device time of the latest sweep received (basis of every mark)."""
        return self.assembler.latest_time_s

    def poll(self) -> int:
        """Read what has arrived; returns the number of chunks processed."""
        if not self.running:
            return 0
        n = 0
        try:
            for kind, payload in self.client.capture_items(self.poll_timeout):
                if kind == "chunk":
                    if self.recorder is not None:
                        self.recorder.write(payload)
                    self.assembler.feed_bytes(payload)
                    n += 1
                    if n >= self.max_chunks_per_poll:
                        break                       # keep each call short; the caller polls again
                elif kind == "ack":
                    self.running = False            # the device finished (max sweeps / stop)
                    break
        except (LinkError, protocol.ProtocolError) as exc:
            self.assembler._problem(LINK, "link failed during capture: %s" % exc)
            self.running = False
        if self.assembler.finished:
            self.running = False
        return n

    def stop(self) -> CaptureResult:
        """Stop the board, drain what it still sends, close the recording, park the ADCs."""
        try:
            if self.running:
                try:
                    self.client.capture_stop()
                    self.stopping = True
                    acked = False
                    deadline = time.monotonic() + STOP_DRAIN_S
                    while time.monotonic() < deadline and not acked:
                        for kind, payload in self.client.capture_items(self.poll_timeout):
                            if kind == "chunk":
                                if self.recorder is not None:
                                    self.recorder.write(payload)
                                self.assembler.feed_bytes(payload)
                            elif kind == "ack":
                                acked = True
                                break
                            if time.monotonic() > deadline:
                                break
                    if not acked:
                        self.assembler._problem(LINK, "the board did not acknowledge capstop")
                except (LinkError, protocol.ProtocolError) as exc:
                    self.assembler._problem(LINK, "link failed while stopping: %s" % exc)
            if not self.assembler.finished and not any(p.kind in (LINK, SESSION) for p in self.assembler.problems):
                self.assembler._problem(GAP, "the capture ended without its final chunk")
        finally:
            self.running = False
            sha = self.recorder.close() if self.recorder is not None else ""
            self.closed = True
            try:
                self.client.resync()                 # abort (ADCs parked on Vmid), drain, check idle
            except (LinkError, protocol.ProtocolError):
                pass
        return self.assembler.result(str(self.recorder.path) if self.recorder else "", sha)

    def cancel(self) -> CaptureResult:
        """Stop and discard: the result carries a CANCELLED problem."""
        result = self.stop()
        result.problems.append(Problem(LINK, "capture cancelled by the user"))
        return result
