"""Compare inherited, explicit 5 us and shorter VMID waits using raw evidence."""
from pathlib import Path
import argparse
import hashlib
import json
import numpy as np

from analyze_adc124_scan_modes import process,summarize,csv_write,ROOT


def analyze(source,output,partial=False):
    source=source.resolve(); output=output.resolve(); output.mkdir(parents=True,exist_ok=True)
    cache_path=output/'analysis_cache.json'
    saved=json.loads(cache_path.read_text()) if cache_path.exists() else {}
    cache=saved.get('captures',{}) if saved.get('format_version') == 2 else {}
    rows=[]; channels=[]
    for stage,expected_count in (('before_production',18),('after_production',72),('after_diagnostic',24)):
        folder=source/stage
        if not (folder/'session.json').exists():
            if partial: continue
            raise ValueError('Missing '+stage)
        session=json.loads((folder/'session.json').read_text())
        if not partial and (session['state']!='complete' or len(session['runs'])!=expected_count):
            raise ValueError('Incomplete matrix '+stage)
        for run in session['runs']:
            key=stage+'/'+run['name']; raw=folder/run['name']/'capture.bin'
            if key not in cache or not partial:
                if hashlib.sha256(raw.read_bytes()).hexdigest()!=run['sha256']: raise ValueError('Raw hash mismatch')
            if key not in cache:
                row,ch=process(folder,run,session,output,allow_insufficient_quiet=True)
                expected='adc124-fifo-grouped-gpio-v2' if stage.startswith('before') else 'adc124-vmid-settle-v1'
                if row['firmware_revision']!=expected: raise ValueError('Firmware revision mismatch')
                row['vmid_setting']='before' if stage.startswith('before') else ('default' if run['vmid_settle_us'] is None else str(run['vmid_settle_us']))
                for channel in ch: channel['vmid_setting']=row['vmid_setting']
                cache[key]=dict(row=row,channels=ch,sha256=run['sha256'])
            if cache[key]['sha256']!=run['sha256']: raise ValueError('Cache identity mismatch')
            rows.append(cache[key]['row']); channels.extend(cache[key]['channels'])
    cache_path.write_text(json.dumps(dict(format_version=2,captures=cache),allow_nan=False))
    summary=summarize(rows); comparisons=[]; channel_pairs=[]
    for row in summary:
        if row['stage']!='after_production': continue
        mode,engine=row['mode'],row['engine']
        before=next(r for r in summary if r['stage']=='before_production' and r['mode']==mode and r['engine']==engine)
        match=next((r for r in summary if r['stage']=='after_production' and r['mode']==mode and r['engine']==engine and r['vmid_settle_us']==5),None)
        setting='default' if row['vmid_settle_us'] is None else str(int(row['vmid_settle_us']))
        c=dict(mode=mode,engine=engine,vmid_setting=setting,vmid_effective_us=row['vmid_settle_effective_us'],
            before_rate_hz=before['delivered_sweeps_s'],after_rate_hz=row['delivered_sweeps_s'],
            before_period_us=before['mean_period_us'],after_period_us=row['mean_period_us'],
            rate_change_vs_before_percent=100*(before['mean_period_us']/row['mean_period_us']-1),
            measured_period_saved_vs_5_us=match['mean_period_us']-row['mean_period_us'] if match else None,
            rate_gain_vs_5_percent=100*(match['mean_period_us']/row['mean_period_us']-1) if match else None,
            estimated_saved_vs_5_us=(14 if mode=='adc' else 7)*(5-row['vmid_settle_effective_us']),
            repetitions=int(row['repetitions']))
        pairs=[]
        for route in sorted({v['route'] for v in channels}):
            eligible=[v for v in channels if v['mode']==mode and v['engine']==engine and v['route']==route]
            old=[v for v in eligible if v['vmid_setting']=='before']
            new=[v for v in eligible if v['vmid_setting']==setting]
            ref=[v for v in eligible if v['vmid_setting']=='5']
            if not old or not new: continue
            p=dict(mode=mode,engine=engine,vmid_setting=setting,route=route,
                before_sd_counts=float(np.median([v['sd_counts'] for v in old])),
                after_sd_counts=float(np.median([v['sd_counts'] for v in new])),
                explicit5_sd_counts=float(np.median([v['sd_counts'] for v in ref])) if ref else None,
                before_baseline_counts=float(np.median([v['baseline_counts'] for v in old])),
                after_baseline_counts=float(np.median([v['baseline_counts'] for v in new])),
                before_quiet_repetitions=len(old),after_quiet_repetitions=len(new))
            p['baseline_shift_vs_before_counts']=p['after_baseline_counts']-p['before_baseline_counts']
            p['sd_change_vs_before_counts']=p['after_sd_counts']-p['before_sd_counts']
            p['sd_change_vs_5_counts']=p['after_sd_counts']-p['explicit5_sd_counts'] if ref else None
            pairs.append(p); channel_pairs.append(p)
        c.update(matched_quiet_channels=len(pairs),
            before_worst_sd_counts=max((p['before_sd_counts'] for p in pairs),default=None),
            after_worst_sd_counts=max((p['after_sd_counts'] for p in pairs),default=None),
            largest_sd_increase_vs_5_counts=max((p['sd_change_vs_5_counts'] for p in pairs if p['sd_change_vs_5_counts'] is not None),default=None),
            largest_abs_baseline_shift_vs_before_counts=max((abs(p['baseline_shift_vs_before_counts']) for p in pairs),default=None))
        comparisons.append(c)
    csv_write(output/'capture_metrics.csv',rows); csv_write(output/'summary.csv',summary)
    csv_write(output/'comparisons.csv',comparisons); csv_write(output/'channels.csv',channels)
    csv_write(output/'channel_comparisons.csv',channel_pairs)
    result=dict(raw_root=str(source.relative_to(ROOT)),state='partial' if partial else 'complete',
        rows=rows,summary=summary,comparisons=comparisons,channel_comparisons=channel_pairs)
    (output/'analysis.json').write_text(json.dumps(result,indent=2,allow_nan=False))
    print(f'Analyzed {len(rows)} captures / {len(comparisons)} settings',flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('source',type=Path)
    parser.add_argument('output',type=Path); parser.add_argument('--partial',action='store_true')
    args=parser.parse_args(); analyze(args.source,args.output,args.partial)
