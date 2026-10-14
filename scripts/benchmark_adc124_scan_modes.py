"""Protocol-3 scan benchmark with raw evidence and guaranteed production restoration.

No commit/push. One serial owner and one CLI uploader; disconnected connectors,
charger connected. Analysis applies a common all-channel time mask later.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import itertools
import json
import subprocess
import sys
import time
import numpy as np
import serial

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from Arduino_Sketches.TestBoard_ADC124.benchmarks import testboard_adc124_benchmark as bench
from Arduino_Sketches.TestBoard_ADC124.benchmarks.firmware_profile import save_profile
from capture_adc124_settling import decode
from run_adc124_mux_profiles import upload

FW = ROOT / "Arduino_Sketches/TestBoard_ADC124"


def record_capture(protocol, config, routes, output, warmup_ms, window_ms, profiling):
    output.mkdir()
    before = bench.configure(protocol, config, routes, profiling)
    total_ms = warmup_ms + window_ms
    protocol.serial.write(f"run {total_ms}*".encode("ascii")); protocol.serial.flush()
    started = last = time.monotonic()
    with (output / "capture.bin").open("wb") as raw:
        while True:
            data = protocol.serial.read(min(protocol.serial.in_waiting, 262144) or 1)
            now = time.monotonic()
            if data: raw.write(data); last = now
            elif now - started >= total_ms / 1000 and now - last >= .2: break
            if now - started > total_ms / 1000 + 4: raise RuntimeError("Stream did not stop")
    protocol.stop_and_drain()
    after = bench.parse_status(protocol.command("status"))
    bench.write_json(output / "status.json", dict(before=before, after=after))
    bench.validate_status(after, config, routes)
    data = decode(output / "capture.bin")
    accounting, failures = bench.stream_accounting(before, after, len(data))
    errors = {key: bench.uint32_delta(int(after[key]), int(before[key])) for key in bench.error_counter_keys(after)}
    if failures or any(errors.values()): raise RuntimeError(str(failures or errors))
    profile = save_profile(output / "firmware_profile.jsonl", after, received_frames=len(data), requested=profiling)
    starts = np.r_[0, np.cumsum(np.diff(data["start"].astype(np.int64)) % 2**32)]
    durations = (data["end"].astype(np.int64) - data["start"].astype(np.int64)) % 2**32
    if np.any(np.diff(starts) < durations[:-1]): raise RuntimeError("Acquisitions overlap")
    measured = starts >= warmup_ms * 1000
    if not measured.any() or starts[measured][-1] - starts[measured][0] < window_ms * 1000 * .95:
        raise RuntimeError("Insufficient measured coverage")
    return dict(frames=len(data), measured_frames=int(measured.sum()), duration_s=float(starts[-1] / 1e6),
                sha256=hashlib.sha256((output / "capture.bin").read_bytes()).hexdigest(), errors=errors,
                profile_enabled=profile["enabled"], **accounting)


def capture_matrix(root, stage, configs, repetitions, warmup_ms, window_ms, profiling):
    output = root / stage; output.mkdir()
    routes = bench.load_manifest(bench.DEFAULT_MANIFEST)["both_full"]
    meta = dict(board="TestBoard_ADC124", protocol_version=3, stage=stage, profile=profiling,
                created_utc=datetime.now(timezone.utc).isoformat(), warmup_s=warmup_ms / 1000,
                measurement_s=window_ms / 1000, repetitions=repetitions, state="running", runs=[],
                setup=dict(connectors="both disconnected", charger="connected", reference_volts=3.3,
                           vmid_volts=1.65, bias_ohms=[[470000,1000000,470000,1000000],[1000000,470000,1000000,470000]]))
    def save(): bench.write_json(output / "session.json", meta)
    save()
    with (output / "commands.log").open("w", encoding="utf-8") as log, serial.Serial("COM4", 460800, timeout=.02, write_timeout=3) as port:
        protocol = bench.SerialProtocol(port, log); protocol.stop_and_drain()
        if "# TestBoard_ADC124" not in protocol.command("mcu"): raise RuntimeError("Wrong board")
        try:
            for repetition in range(1, repetitions + 1):
                rotation = (repetition - 1) * 20 % len(configs)
                for config in configs[rotation:] + configs[:rotation]:
                    name = f"{config.test_id}__rep{repetition}"
                    print(f"CAPTURE {stage} {repetition}/{repetitions} {config.test_id}", flush=True)
                    evidence = record_capture(protocol, config, routes, output / name, warmup_ms, window_ms, profiling)
                    meta["runs"].append(dict(name=name, repetition=repetition, **bench.asdict(config), **evidence))
                    save()
                    print(f"VALIDATED {evidence['frames']:,} frames; discarded {evidence['usb_frames_discarded']}", flush=True)
            meta["state"] = "complete"
        except BaseException as error:
            meta["state"] = "failed"; meta["error"] = repr(error); raise
        finally:
            protocol.stop_and_drain(); save()


def board_status(root):
    with (root / "firmware_commands.log").open("a") as log, serial.Serial("COM4", 460800, timeout=.02, write_timeout=3) as port:
        protocol = bench.SerialProtocol(port, log); protocol.stop_and_drain()
        return bench.parse_status(protocol.command("status"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--window-ms", type=int, default=2000)
    args = parser.parse_args()
    if args.repetitions < 1 or args.window_ms < 2000: parser.error("At least one repetition and 2 s window required")
    root = args.output
    lifecycle = json.loads((root / "firmware_lifecycle.json").read_text())
    files = list((FW / "src").glob("*.cpp")) + list((FW / "include").glob("*.h"))
    files += [FW / ".pio/build" / e / "firmware.hex" for e in ("teensy41", "teensy41_profile")]
    lifecycle["new_firmware_sha256"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    def save(): bench.write_json(root / "firmware_lifecycle.json", lifecycle)
    configs = [bench.BenchmarkConfig("both", mode, engine, 16000000, 1, settle, vmid, "burst")
               for engine, settle, vmid, mode in itertools.product(("blocking","dma","lpspi"), (1,2,5,10,50), (False,True), ("adc","mux"))]
    controls = [bench.BenchmarkConfig("both", mode, engine, 16000000, 1, 5, vmid, "single")
                for engine, vmid, mode in itertools.product(("blocking","dma","lpspi"), (False,True), ("adc","mux"))]
    try:
        if board_status(root).get("protocol_version") != "3": raise RuntimeError("Upload protocol-3 production first")
        lifecycle["state"] = "production_captures"; save()
        capture_matrix(root, "production", configs, args.repetitions, 1000, args.window_ms, "off")
        capture_matrix(root, "single_controls", controls, 1, 1000, args.window_ms, "off")
        with (root / "firmware_uploads.log").open("a") as log: upload("teensy41_profile", log)
        if board_status(root).get("profile_available") != "true": raise RuntimeError("Diagnostic identity not verified")
        lifecycle["state"] = "diagnostic_captures"; save()
        capture_matrix(root, "diagnostic", configs, 1, 1000, 2000, "on")
    except BaseException as error:
        lifecycle["error"] = repr(error); save(); raise
    finally:
        print("RESTORING production firmware", flush=True)
        with (root / "firmware_uploads.log").open("a") as log: upload("teensy41", log)
        old = lifecycle["before_status"]
        mode = "adc" if old.get("protocol_version") in ("1","2") else old["scanorder"]
        restore = bench.BenchmarkConfig(old["array"], mode, old["spiengine"], int(old["spi_clock_hz"]),
            int(old["channelrepeat"]), int(old["mux_settle_us"]), old["vmid_between_groups"] == "true", old.get("spi_frame","burst"),
            bench.vmid_settle_from_status(old))
        routes = tuple(bench.Route.parse(r) for r in old["adcchannels"].split(","))
        with (root / "firmware_commands.log").open("a") as log, serial.Serial("COM4", 460800, timeout=.02, write_timeout=3) as port:
            protocol = bench.SerialProtocol(port, log); protocol.stop_and_drain()
            final = bench.configure(protocol, restore, routes, "off")
            if final.get("profile_available") != "false" or final.get("running") != "false": raise RuntimeError("Restoration not verified")
        lifecycle["final_production_status"] = final
        lifecycle["state"] = "failed_production_restored_verified" if "error" in lifecycle else "complete_production_restored_verified"
        save(); print(lifecycle["state"], flush=True)


if __name__ == "__main__": main()
