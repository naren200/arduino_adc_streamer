"""Command line front end: python -m captest [options]."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional, Sequence

from . import runner
from .boards import DEFAULT_BOARD_ID, get_registry
from .client import CapTestClient
from .link import RealClock, SerialLink, VirtualClock
from .settings import Settings, resolve_output
from .sim import SimConfig, SimLink, SimulatedDevice


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="captest",
        description="Capacitance test of the TestBoard family (automatic or manual channels).")
    p.add_argument("--gui", action="store_true", help="open the PyQt6 application (default without other options)")
    p.add_argument("--board", help="board id from boards.json (default: the first entry, or the "
                                   "board of --settings); see --list-boards")
    p.add_argument("--list-boards", action="store_true", help="list the boards of boards.json and exit")
    p.add_argument("--port", help="serial port of the board, e.g. COM3")
    p.add_argument("--simulate", action="store_true", help="use the emulated device (no hardware)")
    p.add_argument("--sim-realtime", action="store_true",
                   help="simulation waits real time for discharge (default: virtual clock)")
    p.add_argument("--sim-seed", type=int, default=None,
                   help="seed of the emulated device (default: the board's)")
    p.add_argument("--auto", action="store_true", help="measure all valid channels of the selected groups")
    p.add_argument("--adc", "--array", dest="groups", default=None, metavar="LIST",
                   help="groups for --auto: ADCs of the TestBoard_7953 (--adc 1,2) or arrays of the "
                        "TestBoard_ADC124 (--array 1,2); default all")
    p.add_argument("--channels", help="manual selection in the board's syntax, e.g. 2:0,2:5,4:1-3 "
                                      "(7953) or 1:1:0,1:4:6,2:2:0 (ADC124)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--per-address", dest="per_address", action="store_true", default=None,
                      help="ADC124: one burst per address reads every mux there (default)")
    mode.add_argument("--per-channel", dest="per_address", action="store_false",
                      help="ADC124: one burst per channel (the others are connected but not read)")
    p.add_argument("--bursts", type=int, help="bursts per channel M (averaged)")
    p.add_argument("--discharge-ms", type=float, help="discharge wait between connections of a channel")
    p.add_argument("--samples", type=int, help="samples per burst (per channel on the ADC124; default: automatic)")
    p.add_argument("--fit", choices=("1exp", "2exp"),
                   help="fit model: one exponential or two (default 2exp; falls back to 1exp when the "
                        "second one is not needed)")
    p.add_argument("--method", choices=("alternate", "burst"),
                   help="TestBoard_7953: alternate = Vmid between samples (default, firmware 1.3.0), "
                        "burst = one connection per burst")
    p.add_argument("--settings", help="settings JSON to load")
    p.add_argument("--save-settings", help="write the effective settings JSON")
    p.add_argument("--out", help="results JSON path")
    p.add_argument("--csv", help="results CSV path")
    p.add_argument("--raw-out", help="save raw bursts (for offline re-analysis)")
    p.add_argument("--status", action="store_true", help="identify the firmware and print capstatus")
    p.add_argument("--detect", action="store_true", help="identify the attached firmware and the board it belongs to")
    p.add_argument("--vmid", type=int, metavar="TARGET",
                   help="read the Vmid reading of an ADC (7953) or an array (ADC124)")
    p.add_argument("--discharge-check", metavar="CHANNEL", help="same channel after increasing waits")
    p.add_argument("--reanalyze", metavar="RAW.json", help="re-analyse saved raw bursts offline")
    return p


def _table(results: Sequence[runner.ChannelResult], tolerance: float) -> str:
    lines = ["%-8s %-5s %9s %9s %9s %7s %8s %9s %8s %7s %6s %s" % (
        "channel", "fit", "tau[us]", "tauF[us]", "Ctot[pF]", "ratio", "CD[pF]", "Cside[pF]", "res[LSB]",
        "Vs0[V]", "M", "flags")]
    for r in results:
        row = r.row(tolerance)
        key = ":".join(str(x) for x in r.ref)
        lines.append("%-8s %-5s %9.2f %9.2f %9.2f %7.3f %8.2f %9.2f %8.2f %7.3f %6d %s" % (
            key, row["fit_model"], row["tau_us"], row["tau_fast_us"], row["c_total_pf"],
            row["ratio"], row["c_d_pf"], row["c_sensor_pf"], row["residual_rms_lsb"],
            row["vs0_est_v"], r.aggregate.count, row["flags"]))
    return "\n".join(lines)


def list_boards() -> str:
    registry = get_registry()
    lines = []
    if registry.load_error:
        lines.append("ERROR: " + registry.load_error)
    for entry in registry.entries:
        state = "ok" if entry.enabled else "DISABLED: " + entry.error
        lines.append("%-18s %s [%s]" % (entry.id, entry.name, state))
        if entry.enabled:
            fw = entry.firmware
            lines.append("    firmware: test %s (env %s), production %s (env %s)"
                         % (fw["test_project"], fw["test_env"], fw["production_project"],
                            fw["production_env"]))
            for kind, path in entry.docs.items():
                lines.append("    %-10s %s" % (kind, path))
    return "\n".join(lines)


def resolve_board(args) -> str:
    """Board id: --board, else the board of --settings, else the first enabled entry."""
    registry = get_registry()
    if args.board:
        entry = registry.get(args.board)
        if entry is None:
            raise SystemExit("unknown board %r; known: %s" % (args.board, ", ".join(registry.ids())))
        if not entry.enabled:
            raise SystemExit("board %s is disabled: %s" % (entry.id, entry.error))
        return entry.id
    if args.settings:
        import json

        board = json.loads(open(args.settings, encoding="utf-8").read()).get("board_id", DEFAULT_BOARD_ID)
        if registry.get(board) is not None and registry.get(board).enabled:
            return board
    first = registry.first_enabled()
    if first is None:
        raise SystemExit("no usable board in boards.json")
    return first.id


def make_session(args, settings: Settings, log):
    from .boards import load_driver

    driver = load_driver(settings.board_id)
    if args.simulate:
        clock = RealClock() if args.sim_realtime else VirtualClock()
        if settings.board_id == DEFAULT_BOARD_ID:
            # Module-level names so tests can substitute the emulated firmware.
            config = SimConfig(seed=args.sim_seed if args.sim_seed is not None else 7953)
            device = SimulatedDevice(config, clock=clock, r_bias_table=settings.bias)
        else:
            kw = {} if args.sim_seed is None else {"seed": args.sim_seed}
            device = driver.create_simulator(clock, settings.bias, driver.sim_config(**kw))
        client = CapTestClient(SimLink(device))
        session = runner.MeasurementSession(client, settings, clock, simulated=True, driver=driver)
        session.sim_device = device
    else:
        if not args.port:
            raise SystemExit("--port or --simulate required")
        clock = RealClock()
        client = CapTestClient(SerialLink(args.port))
        session = runner.MeasurementSession(client, settings, clock, driver=driver)
    session.on_log = log
    return session


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_boards:
        print(list_boards())
        return 0
    actions = (args.auto or args.channels or args.status or args.detect or args.vmid
               or args.discharge_check or args.reanalyze)
    if args.gui or not (actions or args.simulate or args.port):
        from . import gui
        return gui.run(args)

    board_id = resolve_board(args)
    if args.settings:
        settings = Settings.load(args.settings)
        if settings.board_id != board_id:
            settings = Settings.for_board(board_id)
    elif board_id == DEFAULT_BOARD_ID:
        settings = Settings()
    else:
        settings = Settings.for_board(board_id)
    if args.bursts:
        settings.bursts = args.bursts
    if args.discharge_ms is not None:
        settings.discharge_ms = args.discharge_ms
    if args.samples:
        settings.n_samples = args.samples
    if args.fit:
        settings.fit_model = args.fit
    if args.method:
        settings.method = args.method
    if args.per_address is not None:
        settings.per_address = args.per_address
    if args.save_settings:
        settings.save(args.save_settings)

    def log(text: str) -> None:
        print(text, file=sys.stderr)

    if args.reanalyze:
        results = runner.reanalyze(args.reanalyze, settings if args.settings else None)
        settings = settings if args.settings else runner.load_raw(args.reanalyze)[0]
        print(_table(results, settings.bias_tolerance))
        if args.out:
            runner.write_results_json(results, settings, args.out)
        if args.csv:
            runner.write_results_csv(results, settings, args.csv)
        return 0

    session = make_session(args, settings, log)
    driver = session.driver
    info = session.client.identify()
    print("Firmware:", info.label)
    if args.detect:
        entry = get_registry().get(info.board_id) if info.board_id else None
        print("Attached firmware: %s%s" % (
            info.kind, " of board %s" % entry.name if entry else " (matches no board in boards.json)"))
        return 0 if info.kind != "unknown" else 2
    if info.kind != "captest":
        print("The capacitance-test firmware of %s is not running. Flash it with "
              "CapTest/scripts/flash_firmware.ps1 -Board %s -Firmware test (or the .sh)."
              % (driver.id, driver.id), file=sys.stderr)
        return 2
    if info.board_id and info.board_id != driver.id:
        print("The attached capacitance-test firmware belongs to %s, not to %s. Use --board %s."
              % (info.board_id, driver.id, info.board_id), file=sys.stderr)
        return 2
    if args.status:
        print(info.status)
    if not args.simulate or settings.board_id != DEFAULT_BOARD_ID:
        driver.setup_device(session.client, settings)
    if args.vmid:
        print(driver.vmid_text(session.vmid_read(args.vmid), settings.vref))

    exit_code = 0
    results: List[runner.ChannelResult] = []
    if args.discharge_check:
        channel = driver.parse_spec(args.discharge_check)[0]
        print(session.discharge_check(channel))
    if args.auto:
        groups = [int(x) for x in args.groups.split(",") if x] if args.groups else None

        def progress(p):
            print("\r[%3d/%3d] %s" % (p.done, p.total, p.message), end="", file=sys.stderr)
        session.on_progress = progress
        results = session.run_auto(groups)
        print(file=sys.stderr)
    elif args.channels:
        results = session.run_manual(driver.parse_spec(args.channels))
    if results:
        print(_table(results, settings.bias_tolerance))
        failed = [r for r in results if r.failed]
        if failed:
            print("%d channel(s) failed: %s" % (
                len(failed), ", ".join(driver.key(r.ref) for r in failed)))
            exit_code = 1
    for option, save in ((args.out, session.save_results), (args.csv, session.save_csv),
                         (args.raw_out, session.save_raw)):
        if option:
            path = resolve_output(option, settings.board_id)
            save(path)
            print("Wrote", path, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
