"""All-route connected pattern audit, full-time envelopes, spike zooms and PSD."""
from pathlib import Path
import argparse,json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from analyze_adc124_upgrade import ROOT,ROUTES,LSB
from analyze_adc124_scan_modes import csv_write
from analyze_adc124_noise_patterns import spectrum
from capture_adc124_settling import decode
from report_adc124_settling import canonical_samples


def build(folder):
    data=json.loads((folder/'analysis.json').read_text())
    assert not data['partial']
    reps=data['raw_channel_repetitions'];summary=data['raw_channels'];audit=[];selected=[];figures=[];loaded={};coverage=[];psd_rows=[]
    names=[str(r) for r in ROUTES]
    reference=json.loads(Path(data['reference_analysis']).read_text())
    roots={'connected':Path(data['raw_root'])/'production','disconnected':ROOT/reference['raw_root']/'production'}
    def identity(r):return tuple(r[k] for k in ('mode','vmid','settle_us','vmid_settle_effective_us','requested_spi_clock_hz','route'))
    def load(dataset,case,rep):
        mode,vmid,delay,vd,clock,route=case;ident=(dataset,mode,vmid,delay,vd,clock,rep)
        if ident not in loaded:
            name=f"arrayboth__{mode}__lpspi__spi{clock}__repeat1__settle{delay}__vmid{'on' if vmid else 'off'}__burst__vmidsettle{vd}__rep{rep}"
            root=roots[dataset]/name;f=decode(root/'capture.bin');status=json.loads((root/'status.json').read_text())['after']
            t=np.r_[0,np.cumsum(np.diff(f['start'].astype(np.int64))%2**32)]/1e6;mask=(t>=1)&(t<16)
            loaded[ident]=(t[mask]-1,canonical_samples(f,status)[mask]);tt=loaded[ident][0];dt=np.diff(tt)
            edges=np.r_[0,np.flatnonzero(dt>1.5*np.median(dt))+1,len(tt)]
            eligible=sum(b-a for a,b in zip(edges[:-1],edges[1:]) if b-a>=4096)
            windowed=sum(4096+2048*((b-a-4096)//2048) for a,b in zip(edges[:-1],edges[1:]) if b-a>=4096)
            coverage.append(dict(dataset=dataset,mode=mode,vmid=vmid,settle_us=delay,vmid_settle_us=vd,requested_spi_clock_hz=clock,repetition=rep,
                frames=len(tt),eligible_frames=eligible,windowed_frames=windowed,eligible_fraction=eligible/len(tt),windowed_fraction=windowed/len(tt)))
        return loaded[ident]
    for delay in (1,5):
        for route in ROUTES:
            case=('mux',False,delay,delay,16000000,str(route))
            rr=[r for r in reps if r['dataset']=='connected' and identity(r)==case]
            assert len(rr)==3
            sd=float(np.median([r['sd_counts'] for r in rr]));pattern=sum(r['max_abs_acf_lags1_64']>=.05 for r in rr)
            old=next(r for r in summary if r['dataset']=='disconnected' and identity(r)==case)
            item=dict(settle_us=delay,route=str(route),array=route.array,mux=route.mux,external_address=route.input,sensor='PZT'+str(route.sensor),
                sensor_channel=route.sensor_channel,sd_counts=sd,sd_volts=sd*LSB,sd_min_counts=min(r['sd_counts'] for r in rr),
                sd_max_counts=max(r['sd_counts'] for r in rr),pattern_repeats=pattern,lag1_acf=float(np.median([r['lag1_acf'] for r in rr])),
                disconnected_sd_counts=old['sd_counts'],selected=bool(sd>.5 and pattern>=2))
            audit.append(item)
            if item['selected']:selected.append(item)
    plt.rcParams.update({'font.size':8,'axes.grid':True,'grid.alpha':.2,'axes.spines.top':False})
    figdir=folder/'figures';figdir.mkdir(exist_ok=True)
    def plot_cases(cases,name,title):
        fig,axes=plt.subplots(len(cases),3,figsize=(12,5.3 if len(cases)==2 else 3.2),squeeze=False,constrained_layout=True)
        for row,case in enumerate(cases):
            mode,vmid,delay,vd,clock,route=case;j=names.index(route)
            for dataset,rep,col,label in [('disconnected',1,'#999999','Disconnected rep 1')]+[('connected',rep,col,'Connected rep '+str(rep)) for rep,col in zip((1,2,3),('#0072B2','#D55E00','#009E73'))]:
                t,x=load(dataset,case,rep);z=x[:,j];median=np.median(z)
                edges=np.linspace(0,len(t),601,dtype=int)
                mids=np.array([np.mean(t[a:b]) for a,b in zip(edges[:-1],edges[1:])]);lo=np.array([z[a:b].min() for a,b in zip(edges[:-1],edges[1:])])-median
                hi=np.array([z[a:b].max() for a,b in zip(edges[:-1],edges[1:])])-median
                axes[row,0].fill_between(mids,lo,hi,color=col,alpha=.18,lw=.3,label=label)
                center=float(t[np.argmax(np.abs(z-median))]);start=float(np.clip(center-.0125,0,t[-1]-.025));clip=(t>=start)&(t<start+.025)
                axes[row,1].plot((t[clip]-start)*1000,z[clip]-median,'.-',ms=2,lw=.6,color=col,alpha=.8,label=label+f' @{start:.3f}s')
                f,p,fs,_=spectrum(t,z);axes[row,2].semilogy(f[1:],np.maximum(p[1:],1e-16),color=col,lw=.7,label=label)
                psd_rows.extend(dict(dataset=dataset,mode=mode,vmid=vmid,settle_us=delay,vmid_settle_us=vd,requested_spi_clock_hz=clock,route=route,
                    repetition=rep,frequency_hz=float(ff),psd_counts2_per_hz=float(pp),psd_volts2_per_hz=float(pp)*LSB**2) for ff,pp in zip(f,p))
            cur=next(r for r in summary if r['dataset']=='connected' and identity(r)==case)
            axes[row,0].set(title=f"{mode.upper()} {delay}/{vd} us / VMID {'on' if vmid else 'off'} / {route}",xlabel='Time after warm-up (s)',ylabel='Deviation (counts)')
            axes[row,1].set(title=f"Largest-excursion zoom / SD {cur['sd_counts']:.3f} counts",xlabel='Time within each 25 ms window (ms)',ylabel='Deviation (counts)')
            axes[row,2].set(title='Unfiltered Welch PSD',xlabel='Frequency (Hz)',ylabel='Counts squared / Hz')
            for ax in axes[row]:ax.legend(fontsize=5.5)
        fig.suptitle(title,fontsize=11);path=figdir/(name+'.png');fig.savefig(path,dpi=180);plt.close(fig)
        figures.append(dict(title=title,path=str(path.relative_to(folder)).replace('\\','/'),height=335 if len(cases)==2 else 202,
            caption='All three connected repetitions plus disconnected repetition 1. Left: min-max envelope of 600 display bins over the full 15 s, preserving sampled extremes. Middle: raw 25 ms around each record\'s own largest deviation; legends identify separate elapsed-time starts. Right: 4096-sample Hann Welch PSD, 50% overlap, means removed; split timestamp gaps >1.5 median periods, exclude sections shorter than 4096 and unused tails. No quiet mask or sample clipping. Frequency axis uses approximate sweep timing; counts squared/Hz converts to V squared/Hz by (3.3/4096) squared.'))
    for delay in (1,5):
        cases=[('mux',False,delay,delay,16000000,r['route']) for r in selected if r['settle_us']==delay]
        for start in range(0,len(cases),2):plot_cases(cases[start:start+2],f'selected_{delay}us_{start//2+1}',f'Selected temporal structure - MUX / VMID off / {delay} us')
    below=[r for r in audit if not r['selected'] and r['pattern_repeats']>=2]
    for delay in (1,5):
        cases=[('mux',False,delay,delay,16000000,r['route']) for r in below if r['settle_us']==delay]
        for start in range(0,len(cases),2):plot_cases(cases[start:start+2],f'low_amplitude_{delay}us_{start//2+1}',f'Below 0.5 count SD - correlated MUX / VMID off / {delay} us')
    examples=[]
    # Keep representative worst cases even when the >0.5-count screen selects none.
    for mode in ('adc','mux'):
        for vmid in (False,True):
            chosen=max((r for r in summary if r['dataset']=='connected' and r['mode']==mode and r['vmid']==vmid and r['settle_us']==5
                and r['vmid_settle_effective_us']==5 and r['requested_spi_clock_hz']==16000000),key=lambda r:r['sd_counts'])
            examples.append(identity(chosen))
    for start in (0,2):plot_cases(examples[start:start+2],f'representative_{start//2+1}','Representative worst-SD routes - sensor = VMID 5 us / 16 MHz')
    extremes=[]
    for mode in ('adc','mux'):
        chosen=max((r for r in summary if r['dataset']=='connected' and r['mode']==mode),key=lambda r:r['sd_counts'])
        extremes.append(identity(chosen))
    plot_cases(extremes,'extreme_noise_cases','Largest observed SD in each scan mode - full matrix')
    peaks=[]
    for mode in ('adc','mux'):
        peak=max((r for r in reps if r['dataset']=='connected' and r['mode']==mode),key=lambda r:r['ptp_counts'])
        peaks.append(identity(peak))
    plot_cases(peaks,'largest_excursions','Largest recorded p-p excursion in each scan mode - all repetitions')
    adc5=max((r for r in reps if r['dataset']=='connected' and r['mode']=='adc' and not r['vmid'] and r['settle_us']==5
        and r['vmid_settle_effective_us']==5 and r['requested_spi_clock_hz']==16000000),key=lambda r:r['ptp_counts'])
    if adc5['ptp_counts']>2 and identity(adc5) not in peaks:
        plot_cases([identity(adc5)],'adc5_excursion','Detected ADC-scan excursion - VMID off / 5 us / 16 MHz')
    psd_rows=list({tuple(r[k] for k in ('dataset','mode','vmid','settle_us','vmid_settle_us','requested_spi_clock_hz','route','repetition','frequency_hz')):r for r in psd_rows}.values())
    csv_write(folder/'selected_noise_audit.csv',audit);csv_write(folder/'noise_psd.csv',psd_rows);csv_write(folder/'noise_psd_coverage.csv',coverage)
    result=dict(selected=selected,correlated_below_threshold=below,audit=audit,figures=figures,checked_route_settings=100,
        method='All 50 MUX/LPSPI/16 MHz/VMID-off routes checked separately at 1 and 5 us. Selection: median unfiltered SD >0.5 counts and maximum absolute ACF at lags 1-64 >=0.05 in at least two of three repetitions. Descriptive screen only; no causal attribution.')
    (folder/'selected_noise_current.json').write_text(json.dumps(result,indent=2));print('Selected',len(selected),'of 100 route/settings',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('folder',type=Path);a=p.parse_args();build(a.folder.resolve())
