"""20-second VMID holds, retained ten-conversion bursts and long VMID scans.

Uses pcbdiag-v2 only. Always restores the previously frozen production image and
the settings read immediately before this extension. Original captures unchanged.
"""
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
import argparse,hashlib,json,shutil,time
import numpy as np
import serial
import pcb_diagnostics as d
from benchmark_adc124_fifo_gpio import upload_image,restore
from benchmark_adc124_scan_modes import board_status
from testboard_adc124_benchmark import SerialProtocol

RAW=d.ROOT/'docs/testing/datasets/testboard-adc124/vmid_long_hold_2026-10-15'
OUT=d.ROOT/'docs/testing/results/testboard-adc124/vmid_long_hold_2026-10-15'

def full_steps(vmid_us=None):
    ss=[]
    for address in range(7):
        ss.append(d.Step(address,5,mask=8 if address==6 else 15))
        if vmid_us is not None:ss.append(d.Step(7,vmid_us,mask=15,flags=0))
    return tuple(ss)

def plan():
    pairs=[]
    pairs.append(dict(key='full',before=d.Program('hold_full_before','hold_full_before',full_steps()),
        after=d.Program('hold_full_after','hold_full_after',full_steps(),warmup_ms=0,window_ms=0)))
    for first in (0,1):
        for mux in range(1,5):
            key=f'mux{mux}_first{first}'
            pairs.append(dict(key=key,before=d.Program('hold_'+key+'_before','hold_ten_before',full_steps(),first_bank=first),
                after=d.Program('hold_'+key+'_after','hold_ten_after',(d.Step(2,5,mask=1<<(mux-1),repeat=10),),
                    first_bank=first,warmup_ms=0,window_ms=0)))
    scans=[d.Program(f'long_vmid_{wait}','long_vmid_scan',full_steps(wait),warmup_ms=2000,window_ms=10000) for wait in (1000,5000,10000)]
    for pair in pairs:
        pair['before'].validate();pair['after'].validate()
    for p in scans:p.validate()
    return pairs,scans

def capture_single(port,proto,p,rep,root,hold_us=20000000):
    assert 0<=hold_us<=30000000
    folder=root/'captures';folder.mkdir(exist_ok=True);stem=folder/f'{p.name}_r{rep}'
    if stem.with_suffix('.json').exists():
        old=json.loads(stem.with_suffix('.json').read_text())
        if old.get('state')=='complete':return
        raise RuntimeError(f'Incomplete existing single capture: {stem}')
    proto.command('stop');proto.command('clear')
    proto.command(f'config {p.clock} {p.arrays} {p.first_bank} {p.order} {p.period}')
    for step in p.steps:proto.command(step.command())
    meta=dict(state='starting',program=asdict(p),rep=rep,labels=p.labels()[0],
        command='single',requested_hold_us=hold_us,started_utc=datetime.now(timezone.utc).isoformat())
    stem.with_suffix('.json').write_text(json.dumps(meta,indent=2))
    proto.log_line(f'> single {hold_us}*')
    port.write(f'single {hold_us}*'.encode());port.flush()
    chunks=[];started=time.monotonic();last=started
    finish_after=hold_us/1e6+d.completion_wait_s(p)
    while time.monotonic()-started<finish_after+2:
        b=port.read(max(1,min(port.in_waiting,262144)))
        if b:chunks.append(b);last=time.monotonic()
        elif time.monotonic()-started>finish_after and time.monotonic()-last>.2:break
    data=b''.join(chunks);stem.with_suffix('.bin').write_bytes(data)
    final=d.status(proto);parsed=d.decode(data,p)
    actual=(int(final['hold_end_us'])-int(final['hold_start_us']))&0xffffffff
    if final['firmware_revision']!='adc124-pcbdiag-v2' or final['running']!='false' or final['single_mode']!='true':
        raise RuntimeError('Single-capture identity/state mismatch')
    if len(parsed['sequence'])!=1 or int(parsed['sequence'][0])!=0 or int(final['cycles'])!=1 or int(final['sent'])!=1 or int(final['dropped']):
        raise RuntimeError('Single capture must contain exactly one complete transmitted cycle')
    if int(final['overruns']) or actual<hold_us or actual>hold_us+100000:
        raise RuntimeError(f'Hold timing/accounting mismatch: {actual} vs {hold_us}')
    np.savez_compressed(stem.with_suffix('.npz'),**parsed)
    meta.update(state='complete',status=final,frames=1,actual_hold_us=actual,
        first_received_sequence=0,last_received_sequence=0,host_elapsed_s=time.monotonic()-started,
        raw_sha256=hashlib.sha256(data).hexdigest())
    stem.with_suffix('.json').write_text(json.dumps(meta,indent=2))
    print(f'{p.name} r{rep}: hold={actual/1e6:.6f}s; exactly one cycle',flush=True)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--smoke',action='store_true');args=parser.parse_args()
    RAW.mkdir(parents=True,exist_ok=True);OUT.mkdir(parents=True,exist_ok=True)
    lp=RAW/'lifecycle.json'
    if lp.exists():
        life=json.loads(lp.read_text())
    else:
        original=json.loads((d.RAW/'lifecycle.json').read_text())
        before=board_status(RAW)
        if before['firmware_revision']!=original['final_status']['firmware_revision'] or before['profile_available']!='false':
            raise RuntimeError('Expected restored production firmware before extension')
        for name,expected in original['production_hashes'].items():
            rel=Path(name).relative_to('Arduino_Sketches/TestBoard_ADC124')
            source=d.RAW/'production_backup'/rel
            if hashlib.sha256(source.read_bytes()).hexdigest()!=expected:raise RuntimeError('Original frozen backup hash mismatch')
            if rel.suffix in ('.cpp','.h') and hashlib.sha256((d.FW/rel).read_bytes()).hexdigest()!=expected:
                raise RuntimeError(f'Production source changed: {rel}')
        shutil.copytree(d.RAW/'production_backup',RAW/'production_backup')
        life=dict(state='prepared',before_status=before,production_hashes=original['production_hashes'],
            original_dataset=str(d.RAW),created_utc=datetime.now(timezone.utc).isoformat(),attempts=[])
    image=d.FW/'.pio/build/teensy41_pcbdiag/firmware.hex'
    frozen=RAW/'diagnostic_firmware';frozen.mkdir(exist_ok=True)
    for source in list((d.FW/'src').glob('*.cpp'))+list((d.FW/'include').glob('*.h'))+[d.FW/'platformio.ini',image,image.with_suffix('.elf')]:
        target=frozen/source.relative_to(d.FW);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    life['diagnostic_sha256']=hashlib.sha256(image.read_bytes()).hexdigest()
    attempt=dict(smoke=args.smoke,started_utc=datetime.now(timezone.utc).isoformat());life['attempts'].append(attempt)
    def save():lp.write_text(json.dumps(life,indent=2))
    pairs,scans=plan()
    (RAW/'plan.json').write_text(json.dumps(dict(pairs=[dict(key=p['key'],before=asdict(p['before']),after=asdict(p['after'])) for p in pairs],
        scans=[asdict(p) for p in scans],hold_us=20000000,repetitions=3,expected_captures=63),indent=2))
    life['state']='uploading';save()
    try:
        with (RAW/'uploads.log').open('a') as log:upload_image(image,log)
        life['state']='capturing';save()
        with (RAW/'commands.log').open('a',encoding='utf-8') as log,serial.Serial('COM4',460800,timeout=.01,write_timeout=3) as port:
            proto=SerialProtocol(port,log);proto.stop_and_drain()
            if d.status(proto)['firmware_revision']!='adc124-pcbdiag-v2':raise RuntimeError('Wrong diagnostic image')
            try:
                if args.smoke:
                    p=d.Program('smoke_ten','smoke',(d.Step(2,5,mask=1,repeat=10),),warmup_ms=0,window_ms=0)
                    capture_single(port,proto,p,1,RAW,100000)
                else:
                    for rep in (1,2,3):
                        for pair in (pairs[::-1] if rep==2 else pairs):
                            # A defined continuous-scan history precedes the hold;
                            # retain every pre-cycle, including the last full cycle.
                            d.capture(port,proto,pair['before'],rep,RAW)
                            capture_single(port,proto,pair['after'],rep,RAW)
                    for rep in (1,2,3):
                        for p in (scans[::-1] if rep==2 else scans):d.capture(port,proto,p,rep,RAW)
            finally:proto.stop_and_drain()
        attempt['result']='captures_complete'
    except BaseException as exc:
        attempt['error']=repr(exc);save();raise
    finally:
        print('Restoring frozen production firmware and original settings',flush=True)
        life['final_status']=restore(RAW,RAW/'production_backup/.pio/build/teensy41/firmware.hex',life['before_status'])
        life['state']='production_restored_verified';save();print(life['state'],flush=True)

if __name__=='__main__':main()
