"""Before/after VMID-settling checks at 5 us sensor settling, with production restoration."""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import itertools
import json

from benchmark_adc124_fifo_gpio import snapshot, upload_image, restore
from benchmark_adc124_scan_modes import board_status, capture_matrix, bench


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version', choices=('before','after'))
    parser.add_argument('output', type=Path)
    args = parser.parse_args(); root = args.output.resolve()
    if args.version == 'before': root.mkdir(parents=True)
    elif not (root/'before_lifecycle.json').exists(): parser.error('Before evidence is required')
    folder = snapshot(root,args.version)
    status = board_status(root)
    expected = 'adc124-fifo-grouped-gpio-v2' if args.version=='before' else 'adc124-vmid-settle-v1'
    if status['firmware_revision'] != expected or status['profile_available'] != 'false':
        raise RuntimeError('Unexpected production firmware identity')
    record = dict(state='starting',version=args.version,before_status=status,
        created_utc=datetime.now(timezone.utc).isoformat(),setup='Connectors disconnected; charger connected')
    def save(): bench.write_json(root/(args.version+'_lifecycle.json'),record)
    save()
    settings = (None,) if args.version=='before' else (None,5,0,1)
    configs = []
    for mode,engine,delay in itertools.product(('adc','mux'),('blocking','dma','lpspi'),settings):
        if args.version=='before': config=bench.BenchmarkConfig('both',mode,engine,16000000,1,5,True,'burst')
        else: config=bench.BenchmarkConfig('both',mode,engine,16000000,1,5,True,'burst',delay)
        configs.append(config)
    try:
        record['state']='production_captures'; save()
        capture_matrix(root,args.version+'_production',configs,3,1000,5000,'off')
        if args.version=='after':
            with (root/'firmware_uploads.log').open('a') as log: upload_image(folder/'teensy41_profile.hex',log)
            diagnostic=board_status(root)
            if diagnostic['firmware_revision']!=expected or diagnostic['profile_available']!='true':
                raise RuntimeError('Diagnostic identity mismatch')
            record['state']='diagnostic_captures'; save()
            capture_matrix(root,'after_diagnostic',configs,1,1000,2000,'on')
    except BaseException as error:
        record['error']=repr(error); save(); raise
    finally:
        original = status if args.version=='before' else json.loads((root/'before_lifecycle.json').read_text())['before_status']
        record['final_production_status']=restore(root,folder/'teensy41.hex',original)
        record['state']='failed_production_restored_verified' if 'error' in record else 'complete_production_restored_verified'
        save(); print(record['state'],flush=True)


if __name__=='__main__': main()
