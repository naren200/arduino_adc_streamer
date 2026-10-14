"""Timing budgets from raw production captures and existing diagnostic profiles."""
from pathlib import Path
import json
import csv
import hashlib
import argparse
import numpy as np
from capture_adc124_settling import decode, bench
from adc124_timing_model import operation_counts, fit_delay

ENGINES=('blocking','dma','lpspi')
SETTLES=(1,2,5,10,50)
LSB=3.3/4096

def csv_write(path,rows):
    with path.open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)

def load_runs(source):
    session=json.loads((source/'session.json').read_text())
    if session['state']!='complete':raise ValueError(f'Incomplete session: {source}')
    rows=[]
    for run in session['runs']:
        folder=source/run['name']
        if hashlib.sha256((folder/'capture.bin').read_bytes()).hexdigest()!=run['sha256']:
            raise ValueError('Hash mismatch')
        frames=decode(folder/'capture.bin')
        t=np.r_[0,np.cumsum(np.diff(frames['start'].astype(np.int64))%2**32)]/1e6
        use=t>=session['warmup_s'];period=np.diff(frames['start'].astype(np.int64))%2**32
        acq=(frames['end'].astype(np.int64)-frames['start'].astype(np.int64))%2**32
        # Pair each period with the acquisition that precedes it.
        interval_use=use[:-1]&use[1:]
        gaps=period[interval_use]-acq[:-1][interval_use]
        if np.any(gaps<0):raise ValueError('Overlapping device acquisition timestamps')
        status=json.loads((folder/'status.json').read_text())
        engine=run.get('engine',status['after']['spiengine']);order=run.get('order',status['after']['scanorder'])
        profile=json.loads((folder/'firmware_profile.jsonl').read_text().splitlines()[-1])['summary']
        values=frames['samples'][use].astype(float)
        base=np.median(values,axis=0)
        cnt=operation_counts(order,run['vmid'])
        meanperiod=float(period[interval_use].mean())
        row={'source':str(source),'name':run['name'],'repetition':run['repetition'],'engine':engine,
            'order':order,'vmid':run['vmid'],'settle_us':run['settle_us'],'profile':profile['enabled'],
            'frames':run['frames'],'measured_frames':int(use.sum()),'acquired':run['sampling_sweeps'],
            'discarded':run['usb_frames_discarded'],'mean_period_us':meanperiod,
            'rate_hz_channel':1e6/meanperiod,'values_per_s':50e6/meanperiod,
            'mean_acquisition_us':float(acq[:-1][interval_use].mean()),
            'mean_gap_us':float(gaps.mean()),'period_p99_us':float(np.quantile(period[interval_use],.99)),
            'settle_budget_us':cnt['address_changes']*run['settle_us'],
            'wire_budget_us':cnt['wire_clock_us'],'explicit_allowances_us':cnt['switch_allowance_us']+cnt['cs_allowance_us'],
            'address_changes':cnt['address_changes'],'words':cnt['words'],
            'USB_bytes_s':114e6/meanperiod,'baseline_spread_A_counts':float(np.ptp(base[:25])),
            'baseline_spread_B_counts':float(np.ptp(base[25:])),
            'max_channel_std_counts':float(values.std(axis=0,ddof=1).max()),
            'max_channel_ptp_counts':float(np.ptp(values,axis=0).max())}
        row['remaining_acquisition_us']=row['mean_acquisition_us']-row['settle_budget_us']-row['wire_budget_us']-row['explicit_allowances_us']
        row['effective_us_per_sensor']=meanperiod/50
        if profile['enabled']:
            for phase,data in profile['phases'].items():row['profile_'+phase+'_us']=data['mean_us']
            row['profile_clock_hz']=profile['clock_hz']
            row['profile_probe_min_ns']=profile['probe_cycles_min']/profile['clock_hz']*1e9
            row['profile_probe_max_ns']=profile['probe_cycles_max']/profile['clock_hz']*1e9
            row['profile_prepare_fixed_us']=row['profile_prepare_us']-cnt['sensor_address_changes']*run['settle_us']-(cnt['mandatory_park_changes']*run['settle_us'])
            # transfer scope includes VMID parking, unlike prepare.
            row['profile_transfer_excluding_vmid_settle_us']=row['profile_transfer_us']-cnt['vmid_park_changes']*run['settle_us']
        rows.append(row)
    return session,rows

def summarize(rows):
    summaries=[]
    keys=sorted({(r['order'],r['engine'],r['vmid'],r['settle_us']) for r in rows})
    numeric=[k for k,v in rows[0].items() if isinstance(v,(float,int)) and k not in ('repetition','settle_us','vmid','profile')]
    for order,engine,vmid,settle in keys:
        selected=[r for r in rows if (r['order'],r['engine'],r['vmid'],r['settle_us'])==(order,engine,vmid,settle)]
        result=dict(order=order,engine=engine,vmid=vmid,settle_us=settle,repetitions=len(selected))
        for key in numeric:
            result[key]=float(np.median([r[key] for r in selected]))
            if key in ('rate_hz_channel','mean_period_us'):
                result[key+'_min']=float(min(r[key] for r in selected));result[key+'_max']=float(max(r[key] for r in selected))
        summaries.append(result)
    return summaries

def unit_models(profiles):
    """Estimate uniform word/batch costs from two operation-count topologies."""
    models=[]
    for engine in ENGINES:
        rows=[r for r in profiles if r['engine']==engine and not r['vmid']]
        design=np.array([[r['words'],operation_counts(r['order'],False)['address_groups']] for r in rows])
        target=np.array([r['profile_transfer_us'] for r in rows])
        word,batch=np.linalg.lstsq(design,target,rcond=None)[0]
        residual=target-design@np.array([word,batch])
        switches=[r['profile_prepare_fixed_us']/r['address_changes'] for r in rows]
        models.append(dict(engine=engine,word_us=float(word),batch_overhead_us=float(batch),
                           switch_residual_us=float(np.median(switches)),
                           transfer_fit_rms_us=float(np.sqrt(np.mean(residual**2)))))
    return models

def analyze(main,control,profile,output):
    s_main,production=load_runs(main);s_control,controls=load_runs(control)
    s_profile,profiles=load_runs(profile) if profile else ({'state':'unavailable; awaiting hardware recovery'},[])
    if len(production)!=90 or len(controls)!=30 or (profile and len(profiles)!=60):
        raise ValueError('Expected 90 primary, 30 control and 60 diagnostic captures')
    output.mkdir(parents=True,exist_ok=True)
    combined=production+controls
    summary=summarize(combined);psummary=summarize(profiles) if profiles else []
    fits=[]
    for order in ('channel','mux'):
        for engine in ENGINES:
            for vmid in (False,True):
                selected=[r for r in summary if r['order']==order and r['engine']==engine and r['vmid']==vmid]
                for field in ('mean_period_us','mean_acquisition_us','mean_gap_us'):
                    fits.append(dict(order=order,engine=engine,vmid=vmid,quantity=field,
                        expected_settle_slope=operation_counts(order,vmid)['address_changes'],
                        **fit_delay([r['settle_us'] for r in selected],[r[field] for r in selected])))
    csv_write(output/'production_capture_metrics.csv',combined)
    csv_write(output/'production_summary.csv',summary)
    if profiles:
        csv_write(output/'diagnostic_capture_metrics.csv',profiles)
        csv_write(output/'diagnostic_summary.csv',psummary)
    csv_write(output/'delay_fits.csv',fits)
    units=unit_models(profiles) if profiles else []
    if units:csv_write(output/'unit_cost_estimates.csv',units)
    result=dict(production_sessions=[s_main,s_control],profile_session=s_profile,
                production=combined,profiles=profiles,summary=summary,profile_summary=psummary,fits=fits,
                firmware_lifecycle=json.loads((output/'firmware_lifecycle.json').read_text()),unit_models=units)
    (output/'timing_analysis.json').write_text(json.dumps(result,indent=2))
    return result

def build_report(data,output,pdf):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,Image,PageBreak
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.enums import TA_CENTER
    summary=data['summary'];profiles=data['profile_summary'];fits=data['fits']
    def get(s,v,e='lpspi',o='channel',profile=False):
        if profile and not profiles:return {}
        return next(r for r in (profiles if profile else summary) if (r['settle_us'],r['vmid'],r['engine'],r['order'])==(s,v,e,o))
    def fit(e,v,o='channel',quantity='mean_period_us'):
        return next(r for r in fits if (r['engine'],r['vmid'],r['order'],r['quantity'])==(e,v,o,quantity))
    plt.rcParams.update({'font.size':9,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.alpha':.15})
    palette=['#658c49','#b66525','#2278b4']
    def save(fig,name):
        fig.tight_layout();path=output/(name+'.png');fig.savefig(path,dpi=180);plt.close(fig);return path
    fig,axes=plt.subplots(1,2,figsize=(10,3.6))
    for v,ax in zip((False,True),axes):
        for e,c in zip(ENGINES,palette):
            y=np.array([get(s,v,e)['rate_hz_channel'] for s in SETTLES])
            low=np.array([get(s,v,e)['rate_hz_channel_min'] for s in SETTLES]);high=np.array([get(s,v,e)['rate_hz_channel_max'] for s in SETTLES])
            ax.errorbar(SETTLES,y,yerr=[y-low,high-y],fmt='o-',color=c,label=e,capsize=3)
        ax.set_xscale('log');ax.set_xticks(SETTLES,labels=list(map(str,SETTLES)));ax.set_xlabel('Settling delay (us)');ax.set_ylabel('Samples/s/channel');ax.set_title('ADC order / VMID '+('on' if v else 'off'));ax.legend()
    rates=save(fig,'three_engine_rates')
    fig,axes=plt.subplots(1,2,figsize=(10,4))
    colorset=['#467fbd','#95b5ce','#ce8d39','#719a6f','#8c738d']
    for v,ax in zip((False,True),axes):
        bottom=np.zeros(5)
        for key,label,c in zip(('settle_budget_us','wire_budget_us','explicit_allowances_us','remaining_acquisition_us','mean_gap_us'),('Requested settling','SPI clocking','Explicit 0.4/0.2 us allowances','Remaining acquisition','Outside acquisition'),colorset):
            values=np.array([get(s,v)[key] for s in SETTLES]);ax.bar(np.arange(5),values,bottom=bottom,label=label,color=c);bottom+=values
        ax.set_xticks(range(5),labels=list(map(str,SETTLES)));ax.set_xlabel('Settling delay (us)');ax.set_ylabel('Mean sweep period (us)');ax.set_title('LPSPI / VMID '+('on' if v else 'off'))
    axes[1].legend(fontsize=7)
    budgetplot=save(fig,'production_timing_budget')
    fig,axes=plt.subplots(1,2,figsize=(10,3.6))
    for v,ax in zip((False,True),axes):
        for e,c in zip(ENGINES,palette):
            for o,style in (('channel','o--'),('mux','s-')):
                ax.plot(SETTLES,[get(s,v,e,o)['rate_hz_channel'] for s in SETTLES],style,color=c,label=e+' / '+('ADC' if o=='channel' else 'address'))
        ax.set_xscale('log');ax.set_xticks(SETTLES,labels=list(map(str,SETTLES)));ax.set_xlabel('Settling delay (us)');ax.set_ylabel('Samples/s/channel');ax.set_title('VMID '+('on' if v else 'off'));ax.legend(fontsize=7)
    orderplot=save(fig,'scan_order_rates')
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='ADCtitle',fontName='Helvetica-Bold',fontSize=22,leading=26,alignment=TA_CENTER,textColor=colors.HexColor('#184f73'),spaceAfter=16))
    styles.add(ParagraphStyle(name='ADCbody',fontName='Helvetica',fontSize=9,leading=12,spaceAfter=8,textColor=colors.HexColor('#203747')))
    styles.add(ParagraphStyle(name='ADCsmall',fontName='Helvetica',fontSize=7.5,leading=10,spaceAfter=6,textColor=colors.HexColor('#203747')))
    styles['Heading1'].textColor=colors.HexColor('#184f73')
    story=[]
    def p(t,small=False):story.append(Paragraph(t,styles['ADCsmall' if small else 'ADCbody']))
    def h(t):story.append(Paragraph(t,styles['Heading1']))
    def page():story.append(PageBreak())
    def image(path,height):story.append(Image(str(path),width=490,height=height))
    def table(headers,rows,widths,size=8,pad=4):
        rows=[[Paragraph(str(c),styles['ADCsmall']) for c in headers]]+rows
        t=Table(rows,colWidths=widths,repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e6eef3')),('FONTNAME',(0,1),(-1,-1),'Helvetica'),('FONTSIZE',(0,1),(-1,-1),size),('LEADING',(0,1),(-1,-1),size+2),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),pad),('BOTTOMPADDING',(0,0),(-1,-1),pad),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f3f5f7')])]))
        story.append(t);story.append(Spacer(1,9))
    story.append(Paragraph('TestBoard_ADC124<br/>Timing budget and SPI engine comparison',styles['ADCtitle']))
    p('13 October 2026 | Both full arrays | 16 MHz requested SPI | Manual / repeat 1')
    if not profiles:p('<b>Production measurements complete; detailed diagnostic profiling and firmware recovery pending a Program-button press.</b> The diagnostic upload reported success, but no USB serial device reappeared. No diagnostic phase values are presented as measurements.')
    h('Production sampling rate: three engines')
    p('Same open-connector, charger-connected setup and bias resistors as the settling report. ADC-order scan, five settling delays and VMID off/on. Three fresh repetitions per setting: 1 s warm-up + 5 s measurement. Rates use device timestamps, not host arrival or disk-write time. Diagnostic firmware results are separate.')
    rows=[]
    for s in SETTLES:
        for v in (False,True):
            r=[get(s,v,e) for e in ENGINES]
            rows.append([s,'on' if v else 'off',*[f'{x["rate_hz_channel"]:,.1f}' for x in r],f'{100*(r[2]["rate_hz_channel"]/r[0]["rate_hz_channel"]-1):.1f}%',f'{100*(r[2]["rate_hz_channel"]/r[1]["rate_hz_channel"]-1):.1f}%'])
    table(['Settle us','VMID','Blocking Hz/ch','DMA Hz/ch','LPSPI Hz/ch','LPSPI vs blocking','LPSPI vs DMA'],rows,[44,40,83,80,83,80,80])
    image(rates,176)
    p('Cells are medians of three repetitions; graph error bars show the repetition range. One sweep = one value for each of 50 sensor routes. Aggregate useful values/s = 50 x Hz/channel. All three engines use the same shared SPI bus and acquire the two ADCs sequentially.')
    page();h('What happens in one sweep')
    rows=[]
    for o in ('channel','mux'):
        for v in (False,True):
            c=operation_counts(o,v)
            rows.append(['ADC' if o=='channel' else 'Address','on' if v else 'off',c['address_groups'],c['address_changes'],c['prime_words'],c['sensor_words'],c['vmid_words'],c['words']])
    table(['Order','VMID','Groups','Actual address changes','Prime words','Useful words','VMID words','Total words'],rows,[62,41,44,80,64,62,66,51])
    p('<b>ADC order:</b> array, MUX / ADC input, external input. Each of the 50 sensor routes forms its own address group. Each group clocks one selector prime and one retained sample. VMID-on additionally parks the MUX quad and clocks five discarded words (one prime + four ADC inputs).')
    p('<b>Address order (firmware scanorder mux):</b> seven external addresses per array; addresses 0-5 read four ADC inputs, and address 6 reads only MUX4. One shared selector prime per group reduces the sensor+prime word count from 100 to 64. All 50 physical routes are still measured.')
    p('VMID-off adds two real park changes per sweep, one at the array handoff and one at the end. VMID-on already parks after each group, so those two additional park calls return without changing GPIOs or waiting. Counting calls rather than actual address changes would overestimate VMID-on settling by 2 x delay.')
    table(['Operation','Nominal component at 16 MHz','Evidence'],[['MUX change','EN off; 200 ns; 3 address writes; EN on; 200 ns; settling delay','Firmware sequence'],['GPIO writes','5 writes per actual address change','Firmware sequence'],['ADC track window','3 SCLK cycles = 0.1875 us','TI datasheet'],['Conversion / serial output','13 SCLK cycles = 0.8125 us','TI datasheet'],['Complete ADC word','16 SCLK cycles = 1.0000 us','Clock-derived'],['CS allowances','100 ns setup + 100 ns high per word','Firmware delays'],['USB frame','4-byte header + 100-byte values + 10-byte timing = 114 bytes','Protocol']], [127,218,145])
    p('ADC tracking, conversion and data shifting occupy the same 16-clock frame; they must not be added again as separate delays. The DIN selector applies to the following conversion. These ADC timing facts follow TI ADC124S101 datasheet SNAS283D, sections Operation / Using the ADC and timing specifications. <link href="https://www.ti.com/lit/ds/symlink/adc124s101.pdf">TI datasheet</link>.',True)
    p('The pinned SPI library configures a 240 MHz peripheral root and divider 15 for a 16 MHz request. Timing calculations use nominal 16 MHz; SCLK and GPIO edge times were not independently scoped. The 0.4 us MUX allowance is a firmware wait budget, not a measured electrical switch propagation time.',True)
    page();h('Calculated production timing budget: LPSPI')
    p('T = 1,000,000 / rate = acquisition + outside-acquisition gap. Acquisition contains requested settling, SPI clocking, explicit MUX/CS allowances, and a remaining cost. The remaining cost includes GPIO/register work, batch/transfer loops, delay-call overhead and interrupts; it is not a uniquely identified phase.')
    rows=[]
    for s in SETTLES:
        for v in (False,True):
            r=get(s,v)
            rows.append([s,'on' if v else 'off',*[f'{r[k]:.2f}' for k in ('settle_budget_us','wire_budget_us','explicit_allowances_us','remaining_acquisition_us','mean_gap_us','mean_period_us')]])
    table(['Settle us','VMID','Settling us','SPI clock us','Explicit waits us','Remaining acquisition us','Gap us','Total period us'],rows,[40,36,68,65,67,82,56,76],7.5)
    image(budgetplot,196)
    p('Settling = 52 x delay with VMID off, or 100 x delay with it on. SPI clocking = 100 or 350 us. Explicit allowance totals = 52 x 0.4 + 100 x 0.2 = 40.8 us off, or 100 x 0.4 + 350 x 0.2 = 110.0 us on. These are additive nominal budgets; the residual closes the measured accounting.')
    p('Rows use median repetition means. All CSV calculations retain full precision. Acquisition is measured by firmware start/end timestamps. The gap is paired with the preceding acquisition, avoiding errors from subtracting unrelated time intervals.')
    page();h('Timing slopes and fixed engine costs')
    p('Fit T(delay) = intercept + slope x delay to the five measured settling settings. Slope checks how many settling waits occur per sweep. The zero-delay intercept is an extrapolated combined fixed cost; it does not isolate switch, transfer or USB time.')
    rows=[]
    for e in ENGINES:
        for v in (False,True):
            f=fit(e,v)
            rows.append([e,'on' if v else 'off',f'{f["slope_us_per_us"]:.3f}',f['expected_settle_slope'],f'{f["intercept_us"]:.2f}',f'{f["residual_max_us"]:.3f}'])
    table(['Engine','VMID','Measured slope us/us','Expected switches','Fixed intercept us','Max fit residual us'],rows,[78,46,104,82,94,86])
    h('Per address and per channel')
    p('For ADC order, each address visit yields one retained sensor value. Average address service is acquisition/50; period/50 is the amortized cost per sensor including shared work. Neither is the ADC conversion time. The ADC itself still uses a nominal 1 us serial frame; prime and VMID words are additional conversions.')
    rows=[]
    for s in SETTLES:
        for v in (False,True):
            r=get(s,v);pr=get(s,v,profile=True);c=operation_counts('channel',v)
            transfer=f'{pr["profile_transfer_us"]/50:.3f}' if profiles else 'unavailable'
            rows.append([s,'on' if v else 'off',f'{c["address_changes"]*s/50:.2f}',f'{r["mean_acquisition_us"]/50:.3f}',f'{r["mean_period_us"]/50:.3f}',transfer])
    table(['Settle us','VMID','Amortized settle / channel us','Production acquisition / channel us','Production period / channel us','Diagnostic transfer scope / address us'],rows,[40,38,97,106,106,103],7.4)
    p('Off: each address uses 2 ADC words; on: 7 words plus one VMID park/settle inside the transfer scope. Per-array sweep cost is approximately half the symmetric full-sweep work, with array handoff/parking costs assigned by the actual sequence. ADC-order array durations and individual GPIO edges were not separately timestamped.')
    if profiles:
        page();h('Measured diagnostic phases')
        p('Existing teensy41_profile firmware; one repetition per setting, 1 s warm-up + 2 s measurement. Values below are mean phase times per whole sweep at 5 us settling, including warm-up. Profile scopes use the 600 MHz CPU cycle counter; interrupts remain enabled. Production firmware was restored afterward.')
        rows=[]
        for e in ENGINES:
            for v in (False,True):
                r=get(5,v,e,profile=True)
                rows.append([e,'on' if v else 'off',*[f'{r[k]:.3f}' for k in ('profile_prepare_us','profile_session_us','profile_transfer_us','profile_validate_us','profile_encode_us','profile_usb_write_us','profile_bookkeeping_us')]])
        table(['Engine','VMID','MUX prepare us','Session us','Transfer us','Validation us','Encode us','USB enqueue us','Profiler bookkeeping us'],rows,[65,34,64,51,61,59,51,58,71],7.1)
        p('<b>Scopes overlap:</b> validation is inside transfer; prepare/session/transfer are inside acquisition. Transfer with VMID on includes VMID park settling. Acquisition plus outside gap describes a period. Do not sum every phase column. Bookkeeping is diagnostic overhead, not a production component.')
        rows=[]
        for e in ENGINES:
            off=get(5,False,e,profile=True);on=get(5,True,e,profile=True)
            rows.append([e,f'{off["profile_transfer_us"]/100:.4f}',f'{off["profile_validate_us"]/100:.4f}',f'{(off["profile_prepare_us"]-52*5)/52:.4f}',f'{(on["profile_prepare_us"]-50*5)/50:.4f}'])
        table(['Engine','Off transfer / 16-bit word us','Validation / word us','Off prepare residual / switch us','On prepare residual / switch us'],rows,[69,108,96,108,109])
        p('Per-word figures are aggregate diagnostic averages including loop/probe overhead, not oscilloscope CS-to-CS timings. Prepare residual subtracts the requested settling; it still includes 0.4 us explicit allowances, GPIO writes and scope overhead. VMID-on includes two no-op park calls in prepare, making its residual slightly different.')
        rows=[]
        for s in SETTLES:
            for v in (False,True):
                r=get(s,v);pr=get(s,v,profile=True)
                rows.append([s,'on' if v else 'off',f'{r["mean_period_us"]:.2f}',f'{pr["mean_period_us"]:.2f}',f'{100*(pr["mean_period_us"]/r["mean_period_us"]-1):.2f}%'])
        table(['Settle us','VMID','Production period us','Profile period us','Probe/build slowdown'],rows,[61,54,120,120,135],8,3)
        p('Probe overhead is measured as a whole-build difference. Subtracting adjacent cycle-read calibration from every phase would not remove all instrumentation work, so no such correction is claimed.',True)
    else:
        page();h('Diagnostic phase measurements pending')
        p('The existing diagnostic image was built and uploaded, but USB serial did not re-enumerate. A pre-existing Teensy GUI loader was found and closed to avoid competing uploads. The reason for the missing USB device remains unresolved. The production image uploader is waiting for the physical Program button; production recovery must be verified before further diagnostic work.')
        p('The 120 production captures, rate fits, acquisition durations and paired outside-acquisition gaps are valid and preserved. Individual session, transfer, validation, encode and enqueue phase timings cannot be uniquely recovered from rate alone. They are deliberately shown as unavailable rather than estimated as exact measurements.')
    if profiles:
        page();h('Per-word and per-address cost estimates')
        p('VMID-off transfer profiles provide two different operation counts: ADC order uses 100 words / 50 batches; address order uses 64 words / 14 batches. Fit transfer = words x word_cost + batches x batch_cost to all ten off profiles for each engine. This separates an estimated average word cost from per-address read-batch overhead under a uniform-cost assumption; these are diagnostic model estimates, not individual edge measurements.')
        rows=[]
        for u in data['unit_models']:
            rows.append([u['engine'],f'{u["word_us"]:.4f}',f'{u["batch_overhead_us"]:.4f}',f'{u["switch_residual_us"]:.4f}',f'{u["transfer_fit_rms_us"]:.4f}'])
        table(['Engine','Estimated word us','Read-batch overhead us','Prepare residual / switch us','Transfer model RMS us'],rows,[73,96,112,118,91])
        p('Word cost includes the nominal 1 us ADC/serial frame, CS allowances, validation and driver/probe work. Switching residual is median((prepare minus requested settling)/actual changes) from VMID-off profiles across both orders; it includes the 0.4 us switch allowances, GPIO work and prepare probes. Neither component is corrected to an exact production-only electrical duration.')
        u=next(x for x in data['unit_models'] if x['engine']=='lpspi')
        rows=[]
        for order,group,values,offwords,onwords in [('ADC','each sensor-address visit',1,2,7),('Address','inputs 0-5',4,5,10),('Address','input 6 / MUX4 only',1,2,7)]:
            for v,words in [(False,offwords),(True,onwords)]:
                switches=2 if v else 1
                scan=words*u['word_us']+(2 if v else 1)*u['batch_overhead_us']
                change=switches*u['switch_residual_us'];settle=switches*5
                total=change+settle+scan
                rows.append([order,group,'on' if v else 'off',values,words,f'{change:.3f}',f'{settle:.1f}',f'{scan:.3f}',f'{total:.3f}',f'{total/values:.3f}'])
        table(['Order','Address group','VMID','Values','Words','Switch us','Settle us','Scan us','Service us','Per value us'],rows,[47,103,36,36,38,48,48,48,47,39],7.2)
        p('LPSPI estimates at 5 us settling. Service includes the group\'s sensor switch, any VMID park, the requested waits and converter read batches. Off excludes the two additional shared mandatory park operations per sweep; both settings exclude sweep session, outer-loop work and USB. VMID wrapper-specific costs are approximated by the read-batch model, not independently isolated.')
        p('Each ADC has six four-value address groups and one single-value address group in address order; each has 25 single-value groups in ADC order. Divide a four-value service cost by four only to amortize work; this does not assign four simultaneous sample apertures. SPI conversions are sequential. Actual per-channel timestamps or exact array boundary intervals require extra instrumentation.')
    page();h('USB transmission and outside-acquisition costs')
    p('The timestamp gap after each acquisition covers frame encoding, the atomic USB enqueue, counters and main-loop/yield work. Production gaps are directly measured; encode and enqueue are separately measured only in the diagnostic build. Enqueue copies bytes to the USB transmit queue; it does not wait for host receipt.')
    rows=[]
    for s in SETTLES:
        for v in (False,True):
            r=get(s,v);pr=get(s,v,profile=True)
            rows.append([s,'on' if v else 'off',f'{r["mean_gap_us"]:.3f}',f'{pr["profile_encode_us"]:.3f}' if profiles else 'unavailable',f'{pr["profile_usb_write_us"]:.3f}' if profiles else 'unavailable',f'{r["USB_bytes_s"]/1000:.1f}',f'{r["mean_gap_us"]/r["mean_period_us"]*100:.3f}%'])
    table(['Settle us','VMID','Production gap us','Profile encode us','Profile enqueue us','USB payload kB/s','Production gap / period'],rows,[42,38,87,83,87,75,78],7.5)
    p('The firmware uses USB CDC, so the 460800 serial setting is not a UART bit-rate limit. Each frame is 114 bytes, with 100 bytes of sensor data (87.7% useful payload). The core submits full buffers immediately and flushes a partial transmit buffer with a nominal 75 us timer. That delay is asynchronous and is not an added 75 us wait in the sampling loop.')
    p('Pure serialization of 114 bytes would take 1.90 us at 480 Mbit/s or 76.0 us at 12 Mbit/s, before USB packet overhead and scheduling. These are illustrative lower bounds, not measured wire times or measured negotiated link speed. USB bus scheduling, driver buffering and host end-to-end latency were not instrumented and cannot be inferred from sample-rate slope.')
    p('The useful optimization bound is the measured production gap: removing all encoding/enqueue/loop gap would save only that amount per sweep. USB interrupt servicing can also occur inside acquisition; its contribution is included in measured acquisition/remaining costs and is not individually resolved.')
    p('Sources: project UsbSerialController.cpp and usb_core_patch.py; pinned Teensy 1.162.0 core usb_serial.c (TRANSMIT_FLUSH_TIMEOUT, TX_SIZE and flush callback). No blocking binary flush is used.',True)
    page();h('Optimization control: group readings by address')
    p('Exploratory production control: scanorder mux, all three engines and the same five settling delays/VMID settings; one repetition per setting, 1 s warm-up + 3 s measurement. ADC-order primary results have three repetitions. This comparison establishes timing opportunity; different conversion history can change analog baseline or stimulated crosstalk.')
    rows=[]
    for s in SETTLES:
        for v in (False,True):
            r=[get(s,v,e,'mux') for e in ENGINES];base=get(s,v)
            rows.append([s,'on' if v else 'off',*[f'{x["rate_hz_channel"]:,.1f}' for x in r],f'{r[2]["rate_hz_channel"]/base["rate_hz_channel"]:.2f}x'])
    table(['Settle us','VMID','Blocking address Hz/ch','DMA address Hz/ch','LPSPI address Hz/ch','LPSPI gain vs ADC order'],rows,[43,37,104,104,104,98])
    image(orderplot,178)
    p('Off: address grouping removes 36 settling events and 36 selector-prime words per sweep. On: it removes 72 settling events and 216 total words. No higher SCLK or concurrent ADC acquisition is required. Scan order changes are already supported; no firmware modification was made.')
    page();h('Baseline context for address grouping')
    p('The two orders are not electrically equivalent histories. Table uses median channel baselines in the post-warm-up record; primary ADC-order values are medians across three repetitions, address-order control values come from one repetition. These are whole-record comparisons, not a new burst-masked noise study.')
    rows=[]
    for s in SETTLES:
        for v in (False,True):
            r=get(s,v);c=get(s,v,o='mux')
            rows.append([s,'on' if v else 'off',f'{r["baseline_spread_A_counts"]:.1f} / {c["baseline_spread_A_counts"]:.1f}',f'{r["baseline_spread_B_counts"]:.1f} / {c["baseline_spread_B_counts"]:.1f}',f'{r["max_channel_std_counts"]:.3f} / {c["max_channel_std_counts"]:.3f}'])
    table(['Settle us','VMID','Array A spread counts ADC / address','Array B spread counts ADC / address','Worst channel SD counts ADC / address'],rows,[43,39,135,135,138])
    p('Baseline spread is the range across 25 channel median levels, not temporal noise. Counts x 3.3/4096 converts these measurements to volts. Individual control-channel metrics can be recomputed from the hashed raw frames and exact payload_routes in each stopped status snapshot.')
    p('A faster setting is a useful candidate only if acceptable analog settling, ghosting and response are maintained with sensors attached. Open-connector baseline measurements alone cannot establish those properties. The original quiet-noise report remains unchanged; this supplement targets timing.')
    page();h('Optimization priorities and likely savings')
    for v in (False,True):
        h('5 us settling / VMID '+('on' if v else 'off'))
        lp=get(5,v);dma=get(5,v,'dma');block=get(5,v,'blocking');group=get(5,v,o='mux')
        table(['Measured change','Time saved per sweep','Rate change'],[['DMA to LPSPI',f'{dma["mean_period_us"]-lp["mean_period_us"]:.2f} us',f'{100*(lp["rate_hz_channel"]/dma["rate_hz_channel"]-1):.1f}%'],['Blocking to LPSPI',f'{block["mean_period_us"]-lp["mean_period_us"]:.2f} us',f'{100*(lp["rate_hz_channel"]/block["rate_hz_channel"]-1):.1f}%'],['LPSPI ADC to address order',f'{lp["mean_period_us"]-group["mean_period_us"]:.2f} us',f'{group["rate_hz_channel"]/lp["rate_hz_channel"]:.2f}x']], [242,129,119])
    p('<b>1. Keep LPSPI for current ADC-order timing.</b> It retains one SPI transaction per sweep and directly transfers 16-bit words. Blocking enters/exits an SPIClass transaction and changes frame format per word. DMA configures/completes a two-byte asynchronous transfer per word, including event handling and yield. With the present implementation, DMA is not a speed improvement.')
    p('<b>2. Prefer the existing address-group order if analog behavior permits.</b> This has the largest measured opportunity without changing firmware. It amortizes one external switch/settle and one selector prime over up to four retained values. Validate stimulated ghosting and channel response before changing the operational scan order.')
    p('<b>3. Investigate burst transfers within a fixed address group.</b> A driver could keep CS low for multiple 16-clock conversions and pipeline a prepared command sequence, reducing per-word setup, CS delays and DMA launches. Firmware currently toggles CS for every word. At ADC order, consolidating each 2-word sensor batch and each 5-word VMID batch would reduce explicit CS allowances by 10 us/sweep off or 50 us/sweep on, before any software saving. These are calculations, not measured optimized-driver results.')
    p('<b>4. Consider selector primes conditionally, with validation.</b> In VMID-off ADC order, consecutive external inputs within one MUX keep the same ADC selector. If electrical settling and next-conversion state allow omitting redundant primes, eight MUX starts per sweep would need primes rather than all 50, removing up to 42 conversion words. That changes conversion history and has not been implemented or electrically validated.')
    p('<b>5. Reduce GPIO/delay-call overhead only after the larger changes.</b> Cache GPIO masks or use grouped address writes while preserving disable, turn-off/turn-on allowances and EN polarity. Do not reduce analog settling merely because switching CPU overhead is small. USB encoding/enqueue is a small measured share at these rates; prioritize it only if counters show transport pressure.')
    p('Multiple back-to-back 16-clock conversions with CS low are supported by the TI serial interface. Burst-driver feasibility follows that specification; safe timing, payload identity, bounds/timeouts, cleanup and analog history still require tests. <link href="https://www.ti.com/lit/ds/symlink/adc124s101.pdf">TI ADC124S101, Using the ADC / Power Management</link>.',True)
    page();h('Integrity, evidence and limits')
    allruns=data['production']+data['profiles']
    total=sum(r['frames'] for r in allruns);acquired=sum(r['acquired'] for r in allruns);drops=sum(r['discarded'] for r in allruns)
    p(f'<b>{len(allruns)} captures completed:</b> 90 primary ADC-order production, 30 address-order production controls, {len(data["profiles"])} diagnostic profiles. {total:,} full sweeps received ({50*total:,} transmitted channel values, including warm-up). {acquired:,} acquired sweeps; {drops:,} explicitly accounted whole-sweep USB discards. Received equals sent for every capture; acquired equals sent plus discarded.')
    p('All raw streams were independently decoded with the existing incremental frame parser and structured frame reader; samples/timing/counts agreed. SHA-256 hashes were rechecked during report analysis. ADC values stayed within 0-4095, timestamps were monotonic after wrap handling, and every acquisition ended before the next one started. Firmware error counters remained zero.')
    if profiles:
        p('Production tests used ordinary teensy41 firmware. The existing teensy41_profile build was temporarily loaded for phase measurement and then ordinary production firmware restored. No acquisition source or wire format was changed. The recorder saves stopped status snapshots, profiling summaries and command logs. Final board status is saved in firmware_lifecycle.json; original full-array, blocking, MUX-order, 16 MHz, repeat-1, 5 us, VMID-off settings were restored and the board left stopped.')
    else:
        p('<b>Firmware recovery remains pending.</b> The board has not reappeared on USB serial after diagnostic upload. Production flashing is waiting for the Program button. The final board configuration is not yet verified; firmware_lifecycle.json records the pending state. No acquisition source or wire format was changed.')
    h('What is measured, calculated or still unresolved')
    table(['Quantity','Evidence / interpretation'],[['Sweep period, useful sampling rate','Production device start timestamps; after warm-up'],['Total acquisition and outside gap','Production start/end timestamps; correctly paired'],['Address/word/event counts','Firmware plan plus host replay tests'],['Requested settling and explicit allowances','Event counts x configured firmware delays'],['ADC track / conversion / shift','Clock-derived datasheet components; overlap'],['Prepare / transfer / session / encode / enqueue','Cycle-counter profiles; probes included' if profiles else 'Unavailable; diagnostic USB recovery pending'],['Electrical MUX propagation / analog settling curve','Not individually measured by this experiment'],['Per-channel aperture / exact sample timestamps','Not transmitted; only shared sweep start/end interval'],['USB physical transfer / host end-to-end latency','Not independently measured; enqueue overlaps acquisition']], [208,282],8,3)
    p('Rate slope alone cannot identify every independent time component: it reveals the number of settling waits and a combined intercept. Diagnostic scopes narrow that intercept, but exact electrical switch latency, exact SCLK waveform and host transport latency would require synchronized external measurements. No unsupported precision is assigned to those quantities.')
    p(f'CSV evidence: production_capture_metrics.csv (120 rows), production_summary.csv (60 settings), diagnostic settings: {len(profiles)}, delay_fits.csv (36 fits) and timing_analysis.json. The original settling report and its 30 captures provide an independent prior rate/baseline reference.',True)
    pdf.parent.mkdir(parents=True,exist_ok=True)
    def footer(canvas,doc):
        canvas.saveState();canvas.setFont('Helvetica',7);canvas.setFillColor(colors.HexColor('#607c90'))
        canvas.drawString(42,25,'TestBoard_ADC124 | 13 October 2026 | timing budget | blocking / DMA / LPSPI')
        canvas.drawRightString(A4[0]-42,25,str(doc.page));canvas.restoreState()
    SimpleDocTemplate(str(pdf),pagesize=A4,leftMargin=42,rightMargin=42,topMargin=38,bottomMargin=42,title='TestBoard_ADC124 timing budget and engine comparison').build(story,onFirstPage=footer,onLaterPages=footer)
    print('PDF',pdf,flush=True)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('primary',type=Path);parser.add_argument('control',type=Path);parser.add_argument('profile',type=Path)
    parser.add_argument('output',type=Path);parser.add_argument('--pdf',type=Path)
    parser.add_argument('--production-only',action='store_true')
    args=parser.parse_args()
    result=analyze(args.primary,args.control,None if args.production_only else args.profile,args.output)
    if args.pdf:build_report(result,args.output,args.pdf)

if __name__=='__main__':main()
