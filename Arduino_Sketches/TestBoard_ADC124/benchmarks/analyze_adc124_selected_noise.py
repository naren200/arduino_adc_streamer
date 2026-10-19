"""Audit every route in MUX/LPSPI/16 MHz/VMID-off captures at 1 and 5 us."""
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from analyze_adc124_noise_patterns import SOURCE,OUTPUT,NAMES,ROUTES,LSB,load,acf,spectrum
from analyze_adc124_scan_modes import csv_write

THRESHOLD=.5
CORRELATION_THRESHOLD=.05


def main():
    session=json.loads((SOURCE/'session.json').read_text())
    runs=[r for r in session['runs'] if r['scanorder']=='mux' and r['spiengine']=='lpspi' and not r['vmid']
          and r['spi_clock_hz']==16000000 and r['mux_settle_us'] in (1,5) and r['vmid_settle_us']==r['mux_settle_us']]
    assert len(runs)==6
    loaded={};details={};audit=[];spectra=[];zoom=[]
    for run in runs:
        delay=run['mux_settle_us'];rep=run['repetition'];t,x,_=load(run);loaded[delay,rep]=(t,x)
        dt=np.diff(t);period=float(np.median(dt));edges=np.r_[0,np.flatnonzero(dt>1.5*period)+1,len(t)]
        eligible=sum(b-a for a,b in zip(edges[:-1],edges[1:]) if b-a>=4096)
        windowed=sum(4096+2048*((b-a-4096)//2048) for a,b in zip(edges[:-1],edges[1:]) if b-a>=4096)
        for j,route in enumerate(ROUTES):
            z=x[:,j];sd=float(z.std(ddof=1));a=acf(z,64);f,p,fs,peaks=spectrum(t,z)
            maxacf=float(np.nanmax(np.abs(a[1:]))) if np.isfinite(a[1:]).any() else 0.
            record=dict(settle_us=delay,repetition=rep,route=str(route),array=route.array,mux=route.mux,
                adc_input='IN'+str(route.mux),external_address=route.input,sensor='PZT'+str(route.sensor),sensor_channel=route.sensor_channel,
                frames=len(z),sd_counts=sd,sd_volts=sd*LSB,ptp_counts=float(np.ptp(z)),baseline_counts=float(np.median(z)),
                lag1_acf=float(a[1]) if np.isfinite(a[1]) else 0.,max_abs_acf_lags1_64=maxacf,
                sd_above_threshold=bool(sd>THRESHOLD),correlated_pattern=bool(maxacf>=CORRELATION_THRESHOLD),sample_rate_hz=fs,
                psd_eligible_frame_fraction=float(eligible/len(t)),psd_windowed_frame_fraction=float(windowed/len(t)),
                strongest_interior_peak_hz=peaks[0]['frequency_hz'] if peaks else 0.)
            audit.append(record);details[delay,str(route),rep]=(record,f,p,a)
        print('Checked all 50 routes:',delay,'us, repetition',rep,flush=True)
    selected=[];allroutes=[]
    for delay in (1,5):
        for j,route in enumerate(ROUTES):
            records=[details[delay,str(route),rep][0] for rep in (1,2,3)];sd=[r['sd_counts'] for r in records]
            hits=sum(r['correlated_pattern'] for r in records);passes=bool(np.median(sd)>THRESHOLD and hits>=2)
            item=dict(settle_us=delay,route=str(route),array=route.array,mux=route.mux,adc_input='IN'+str(route.mux),
                external_address=route.input,sensor='PZT'+str(route.sensor),sensor_channel=route.sensor_channel,
                bias_ohms=route.bias_ohms,sd_median_counts=float(np.median(sd)),sd_min_counts=min(sd),sd_max_counts=max(sd),
                sd_median_volts=float(np.median(sd))*LSB,sd_above_repetitions=sum(r['sd_above_threshold'] for r in records),
                correlated_repetitions=hits,lag1_median=float(np.median([r['lag1_acf'] for r in records])),
                max_abs_acf_median=float(np.median([r['max_abs_acf_lags1_64'] for r in records])),selected=passes)
            allroutes.append(item)
            if passes:selected.append(item)
    plt.rcParams.update({'font.size':9,'axes.grid':True,'grid.alpha':.2,'axes.spines.top':False})
    colors=('#0072B2','#D55E00','#009E73');figures=[]
    for delay in (1,5):
        channels=[r for r in selected if r['settle_us']==delay]
        for start in range(0,len(channels),2):
            chunk=channels[start:start+2];fig,axes=plt.subplots(len(chunk),2,figsize=(12,5.3 if len(chunk)==2 else 3.2),squeeze=False,constrained_layout=True)
            for row,item in enumerate(chunk):
                route=item['route'];j=NAMES.index(route)
                title=f"Array {'A' if item['array']==1 else 'B'} / MUX{item['mux']} / external address {item['external_address']} / {item['sensor']} ch {item['sensor_channel']}"
                for rep,color in zip((1,2,3),colors):
                    t,x=loaded[delay,rep];z=x[:,j];clip=t<.025
                    axes[row,0].plot(t[clip]*1000,z[clip]-np.median(z),'.-',lw=.65,ms=2.4,alpha=.7,color=color,label='Rep '+str(rep))
                    record,f,p,a=details[delay,route,rep]
                    axes[row,1].semilogy(f[1:],np.maximum(p[1:],1e-16),lw=.8,alpha=.8,color=color,label='Rep '+str(rep))
                    spectra.extend(dict(settle_us=delay,route=route,repetition=rep,frequency_hz=float(ff),psd_counts2_per_hz=float(pp),psd_volts2_per_hz=float(pp)*LSB**2) for ff,pp in zip(f,p))
                    zoom.extend(dict(settle_us=delay,route=route,repetition=rep,time_after_warmup_s=float(tt),counts=int(zz),deviation_from_full_median_counts=float(zz-np.median(z))) for tt,zz in zip(t[clip],z[clip]))
                axes[row,0].set(title=title,xlabel='Time after warm-up (ms)',ylabel='Deviation from own median (counts)',xlim=(0,25))
                axes[row,0].legend(fontsize=7,ncol=3)
                axes[row,1].set(title=f"15 s capture PSD | SD median {item['sd_median_counts']:.4f} counts; lag-1 {item['lag1_median']:+.3f}",xlabel='Frequency (Hz)',ylabel='PSD (counts²/Hz)',xlim=(0,f[-1]))
                axes[row,1].legend(fontsize=7,ncol=3)
            fig.suptitle(f'MUX scan / LPSPI / requested 16 MHz / VMID off / settling {delay} us | all three repeats',fontsize=12)
            name=f'selected_noise_{delay}us_{start//2+1}.png';fig.savefig(OUTPUT/'figures'/name,dpi=180);plt.close(fig)
            figures.append(dict(settle_us=delay,path='figures/'+name,routes=[r['route'] for r in chunk],height=335 if len(chunk)==2 else 202))
    result=dict(source=str(SOURCE),
        firmware_revision='adc124-vmid-settle-v1',clock_hz=16000000,scanorder='mux',engine='lpspi',vmid=False,
        checked_captures=6,checked_route_settings=100,checked_channel_repetitions=300,
        method=dict(sd='Unfiltered full 15 s sample SD, ddof=1, after 1 s warm-up; no clipping or quiet mask.',
            sd_threshold_counts=THRESHOLD,aggregation='Median SD across three repetitions must be strictly above 0.5 counts.',
            pattern='Maximum absolute normalized autocorrelation at lags 1-64 >=0.05 in at least two of three repetitions; PSD and raw traces supplied for inspection. A descriptive structure screen, not a proof of electrical cause or a formal acceptance test for whiteness.',
            acf_threshold=CORRELATION_THRESHOLD,psd='Hann-window Welch, 4096 samples, 50% overlap, mean removed; split timestamp gaps >1.5 times median period; approximate mean sampling clock. Normal timing jitter remains.'),
        selected=selected,all_routes=allroutes,figures=figures)
    (OUTPUT/'selected_noise_1us_5us.json').write_text(json.dumps(result,indent=2))
    csv_write(OUTPUT/'selected_noise_channel_audit.csv',audit);csv_write(OUTPUT/'selected_noise_route_summary.csv',allroutes)
    csv_write(OUTPUT/'selected_noise_psd.csv',spectra);csv_write(OUTPUT/'selected_noise_time_zoom.csv',zoom)
    print('SELECTED',json.dumps(selected),flush=True)
    print('COUNTS',[(d,sum(r['settle_us']==d for r in selected)) for d in (1,5)],flush=True)


if __name__=='__main__':main()
