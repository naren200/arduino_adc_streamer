"""Automatic and manual measurement sessions (GUI-free).

A MeasurementSession owns one client, the settings, the per-channel history and
the raw bursts. The CLI and the GUI both drive it; progress and stop requests
use plain callables so a Qt worker thread can wrap it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from . import capfit, protocol
from .boards import DEFAULT_BOARD_ID, BoardDriver, Connection, load_driver
from .client import CapTestClient
from .link import LinkError
from .settings import Channel, Settings, all_channels


@dataclass
class ChannelResult:
    adc: int
    channel: int
    aggregate: capfit.Aggregate
    measurements: List[capfit.Measurement]
    r_bias: float
    bursts: int
    since_previous_s: Optional[float] = None    # before this measurement's first burst
    fresh: bool = False                          # first connection since power-up
    failed: bool = False
    message: str = ""
    vs0_est_v: float = float("nan")              # estimated sensor start voltage (see start_voltage_check)
    ref: Optional[Channel] = None                # board channel id; default (adc, channel)
    identity: Optional[Dict[str, int]] = None    # leading columns of an exported row
    label: str = ""
    group: int = 0                               # grid group (7953: ADC, ADC124: array)
    chart_group: str = ""                        # bar-chart group label

    def __post_init__(self) -> None:
        if self.ref is None:
            self.ref = (self.adc, self.channel)
        if self.identity is None:
            self.identity = {"adc": self.adc, "channel": self.channel}

    @property
    def flags(self) -> List[str]:
        return self.aggregate.flags if self.aggregate else []

    @property
    def raw_bursts(self) -> List["RawBurst"]:
        """The recorded packets this result was computed from (for re-analysis)."""
        return [m.source for m in self.measurements if m.source is not None]

    @property
    def fit_used(self) -> str:
        """Fit model of the bursts that were used ('1exp', '2exp' or 'mixed')."""
        models = {m.fit_model_used for m in self.measurements if m.ok and np.isfinite(m.tau)}
        return models.pop() if len(models) == 1 else ("mixed" if models else "-")

    def row(self, tolerance: float = 0.01) -> Dict[str, object]:
        m, s = self.aggregate.mean, self.aggregate.std
        pf = 1e12
        return {
            **self.identity, "tau_us": m["tau"] * 1e6,
            "c_total_pf": m["c_total"] * pf, "c_total_std_pf": s["c_total"] * pf,
            "c_total_tol_pf": m["c_total"] * pf * tolerance if np.isfinite(m["c_total"]) else float("nan"),
            "ratio": m["ratio"], "ratio_std": s["ratio"], "c_d_pf": m["c_d"] * pf,
            "c_sensor_pf": m["c_sensor_side"] * pf, "c_pzt_pf": m["c_pzt"] * pf,
            "leak_na": m["leak_na"], "fit_model": self.fit_used,
            "tau_slow_us": m["tau_slow"] * 1e6, "tau_fast_us": m["tau_fast"] * 1e6,
            "fast_fraction": m["fast_fraction"], "fast_pct": m["fast_fraction"] * 100.0,
            "c_total_1exp_pf": m["c_total_1exp"] * pf, "c_total_2exp_pf": m["c_total_2exp"] * pf,
            "c_total_shift_pct": m["c_total_shift_pct"],
            "ratio_1exp": m["ratio_1exp"], "ratio_2exp": m["ratio_2exp"],
            "v0_1exp_v": m["v0_1exp"], "v0_2exp_v": m["v0_2exp"],
            "rms_1exp_lsb": m["rms_1exp"], "rms_2exp_lsb": m["rms_2exp"],
            "vs0_est_v": self.vs0_est_v,
            "v0_v": m["v0"], "vinf_v": m["vinf"], "residual_rms_lsb": m["residual_rms_lsb"],
            "amplitude_lsb": m["amplitude_lsb"], "bursts_used": self.aggregate.count,
            "bursts": self.bursts, "r_bias_ohm": self.r_bias,
            "since_previous_s": self.since_previous_s, "fresh": self.fresh,
            "flags": " ".join(self.flags),
        }


def apply_start_voltage(results: Dict[Channel, "ChannelResult"],
                        node_group: Optional[Callable[[Channel], object]] = None,
                        default_v_d: float = 1.25) -> List[Channel]:
    """Run capfit.start_voltage_check per shared node group (7953: ADC; ADC124: array and
    mux); set vs0_est_v and the VS0_HIGH flag.

    Returns the channels whose estimate or flag changed (so a GUI can refresh them)."""
    node_group = node_group or (lambda c: c[0])
    changed: List[Channel] = []
    for adc in sorted({node_group(c) for c in results}):
        # Vmid-between-samples results do not depend on the start voltage: left out.
        group = {c: r for c, r in results.items()
                 if node_group(c) == adc and not r.failed and np.isfinite(r.aggregate.mean["tau"])
                 and r.fit_used != capfit.FIT_ALT}
        v_d_values = [m.v_d for r in group.values() for m in r.measurements if np.isfinite(m.v_d)]
        v_d = float(np.mean(v_d_values)) if v_d_values else default_v_d
        estimate = capfit.start_voltage_check(
            {c: (r.aggregate.mean["v0"], r.aggregate.mean["tau"]) for c, r in group.items()}, v_d)
        for channel, result in group.items():
            value = estimate[channel]
            high = bool(np.isfinite(value) and value > capfit.VS0_THRESHOLD_V)
            had = capfit.VS0_HIGH in result.aggregate.flags
            old = result.vs0_est_v
            result.vs0_est_v = value
            if high and not had:
                result.aggregate.flags.append(capfit.VS0_HIGH)
            elif had and not high:
                result.aggregate.flags.remove(capfit.VS0_HIGH)
            if high != had or not (old == value or (np.isnan(old) and np.isnan(value))):
                changed.append(channel)
    return changed


@dataclass
class RawBurst:
    packet: bytes
    adc: int
    channel: int
    since_previous_s: Optional[float]
    fresh: bool
    wall_time: float
    ref: Optional[Channel] = None      # board channel id this reading of the packet belongs to

    def __post_init__(self) -> None:
        if self.ref is None:
            self.ref = (self.adc, self.channel)
        self.ref = tuple(self.ref)

    def to_json(self) -> dict:
        data = {"adc": self.adc, "channel": self.channel, "fresh": self.fresh,
                "since_previous_s": self.since_previous_s, "wall_time": self.wall_time,
                "packet_b64": base64.b64encode(self.packet).decode("ascii")}
        return data

    @classmethod
    def from_json(cls, data: dict, packets: Optional[Dict[str, bytes]] = None) -> "RawBurst":
        if "packet_id" in data and packets is not None:
            packet = packets[data["packet_id"]]
        else:
            packet = base64.b64decode(data["packet_b64"])
        ref = tuple(data["ref"]) if "ref" in data else None
        return cls(packet, data["adc"], data["channel"], data.get("since_previous_s"),
                   bool(data.get("fresh")), data.get("wall_time", 0.0), ref)


@dataclass
class Progress:
    done: int
    total: int
    elapsed_s: float
    remaining_s: float
    current: Optional[Channel]
    message: str = ""


# Tolerance for float time arithmetic and sleep granularity when comparing the
# elapsed time with the discharge time.
DISCHARGE_EPSILON_MS = 0.01


class StopRequested(Exception):
    pass


class MeasurementSession:
    def __init__(self, client: CapTestClient, settings: Settings, clock,
                 simulated: bool = False, driver: Optional[BoardDriver] = None):
        self.client = client
        self.settings = settings
        self.clock = clock
        self.simulated = simulated
        self.driver: BoardDriver = driver or load_driver(settings.board_id)
        self.boot_id: Optional[int] = None
        self.last_connect: Dict[Channel, float] = {}       # clock time of last connection
        self.connected_since_boot: set = set()             # channels no longer "fresh"
        self.results: Dict[Channel, ChannelResult] = {}
        self.raw: List[RawBurst] = []
        self.latest: Optional[capfit.Measurement] = None
        # Callbacks (set by the CLI/GUI).
        self.on_log: Callable[[str], None] = lambda text: None
        self.on_warning: Callable[[str], None] = lambda text: None
        self.on_result: Callable[[ChannelResult], None] = lambda result: None
        self.on_burst: Callable[[capfit.Measurement], None] = lambda m: None
        self.on_progress: Callable[[Progress], None] = lambda p: None
        self.should_stop: Callable[[], bool] = lambda: False

    # --------------------------------------------------------------- boot state
    def refresh_boot(self) -> protocol.DeviceStatus:
        """Read capstatus; a new boot id (power cycle) resets freshness tracking."""
        status = self.client.status()
        if self.boot_id is not None and status.boot_id != self.boot_id:
            self.on_log("Board rebooted (boot id 0x%X -> 0x%X): sensors are fresh again"
                        % (self.boot_id, status.boot_id))
            self.last_connect.clear()
            self.connected_since_boot.clear()
        self.boot_id = status.boot_id
        return status

    def is_fresh(self, channel: Channel) -> bool:
        return channel not in self.connected_since_boot

    def since_last_s(self, channel: Channel) -> Optional[float]:
        last = self.last_connect.get(channel)
        return None if last is None else self.clock.now() - last

    # ------------------------------------------------------------ one connection
    def _check_stop(self) -> None:
        if self.should_stop():
            raise StopRequested()

    def _mark_connected(self, connection: Connection) -> None:
        now = self.clock.now()
        for sensor in connection.touches:
            self.last_connect[sensor] = now
            self.connected_since_boot.add(sensor)

    def measure_connection(self, connection: Connection,
                           enforce_discharge: bool) -> Dict[Channel, capfit.Measurement]:
        """One burst. In automatic runs the discharge time is enforced (waited out); in
        manual use it only produces a warning. Every sensor the connection touches
        (not only the ones read) restarts its discharge clock."""
        s, driver = self.settings, self.driver
        since = {c: self.since_last_s(c) for c in connection.reads}

        def too_soon(channel):
            return (since[channel] is not None
                    and since[channel] * 1000.0 < s.discharge_ms - DISCHARGE_EPSILON_MS)

        soon = [c for c in connection.reads if too_soon(c)]
        if soon:
            if enforce_discharge:
                waiting = max(s.discharge_ms / 1000.0 - since[c] for c in soon)
                self.on_log("%s discharging, waiting %.0f ms"
                            % (connection.label or driver.display(soon[0]), waiting * 1000.0))
                self.clock.sleep(waiting)
                since = {c: self.since_last_s(c) for c in connection.reads}
            else:
                for channel in soon:
                    message = ("%s was connected %.0f ms ago (< discharge %.0f ms): the sensor "
                               "may not be discharged, V_s0 assumption suspect"
                               % (driver.display(channel), since[channel] * 1000.0, s.discharge_ms))
                    self.on_warning(message)
                    self.on_log("WARNING: " + message)
        self._check_stop()
        status = self.refresh_boot()
        fresh = {c: self.is_fresh(c) for c in connection.reads}
        try:
            outcome = driver.measure(self.client, connection, s)
        except protocol.ProtocolError:
            # A damaged reply still means the sensors may have been connected.
            self._mark_connected(connection)
            raise
        if outcome.boot_id != status.boot_id:
            self.refresh_boot()
        self._mark_connected(connection)
        since_ms = {c: None if since[c] is None else since[c] * 1000.0 for c in connection.reads}
        measurements = driver.analyze(outcome.decoded, list(connection.reads), s, since_ms)
        wall = time.time()
        for channel in connection.reads:
            raw = RawBurst(outcome.packet, channel[0], channel[-1], since[channel], fresh[channel],
                           wall, channel)
            self.raw.append(raw)
            measurement = measurements[channel]
            measurement.source = raw
            self.latest = measurement
            self.on_burst(measurement)
        return measurements

    def measure_burst(self, channel: Channel, enforce_discharge: bool) -> capfit.Measurement:
        """One burst that reads exactly `channel`."""
        return self.measure_connection(self.driver.single(channel), enforce_discharge)[tuple(channel)]

    def _finish_channel(self, channel: Channel, measurements: List[capfit.Measurement],
                        since_first: Optional[float], fresh: bool) -> ChannelResult:
        driver = self.driver
        agg = capfit.aggregate(measurements)
        result = ChannelResult(channel[0], channel[-1], agg, measurements,
                               self.settings.r_bias(*channel), len(measurements), since_first,
                               fresh, failed=agg.count == 0, ref=tuple(channel),
                               identity=driver.identity(channel), label=driver.display(channel),
                               group=driver.group_of(channel),
                               chart_group=driver.chart_group(channel))
        if result.failed:
            result.message = "no usable burst (%s)" % ", ".join(agg.flags)
        self.results[channel] = result
        for other in self.apply_start_voltage():
            if other != channel:
                self.on_result(self.results[other])      # its estimate or flag changed
        self.on_result(result)
        return result

    def apply_start_voltage(self) -> List[Channel]:
        return apply_start_voltage(self.results, self.driver.shared_node, self.driver.vmid_volts)

    # --------------------------------------------------------------- run modes
    def measure_channel(self, channel: Channel, bursts: Optional[int] = None,
                        enforce_discharge: bool = False) -> ChannelResult:
        """Measure one channel M times (manual 'Measure this channel now')."""
        channel = tuple(channel)
        count = bursts or self.settings.bursts
        since_first = self.since_last_s(channel)
        fresh = self.is_fresh(channel)
        # Repeats of one channel always wait out the discharge time.
        measurements = [self.measure_burst(channel, enforce_discharge=enforce_discharge or index > 0)
                        for index in range(count)]
        return self._finish_channel(channel, measurements, since_first, fresh)

    def run_manual(self, channels: Sequence[Channel], bursts: Optional[int] = None,
                   per_address: Optional[bool] = None) -> List[ChannelResult]:
        connections = self.driver.plan([tuple(c) for c in channels], self.settings, per_address)
        return self._run(connections, bursts, enforce_discharge=False, park_when_done=True)

    def run_auto(self, groups: Optional[Iterable[int]] = None, bursts: Optional[int] = None,
                 per_address: Optional[bool] = None) -> List[ChannelResult]:
        """Measure every valid channel of the selected groups (default: all groups)."""
        channels = self.driver.auto_channels(None if groups is None else list(groups))
        connections = self.driver.plan(channels, self.settings, per_address)
        return self._run(connections, bursts, enforce_discharge=True, park_when_done=True)

    def failed_channels(self) -> List[Channel]:
        return [c for c, r in self.results.items() if r.failed]

    def remeasure_failed(self, bursts: Optional[int] = None) -> List[ChannelResult]:
        connections = self.driver.plan(self.failed_channels(), self.settings, None)
        return self._run(connections, bursts, enforce_discharge=True, park_when_done=True)

    def _run(self, connections: Sequence[Connection], bursts: Optional[int],
             enforce_discharge: bool, park_when_done: bool) -> List[ChannelResult]:
        """Pass-major order: pass 1 over all connections, then pass 2, ... so a
        sensor is not revisited before the other connections were measured."""
        count = bursts or self.settings.bursts
        total = len(connections) * count
        started = self.clock.now()
        channels: List[Channel] = []
        for connection in connections:
            for ref in connection.reads:
                if ref not in channels:
                    channels.append(ref)
        per_channel: Dict[Channel, List[capfit.Measurement]] = {c: [] for c in channels}
        since_first: Dict[Channel, Optional[float]] = {}
        fresh_first: Dict[Channel, bool] = {}
        done = 0
        stopped = False
        try:
            self.refresh_boot()
            for index in range(count):
                for connection in connections:
                    self._check_stop()
                    if index == 0:
                        for ref in connection.reads:
                            since_first[ref] = self.since_last_s(ref)
                            fresh_first[ref] = self.is_fresh(ref)
                    self.on_progress(Progress(done, total, self._elapsed(started),
                                              self._remaining(started, done, total),
                                              connection.reads[0], "measuring " + connection.label))
                    try:
                        measured = self.measure_connection(connection, enforce_discharge)
                        for ref in connection.reads:
                            per_channel[ref].append(measured[ref])
                    except protocol.ProtocolError as exc:
                        # A failing connection is recorded and the run continues.
                        self.on_log("%s failed: %s" % (connection.label, exc))
                        for ref in connection.reads:
                            failed = capfit.Measurement(ref[0], ref[-1], self.settings.r_bias(*ref))
                            failed.ref = tuple(ref)
                            failed.flags.append(capfit.FIT_FAILED)
                            per_channel[ref].append(failed)
                    done += 1
        except StopRequested:
            stopped = True
            self.on_log("Stop requested")
        finally:
            if park_when_done:
                self.park()
        results = []
        for channel in channels:
            if per_channel[channel]:
                results.append(self._finish_channel(
                    channel, per_channel[channel], since_first.get(channel),
                    fresh_first.get(channel, False)))
        self.on_progress(Progress(done, total, self._elapsed(started), 0.0, None,
                                  "stopped" if stopped else "finished"))
        return results

    def _elapsed(self, started: float) -> float:
        return self.clock.now() - started

    def _remaining(self, started: float, done: int, total: int) -> float:
        if done == 0:
            return float("nan")
        elapsed = self._elapsed(started)
        return elapsed / done * (total - done)

    def park(self) -> bool:
        """Leave every converter parked on Vmid (capabort)."""
        try:
            return self.client.abort()
        except (protocol.ProtocolError, LinkError) as exc:
            self.on_log("park failed: %s" % exc)
            return False

    def reanalyze(self, settings: Optional[Settings] = None) -> List[Channel]:
        """Recompute every result from its recorded packets with `settings` (default: the
        session settings), e.g. after switching the fit model. No measurement is repeated.
        Channels that failed before the packet was decoded keep their result. Returns the
        channels that were recomputed."""
        if settings is not None:
            self.settings = settings
        redone: List[Channel] = []
        for channel, result in list(self.results.items()):
            raws = result.raw_bursts
            if not raws:
                continue
            self.results[channel] = analyze_recorded(channel, raws, self.settings, self.driver)
            redone.append(channel)
        self.apply_start_voltage()
        if self.latest is not None and self.latest.source is not None:
            channel = tuple(self.latest.ref)
            if channel in self.results and self.results[channel].measurements:
                self.latest = self.results[channel].measurements[-1]
        return redone

    # ------------------------------------------------------------- other tests
    def vmid_read(self, target: int) -> dict:
        return self.driver.vmid(self.client, target)

    def discharge_check(self, channel: Channel,
                        waits_ms: Optional[Sequence[float]] = None) -> dict:
        """Same channel after increasing waits; V0 versus wait should plateau."""
        channel = tuple(channel)
        if waits_ms is None:
            waits_ms = self.driver.discharge_check["waits_ms"]
        points = []
        # Prime the sensor (connect once) so every point has a known history.
        self.measure_burst(channel, enforce_discharge=False)
        for wait in waits_ms:
            self._check_stop()
            self.clock.sleep(wait / 1000.0)
            m = self.measure_burst(channel, enforce_discharge=False)
            points.append({"wait_ms": wait, "v0": m.v0, "tau": m.tau, "ratio": m.ratio,
                           "v_d": m.v_d, "flags": list(m.flags)})
        self.park()
        v0 = [p["v0"] for p in points]
        reference = None
        if self.driver.discharge_check.get("relative_to_baseline"):
            baselines = [p["v_d"] for p in points if np.isfinite(p["v_d"])]
            reference = float(np.mean(baselines)) if baselines else None
        verdict = capfit.plateau_check([p["wait_ms"] for p in points], v0,
                                       vref=self.settings.vref, reference=reference)
        verdict["points"] = points
        verdict["channel"] = self.driver.key(channel)
        return verdict

    # ---------------------------------------------------------------- persistence
    def save_raw(self, path) -> None:
        write_raw(self.raw, self.settings, path)

    def save_results(self, path) -> None:
        write_results_json(sorted(self.results.values(), key=lambda r: r.ref),
                           self.settings, path)

    def save_csv(self, path) -> None:
        write_results_csv(sorted(self.results.values(), key=lambda r: r.ref),
                          self.settings, path)


def write_raw(bursts: Sequence[RawBurst], settings: Settings, path) -> None:
    """Raw packets for offline re-analysis. TestBoard_7953 files keep the first format
    (`captest-raw-1`, one packet per entry); other boards store each packet once
    (`captest-raw-2`, packets shared by the channels read from them)."""
    if settings.board_id == DEFAULT_BOARD_ID:
        payload = {"format": "captest-raw-1", "settings": json.loads(settings.to_json()),
                   "bursts": [b.to_json() for b in bursts]}
    else:
        packets: Dict[str, str] = {}
        entries = []
        for burst in bursts:
            packet_id = hashlib.sha1(burst.packet).hexdigest()[:16]
            packets.setdefault(packet_id, base64.b64encode(burst.packet).decode("ascii"))
            entry = burst.to_json()
            del entry["packet_b64"]
            entry["packet_id"] = packet_id
            entry["ref"] = list(burst.ref)
            entries.append(entry)
        payload = {"format": "captest-raw-2", "board": settings.board_id,
                   "settings": json.loads(settings.to_json()), "packets": packets,
                   "bursts": entries}
    Path(path).write_text(json.dumps(payload), encoding="utf-8")


def write_results_json(results: Sequence[ChannelResult], settings: Settings, path) -> None:
    rows = [r.row(settings.bias_tolerance) for r in results]
    payload = {"format": "captest-results-1", "settings": json.loads(settings.to_json()),
               "rows": rows}
    if settings.board_id != DEFAULT_BOARD_ID:
        payload["board"] = settings.board_id
    Path(path).write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")


def write_results_csv(results: Sequence[ChannelResult], settings: Settings, path) -> None:
    import csv

    rows = [r.row(settings.bias_tolerance) for r in results]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        if not rows:
            return
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _json_default(value):
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    raise TypeError(type(value))


def load_raw(path) -> Tuple[Settings, List[RawBurst]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    fmt = data.get("format")
    if fmt == "captest-raw-1":
        return Settings.from_dict(data["settings"]), [RawBurst.from_json(b) for b in data["bursts"]]
    if fmt == "captest-raw-2":
        packets = {k: base64.b64decode(v) for k, v in data["packets"].items()}
        settings = Settings.from_dict(dict(data["settings"], board_id=data["board"]))
        return settings, [RawBurst.from_json(b, packets) for b in data["bursts"]]
    raise ValueError("not a captest raw file")


def analyze_recorded(channel: Channel, raws: Sequence["RawBurst"], settings: Settings,
                     driver: Optional[BoardDriver] = None) -> ChannelResult:
    """Analyse the recorded packets of one channel with `settings` (no device needed)."""
    driver = driver or load_driver(settings.board_id)
    channel = tuple(channel)
    measurements = []
    for raw in raws:
        decoded = driver.decode(raw.packet)
        since_ms = None if raw.since_previous_s is None else raw.since_previous_s * 1000.0
        m = driver.analyze(decoded, [raw.ref], settings, {raw.ref: since_ms})[raw.ref]
        m.source = raw
        measurements.append(m)
    agg = capfit.aggregate(measurements)
    first = raws[0]
    result = ChannelResult(channel[0], channel[-1], agg, measurements, settings.r_bias(*channel),
                           len(measurements), first.since_previous_s, first.fresh,
                           failed=agg.count == 0, ref=channel, identity=driver.identity(channel),
                           label=driver.display(channel), group=driver.group_of(channel),
                           chart_group=driver.chart_group(channel))
    if result.failed:
        result.message = "no usable burst (%s)" % ", ".join(agg.flags)
    return result


def reanalyze(path, settings: Optional[Settings] = None) -> List[ChannelResult]:
    """Offline re-analysis of a saved raw file (optionally with new settings)."""
    saved, bursts = load_raw(path)
    settings = settings or saved
    if settings.board_id != saved.board_id:
        raise ValueError("raw file is from board %s, settings are for %s"
                         % (saved.board_id, settings.board_id))
    driver = load_driver(settings.board_id)
    grouped: Dict[Channel, List[RawBurst]] = {}
    for raw in bursts:
        grouped.setdefault(raw.ref, []).append(raw)
    results = [analyze_recorded(channel, raws, settings, driver)
               for channel, raws in sorted(grouped.items())]
    apply_start_voltage({r.ref: r for r in results}, driver.shared_node, driver.vmid_volts)
    return results
