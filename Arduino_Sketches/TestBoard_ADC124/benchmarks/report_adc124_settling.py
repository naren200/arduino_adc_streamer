"""Reproducible quiet-period analysis and PDF of raw ADC124 settling captures."""
from pathlib import Path
import argparse
import csv
import json
import hashlib
import numpy as np
from capture_adc124_settling import decode, bench

LSB = 3.3 / 4096
BIN = .05

def quiet_mask(t, x, groups, start=1., end=16., require_coverage=True):
    """Common time mask from lower-envelope statistics, without sample clipping."""
    edges = np.arange(start, end + BIN/2, BIN)
    envelopes = np.full((len(edges)-1, len(groups)), np.nan)
    for i, (a,b) in enumerate(zip(edges[:-1],edges[1:])):
        z = x[(t>=a)&(t<b)]
        if len(z)>=5:
            sd = z.std(axis=0, ddof=1)
            envelopes[i] = [np.median(sd[g]) for g in groups]
    valid = np.isfinite(envelopes).all(axis=1)
    if valid.sum() < 20:
        raise ValueError('Insufficient envelope coverage')
    thresholds=[]
    for e in envelopes[valid].T:
        lower = e[e <= np.quantile(e,.4)]
        center = np.median(lower)
        mad = np.median(np.abs(lower-center))
        # Neighboring-code toggling alone can reach ~0.55 counts sample SD
        # in the slowest bins. Do not call normal 1-LSB quantization a burst.
        thresholds.append(max(.6, np.quantile(e,.2)+.15, center+4*1.4826*mad))
    thresholds=np.array(thresholds)
    above=valid & (envelopes>thresholds).any(axis=1)
    reject=above.copy()
    reject[1:] |= above[:-1]
    reject[:-1] |= above[1:]
    keep=valid & ~reject
    index=np.floor((t-start)/BIN + 1e-8).astype(int)
    inside=(index>=0)&(index<len(keep))&(t<end)
    mask=np.zeros(len(t),dtype=bool)
    mask[inside]=keep[index[inside]]
    if require_coverage and (keep.sum()*BIN<1 or mask.sum()<100):
        raise ValueError('Less than 1 second / 100 frames of retained quiet data')
    return mask, edges, envelopes, thresholds, keep, above

def write_csv(path, rows):
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)

def canonical_samples(frames, status):
    """Map verified wire columns to array/MUX/input order for channel summaries."""
    routes=tuple(sorted(bench.load_manifest(bench.DEFAULT_MANIFEST)['both_full']))
    wire=tuple(bench.Route.parse(r) for r in status['payload_routes'].split(','))
    expected=bench.payload_order(routes,status['scanorder'],protocol_version=int(status.get('protocol_version','1')))
    if wire!=expected or len(wire)!=50:
        raise ValueError('Payload route order does not match firmware scan order')
    return frames['samples'][:,[wire.index(r) for r in routes]].astype(float)

def analyze(source, output, partial=False, engine='lpspi'):
    meta=json.loads((source/'session.json').read_text())
    selected_runs=[r for r in meta['runs'] if r.get('engine','lpspi')==engine]
    if not partial and (meta['state']!='complete' or len(selected_runs)!=30):
        raise ValueError('Need all 30 validated captures')
    if not partial:
        expected={(s,v,r) for s in (1,2,5,10,50) for v in (False,True) for r in (1,2,3)}
        actual={(r['settle_us'],r['vmid'],r['repetition']) for r in selected_runs}
        if actual!=expected:
            raise ValueError('Incomplete or incorrect settling/VMID/repetition matrix')
    routes=tuple(sorted(bench.load_manifest(bench.DEFAULT_MANIFEST)['both_full']))
    groups=[[i for i,r in enumerate(routes) if r.array==a and r.mux==m] for a in (1,2) for m in (1,2,3,4)]
    output.mkdir(parents=True,exist_ok=True)
    runs=[]; channel_runs=[]; plots={}
    for run in selected_runs:
        folder=source/run['name']
        if hashlib.sha256((folder/'capture.bin').read_bytes()).hexdigest()!=run['sha256']:
            raise ValueError('Raw hash mismatch')
        d=decode(folder/'capture.bin')
        elapsed=np.r_[0,np.cumsum(np.diff(d['start'].astype(np.int64))%2**32)]/1e6
        status=json.loads((folder/'status.json').read_text())['after']
        if run.get('order',status['scanorder'])!=status['scanorder']:
            raise ValueError('Run/status scan order mismatch')
        x=canonical_samples(d,status)
        mask,edges,env,threshold,keep,above=quiet_mask(elapsed,x,groups)
        z=x[mask]
        measured=elapsed>=1
        tm=elapsed[measured]
        rate=(len(tm)-1)/(tm[-1]-tm[0])
        period=np.diff(d['start'].astype(np.int64))[measured[1:]]%2**32
        acquisition=(d['end'].astype(np.int64)-d['start'].astype(np.int64))%2**32
        row={**run,'received_hz_per_channel':float(rate),'received_values_per_s':float(rate*50),
             'sampling_hz_full_run_estimate':float(run['sampling_sweeps']/(elapsed[-1]+np.median(period)/1e6)),
             'acquisition_median_us':float(np.median(acquisition[measured])),
             'period_median_us':float(np.median(period)),
             'overhead_median_us':float(np.median(period-acquisition[:-1][measured[1:]])),
             'quiet_s':float(keep.sum()*BIN),'rejected_percent':float(100*(1-keep.mean())),
             'quiet_frames':int(mask.sum()),'thresholds_counts':threshold.tolist(),
             'quiet_std_counts_max':float(z.std(axis=0,ddof=1).max()),
             'quiet_ptp_counts_max':float(np.ptp(z,axis=0).max()),
             'invalid_envelope_bins':int((~np.isfinite(env).all(axis=1)).sum())}
        runs.append(row)
        bins=[{'start_s':float(a),'end_s':float(b),'quiet':bool(k),'above_threshold':bool(ab),
               **{f'{"AB"[j//4]}_MUX{j%4+1}_std_counts':float(env[i,j]) for j in range(8)}}
              for i,(a,b,k,ab) in enumerate(zip(edges[:-1],edges[1:],keep,above))]
        write_csv(output/(run['name']+'_quiet_bins.csv'),bins)
        for i,r in enumerate(routes):
            sd=float(z[:,i].std(ddof=1)); ptp=float(np.ptp(z[:,i])); baseline=float(np.median(z[:,i]))
            channel_runs.append({'settle_us':run['settle_us'],'vmid':run['vmid'],'repetition':run['repetition'],
                'route':str(r),'array':r.array,'mux':r.mux,'adc_input':r.mux,'mux_input':r.input,
                'sensor':f'PZT{r.sensor}','sensor_channel':r.sensor_channel,'bias_ohms':r.bias_ohms,
                'quiet_frames':int(mask.sum()),'baseline_counts':baseline,'baseline_V':baseline*LSB,
                'std_counts':sd,'std_V':sd*LSB,'ptp_counts':ptp,'ptp_V':ptp*LSB,
                'quiet_mean_counts':float(z[:,i].mean()),'min_counts':int(z[:,i].min()),'max_counts':int(z[:,i].max())})
        if run['repetition']==1:
            plots[(run['settle_us'],run['vmid'])]=(edges,env,threshold,keep)
    write_csv(output/'per_channel_repetitions.csv',channel_runs)
    flat_runs=[{k:v for k,v in r.items() if not isinstance(v,(dict,list))} for r in runs]
    write_csv(output/'capture_metrics.csv',flat_runs)
    aggregated=[]
    for s in (1,2,5,10,50):
        for v in (False,True):
            for route in routes:
                selected=[r for r in channel_runs if r['settle_us']==s and r['vmid']==v and r['route']==str(route)]
                if not selected: continue
                base={k:selected[0][k] for k in ('settle_us','vmid','route','array','mux','adc_input','mux_input','sensor','sensor_channel','bias_ohms')}
                for key in ('baseline_counts','baseline_V','std_counts','std_V','ptp_counts','ptp_V'):
                    base[key]=float(np.median([r[key] for r in selected]))
                    base[key+'_min_rep']=float(min(r[key] for r in selected))
                    base[key+'_max_rep']=float(max(r[key] for r in selected))
                aggregated.append(base)
    write_csv(output/'per_channel_metrics.csv',aggregated)
    summaries=[]
    for s in (1,2,5,10,50):
        for v in (False,True):
            for a in (1,2):
                for mux in (0,1,2,3,4):
                    sel=[r for r in aggregated if r['settle_us']==s and r['vmid']==v and r['array']==a and (mux==0 or r['mux']==mux)]
                    if not sel: continue
                    base=np.array([r['baseline_counts'] for r in sel])
                    row={'settle_us':s,'vmid':v,'array':a,'mux':mux,'channels':len(sel),
                         'bias_ohms':sel[0]['bias_ohms'] if mux else 'mixed',
                         'baseline_min_counts':float(base.min()),'baseline_max_counts':float(base.max()),
                         'baseline_center_counts':float(np.median(base)),
                         'baseline_spread_counts':float(np.ptp(base)),'baseline_std_counts':float(base.std(ddof=0))}
                    for key in ('std','ptp'):
                        vals=[r[key+'_counts'] for r in sel]
                        row[key+'_median_counts']=float(np.median(vals)); row[key+'_max_counts']=float(max(vals))
                    for key,value in list(row.items()):
                        if key.endswith('_counts'): row[key[:-7]+'_V']=value*LSB
                    summaries.append(row)
    write_csv(output/'group_summary.csv',summaries)
    result={'source':str(source.resolve()),'bin_s':BIN,'lsb_V':LSB,'session':meta,'runs':runs,
            'channels':aggregated,'groups':summaries,'engine':engine,
            'scanorder':selected_runs[0].get('order','channel'),
            'quiet_method':'Per each of 8 MUX/ADC-input groups, median channel ddof=1 SD in complete 50ms bins; threshold max(.6, P20+.15, lower40 median+4*1.4826*MAD); reject if any group exceeds threshold plus one adjacent bin each side; common mask; no individual sample rejection.'}
    (output/'analysis.json').write_text(json.dumps(result,indent=2))
    print('ANALYZED',len(runs),'runs; quiet seconds range',min(r['quiet_s'] for r in runs),max(r['quiet_s'] for r in runs),flush=True)
    return result,plots

def build_report(result,plots,output,pdf):
    if result['scanorder']!='mux':
        raise ValueError('Grouped-MUX report requires scanorder mux captures')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,Image,PageBreak
    from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    plt.rcParams.update({'font.size':9,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.alpha':.18})
    C=['#2374ab','#d66a23']; S=(1,2,5,10,50)
    runs=result['runs']; channels=result['channels']; groups=result['groups']
    allgroups=[r for r in groups if r['mux']==0]
    def rr(s,v):return [r for r in runs if r['settle_us']==s and r['vmid']==v]
    def gg(s,v,a):return next(r for r in allgroups if r['settle_us']==s and r['vmid']==v and r['array']==a)
    def mid(rows,key): return float(np.median([r[key] for r in rows]))
    def save(fig,name):
        fig.tight_layout(); path=output/(name+'.png'); fig.savefig(path,dpi=180); plt.close(fig); return path
    def line(ax,key,scale=1,group=False,a=1):
        for v,c in zip((False,True),C):
            if group:
                y=[gg(s,v,a)[key]*scale for s in S]
                ax.plot(S,y,'o-' if not v else 's--',color=c,label='VMID '+('on' if v else 'off'))
            else:
                sets=[[r[key]*scale for r in rr(s,v)] for s in S]
                med=np.median(sets,axis=1)
                ax.errorbar(S,med,yerr=[med-np.min(sets,axis=1),np.max(sets,axis=1)-med],fmt='o-' if not v else 's--',color=c,capsize=3,label='VMID '+('on' if v else 'off'))
        ax.set_xscale('log'); ax.set_xticks(S,labels=[str(s) for s in S]); ax.set_xlabel('Settling delay (us)'); ax.legend(fontsize=8)
    fig,ax=plt.subplots(1,2,figsize=(10,3.2))
    line(ax[0],'received_hz_per_channel'); ax[0].set_ylabel('Samples/s per channel'); ax[0].set_title('Delivered sampling rate')
    line(ax[1],'period_median_us'); ax[1].set_ylabel('Full 50-channel sweep period (us)'); ax[1].set_title('Typical sweep period')
    rateplot=save(fig,'sampling_rate')
    fig,axes=plt.subplots(5,2,figsize=(10,9),sharex=True)
    for i,s in enumerate(S):
        for j,v in enumerate((False,True)):
            edges,env,threshold,keep=plots[(s,v)]; ax=axes[i,j]
            for a,c in enumerate(C):
                ax.plot((edges[:-1]+edges[1:])/2,np.median(env[:,a*4:(a+1)*4],axis=1),color=c,lw=1,label='Array '+('A' if a==0 else 'B'))
            for left,right,k in zip(edges[:-1],edges[1:],keep):
                if not k: ax.axvspan(left,right,color='#e2b9b9',alpha=.4,lw=0)
            ax.set_title(f'{s} us / VMID {"on" if v else "off"}',fontsize=9)
            ax.set_ylabel('SD counts'); ax.set_ylim(bottom=0)
            if i==0 and j==0:ax.legend(fontsize=7)
            if i==4:ax.set_xlabel('Device elapsed time (s)')
    envelopeplot=save(fig,'quiet_envelopes')
    fig,axes=plt.subplots(2,2,figsize=(10,6))
    for a in (1,2):
        for j,key in enumerate(('std_median_counts','ptp_max_counts')):
            ax=axes[a-1,j];line(ax,key,LSB*1000,True,a)
            ax.set_title(f'Array {"AB"[a-1]} / ADC{a}');ax.set_ylabel('Median channel SD (mV)' if j==0 else 'Worst channel peak-to-peak (mV)')
    noiseplot=save(fig,'quiet_noise')
    fig,axes=plt.subplots(1,2,figsize=(10,3.2))
    for a,ax in zip((1,2),axes):
        line(ax,'baseline_spread_counts',LSB*1000,True,a);ax.set_title(f'Array {"AB"[a-1]} / ADC{a}');ax.set_ylabel('Channel baseline max-min (mV)')
    spreadplot=save(fig,'baseline_spread')
    heatmaps=[]
    baseline_lo=np.floor(min(r['baseline_V'] for r in channels)*100)/100
    baseline_hi=np.ceil(max(1.65,max(r['baseline_V'] for r in channels))*100)/100
    for v in (False,True):
        fig,axes=plt.subplots(2,1,figsize=(10,6.8))
        for a,ax in zip((1,2),axes):
            sel=[r for r in channels if r['array']==a and r['vmid']==v]
            route_order=[r['route'] for r in sel if r['settle_us']==1]
            z=np.array([[next(r['baseline_V'] for r in sel if r['settle_us']==s and r['route']==rt) for rt in route_order] for s in S])
            im=ax.imshow(z,aspect='auto',vmin=baseline_lo,vmax=baseline_hi,cmap='viridis')
            ax.set_xticks(range(25),labels=[f'M{rt.split(":")[1]}:{rt.split(":")[2]}' for rt in route_order],rotation=60,fontsize=7)
            ax.set_yticks(range(5),labels=[str(s) for s in S]);ax.set_ylabel('Settling (us)');ax.set_title(f'Array {"AB"[a-1]} / ADC{a} / VMID {"on" if v else "off"}')
            for b in (5.5,11.5,17.5):ax.axvline(b,color='white',lw=1)
            fig.colorbar(im,ax=ax,label='Quiet baseline (V)')
        heatmaps.append(save(fig,'baselines_'+('on' if v else 'off')))
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='TitleADC',fontName='Helvetica-Bold',fontSize=22,leading=26,textColor=colors.HexColor('#184f73'),alignment=TA_CENTER,spaceAfter=18))
    styles.add(ParagraphStyle(name='BodyADC',fontName='Helvetica',fontSize=9,leading=12,spaceAfter=8,textColor=colors.HexColor('#203747')))
    styles.add(ParagraphStyle(name='SmallADC',fontName='Helvetica',fontSize=7.5,leading=10,spaceAfter=6,textColor=colors.HexColor('#203747')))
    styles['Heading1'].textColor=colors.HexColor('#184f73')
    story=[]
    def p(text,small=False):story.append(Paragraph(text,styles['SmallADC' if small else 'BodyADC']))
    def title(text):story.append(Paragraph(text,styles['Heading1']))
    def page():story.append(PageBreak())
    def picture(path,height):story.append(Image(str(path),width=490,height=height))
    def table(headers,rows,widths=None,size=8,padding=5):
        data=[[Paragraph(str(x),styles['SmallADC']) for x in headers]]+rows
        t=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e6eef3')),('FONTNAME',(0,1),(-1,-1),'Helvetica'),('FONTSIZE',(0,1),(-1,-1),size),('LEADING',(0,1),(-1,-1),size+2),('TEXTCOLOR',(0,1),(-1,-1),colors.HexColor('#203747')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),padding),('BOTTOMPADDING',(0,0),(-1,-1),padding),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f3f5f7')]),('LINEBELOW',(0,0),(-1,0),.5,colors.HexColor('#bfd0dc'))]))
        story.append(t);story.append(Spacer(1,10))
    story.append(Paragraph('TestBoard_ADC124<br/>Grouped MUX scan: settling and VMID',styles['TitleADC']))
    p('13 October 2026 | Production firmware | Manual conversion | '+result['engine'].upper()+' | 16 MHz SPI')
    title('Setup and test matrix')
    p('Both array connectors disconnected. Laptop charger connected; a common quiet-period mask screens for interference bursts. Two full arrays, 25 sensor routes per array, 50 transmitted values per sweep. Each ADC is an ADC124S101, with four external MUX/op-amp inputs. No sensor strips were connected or pressed.')
    table(['Parameter','Tested value'],[['Settling delay','1, 2, 5, 10, 50 us'],['VMID insertion','Off and on at every settling delay'],['Acquisition','Manual / channel repeat = 1'],['Scan order','MUX address grouped: array, external input, ADC IN'],['SPI',result['engine'].upper()+' on one shared bus; requested clock 16 MHz'],['Duration','3 repetitions x (1 s warm-up + 15 s measurement)'],['Reference / VMID','Fixed nominal 3.3 V / 1.65 V']],widths=[150,340])
    table(['Array / ADC','MUX1 / IN1','MUX2 / IN2','MUX3 / IN3','MUX4 / IN4'],[['A / ADC1','470 kOhm','1 MOhm','470 kOhm','1 MOhm'],['B / ADC2','1 MOhm','470 kOhm','1 MOhm','470 kOhm']],widths=[106,96,96,96,96])
    p('The resistors bias each shared MUX-output/op-amp node toward VMID; they are not separate resistors on every sensor route. Array A uses TMUX1108; B uses TMUX1308A. Array and resistor comparisons therefore include different physical circuits.')
    p('<b>Every capture uses scanorder mux.</b> Switch/settle once for each external address, then read ADC IN1-IN4 at addresses 0-5; address 6 reads IN4 alone. This gives 25 values per array, 50 per full sweep. Optional VMID-on parks and converts all four ADC inputs after each group; those conversions are discarded. Mandatory VMID parking remains active in both settings. This report replaces the earlier channel-order characterization for the intended scan plan.')
    p(f'Voltage conversion: V = counts x 3.3/4096. One count = {LSB:.9f} V. Voltages use the configured nominal reference, without a fresh voltage calibration. The requested SCLK was not measured with an oscilloscope in this test.')
    page();title('Sampling rate and acquisition time')
    rows=[]
    for s in S:
        for v in (False,True):
            r=rr(s,v)
            rows.append([s,'on' if v else 'off',f'{mid(r,"received_hz_per_channel"):,.1f}',f'{mid(r,"received_values_per_s"):,.0f}',f'{mid(r,"acquisition_median_us"):.1f}',f'{mid(r,"period_median_us"):.1f}',f'{mid(r,"overhead_median_us"):.1f}'])
    table(['Settle us','VMID','Received Hz / channel','50-channel values/s','Acquisition us','Period us','Overhead us'],rows,[48,43,94,96,72,67,70])
    picture(rateplot,170)
    p('Rates are delivered sweeps per device sweep-start elapsed time after the 1 s warm-up; each sweep contains one value per channel. Total values/s = rate x 50, excluding discarded priming and VMID conversions. Table entries are medians of three repetitions. Error bars show the repetition min-max range.')
    p('Acquisition and period use timestamp medians; overhead uses median(period minus acquisition), so independently summarized columns may not add exactly. Received rate counts retained USB frames. Firmware sampling/sent/discarded accounting, including warm-up, is reported separately in the integrity section.')
    page();title('Quiet-period selection and retained data')
    p('Warm-up is excluded. A common time mask is applied to all 50 channels, so noise and baseline comparisons use exactly the same quiet intervals within each capture. No individual values are clipped, smoothed or detrended.')
    p('For each of eight MUX / ADC-input groups, calculate the median of its channel sample standard deviations in complete 50 ms bins. The bin width gives adequate samples even at the slowest tested sweep rate. Threshold = max(0.60 counts, P20 + 0.15 counts, median of lower 40% + 4 x 1.4826 x MAD of that lower subset). A bin above any group threshold is rejected, with one neighboring bin on each side.')
    p('The 0.60-count floor prevents normal toggling between two adjacent ADC codes from being classified as a burst. This is a quantization-aware adaptation of the earlier ADS7953 report method, applied equally to every setting.')
    p('This is an operational definition of quiet periods. The laptop charger was connected, but neither charger switching nor rail voltage was recorded. The rejected intervals are interference/activity periods; the measurements do not prove that every burst came from the charger. Persistent individual-channel variation remains in the results.')
    rows=[]
    for s in S:
        for v in (False,True):
            r=rr(s,v);quiet=[x['quiet_s'] for x in r]
            rows.append([s,'on' if v else 'off',f'{np.median(quiet):.2f} [{min(quiet):.2f}-{max(quiet):.2f}]',f'{mid(r,"rejected_percent"):.1f}',f'{min(x["quiet_frames"] for x in r):,}'])
    table(['Settle us','VMID','Quiet seconds: median [range]','Rejected time %','Min quiet frames / channel'],rows,[58,52,165,92,123])
    p('All 30 bin masks, eight thresholds per capture and channel metrics are retained alongside the raw data. Peak-to-peak depends on record length and sample count. The repetition CSV includes actual quiet sample counts; the summary CSV includes repetition ranges.')
    if all(r['rejected_percent']<.01 for r in runs):
        p('<b>No post-warm-up bin exceeded the quantization-aware burst thresholds in any capture.</b> All 15 s measurement windows were retained. The pronounced charger-burst pattern seen in the earlier board report was not observed here at this ADC resolution and group-envelope criterion. Sub-LSB interference or isolated channel events are not ruled out.')
    page();title('Quiet and burst envelopes')
    p('First repetition of every setting. Curves show the median bin SD across the four MUX groups in each array; selection itself uses all eight group envelopes. Shaded bins were excluded from every channel. Group thresholds are saved in analysis.json and bin envelopes in CSV.')
    picture(envelopeplot,490)
    page();title('Quiet noise in counts and volts')
    p('Each cell shows <b>median / maximum across channels</b>, after taking the median of three repetitions per channel. SD uses ddof=1. Peak-to-peak is the actual maximum minus minimum of retained quiet samples, with no individual outlier rejection.')
    rows=[]
    for r in allgroups:
        rows.append([r['settle_us'],'on' if r['vmid'] else 'off','AB'[r['array']-1],f'{r["std_median_counts"]:.3f} / {r["std_max_counts"]:.3f}',f'{r["std_median_V"]:.6f} / {r["std_max_V"]:.6f}',f'{r["ptp_median_counts"]:.1f} / {r["ptp_max_counts"]:.1f}',f'{r["ptp_median_V"]:.6f} / {r["ptp_max_V"]:.6f}'])
    table(['Settle us','VMID','Array','SD counts med / max','SD V med / max','P-P counts med / max','P-P V med / max'],rows,[40,35,35,96,112,72,100],7.5)
    p('Noise is time variation around each channel\'s own mean. Slow variation between retained quiet periods contributes to SD and peak-to-peak. Baseline spread, on subsequent pages, is variation between channel median levels. Zero SD indicates a constant quantized output code during the record; it does not establish zero analog noise.')
    page();title('Quiet noise versus settling delay')
    picture(noiseplot,325)
    p('SD graphs show the median channel noise; peak-to-peak graphs show the worst channel. Solid circles: VMID off. Dashed squares: VMID on. Graphs use mV for readability; the table and CSV supply counts and volts.')
    p('Longer settling changes both conversion history and achievable rate. Open connectors and the existing bias network make these useful baseline measurements, but they do not demonstrate performance with sensor strips attached or pressed.')
    page();title('Baseline spread between sampled channels')
    p('Baseline = median of quiet samples per channel, then median across repetitions. Max-min compares the highest and lowest of 25 channel baselines within one array / ADC. Baseline SD is population SD across those 25 baselines (ddof=0), not time-domain noise.')
    rows=[]
    for r in allgroups:
        rows.append([r['settle_us'],'on' if r['vmid'] else 'off','AB'[r['array']-1],f'{r["baseline_spread_counts"]:.1f}',f'{r["baseline_spread_V"]:.6f}',f'{r["baseline_std_counts"]:.2f}',f'{r["baseline_std_V"]:.6f}'])
    table(['Settle us','VMID','Array','Max-min counts','Max-min V','Baseline SD counts','Baseline SD V'],rows,[48,44,40,84,91,93,90],8)
    picture(spreadplot,166)
    for v,path in zip((False,True),heatmaps):
        page();title('Every channel baseline - VMID '+('on' if v else 'off'))
        p('Each column is one physical route; Mx:y means MUX x / ADC INx, external input y. Each row is a settling delay. Both panels use the same voltage scale. Values are quiet channel medians, summarized across three repetitions.')
        picture(path,370)
        p('MUX1-3 have six populated sensor routes (inputs 0-5); MUX4 has seven (0-6). Inputs 0-4 map to PZT1, PZT3, PZT5 and PZT6 on MUX1-4 respectively. Input 5 maps to PZT7 channels 1-4; MUX4 input 6 maps to PZT7 channel 5. VMID parking input 7 is not transmitted.')
        p('Vertical white lines separate the four ADC-input / MUX groups. Full baseline values in volts/counts, per-channel noise and repetition ranges are in per_channel_metrics.csv.')
    for v in (False,True):
        page();title('Per ADC-input / MUX summary - VMID '+('on' if v else 'off'))
        p('Each row groups the 6 or 7 sensor routes entering one ADC input. Bias is the MUX/op-amp node resistor. SD is median channel noise; P-P is maximum channel peak-to-peak. Spread compares quiet channel baselines within this input group. Group_summary.csv contains all values in both volts and counts.')
        rows=[]
        for r in groups:
            if r['mux']==0 or r['vmid']!=v:continue
            rows.append([r['settle_us'],'AB'[r['array']-1],r['mux'],'470k' if r['bias_ohms']==470000 else '1M',f'{r["std_median_counts"]:.3f}',f'{r["ptp_max_counts"]:.1f}',f'{r["baseline_spread_counts"]:.1f}',f'{r["baseline_spread_V"]:.6f}',f'{r["baseline_center_V"]:.6f}'])
        table(['Settle us','Array','MUX / IN','Bias ohm','Noise SD counts','Worst P-P counts','Spread counts','Spread V','Center V'],rows,[40,36,42,46,67,64,55,70,70],7.3,2.5)
    page();title('Integrity, findings and scope')
    total=sum(r['frames'] for r in runs); acquired=sum(r['sampling_sweeps'] for r in runs);drops=sum(r['usb_frames_discarded'] for r in runs)
    p(f'<b>All 30 captures validated.</b> {total:,} received full sweeps ({total*50:,} channel values), including warm-up. Firmware acquired {acquired:,} sweeps, sent {total:,} and intentionally discarded {drops:,} complete sweeps ({100*drops/acquired:.5f}%). Every transmitted sweep was received. No malformed/partial frames, invalid ADC codes or new transfer, data or USB-write errors.')
    rate_difference=max(abs(r['sampling_hz_full_run_estimate']/r['received_hz_per_channel']-1)*100 for r in runs)
    p(f'Firmware counter-derived acquisition rates over the full run (including warm-up) differ from the measured delivered rates by at most {rate_difference:.4f}%. These rate estimates use acquired sweep count divided by the recorded full-run duration plus one typical sweep period. USB omissions were too few to materially alter this comparison; individual estimates are in capture_metrics.csv.')
    p('Every capture was decoded by an independent structured frame reader and the existing incremental protocol parser using chunk boundaries that split frames. Samples, average timing, start/end timestamps and counts agreed. Device times were unwrapped and checked for strict progression. SHA-256 hashes bind every raw capture to its session metadata.')
    rows=[]
    for s in S:
        off=rr(s,False);on=rr(s,True)
        rows.append([s,f'{100*(1-mid(on,"received_hz_per_channel")/mid(off,"received_hz_per_channel")):.1f}%',f'{gg(s,False,1)["baseline_spread_V"]*1000:.2f} / {gg(s,True,1)["baseline_spread_V"]*1000:.2f}',f'{gg(s,False,2)["baseline_spread_V"]*1000:.2f} / {gg(s,True,2)["baseline_spread_V"]*1000:.2f}'])
    table(['Settle us','VMID-on rate cost','A spread mV: off / on','B spread mV: off / on'],rows,[67,106,157,160])
    off1=mid(rr(1,False),'received_hz_per_channel');off50=mid(rr(50,False),'received_hz_per_channel')
    p(f'Increasing settling from 1 to 50 us with VMID off lowers received rate from {off1:,.1f} to {off50:,.1f} samples/s/channel. In array B, the corresponding baseline spread changes from {gg(1,False,2)["baseline_spread_V"]*1000:.2f} to {gg(50,False,2)["baseline_spread_V"]*1000:.2f} mV. These are measurements under the present open-connector setup, not an electrical diagnosis.')
    worst=max(channels,key=lambda r:r['std_counts'])
    p(f'Largest median quiet SD: route {worst["route"]}, {worst["settle_us"]} us / VMID {"on" if worst["vmid"] else "off"}: {worst["std_counts"]:.3f} counts ({worst["std_V"]:.6f} V), P-P {worst["ptp_counts"]:.1f} counts ({worst["ptp_V"]:.6f} V). Channel-specific variations are retained by the common burst mask.')
    over=sum(int(r['sampling_period_over_1ms']) for r in runs)
    p(f'Firmware sampling intervals above 1 ms: {over:,}. Long sweep periods are expected at high settling delay; this counter alone is not an error indication. Maximum sampling periods and individual repetition metrics are retained in capture_metrics.csv.')
    p('No firmware-source, wire-format or hardware changes were made. The board was left stopped and its original full-array, MUX-order, blocking, 16 MHz, repeat-1, 5 us, VMID-off configuration restored. These captures do not establish ghosting, crosstalk under stimulation, channel identity by known-voltage stimulation, or a minimum adequate settling time for attached sensors.')
    p('Evidence: session.json and commands.log; 30 raw captures with status snapshots and hashes; 30 quiet-bin CSVs; capture_metrics.csv; 1,500 per-channel repetition rows; 500 channel summaries; 100 array/MUX group summaries. Reproduce with Arduino_Sketches/TestBoard_ADC124/benchmarks/capture_adc124_settling.py and Arduino_Sketches/TestBoard_ADC124/benchmarks/report_adc124_settling.py.',True)
    pdf.parent.mkdir(parents=True,exist_ok=True)
    def footer(canvas,doc):
        canvas.saveState();canvas.setFont('Helvetica',7);canvas.setFillColor(colors.HexColor('#607c90'))
        canvas.drawString(42,25,'TestBoard_ADC124 | 13 October 2026 | 16 MHz | settling 1-50 us | VMID off/on')
        canvas.drawRightString(A4[0]-42,25,str(doc.page));canvas.restoreState()
    SimpleDocTemplate(str(pdf),pagesize=A4,rightMargin=42,leftMargin=42,topMargin=38,bottomMargin=42,title='TestBoard_ADC124 - Settling and VMID comparison',author='TestBoard hardware characterization').build(story,onFirstPage=footer,onLaterPages=footer)
    print('PDF',pdf,flush=True)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('source',type=Path);parser.add_argument('output',type=Path)
    parser.add_argument('--pdf',type=Path);parser.add_argument('--partial',action='store_true')
    parser.add_argument('--engine',choices=('blocking','dma','lpspi'),default='lpspi')
    args=parser.parse_args()
    result,plots=analyze(args.source,args.output,args.partial,args.engine)
    if args.pdf:build_report(result,plots,args.output,args.pdf)

if __name__=='__main__':main()
