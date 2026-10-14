"""Raw-only ADC124 settling matrix; reuse the board's validated command layer."""
from pathlib import Path
import sys
import json
import hashlib
import time
import argparse
from datetime import datetime, timezone
import numpy as np
import serial

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Arduino_Sketches/TestBoard_ADC124/benchmarks'))
import testboard_adc124_benchmark as bench
from benchmark_common import BinaryFrameParser, uint32_delta
from firmware_profile import save_profile

FRAME = np.dtype([('magic','<u2'), ('count','<u2'), ('samples','<u2',(50,)),
                  ('avg','<u2'), ('start','<u4'), ('end','<u4')])

def decode(path):
    raw = path.read_bytes()
    if len(raw) % FRAME.itemsize:
        raise ValueError('Partial frame')
    data = np.frombuffer(raw, FRAME)
    if not len(data) or not np.all(data['magic'] == 0x55aa) or not np.all(data['count'] == 50):
        raise ValueError('Frame header/count mismatch')
    if data['samples'].max() > 4095:
        raise ValueError('Invalid ADC code')
    parser = BinaryFrameParser(expected_sample_count=50, max_sample_count=50)
    offset = 0
    for start in range(0, len(raw), 65537):
        frames = parser.feed(raw[start:start+65537])
        for frame in frames:
            row = data[offset]
            if (tuple(row['samples']) != frame.samples or int(row['start']) != frame.block_start_us
                    or int(row['end']) != frame.block_end_us or int(row['avg']) != frame.avg_dt_us):
                raise ValueError('Independent parser mismatch')
            offset += 1
    if parser.finish() or parser.invalid_frames or parser.discarded_bytes or offset != len(data):
        raise ValueError('Parser validation failed')
    dt = (np.diff(data['start'].astype(np.int64)) % 2**32)
    if np.any(dt <= 0):
        raise ValueError('Nonmonotonic device timestamps')
    return data

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--engines', nargs='+', choices=('blocking','dma','lpspi'), default=['lpspi'])
    parser.add_argument('--orders', nargs='+', choices=('adc','mux'), default=['adc'],
                        help='Protocol 3: adc sequential arrays, mux shared-bank settling')
    parser.add_argument('--spi-frame', choices=('burst','single'), default='burst')
    parser.add_argument('--settles', nargs='+', type=int, default=[1,2,5,10,50])
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--warmup-ms', type=int, default=1000)
    parser.add_argument('--window-ms', type=int, default=15000)
    parser.add_argument('--profile', choices=('off','on'), default='off')
    args = parser.parse_args()
    if args.repetitions < 1 or args.warmup_ms < 0 or args.window_ms < 1000 or any(s<0 or s>1000 for s in args.settles):
        parser.error('Invalid repetitions, duration or settling delay')
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    routes = bench.load_manifest(bench.DEFAULT_MANIFEST)['both_full']
    configs = [bench.BenchmarkConfig('both',o,e,16000000,1,s,v,args.spi_frame)
               for s in args.settles for v in (False,True) for o in args.orders for e in args.engines]
    total_ms = args.warmup_ms + args.window_ms
    meta = {'board':'TestBoard_ADC124','created_utc':datetime.now(timezone.utc).isoformat(),
            'warmup_s':args.warmup_ms/1000,'measurement_s':args.window_ms/1000,
            'repetitions':args.repetitions,'profile':args.profile,'state':'running',
            'setup':{'connectors':'both disconnected','charger':'connected',
            'reference_volts':3.3,'vmid_volts':1.65,'bias_ohms':[[470000,1000000,470000,1000000],[1000000,470000,1000000,470000]]},
            'runs':[]}
    def save():
        bench.write_json(output/'session.json', meta)
    save()
    with (output/'commands.log').open('w',encoding='utf-8') as log, serial.Serial('COM4',460800,timeout=.02,write_timeout=3) as port:
        protocol = bench.SerialProtocol(port,log)
        protocol.stop_and_drain()
        if '# TestBoard_ADC124' not in protocol.command('mcu'):
            raise ValueError('Wrong board')
        original = bench.parse_status(protocol.command('status'))
        meta['original_status'] = original
        meta['protocol_version'] = int(original['protocol_version'])
        meta['spi_frame'] = args.spi_frame
        save()
        try:
            for rep in range(1,args.repetitions+1):
                rotation = ((rep-1)*(len(configs)//3+1)) % len(configs)
                ordered = configs[rotation:] + configs[:rotation]
                for config in ordered:
                    name = config.test_id + f'__rep{rep}'
                    folder = output/name
                    folder.mkdir()
                    before = bench.configure(protocol,config,routes,args.profile)
                    print(f'CAPTURE rep {rep}/{args.repetitions} {config.scanorder} {config.spiengine} settle {config.mux_settle_us} us VMID {config.vmid}',flush=True)
                    protocol.log_line(f'> run {total_ms}* (raw-only)')
                    port.write(f'run {total_ms}*'.encode('ascii'))
                    port.flush()
                    began = last = time.monotonic()
                    with (folder/'capture.bin').open('wb') as f:
                        while True:
                            chunk = port.read(min(port.in_waiting,262144) or 1)
                            now = time.monotonic()
                            if chunk:
                                f.write(chunk)
                                last = now
                            elif now-began >= total_ms/1000 and now-last >= .2:
                                break
                            if now-began > total_ms/1000+4:
                                raise ValueError('Stream did not stop')
                    protocol.stop_and_drain()
                    after = bench.parse_status(protocol.command('status'))
                    bench.validate_status(after,config,routes)
                    bench.write_json(folder/'status.json',{'before':before,'after':after})
                    data = decode(folder/'capture.bin')
                    accounting, failures = bench.stream_accounting(before,after,len(data))
                    errors = {k:uint32_delta(int(after[k]),int(before[k])) for k in bench.error_counter_keys(after)}
                    if failures or any(errors.values()):
                        raise ValueError(str(failures or errors))
                    profiling = save_profile(folder/'firmware_profile.jsonl',after,received_frames=len(data),requested=args.profile)
                    elapsed = np.r_[0,np.cumsum(np.diff(data['start'].astype(np.int64)) % 2**32)]/1e6
                    measured = elapsed >= args.warmup_ms/1000
                    if elapsed[measured][-1] - elapsed[measured][0] < args.window_ms/1000*.95:
                        raise ValueError('Insufficient measured coverage')
                    meta['runs'].append({'name':name,'repetition':rep,'settle_us':config.mux_settle_us,'vmid':config.vmid,
                        'engine':config.spiengine,'order':config.scanorder,'profile_enabled':profiling['enabled'],
                        'frames':len(data),'measured_frames':int(measured.sum()),'duration_s':float(elapsed[-1]),
                        'sha256':hashlib.sha256((folder/'capture.bin').read_bytes()).hexdigest(),
                        'errors':errors,**accounting})
                    save()
                    print(f'VALIDATED {len(data):,} frames; USB discarded {accounting["usb_frames_discarded"]}',flush=True)
            meta['state']='complete'
        except BaseException as error:
            meta['state']='failed'
            meta['error']=repr(error)
            raise
        finally:
            try:
                protocol.stop_and_drain()
                old = original['protocol_version'] in ('1','2')
                restore = bench.BenchmarkConfig(original['array'],'adc' if old else original['scanorder'],original['spiengine'],
                    int(original['spi_clock_hz']),int(original['channelrepeat']),int(original['mux_settle_us']),original['vmid_between_groups']=='true',
                    'single' if old else original.get('spi_frame','burst'))
                original_routes=tuple(bench.Route.parse(r) for r in original['adcchannels'].split(','))
                meta['restored_status']=bench.configure(protocol,restore,original_routes)
            finally:
                save()
    print(f'COMPLETE {output}',flush=True)

if __name__ == '__main__':
    main()
