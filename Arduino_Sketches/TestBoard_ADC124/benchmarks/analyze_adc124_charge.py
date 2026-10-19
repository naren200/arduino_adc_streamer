"""Cold-start threshold measurements without extrapolating missing crossings."""
from pathlib import Path
import argparse, hashlib, json
import numpy as np
from capture_adc124_settling import decode
from report_adc124_settling import canonical_samples
from analyze_adc124_upgrade import ROUTES, LSB, csv_write


def sustained_crossing(t, z, threshold, hold_s=.002):
    """First upward crossing maintained for hold_s; missing initial edge is censored.

    The raw first sample is checked separately by measure_charge. A plateau
    at the threshold counts as reached. Interpolation is between observed
    points only; gaps exceeding three median sample intervals are rejected.
    """
    t=np.asarray(t); z=np.asarray(z)
    if z[0]>=threshold: return None, 'left_censored'
    above=z>=threshold
    starts=np.flatnonzero(above & ~np.r_[False,above[:-1]])
    ends=np.flatnonzero(above & ~np.r_[above[1:],False])
    period=float(np.median(np.diff(t)))
    for first,last in zip(starts,ends):
        if t[last]-t[first]<hold_s: continue
        confirm=int(np.searchsorted(t,t[first]+hold_s))
        if np.any(np.diff(t[max(0,first-1):confirm+1])>3*period): continue
        a=first-1
        crossing=t[a]+(threshold-z[a])*(t[first]-t[a])/(z[first]-z[a])
        return float(crossing),'observed'
    return None,'not_reached'


def measure_charge(t,z,baseline=None,hold_s=.002):
    """Absolute B/3 and 2B/3, not fractions of B minus initial voltage."""
    t=np.asarray(t,dtype=float);z=np.asarray(z,dtype=float)
    if len(t)<3 or t.shape!=z.shape or np.any(np.diff(t)<=0):raise ValueError('Invalid charge trace')
    tail=t>=max(t[0],t[-1]-10)
    b=float(np.median(z[tail])) if baseline is None else float(baseline)
    sd=float(z[tail].std(ddof=1))
    result=dict(initial_counts=float(z[0]),baseline_counts=b,baseline_volts=b*LSB,tail_sd_counts=sd,
        threshold_one_third_counts=b/3,threshold_two_thirds_counts=2*b/3,
        crossing_hold_s=hold_s,time_resolution_s=float(np.median(np.diff(t))))
    if b<=0:raise ValueError('Positive final baseline required')
    for name,threshold in [('one_third',b/3),('two_thirds',2*b/3)]:
        time,state=sustained_crossing(t,z,threshold,hold_s)
        result['t_'+name+'_s']=time;result[name+'_status']=state
        index=None if time is None else int(np.searchsorted(t,time))
        result['t_'+name+'_lower_s']=None if index is None else float(t[max(0,index-1)])
        result['t_'+name+'_upper_s']=None if index is None else float(t[index])
    a=result['t_one_third_s'];c=result['t_two_thirds_s']
    result['one_third_to_two_thirds_s']=None if a is None or c is None else c-a
    result['interval_lower_s']=None if a is None or c is None else max(0.,result['t_two_thirds_lower_s']-result['t_one_third_upper_s'])
    result['interval_upper_s']=None if a is None or c is None else result['t_two_thirds_upper_s']-result['t_one_third_lower_s']
    for field,tolerance in [('baseline',max(.01*b,3*sd,2)),('noise_band',max(3*sd,2))]:
        inside=np.abs(z-b)<=tolerance
        starts=np.flatnonzero(inside & ~np.r_[False,inside[:-1]])
        ends=np.flatnonzero(inside & ~np.r_[inside[1:],False])
        stable=next((float(t[a]) for a,c in zip(starts,ends) if t[c]-t[a]>=2),None)
        result[field+'_tolerance_counts']=tolerance
        result['first_two_seconds_within_'+('baseline' if field=='baseline' else 'noise_band')+'_s']=stable
    halfway=(t[-1]+max(t[0],t[-1]-10))/2
    result['tail_half_drift_counts']=float(np.median(z[t>=halfway])-np.median(z[tail & (t<halfway)]))
    return result


def analyze(source,output):
    output.mkdir(parents=True,exist_ok=True)
    session=json.loads((source/'session.json').read_text())
    if session['state']!='complete' or len(session['runs'])!=1:raise ValueError('Complete startup capture required')
    run=session['runs'][0];folder=source/run['name'];raw=folder/'capture.bin'
    if hashlib.sha256(raw.read_bytes()).hexdigest()!=run['sha256']:raise ValueError('Startup hash mismatch')
    f=decode(raw);status=json.loads((folder/'status.json').read_text())['after']
    t=np.r_[0,np.cumsum(np.diff(f['start'].astype(np.int64))%2**32)]/1e6
    x=canonical_samples(f,status);rows=[]
    for i,r in enumerate(ROUTES):
        row=measure_charge(t,x[:,i])
        row.update(route=str(r),array=r.array,mux=r.mux,sensor='PZT'+str(r.sensor),sensor_channel=r.sensor_channel,bias_ohms=r.bias_ohms)
        rows.append(row)
    # Preserve the early raw sweeps; decimate only the long plateau for plots.
    selected=np.flatnonzero((t<=1)|(np.arange(len(t))%100==0))
    np.savez_compressed(output/'charge_plot_data.npz',t=t[selected],x=x[selected])
    result=dict(source=str(source),session=session,run=run,channels=rows,duration_s=float(t[-1]),
        measured_intervals=sum(r['one_third_to_two_thirds_s'] is not None for r in rows),
        method='Raw samples, absolute thresholds B/3 and 2B/3, sustained 2 ms; linear interpolation. B = median final 10 s. Time zero = first recorded sweep, not USB power edge. Warmup: first 2 s inside max(1% B, 3 tail SD, 2 counts), plus stricter max(3 tail SD, 2 counts) noise-band arrival.')
    (output/'charge_analysis.json').write_text(json.dumps(result,allow_nan=False,indent=2))
    csv_write(output/'charge_channels.csv',rows)
    print('Charge intervals measured:',result['measured_intervals'],'of',len(rows),flush=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path);p.add_argument('output',type=Path)
    a=p.parse_args();analyze(a.source.resolve(),a.output.resolve())
