"""Unfiltered disconnected ADC124 noise, timing and spectral diagnostics."""
from pathlib import Path
import json, hashlib
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.signal import welch, find_peaks, spectrogram
from scipy.fft import rfft, irfft, next_fast_len
from capture_adc124_settling import decode
from report_adc124_settling import canonical_samples, LSB
from analyze_adc124_upgrade import ROOT, ROUTES
from analyze_adc124_scan_modes import csv_write

SOURCE=ROOT/'docs/testing/datasets/testboard-adc124/upgrade_disconnected_2026-10-14/production'
OUTPUT=ROOT/'docs/testing/results/testboard-adc124/upgrade_disconnected_2026-10-14'
NAMES=[str(r) for r in ROUTES]


def load(run):
    folder=SOURCE/run['name'];raw=folder/'capture.bin'
    if hashlib.sha256(raw.read_bytes()).hexdigest()!=run['sha256']:raise ValueError('Raw hash mismatch')
    frames=decode(raw);status=json.loads((folder/'status.json').read_text())['after']
    t=np.r_[0,np.cumsum(np.diff(frames['start'].astype(np.int64))%2**32)]/1e6
    keep=(t>=1)&(t<16)
    return t[keep]-1,canonical_samples(frames,status)[keep],frames[keep]


def acf(values,lags):
    z=values-values.mean();variance=np.dot(z,z)
    if variance==0:return np.full(lags+1,np.nan)
    n=next_fast_len(2*len(z));s=rfft(z,n);out=irfft(s*s.conj(),n)[:lags+1]
    return out/variance


def spectrum(t,z):
    # Avoid artificial jumps where whole sweeps were dropped; use contiguous
    # sections. Within a section sampling has small firmware timing jitter.
    dt=np.diff(t);period=float(np.median(dt));fs=float(1/np.mean(dt[dt<1.5*period]))
    edges=np.r_[0,np.flatnonzero(dt>1.5*period)+1,len(z)];parts=[];weights=[]
    for a,b in zip(edges[:-1],edges[1:]):
        if b-a<4096:continue
        f,p=welch(z[a:b],fs=fs,nperseg=4096,noverlap=2048,detrend='constant',scaling='density')
        parts.append(p);weights.append(b-a)
    if not parts:
        f,p=welch(z,fs=fs,nperseg=min(4096,len(z)))
    else:p=np.average(parts,axis=0,weights=weights)
    peaks,_=find_peaks(p);peaks=peaks[f[peaks]>10]
    top=peaks[np.argsort(p[peaks])[-5:]][::-1]
    return f,p,fs,[dict(frequency_hz=float(f[i]),psd_counts2_per_hz=float(p[i])) for i in top]


def main():
    session=json.loads((SOURCE/'session.json').read_text());records=[];captures=[];examples={}
    plt.rcParams.update({'font.size':10,'axes.grid':True,'grid.alpha':.2,'axes.spines.top':False})
    figdir=OUTPUT/'figures';figdir.mkdir(exist_ok=True)
    for number,run in enumerate(session['runs']):
        t,x,frames=load(run);z=x-x.mean(axis=0);variance=np.einsum('ij,ij->j',z,z)
        sd=x.std(axis=0,ddof=1);pp=np.ptp(x,axis=0);median=np.median(x,axis=0)
        lag1=np.divide(np.einsum('ij,ij->j',z[:-1],z[1:]),variance,out=np.full(50,np.nan),where=variance>0)
        ident={k:run[k] for k in ('name','repetition','scanorder','spiengine','spi_clock_hz','mux_settle_us','vmid','vmid_settle_us')}
        for j,route in enumerate(ROUTES):
            delta=np.abs(x[:,j]-median[j]);hist=np.bincount(x[:,j].astype(int),minlength=4096)
            records.append(dict(**ident,route=str(route),array=route.array,mux=route.mux,input=route.input,
                frames=len(x),sd_counts=float(sd[j]),sd_volts=float(sd[j]*LSB),ptp_counts=float(pp[j]),ptp_volts=float(pp[j]*LSB),
                median_counts=float(median[j]),lag1_acf=float(lag1[j]),modal_fraction=float(hist.max()/len(x)),
                maximum_deviation_counts=float(delta.max()),deviation_ge3_frames=int((delta>=3).sum()),
                deviation_ge5_frames=int((delta>=5).sum()),deviation_ge10_frames=int((delta>=10).sum())))
        worst=int(sd.argmax());f,p,fs,peaks=spectrum(t,x[:,worst])
        coarse=np.load(OUTPUT/('current__production__'+run['name']+'__quiet_mask.npz'))
        # Packed masks refer to the original capture including warm-up.
        states=json.loads((OUTPUT/('current__production__'+run['name']+'__fine_quiet_states.json')).read_text())['states']
        captures.append(dict(**ident,frames=len(x),worst_route=NAMES[worst],worst_sd_counts=float(sd[worst]),
            worst_ptp_counts=float(pp.max()),worst_sd_route_ptp_counts=float(pp[worst]),
            worst_route_lag1_acf=float(lag1[worst]),active_secondary_state_routes=sum(s['active'] for s in states),
            coherent_excursion_sweeps=int(np.unpackbits(coarse['coherent']).sum()),
            sweep_period_median_us=float(np.median(np.diff(t))*1e6),sample_rate_hz=fs,
            discarded_gaps=int((np.diff(t)>1.5*np.median(np.diff(t))).sum()),
            peak_frequency_hz=peaks[0]['frequency_hz'] if peaks else 0,peaks=peaks))
        selected=(run['repetition']==1 and run['mux_settle_us']==5 and run['vmid_settle_us']==5)
        if selected:examples[(run['scanorder'],run['spiengine'],run['spi_clock_hz'],run['vmid'])]=(t,x,frames)
        if (number+1)%30==0:print('Analyzed',number+1,'of',len(session['runs']),flush=True)
    csv_write(OUTPUT/'unfiltered_noise_channels.csv',records)
    csv_write(OUTPUT/'unfiltered_noise_captures.csv',[{k:v for k,v in r.items() if k!='peaks'} for r in captures])
    (OUTPUT/'noise_patterns.json').write_text(json.dumps(dict(method='All measured samples after 1 s warm-up; no quiet mask, clipping or detrending in time metrics. PSD uses mean-subtracted Hann-window Welch over contiguous runs; approximate mean sample clock. Listed spectral peaks are interior local maxima above 10 Hz, excluding DC and the Nyquist endpoint.',captures=captures),indent=2))
    t,x,frames=examples[('mux','lpspi',16000000,False)]
    routes=('2:1:4','2:3:4','1:1:4','1:3:4');colors=('#0072B2','#D55E00','#009E73','#CC79A7')
    fig,axes=plt.subplots(3,1,figsize=(12,8),constrained_layout=True)
    j=NAMES.index('2:1:4');bins=np.arange(0,15,.05)
    lo=[];hi=[];sd=[]
    for a in bins:
        w=x[(t>=a)&(t<a+.05),j];lo.append(w.min());hi.append(w.max());sd.append(w.std(ddof=1))
    axes[0].fill_between(bins,lo,hi,color='#0072B2',alpha=.25,label='Every sample: min/max in 50 ms bins')
    axes[0].set(title='B / MUX1 / input4: excursions persist across the entire 15 s capture',ylabel='ADC counts',xlabel='Time after warm-up (s)');axes[0].legend()
    for route,color in zip(routes,colors):
        values=x[:,NAMES.index(route)];keep=t<.025
        axes[1].plot(t[keep]*1000,values[keep]-np.median(values),'.-',markersize=3,lw=.8,color=color,label=route)
    axes[1].set(title='Raw first 25 ms: shared higher-code pulses in B; shared lower-code excursions in A',xlabel='Time after warm-up (ms)',ylabel='Deviation from median (counts)');axes[1].legend(ncol=4,title='Array:MUX:input')
    t_on,x_on,_=examples[('mux','lpspi',16000000,True)]
    for label,tt,xx,col in [('VMID off',t,x,'#D55E00'),('VMID on',t_on,x_on,'#0072B2')]:
        keep=tt<.025;values=xx[:,j]
        axes[2].plot(tt[keep]*1000,values[keep]-np.median(values),'.-',lw=.9,markersize=3,label=label,color=col)
    axes[2].set(title='Same physical route and requested delays: VMID on removes the visible pulse sequence',xlabel='Time after warm-up (ms)',ylabel='Deviation from median (counts)');axes[2].legend()
    fig.suptitle('TestBoard_ADC124 - disconnected arrays | shared MUX, LPSPI, 16 MHz, settling 5 us',fontsize=14)
    fig.savefig(figdir/'noise_patterns_raw.png',dpi=170);plt.close(fig)

    fig,axes=plt.subplots(2,2,figsize=(12,8),constrained_layout=True)
    f,p,fs,peaks=spectrum(t,x[:,j]);z=x[:,j];ac=acf(z,400)
    axes[0,0].semilogy(f,p,color='#0072B2',lw=.8)
    axes[0,0].set(xlim=(10,2000),xlabel='Frequency (Hz)',ylabel='PSD (counts²/Hz)',title='Unfiltered spectrum: strong bands, not a flat white spectrum')
    ff,tt,s=spectrogram(z,fs=fs,nperseg=4096,noverlap=3072)
    selected=(ff>=250)&(ff<=420)
    im=axes[0,1].pcolormesh(tt,ff[selected],10*np.log10(np.maximum(s[selected],1e-12)),shading='auto',cmap='viridis')
    axes[0,1].set(xlabel='Time after warm-up (s)',ylabel='Frequency (Hz)',title='Time-frequency view: several components around 330–360 Hz')
    fig.colorbar(im,ax=axes[0,1],label='dB relative to 1 count²/Hz')
    axes[1,0].plot(np.arange(len(ac))/fs*1000,ac,color='#0072B2');axes[1,0].axhline(0,color='gray',lw=.7)
    axes[1,0].set(xlim=(0,10),xlabel='Lag (ms)',ylabel='Normalized autocorrelation',title='Correlated neighboring samples and recurring oscillations')
    codes,counts=np.unique(z,return_counts=True);axes[1,1].bar(codes,100*counts/counts.sum(),color='#0072B2');axes[1,1].ticklabel_format(axis='x',useOffset=False)
    axes[1,1].set(xlabel='ADC code (counts)',ylabel='Fraction of all samples (%)',title='Separated code states: dominant baseline and higher pulse state')
    fig.suptitle('B / MUX1 / input4 | VMID off, MUX/LPSPI, 16 MHz, sensor settling 5 us',fontsize=14)
    fig.savefig(figdir/'noise_patterns_spectrum.png',dpi=170);plt.close(fig)

    # Comparable route, but each acquisition setting has its own median baseline.
    fig,axes=plt.subplots(2,3,figsize=(13,7),constrained_layout=True)
    for ax,ident in zip(axes.flat,[('mux','blocking',16000000,False),('mux','dma',16000000,False),('mux','lpspi',16000000,False),('adc','lpspi',16000000,False),('mux','lpspi',8000000,False),('mux','lpspi',16000000,True)]):
        tt,xx,_=examples[ident];zz=xx[:,j];keep=tt<.025
        ax.plot(tt[keep]*1000,zz[keep]-np.median(zz),'.-',lw=.8,ms=3,color='#0072B2')
        ax.set(title=f'{ident[0].upper()} / {ident[1]} / {ident[2]//1000000} MHz / VMID {"on" if ident[3] else "off"}',xlabel='Time after warm-up (ms)',ylabel='Deviation (counts)',ylim=(-3,6))
    fig.suptitle('B / MUX1 / input4: pattern depends on scan mode, SPI engine/clock and VMID | sensor=VMID 5 us',fontsize=13)
    fig.savefig(figdir/'noise_patterns_settings.png',dpi=170);plt.close(fig)

    coherent=[]
    for rep in (1,2,3):
        run=next(r for r in session['runs'] if r['scanorder']=='mux' and r['spiengine']=='lpspi' and r['spi_clock_hz']==16000000 and r['mux_settle_us']==5 and r['vmid_settle_us']==5 and not r['vmid'] and r['repetition']==rep)
        tt,xx,frames=load(run);values=xx[:,j];hit=values>=1961
        start=np.flatnonzero(hit&~np.r_[False,hit[:-1]]);end=np.flatnonzero(hit&~np.r_[hit[1:],False])+1
        length=end-start;acq=(frames['end'].astype(np.int64)-frames['start'].astype(np.int64))%2**32
        corr=np.corrcoef(xx[:,j],xx[:,NAMES.index('2:3:4')])[0,1]
        ff,pp,rate,top=spectrum(tt,values)
        coherent.append(dict(repetition=rep,sd_counts=float(values.std(ddof=1)),ptp_counts=float(np.ptp(values)),
            higher_state_fraction=float(hit.mean()),events=len(start),single_sweep_events=int((length==1).sum()),
            three_sweep_events=int((length==3).sum()),median_event_duration_us=float(np.median(length)/rate*1e6),
            onset_interval_quantiles_ms=(np.quantile(np.diff(tt[start]),[.1,.5,.9])*1000).tolist(),
            cross_route_correlation=float(corr),lag1_acf=float(acf(values,1)[1]),spectral_peaks=top,
            acquisition_time_correlation=float(np.corrcoef(values,acq)[0,1])))
    (OUTPUT/'noise_pulse_case.json').write_text(json.dumps(coherent,indent=2))
    print('FINISHED',len(records),'channel records;',len(captures),'captures',flush=True)
    print('CASE',json.dumps(coherent[0]),flush=True)
    print('TOP',json.dumps(sorted(captures,key=lambda r:r['worst_sd_counts'],reverse=True)[:8]),flush=True)
    extra_figures()


def extra_figures():
    session=json.loads((SOURCE/'session.json').read_text());examples=[]
    fig,axes=plt.subplots(3,2,figsize=(12,9),constrained_layout=True)
    settings=[('mux','lpspi',1,False,'2:1:5'),('adc','dma',2,False,'2:2:0'),('mux','lpspi',5,True,'2:2:4')]
    for row,(mode,engine,delay,vmid,route) in enumerate(settings):
        run=next(r for r in session['runs'] if r['repetition']==1 and r['scanorder']==mode and r['spiengine']==engine and r['mux_settle_us']==delay and r['vmid_settle_us']==delay and r['vmid']==vmid and r['spi_clock_hz']==16000000)
        t,x,_=load(run);z=x[:,NAMES.index(route)];f,p,fs,peaks=spectrum(t,z);a=acf(z,64)
        keep=t<.025
        axes[row,0].plot(t[keep]*1000,z[keep]-np.median(z),'.-',lw=.8,ms=3,color='#0072B2')
        axes[row,0].set(xlabel='Time after warm-up (ms)',ylabel='Deviation (counts)',title=f'{mode.upper()} / {engine} / {delay} us / VMID {"on" if vmid else "off"} / route {route}')
        axes[row,1].semilogy(f[1:],p[1:],lw=.8,color='#0072B2')
        axes[row,1].set(xlabel='Frequency (Hz)',ylabel='PSD (counts²/Hz)',title=f'SD {z.std(ddof=1):.3f} counts; p-p {np.ptp(z):.0f}; lag-1 {a[1]:+.3f}')
        examples.append(dict(mode=mode,engine=engine,settle_us=delay,vmid=vmid,route=route,sd_counts=float(z.std(ddof=1)),ptp_counts=float(np.ptp(z)),lag1_acf=float(a[1]),max_abs_lag1_to64_acf=float(np.max(np.abs(a[1:]))),spectral_peaks=peaks))
    fig.suptitle('Two wider structured-noise cases and a quiet, nearly uncorrelated digital-code example | 16 MHz',fontsize=13)
    fig.savefig(OUTPUT/'figures/noise_patterns_wide_and_quiet.png',dpi=170);plt.close(fig)
    (OUTPUT/'noise_wide_quiet_cases.json').write_text(json.dumps(examples,indent=2))
    print('WIDE/QUIET',json.dumps(examples),flush=True)


if __name__=='__main__':main()
