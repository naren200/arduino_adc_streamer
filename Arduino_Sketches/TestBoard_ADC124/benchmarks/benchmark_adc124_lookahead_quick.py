"""Upload selector-lookahead production firmware and run a short COM4 matrix."""
from pathlib import Path
import argparse
import itertools
from datetime import datetime, timezone

from benchmark_adc124_fifo_gpio import snapshot, upload_image
from benchmark_adc124_scan_modes import ROOT, board_status, capture_matrix, bench
from benchmark_adc124_upgrade import restore_settings


def configurations():
    matched = [bench.BenchmarkConfig('both', mode, engine, clock, 1, 5, vmid, 'burst')
               for clock, mode, engine, vmid in itertools.product(
                   (8000000, 16000000), ('adc', 'mux'), ('blocking', 'dma', 'lpspi'), (False, True))]
    extra = [bench.BenchmarkConfig('both', mode, 'lpspi', 16000000, 1, 5, True, 'burst', delay)
             for mode, delay in itertools.product(('adc', 'mux'), (0, 1))]
    return matched, extra


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', nargs='?', type=Path, help='New raw directory; defaults to a UTC timestamp below docs/testing/datasets')
    parser.add_argument('--baseline', action='store_true', help='Capture currently installed adc124-vmid-settle-v1 before uploading')
    parser.add_argument('--dry-run', action='store_true', help='Print matrix without serial access, upload or output files')
    parser.add_argument('--warm-up-ms', type=int, default=500)
    parser.add_argument('--window-ms', type=int, default=2000)
    args = parser.parse_args(argv)
    if args.warm_up_ms < 0 or args.window_ms < 1000 or args.warm_up_ms + args.window_ms > 3600000:
        parser.error('Warm-up must be nonnegative; measurement >=1000 ms; total <=3600000 ms')
    configs, extras = configurations()
    print(f'{len(configs) + len(extras)} new settings; {len(configs) if args.baseline else 0} baseline settings; '
          f'{args.warm_up_ms} ms warm-up + {args.window_ms} ms measurement', flush=True)
    if args.dry_run:
        for config in configs + extras: print(config.test_id)
        return 0
    root = args.output.resolve() if args.output else ROOT/'docs/testing/datasets/testboard-adc124'/(
        'selector_lookahead_quick_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ'))
    root.mkdir(parents=True, exist_ok=False)
    original = board_status(root)
    bench.write_json(root/'original_status.json', original)
    if original.get('board') != 'TestBoard_ADC124' or original.get('profile_available') != 'false':
        raise RuntimeError('Expected ADC124 production firmware; no upload performed')
    if args.baseline and original.get('firmware_revision') != 'adc124-vmid-settle-v1':
        raise RuntimeError('Matched baseline requires the previous adc124-vmid-settle-v1 production image')
    images = snapshot(root, 'after')
    life = dict(state='starting', before_status=original, created_utc=datetime.now(timezone.utc).isoformat(),
                setup='Both connectors disconnected; charger connected', baseline_settings=len(configs) if args.baseline else 0,
                new_settings=len(configs) + len(extras), warmup_ms=args.warm_up_ms, window_ms=args.window_ms, repetitions=1)
    def save(): bench.write_json(root/'firmware_lifecycle.json', life)
    save()
    try:
        if args.baseline:
            life['state'] = 'baseline_captures'; save()
            capture_matrix(root, 'before', configs, 1, args.warm_up_ms, args.window_ms, 'off')
        life['state'] = 'production_upload'; save()
        with (root/'firmware_uploads.log').open('a') as log:
            upload_image(images/'teensy41.hex', log)
        status = board_status(root)
        if (status.get('firmware_revision') != 'adc124-selector-lookahead-v1' or
                status.get('profile_available') != 'false' or
                status.get('adc_selector_policy') != 'lookahead' or
                status.get('adc_batch_prime_frames') != '0'):
            raise RuntimeError('Uploaded production identity mismatch')
        life['uploaded_status'] = status; life['state'] = 'new_captures'; save()
        capture_matrix(root, 'after', configs + extras, 1, args.warm_up_ms, args.window_ms, 'off')
    except BaseException as error:
        life['error'] = repr(error); save(); raise
    finally:
        life['final_production_status'] = restore_settings(root, original)
        life['state'] = 'failed_settings_restored' if 'error' in life else 'complete_new_production_restored_verified'
        save(); print(life['state'], flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
