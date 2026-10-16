"""Analyze new VMID holds without treating transient conversion sequences as noise."""
from pathlib import Path
from collections import defaultdict
from functools import lru_cache
import argparse,hashlib,json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from benchmark_adc124_long_vmid import RAW,OUT,plan
from analyze_pcb_diagnostics import steady,csv_write,LSB

def analyze(raw=RAW,out=OUT):
    raw,out=Path(raw),Path(out);out.mkdir(parents=True,exist_ok=True)
    figdir=out/'figures';figdir.mkdir(exist_ok=True)
    @lru_cache(maxsize=None)
    def load(name,rep):
        f=raw/'captures'/f'{name}_r{rep}.json'
        if not f.exists():return None
        m=json.loads(f.read_text())
        if m['state']!='complete':return None
        data=f.with_suffix('.bin').read_bytes()
        assert hashlib.sha256(data).hexdigest()==m['raw_sha256']
        with np.load(f.with_suffix('.npz')) as z:a={k:z[k] for k in z.files}
        assert len(a['sequence'])==m['frames']==int(m['status']['sent'])
        assert int(m['status']['cycles'])==m['frames']+int(m['status']['dropped'])
        assert not any(int(m['status'][k]) for k in ('overruns','transfer_errors','data_errors','usb_write_errors'))
        return m,a
    captures=[];full=[];ten=[];scans=[];holds=[]
    pairs,ps=plan()
    names=[p[s].name for p in pairs for s in ('before','after')]+[p.name for p in ps]
    for name in names:
        for rep in (1,2,3):
            item=load(name,rep)
            if item is None:continue
            m,z=item;s=m['status'];dt=np.diff(z['timing'][:,0].astype(np.int64))&0xffffffff
            captures.append(dict(name=name,family=m['program']['family'],rep=rep,frames=m['frames'],cycles=int(s['cycles']),
                dropped=int(s['dropped']),overruns=int(s['overruns']),transfer_errors=int(s['transfer_errors']),data_errors=int(s['data_errors']),usb_write_errors=int(s['usb_write_errors']),
                period_median_us=float(np.median(dt)) if len(dt) else None,
                acquisition_median_us=float(np.median((z['timing'][:,1].astype(np.int64)-z['timing'][:,0])&0xffffffff)),
                actual_hold_us=m.get('actual_hold_us'),raw_sha256=m['raw_sha256']))
    for pair in pairs:
        for rep in (1,2,3):
            b=load(pair['before'].name,rep);a=load(pair['after'].name,rep)
            if b is None or a is None:continue
            bm,bz=b;am,az=a;t,keep=steady(bm,bz)
            before=bz['values'][keep].astype(float);med=np.median(before,axis=0)
            ix={(r['array'],r['mux'],r['address']):i for i,r in enumerate(bm['labels'])}
            actual_gap=((int(az['timing'][0,0])-int(bz['timing'][-1,1]))&0xffffffff)/1e6
            missing_tail=int(bm['status']['cycles'])-1-int(bz['sequence'][-1])
            holds.append(dict(pair=pair['key'],rep=rep,timed_hold_s=am['actual_hold_us']/1e6,
                last_received_pre_end_to_post_start_s=actual_gap,before_unreceived_tail_cycles=missing_tail))
            assert am['frames']==1 and am['actual_hold_us']>=20000000
            for j,r in enumerate(am['labels']):
                col=ix[(r['array'],r['mux'],r['address'])];v=float(az['values'][0,j]);base=float(med[col])
                row=dict(pair=pair['key'],rep=rep,array=r['array'],mux=r['mux'],address=r['address'],conversion=r['repeat']+1,
                    first_bank=am['program']['first_bank'],before_steady_baseline_counts=base,before_steady_baseline_volts=base*LSB,
                    before_last_received_code=float(bz['values'][-1,col]),after_counts=v,after_volts=v*LSB,
                    delta_from_steady_counts=v-base,delta_from_steady_volts=(v-base)*LSB,
                    before_steady_sd_counts=float(np.std(before[:,col],ddof=1)),
                    timed_hold_s=am['actual_hold_us']/1e6,pre_end_to_post_start_s=actual_gap)
                (full if pair['key']=='full' else ten).append(row)
    for p in ps:
        for rep in (1,2,3):
            item=load(p.name,rep)
            if item is None:continue
            m,z=item;t,keep=steady(m,z);v=z['values'][keep].astype(float)
            dt=np.diff(z['timing'][:,0].astype(np.int64))&0xffffffff
            for j,r in enumerate(m['labels']):
                sd=float(np.std(v[:,j],ddof=1));pp=float(np.ptp(v[:,j]));base=float(np.median(v[:,j]))
                tt=t[keep];drift=float(np.median(v[tt>=tt[-1]-2,j])-np.median(v[tt<tt[0]+2,j]))
                scans.append(dict(name=p.name,rep=rep,vmid_wait_us=p.steps[1].wait,array=r['array'],mux=r['mux'],address=r['address'],
                    samples=len(v),baseline_counts=base,baseline_volts=base*LSB,sd_counts=sd,sd_volts=sd*LSB,
                    ptp_counts=pp,ptp_volts=pp*LSB,drift_last_minus_first_counts=drift,drift_last_minus_first_volts=drift*LSB,
                    period_median_us=float(np.median(dt)),sweeps_per_second=1e6/float(np.median(dt))))
    def aggregate(rr,keys,fields):
        groups=defaultdict(list)
        for r in rr:groups[tuple(r[k] for k in keys)].append(r)
        result=[]
        for k,rs in sorted(groups.items()):
            row=dict(zip(keys,k));row['repetitions']=len(rs)
            for f in fields:row[f]=float(np.median([r[f] for r in rs]))
            if 'after_counts' in fields:
                row['after_min_rep_counts']=min(r['after_counts'] for r in rs);row['after_max_rep_counts']=max(r['after_counts'] for r in rs)
            if 'ptp_counts' in fields:
                row['ptp_counts_max_rep']=max(r['ptp_counts'] for r in rs)
                row['sd_counts_max_rep']=max(r['sd_counts'] for r in rs)
                row['absolute_drift_counts_max_rep']=max(abs(r['drift_last_minus_first_counts']) for r in rs)
            result.append(row)
        return result
    fields=['before_steady_baseline_counts','before_steady_baseline_volts','before_last_received_code','after_counts','after_volts','delta_from_steady_counts','delta_from_steady_volts']
    full_agg=aggregate(full,['array','mux','address'],fields)
    ten_agg=aggregate(ten,['array','mux','first_bank','conversion'],fields)
    scan_agg=aggregate(scans,['vmid_wait_us','array','mux','address'],['baseline_counts','baseline_volts','sd_counts','sd_volts','ptp_counts','ptp_volts',
        'drift_last_minus_first_counts','drift_last_minus_first_volts','samples','period_median_us','sweeps_per_second'])
    full_mux=[];ten_end=[];scan_mux=[]
    for array in (1,2):
        for mux in range(1,5):
            rr=[r for r in full_agg if r['array']==array and r['mux']==mux and r['address']<6]
            if rr:full_mux.append(dict(array=array,mux=mux,before_center_counts=float(np.median([r['before_steady_baseline_counts'] for r in rr])),
                after_center_counts=float(np.median([r['after_counts'] for r in rr])),median_route_change_counts=float(np.median([r['delta_from_steady_counts'] for r in rr])),
                min_route_change_counts=min(r['delta_from_steady_counts'] for r in rr),max_route_change_counts=max(r['delta_from_steady_counts'] for r in rr)))
            for first in (0,1):
                rr=[r for r in ten_agg if r['array']==array and r['mux']==mux and r['first_bank']==first]
                if len(rr)!=10:continue
                one=next(r for r in rr if r['conversion']==1);last=next(r for r in rr if r['conversion']==10)
                ten_end.append(dict(array=array,mux=mux,first_bank=first,before_baseline_counts=one['before_steady_baseline_counts'],
                    first_counts=one['after_counts'],tenth_counts=last['after_counts'],tenth_minus_first_counts=last['after_counts']-one['after_counts'],
                    first_delta_counts=one['delta_from_steady_counts'],tenth_delta_counts=last['delta_from_steady_counts']))
            for wait in (1000,5000,10000):
                rr=[r for r in scan_agg if r['array']==array and r['mux']==mux and r['vmid_wait_us']==wait and r['address']<6]
                if len(rr)!=6:continue
                vals=[r['baseline_counts'] for r in rr]
                scan_mux.append(dict(array=array,mux=mux,vmid_wait_us=wait,baseline_center_counts=float(np.median(vals)),
                    address_spread_counts=max(vals)-min(vals),median_route_sd_counts=float(np.median([r['sd_counts'] for r in rr])),
                    max_route_ptp_counts=max(r['ptp_counts_max_rep'] for r in rr),max_absolute_drift_counts=max(r['absolute_drift_counts_max_rep'] for r in rr),
                    samples_per_record_median=rr[0]['samples'],
                    period_median_us=rr[0]['period_median_us'],sweeps_per_second=rr[0]['sweeps_per_second']))
    result=dict(partial=len(captures)!=63,expected_captures=63,captures=captures,raw_root=str(raw),
        lifecycle=json.loads((raw/'lifecycle.json').read_text()),holds=holds,full_routes=full_agg,full_mux=full_mux,ten_readings=ten_agg,ten_endpoints=ten_end,
        scan_routes=scan_agg,scan_mux=scan_mux)
    for name,rr in [('captures',captures),('hold_intervals',holds),('full_cycle_repetitions',full),('full_cycle_routes',full_agg),('full_cycle_mux',full_mux),
        ('ten_conversion_repetitions',ten),('ten_conversion_readings',ten_agg),('ten_conversion_endpoints',ten_end),('long_scan_repetitions',scans),('long_scan_routes',scan_agg),('long_scan_mux',scan_mux)]:
        csv_write(out/(name+'.csv'),rr)
    (out/'analysis.json').write_text(json.dumps(result,indent=2))
    plt.rcParams.update({'font.size':8,'axes.grid':True,'grid.alpha':.2,'axes.spines.top':False})
    def save(fig,name):
        for ax in fig.axes:
            if ax.get_yscale()=='linear':ax.ticklabel_format(axis='y',useOffset=False,style='plain')
        fig.savefig(figdir/(name+'.png'),dpi=190,bbox_inches='tight');plt.close(fig)
    if full_agg:
        fig,axs=plt.subplots(2,4,figsize=(12.6,5.4),constrained_layout=True)
        for a in (1,2):
            for m in range(1,5):
                ax=axs[a-1,m-1];rr=[r for r in full_agg if r['array']==a and r['mux']==m];xx=[r['address'] for r in rr]
                ax.plot(xx,[r['before_steady_baseline_counts'] for r in rr],'o-',label='Before: steady baseline')
                ax.plot(xx,[r['before_last_received_code'] for r in rr],'s--',label='Before: last received cycle',ms=3)
                ax.plot(xx,[r['after_counts'] for r in rr],'o-',color='tab:red',label='After: first cycle')
                ax.fill_between(xx,[r['after_min_rep_counts'] for r in rr],[r['after_max_rep_counts'] for r in rr],color='tab:red',alpha=.12)
                ax.set(title=f'Array {"AB"[a-1]} / MUX {m} / ADC IN{m}',xlabel='External MUX address',ylabel='Code (counts)',xticks=xx)
        handles,labels=axs[0,0].get_legend_handles_labels();fig.legend(handles,labels,loc='outside lower center',ncol=3)
        save(fig,'hold_full_cycle')
    if ten_agg:
        fig,axs=plt.subplots(2,4,figsize=(12.6,5.4),constrained_layout=True)
        for a in (1,2):
            for m in range(1,5):
                ax=axs[a-1,m-1]
                for first,color in ((0,'tab:blue'),(1,'tab:orange')):
                    rr=[r for r in ten_agg if r['array']==a and r['mux']==m and r['first_bank']==first]
                    if not rr:continue
                    ax.plot([r['conversion'] for r in rr],[r['after_counts'] for r in rr],'o-' if first==0 else 's-',color=color,label=f'After hold; ADC {"AB"[first]} first',ms=3)
                    ax.fill_between([r['conversion'] for r in rr],[r['after_min_rep_counts'] for r in rr],[r['after_max_rep_counts'] for r in rr],color=color,alpha=.1)
                    ax.axhline(rr[0]['before_steady_baseline_counts'],ls='--',color=color,label=f'Before baseline; ADC {"AB"[first]} first')
                ax.set(title=f'Array {"AB"[a-1]} / MUX {m} / ADC IN{m}',xlabel='Conversion of fixed ADC input',ylabel='Code (counts)',xticks=(1,2,4,6,8,10))
        handles,labels=axs[0,0].get_legend_handles_labels();fig.legend(handles,labels,loc='outside lower center',ncol=2)
        save(fig,'hold_ten_conversions')
    if scan_mux:
        original=json.loads((Path(__file__).resolve().parents[3]/'docs/testing/results/testboard-adc124/pcb_diagnostics_2026-10-15/analysis.json').read_text())
        fig,axs=plt.subplots(2,2,figsize=(12,5.2),constrained_layout=True)
        for a in (1,2):
            for col,field,ylabel in ((0,'baseline_center_counts','Median baseline across addresses 0-5 (counts)'),(1,'median_route_sd_counts','Median temporal route SD (counts)')):
                ax=axs[a-1,col]
                for m in range(1,5):
                    rr=sorted([r for r in scan_mux if r['array']==a and r['mux']==m],key=lambda r:r['vmid_wait_us'])
                    if rr:ax.plot([r['vmid_wait_us']/1000 for r in rr],[r[field] for r in rr],'o-',label=f'MUX {m}')
                ax.set(title=f'Array {"AB"[a-1]}',xlabel='VMID settling delay (ms)',ylabel=ylabel,xticks=(1,5,10));ax.legend(fontsize=7)
        save(fig,'long_vmid_centers_noise')
        fig,axs=plt.subplots(2,4,figsize=(12.6,5.4),constrained_layout=True)
        for a in (1,2):
            for m in range(1,5):
                ax=axs[a-1,m-1]
                for wait in (1000,5000,10000):
                    rr=[r for r in scan_agg if r['array']==a and r['mux']==m and r['vmid_wait_us']==wait]
                    if rr:ax.plot([r['address'] for r in rr],[r['baseline_counts'] for r in rr],'o-',label=f'VMID wait {wait//1000} ms',ms=3)
                old=sorted([r for r in original['channels'] if r['name']=='fast_full_vmid_5' and r['array']==a and r['mux']==m],key=lambda r:r['address'])
                ax.plot([r['address'] for r in old],[r['baseline_counts'] for r in old],color='gray',ls='--',label='Earlier: VMID wait 5 us')
                ax.set(title=f'Array {"AB"[a-1]} / MUX {m}',xlabel='External MUX address',ylabel='Baseline (counts)')
        handles,labels=axs[0,0].get_legend_handles_labels();fig.legend(handles,labels,loc='outside lower center',ncol=4)
        save(fig,'long_vmid_all_baselines')
        fig,axs=plt.subplots(2,4,figsize=(12.6,5.4),constrained_layout=True)
        for wait in (1000,5000,10000):
            item=load(f'long_vmid_{wait}',1)
            if item is None:continue
            meta,z=item;t,keep=steady(meta,z)
            for a in (1,2):
                for m in range(1,5):
                    col=next(i for i,r in enumerate(meta['labels']) if r['array']==a and r['mux']==m and r['address']==2)
                    ax=axs[a-1,m-1];ax.plot(t[keep]-2,z['values'][keep,col],'.-',ms=2,lw=.7,label=f'VMID wait {wait//1000} ms')
                    ax.set(title=f'Array {"AB"[a-1]} / MUX {m} / address 2',xlabel='Analyzed time after exclusion (s)',ylabel='Raw code (counts)')
        handles,labels=axs[0,0].get_legend_handles_labels();fig.legend(handles,labels,loc='outside lower center',ncol=3)
        save(fig,'long_vmid_time_examples')
    print(json.dumps(dict(partial=result['partial'],captures=len(captures),full_routes=len(full_agg),ten_readings=len(ten_agg),scan_routes=len(scan_agg))))
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--raw',type=Path,default=RAW);p.add_argument('--out',type=Path,default=OUT);a=p.parse_args();analyze(a.raw,a.out)
