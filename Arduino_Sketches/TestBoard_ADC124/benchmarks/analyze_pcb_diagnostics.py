"""Analyze complete diagnostic captures; preserve raw noise and physical labels."""
from pathlib import Path
from collections import defaultdict,Counter
import argparse,csv,json
import numpy as np
import pcb_diagnostics as d

OUT=d.ROOT/'docs/testing/results/testboard-adc124/pcb_diagnostics_2026-10-15'
LSB=3.3/4096

def csv_write(path,rows):
    if not rows:return
    keys=list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)

def steady(m,z):
    t=((z['timing'][:,0].astype(np.int64)-int(z['timing'][0,0]))&0xffffffff)/1e6
    return t,t>=m['program']['warmup_ms']/1000

def longest_quiet(mask,sequence):
    """Use one uninterrupted quiet interval, never join different baseline states."""
    indices=np.flatnonzero(mask)
    if not len(indices):raise ValueError('No common quiet samples')
    seq=np.asarray(sequence,dtype=np.int64)
    breaks=(np.diff(indices)!=1)|(np.diff(seq[indices])!=1)
    edges=np.r_[0,np.flatnonzero(breaks)+1,len(indices)]
    k=int(np.argmax(np.diff(edges)))
    selected=np.zeros(len(mask),dtype=bool);selected[indices[edges[k]:edges[k+1]]]=True
    return selected

def burst_window_screen(t,samples):
    """Exclude sustained high-variance seconds; retain isolated small-mass spikes.

    A single route can flag a burst. Gaussian/quantized stationary noise anchors
    its own threshold. Pad each rejected one-second bin by one bin on each side.
    """
    t=np.asarray(t);v=np.asarray(samples,dtype=float)
    bins=np.floor(t-t[0]).astype(int);unique=np.unique(bins)
    sd=np.array([np.std(v[bins==b],axis=0) for b in unique])
    thresholds=np.maximum(.75,4*np.median(sd,axis=0))
    bad=set(int(b) for b in unique[np.any(sd>thresholds,axis=1)])
    padded=bad|{b-1 for b in bad}|{b+1 for b in bad}
    return ~np.isin(bins,list(padded)),dict(version=1,bin_seconds=1,sd_floor_counts=.75,
        sd_multiplier=4,thresholds_counts=thresholds.tolist(),flagged_bins=sorted(bad),padded_bins=sorted(padded))

def analyze(root=d.RAW,out=OUT):
    out.mkdir(parents=True,exist_ok=True)
    channels=[];captures=[];metadata={}
    for f in sorted((root/'captures').glob('*.json')):
        m=json.loads(f.read_text())
        if m['state']!='complete' or m['program']['family']=='smoke':continue
        p=m['program'];name=p['name'];metadata[name]=p
        z=np.load(f.with_suffix('.npz'));t,keep=steady(m,z)
        if not np.any(keep):raise ValueError(f'No steady data: {f}')
        v=z['values'][keep];tm=z['timing'][keep]
        dt=np.diff(tm[:,0].astype(np.int64))&0xffffffff
        cpu=int(m['status']['cpu_hz'])
        event_times=z['events'][keep].astype(float)
        burst_delays=np.median(event_times[:,:,1]-event_times[:,:,0],axis=0)*1e6/cpu
        burst_durations=np.median(event_times[:,:,2]-event_times[:,:,1],axis=0)*1e6/cpu
        captures.append(dict(name=name,family=p['family'],rep=m['rep'],received=m['frames'],
            cycles=int(m['status']['cycles']),dropped=int(m['status']['dropped']),overruns=int(m['status']['overruns']),
            steady_frames=len(v),elapsed_s=float(t[-1]),period_median_us=float(np.median(dt)) if len(dt) else None,
            period_p99_us=float(np.percentile(dt,99)) if len(dt) else None,
            acquisition_median_us=float(np.median((tm[:,1].astype(np.int64)-tm[:,0])&0xffffffff)),
            first_sequence=int(z['sequence'][0]),last_sequence=int(z['sequence'][-1]),
            transfer_errors=int(m['status']['transfer_errors']),data_errors=int(m['status']['data_errors']),
            usb_write_errors=int(m['status']['usb_write_errors'])))
        col=0
        for ev_index in range(event_times.shape[1]):
            lab=m['labels'][col];s=p['steps'][lab['step']]
            n=int(s['mask']).bit_count()*s['repeat']
            for j in range(n):
                route=m['labels'][col+j];values=v[:,col+j].astype(float)
                base=float(np.median(values));sd=float(np.std(values,ddof=1)) if len(values)>1 else 0.
                channels.append(dict(name=name,family=p['family'],rep=m['rep'],**route,
                    bias_ohms=((470000,1000000,470000,1000000),(1000000,470000,1000000,470000))[route['array']-1][route['mux']-1],
                    clock=p['clock'],arrays=p['arrays'],first_bank=p['first_bank'],input_order=p['order'],
                    configured_wait_us=s['wait'],steady_frames=len(values),baseline_counts=base,baseline_volts=base*LSB,
                    mean_counts=float(np.mean(values)),sd_counts=sd,sd_volts=sd*LSB,
                    ptp_counts=float(np.ptp(values)),ptp_volts=float(np.ptp(values))*LSB,
                    min_counts=float(values.min()),max_counts=float(values.max()),
                    burst_start_after_switch_us=float(burst_delays[ev_index]),
                    burst_duration_us=float(burst_durations[ev_index])))
            col+=n
    by=defaultdict(list)
    for r in channels:by[(r['name'],r['step'],r['array'],r['mux'],r['address'],r['repeat'])].append(r)
    aggregate=[]
    fields=('baseline_counts','baseline_volts','mean_counts','sd_counts','sd_volts','ptp_counts','ptp_volts',
            'min_counts','max_counts','burst_start_after_switch_us','burst_duration_us')
    for k,rr in by.items():
        row={key:value for key,value in rr[0].items() if key not in fields and key not in ('rep','steady_frames')}
        row['repetitions']=len(rr)
        for field in fields:row[field]=float(np.median([r[field] for r in rr]))
        row['baseline_rep_range_counts']=max(r['baseline_counts'] for r in rr)-min(r['baseline_counts'] for r in rr)
        row['sd_counts_max_rep']=max(r['sd_counts'] for r in rr)
        row['ptp_counts_max_rep']=max(r['ptp_counts'] for r in rr)
        aggregate.append(row)
    spread=[];g=defaultdict(list)
    for r in aggregate:
        if r['role']=='sensor' and r['repeat']==0:g[(r['name'],r['array'],r['mux'])].append(r)
    for (name,arr,mux),rr in g.items():
        # Equal comparison addresses 0..5. Address 6 retained separately in channels.
        rr=[r for r in rr if r['address']<6]
        if not rr:continue
        v=[r['baseline_counts'] for r in rr]
        spread.append(dict(name=name,family=rr[0]['family'],array=arr,mux=mux,bias_ohms=rr[0]['bias_ohms'],
            baseline_center_counts=float(np.median(v)),baseline_center_volts=float(np.median(v))*LSB,
            address_count=len(v),spread_counts=max(v)-min(v),spread_volts=(max(v)-min(v))*LSB,
            median_sd_counts=float(np.median([r['sd_counts'] for r in rr])),max_sd_counts=max(r['sd_counts'] for r in rr)))
    address=[];g=defaultdict(list)
    for r in aggregate:
        if r['role']=='sensor' and r['repeat']==0:g[(r['name'],r['array'],r['address'])].append(r)
    for (name,arr,a),rr in g.items():
        if len(rr)!=4:continue
        v=[r['baseline_counts'] for r in rr]
        address.append(dict(name=name,array=arr,address=a,spread_counts=max(v)-min(v),spread_volts=(max(v)-min(v))*LSB))
    planned=d.programs();expected=sum(p.repetitions for p in planned)
    result=dict(partial=len(captures)!=expected,expected_captures=expected,raw_root=str(root.resolve()),captures=captures,channels=aggregate,
        channel_repetitions=channels,mux_spreads=spread,address_spreads=address,programs=metadata,
        families=dict(Counter(r['family'] for r in captures)),lifecycle=json.loads((root/'lifecycle.json').read_text()),
        notes=['No quiet-window or spike exclusion: all steady-state raw ADC samples retained.',
            'Timing markers bracket ADC bursts; they are not measurements of analog aperture or GPIO edges.',
            'Diagnostic frame encoding/timestamps add overhead; measured rates are diagnostic rates.',
            'Input order and one-conversion lookahead were validated in a tagged digital model; quiet sensors alone do not validate physical channel identity.',
            'Address 6 is VMID on MUX1-3, sensor on MUX4; software VMID-off still contains reference visits when address 6 is present.',
            'Matched-boundary programs fix seven 100 us slots and a requested 710 us cycle. Boundary phase changes are intentional; sensor-5 exposure differs in hold control.',
            'Recovery uses one ADC input on one bank, fixed sensor/reference slots, and retains eight conversions or waits seven nominal word periods before one conversion.',
            'All leakage gaps use powered MUX isolation; these are reconnection responses, not direct measurements of sensor voltage while disconnected.'])
    for key in ('captures','channels','channel_repetitions','mux_spreads','address_spreads'):
        csv_write(out/(key+'.csv'),result[key])
    (out/'analysis.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(dict(partial=result['partial'],captures=len(captures),expected=expected,
        cycles=sum(r['cycles'] for r in captures),dropped=sum(r['dropped'] for r in captures),families=result['families'])),flush=True)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=d.RAW);p.add_argument('--out',type=Path,default=OUT)
    a=p.parse_args();analyze(a.root,a.out)
