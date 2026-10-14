"""Matched full-window and common-quiet analysis of LPSPI lookahead rerun."""
from pathlib import Path
import argparse,json,hashlib
import numpy as np
from analyze_adc124_scan_modes import ROOT,process,csv_write,LSB
from analyze_adc124_upgrade import ROUTES,aggregate,refine,synchronize_capture_noise,key
from capture_adc124_settling import decode
from report_adc124_settling import canonical_samples
from analyze_adc124_noise_patterns import acf,spectrum

PREVIOUS=ROOT/'docs/testing/datasets/testboard-adc124/upgrade_disconnected_2026-10-14/production'
VERSION=1


def analyze_capture(source,run,session,out,dataset):
    row,coarse_channels=process(source,run,session,out,allow_insufficient_quiet=True)
    row['dataset']=dataset
    for c in coarse_channels:
        c['dataset']=dataset;c['requested_spi_clock_hz']=row['requested_spi_clock_hz']
    frames=decode(source/run['name']/'capture.bin');status=json.loads((source/run['name']/'status.json').read_text())['after']
    t=np.r_[0,np.cumsum(np.diff(frames['start'].astype(np.int64))%2**32)]/1e6
    measured=(t>=session['warmup_s'])&(t<session['warmup_s']+session['measurement_s'])
    full=canonical_samples(frames,status);x=full[measured];tm=t[measured];raw=[]
    for i,r in enumerate(ROUTES):
        z=x[:,i];a=acf(z,64);corr=float(np.max(np.abs(a[1:]))) if np.isfinite(a[1:]).any() else 0.
        c={k:row[k] for k in ('dataset','stage','name','repetition','mode','engine','vmid','settle_us','vmid_settle_effective_us','requested_spi_clock_hz')}
        c.update(route=str(r),array=r.array,mux=r.mux,sensor='PZT'+str(r.sensor),sensor_channel=r.sensor_channel,bias_ohms=r.bias_ohms,
            quiet_frames=len(z),sd_counts=float(z.std(ddof=1)),ptp_counts=float(np.ptp(z)),baseline_counts=float(np.median(z)),
            lag1_acf=float(a[1]) if np.isfinite(a[1]) else 0.,max_abs_acf_lags1_64=corr)
        for f in ('sd','ptp','baseline'):c[f+'_volts']=c[f+'_counts']*LSB
        raw.append(c)
    rawrow=dict(row);rawrow['quiet_qualified']=True;rawrow['quiet_seconds']=session['measurement_s'];synchronize_capture_noise(rawrow,raw)
    fine,quiet=refine(source,run,session,out,row,coarse_channels)
    synchronize_capture_noise(fine,quiet)
    return dict(version=VERSION,sha256=run['sha256'],row=fine,channels=quiet,raw_row=rawrow,raw_channels=raw,
        coarse_row=row,coarse_channels=coarse_channels)


def analyze(source,out,partial=False):
    out.mkdir(parents=True,exist_ok=True);cp=out/'analysis_cache.json';cache=json.loads(cp.read_text()) if cp.exists() else {}
    rows=[];channels=[];rawrows=[];rawchannels=[];coarserows=[];coarsechannels=[]
    for dataset,folder,revision in (('lookahead',source/'production','adc124-selector-lookahead-v1'),('previous',PREVIOUS,'adc124-vmid-settle-v1')):
        session=json.loads((folder/'session.json').read_text());runs=[r for r in session['runs'] if r['spiengine']=='lpspi']
        if not partial and (session['state']!='complete' or len(runs)!=84):raise ValueError('Incomplete '+dataset+' matrix')
        for run in runs:
            ident=dataset+'/'+run['name'];raw=folder/run['name']/'capture.bin'
            if hashlib.sha256(raw.read_bytes()).hexdigest()!=run['sha256']:raise ValueError('Hash mismatch '+ident)
            if ident not in cache or cache[ident].get('version')!=VERSION:
                cache[ident]=analyze_capture(folder,run,session,out,dataset)
            c=cache[ident]
            if c['sha256']!=run['sha256'] or c['row']['firmware_revision']!=revision:raise ValueError('Cached identity mismatch')
            rows.append(c['row']);channels.extend(c['channels']);rawrows.append(c['raw_row']);rawchannels.extend(c['raw_channels'])
            coarserows.append(c['coarse_row']);coarsechannels.extend(c['coarse_channels'])
            if len(rows)%10==0:cp.write_text(json.dumps(cache,allow_nan=False));print('Analyzed',len(rows),'captures',flush=True)
    cp.write_text(json.dumps(cache,allow_nan=False))
    summary,ch=aggregate(rows,channels);rawsummary,rawch=aggregate(rawrows,rawchannels);coarsesummary,coarsech=aggregate(coarserows,coarsechannels)
    comparisons=[];routepairs=[]
    def match(r,old):return all(r[k]==old[k] for k in ('mode','engine','vmid','settle_us','vmid_settle_effective_us','requested_spi_clock_hz'))
    for cur in rawsummary:
        if cur['dataset']!='lookahead':continue
        old=next(r for r in rawsummary if r['dataset']=='previous' and match(cur,r))
        pair={k:cur[k] for k in ('mode','vmid','settle_us','vmid_settle_effective_us','requested_spi_clock_hz')}
        pair.update(previous_rate_hz=old['delivered_sweeps_s'],current_rate_hz=cur['delivered_sweeps_s'],speed_gain_percent=100*(cur['delivered_sweeps_s']/old['delivered_sweeps_s']-1))
        for metric in ('all_sd_max_counts','all_sd_median_counts','all_ptp_max_counts','A1_baseline_spread_counts','A2_baseline_spread_counts'):
            pair['previous_raw_'+metric]=old[metric];pair['current_raw_'+metric]=cur[metric]
        comparisons.append(pair)
        for route in ROUTES:
            a=next(c for c in rawch if key(c)==key(cur) and c['route']==str(route));b=next(c for c in rawch if key(c)==key(old) and c['route']==str(route))
            routepairs.append(dict(**{k:pair[k] for k in ('mode','vmid','settle_us','vmid_settle_effective_us','requested_spi_clock_hz')},route=str(route),
                previous_baseline_counts=b['baseline_counts'],current_baseline_counts=a['baseline_counts'],baseline_shift_counts=a['baseline_counts']-b['baseline_counts'],
                baseline_shift_volts=(a['baseline_counts']-b['baseline_counts'])*LSB,previous_sd_counts=b['sd_counts'],current_sd_counts=a['sd_counts'],
                previous_ptp_counts=b['ptp_counts'],current_ptp_counts=a['ptp_counts']))
    for name,v in [('capture_metrics',rows),('summary',summary),('channel_summary',ch),('channel_repetitions',channels),
            ('unfiltered_capture_metrics',rawrows),('unfiltered_summary',rawsummary),('unfiltered_channel_summary',rawch),('unfiltered_channel_repetitions',rawchannels),
            ('coarse_summary',coarsesummary),('coarse_channel_summary',coarsech),('firmware_comparisons',comparisons),('channel_comparisons',routepairs)]:csv_write(out/(name+'.csv'),v)
    life=json.loads((source/'firmware_lifecycle.json').read_text())
    if not partial and life['state']!='complete_production_restored_verified':raise ValueError('Restoration incomplete')
    result=dict(version=VERSION,partial=partial,rows=rows,summary=summary,channels=ch,raw_rows=rawrows,raw_summary=rawsummary,raw_channels=rawch,
        raw_channel_repetitions=rawchannels,comparisons=comparisons,channel_comparisons=routepairs,lifecycle=life,raw_root=str(source.relative_to(ROOT)),reference_root=str(PREVIOUS.relative_to(ROOT)),lsb_volts=LSB)
    (out/'analysis.json').write_text(json.dumps(result,allow_nan=False))
    print('Analysis',len(rows),'captures',len(rawsummary),'settings',flush=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path);p.add_argument('output',type=Path);p.add_argument('--partial',action='store_true')
    a=p.parse_args();analyze(a.source.resolve(),a.output.resolve(),a.partial)
