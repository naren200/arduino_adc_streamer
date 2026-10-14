"""Hash-verified current and yesterday ADC124 comparisons in physical route order."""
from pathlib import Path
import argparse
import hashlib
import json
import numpy as np

from analyze_adc124_scan_modes import ROOT, bench, process, csv_write, LSB
from capture_adc124_settling import decode
from report_adc124_settling import canonical_samples, quiet_mask
from adc124_timing_model import operation_counts as legacy_counts
from adc124_quiet_windows import between_bursts, VERSION as QUIET_VERSION

ORIGINAL = ROOT/'docs/testing/datasets/testboard-adc124/mux_production_2026-10-13'
SHARED = ROOT/'docs/testing/datasets/testboard-adc124/scan_modes_20261013T214607Z/production'
ROUTES = tuple(sorted(bench.load_manifest(bench.DEFAULT_MANIFEST)['both_full']))


def synchronize_capture_noise(row,channels):
    for field in list(row):
        if field.startswith(('A1_','A2_')): row.pop(field)
    if not channels: return
    for a in (1,2):
        selected=[c for c in channels if c['array']==a]
        sd=[c['sd_counts'] for c in selected];pp=[c['ptp_counts'] for c in selected];levels=np.array([c['baseline_counts'] for c in selected])
        row.update({f'A{a}_quiet_sd_median_counts':float(np.median(sd)),f'A{a}_quiet_sd_max_counts':float(max(sd)),
            f'A{a}_quiet_ptp_max_counts':float(max(pp)),f'A{a}_baseline_spread_counts':float(np.ptp(levels)),
            f'A{a}_baseline_min_volts':float(levels.min()*LSB),f'A{a}_baseline_max_volts':float(levels.max()*LSB)})


def refine(source,run,session,output,row,channels):
    folder=source/run['name'];frames=decode(folder/'capture.bin');status=json.loads((folder/'status.json').read_text())['after']
    t=np.r_[0,np.cumsum(np.diff(frames['start'].astype(np.int64))%2**32)]/1e6;x=canonical_samples(frames,status)
    groups=[[i for i,r in enumerate(ROUTES) if (r.array,r.mux)==(a,m)] for a in (1,2) for m in (1,2,3,4)]
    coarse,*_=quiet_mask(t,x,groups,start=session['warmup_s'],end=session['warmup_s']+session['measurement_s'],require_coverage=False)
    fine=between_bursts(t,x,coarse);mask=fine['mask'];result=dict(row)
    result.update(coarse_quiet_seconds=row['quiet_seconds'],coarse_quiet_frames=row['quiet_frames'],
        quiet_seconds=fine['quiet_seconds'],quiet_frames=int(mask.sum()),quiet_method_version=QUIET_VERSION,
        fine_excursion_frames=fine['fine_excursion_frames'],fine_rejected_frames=fine['fine_rejected_frames'],
        fine_rejected_percent=100*fine['fine_rejected_fraction'],
        rejected_time_percent=100*(1-fine['quiet_seconds']/session['measurement_s']),
        quiet_qualified=bool(fine['quiet_seconds']>=1 and mask.sum()>=100))
    prefix=row['dataset']+'__'+row['stage']+'__'+run['name']
    np.savez_compressed(output/(prefix+'__quiet_mask.npz'),frames=len(t),final=np.packbits(mask),coarse=np.packbits(coarse),coherent=np.packbits(fine['coherent']))
    (output/(prefix+'__fine_quiet_states.json')).write_text(json.dumps(dict(version=QUIET_VERSION,states=fine['states'])))
    old={c['route']:c for c in channels};updated=[]
    if result['quiet_qualified']:
        z=x[mask]
        for i,r in enumerate(ROUTES):
            c=dict(old.get(str(r),{}))
            c.update({k:row[k] for k in ('dataset','stage','name','repetition','mode','engine','vmid','settle_us','vmid_settle_effective_us','requested_spi_clock_hz')})
            c.update(route=str(r),array=r.array,mux=r.mux,sensor=f'PZT{r.sensor}',sensor_channel=r.sensor_channel,bias_ohms=r.bias_ohms,
                quiet_frames=len(z),sd_counts=float(z[:,i].std(ddof=1)),ptp_counts=float(np.ptp(z[:,i])),baseline_counts=float(np.median(z[:,i])))
            for a,b in (('sd_counts','sd_volts'),('ptp_counts','ptp_volts'),('baseline_counts','baseline_volts')):c[b]=c[a]*LSB
            updated.append(c)
    return result,updated


def legacy_process(source, run, session, output):
    folder = source/run['name']; saved = json.loads((folder/'status.json').read_text()); status = saved['after']
    frames = decode(folder/'capture.bin')
    accounting, failures = bench.stream_accounting(saved['before'], status, len(frames))
    errors = {k:bench.uint32_delta(int(status[k]),int(saved['before'][k])) for k in bench.error_counter_keys(status)}
    if failures or any(errors.values()): raise ValueError(str(failures or errors))
    for key, value in dict(scanorder='mux',spiengine=run['engine'],array='both',mux_settle_us=str(run['settle_us']),
                           spi_clock_hz='16000000',channelrepeat='1',vmid_between_groups=str(run['vmid']).lower()).items():
        if status[key] != value: raise ValueError('Historical status mismatch: '+key)
    t = np.r_[0,np.cumsum(np.diff(frames['start'].astype(np.int64))%2**32)]/1e6
    acq = (frames['end'].astype(np.int64)-frames['start'].astype(np.int64))%2**32
    interval = (t[:-1]>=session['warmup_s']) & (t[1:]>=session['warmup_s'])
    periods = np.diff(t)*1e6; gaps = periods[interval]-acq[:-1][interval]
    if np.any(gaps < -.001): raise ValueError('Historical acquisition overlap')
    c = legacy_counts('mux',run['vmid']); x = canonical_samples(frames,status)
    groups = [[i for i,r in enumerate(ROUTES) if (r.array,r.mux)==(a,m)] for a in (1,2) for m in (1,2,3,4)]
    mask,edges,envelope,thresholds,keep,above = quiet_mask(t,x,groups,start=session['warmup_s'],
        end=session['warmup_s']+session['measurement_s'],require_coverage=False)
    qualified = keep.sum()*.05>=1 and mask.sum()>=100
    row = dict(stage='production',name=run['name'],repetition=run['repetition'],mode='adc',engine=run['engine'],
        vmid=run['vmid'],settle_us=run['settle_us'],vmid_settle_effective_us=run['settle_us'],
        requested_spi_clock_hz=16000000,spi_frame='single',firmware_revision='original_protocol_'+status['protocol_version'],
        protocol_version=int(status['protocol_version']),scan_semantics='array_sequential_address_grouped',
        frames=len(frames),measured_frames=int((t>=session['warmup_s']).sum()),mean_period_us=float(periods[interval].mean()),
        delivered_sweeps_s=float(1e6/periods[interval].mean()),mean_acquisition_us=float(acq[t>=session['warmup_s']].mean()),
        mean_gap_us=float(gaps.mean()),period_p99_us=float(np.quantile(periods[interval],.99)),
        quiet_seconds=float(keep.sum()*.05),quiet_frames=int(mask.sum()),quiet_qualified=bool(qualified),
        rejected_time_percent=float(100*(1-keep.mean())),**accounting,**errors,
        estimated_settle_budget_us=c['address_changes']*run['settle_us'],estimated_spi_words=c['words'],
        estimated_cs_allowance_us=c['cs_allowance_us'],estimated_mux_switch_guard_us=c['switch_allowance_us'])
    csv_write(output/('original__'+run['name']+'__quiet_bins.csv'),[
        dict(start_s=float(a),end_s=float(b),quiet=bool(k),above_threshold=bool(ab))
        for a,b,k,ab in zip(edges[:-1],edges[1:],keep,above)])
    (output/('original__'+run['name']+'__quiet_thresholds.json')).write_text(json.dumps(thresholds.tolist()))
    channels=[]
    if qualified:
        z=x[mask]
        for i,r in enumerate(ROUTES):
            sd=float(z[:,i].std(ddof=1)); pp=float(np.ptp(z[:,i])); baseline=float(np.median(z[:,i]))
            channels.append(dict(stage='production',name=run['name'],repetition=run['repetition'],mode='adc',engine=run['engine'],
                vmid=run['vmid'],settle_us=run['settle_us'],vmid_settle_effective_us=run['settle_us'],spi_frame='single',
                route=str(r),array=r.array,mux=r.mux,sensor=f'PZT{r.sensor}',sensor_channel=r.sensor_channel,bias_ohms=r.bias_ohms,
                quiet_frames=len(z),sd_counts=sd,sd_volts=sd*LSB,ptp_counts=pp,ptp_volts=pp*LSB,
                baseline_counts=baseline,baseline_volts=baseline*LSB))
    return row, channels


def key(row):
    return (row['dataset'],row['stage'],row['mode'],row['engine'],row['vmid'],row['settle_us'],
            row['vmid_settle_effective_us'],row['requested_spi_clock_hz'])


def aggregate(rows, channels):
    groups={}; cg={}
    for row in rows: groups.setdefault(key(row),[]).append(row)
    for row in channels: cg.setdefault(key(row)+(row['route'],),[]).append(row)
    summaries=[]; ch_summary=[]
    for ident, records in cg.items():
        out={k:records[0][k] for k in ('dataset','stage','mode','engine','vmid','settle_us','vmid_settle_effective_us',
            'requested_spi_clock_hz','route','array','mux','sensor','sensor_channel','bias_ohms')}
        out['qualified_repetitions']=len(records)
        for field in ('baseline_counts','baseline_volts','sd_counts','sd_volts','ptp_counts','ptp_volts'):
            vals=[r[field] for r in records]
            out[field]=float(np.median(vals)); out[field+'_min_rep']=float(min(vals)); out[field+'_max_rep']=float(max(vals))
        ch_summary.append(out)
    for ident,records in groups.items():
        out={k:records[0][k] for k in ('dataset','stage','mode','engine','vmid','settle_us','vmid_settle_effective_us','requested_spi_clock_hz','firmware_revision')}
        out.update(repetitions=len(records),qualified_repetitions=sum(r.get('quiet_qualified',False) for r in records))
        skip=set(out)|{'repetition','frames','measured_frames','quiet_frames','protocol_version'}
        for field,value in records[0].items():
            if isinstance(value,(int,float)) and not isinstance(value,bool) and field not in skip:
                vals=[r[field] for r in records if field in r]
                out[field]=float(np.median(vals)); out[field+'_min_rep']=float(min(vals)); out[field+'_max_rep']=float(max(vals))
        selected=[c for c in ch_summary if key(c)==ident]
        if selected:
            for a in (0,1,2):
                sel=[c for c in selected if a==0 or c['array']==a]
                levels=np.array([c['baseline_counts'] for c in sel]); prefix='all' if a==0 else 'A'+str(a)
                for field, val in dict(sd_median_counts=np.median([c['sd_counts'] for c in sel]),
                    sd_max_counts=max(c['sd_counts'] for c in sel),ptp_median_counts=np.median([c['ptp_counts'] for c in sel]),
                    ptp_max_counts=max(c['ptp_counts'] for c in sel),baseline_spread_counts=np.ptp(levels),
                    baseline_population_sd_counts=levels.std(ddof=0),baseline_min_counts=levels.min(),baseline_max_counts=levels.max()).items():
                    out[prefix+'_'+field]=float(val); out[prefix+'_'+field.replace('_counts','_volts')]=float(val)*LSB
        summaries.append(out)
    return summaries,ch_summary


def analyze(source, output, partial=False):
    output.mkdir(parents=True,exist_ok=True)
    cache_path=output/'analysis_cache.json'
    cache=json.loads(cache_path.read_text()) if cache_path.exists() else {}
    rows=[]; channels=[];broad_rows=[];broad_channels=[]
    for dataset, folder, expected in (('current',source/'production',252),('current',source/'diagnostic',84),
                                    ('yesterday_shared',SHARED,180),('yesterday_original',ORIGINAL,90)):
        if not (folder/'session.json').exists():
            if partial: continue
            raise ValueError('Missing session '+str(folder))
        session=json.loads((folder/'session.json').read_text())
        if not partial and (session['state']!='complete' or len(session['runs'])!=expected):
            raise ValueError('Incomplete matrix '+str(folder))
        for run in session['runs']:
            ident=dataset+'/'+folder.name+'/'+run['name']; raw=folder/run['name']/'capture.bin'
            if not partial or ident not in cache:
                if hashlib.sha256(raw.read_bytes()).hexdigest()!=run['sha256']: raise ValueError('Capture hash mismatch')
            if ident not in cache:
                if dataset=='yesterday_original': row,ch=legacy_process(folder,run,session,output)
                else: row,ch=process(folder,run,session,output,allow_insufficient_quiet=True)
                row.update(dataset=dataset,measurement_s=session['measurement_s'],warmup_s=session['warmup_s'])
                row['estimated_wire_budget_us']=row['estimated_spi_words']*16e6/row['requested_spi_clock_hz']
                row['estimated_guard_budget_us']=row['estimated_cs_allowance_us']+row['estimated_mux_switch_guard_us']
                row['remaining_acquisition_us']=row['mean_acquisition_us']-row['estimated_settle_budget_us']-row['estimated_wire_budget_us']-row['estimated_guard_budget_us']
                for c in ch:
                    c.update(dataset=dataset,requested_spi_clock_hz=row['requested_spi_clock_hz'],measurement_s=session['measurement_s'])
                if dataset=='current' and row['firmware_revision']!='adc124-vmid-settle-v1': raise ValueError('Current revision mismatch')
                cache[ident]=dict(row=row,channels=ch,sha256=run['sha256'])
            if cache[ident]['sha256']!=run['sha256']: raise ValueError('Cached identity mismatch')
            entry=cache[ident]
            if 'quiet_qualified' in entry['row']:
                if entry.get('fine_version')!=QUIET_VERSION:
                    entry.setdefault('broad_row',dict(entry['row']));entry.setdefault('broad_channels',entry['channels'])
                    entry['row'],entry['channels']=refine(folder,run,session,output,entry['broad_row'],entry['broad_channels'])
                    entry['fine_version']=QUIET_VERSION
                synchronize_capture_noise(entry['row'],entry['channels'])
                broad_rows.append(entry['broad_row']);broad_channels.extend(entry['broad_channels'])
            rows.append(cache[ident]['row']); channels.extend(cache[ident]['channels'])
            if len(rows)%20==0:
                cache_path.write_text(json.dumps(cache,allow_nan=False)); print('ANALYZED',len(rows),'captures',flush=True)
    cache_path.write_text(json.dumps(cache,allow_nan=False))
    summary,ch_summary=aggregate(rows,channels);broad_summary,broad_ch_summary=aggregate(broad_rows,broad_channels)
    comparisons=[]; channel_comparisons=[]; groups=[]; addresses=[]; fits=[]
    for setting in summary:
        selected=[c for c in ch_summary if key(c)==key(setting)]
        if not selected: continue
        identity={k:setting[k] for k in ('dataset','stage','mode','engine','vmid','settle_us','vmid_settle_effective_us','requested_spi_clock_hz')}
        for a in (1,2):
            centers=[]
            for mux in (1,2,3,4):
                ch=[c for c in selected if (c['array'],c['mux'])==(a,mux)]
                if not ch: continue
                levels=np.array([c['baseline_counts'] for c in ch]);center=float(np.median(levels));centers.append(center)
                groups.append(dict(identity,array=a,mux=mux,channels=len(ch),baseline_center_counts=center,baseline_center_volts=center*LSB,
                    baseline_spread_counts=float(np.ptp(levels)),baseline_spread_volts=float(np.ptp(levels))*LSB,
                    sd_max_counts=max(c['sd_counts'] for c in ch),ptp_max_counts=max(c['ptp_counts'] for c in ch)))
            if centers:
                groups.append(dict(identity,array=a,mux=0,channels=len(centers),
                    baseline_center_counts=float(np.median(centers)),baseline_center_volts=float(np.median(centers))*LSB,
                    adc_lane_center_spread_counts=float(np.ptp(centers)),adc_lane_center_spread_volts=float(np.ptp(centers))*LSB))
            for external in range(7):
                ch=[c for c in selected if c['array']==a and int(c['route'].split(':')[2])==external]
                if not ch: continue
                levels=np.array([c['baseline_counts'] for c in ch])
                addresses.append(dict(identity,array=a,external_input=external,adc_channels=len(ch),
                    baseline_spread_counts=float(np.ptp(levels)),baseline_spread_volts=float(np.ptp(levels))*LSB,
                    baseline_population_sd_counts=float(levels.std(ddof=0)),baseline_population_sd_volts=float(levels.std(ddof=0))*LSB))
    for mode in ('adc','mux'):
        for engine in ('blocking','dma','lpspi'):
            for vmid in (False,True):
                matched=[r for r in summary if r['dataset']=='current' and r['stage']=='production' and
                    (r['mode'],r['engine'],r['vmid'],r['requested_spi_clock_hz'])==(mode,engine,vmid,16000000) and
                    r['vmid_settle_effective_us']==r['settle_us']]
                if len(matched)<2: continue
                x=np.array([r['settle_us'] for r in matched]);y=np.array([r['mean_period_us'] for r in matched])
                slope,intercept=np.polyfit(x,y,1);residual=y-(intercept+slope*x)
                fits.append(dict(mode=mode,engine=engine,vmid=vmid,settings=len(matched),intercept_us=float(intercept),
                    slope_us_per_us=float(slope),expected_slope=(14 if mode=='adc' else 7)*(2 if vmid else 1),
                    residual_rms_us=float(np.sqrt(np.mean(residual**2))),residual_max_abs_us=float(np.abs(residual).max())))
    for current in summary:
        if current['dataset']!='current' or current['stage']!='production': continue
        if current['requested_spi_clock_hz']!=16000000 or current['vmid_settle_effective_us']!=current['settle_us']: continue
        for dataset in ('yesterday_shared','yesterday_original'):
            mode=current['mode'] if dataset=='yesterday_shared' else 'adc'
            old=next((r for r in summary if r['dataset']==dataset and r['stage']=='production' and
                (r['mode'],r['engine'],r['vmid'],r['settle_us'])==(mode,current['engine'],current['vmid'],current['settle_us'])),None)
            if not old: continue
            pair={k:current[k] for k in ('mode','engine','vmid','settle_us')}
            pair.update(reference=dataset,reference_mode=mode,current_rate_hz=current['delivered_sweeps_s'],old_rate_hz=old['delivered_sweeps_s'],
                speed_gain_percent=100*(old['mean_period_us']/current['mean_period_us']-1),
                current_qualified_repetitions=current['qualified_repetitions'],old_qualified_repetitions=old['qualified_repetitions'])
            for field in ('all_sd_max_counts','all_sd_median_counts','all_ptp_max_counts','A1_baseline_spread_counts','A2_baseline_spread_counts'):
                if field in current and field in old:
                    pair['current_'+field]=current[field];pair['old_'+field]=old[field];pair['change_'+field]=current[field]-old[field]
            comparisons.append(pair)
            for route in ROUTES:
                cur=next((c for c in ch_summary if key(c)==key(current) and c['route']==str(route)),None)
                prev=next((c for c in ch_summary if key(c)==key(old) and c['route']==str(route)),None)
                if cur and prev:
                    channel_comparisons.append(dict(**{k:pair[k] for k in ('mode','engine','vmid','settle_us','reference')},route=str(route),
                        current_sd_counts=cur['sd_counts'],old_sd_counts=prev['sd_counts'],sd_change_counts=cur['sd_counts']-prev['sd_counts'],
                        baseline_shift_counts=cur['baseline_counts']-prev['baseline_counts'],
                        baseline_shift_volts=(cur['baseline_counts']-prev['baseline_counts'])*LSB,
                        current_ptp_counts=cur['ptp_counts'],old_ptp_counts=prev['ptp_counts']))
    for name,records in [('capture_metrics',rows),('summary',summary),('channel_repetitions',channels),
                         ('channel_summary',ch_summary),('firmware_comparisons',comparisons),('channel_comparisons',channel_comparisons),
                         ('mux_group_summary',groups),('address_baseline_spread',addresses),('delay_fits',fits),
                         ('broader_window_capture_metrics',broad_rows),('broader_window_summary',broad_summary),('broader_window_channel_summary',broad_ch_summary)]:
        csv_write(output/(name+'.csv'),records)
    life=json.loads((source/'firmware_lifecycle.json').read_text())
    if not partial and life['state']!='complete_production_restored_verified': raise ValueError('Production restoration must be verified')
    result=dict(rows=rows,summary=summary,channels=ch_summary,comparisons=comparisons,channel_comparisons=channel_comparisons,
        groups=groups,addresses=addresses,fits=fits,
        broad_summary=broad_summary,broad_channels=broad_ch_summary,quiet_method_version=QUIET_VERSION,
        lifecycle=life,raw_root=str(source.relative_to(ROOT)),lsb_volts=LSB,reference_roots=[str(p.relative_to(ROOT)) for p in (SHARED,ORIGINAL)])
    (output/'analysis.json').write_text(json.dumps(result,allow_nan=False))
    print('COMPLETE analysis',len(rows),'captures',len(summary),'settings',flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('source',type=Path);parser.add_argument('output',type=Path)
    parser.add_argument('--partial',action='store_true'); args=parser.parse_args();analyze(args.source.resolve(),args.output.resolve(),args.partial)
