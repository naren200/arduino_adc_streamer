"""Automatic and manual measurement sessions (GUI-free).

A MeasurementSession owns one client, the settings, the per-channel history and
the raw bursts. The CLI and the GUI both drive it; progress and stop requests
use plain callables so a Qt worker thread can wrap it.
"""

from __future__ import annotations

import base64
import dataclasses
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
from .settings import Channel, Settings, all_channels, settings_dir


@dataclass
class ChannelResult:
    adc: int
    channel: int
    aggregate: capfit.Aggregate
    measurements: List[capfit.Measurement]
    r_bias: float
    bursts: int
    since_previous_s: Optional[float] = None    # before this measurement's first burst
    fresh: Optional[bool] = False                # first connection since power-up; None = no record for this boot
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
    def used(self) -> List[capfit.Measurement]:
        """The bursts the means were computed from: usable, and of the one method averaged."""
        method = self.aggregate.method if self.aggregate else None
        return [m for m in self.measurements
                if m.ok and np.isfinite(m.tau) and (method is None or m.c_total_method == method)]

    @property
    def fit_used(self) -> str:
        """Fit model of the bursts that were used ('1exp', '2exp' or 'mixed')."""
        models = {m.fit_model_used for m in self.used}
        return models.pop() if len(models) == 1 else ("mixed" if models else "-")

    @property
    def c_total_method(self) -> str:
        """How C_total was obtained (see capfit.C_TOTAL_*), '-' when no burst was usable."""
        methods = {m.c_total_method for m in self.used}
        return methods.pop() if len(methods) == 1 else ("mixed" if methods else "-")

    @property
    def step_valid(self) -> bool:
        """False when V0 and the step ratio of the used bursts have no valid basis."""
        return all(m.step_valid for m in self.used)

    def row(self, tolerance: float = 0.01) -> Dict[str, object]:
        m, s = self.aggregate.mean, self.aggregate.std
        pf = 1e12
        return {
            **self.identity, "tau_us": m["tau"] * 1e6,
            "c_total_pf": m["c_total"] * pf, "c_total_std_pf": s["c_total"] * pf,
            "c_total_method": self.c_total_method,
            "c_total_tol_pf": m["c_total"] * pf * tolerance if np.isfinite(m["c_total"]) else float("nan"),
            "ratio": m["ratio"], "ratio_std": s["ratio"], "c_d_pf": m["c_d"] * pf,
            "c_share_ratio": m["c_share_ratio"], "c_share_pf": m["c_share"] * pf,
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
        # Results without a valid V0 (Vmid between samples, 7953 single connection) are left out.
        group = {c: r for c, r in results.items()
                 if node_group(c) == adc and not r.failed and np.isfinite(r.aggregate.mean["tau"])
                 and r.fit_used != capfit.FIT_ALT and r.step_valid}
        v_d_values = [m.v_d for r in group.values() for m in r.measurements if np.isfinite(m.v_d)]
        v_d = float(np.mean(v_d_values)) if v_d_values else default_v_d
        estimate = capfit.start_voltage_check(
            {c: (r.aggregate.mean["v0"], r.aggregate.mean["tau"]) for c, r in group.items()}, v_d)
        for channel, result in group.items():
            value = estimate[channel]
            high = bool(np.isfinite(value) and abs(value) > capfit.VS0_THRESHOLD_V)
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
    fresh: Optional[bool]
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
                   None if data.get("fresh") is None and "fresh" in data else bool(data.get("fresh")),
                   data.get("wall_time", 0.0), ref)


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
                 simulated: bool = False, driver: Optional[BoardDriver] = None,
                 state_file: Optional[Path] = None):
        self.client = client
        self.settings = settings
        self.clock = clock
        self.simulated = simulated
        self.driver: BoardDriver = driver or load_driver(settings.board_id)
        self.boot_id: Optional[int] = None
        self.last_connect: Dict[Channel, float] = {}       # clock time of last connection
        self.connected_since_boot: set = set()             # channels no longer "fresh"
        # Whether the whole connection history of the current boot is known. A simulated device
        # is new; a real board may have been used before this session, unless the record kept
        # from an earlier session covers this boot or the app saw it reboot.
        self.boot_known = simulated
        # Connections are kept per boot id on disk so a second session on the same boot does not
        # call the sensors fresh. Simulated sessions keep nothing unless a file is given.
        self.state_file: Optional[Path] = (
            Path(state_file) if state_file is not None
            else None if simulated else settings_dir() / ("%s_session.json" % settings.board_id))
        self.results: Dict[Channel, ChannelResult] = {}
        self.raw: List[RawBurst] = []
        self.latest: Optional[capfit.Measurement] = None
        # Callbacks (set by the CLI/GUI).
        self.on_log: Callable[[str], None] = lambda text: None
        self.on_warning: Callable[[str], None] = lambda text: None
        self.on_result: Callable[[ChannelResult], None] = lambda result: None
        self.on_burst: Callable[[capfit.Measurement], None] = lambda m: None
        self.on_progress: Callable[[Progress], None] = lambda p: None
        self.on_vinf: Callable[[capfit.VinfReading], None] = lambda reading: None
        self.vinf_readings: Dict[Channel, capfit.VinfReading] = {}
        self.should_stop: Callable[[], bool] = lambda: False

    # --------------------------------------------------------------- boot state
    def refresh_boot(self) -> protocol.DeviceStatus:
        """Read capstatus; a new boot id (power cycle) resets freshness tracking."""
        status = self.client.status()
        if self.boot_id is None:
            self._load_boot_record(status.boot_id)
        elif status.boot_id != self.boot_id:
            self.on_log("Board rebooted (boot id 0x%X -> 0x%X): sensors are fresh again"
                        % (self.boot_id, status.boot_id))
            self.last_connect.clear()
            self.connected_since_boot.clear()
            self.boot_known = True            # seen from the start of the new boot
            self._save_boot_record(status.boot_id)
        self.boot_id = status.boot_id
        return status

    def _load_boot_record(self, boot_id: int) -> None:
        """Channels connected on this boot by an earlier session, if there is a record of it."""
        if self.state_file is None:
            return
        try:
            record = json.loads(self.state_file.read_text(encoding="utf-8"))
            if int(record["boot_id"]) != boot_id:
                return
            self.connected_since_boot |= {tuple(c) for c in record["connected"]}
            self.boot_known = self.boot_known or bool(record.get("complete"))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save_boot_record(self, boot_id: Optional[int]) -> None:
        if self.state_file is None or boot_id is None:
            return
        record = {"boot_id": boot_id, "complete": self.boot_known,
                  "connected": sorted(list(c) for c in self.connected_since_boot)}
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(record), encoding="utf-8")
        except OSError:
            pass                              # history is a convenience; never stop a measurement for it

    def is_fresh(self, channel: Channel) -> Optional[bool]:
        """True: not connected since the board booted. False: connected. None: the app has no
        record of this boot (the board may have been used before), so it cannot say."""
        if channel in self.connected_since_boot:
            return False
        return True if self.boot_known else None

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
        self._save_boot_record(self.boot_id)

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
        self.refresh_boot()                    # freshness needs the boot id (and its saved record)
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

    # ------------------------------------------------------------------ Vinf mode
    def run_vinf(self, channels: Sequence[Channel], bursts: Optional[int] = None,
                 park_when_done: bool = True) -> List[capfit.VinfReading]:
        """Settling voltage (Vinf mode), channel by channel, three ways.

        Per channel and repeat: (1) a Vmid-between-samples burst, whose fit gives the calculated
        Vinf; (2) a long burst with the sensor alternating with Vmid, one connected time, as
        production runs the channels, whose settled-tail median is the measured Vinf (the
        baseline production uses); (3) a burst with the sensor held connected for about 12 tau,
        with its settled-tail median. It is a separate process from the capacitance run: nothing
        is added to the results table."""
        driver = self.driver
        if not driver.supports_vinf:
            raise ValueError("%s has no Vinf mode" % driver.name)
        saved = self.settings
        count = bursts or saved.bursts
        total = len(channels) * count * 3
        started = self.clock.now()
        done = 0
        readings: List[capfit.VinfReading] = []
        try:
            self.refresh_boot()
            for channel in (tuple(c) for c in channels):
                self._check_stop()
                alternate: List[capfit.Measurement] = []
                held: List[capfit.VinfReading] = []
                alternating: List[capfit.VinfReading] = []
                for _ in range(count):
                    self._check_stop()
                    self.on_progress(Progress(done, total, self._elapsed(started),
                                              self._remaining(started, done, total), channel,
                                              "calculating Vinf " + driver.display(channel)))
                    try:
                        self.settings = dataclasses.replace(saved, method="alternate")
                        try:
                            alternate.append(self.measure_burst(channel, enforce_discharge=True))
                        finally:
                            self.settings = saved
                        done += 1
                        fitted = alternate[-1].ok and np.isfinite(alternate[-1].c_total)
                        c_total = alternate[-1].c_total * 1e12 if fitted else None
                        c_share = alternate[-1].c_share * 1e12 if fitted else None
                        self.on_progress(Progress(done, total, self._elapsed(started),
                                                  self._remaining(started, done, total), channel,
                                                  "Vinf alternating with Vmid " + driver.display(channel)))
                        alternating.append(self._alternating_burst(channel, c_total, c_share))
                        done += 1
                        self.on_progress(Progress(done, total, self._elapsed(started),
                                                  self._remaining(started, done, total), channel,
                                                  "Vinf held " + driver.display(channel)))
                        held.append(self._hold_burst(channel, c_total))
                    except protocol.ProtocolError as exc:
                        self.on_log("%s Vinf failed: %s" % (driver.display(channel), exc))
                    done += 1
                reading = combine_vinf(channel, saved.r_bias(*channel), driver.display(channel),
                                       alternate, held, alternating)
                self.vinf_readings[channel] = reading
                readings.append(reading)
                self.on_vinf(reading)
        except StopRequested:
            self.on_log("Stop requested")
        finally:
            self.settings = saved
            if park_when_done:
                self.park()
        self.on_progress(Progress(done, total, self._elapsed(started), 0.0, None, "finished"))
        return readings

    def _alternating_burst(self, channel: Channel, c_total_pf: Optional[float],
                           c_share_pf: Optional[float]) -> capfit.VinfReading:
        s, driver = self.settings, self.driver
        connection = driver.single(channel)
        status = self.refresh_boot()
        try:
            outcome = driver.measure_alternating_hold(
                self.client, connection, s, driver.settle_frames(s, channel, c_total_pf, c_share_pf))
        except protocol.ProtocolError:
            self._mark_connected(connection)
            raise
        if outcome.boot_id != status.boot_id:
            self.refresh_boot()
        self._mark_connected(connection)
        return capfit.analyze_vinf_alternating(outcome.decoded, s.r_bias(*channel), s.analysis())

    def _hold_burst(self, channel: Channel, c_total_pf: Optional[float]) -> capfit.VinfReading:
        s, driver = self.settings, self.driver
        connection = driver.single(channel)
        status = self.refresh_boot()
        try:
            outcome = driver.measure_hold(self.client, connection, s,
                                          driver.hold_samples(s, channel, c_total_pf))
        except protocol.ProtocolError:
            self._mark_connected(connection)
            raise
        if outcome.boot_id != status.boot_id:
            self.refresh_boot()
        self._mark_connected(connection)
        return capfit.analyze_vinf(outcome.decoded, s.r_bias(*channel), s.analysis())

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


def _median_stats(values: Sequence[float]):
    """(mean, sd) of per-burst medians; the sd needs two bursts."""
    values = np.array(values, dtype=np.float64)
    return float(values.mean()), (float(values.std(ddof=1)) if len(values) > 1 else float("nan"))


def combine_vinf(ref: Channel, r_bias: float, label: str,
                 alternate: Sequence[capfit.Measurement],
                 held: Sequence[capfit.VinfReading],
                 alternating: Sequence[capfit.VinfReading] = ()) -> capfit.VinfReading:
    """One reading per channel from its repeats: each measured Vinf is the mean of the per-burst
    medians (sd over the bursts), calculated Vinf the mean of the fits'."""
    reading = capfit.VinfReading(tuple(ref), float(r_bias), label=label)
    held_ok = [h for h in held if h.bursts]
    alt_ok = [a for a in alternating if a.alt_bursts]
    flags = {f for r in list(held) + list(alternating) for f in r.flags}
    if held_ok:
        reading.v_held, reading.v_held_sd = _median_stats([h.v_held for h in held_ok])
        reading.held_spread_lsb = float(np.mean([h.held_spread_lsb for h in held_ok]))
        reading.bursts = len(held_ok)
        reading.t, reading.v, reading.tail_start_s = held_ok[-1].t, held_ok[-1].v, held_ok[-1].tail_start_s
    if alt_ok:
        reading.v_alt, reading.v_alt_sd = _median_stats([a.v_alt for a in alt_ok])
        reading.alt_spread_lsb = float(np.mean([a.alt_spread_lsb for a in alt_ok]))
        reading.alt_bursts, reading.alt_cycles = len(alt_ok), alt_ok[-1].alt_cycles
        reading.alt_t, reading.alt_v = alt_ok[-1].alt_t, alt_ok[-1].alt_v
        reading.alt_tail_start_s = alt_ok[-1].alt_tail_start_s
    if not held_ok and not alt_ok:
        flags.add(capfit.FIT_FAILED)
    levels = [r.v_d for r in held_ok + alt_ok if np.isfinite(r.v_d)]
    agg = capfit.aggregate(list(alternate), method=capfit.C_TOTAL_VMID_CYCLES) if alternate else None
    if agg is not None and agg.count:
        reading.v_calc, reading.tau_calc = agg.mean["vinf"], agg.mean["tau"]
        levels += [m.v_d for m in alternate if np.isfinite(m.v_d)]
    elif alternate:
        flags |= {f for m in alternate for f in m.flags
                  if f in (capfit.ALT_SMALL_RANGE, capfit.FIT_FAILED, capfit.FEW_SAMPLES, capfit.INCOMPLETE)}
    reading.v_d = float(np.mean(levels)) if levels else float("nan")
    reading.flags = sorted(flags)
    return reading


def vinf_rows(readings: Sequence[capfit.VinfReading], driver: BoardDriver) -> List[Dict[str, object]]:
    rows = []
    for r in readings:
        rows.append({
            **driver.identity(r.ref), "r_bias_ohm": r.r_bias, "v_d_v": r.v_d,
            "v_alt_v": r.v_alt, "v_alt_sd_mv": r.v_alt_sd * 1e3, "alt_spread_lsb": r.alt_spread_lsb,
            "v_held_v": r.v_held, "v_held_sd_mv": r.v_held_sd * 1e3, "held_spread_lsb": r.held_spread_lsb,
            "v_calc_v": r.v_calc, "tau_calc_us": r.tau_calc * 1e6,
            "alt_minus_vmid_mv": r.offset_alt_mv, "held_minus_vmid_mv": r.offset_held_mv,
            "calc_minus_vmid_mv": r.offset_calc_mv, "alt_minus_calc_mv": r.alt_minus_calc_mv,
            "held_minus_calc_mv": r.held_minus_calc_mv, "alt_bursts": r.alt_bursts,
            "held_bursts": r.bursts, "flags": " ".join(r.flags)})
    return rows


def write_vinf_csv(readings: Sequence[capfit.VinfReading], driver: BoardDriver, path) -> None:
    import csv

    rows = vinf_rows(readings, driver)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        if not rows:
            return
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


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
                     driver: Optional[BoardDriver] = None,
                     method: Optional[str] = None) -> ChannelResult:
    """Analyse the recorded packets of one channel with `settings` (no device needed).

    Bursts of different methods are never averaged together: one method is used (`method`, or
    the preferred one), the others stay in `measurements` and the result is flagged
    MIXED_METHODS."""
    driver = driver or load_driver(settings.board_id)
    channel = tuple(channel)
    measurements = []
    for raw in raws:
        decoded = driver.decode(raw.packet)
        since_ms = None if raw.since_previous_s is None else raw.since_previous_s * 1000.0
        m = driver.analyze(decoded, [raw.ref], settings, {raw.ref: since_ms})[raw.ref]
        m.source = raw
        measurements.append(m)
    agg = capfit.aggregate(measurements, method=method)
    if method is not None:       # the result of one method holds only that method's bursts
        measurements = [m for m in measurements
                        if m.c_total_method == method or not (m.ok and np.isfinite(m.tau))]
    first = raws[0]
    result = ChannelResult(channel[0], channel[-1], agg, measurements, settings.r_bias(*channel),
                           len(measurements), first.since_previous_s, first.fresh,
                           failed=agg.count == 0, ref=channel, identity=driver.identity(channel),
                           label=driver.display(channel), group=driver.group_of(channel),
                           chart_group=driver.chart_group(channel))
    if result.failed:
        result.message = "no usable burst (%s)" % ", ".join(agg.flags)
    elif capfit.MIXED_METHODS in agg.flags:
        others = ", ".join("%d by %s" % (n, name) for name, n in sorted(agg.methods_seen.items())
                           if name != agg.method)
        result.message = "averaged %s only; not averaged: %s" % (agg.method, others)
    return result


def reanalyze(path, settings: Optional[Settings] = None,
              all_methods: bool = False) -> List[ChannelResult]:
    """Offline re-analysis of a saved raw file (optionally with new settings). A channel recorded
    by several methods gives one result of the preferred method (flag MIXED_METHODS), or with
    `all_methods` one result per method."""
    saved, bursts = load_raw(path)
    settings = settings or saved
    if settings.board_id != saved.board_id:
        raise ValueError("raw file is from board %s, settings are for %s"
                         % (saved.board_id, settings.board_id))
    driver = load_driver(settings.board_id)
    grouped: Dict[Channel, List[RawBurst]] = {}
    for raw in bursts:
        grouped.setdefault(raw.ref, []).append(raw)
    results: List[ChannelResult] = []
    for channel, raws in sorted(grouped.items()):
        result = analyze_recorded(channel, raws, settings, driver)
        if all_methods and len(result.aggregate.methods_seen) > 1:
            results.extend(analyze_recorded(channel, raws, settings, driver, method=name)
                           for name in sorted(result.aggregate.methods_seen))
        else:
            results.append(result)
    apply_start_voltage({r.ref: r for r in results}, driver.shared_node, driver.vmid_volts)
    return results
