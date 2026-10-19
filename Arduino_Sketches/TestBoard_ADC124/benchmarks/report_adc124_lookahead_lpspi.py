"""LPSPI-only full disconnected lookahead comparison, with baseline line plots."""
from pathlib import Path
import argparse,json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from reportlab.platypus import SimpleDocTemplate,Paragraph,Table,TableStyle,Image,PageBreak,Spacer
from reportlab.lib.pagesizes import A4,landscape
from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
from reportlab.lib import colors
from analyze_adc124_upgrade import ROOT,ROUTES,LSB,key
from analyze_adc124_scan_modes import csv_write

DELAYS=(1,2,5,10,50);COLORS=('#0072B2','#E69F00','#009E73','#CC79A7','#D55E00')


def build(folder,pdf,preview=False):
    data=json.loads((folder/'analysis.json').read_text());raw=data['raw_summary'];quiet=data['summary'];channels=data['raw_channels']
    if not preview:
        current=[r for r in raw if r['dataset']=='lookahead']
        assert len(current)==28 and all(r['repetitions']==3 for r in current)
        assert data['lifecycle']['state']=='complete_production_restored_verified' and not data['partial']
    figdir=folder/'figures';figdir.mkdir(exist_ok=True)
    plt.rcParams.update({'font.size':8,'axes.grid':True,'grid.alpha':.2,'axes.spines.top':False})
    styles=getSampleStyleSheet();styles.add(ParagraphStyle(name='Body',fontSize=9,leading=12,spaceAfter=7))
    styles.add(ParagraphStyle(name='Small',fontSize=7.5,leading=10,spaceAfter=5))
    styles['Heading1'].fontSize=16;styles['Heading1'].leading=20;styles['Heading1'].textColor=colors.HexColor('#184f73')
    story=[];width=758
    def p(t,small=False):story.append(Paragraph(t,styles['Small' if small else 'Body']))
    def h(t):story.append(Paragraph(t,styles['Heading1']))
    def page():story.append(PageBreak())
    def table(head,rows,widths=None):
        style=ParagraphStyle('Cell',fontSize=7.3,leading=9)
        t=Table([[Paragraph(str(c),style) for c in row] for row in [head]+rows],colWidths=widths or [width/len(head)]*len(head),repeatRows=1)
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e6eef4')),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f4f6f8')]),
            ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)]))
        story.extend([t,Spacer(1,6)])
    def chart(fig,name,height=340):
        path=figdir/(name+'.png');fig.savefig(path,dpi=180);plt.close(fig);story.append(Image(str(path),width=width,height=height))
    def n(x,d=3):return '-' if x is None or not np.isfinite(x) else f'{x:,.{d}f}'
    def get(mode,vmid,delay=5,vd=None,clock=16000000,dataset='lookahead',values=None):
        vd=delay if vd is None else vd
        return next((r for r in (raw if values is None else values) if (r['dataset'],r['mode'],r['vmid'],r['settle_us'],r['vmid_settle_effective_us'],r['requested_spi_clock_hz'])==(dataset,mode,vmid,delay,vd,clock)),None)
    def val(r,f):return r.get(f,np.nan) if r else np.nan
    def route_points(setting,array,field='baseline_counts'):
        v={c['route']:c for c in channels if setting and key(c)==key(setting)}
        return np.array([v.get(str(r),{}).get(field,np.nan) for r in ROUTES if r.array==array])
    def worst_route(setting):
        selected=[c for c in channels if setting and key(c)==key(setting)]
        return max(selected,key=lambda c:c['sd_counts'])['route'] if selected else '-'
    def baseline_axis(ax,array):
        ax.set_xticks(range(25),[f'M{r.mux}/{r.input}' for r in ROUTES if r.array==array],rotation=90,fontsize=6)
        for edge in (5.5,11.5,17.5):ax.axvline(edge,color='#cccccc',lw=.7)
        ax.set(xlabel='Physical route: MUX / external address',ylabel='Median baseline (counts)');ax.ticklabel_format(axis='y',useOffset=False)
        ax.secondary_yaxis('right',functions=(lambda x:x*LSB,lambda x:x/LSB)).set_ylabel('Baseline (V)')
    title='TestBoard_ADC124 - selector lookahead / LPSPI'
    story.append(Paragraph(title,styles['Title']))
    p('14 October 2026 | Both sensor connectors disconnected | Charger connected | TPS7A2033PDBVR 3.3 V LDO')
    h('Fresh measurements and matched previous-firmware comparison')
    p('Current captured revision adc124-selector-lookahead-v1, selector policy lookahead, zero batch-prime frames. A four-channel external-address group now uses four conversions; address 6 uses one IN4 conversion. A complete sweep still contains 50 useful sensor samples across both arrays. Optional VMID batches have four discarded conversions per ADC. The saved previous revision adc124-vmid-settle-v1 used a discarded selector prime in each batch.')
    p('LPSPI only, manual timed acquisition, repeat 1, burst framing. Both ADC and shared-MUX scan modes; sensor settling 1/2/5/10/50 us, VMID off/on, VMID settling equal to sensor settling. Extra tests: sensor 5 us, VMID on, VMID waits 0/1 us; sensor=VMID=5 us at requested 8 MHz versus 16 MHz, VMID off/on. 28 settings, three repetitions, each 1 s warm-up + 15 s measurement. Production firmware only; no DMA/blocking reruns or diagnostic acquisition.')
    currentrows=[r for r in data['rows'] if r['dataset']=='lookahead'];priorrows=[r for r in data['rows'] if r['dataset']=='previous']
    table(['Current captures','Matched previous captures','Current received sweeps','Accounted USB discards','Qualified current quiet records'],[[len(currentrows),len(priorrows),f"{sum(r['frames'] for r in currentrows):,}",sum(r['usb_frames_discarded'] for r in currentrows),sum(r['quiet_qualified'] for r in currentrows)]])
    p('Noise is reported twice: unfiltered measurements include every excursion; quiet-period measurements use the same common all-channel mask as the prior report. Baseline line graphs use full-window medians. Nominal 1 count = 0.805664 mV. The data cannot identify electrical interference sources or prove physical channel identity without known-input stimulation.',True)
    table(['5 us / 16 MHz','Previous rate Hz/channel','Current rate Hz/channel','Gain %','Current raw worst SD counts','Current quiet worst SD counts'],[
        [m.upper()+' / VMID '+('on' if v else 'off'),n(val(get(m,v,dataset='previous'),'delivered_sweeps_s'),1),n(val(get(m,v),'delivered_sweeps_s'),1),
         n(100*(val(get(m,v),'delivered_sweeps_s')/val(get(m,v,dataset='previous'),'delivered_sweeps_s')-1),2),n(val(get(m,v),'all_sd_max_counts')),n(val(get(m,v,values=quiet),'all_sd_max_counts'))] for m in ('adc','mux') for v in (False,True)])

    page();h('Sampling rates across settling delays')
    fig,axes=plt.subplots(2,2,figsize=(12,5.3),constrained_layout=True)
    for i,m in enumerate(('adc','mux')):
        for j,v in enumerate((False,True)):
            ax=axes[i,j]
            for dataset,label,color in [('previous','Previous primed','#777777'),('lookahead','Current lookahead','#0072B2')]:
                s=[get(m,v,d,dataset=dataset) for d in DELAYS]
                ax.plot(DELAYS,[val(r,'delivered_sweeps_s') for r in s],'o-',label=label,color=color)
            ax.set(title=m.upper()+' / VMID '+('on' if v else 'off'),xlabel='Sensor = VMID settling (us)',ylabel='Samples/s/channel');ax.legend(fontsize=7)
    chart(fig,'sampling_rates');p('Requested 16 MHz. One useful sample per physical channel per complete sweep; aggregate useful values/s is 50 times this rate. Rates use delivered device timestamp intervals after warm-up. Accounted USB discards can reduce delivered rate; acquisition counters provide a separate whole-run estimate. Repetition medians are plotted.',True)
    for m in ('adc','mux'):
        page();h(m.upper()+' scan - matched settling, rates and baseline spread')
        records=[]
        for v in (False,True):
            for d in DELAYS:
                cur=get(m,v,d);old=get(m,v,d,dataset='previous');q=get(m,v,d,values=quiet)
                records.append(['on' if v else 'off',d,n(val(old,'delivered_sweeps_s'),1),n(val(cur,'delivered_sweeps_s'),1),n(100*(val(cur,'delivered_sweeps_s')/val(old,'delivered_sweeps_s')-1),2),
                    n(val(cur,'all_sd_max_counts')),n(val(q,'all_sd_max_counts')),n(val(cur,'all_ptp_max_counts'),1),
                    n(val(cur,'A1_baseline_spread_counts'),1),n(val(cur,'A2_baseline_spread_counts'),1),worst_route(cur)])
        table(['VMID','Delay us','Old Hz','New Hz','Gain %','Raw worst SD counts','Quiet worst SD counts','Raw worst p-p counts','A spread counts','B spread counts','Worst raw SD route'],records)
        p('Full-window baseline spread is max-min across the 25 route medians in that array. Temporal SD and baseline spread measure different things. Worst SD/p-p values are the maximum across per-route medians of repetition metrics. Full per-channel data include volts, repetition ranges, baseline population SD and quiet qualification. Neither smoothing nor individual-channel clipping is applied.',True)

    for metric,title2 in [('all_sd_max_counts','Worst channel temporal SD'),('all_ptp_max_counts','Worst channel peak-to-peak')]:
        page();h(title2+' - raw and quiet noise')
        fig,axes=plt.subplots(2,2,figsize=(12,5.3),constrained_layout=True)
        for i,m in enumerate(('adc','mux')):
            for j,v in enumerate((False,True)):
                ax=axes[i,j]
                for ds,col,label in [('previous','#777777','Previous'),('lookahead','#0072B2','Current')]:
                    for values,style,suffix in [(raw,'-',' raw'),(quiet,'--',' quiet')]:
                        ax.plot(DELAYS,[val(get(m,v,d,dataset=ds,values=values),metric) for d in DELAYS],'o'+style,color=col,label=label+suffix,ms=3)
                ax.set(title=m.upper()+' / VMID '+('on' if v else 'off'),xlabel='Sensor = VMID settling (us)',ylabel=title2+' (counts)');ax.legend(fontsize=6)
                ax.secondary_yaxis('right',functions=(lambda x:x*LSB*1000,lambda x:x/LSB/1000)).set_ylabel('Noise (mV)')
        chart(fig,'noise_'+metric);p('Solid: unfiltered full windows. Dashed: common quiet windows. Quiet-only metrics do not imply absence of pulses. A zero digital SD means identical recorded codes, not zero analog noise. Peak-to-peak depends on sampled extremes and observation length; both generations have 15 s measurement windows, but quiet retention and sample rates differ.',True)

    for mode in ('adc','mux'):
        page();h(mode.upper()+' - every setting / previous versus current unfiltered noise')
        table(['VMID','Sensor / VMID us','MHz','Old SD counts','New SD counts','Old p-p counts','New p-p counts','Rate gain %'],[
            ['on' if r['vmid'] else 'off',f"{r['settle_us']}/{r['vmid_settle_effective_us']}",r['requested_spi_clock_hz']//1000000,
             n(r['previous_raw_all_sd_max_counts']),n(r['current_raw_all_sd_max_counts']),n(r['previous_raw_all_ptp_max_counts'],1),n(r['current_raw_all_ptp_max_counts'],1),n(r['speed_gain_percent'],2)] for r in data['comparisons'] if r['mode']==mode])
        p('All exact setting matches include explicit VMID delay 0/1 us and requested 8 MHz; neither clocks nor effective VMID delays are pooled. Environmental drift and shorter analog conversion history remain possible contributors to changes.',True)

    for m in ('adc','mux'):
        page();h('Every channel baseline - '+m.upper()+' scan / current lookahead')
        fig,axes=plt.subplots(2,2,figsize=(12,5.3),constrained_layout=True)
        for i,v in enumerate((False,True)):
            for j,array in enumerate((1,2)):
                ax=axes[i,j]
                for d,color in zip(DELAYS,COLORS):ax.plot(range(25),route_points(get(m,v,d),array),'.-',lw=.9,ms=3,label=str(d)+' us',color=color)
                baseline_axis(ax,array);ax.set_title(f"Array {'A' if array==1 else 'B'} / VMID {'on' if v else 'off'}");ax.legend(fontsize=6,ncol=5)
        chart(fig,'baselines_'+m);p('16 MHz, sensor=VMID settling. Full-window per-route median in each repetition, aggregated by median across three repetitions. Lines connect discrete routes for comparison. External addresses are zero-based; MUX1..4 feed ADC IN1..4. Connector channel mappings and bias resistors are retained in the CSV.',True)
        page();h('Baseline changes - '+m.upper()+' / 5 us / 16 MHz')
        fig,axes=plt.subplots(2,2,figsize=(12,5.3),constrained_layout=True)
        for i,v in enumerate((False,True)):
            for j,array in enumerate((1,2)):
                ax=axes[i,j]
                for ds,col,label in [('previous','#777777','Previous primed'),('lookahead','#0072B2','Current lookahead')]:ax.plot(range(25),route_points(get(m,v,dataset=ds),array),'.-',lw=1,label=label,color=col)
                baseline_axis(ax,array);ax.set_title(f"Array {'A' if array==1 else 'B'} / VMID {'on' if v else 'off'}");ax.legend(fontsize=7)
        chart(fig,'baseline_comparison_'+m);p('Same physical routes and requested settings. Baseline changes are observations, not calibrated offset measurements. The recordings were sequential and no reference/VMID rail waveforms were measured. All setting-specific route shifts, in counts and volts, are in channel_comparisons.csv.',True)

    page();h('Independent VMID delay at sensor settling 5 us')
    table(['Mode','VMID wait us','Previous Hz','Current Hz','Gain %','Raw SD counts / mV','Quiet SD counts / mV','Raw p-p counts / mV'],[
        [m.upper(),vd,n(val(get(m,True,vd=vd,dataset='previous'),'delivered_sweeps_s'),1),n(val(get(m,True,vd=vd),'delivered_sweeps_s'),1),
         n(100*(val(get(m,True,vd=vd),'delivered_sweeps_s')/val(get(m,True,vd=vd,dataset='previous'),'delivered_sweeps_s')-1),2),
         n(val(get(m,True,vd=vd),'all_sd_max_counts'))+' / '+n(val(get(m,True,vd=vd),'all_sd_max_volts')*1000),
         n(val(get(m,True,vd=vd,values=quiet),'all_sd_max_counts'))+' / '+n(val(get(m,True,vd=vd,values=quiet),'all_sd_max_volts')*1000),
         n(val(get(m,True,vd=vd),'all_ptp_max_counts'),1)+' / '+n(val(get(m,True,vd=vd),'all_ptp_max_volts')*1000)] for m in ('adc','mux') for vd in (0,1,5)])
    p('16 MHz, VMID on. Zero omits the VMID analog wait while preserving switching guards and the four discarded VMID conversions. Sensor delay stays 5 us. Settling changes, clock changes and conversion-history changes are separate experimental variables.',True)
    page();h('Every channel baseline - independent VMID settling')
    fig,axes=plt.subplots(2,2,figsize=(12,5.3),constrained_layout=True)
    for i,m in enumerate(('adc','mux')):
        for j,array in enumerate((1,2)):
            ax=axes[i,j]
            for vd,col in zip((0,1,5),COLORS):ax.plot(range(25),route_points(get(m,True,vd=vd),array),'.-',lw=.9,label=f'VMID {vd} us',color=col)
            baseline_axis(ax,array);ax.set_title(m.upper()+f" / array {'A' if array==1 else 'B'}");ax.legend(fontsize=7)
    chart(fig,'vmid_delay_baselines');p('Current lookahead, 16 MHz, VMID on, sensor 5 us. Every point is a full-window median baseline, with all three repetitions contributing.',True)
    page();h('SPI clock comparison - requested 8 versus 16 MHz')
    table(['Mode','VMID','Old 8 MHz Hz','New 8 MHz Hz','Old 16 MHz Hz','New 16 MHz Hz','New 8 MHz raw SD','New 16 MHz raw SD'],[
        [m.upper(),'on' if v else 'off',n(val(get(m,v,clock=8000000,dataset='previous'),'delivered_sweeps_s'),1),n(val(get(m,v,clock=8000000),'delivered_sweeps_s'),1),
         n(val(get(m,v,dataset='previous'),'delivered_sweeps_s'),1),n(val(get(m,v),'delivered_sweeps_s'),1),n(val(get(m,v,clock=8000000),'all_sd_max_counts')),n(val(get(m,v),'all_sd_max_counts'))] for m in ('adc','mux') for v in (False,True)])
    p('Sensor=VMID settling 5 us. Both VMID off/on. The faster request also changes analog sampling history; it is not a transport-only change. Actual SCLK was not probed.',True)
    for v in (False,True):
        page();h('Every channel baseline - SPI clocks / VMID '+('on' if v else 'off'))
        fig,axes=plt.subplots(2,2,figsize=(12,5.3),constrained_layout=True)
        for i,m in enumerate(('adc','mux')):
            for j,array in enumerate((1,2)):
                ax=axes[i,j]
                for clock,col in zip((8000000,16000000),COLORS):ax.plot(range(25),route_points(get(m,v,clock=clock),array),'.-',lw=.9,label=str(clock//1000000)+' MHz',color=col)
                baseline_axis(ax,array);ax.set_title(m.upper()+f" / array {'A' if array==1 else 'B'}");ax.legend(fontsize=7)
        chart(fig,'clock_baselines_'+str(v));p('Current lookahead, sensor=VMID settling 5 us. Full-window baseline medians across three repeats.',True)

    page();h('Conversion budgets and measured settling slopes')
    table(['Full sweep / repeat 1','Previous words','Current words','Words removed','Sensor / VMID waits: ADC','Sensor / VMID waits: MUX'],[
        ['VMID off',64,50,14,'14 / 0','7 / 0'],['VMID on',134,106,28,'14 / 14','7 / 7']])
    p('Four-input sensor and VMID groups now use four words, instead of five. The sensor group at address 6 uses one word instead of two. Nominal clocking saved: 14 us off / 28 us on at 16 MHz, or 28/56 us at 8 MHz. These are arithmetic budgets, not measured GPIO/SPI edge timing. Both ADC and MUX modes use one shared SPI bus. Startup selector initialization is outside steady-state sweep counts.',True)
    p('ADC mode finishes array A before array B. MUX mode switches both banks at each address, shares one settling wait and reads ADC A then B. Bank B therefore continues settling during A\'s read batch. Removing the prime changes this additional analog dwell as well as the SPI word count; equal configured delays do not guarantee identical effective dwell between revisions.',True)
    fits=[]
    for m in ('adc','mux'):
        for v in (False,True):
            for ds in ('previous','lookahead'):
                pts=[get(m,v,d,dataset=ds) for d in DELAYS]
                if any(r is None for r in pts):continue
                yy=np.array([r['mean_period_us'] for r in pts]);slope,intercept=np.polyfit(DELAYS,yy,1);res=yy-(intercept+slope*np.array(DELAYS))
                fits.append(dict(dataset=ds,mode=m,vmid=v,intercept_us=float(intercept),slope_us_per_us=float(slope),expected_slope=(14 if m=='adc' else 7)*(2 if v else 1),max_fit_residual_us=float(np.max(np.abs(res)))))
    csv_write(folder/'delay_fits.csv',fits)
    table(['Firmware','Mode','VMID','Fit intercept us','Measured us/us slope','Expected slope','Max fit residual us'],[[r['dataset'],r['mode'].upper(),'on' if r['vmid'] else 'off',n(r['intercept_us']),n(r['slope_us_per_us'],4),r['expected_slope'],n(r['max_fit_residual_us'])] for r in fits])
    p('Fit period = intercept + slope x matched sensor/VMID delay. Intercepts contain conversion clocking, GPIO/software work, interrupts and enqueue gaps. No diagnostic profiling was run, so this report does not assign those residuals to individual phases.',True)

    noise_file=folder/'selected_noise_current.json'
    if noise_file.exists():
        noise=json.loads(noise_file.read_text())
        if not preview and noise.get('partial'):raise ValueError('Partial noise audit cannot be published')
        page();h('Complete selected-route noise audit - MUX/LPSPI/VMID off')
        p('Requested 16 MHz, sensor settling 1/5 us. All 50 routes at each delay, three repeats. Select median unfiltered SD >0.5 counts and max absolute autocorrelation over lags 1-64 >=0.05 in at least two repeats. Listed cases are descriptive evidence of temporal structure, without identifying cause. Below-threshold routes can also be structured.',True)
        if noise['selected']:
            table(['Settle us','Array','MUX / ADC IN','External address','Sensor/channel','SD counts','SD repeat range','SD >0.5 repeats','Lag-1','Old SD counts'],[
                [r['settle_us'],'A' if r['array']==1 else 'B',f"{r['mux']} / IN{r['mux']}",r['external_address'],r['sensor']+'/'+str(r['sensor_channel']),n(r['sd_counts'],4),n(r['sd_min_counts'],4)+'-'+n(r['sd_max_counts'],4),r['sd_above_repeats'],n(r['lag1_acf'],3),n(r['previous_sd_counts'],4)] for r in noise['selected']])
        else:p('No current routes meet both the >0.5-count median SD and reproducible-pattern criteria in these two settings.')
        p('Qualifying routes by delay: '+', '.join(str(delay)+' us: '+str(sum(r['settle_us']==delay for r in noise['selected'])) for delay in (1,5))+'. Route addresses are zero-based; connector sensor/channel labels remain physical positions despite sensors being disconnected. One count = 0.805664 mV. PSD section/window coverage is retained in noise_psd_coverage.csv.',True)
        p('The time/PSD figures on following pages include every selected current route. Original problem routes are also compared separately. SD and PSD use full unfiltered recordings, rather than quiet-only selection.',True)
        for f in noise['figures']:
            page();h(f['title']);story.append(Image(str(folder/f['path']),width=width,height=f.get('height',335)))
            p(f.get('caption','Left: raw first 25 ms after warm-up, referenced to each repetition\'s full median. Right: Welch PSD from each 15 s capture. All three repetitions shown. Hann windows 4096 samples, 50% overlap; split dropped-frame gaps, exclude sections shorter than 4096, and remove window means. Incomplete tails are unused; sampling clock is approximate. No quiet mask, smoothing or independent channel clipping.'),True)

    page();h('Noise method, verification and final state')
    p('Unfiltered SD and p-p use all measured samples after warm-up. Quiet values reuse the prior common-mask method: 50 ms bins, median channel SD in each of eight array/MUX groups; group threshold max(0.6 counts, P20+0.15, lower40% median+4 x 1.4826 x MAD). Reject a bin exceeding any group threshold and its neighbors. Then reject coherent separated secondary-state excursions on at least two routes, plus one neighboring sweep on each side, identically for all 50 channels.')
    p('The fine detector anchors dominant one-code states (>=60%) or adjacent two-code states (>=80%), allows one extra count, and requires >=1% tail mass plus a separated histogram peak >=0.5% and >=3 times edge density. Unimodal Gaussian tails and isolated route variation remain. A quiet record requires >=1 s and >=100 retained frames. Retained seconds credit observed intervals only, capped at median period. These masks do not establish a charger cause or guarantee white noise.',True)
    p('Per-setting noise/baseline values are per-route medians of three repetition statistics; maxima are selected independently across routes. Baseline spread is max-min of route baseline medians, while population SD across baselines is also saved. Both differ from temporal noise. Counts use nominal 3.3/4096 V; voltage/reference calibration was not performed.')
    table(['Generation','Captures','Qualified quiet records','Firmware error totals'],[[ds,len([r for r in data['rows'] if r['dataset']==ds]),sum(r['quiet_qualified'] for r in data['rows'] if r['dataset']==ds),sum(r[k] for r in data['rows'] if r['dataset']==ds for k in ('transfer_errors','data_errors','usb_write_errors'))] for ds in ('previous','lookahead')])
    p('Each stream passes raw SHA-256, two independent binary decoders, route labels, 12-bit ranges, status/configuration, nonoverlapping timestamps and sent/received accounting. Firmware intentionally discards complete sweeps under USB pressure; received must equal sent, and acquired equals sent+discarded. Both firmware builds passed; 81 focused Python checks and two native firmware checks passed. Native checks include exact lookahead conversion counts and digital payload mapping; physical wiring requires known-input stimulation.',True)
    final=data['lifecycle'].get('final_production_status',{})
    p('Lifecycle: '+data['lifecycle']['state']+'. Final production revision '+final.get('firmware_revision','pending')+', running='+final.get('running','pending')+'. Original settings restored. Frozen source/HEX images and hashes, command/status logs and raw data accompany the dataset. Only the frozen current production image was uploaded for this rerun; no firmware source changes were made.',True)
    page();h('Data and reproducibility')
    p('Current raw: '+data['raw_root']);p('Matched reference raw: '+data['reference_root'])
    table(['File','Contents'],[['analysis.json / analysis_cache.json','Verified structured results and hash-bound per-capture cache'],['capture_metrics.csv / summary.csv','Quiet retention, transport accounting, timing and setting summaries'],
        ['channel_summary.csv / channel_repetitions.csv','Quiet route baseline, sample SD and p-p, counts and volts'],['unfiltered_*','Every-sample capture/setting/channel metrics, including temporal correlations in repetition records'],
        ['firmware_comparisons.csv','All 28 exact-setting rate and unfiltered-noise comparisons'],['channel_comparisons.csv','Every route baseline shift and unfiltered noise comparison'],
        ['delay_fits.csv','Measured matched-delay slopes/intercepts, both revisions'],['*quiet_mask.npz / *fine_quiet_states.json','Common raw-frame masks and detector parameters'],
        ['selected_noise_current.json / selected_noise_audit.csv','All-route MUX/LPSPI/VMID-off 1/5 us pattern audit and qualifying cases'],['figures/','Baseline line graphs, rate/noise comparisons and raw time/PSD plots']],[240,518])
    p('Reproduce with benchmark_adc124_lookahead_lpspi.py, analyze_adc124_lookahead_lpspi.py and report_adc124_lookahead_lpspi.py in Arduino_Sketches/TestBoard_ADC124/benchmarks. Preserve both disconnected generations before the later connected-sensor experiment. No commit or push was performed.',True)
    def footer(canvas,doc):
        canvas.saveState();canvas.setFont('Helvetica',7);canvas.drawString(42,22,'ADC124 | lookahead / LPSPI | disconnected | 2026-10-14'+(' | PREVIEW' if preview else ''));canvas.drawRightString(800,22,str(doc.page));canvas.restoreState()
    pdf.parent.mkdir(parents=True,exist_ok=True);SimpleDocTemplate(str(pdf),pagesize=landscape(A4),leftMargin=42,rightMargin=42,topMargin=35,bottomMargin=36,title=title).build(story,onFirstPage=footer,onLaterPages=footer)
    if not preview:
        lines=['# TestBoard_ADC124 selector lookahead - full LPSPI disconnected rerun','',
            '14 October 2026. Complete; production firmware stopped and original settings restored.','',
            f'[PDF report](../../../../reports/exports/{pdf.name})','',
            'Current captured revision `adc124-selector-lookahead-v1`, policy `lookahead`, zero batch-prime frames. '
            'Matched reference: `adc124-vmid-settle-v1` from the previous extended disconnected test. '
            'Both arrays, 50 useful routes, manual timed acquisition, repeat 1, burst SPI, LPSPI only. '
            'Connectors remain disconnected; charger connected; TPS7A2033PDBVR LDO. Nominal reference 3.3 V, VMID 1.65 V. '
            'Biases A: 470k/1M/470k/1M; B: 1M/470k/1M/470k.','',
            '## Matrix and verification','',
            '28 settings x 3 repetitions: ADC/MUX scan, sensor settling 1/2/5/10/50 us, VMID off/on and matching VMID delay at 16 MHz; '
            'extra sensor-5-us/VMID-on waits 0/1 us; requested 8 versus 16 MHz at sensor=VMID=5 us, VMID off/on. '
            'Each recording has 1 s warm-up plus 15 s measurement. Only production LPSPI captures; DMA, blocking and profiling were not rerun.','',
            f"{len(currentrows)} current plus {len(priorrows)} matched previous captures verified. "
            f"Current: {sum(r['frames'] for r in currentrows):,} full sweeps received, "
            f"{sum(r['sampling_sweeps'] for r in currentrows):,} acquired, "
            f"{sum(r['usb_frames_discarded'] for r in currentrows)} accounted USB discards. "
            'Every sent frame received; all transfer/data/USB-write errors zero. Raw hashes, two independent decoders, status/routes, '
            '12-bit ranges and timestamp accounting passed. Both PlatformIO builds passed; 81 focused Python checks and two native firmware checks passed.','',
            f"Current quiet records qualified: {sum(r['quiet_qualified'] for r in currentrows)}/{len(currentrows)}; "
            f"minimum retained quiet duration {min(r['quiet_seconds'] for r in currentrows):.3f} s.",'',
            '## Matched 5 us, requested 16 MHz','',
            '| Mode | VMID | Previous Hz/channel | Current Hz/channel | Gain % | Current raw worst SD counts | Current quiet worst SD counts |',
            '|---|---|---:|---:|---:|---:|---:|']
        for m in ('adc','mux'):
            for v in (False,True):
                cur=get(m,v);old=get(m,v,dataset='previous');q=get(m,v,values=quiet)
                lines.append(f"| {m.upper()} | {'on' if v else 'off'} | {old['delivered_sweeps_s']:.1f} | {cur['delivered_sweeps_s']:.1f} | {100*(cur['delivered_sweeps_s']/old['delivered_sweeps_s']-1):.2f} | {cur['all_sd_max_counts']:.4f} | {q.get('all_sd_max_counts',float('nan')):.4f} |")
        lines+=['','## Noise and baseline definitions','',
            'Unfiltered statistics include every measured sample; quiet statistics apply the same two-stage common all-channel mask to both generations. '
            'The 50 ms group-envelope stage and fine coherent separated-state stage retain their prior definitions. No independent channel clipping, '
            'smoothing or detrending is used for temporal SD/p-p. Baseline line graphs use full-window medians; quiet baselines are also saved. '
            'Per-setting values are medians of three per-route repetition metrics. Baseline spread across routes is distinct from temporal noise. '
            'Nominal conversion is 3.3/4096 V/count. Zero digital SD is not zero analog noise; a below-0.5-count route need not be white.','',
            'The 1/5 us MUX/LPSPI/VMID-off noise audit checks every route at 16 MHz. Selection requires median unfiltered SD >0.5 counts and '
            'maximum absolute autocorrelation at lags 1-64 >=0.05 in at least two repeats. Time/PSD figures show every selected current route, '
            'representative previously structured routes, and additional ADC-1-us/MUX-2-us cases. This describes sampled patterns without establishing '
            'a charger, rail or acquisition cause. PSD section/window coverage is saved; aliasing and ADC aperture uncertainty remain.','',
            '## Conversion history','',
            'Full sweeps now use 50 SPI words with VMID off, 106 on, versus 64/134 previously. A four-input group uses four conversions; '
            'address 6 uses one. VMID conversion responses remain discarded. Removing the selector prime changes both SPI overhead and analog dwell. '
            'MUX mode reads ADC B after A, adding dwell for B. Nominal word savings are arithmetic budgets, not oscilloscope measurements.','',
            '## Data','',
            '- [Every exact-setting firmware comparison](firmware_comparisons.csv)',
            '- [Every route baseline shift and unfiltered noise comparison](channel_comparisons.csv)',
            '- [Unfiltered channel summaries](unfiltered_channel_summary.csv)',
            '- [Unfiltered repetitions and correlations](unfiltered_channel_repetitions.csv)',
            '- [Quiet channel summaries](channel_summary.csv)',
            '- [Capture timing and transport accounting](capture_metrics.csv)',
            '- [Baseline line graphs and other figures](figures/)',
            '- [All-route selected-noise audit](selected_noise_audit.csv)',
            '- [Noise spectra](noise_psd.csv)',
            '- [Raw time zooms](noise_time_zoom.csv)',
            '- [PSD coverage](noise_psd_coverage.csv)','',
            f"Raw current: `{data['raw_root']}`. Raw matched reference: `{data['reference_root']}`. "
            'Current source/production and diagnostic HEX snapshots and hashes are frozen under `current_firmware/`; only the production HEX was uploaded '
            'for this run. Lifecycle, command logs, status before/after and raw binaries provide reproducibility. ','',
            'Reproduce with the four `*_lookahead_lpspi.py` tools in `Arduino_Sketches/TestBoard_ADC124/benchmarks`. '
            'No firmware source changes, commit or push were made. Physical channel identity and connected-sensor behavior require separate experiments. '
            'Preserve both disconnected generations for the planned connected-sensor comparison.']
        (folder/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('REPORT',pdf,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('folder',type=Path);p.add_argument('pdf',type=Path);p.add_argument('--preview',action='store_true');a=p.parse_args();build(a.folder,a.pdf,a.preview)
