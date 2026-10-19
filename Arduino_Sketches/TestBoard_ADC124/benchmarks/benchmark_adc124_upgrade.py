"""Disconnected-array production matrix for independent VMID delay and SPI clock."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import itertools
import json

from benchmark_adc124_scan_modes import bench, board_status, capture_matrix, serial
from benchmark_adc124_fifo_gpio import snapshot, upload_image, restore


def configurations():
    settings = [(s, s, 16000000, v) for s in (1, 2, 5, 10, 50) for v in (False, True)]
    settings += [(5, d, 16000000, True) for d in (0, 1)]
    settings += [(5, 5, 8000000, v) for v in (False, True)]
    return [bench.BenchmarkConfig('both', mode, engine, clock, 1, sensor, vmid, 'burst', vmid_delay)
            for mode, engine, (sensor, vmid_delay, clock, vmid) in itertools.product(
                ('adc', 'mux'), ('blocking', 'dma', 'lpspi'), settings)]


def restore_settings(root, status):
    config = bench.BenchmarkConfig(status['array'], status['scanorder'], status['spiengine'],
        int(status['spi_clock_hz']), int(status['channelrepeat']), int(status['mux_settle_us']),
        status['vmid_between_groups'] == 'true', status['spi_frame'], bench.vmid_settle_from_status(status))
    routes = tuple(bench.Route.parse(r) for r in status['adcchannels'].split(','))
    with (root/'firmware_commands.log').open('a') as log, serial.Serial('COM4',460800,timeout=.02,write_timeout=3) as port:
        protocol = bench.SerialProtocol(port, log); protocol.stop_and_drain()
        result = bench.configure(protocol, config, routes, 'off')
        if result['running'] != 'false' or result['profile_available'] != 'false':
            raise RuntimeError('Stopped production state not verified')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args(); root = args.output.resolve(); root.mkdir(parents=True, exist_ok=False)
    status = board_status(root)
    if status.get('firmware_revision') != 'adc124-vmid-settle-v1' or status.get('profile_available') != 'false':
        raise RuntimeError('Unexpected production firmware; do not overwrite it')
    images = snapshot(root, 'current')
    lifecycle = dict(state='starting', created_utc=datetime.now(timezone.utc).isoformat(),
        before_status=status, settings_count=len(configurations()), sensor_setup='Both array connectors disconnected; charger connected')
    def save(): bench.write_json(root/'firmware_lifecycle.json', lifecycle)
    save(); diagnostic_upload_attempted = False
    try:
        lifecycle['state'] = 'production_captures'; save()
        capture_matrix(root, 'production', configurations(), 3, 1000, 15000, 'off')
        lifecycle['state'] = 'diagnostic_upload'; save(); diagnostic_upload_attempted = True
        with (root/'firmware_uploads.log').open('a') as log: upload_image(images/'teensy41_profile.hex', log)
        diagnostic = board_status(root)
        if diagnostic.get('firmware_revision') != status['firmware_revision'] or diagnostic.get('profile_available') != 'true':
            raise RuntimeError('Unexpected diagnostic identity')
        lifecycle['state'] = 'diagnostic_captures'; save()
        capture_matrix(root, 'diagnostic', configurations(), 1, 1000, 2000, 'on')
    except BaseException as error:
        lifecycle['error'] = repr(error); save(); raise
    finally:
        if diagnostic_upload_attempted:
            lifecycle['final_production_status'] = restore(root, images/'teensy41.hex', status)
        else:
            lifecycle['final_production_status'] = restore_settings(root, status)
        lifecycle['state'] = 'failed_production_restored_verified' if 'error' in lifecycle else 'complete_production_restored_verified'
        save(); print(lifecycle['state'], flush=True)


if __name__ == '__main__': main()
