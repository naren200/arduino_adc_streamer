"""Connected-versus-disconnected comparison at identical lookahead settings."""
from pathlib import Path
import argparse, hashlib, json
import numpy as np
from copy import deepcopy
from analyze_adc124_scan_modes import ROOT, csv_write
from analyze_adc124_upgrade import ROUTES, LSB, aggregate, key
from analyze_adc124_lookahead_lpspi import analyze_capture

REFERENCE=ROOT/'docs/testing/results/testboard-adc124/selector_lookahead_lpspi_2026-10-14/analysis.json'
VERSION=1


def setting_key(r):
    return tuple(r[k] for k in ('mode','engine','vmid','settle_us','vmid_settle_effective_us','requested_spi_clock_hz'))


def analyze(source,out,partial=False):
    out.mkdir(parents=True,exist_ok=True);cp=out/'connected_analysis_cache.json'
    cache=json.loads(cp.read_text()) if cp.exists() else {}
    session=json.loads((source/'production/session.json').read_text())
    if not partial and (session['state']!='complete' or len(session['runs'])!=84):raise ValueError('Incomplete connected matrix')
    rows=[];channels=[];rawrows=[];rawchannels=[]
    reference=json.loads(REFERENCE.read_text())
    old_identity=json.loads((ROOT/reference['raw_root']/'current_firmware/identity.json').read_text())['source_image_sha256']
    new_identity=json.loads((source/'current_firmware/identity.json').read_text())['source_image_sha256']
    firmware_files_match={name:old_identity.get(name)==digest for name,digest in new_identity.items()}
    if not all(firmware_files_match.values()):raise ValueError('Frozen local firmware snapshots differ from reference')
    for field,dest in [('rows',rows),('raw_rows',rawrows),('raw_channel_repetitions',rawchannels)]:
        for r in reference[field]:
            if r['dataset']=='lookahead':
                item=deepcopy(r);item['dataset']='disconnected';dest.append(item)
    # Quiet per-repetition records are retained in the previous analysis cache.
    previous_cache=json.loads((REFERENCE.parent/'analysis_cache.json').read_text())
    for name,c in previous_cache.items():
        if name.startswith('lookahead/'):
            for r in c['channels']:
                item=deepcopy(r);item['dataset']='disconnected';channels.append(item)
    for number,run in enumerate(session['runs'],1):
        raw=source/'production'/run['name']/'capture.bin'
        if hashlib.sha256(raw.read_bytes()).hexdigest()!=run['sha256']:raise ValueError('Connected capture hash mismatch')
        ident=run['name']
        if ident not in cache:
            cache[ident]=analyze_capture(source/'production',run,session,out,'connected')
        c=cache[ident]
        if c['sha256']!=run['sha256'] or c['row']['firmware_revision']!='adc124-selector-lookahead-v1':raise ValueError('Identity mismatch')
        rows.append(c['row']);channels.extend(c['channels']);rawrows.append(c['raw_row']);rawchannels.extend(c['raw_channels'])
        if number%5==0:
            cp.write_text(json.dumps(cache,allow_nan=False));print('Analyzed connected',number,'captures',flush=True)
    cp.write_text(json.dumps(cache,allow_nan=False))
    quiet_summary,quiet_channels=aggregate(rows,channels);raw_summary,raw_channels=aggregate(rawrows,rawchannels)
    adc_inputs=[];adc_spread=[];address_spread=[]
    for setting in raw_summary:
        selected=[c for c in raw_channels if key(c)==key(setting)]
        base={k:setting[k] for k in ('dataset','stage','mode','engine','vmid','settle_us','vmid_settle_effective_us','requested_spi_clock_hz')}
        for array in (1,2):
            centers=[]
            for mux in (1,2,3,4):
                values=np.array([c['baseline_counts'] for c in selected if (c['array'],c['mux'])==(array,mux)])
                center=float(np.median(values));centers.append(center)
                adc_inputs.append(dict(base,array=array,adc_input=mux,mux=mux,external_routes=len(values),center_counts=center,center_volts=center*LSB,
                    route_spread_counts=float(np.ptp(values)),route_spread_volts=float(np.ptp(values))*LSB,
                    route_population_sd_counts=float(values.std()),route_population_sd_volts=float(values.std())*LSB))
            item=dict(base,array=array,center_spread_counts=float(np.ptp(centers)),center_spread_volts=float(np.ptp(centers))*LSB,
                center_population_sd_counts=float(np.std(centers)),center_population_sd_volts=float(np.std(centers))*LSB)
            for mux,value in enumerate(centers,1):item[f'IN{mux}_counts']=value;item[f'IN{mux}_volts']=value*LSB
            adc_spread.append(item)
            for address in range(6):
                values=np.array([c['baseline_counts'] for c in selected if c['array']==array and int(c['route'].split(':')[-1])==address])
                address_spread.append(dict(base,array=array,external_address=address,adc_inputs=4,spread_counts=float(np.ptp(values)),
                    spread_volts=float(np.ptp(values))*LSB,population_sd_counts=float(values.std()),population_sd_volts=float(values.std())*LSB))
    comparisons=[];pairs=[];excursions=[]
    for cur in raw_summary:
        if cur['dataset']!='connected':continue
        old=next(r for r in raw_summary if r['dataset']=='disconnected' and setting_key(r)==setting_key(cur))
        pair={k:cur[k] for k in ('mode','engine','vmid','settle_us','vmid_settle_effective_us','requested_spi_clock_hz')}
        pair.update(disconnected_rate_hz=old['delivered_sweeps_s'],connected_rate_hz=cur['delivered_sweeps_s'],
            rate_change_percent=100*(cur['delivered_sweeps_s']/old['delivered_sweeps_s']-1),
            disconnected_acquisition_us=old['mean_acquisition_us'],connected_acquisition_us=cur['mean_acquisition_us'],
            acquisition_change_us=cur['mean_acquisition_us']-old['mean_acquisition_us'],
            disconnected_counter_rate_estimate_hz=old['sampling_sweeps_s_estimate_whole_run'],
            connected_counter_rate_estimate_hz=cur['sampling_sweeps_s_estimate_whole_run'])
        for name in ('all_sd_median_counts','all_sd_max_counts','all_ptp_max_counts','A1_baseline_spread_counts','A2_baseline_spread_counts'):
            pair['disconnected_'+name]=old[name];pair['connected_'+name]=cur[name]
        comparisons.append(pair)
        event={k:pair[k] for k in ('mode','engine','vmid','settle_us','vmid_settle_effective_us','requested_spi_clock_hz')}
        for dataset in ('connected','disconnected'):
            selected=[r for r in rawchannels if r['dataset']==dataset and setting_key(r)==setting_key(cur)]
            peak=max(selected,key=lambda r:r['ptp_counts']);worst_sd=max(selected,key=lambda r:r['sd_counts'])
            event[dataset+'_max_ptp_counts']=peak['ptp_counts'];event[dataset+'_max_ptp_volts']=peak['ptp_volts']
            event[dataset+'_max_sd_counts']=worst_sd['sd_counts'];event[dataset+'_max_sd_volts']=worst_sd['sd_volts']
            event[dataset+'_peak_route']=peak['route'];event[dataset+'_peak_repetition']=peak['repetition']
        excursions.append(event)
        for route in ROUTES:
            a=next(c for c in raw_channels if key(c)==key(cur) and c['route']==str(route))
            b=next(c for c in raw_channels if key(c)==key(old) and c['route']==str(route))
            item=dict(pair,route=str(route),array=route.array,mux=route.mux,sensor='PZT'+str(route.sensor),sensor_channel=route.sensor_channel)
            for metric in ('baseline','sd','ptp'):
                item['disconnected_'+metric+'_counts']=b[metric+'_counts'];item['connected_'+metric+'_counts']=a[metric+'_counts']
                item[metric+'_change_counts']=a[metric+'_counts']-b[metric+'_counts']
                for state in ('connected','disconnected',metric+'_change'):
                    field=state+'_'+metric if state in ('connected','disconnected') else state
                    item[field+'_volts']=item[field+'_counts']*LSB
            pairs.append(item)
    for name,records in [('capture_metrics',rows),('channel_repetitions',channels),('summary',quiet_summary),('channel_summary',quiet_channels),
        ('unfiltered_capture_metrics',rawrows),('unfiltered_channel_repetitions',rawchannels),('unfiltered_summary',raw_summary),
        ('unfiltered_channel_summary',raw_channels),('setup_comparisons',comparisons),('channel_comparisons',pairs),('excursion_audit',excursions),
        ('adc_input_baselines',adc_inputs),('adc_input_spread',adc_spread),('address_input_spread',address_spread)]:csv_write(out/(name+'.csv'),records)
    lifecycle=json.loads((source/'firmware_lifecycle.json').read_text())
    if not partial and lifecycle['state']!='complete_production_restored_verified':raise ValueError('Firmware settings restoration incomplete')
    result=dict(version=VERSION,partial=partial,rows=rows,channels=quiet_channels,summary=quiet_summary,raw_rows=rawrows,
        raw_summary=raw_summary,raw_channels=raw_channels,raw_channel_repetitions=rawchannels,comparisons=comparisons,
        channel_comparisons=pairs,lifecycle=lifecycle,reference_analysis=str(REFERENCE),raw_root=str(source),lsb_volts=LSB,
        local_firmware_snapshot_matches=firmware_files_match,excursion_audit=excursions,adc_input_baselines=adc_inputs,
        adc_input_spread=adc_spread,address_input_spread=address_spread)
    (out/'analysis.json').write_text(json.dumps(result,allow_nan=False))
    print('Analysis:',len(session['runs']),'connected captures;',len(comparisons),'matched settings',flush=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path);p.add_argument('output',type=Path);p.add_argument('--partial',action='store_true')
    a=p.parse_args();analyze(a.source.resolve(),a.output.resolve(),a.partial)
