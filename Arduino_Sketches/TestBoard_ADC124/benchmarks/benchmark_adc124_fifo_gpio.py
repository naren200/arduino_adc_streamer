"""Matched before/after ADC124 matrix with frozen images and stopped production restore."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import itertools
import json
import shutil
import subprocess
import time

from benchmark_adc124_scan_modes import FW, ROOT, board_status, capture_matrix, bench, serial
from run_adc124_mux_profiles import LOADER, wait_port


def upload_image(image, log):
    with subprocess.Popen([str(LOADER), '--mcu=TEENSY41', '-w', '-v', str(image)],
            stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW) as process:
        try:
            with serial.Serial('COM4', 134, timeout=.05, write_timeout=3): time.sleep(.2)
            if process.wait(timeout=30): raise RuntimeError('Firmware upload failed')
        except BaseException:
            process.terminate(); process.wait(timeout=5); raise
    wait_port()


def snapshot(root, version):
    folder = root / (version + '_firmware'); folder.mkdir()
    files = list((FW / 'src').glob('*.cpp')) + list((FW / 'include').glob('*.h'))
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    for source in files:
        target = folder / 'source' / source.relative_to(FW)
        target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)
    for env in ('teensy41', 'teensy41_profile'):
        source = FW / '.pio/build' / env / 'firmware.hex'
        target = folder / (env + '.hex'); shutil.copy2(source, target)
        hashes[str(source.relative_to(ROOT))] = hashlib.sha256(target.read_bytes()).hexdigest()
    bench.write_json(folder / 'identity.json', dict(
        git_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        source_image_sha256=hashes))
    return folder


def restore(root, image, settings):
    with (root / 'firmware_uploads.log').open('a') as log: upload_image(image, log)
    config = bench.BenchmarkConfig(settings['array'], settings['scanorder'], settings['spiengine'],
        int(settings['spi_clock_hz']), int(settings['channelrepeat']), int(settings['mux_settle_us']),
        settings['vmid_between_groups'] == 'true', settings['spi_frame'], bench.vmid_settle_from_status(settings))
    routes = tuple(bench.Route.parse(r) for r in settings['adcchannels'].split(','))
    with (root / 'firmware_commands.log').open('a') as log, serial.Serial('COM4', 460800, timeout=.02, write_timeout=3) as port:
        protocol = bench.SerialProtocol(port, log); protocol.stop_and_drain()
        result = bench.configure(protocol, config, routes, 'off')
        if result['running'] != 'false' or result['profile_available'] != 'false':
            raise RuntimeError('Stopped production restore not verified')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version', choices=('before', 'after')); parser.add_argument('output', type=Path)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--window-ms', type=int, default=2000)
    parser.add_argument('--extended-quiet', action='store_true',
        help='Use frozen production images for three matched 15 s ADC/blocking/2 us/VMID-off quiet captures')
    args = parser.parse_args()
    if args.repetitions < 1 or args.window_ms < 2000: parser.error('Positive repeats and >=2 s required')
    root = args.output.resolve()
    if args.extended_quiet:
        folder = root / (args.version + '_firmware')
        lifecycle_path = root / (args.version + '_lifecycle.json')
        if not lifecycle_path.exists(): parser.error('Completed version evidence is required')
        lifecycle = json.loads(lifecycle_path.read_text())
        if lifecycle['state'] != 'complete_production_restored_verified': parser.error('Main matrix must be complete')
        try:
            with (root / 'firmware_uploads.log').open('a') as log: upload_image(folder / 'teensy41.hex', log)
            configs = [bench.BenchmarkConfig('both','adc','blocking',16000000,1,2,False,'burst')]
            capture_matrix(root, args.version + '_extended_quiet', configs, args.repetitions, 1000, 15000, 'off')
            lifecycle['extended_quiet_state'] = 'complete'
        except BaseException as error:
            lifecycle['extended_quiet_state'] = 'failed'; lifecycle['extended_quiet_error'] = repr(error); raise
        finally:
            lifecycle['final_production_status'] = restore(root, folder / 'teensy41.hex', lifecycle['before_status'])
            bench.write_json(lifecycle_path, lifecycle)
        return
    if args.version == 'before': root.mkdir(parents=True)
    elif not (root / 'before_lifecycle.json').exists(): parser.error('Before evidence is required')
    image_folder = snapshot(root, args.version)
    lifecycle = dict(version=args.version, created_utc=datetime.now(timezone.utc).isoformat(),
        state='starting', before_status=board_status(root), setup='Connectors disconnected; charger connected')
    if lifecycle['before_status']['profile_available'] != 'false': raise RuntimeError('Expected production firmware')
    expected = 'adc124-shared-mux-burst-v1' if args.version == 'before' else 'adc124-fifo-grouped-gpio-v2'
    if lifecycle['before_status']['firmware_revision'] != expected: raise RuntimeError('Unexpected firmware revision')
    def save(): bench.write_json(root / (args.version + '_lifecycle.json'), lifecycle)
    save()
    configs = [bench.BenchmarkConfig('both', mode, engine, 16000000, 1, settle, vmid, 'burst')
        for engine, settle, vmid, mode in itertools.product(('blocking', 'dma', 'lpspi'),
            (1, 2, 5, 10, 50), (False, True), ('adc', 'mux'))]
    try:
        lifecycle['state'] = 'production_captures'; save()
        capture_matrix(root, args.version + '_production', configs, args.repetitions, 1000, args.window_ms, 'off')
        with (root / 'firmware_uploads.log').open('a') as log:
            upload_image(image_folder / 'teensy41_profile.hex', log)
        diagnostic = board_status(root)
        if diagnostic['profile_available'] != 'true' or diagnostic['firmware_revision'] != expected:
            raise RuntimeError('Diagnostic image identity mismatch')
        lifecycle['state'] = 'diagnostic_captures'; save()
        capture_matrix(root, args.version + '_diagnostic', configs, 1, 1000, 2000, 'on')
    except BaseException as error:
        lifecycle['error'] = repr(error); save(); raise
    finally:
        print('RESTORING ' + args.version + ' production', flush=True)
        lifecycle['final_production_status'] = restore(root, image_folder / 'teensy41.hex', lifecycle['before_status'])
        lifecycle['state'] = 'failed_production_restored_verified' if 'error' in lifecycle else 'complete_production_restored_verified'
        save(); print(lifecycle['state'], flush=True)


if __name__ == '__main__': main()
