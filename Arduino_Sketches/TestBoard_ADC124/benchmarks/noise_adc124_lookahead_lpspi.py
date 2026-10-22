"""Current 1/5 us route audit plus old/new raw waveform and PSD comparisons."""
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


def build(folder,preview=False):
    data=json.loads((folder/'analysis.json').read_text())
    if data['partial'] and not preview:raise ValueError('Complete analysis is required')
    reps=data['raw_channel_repetitions'];summary=data['raw_channels'];selected=[];audit=[];figures=[];loaded={};psd_rows=[];trace_rows=[]
    names=[str(r) for r in ROUTES];coverage=[]
    def matches(r,dataset,mode,delay,route):
        return (r['dataset'],r['mode'],r['vmid'],r['settle_us'],r['vmid_settle_effective_us'],r['requested_spi_clock_hz'],r['route'])==(dataset,mode,False,delay,delay,16000000,route)
    def load(dataset,mode,delay,rep):
        ident=dataset,mode,delay,rep
        if ident not in loaded:
            root=ROOT/(data['raw_root']+'/production' if dataset=='lookahead' else data['reference_root'])
            name=f'arrayboth__{mode}__lpspi__spi16000000__repeat1__settle{delay}__vmidoff__burst__vmidsettle{delay}__rep{rep}'
            frames=decode(root/name/'capture.bin');status=json.loads((root/name/'status.json').read_text())['after']
            t=np.r_[0,np.cumsum(np.diff(frames['start'].astype(np.int64))%2**32)]/1e6;mask=(t>=1)&(t<16)
            loaded[ident]=t[mask]-1,canonical_samples(frames,status)[mask]
            tt=loaded[ident][0];dt=np.diff(tt);edges=np.r_[0,np.flatnonzero(dt>1.5*np.median(dt))+1,len(tt)]
            eligible=sum(b-a for a,b in zip(edges[:-1],edges[1:]) if b-a>=4096)
            windowed=sum(4096+2048*((b-a-4096)//2048) for a,b in zip(edges[:-1],edges[1:]) if b-a>=4096)
            coverage.append(dict(dataset=dataset,mode=mode,settle_us=delay,repetition=rep,frames=len(tt),eligible_frames=eligible,windowed_frames=windowed,
                eligible_frame_fraction=eligible/len(tt),windowed_frame_fraction=windowed/len(tt)))
        return loaded[ident]
    for delay in (1,5):
        for route in ROUTES:
            rr=[r for r in reps if matches(r,'lookahead','mux',delay,str(route))]
            assert len(rr)==3
            sd=float(np.median([r['sd_counts'] for r in rr]));pattern=sum(r['max_abs_acf_lags1_64']>=.05 for r in rr)
            old=next(r for r in summary if matches(r,'previous','mux',delay,str(route)))
            item=dict(settle_us=delay,array=route.array,mux=route.mux,external_address=route.input,route=str(route),
                sensor='PZT'+str(route.sensor),sensor_channel=route.sensor_channel,sd_counts=sd,sd_volts=sd*LSB,
                sd_min_counts=min(r['sd_counts'] for r in rr),sd_max_counts=max(r['sd_counts'] for r in rr),
                sd_above_repeats=sum(r['sd_counts']>.5 for r in rr),pattern_repeats=pattern,
                lag1_acf=float(np.median([r['lag1_acf'] for r in rr])),previous_sd_counts=old['sd_counts'],selected=bool(sd>.5 and pattern>=2))
            audit.append(item)
            if item['selected']:selected.append(item)
    plt.rcParams.update({'font.size':8.5,'axes.grid':True,'grid.alpha':.2,'axes.spines.top':False})
    figdir=folder/'figures';figdir.mkdir(exist_ok=True)
    def plot_cases(cases,name,title,comparison=False,window_start=0.):
        fig,axes=plt.subplots(len(cases),2,figsize=(12,5.3 if len(cases)==2 else 3.2),squeeze=False,constrained_layout=True)
        for row,(mode,delay,route) in enumerate(cases):
            routeinfo=next(r for r in ROUTES if str(r)==route);j=names.index(route)
            series=[('previous',1,'#777777','Previous / rep 1'),('lookahead',1,'#0072B2','Lookahead / rep 1')] if comparison else [('lookahead',rep,col,'Rep '+str(rep)) for rep,col in zip((1,2,3),('#0072B2','#D55E00','#009E73'))]
            for dataset,rep,col,label in series:
                t,x=load(dataset,mode,delay,rep);z=x[:,j];clip=(t>=window_start)&(t<window_start+.025)
                axes[row,0].plot((t[clip]-window_start)*1000,z[clip]-np.median(z),'.-',lw=.7,ms=2.5,alpha=.8,color=col,label=label)
                f,p,fs,_=spectrum(t,z);axes[row,1].semilogy(f[1:],np.maximum(p[1:],1e-16),lw=.8,alpha=.8,color=col,label=label)
                psd_rows.extend(dict(dataset=dataset,mode=mode,settle_us=delay,route=route,repetition=rep,frequency_hz=float(ff),psd_counts2_per_hz=float(pp),psd_volts2_per_hz=float(pp)*LSB**2) for ff,pp in zip(f,p))
                trace_rows.extend(dict(dataset=dataset,mode=mode,settle_us=delay,route=route,repetition=rep,time_after_warmup_s=float(tt),time_within_zoom_ms=float((tt-window_start)*1000),counts=int(zz),deviation_counts=float(zz-np.median(z))) for tt,zz in zip(t[clip],z[clip]))
            cur=next(r for r in summary if matches(r,'lookahead',mode,delay,route));old=next(r for r in summary if matches(r,'previous',mode,delay,route))
            axes[row,0].set(title=f"{mode.upper()} / {delay} us / array {'A' if routeinfo.array==1 else 'B'} / MUX{routeinfo.mux} / address {routeinfo.input}",xlabel='Time within zoom window (ms)' if window_start else 'Time after warm-up (ms)',ylabel='Deviation from own median (counts)',xlim=(0,25));axes[row,0].legend(fontsize=6.5,ncol=2)
            axes[row,1].set(title=f"Median SD: previous {old['sd_counts']:.4f}, current {cur['sd_counts']:.4f} counts",xlabel='Frequency (Hz)',ylabel='PSD (counts²/Hz)');axes[row,1].legend(fontsize=6.5,ncol=2)
        fig.suptitle(title,fontsize=11);path=figdir/(name+'.png');fig.savefig(path,dpi=180);plt.close(fig)
        caption=('Previous and current first repetitions are shown. ' if comparison else 'All three current repetitions are shown. ')+ 'Left: unfiltered first 25 ms after warm-up, referenced to each full-record median. Right: Welch PSD from the 15 s capture. 4096-sample Hann windows, 50% overlap, window means removed; split timestamp gaps >1.5 times median period, exclude sections shorter than 4096, unused trailing samples omitted. Normal timestamp jitter remains; frequency axis uses approximate sweep timing. No quiet mask or sample clipping.'
        if window_start:
            caption=caption.replace('Left: unfiltered first 25 ms after warm-up,',f'Left: unfiltered 25 ms window starting {window_start:.6f} s after warm-up, selected around the largest absolute excursion in the current first repetition and using the same elapsed-time interval from the previous capture,')
        figures.append(dict(title=title,path=str(path.relative_to(folder)).replace('\\','/'),height=335 if len(cases)==2 else 202,caption=caption))
    for delay in (1,5):
        routes=[r['route'] for r in selected if r['settle_us']==delay]
        for start in range(0,len(routes),2):plot_cases([('mux',delay,r) for r in routes[start:start+2]],f'current_noise_{delay}us_{start//2+1}',f'Current selected noise - MUX/LPSPI/VMID off / {delay} us / 16 MHz')
    oldcases=[('mux',1,'2:1:5'),('mux',5,'2:1:4'),('mux',5,'1:1:4'),('mux',5,'2:3:3')]
    for start in (0,2):plot_cases(oldcases[start:start+2],f'old_problem_noise_{start//2+1}','Previously structured routes - before/after lookahead',True)
    extra=[]
    for mode,delay in (('adc',1),('mux',2)):
        candidates=[r for r in summary if r['dataset']=='lookahead' and r['mode']==mode and r['settle_us']==delay and r['vmid_settle_effective_us']==delay and not r['vmid'] and r['requested_spi_clock_hz']==16000000]
        worst=max(candidates,key=lambda r:r['sd_counts']);extra.append((mode,delay,worst['route']))
    plot_cases(extra,'additional_noise_cases','Additional measured noise cases - ADC 1 us and MUX 2 us / VMID off',True)
    candidates=[r for r in summary if matches(r,'lookahead','adc',5,r['route'])]
    worst=max(candidates,key=lambda r:r['sd_counts'])
    tt,xx=load('lookahead','adc',5,1);zz=xx[:,names.index(worst['route'])]
    start=float(np.clip(tt[np.argmax(np.abs(zz-np.median(zz)))]-.0125,0,tt[-1]-.025))
    plot_cases([('adc',5,worst['route'])],'adc5_noise_case','ADC scan / 5 us / VMID off - detected spike before/after',True,start)
    psd_rows=list({tuple(r[k] for k in ('dataset','mode','settle_us','route','repetition','frequency_hz')):r for r in psd_rows}.values())
    trace_rows=list({tuple(r[k] for k in ('dataset','mode','settle_us','route','repetition','time_after_warmup_s')):r for r in trace_rows}.values())
    csv_write(folder/'selected_noise_audit.csv',audit);csv_write(folder/'noise_psd.csv',psd_rows);csv_write(folder/'noise_time_zoom.csv',trace_rows);csv_write(folder/'noise_psd_coverage.csv',coverage)
    result=dict(partial=data['partial'],selected=selected,audit=audit,figures=figures,checked_route_settings=100,
        method='Median full-window SD >0.5 counts; max absolute ACF lags1-64 >=0.05 in at least two of three repeats. All 50 MUX/LPSPI/16 MHz/VMID-off routes checked at 1/5 us. Descriptive temporal-structure screen; no causal attribution.')
    (folder/'selected_noise_current.json').write_text(json.dumps(result,indent=2));print('SELECTED',selected,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('folder',type=Path);p.add_argument('--preview',action='store_true');a=p.parse_args();build(a.folder.resolve(),a.preview)
