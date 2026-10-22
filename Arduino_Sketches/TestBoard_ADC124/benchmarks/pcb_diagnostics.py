"""Isolated PCB diagnostic microprograms and strict ADG1 frame decoder.

No production frame/parser changes. Physical address 6 is VMID on MUX1-3.
"""
from dataclasses import dataclass, asdict
from pathlib import Path
import argparse, hashlib, json, struct, sys, time
import numpy as np

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
FW=HERE.parent
RAW=ROOT/'docs/testing/datasets/testboard-adc124/pcb_diagnostics_2026-10-15'
HEADER=struct.Struct('<4sHHIIII')
EVENT=struct.Struct('<BBBBIII')

@dataclass(frozen=True)
class Step:
    address:int
    wait:int=5
    slot:int=0
    mask:int=15
    repeat:int=1
    flags:int=1
    def command(self):
        return f'step {self.address} {self.wait} {self.slot} {self.mask} {self.repeat} {self.flags}'

@dataclass(frozen=True)
class Program:
    name:str
    family:str
    steps:tuple
    clock:int=16000000
    arrays:int=3
    first_bank:int=0
    order:int=228
    period:int=0
    warmup_ms:int=1000
    window_ms:int=2500
    repetitions:int=3
    def labels(self):
        order=[(self.order>>(2*i))&3 for i in range(4)]
        labels=[];events=[]
        for i,s in enumerate(self.steps):
            if not(s.flags&1) or not s.mask:continue
            for b in ((self.first_bank+j)%2 for j in range(2)):
                if not(self.arrays&(1<<b)):continue
                inputs=[k for k in order if s.mask&(1<<k)]
                events.append((i,b,s.address,len(inputs)*s.repeat))
                for k in inputs:
                    for r in range(s.repeat):
                        labels.append(dict(step=i,array=b+1,mux=k+1,address=s.address,repeat=r,
                            role='vmid' if s.address==7 or(s.address==6 and k<3) else 'sensor'))
        return labels,events
    def validate(self):
        assert self.arrays in (1,2,3) and self.first_bank in (0,1)
        assert sorted((self.order>>(2*i))&3 for i in range(4))==list(range(4))
        assert self.clock in (8000000,16000000) and 0<=self.period<=2000000
        assert 1<=len(self.steps)<=32
        for s in self.steps:
            assert 0<=s.address<=7 and 0<=s.wait<=1500000 and 0<=s.slot<=1500000
            assert 0<=s.mask<=15 and 1<=s.repeat<=10 and 0<=s.flags<=15
            assert not(s.flags&2 and s.mask)
            assert s.mask.bit_count()*s.repeat<=12
        labels,events=self.labels()
        assert labels and 28+sum(16+2*e[3] for e in events)<=2048
        assert sum(max(s.wait,s.slot) for s in self.steps)<=2000000
        return self

def fnv(data):
    h=2166136261
    for b in data:h=((h^b)*16777619)&0xffffffff
    return h

def completion_wait_s(program):
    # A run duration is checked BETWEEN cycles. A cycle may start just before
    # the deadline and finish as much as one full isolation interval afterward.
    bound=max(program.period,sum(max(s.wait,s.slot)+100 for s in program.steps))
    return bound/1e6+.35

def decode(data,program):
    labels,expected=program.labels(); pos=0;frames=[];times=[];seqs=[];events=[]
    frame_bytes=28+sum(16+2*e[3] for e in expected)
    # run ACK is captured with the first frame, never consumed/lost by an ACK reader.
    if data.startswith(b'#OK\r\n'):pos=5
    elif data.startswith(b'#OK\n'):pos=4
    else:raise ValueError('Missing run ACK')
    while pos<len(data):
        if len(data)-pos<frame_bytes:raise ValueError('Truncated final diagnostic frame')
        raw=memoryview(data)[pos:pos+frame_bytes]
        magic,n,ne,seq,start,end,overruns=HEADER.unpack_from(raw)
        if magic!=b'ADG1' or n!=frame_bytes or ne!=len(expected):raise ValueError('Frame schema mismatch')
        if fnv(raw[:-4])!=struct.unpack_from('<I',raw,n-4)[0]:raise ValueError('Frame checksum mismatch')
        at=24;values=[];ev=[]
        for exp in expected:
            a,b,c,count,sw,t0,t1=EVENT.unpack_from(raw,at);at+=16
            if (a,b,c,count)!=exp:raise ValueError('Physical event ordering mismatch')
            if not(sw<=t0<=t1):raise ValueError('Invalid event timestamps')
            v=struct.unpack_from('<'+'H'*count,raw,at);at+=count*2
            if any(z>4095 for z in v):raise ValueError('Non-12-bit ADC data')
            values.extend(v);ev.append((sw,t0,t1))
        if seqs and seq<=seqs[-1]:raise ValueError('Nonmonotonic cycle sequence')
        frames.append(values);times.append((start,end,overruns));seqs.append(seq);events.append(ev)
        pos+=n
    if not frames:raise ValueError('No diagnostic frames')
    return dict(values=np.asarray(frames,dtype=np.uint16),timing=np.asarray(times,dtype=np.uint32),
        sequence=np.asarray(seqs,dtype=np.uint32),events=np.asarray(events,dtype=np.uint32))

def programs():
    out=[]
    def add(name,family,steps,**kw):out.append(Program(name,family,tuple(steps),**kw).validate())
    # Fast conditions reproduce the production electrical sequence; their extra
    # diagnostic timestamp/encoding overhead means rates are not production rates.
    for d in (1,5,50):
        for which in ('full','core','full_vmid'):
            ss=[]
            for addr in range(7 if which!='core' else 6):
                ss.append(Step(addr,d,mask=8 if addr==6 else 15))
                if which=='full_vmid':ss.append(Step(7,d,mask=15,flags=0))
            add(f'fast_{which}_{d}','boundary_fast',ss)
        for boundary in ('hold','address6','vmid','vmid_adc','disabled'):
            ss=[Step(a,d,100) for a in range(6)]
            ss.append(Step(5 if boundary in ('hold','disabled') else 6 if boundary=='address6' else 7,
                d,100,15 if boundary=='vmid_adc' else 0,1,2 if boundary=='disabled' else 0))
            add(f'boundary_{boundary}_{d}','boundary_matched',ss,period=710)
    for addr in (0,2,5):
        for mask in (15,1,2,4,8):
            add(f'held_a{addr}_mask{mask}','held', [Step(addr,5,100,mask)],period=710)
    # All eight paths: eight retained consecutive conversions vs a delayed single
    # conversion. One bank at a time avoids the other bank changing target timing.
    for bank in (0,1):
        for mux in range(4):
            for clock,d in ((16000000,1),(16000000,5),(16000000,50),(8000000,1),(8000000,5)):
                for kind in ('eight','wait'):
                    extra=0 if kind=='eight' else 7*(16000000//clock)
                    add(f'recovery_b{bank}_m{mux}_{clock}_{d}_{kind}','recovery',
                        [Step(7,100,120,0,1,0),Step(2,d+extra,100,1<<mux,8 if kind=='eight' else 1)],
                        clock=clock,arrays=1<<bank,period=250)
    orders=dict(ascending=(0,1,2,3,4,5),descending=(5,4,3,2,1,0),
        rotated=(3,4,5,0,1,2),gray=(0,2,3,1,5,4),permuted=(2,5,1,4,0,3))
    for name,order in orders.items():
        for ref in (False,True):
            add(f'order_{name}_ref{int(ref)}','order',
                [Step(a,5,100) for a in order]+[Step(7 if ref else order[-1],5,100,0,1,0)],period=710)
    for d in (1,5,50):
        for arrays in (1,2,3):
            for first,order in ((0,228),(1,228),(0,27)):
                if arrays!=3 and(first!=0 or order!=228):continue
                add(f'readorder_{d}_banks{arrays}_first{first}_order{order}','readorder',
                    [Step(a,d,100) for a in range(6)]+[Step(7,5,100,0,1,0)],
                    arrays=arrays,first_bank=first,order=order,period=710)
        for ref in ('idle','vmid','vmid_adc'):
            ss=[]
            for a in range(6):
                ss.extend([Step(a,d,100,15,1,9),
                    Step(7,5,50,15 if ref=='vmid_adc' else 0,1,10 if ref=='idle' else 8)])
            add(f'reset_control_{ref}_{d}','reset_control',ss,period=920)
    for bank in (0,1):
        for mux in range(4):
            for gap in (1000,10000,100000,1000000):
                add(f'leak_b{bank}_m{mux}_{gap}','leakage',[
                    Step(2,100000,100100,0,1,0),Step(2,gap,gap+100,0,1,2),
                    Step(2,1,100,1<<mux,8)],arrays=1<<bank,
                    period=gap+100500,warmup_ms=0,window_ms=max(2500,int((gap+100500)*6/1000)))
    for name,ss in (('scan',[Step(a,5,100) for a in range(6)]),
        ('scan_vmid',sum(([Step(a,5,100),Step(7,5,50,15,1,0)] for a in range(6)),[])),
        ('held',[Step(2,5,100)])):
        add('long_'+name,'long_noise',ss,period=1000,warmup_ms=2000,window_ms=120000,repetitions=1)
    # Adaptive controls: bracket the nominal equal-time endpoint by one extra us.
    # The running first matrix has its own frozen in-memory plan; execute these
    # afterward with --only recovery_bracket known_vmid.
    for bank in (0,1):
        for mux in range(4):
            for clock,delay in ((16000000,1),(16000000,5),(16000000,50),(8000000,1),(8000000,5)):
                extra=7*(16000000//clock)+1
                add(f'recovery_b{bank}_m{mux}_{clock}_{delay}_wait_plus','recovery_bracket',
                    [Step(7,100,120,0,1,0),Step(2,delay+extra,100,1<<mux,1)],
                    clock=clock,arrays=1<<bank,period=250)
    for delay in (1,5,50):
        for which in ('held6','held7','toggle67'):
            aa=(6,7) if which=='toggle67' else (6,) if which=='held6' else (7,)
            add(f'known_{which}_{delay}_banks3','known_vmid',[Step(a,delay,100,7) for a in aa],period=710)
    for arrays in (1,2):
        add(f'known_toggle67_5_banks{arrays}','known_vmid',[Step(a,5,100,7) for a in (6,7)],arrays=arrays,period=710)
    for delay in (1,5):
        for ref in ('idle','vmid','vmid_adc'):
            ss=[]
            for a in range(6):
                ss.extend([Step(a,delay,25,15,1,9),
                    Step(7,5,20,15 if ref=='vmid_adc' else 0,1,10 if ref=='idle' else 8)])
            add(f'reset_short_{ref}_{delay}','reset_short',ss,period=300)
    # Address 4 has a recurring peak in several scan orders; check it statically.
    add('held_a4_mask15','held_extra',[Step(4,5,100)],period=710)
    for name,aa in (('full_descending',(6,5,4,3,2,1,0)),('full_rotated',(3,4,5,6,0,1,2)),('core_gray',(0,2,3,1,5,4))):
        add('fast_order_'+name,'fast_order',[Step(a,5,mask=8 if a==6 else 15) for a in aa])
    for arrays,first,repeat in ((1,0,8),(2,0,8),(3,0,8),(3,1,8),(1,0,1),(2,0,1),(3,0,1)):
        add(f'refburst_banks{arrays}_first{first}_n{repeat}','refburst',
            [Step(7,100,120,0,1,0),Step(7,1,100,1,repeat)],arrays=arrays,first_bank=first,period=250)
    assert len({p.name for p in out})==len(out)
    return out

def status(proto):
    from testboard_adc124_benchmark import parse_status
    s=parse_status(proto.command('status'))
    if s.get('firmware_revision') not in ('adc124-pcbdiag-v1','adc124-pcbdiag-v2') or s.get('protocol_version')!='pcbdiag1':
        raise RuntimeError('Unexpected diagnostic firmware')
    for k in ('transfer_errors','data_errors','usb_write_errors'):
        if int(s[k]):raise RuntimeError(f'Diagnostic fault: {k}={s[k]}')
    return s

def capture(port,proto,p,rep,root):
    folder=root/'captures';folder.mkdir(exist_ok=True)
    stem=folder/f'{p.name}_r{rep}'
    if stem.with_suffix('.json').exists():
        old=json.loads(stem.with_suffix('.json').read_text())
        if old.get('state')=='complete':return
        raise RuntimeError(f'Existing incomplete capture {stem}')
    proto.command('stop');proto.command('clear')
    proto.command(f'config {p.clock} {p.arrays} {p.first_bank} {p.order} {p.period}')
    for s in p.steps:proto.command(s.command())
    meta=dict(state='starting',program=asdict(p),rep=rep,labels=p.labels()[0])
    stem.with_suffix('.json').write_text(json.dumps(meta,indent=2))
    duration=p.warmup_ms+p.window_ms
    port.write(f'run {duration}*'.encode());port.flush()
    chunks=[];start=time.monotonic();last=start
    finish_after=duration/1000+completion_wait_s(p)
    while time.monotonic()-start<finish_after+2:
        b=port.read(max(1,min(port.in_waiting,262144)))
        if b:chunks.append(b);last=time.monotonic()
        elif time.monotonic()-start>finish_after and time.monotonic()-last>.2:break
    data=b''.join(chunks);stem.with_suffix('.bin').write_bytes(data)
    final=status(proto)
    parsed=decode(data,p)
    n=len(parsed['sequence'])
    if final['running']!='false' or n!=int(final['sent']) or int(final['cycles'])!=n+int(final['dropped']):
        raise RuntimeError('Incomplete diagnostic accounting')
    if int(parsed['sequence'][-1])>=int(final['cycles']):
        raise RuntimeError('Out-of-range diagnostic sequence')
    if int(final['overruns']):raise RuntimeError(f'Controlled timing overrun: {final}')
    np.savez_compressed(stem.with_suffix('.npz'),**parsed)
    meta.update(state='complete',status=final,frames=n,first_received_sequence=int(parsed['sequence'][0]),
        last_received_sequence=int(parsed['sequence'][-1]),raw_sha256=hashlib.sha256(data).hexdigest())
    stem.with_suffix('.json').write_text(json.dumps(meta,indent=2))
    print(f'{p.family}: {p.name} r{rep}: {n} cycles, dropped={final["dropped"]}',flush=True)

def run(root,only=None,smoke=False):
    import serial
    from testboard_adc124_benchmark import SerialProtocol
    ps=programs()
    if only:ps=[p for p in ps if p.family in only]
    if smoke:ps=[Program('smoke','smoke',(Step(0,5,100),Step(7,5,50,15,1,0)),period=200,warmup_ms=0,window_ms=1000,repetitions=1)]
    (root/'plan.json').write_text(json.dumps([asdict(p) for p in programs()],indent=2))
    with (root/'commands.log').open('a',encoding='utf-8') as log, serial.Serial('COM4',460800,timeout=.01,write_timeout=3) as port:
        proto=SerialProtocol(port,log);proto.stop_and_drain();status(proto)
        try:
            for rep in (1,2,3):
                # Reverse second repetition to expose time/order drift.
                for p in (ps[::-1] if rep==2 else ps):
                    if rep<=p.repetitions:capture(port,proto,p,rep,root)
        finally:proto.stop_and_drain()

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--root',type=Path,default=RAW)
    a.add_argument('--only',nargs='+');a.add_argument('--smoke',action='store_true');a.add_argument('--plan',action='store_true')
    args=a.parse_args()
    if args.plan:
        from collections import Counter
        ps=programs();print(Counter(p.family for p in ps));print('Programs',len(ps),'captures',sum(p.repetitions for p in ps))
        print('Acquisition minutes',sum(p.repetitions*(p.warmup_ms+p.window_ms) for p in ps)/60000)
    else:run(args.root,args.only,args.smoke)
