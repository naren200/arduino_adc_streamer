"""Disconnected ADC124 upgrade report: tables and physical-channel line graphs."""
from pathlib import Path
import argparse
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Image, PageBreak, Spacer
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

from analyze_adc124_upgrade import ROOT, ROUTES, LSB, key
from capture_adc124_settling import decode
from analyze_adc124_scan_modes import csv_write

ENGINES=('blocking','dma','lpspi'); MODES=('adc','mux'); DELAYS=(1,2,5,10,50)
PALETTE=('#0072B2','#009E73','#D55E00'); DELAY_COLORS=('#0072B2','#E69F00','#009E73','#CC79A7','#D55E00')


def build(folder, pdf, preview=False):
    data=json.loads((folder/'analysis.json').read_text()); summary=data['summary']; channels=data['channels']; rows=data['rows']
    if not preview:
        if data['lifecycle']['state']!='complete_production_restored_verified': raise ValueError('Production state unverified')
        current=[r for r in summary if r['dataset']=='current' and r['stage']=='production']
        if len(current)!=84 or any(r['repetitions']!=3 for r in current): raise ValueError('Incomplete 84 x 3 production matrix')
        diagnostic=[r for r in summary if r['dataset']=='current' and r['stage']=='diagnostic']
        if len(diagnostic)!=84: raise ValueError('Incomplete timing diagnostics')
    plt.rcParams.update({'font.size':8,'axes.grid':True,'grid.alpha':.2,'axes.spines.top':False,'figure.dpi':160})
    figures=folder/'figures';figures.mkdir(exist_ok=True)
    def get(mode,engine,vmid,delay=5,vmid_delay=None,clock=16000000,dataset='current',stage='production'):
        if dataset=='yesterday_original': mode='adc'
        vd=delay if vmid_delay is None else vmid_delay
        return next((r for r in summary if (r['dataset'],r['stage'],r['mode'],r['engine'],r['vmid'],r['settle_us'],
            r['vmid_settle_effective_us'],r['requested_spi_clock_hz'])==(dataset,stage,mode,engine,vmid,delay,vd,clock)),None)
    def save(fig,name):
        path=figures/(name+'.png');fig.savefig(path,dpi=180);plt.close(fig);return path
    def metric(row,field): return row.get(field,np.nan) if row else np.nan
    def broad(setting):
        return next((r for r in data.get('broad_summary',[]) if setting and key(r)==key(setting)),None)
    def n(value,decimals=2): return '-' if value is None or not np.isfinite(value) else f'{value:,.{decimals}f}'
    def cv(value): return '-' if value is None else n(value,3)+' / '+n(value*LSB*1000,3)
    def points(mode,engine,vmid,delay=5,vmid_delay=None,clock=16000000,dataset='current',array=1):
        r=get(mode,engine,vmid,delay,vmid_delay,clock,dataset)
        if not r: return np.full(25,np.nan)
        selected={c['route']:c for c in channels if key(c)==key(r)}
        return np.array([selected.get(str(route),{}).get('baseline_counts',np.nan) for route in ROUTES if route.array==array])
    def baseline_axes(ax,array):
        routes=[r for r in ROUTES if r.array==array]
        ax.set_xticks(range(25),labels=[f'M{r.mux}/{r.input}' for r in routes],rotation=90,fontsize=6)
        for edge in (5.5,11.5,17.5): ax.axvline(edge,color='#cccccc',linewidth=.7)
        ax.set_xlabel('Physical route: MUX / external input'); ax.set_ylabel('Baseline (counts)')
        right=ax.secondary_yaxis('right',functions=(lambda x:x*LSB,lambda x:x/LSB))
        right.set_ylabel('Baseline (V)'); right.ticklabel_format(useOffset=False)
        ax.ticklabel_format(axis='y',useOffset=False)

    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='ReportTitle',fontName='Helvetica-Bold',fontSize=24,leading=29,textColor=colors.HexColor('#184f73'),spaceAfter=14))
    styles.add(ParagraphStyle(name='ReportBody',fontSize=9,leading=12,spaceAfter=7))
    styles.add(ParagraphStyle(name='ReportSmall',fontSize=7.6,leading=10,spaceAfter=5))
    styles['Heading1'].fontSize=16;styles['Heading1'].leading=20;styles['Heading1'].textColor=colors.HexColor('#184f73')
    story=[]; width=758
    def p(text,small=False): story.append(Paragraph(text,styles['ReportSmall' if small else 'ReportBody']))
    def h(text): story.append(Paragraph(text,styles['Heading1']))
    def page(): story.append(PageBreak())
    def chart(path,height=365): story.append(Image(str(path),width=width,height=height))
    def table(headers,records,widths=None,size=7.5,pad=4):
        cells=[[Paragraph(str(v),styles['ReportSmall']) for v in headers]]+[[Paragraph(str(v),styles['ReportSmall']) for v in r] for r in records]
        t=Table(cells,colWidths=widths or [width/len(headers)]*len(headers),repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e6eef3')),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f4f6f8')]),
            ('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),pad),('BOTTOMPADDING',(0,0),(-1,-1),pad),('LINEBELOW',(0,0),(-1,0),.5,colors.HexColor('#184f73'))]))
        story.append(t);story.append(Spacer(1,5))
    title='TestBoard_ADC124 - upgraded firmware'
    story.append(Paragraph(title,styles['ReportTitle']))
    p('14 October 2026 | No sensor strips connected | Charger connected | Production measurements')
    h('Scope and interpretation')
    p('Both full arrays: 50 useful channels/sweep. Manual timed acquisition; repeat 1; continuous-CS burst batches; requested SPI 16 MHz unless explicitly labeled 8 MHz. ADC mode scans address groups in array A, then B. MUX mode switches both banks before one shared settling wait, then reads ADC A and ADC B on their shared SPI bus.')
    p('Sensor delays 1, 2, 5, 10 and 50 us, with VMID delay explicitly equal to the sensor delay. Each has VMID off/on, both scan modes and all three engines. Extra tests: sensor 5 us with VMID on and VMID waits 0/1 us; sensor=VMID=5 us at 8 MHz with VMID off/on. Total 84 settings x 3 production repetitions, each 1 s warm-up + 15 s measurement. Diagnostic captures are separate and are excluded from production rate/noise results.')
    p('Nominal reference 3.3 V; 1 count = 3.3/4096 V = 0.805664 mV. Biases A: 470k / 1M / 470k / 1M; B: 1M / 470k / 1M / 470k. No rail or SPI-edge measurement was made. This report qualifies the disconnected setup; connected-sensor testing is a separate next phase.')
    current_rows=[r for r in rows if r['dataset']=='current'];prod=[r for r in current_rows if r['stage']=='production']
    qualified=sum(r.get('quiet_qualified',False) for r in prod)
    table(['Production captures','Diagnostic captures','Complete received sweeps','Accounted USB discards','Qualified quiet records'],[[len(prod),len(current_rows)-len(prod),
        f"{sum(r['frames'] for r in current_rows):,}",f"{sum(r['usb_frames_discarded'] for r in current_rows):,}",f'{qualified}/{len(prod)}']])
    errors={k:sum(r[k] for r in current_rows) for k in ('transfer_errors','data_errors','usb_write_errors')}
    p('Firmware error totals: '+str(errors)+'. Every captured stream passed SHA-256, independent binary decoding, 12-bit code checks, wire-route validation, nonoverlapping device timestamps and sent/received accounting.')
    table(['LPSPI / 5 us','VMID off rate','VMID on rate','VMID on worst SD (counts / mV)','VMID on worst p-p (counts / mV)'],[
        [m.upper(),n(metric(get(m,'lpspi',False),'delivered_sweeps_s'),1),n(metric(get(m,'lpspi',True),'delivered_sweeps_s'),1),
         cv(metric(get(m,'lpspi',True),'all_sd_max_counts')),cv(metric(get(m,'lpspi',True),'all_ptp_max_counts'))] for m in MODES])
    comparisons=[r for r in data['comparisons'] if r['reference']=='yesterday_shared' and r['engine']=='lpspi' and r['settle_us']==5]
    if len(comparisons)==4:
        p('At 5 us, LPSPI rate gains versus yesterday\'s same-mode burst firmware: '+
            '; '.join(m.upper()+' '+('on' if v else 'off')+' '+n(next(r['speed_gain_percent'] for r in comparisons if r['mode']==m and r['vmid']==v),2)+'%'
            for m in MODES for v in (False,True))+'.',True)
    off=get('mux','lpspi',False);on=get('mux','lpspi',True)
    if off and on:
        p('Speed and noise must be assessed together: MUX/LPSPI at 5 us has worst retained sensor SD '+cv(metric(off,'all_sd_max_counts'))+
          ' counts / mV with VMID off, versus '+cv(metric(on,'all_sd_max_counts'))+' with VMID on. Isolated channel variation remains in the analysis.',True)
    p('Primary baseline/noise values use between-burst windows: the earlier 50 ms mask plus rejection of separated, coherent short excursions. Broader-window noise is retained separately. The same two-stage method reanalyzes both historical datasets; its noise values can differ from their printed reports. Repetition summaries use medians; all route values, masks, ranges and comparisons are in CSV.',True)

    page();h('Sampling rate - both scan modes and all engines')
    fig,axes=plt.subplots(2,2,figsize=(12,5.6),constrained_layout=True)
    for i,m in enumerate(MODES):
        for j,v in enumerate((False,True)):
            ax=axes[i,j]
            for e,c in zip(ENGINES,PALETTE):
                selected=[get(m,e,v,s) for s in DELAYS]
                y=np.array([metric(r,'delivered_sweeps_s') for r in selected])
                lo=np.array([metric(r,'delivered_sweeps_s_min_rep') for r in selected]);hi=np.array([metric(r,'delivered_sweeps_s_max_rep') for r in selected])
                ax.errorbar(DELAYS,y,yerr=[y-lo,hi-y],fmt='o-',capsize=3,label=e,color=c)
            ax.set_xscale('log');ax.set_xlim(.75,65);ax.set_xticks(DELAYS,labels=DELAYS);ax.set(xlabel='Sensor = VMID settling (us)',ylabel='Delivered samples/s/channel',title=m.upper()+' / VMID '+('on' if v else 'off'));ax.legend(fontsize=7)
    chart(save(fig,'rates'));p('One sweep contains one value for each selected channel; sweep rate equals per-channel sampling rate. Multiply by 50 for useful aggregate values/s. Error bars show the minimum/maximum of three repetition rates. USB discards can lower delivered rate relative to sampling.',True)

    for field,label in (('all_sd_max_counts','Worst channel quiet SD'),('all_ptp_max_counts','Worst channel quiet peak-to-peak')):
        page();h(label+' - absolute levels')
        fig,axes=plt.subplots(2,2,figsize=(12,5.6),constrained_layout=True)
        for i,m in enumerate(MODES):
            for j,v in enumerate((False,True)):
                ax=axes[i,j]
                for e,c in zip(ENGINES,PALETTE):
                    ax.plot(DELAYS,[metric(get(m,e,v,s),field) for s in DELAYS],'-o',label=e,color=c)
                ax.set_xscale('log');ax.set_xlim(.75,65);ax.set_xticks(DELAYS,labels=DELAYS)
                ax.set(xlabel='Sensor = VMID settling (us)',ylabel=label+' (counts)',title=m.upper()+' / VMID '+('on' if v else 'off'));ax.legend(fontsize=7)
                ax.set_ylim(bottom=0)
                right=ax.secondary_yaxis('right',functions=(lambda x:x*LSB*1000,lambda x:x/(LSB*1000)))
                right.set_ylabel('mV')
        chart(save(fig,'absolute_'+field));p('16 MHz. For each physical route, combine qualified repetition metrics by median; show the maximum across all 50 routes. No individual sample clipping. Per-route values and repetition ranges are in the channel CSV.',True)

    for m in MODES:
        for v in (False,True):
            page();h(m.upper()+' scan / VMID '+('on' if v else 'off')+' - rate, quiet noise and baseline spread')
            p('16 MHz; sensor = VMID settling. Noise and spread columns show counts / mV. Baseline spread is max-min across the 25 per-route median baselines in that array. SD is temporal sample SD around each channel\'s own mean; it is separate from baseline variation across channels.',True)
            records=[]
            for e in ENGINES:
                for s in DELAYS:
                    r=get(m,e,v,s)
                    records.append([e,s,n(metric(r,'delivered_sweeps_s'),1),cv(metric(r,'all_sd_median_counts')),cv(metric(r,'all_sd_max_counts')),
                        cv(metric(r,'all_ptp_max_counts')),cv(metric(r,'A1_baseline_spread_counts')),cv(metric(r,'A2_baseline_spread_counts')),
                        n(metric(r,'quiet_seconds_min_rep'),1)+'-'+n(metric(r,'quiet_seconds_max_rep'),1)])
            table(['Engine','Delay us','Samples/s/ch','Median SD c/mV','Worst SD c/mV','Worst p-p c/mV','A spread c/mV','B spread c/mV','Quiet s range'],records,[66,44,76,91,91,91,91,91,117])
            p('Full voltage values are saved in volts in summary.csv and channel_summary.csv. Peak-to-peak is the actual retained maximum minus minimum, without individual sample clipping. It depends on retained observation length and sampling rate.',True)

    page();h('Between-burst noise and the retained broader-window measurement')
    p('LPSPI, sensor settling 5 us. The broader 50 ms method can include repeated submillisecond excursions. Primary noise excludes clearly separated excursions occurring coherently on at least two channels, with one whole-sweep guard on each side. The electrical source of these excursions was not independently measured.',True)
    records=[]
    for m in MODES:
        settings=[(v,5,16000000) for v in (False,True)]+[(True,d,16000000) for d in (0,1)]+[(v,5,8000000) for v in (False,True)]
        for v,vd,clock in settings:
            r=get(m,'lpspi',v,vmid_delay=vd,clock=clock);b=broad(r)
            records.append([m.upper(),'on' if v else 'off',vd,clock//1000000,cv(metric(b,'all_sd_max_counts')),cv(metric(r,'all_sd_max_counts')),
                cv(metric(b,'all_ptp_max_counts')),cv(metric(r,'all_ptp_max_counts')),n(metric(r,'fine_rejected_percent'),2),
                n(metric(r,'quiet_seconds_min_rep'),2)+'-'+n(metric(r,'quiet_seconds_max_rep'),2)])
    table(['Mode','VMID','VMID us','MHz','Broader SD c/mV','Between SD c/mV','Broader p-p c/mV','Between p-p c/mV','Extra rejected %','Quiet s range'],records)
    p('Broader and between-burst metrics answer different questions. The broader metric retains burst burden; the conditional metric estimates noise between detected excursions. Detection sensitivity depends on amplitude and separation of the secondary state. It does not prove that every charger-related excursion was detected, or that reduced conditional SD alone means improved acquisition quality. Whole-sweep masks and unfiltered raw data remain available.',True)

    page();h('Timing breakdown - configured budgets and measured residuals')
    fig,axes=plt.subplots(2,2,figsize=(12,4.6),constrained_layout=True)
    fields=('estimated_settle_budget_us','estimated_wire_budget_us','estimated_guard_budget_us','remaining_acquisition_us','mean_gap_us')
    labels=('Analog waits','Nominal SPI clocks','Configured MUX/CS guards','Remaining acquisition','Outside acquisition')
    for i,m in enumerate(MODES):
        for j,v in enumerate((False,True)):
            bottom=np.zeros(5);ax=axes[i,j]
            for field,label,c in zip(fields,labels,('#0072B2','#56B4E9','#E69F00','#009E73','#CC79A7')):
                y=np.array([metric(get(m,'lpspi',v,s),field) for s in DELAYS]);ax.bar(range(5),y,bottom=bottom,label=label,color=c);bottom+=y
            ax.set(xticks=range(5),xticklabels=DELAYS,xlabel='Matched delay (us)',ylabel='Sweep period (us)',title=m.upper()+' / VMID '+('on' if v else 'off'))
    axes[0,0].legend(fontsize=6);chart(save(fig,'timing_budget'),305)
    table(['Mode / VMID','Prod period us','Prod acquisition us','Prod gap us','Diag prepare us','Diag transfer us','Diag encode us','Diag enqueue us'],[
        [m.upper()+'/'+('on' if v else 'off')]+[n(metric(get(m,'lpspi',v),k),3) for k in ('mean_period_us','mean_acquisition_us','mean_gap_us')]+
        [n(metric(get(m,'lpspi',v,stage='diagnostic'),k),3) for k in ('profile_prepare_us','profile_transfer_us','profile_encode_us','profile_usb_write_us')]
        for m in MODES for v in (False,True)])
    p('Diagnostic probes add overhead; these phase means are not subtracted from production totals. Clock and guard budgets are arithmetic estimates, not measured pin timings. ADC conversion overlaps its SPI clocks; do not add a separate conversion duration to the clock budget.',True)

    page();h('Timing by address, ADC channel and full sweep')
    table(['Unit / fixed address','Useful values','Prime words','Total conversion words','Nominal SPI time at 16 / 8 MHz'],[
        ['One ADC, sensor address 0-5',4,1,5,'5 / 10 us'],['One ADC, sensor address 6',1,1,2,'2 / 4 us'],
        ['Both ADCs, sensor address 0-5',8,2,10,'10 / 20 us'],['Both ADCs, sensor address 6',2,2,4,'4 / 8 us'],
        ['One ADC, optional VMID batch',0,1,5,'5 / 10 us'],['Both ADCs, optional VMID batches',0,2,10,'10 / 20 us'],
        ['Full sensor sweep, VMID off',50,14,64,'64 / 128 us'],['Full sweep, VMID on',50,28,134,'134 / 268 us']])
    p('Each useful conversion occupies one 16-clock word: nominally 1 us at 16 MHz or 2 us at 8 MHz. This is conversion/clock time, not the elapsed time between revisits of that sensor. Each sensor is revisited once per complete sweep. A prime conversion is discarded at the start of each batch; actual preparation and batch software costs are additional.',True)
    p('ADC mode switches/settles 14 sensor bank-address groups; MUX mode switches the same 14 banks in 7 shared phases. VMID on adds 14 bank switches / 14 waits in ADC mode, or 14 bank switches / 7 waits in MUX mode. Coordinated switch guards cost 0.4 us per changed phase; CS setup/high allowances cost 0.2 us per burst batch. EN changes only for required address transitions. These are source-derived estimates, not oscilloscope edge measurements.',True)
    table(['Mode','Engine','VMID','Measured intercept us','Measured slope us/us','Expected wait slope','Fit residual max us'],[
        [r['mode'].upper(),r['engine'],'on' if r['vmid'] else 'off',n(r['intercept_us'],3),n(r['slope_us_per_us'],4),r['expected_slope'],n(r['residual_max_abs_us'],3)]
        for r in data.get('fits',[])],pad=2)
    p('Fit: period = intercept + slope x matched sensor/VMID delay. The intercept estimates total zero-configured-delay period; it cannot separately identify GPIO, per-word and per-batch costs. Details: delay_fits.csv.',True)

    page();h('Baseline differences between the four ADC input lanes')
    p('LPSPI; sensor = VMID settling = 5 us; 16 MHz. Each ADC input lane corresponds to one external MUX. Lane center is the median of that lane\'s 6 or 7 channel baselines. The spread across centers compares the four input groups. These are descriptive offsets in this disconnected setup.',True)
    records=[]
    for m in MODES:
        for v in (False,True):
            setting=get(m,'lpspi',v)
            for a in (1,2):
                selected=[g for g in data.get('groups',[]) if setting and key(g)==key(setting) and g['array']==a]
                lane=[next((g for g in selected if g['mux']==mux),{}) for mux in (1,2,3,4)]
                whole=next((g for g in selected if g['mux']==0),{})
                records.append([m.upper(),'on' if v else 'off','AB'[a-1]]+
                    [n(g.get('baseline_center_counts'),1)+' / '+n(g.get('baseline_center_volts'),6) for g in lane]+
                    [cv(whole.get('adc_lane_center_spread_counts'))])
    table(['Mode','VMID','Array','MUX1 center c / V','MUX2 center c / V','MUX3 center c / V','MUX4 center c / V','Lane-center spread c/mV'],records)
    p('mux_group_summary.csv contains every engine, delay and experiment. address_baseline_spread.csv compares ADC channels at each common external address: four lanes at inputs 0-5; input 6 has one populated lane, so its single-value spread is zero and does not measure inter-lane variation. The channel line graphs preserve every individual route.',True)

    page();h('Independent VMID wait - sensor settling fixed at 5 us')
    p('16 MHz; VMID on. Only the extra VMID analog wait changes. All 134 SPI conversion words, 28 burst batches, selector primes and switching guards remain.',True)
    records=[]
    for m in MODES:
        for e in ENGINES:
            control=get(m,e,True)
            for vd in (0,1,5):
                r=get(m,e,True,vmid_delay=vd)
                gain=100*(metric(control,'mean_period_us')/metric(r,'mean_period_us')-1)
                saved=metric(control,'mean_period_us')-metric(r,'mean_period_us')
                theoretical=(14 if m=='adc' else 7)*(5-vd)
                records.append([m.upper(),e,vd,n(metric(r,'delivered_sweeps_s'),1),n(gain,2),n(saved,3),theoretical,
                    cv(metric(r,'all_sd_max_counts')),cv(metric(r,'all_ptp_max_counts')),cv(metric(r,'A1_baseline_spread_counts')),cv(metric(r,'A2_baseline_spread_counts'))])
    table(['Mode','Engine','VMID us','Rate /ch','Gain % vs5','Saved us measured','Saved us estimated','Worst SD c/mV','Worst p-p c/mV','A spread c/mV','B spread c/mV'],records)
    p('VMID zero skips the configured analog wait, retaining mandatory switch guards and discarded VMID conversions. The transmitted payload contains sensor readings; discarded VMID responses themselves are not recorded or assessed for analog noise.',True)

    page();h('SPI clock comparison - 8 MHz versus 16 MHz')
    p('Sensor settling = VMID settling = 5 us. Both VMID off/on are tested. Requested clocks are reported; actual SCLK was not probed. Slower clocks also change ADC acquisition timing and analog history.',True)
    records=[]
    for m in MODES:
        for e in ENGINES:
            for v in (False,True):
                a=get(m,e,v,clock=8000000);b=get(m,e,v)
                records.append([m.upper(),e,'on' if v else 'off',n(metric(a,'delivered_sweeps_s'),1),n(metric(b,'delivered_sweeps_s'),1),
                    n(100*(metric(a,'mean_period_us')/metric(b,'mean_period_us')-1),2),cv(metric(a,'all_sd_max_counts')),cv(metric(b,'all_sd_max_counts')),
                    cv(metric(a,'all_ptp_max_counts')),cv(metric(b,'all_ptp_max_counts'))])
    table(['Mode','Engine','VMID','8 MHz rate','16 MHz rate','16 MHz gain %','8 MHz SD c/mV','16 MHz SD c/mV','8 MHz p-p c/mV','16 MHz p-p c/mV'],records)
    p('Nominal SPI clocking doubles from 64 to 128 us/sweep with VMID off, and from 134 to 268 us/sweep with VMID on. Total period includes settling and software overhead, so rate does not necessarily double at 16 MHz.',True)

    for reference,label in (('yesterday_shared','Yesterday\'s shared-MUX/burst firmware'),('yesterday_original','Yesterday\'s original sequential firmware')):
        page();h('Speed change versus '+label)
        p('Only 16 MHz settings with sensor = VMID settling are compared. '+('Same scan-mode semantics are matched.' if reference=='yesterday_shared' else 'The old scanorder mux means sequential arrays, equivalent to today\'s ADC mode. Today\'s MUX results compare against that sequential reference and include the change in scan behavior.'),True)
        fig,axes=plt.subplots(2,2,figsize=(12,5.5),constrained_layout=True)
        for i,m in enumerate(MODES):
            for j,v in enumerate((False,True)):
                ax=axes[i,j]
                for e,c in zip(ENGINES,PALETTE):
                    selected=[next((r for r in data['comparisons'] if (r['reference'],r['mode'],r['engine'],r['vmid'],r['settle_us'])==(reference,m,e,v,s)),{}) for s in DELAYS]
                    ax.plot(DELAYS,[r.get('speed_gain_percent',np.nan) for r in selected],'-o',label=e,color=c)
                ax.axhline(0,color='#555555',linewidth=.7);ax.set_xscale('log');ax.set_xlim(.75,65);ax.set_xticks(DELAYS,labels=DELAYS)
                ax.set(xlabel='Matched delay (us)',ylabel='Current rate gain (%)',title=m.upper()+' / VMID '+('on' if v else 'off'));ax.legend(fontsize=7)
        chart(save(fig,'history_speed_'+reference))
        page();h('Quiet noise change versus '+label)
        fig,axes=plt.subplots(2,2,figsize=(12,5.5),constrained_layout=True)
        for i,m in enumerate(MODES):
            for j,v in enumerate((False,True)):
                ax=axes[i,j]
                for e,c in zip(ENGINES,PALETTE):
                    selected=[next((r for r in data['comparisons'] if (r['reference'],r['mode'],r['engine'],r['vmid'],r['settle_us'])==(reference,m,e,v,s)),{}) for s in DELAYS]
                    ax.plot(DELAYS,[r.get('change_all_sd_max_counts',np.nan) for r in selected],'-o',label=e+' between',color=c)
                    delta=[metric(broad(get(m,e,v,s)),'all_sd_max_counts')-metric(broad(get(m,e,v,s,dataset=reference)),'all_sd_max_counts') for s in DELAYS]
                    ax.plot(DELAYS,delta,'--s',label=e+' broader',color=c,markersize=3)
                ax.axhline(0,color='#555555',linewidth=.7);ax.set_xscale('log');ax.set_xlim(.75,65);ax.set_xticks(DELAYS,labels=DELAYS)
                ax.set(xlabel='Matched delay (us)',ylabel='Change in worst route SD (counts)',title=m.upper()+' / VMID '+('on' if v else 'off'));ax.legend(fontsize=6,ncol=2)
        chart(save(fig,'history_noise_'+reference))
        p('Positive change means higher noise. Solid lines: common between-burst windows; dashed lines: broader 50 ms windows. Each worst value is selected independently over routes. Both methods reanalyze the historical raw data. '+('Yesterday\'s measurement windows were 2 s versus 15 s today; peak-to-peak extremes are not equal-length comparisons.' if reference=='yesterday_shared' else 'Both sessions have 15 s measurement windows, but retained quiet durations and sample counts differ.'),True)
        page();h('LPSPI comparison table - '+label)
        records=[]
        for m in MODES:
            for v in (False,True):
                for s in DELAYS:
                    r=next((r for r in data['comparisons'] if (r['reference'],r['mode'],r['engine'],r['vmid'],r['settle_us'])==(reference,m,'lpspi',v,s)),{})
                    records.append([m.upper(),'on' if v else 'off',s,n(r.get('old_rate_hz'),1),n(r.get('current_rate_hz'),1),n(r.get('speed_gain_percent'),2),
                        cv(r.get('old_all_sd_max_counts')),cv(r.get('current_all_sd_max_counts')),cv(r.get('old_all_ptp_max_counts')),cv(r.get('current_all_ptp_max_counts')),
                        str(r.get('old_qualified_repetitions','-'))+'/3'])
        table(['Mode','VMID','us','Old rate','Current rate','Gain %','Old SD c/mV','Current SD c/mV','Old p-p c/mV','Current p-p c/mV','Old quiet reps'],records)
        p('All three engines and every matched physical route are included in firmware_comparisons.csv and channel_comparisons.csv. Unqualified old noise records are excluded rather than replaced with zeros. Sequential-session environmental drift remains part of this comparison.',True)

    for m in MODES:
        for e in ENGINES:
            page();h('Every channel baseline - '+m.upper()+' / '+e)
            fig,axes=plt.subplots(2,2,figsize=(12,5.6),constrained_layout=True)
            for i,v in enumerate((False,True)):
                for j,a in enumerate((1,2)):
                    ax=axes[i,j]
                    for s,c in zip(DELAYS,DELAY_COLORS): ax.plot(range(25),points(m,e,v,s,array=a),'-o',markersize=2,linewidth=1,label=str(s)+' us',color=c)
                    baseline_axes(ax,a);ax.set_title('Array '+'AB'[a-1]+' / VMID '+('on' if v else 'off'));ax.legend(fontsize=6,ncol=5,loc='best')
            chart(save(fig,'baselines_'+m+'_'+e))
            p('16 MHz; sensor = VMID settling. Each point is the median quiet baseline for that physical route, aggregated by median over qualified repetitions. Lines connect discrete channels for comparison and do not imply spatial interpolation. Left axis counts; right axis volts.',True)

    for e in ENGINES:
        page();h('Every channel baseline - VMID and clock experiments / '+e)
        fig,axes=plt.subplots(2,2,figsize=(12,5.6),constrained_layout=True)
        for i,m in enumerate(MODES):
            for j,a in enumerate((1,2)):
                ax=axes[i,j]
                for vd,c in zip((0,1,5),PALETTE): ax.plot(range(25),points(m,e,True,vmid_delay=vd,array=a),'-o',markersize=2,linewidth=1,color=c,label='16 MHz / VMID '+str(vd)+' us')
                ax.plot(range(25),points(m,e,True,clock=8000000,array=a),'--o',markersize=2,linewidth=1,color='#CC79A7',label='8 MHz / VMID 5 us')
                baseline_axes(ax,a);ax.set_title(m.upper()+' / Array '+'AB'[a-1]);ax.legend(fontsize=6,ncol=2)
        chart(save(fig,'baseline_experiments_'+e));p('Sensor settling fixed at 5 us; VMID on. All four experimental settings are plotted in physical channel order.',True)
        page();h('Every channel baseline - 8/16 MHz with VMID off / '+e)
        fig,axes=plt.subplots(2,2,figsize=(12,5.6),constrained_layout=True)
        for i,m in enumerate(MODES):
            for j,a in enumerate((1,2)):
                ax=axes[i,j]
                for clock,c in zip((8000000,16000000),PALETTE): ax.plot(range(25),points(m,e,False,clock=clock,array=a),'-o',markersize=2,linewidth=1,color=c,label=str(clock//1000000)+' MHz')
                baseline_axes(ax,a);ax.set_title(m.upper()+' / Array '+'AB'[a-1]+' / VMID off');ax.legend(fontsize=7)
        chart(save(fig,'baseline_clock_vmid_off_'+e));p('Sensor settling = VMID settling = 5 us; VMID off. Both requested SPI clocks are shown for every physical channel in both scan modes.',True)

    for m in MODES:
        page();h('Every channel baseline - firmware history / LPSPI / '+m.upper())
        fig,axes=plt.subplots(2,2,figsize=(12,5.6),constrained_layout=True)
        for i,v in enumerate((False,True)):
            for j,a in enumerate((1,2)):
                ax=axes[i,j]
                for dataset,label,c in zip(('yesterday_original','yesterday_shared','current'),('Original sequential','Yesterday ADC/MUX burst','Current'),PALETTE):
                    ax.plot(range(25),points(m,'lpspi',v,dataset=dataset,array=a),'-o',markersize=2,linewidth=1,color=c,label=label)
                baseline_axes(ax,a);ax.set_title('Array '+'AB'[a-1]+' / VMID '+('on' if v else 'off'));ax.legend(fontsize=6)
        chart(save(fig,'baseline_history_'+m));p('Sensor = VMID settling = 5 us; 16 MHz. Original firmware is sequential for both comparisons. Yesterday\'s burst firmware matches the current scan mode. Every delay/engine route comparison is saved in CSV.',True)

    case_root=ROOT/data['raw_root']/'production'
    case_folders=[case_root/f'arrayboth__mux__lpspi__spi16000000__repeat1__settle5__vmid{state}__burst__vmidsettle5__rep1' for state in ('off','on')]
    if all((f/'capture.bin').exists() for f in case_folders):
        page();h('Fast coherent excursions hidden inside the 50 ms envelope')
        fig,axes=plt.subplots(2,2,figsize=(12,5.3),constrained_layout=True);case_rows=[]
        for j,(state,case) in enumerate(zip(('off','on'),case_folders)):
            frames=decode(case/'capture.bin');status=json.loads((case/'status.json').read_text())['after'];wire=status['payload_routes'].split(',')
            t=np.r_[0,np.cumsum(np.diff(frames['start'].astype(np.int64))%2**32)]/1e6
            for route,color in zip(('2:1:4','2:3:4'),PALETTE):
                values=frames['samples'][:,wire.index(route)].astype(float);bins=np.arange(1,16,.05)
                std=np.array([values[(t>=a)&(t<a+.05)].std(ddof=1) for a in bins])
                axes[0,j].plot(bins+.025,std,label=route,color=color,linewidth=.8)
                case_rows.extend(dict(vmid=state,route=route,repetition=1,start_s=float(a),end_s=float(a+.05),sd_counts=float(sd),sd_volts=float(sd)*LSB)
                    for a,sd in zip(bins,std))
            axes[0,j].set(xlabel='Time since sweep start (s)',ylabel='Per-channel 50 ms SD (counts)',title='VMID '+state,ylim=(0,1.6));axes[0,j].legend(fontsize=7)
            values=frames['samples'][:,wire.index('2:1:4')];measured=(t>=1)&(t<16);codes,counts=np.unique(values[measured],return_counts=True)
            axes[1,j].bar(codes,100*counts/counts.sum(),color=PALETTE[j],width=.7)
            axes[1,j].set(xlabel='ADC code (counts)',ylabel='Fraction of measured samples (%)',title='B / MUX1 / input4 code distribution')
            axes[1,j].set_xticks(codes.astype(int))
            axes[1,j].ticklabel_format(axis='x',useOffset=False)
        csv_write(folder/'noise_case_study_bins.csv',case_rows)
        chart(save(fig,'sustained_noise_case_study'),345)
        p('Illustrative first repetition, current MUX/LPSPI, 16 MHz, sensor=VMID settling=5 us. Routes 2:1:4 and 2:3:4 are B/MUX1/input4 and B/MUX3/input4 (both 1 MOhm bias). The earlier 50 ms mask retains these windows, but a finer trace reveals separated short excursions. Histograms include every measured sample. The broader-window metric includes these events; primary noise uses between-burst windows. This plot does not identify their electrical cause.',True)
        page();h('Common between-burst time windows - short trace')
        case=case_folders[0];frames=decode(case/'capture.bin');status=json.loads((case/'status.json').read_text())['after'];wire=status['payload_routes'].split(',')
        t=np.r_[0,np.cumsum(np.diff(frames['start'].astype(np.int64))%2**32)]/1e6
        mask_file=folder/('current__production__'+case.name+'__quiet_mask.npz')
        if mask_file.exists():
            saved=np.load(mask_file);mask=np.unpackbits(saved['final'])[:len(frames)].astype(bool)
            fig,axes=plt.subplots(2,1,figsize=(12,5.2),constrained_layout=True);clip=(t>=1)&(t<1.05);trace_rows=[]
            for ax,route in zip(axes,('2:1:4','2:3:4')):
                values=frames['samples'][:,wire.index(route)]
                ax.plot((t[clip]-1)*1000,values[clip],color='#999999',linewidth=.7)
                for keep,color,label in ((True,'#0072B2','Kept common quiet sweep'),(False,'#D55E00','Rejected whole sweep / guard')):
                    selected=clip&(mask==keep);ax.scatter((t[selected]-1)*1000,values[selected],s=9,color=color,label=label)
                ax.set(xlabel='Time after 1 s warm-up (ms)',ylabel='ADC code (counts)',title=route);ax.legend(fontsize=7,ncol=2)
                trace_rows.extend(dict(route=route,time_s=float(time),counts=int(value),common_quiet=bool(kept)) for time,value,kept in zip(t[clip],values[clip],mask[clip]))
            csv_write(folder/'noise_case_study_trace.csv',trace_rows);chart(save(fig,'burst_time_windows'),345)
        p('Same first VMID-off capture, first 50 ms after warm-up. Excursion intervals are selected from separated secondary states moving coherently on at least two routes. A whole sweep and one neighboring sweep on each side are rejected for every channel together. A point can be rejected because other channels trigger the common window; there is no independent clipping of this route. Full packed masks and detector states accompany every production capture.',True)

    pattern_file=folder/'noise_patterns.json'
    pulse_file=folder/'noise_pulse_case.json'
    if pattern_file.exists() and pulse_file.exists():
        patterns=json.loads(pattern_file.read_text());pulses=json.loads(pulse_file.read_text())
        page();h('Unfiltered raw noise - recurrent pulses and cross-channel motion')
        chart(figures/'noise_patterns_raw.png',375)
        p('These figures use all samples after warm-up, including excursions. At MUX/LPSPI, 16 MHz, sensor settling 5 us and VMID off, B/MUX1/input4 and B/MUX3/input4 rise together. A/MUX1/input4 and A/MUX3/input4 show shared lower-code motion. The pulses recur throughout all three 15 s repetitions. Route notation is array:MUX:external address, with addresses numbered 0-6; input4 is external address 4, not ADC channel 4. One count = 0.805664 mV.',True)
        page();h('Spectral and temporal evidence - this case is not white noise')
        chart(figures/'noise_patterns_spectrum.png',345)
        table(['Rep','B/MUX1/input4 SD counts','p-p counts','Higher-state samples %','Events / 15 s','Median duration us','B1/B3 correlation','Lag-1 correlation'],[
            [r['repetition'],n(r['sd_counts'],4),n(r['ptp_counts'],0),n(100*r['higher_state_fraction'],2),r['events'],
             n(r['median_event_duration_us'],1),n(r['cross_route_correlation'],4),n(r['lag1_acf'],3)] for r in pulses])
        p('The higher state is defined here as B/MUX1/input4 >=1961 counts, for this fixed-baseline case only. Most pulses occupy three consecutive samples; some are single-sweep spikes. Welch spectra show components near 330-360 Hz and 1 kHz. These frequencies are sampled-domain observations; aliasing and the lack of per-channel aperture timestamps limit source identification. PSD excludes dropped-frame gaps; small within-run timing jitter remains. Spectrogram/autocorrelation use approximate sweep timing.',True)
        page();h('Pattern changes with acquisition settings')
        chart(figures/'noise_patterns_settings.png',340)
        p('First repetition; same physical route B/MUX1/input4. Each trace is referenced to its own full-capture median, because scan settings also shift baseline. All sensor and VMID delays are 5 us. VMID-on results use 16 MHz. These are unfiltered samples; the axes share a scale. Full 15 s statistics and per-route lag-1 correlation for all 252 production captures are saved in unfiltered_noise_channels.csv.',True)
        p('A nearly constant digital code, as in this VMID-on route, does not demonstrate zero analog noise or establish whiteness. Other channels can retain one-code toggling, structured variation or wider noise. Slower SPI and longer waits are not uniformly quieter for every route.',True)
        page();h('Wider noise at short delays; a quiet white-like example')
        chart(figures/'noise_patterns_wide_and_quiet.png',365)
        p('All unfiltered samples in the first repetition. The 1 us MUX/LPSPI case reaches 17 counts p-p (13.696 mV), with strong correlation and spectral structure. ADC/DMA at 2 us also shows spectral components rather than featureless white noise; repetitions differ in its amplitude and spectrum. In contrast, B/MUX2/address4 at MUX/LPSPI/5 us/VMID on toggles between adjacent codes with SD 0.487 counts, a relatively flat spectrum and lag-1 correlation -0.0006. Its largest absolute autocorrelation over lags 1-64 is 0.0112: this route is consistent with a white-like quantized background, without proving the analog noise is exactly white.',True)
        page();h('Whole-matrix noise findings and the TPS7A2033 LDO')
        worst=sorted(patterns['captures'],key=lambda r:r['worst_sd_counts'],reverse=True)[:9]
        table(['Mode','Engine','SPI MHz','Sensor/VMID us','VMID','Rep','Worst SD route','Unfiltered SD counts/mV','Largest route p-p counts/mV','Lag-1'],[
            [r['scanorder'].upper(),r['spiengine'],r['spi_clock_hz']//1000000,f"{r['mux_settle_us']}/{r['vmid_settle_us']}",
             'on' if r['vmid'] else 'off',r['repetition'],r['worst_route'],cv(r['worst_sd_counts']),cv(r['worst_ptp_counts']),n(r['worst_route_lag1_acf'],3)] for r in worst])
        p('All 252 unfiltered production captures were checked. The previous eight-group 50 ms envelope detector retained all complete measurement bins: it found no long envelope bursts under that rule. This does not rule out channel-specific events. Short recurrent excursions are present, and some short-delay settings show much wider structured noise. Quiet-only numbers must therefore be accompanied by raw excursion burden and spectra.',True)
        p('Across the entire current matrix, the largest per-route observed range was 17 counts (13.696 mV), and the largest absolute excursion from that route\'s median was 11 counts (8.862 mV). These are digital observations during the saved windows; they do not exclude unsampled faster spikes.',True)
        p('The installed LDO is TPS7A2033PDBVR (user-confirmed). TI lists typical PSRR of 95 dB at 1 kHz with 20 mA load and input 1 V above output. Rejection depends on frequency, load, headroom and implementation; it is not complete isolation. The datasheet also lists 7 uV RMS output noise at 2.8 V/300 mA over 10 Hz-100 kHz. Those specifications are not a measurement of this board or its 3.3 V reference.',True)
        p('Source: <link href="https://www.ti.com/lit/ds/symlink/tps7a20.pdf" color="#0072B2">Texas Instruments TPS7A20 datasheet, electrical characteristics and layout guidance</link>.',True)
        p('Interpretation: the setting dependence, shared route motion and non-flat spectrum make a purely independent white-noise model inadequate. Candidate causes include acquisition/settling effects, reference or ground coupling, digital switching and external interference. The observed 1 kHz component alone cannot identify USB or charger coupling. Direct ripple through the LDO is not established. Rail/VMID probing synchronized with SPI/MUX activity and a controlled charger-on/off repeat would distinguish these hypotheses.',True)

    selected_file=folder/'selected_noise_1us_5us.json'
    if selected_file.exists():
        selected=json.loads(selected_file.read_text())
        page();h('Complete route audit - non-white noise above 0.5 counts SD')
        p('Scope: current captured firmware adc124-vmid-settle-v1; shared MUX scan, LPSPI, requested 16 MHz, VMID off, sensor settling 1 us or 5 us. Both disconnected arrays, all 50 physical routes at each delay, all three production repetitions. No additional hardware acquisition was performed for this extension.')
        p('Selection uses unfiltered 15 s sample SD (ddof=1), after the 1 s warm-up. Median SD across three repeats must be strictly above 0.5 counts. A reproducible temporal pattern requires max absolute normalized autocorrelation over lags 1-64 >=0.05 in at least two repeats. PSD and raw time plots accompany every selected route. This is a descriptive structure screen; it does not identify an electrical source or establish whiteness for excluded routes.',True)
        table(['Settle us','Array','MUX / ADC input','External address','Sensor / channel','SD median counts','SD repeat range counts','SD median mV','Lag-1 median'],[
            [r['settle_us'],'A (1)' if r['array']==1 else 'B (2)',f"MUX{r['mux']} / {r['adc_input']}",r['external_address'],
             f"{r['sensor']} / {r['sensor_channel']}",n(r['sd_median_counts'],4),n(r['sd_min_counts'],4)+'-'+n(r['sd_max_counts'],4),
             n(r['sd_median_volts']*1000,4),n(r['lag1_median'],3)] for r in selected['selected']])
        p('Result: 4 routes at 1 us and 9 routes at 5 us meet both criteria; each exceeds 0.5 counts in all three repetitions. External addresses are zero-based 0-6. Sensor/channel names identify connector positions even though sensors are disconnected. Firmware wiring maps MUX1..4 to ADC IN1..4. The 0.5-count cutoff is 0.402832 mV SD at the nominal 3.3 V reference.',True)
        p('Examples below the amplitude cutoff can still be non-white. Array A/MUX4/address4 at 5 us, for example, has correlated variation but SD below 0.5 counts and is excluded from this requested list. Full audit: selected_noise_channel_audit.csv (300 channel/repetition records) and selected_noise_route_summary.csv (100 route/delay summaries).',True)
        for figure in selected['figures']:
            page();h(f"{figure['settle_us']} us settling - selected channel time traces and PSD")
            chart(folder/figure['path'],figure['height'])
            p('Every plotted route meets the SD and reproducible-pattern criteria. Left: first 25 ms after warm-up, raw samples referenced to that repetition\'s full-capture median; no smoothing or clipping. Right: Welch PSD from each 15 s capture, in counts squared/Hz. Colors show repetitions 1/2/3. Baseline offsets are removed only for display; unfiltered SD includes every measured sample.',True)
            p('PSD uses 4096-sample Hann windows with 50% overlap and constant detrending. Split dropped-frame gaps >1.5 times the median period; exclude sections shorter than 4096 samples. Across these captures, 88.8-100% of frames lie in eligible sections; incomplete window tails are also unused. Coverage fractions are recorded in selected_noise_channel_audit.csv. Small normal timing jitter remains. Frequencies use the approximate mean sweep rate; PSD includes Nyquist. Numerical time/PSD tables also supply volts squared/Hz.',True)

    page();h('Quiet-period selection and statistical definitions')
    p('Exclude the first 1 s of each run. In complete 50 ms bins, calculate each channel\'s sample SD (ddof=1), then take its median within each of the eight array/MUX groups. For each group, use threshold max(0.6 counts, P20+0.15, lower-40% median + 4 x 1.4826 x MAD). Reject any bin exceeding any group threshold, plus its adjacent bins. Apply the same retained time mask to all 50 channels.')
    p('This first stage identifies longer envelope bursts. A second stage detects shorter coherent excursions. A one-code modal state containing >=60% of coarse-window samples, or an adjacent two-code state containing >=80%, anchors detection. Allow one count outside that state. An excursion direction must contain >=1% of samples beyond the allowed edge and a separated histogram peak >=0.5% of samples and >=3 times the edge density. At least two routes must depart in the same sweep; reject that entire sweep and one neighboring sweep each side.',True)
    p('The same final sweep mask applies to all 50 channels. There is no independent channel clipping, smoothing or detrending. Unimodal Gaussian tails and isolated channel variation remain. Charger switching was not recorded, so the detector classifies coherent excursions without proving their source. Both the broader first-stage metrics and final conditional metrics remain available. A record qualifies with >=1 s and >=100 retained frames. Fine quiet seconds sum retained sweep intervals capped at the median sweep period, excluding credit for missing frames.',True)
    p('Baseline: median of retained samples per physical route. Temporal noise SD: sample SD about that route\'s retained mean. Peak-to-peak: retained max-min. Per-setting metrics: median of the three per-route repetition metrics. Baseline spread: max-min across those channel baselines; baseline population SD across channels is saved separately. Neither baseline spread nor its population SD is temporal noise.')
    p('Counts to volts uses the nominal reference, without calibration or reference-rail measurement. A reported zero SD means every retained digital code was identical; it does not establish zero analog noise. Peak-to-peak depends on observation duration and the number of sampled extremes. Repetition min/max fields show variation between captures.')
    p('In MUX mode, both banks share the configured settling wait, but ADC B is read after ADC A. Bank B therefore continues settling during the A batch. Equal delay settings across modes do not guarantee equal analog dwell for every route; baseline and noise comparisons retain those scheduling effects.',True)
    unqualified=[r for r in rows if r['stage']=='production' and not r.get('quiet_qualified',False)]
    table(['Dataset','Production records','Qualified quiet records','Measurement s'],[
        [ds,len([r for r in rows if r['dataset']==ds and r['stage']=='production']),
         sum(r.get('quiet_qualified',False) for r in rows if r['dataset']==ds and r['stage']=='production'),
         '2' if ds=='yesterday_shared' else '15'] for ds in ('current','yesterday_shared','yesterday_original')])
    if unqualified: p('Unqualified records: '+str(len(unqualified))+'. They remain in capture_metrics.csv; their noise values are not used in channel summaries. Comparisons show the number of qualified reference repetitions.',True)
    h('Timing limits and optimization evidence')
    p('Steady-state current ADC mode has 14 sensor settling windows/sweep; MUX mode has 7. VMID on adds 14/7 VMID waits. VMID off has 64 conversion words and 14 burst batches; VMID on has 134 words and 28 batches. The preparation/stop parking waits are outside the steady-state model. Guard arithmetic preserves EN polarity and configured 200 ns turn-off/turn-on allowances.')
    p('Current firmware uses bounded LPSPI FIFO bursts and grouped GPIO writes. Measured residuals include GPIO/register work, batch setup, loop costs, interrupts and timestamp quantization; they are not exact per-channel aperture times. Independently aggregated component medians may not sum exactly to the median total. USB enqueue timing is not end-to-end host latency. Hardware edge timing, crosstalk and connected-sensor analog behavior require separate measurements.')

    page();h('Evidence, reproducibility and final board state')
    p('Current raw root: '+data['raw_root'])
    for ref in data['reference_roots']:p('Verified reference raw root: '+ref)
    p('Current sources, production and diagnostic HEX images and SHA-256 identities are frozen under current_firmware/. Command logs, status before/after every capture, raw binary streams, independent parser checks, profiling summaries and lifecycle records accompany the data. Hashes identify the local builds; initial production identity was checked through its runtime revision and reported policies, without MCU image readback. The frozen diagnostic and restored production images are uploaded by this session.')
    final=data['lifecycle'].get('final_production_status',{})
    p('Final lifecycle: '+data['lifecycle']['state']+'. Production revision: '+final.get('firmware_revision','pending')+'. Acquisition running: '+final.get('running','pending')+'. Original settings restored: '+
      ', '.join(k+'='+final.get(k,'pending') for k in ('array','scanorder','spiengine','spi_clock_hz','mux_settle_us','vmid_settle_us','vmid_settle_mode','vmid_between_groups'))+'.')
    broader_pairs=[]
    for r in data['comparisons']:
        cur=broad(get(r['mode'],r['engine'],r['vmid'],r['settle_us']))
        old=broad(get(r['mode'],r['engine'],r['vmid'],r['settle_us'],dataset=r['reference']))
        pair={k:r[k] for k in ('mode','engine','vmid','settle_us','reference')}
        for field in ('all_sd_max_counts','all_sd_median_counts','all_ptp_max_counts'):
            pair['current_'+field]=metric(cur,field);pair['old_'+field]=metric(old,field);pair['change_'+field]=metric(cur,field)-metric(old,field)
        broader_pairs.append(pair)
    csv_write(folder/'firmware_broader_comparisons.csv',broader_pairs)
    table(['File','Contents'],[['capture_metrics.csv','Every raw capture: timing, transport counters, quiet qualification, estimates and diagnostic phases'],
        ['summary.csv','Every setting: rates, timing budgets, quiet noise, baseline spread and repetition ranges'],
        ['channel_repetitions.csv / channel_summary.csv','All 50 physical routes, in counts and volts; baseline, SD and peak-to-peak'],
        ['firmware_comparisons.csv / channel_comparisons.csv','Matched-delay firmware speed/noise comparisons and route baseline shifts'],
        ['mux_group_summary.csv / address_baseline_spread.csv','Within-MUX, across-lane and same-address baseline spreads'],
        ['delay_fits.csv','Measured delay slopes/intercepts and expected steady-state wait counts'],
        ['broader_window_summary.csv / broader_window_channel_summary.csv','The earlier 50 ms-window noise/baseline metrics, retained alongside between-burst results'],
        ['firmware_broader_comparisons.csv','Matched-setting broader-window noise comparisons for both historical references'],
        ['*quiet_mask.npz / *fine_quiet_states.json','Raw-frame-indexed common masks and reproducible separated-state detector parameters'],
        ['unfiltered_noise_channels.csv / noise_patterns.json','All-sample temporal noise, excursions, lag-1 correlation and spectral diagnostics'],
        ['selected_noise_*','Complete 1/5 us MUX/LPSPI/VMID-off route audit, selected cases, raw zoom traces and PSD'],
        ['figures/','All report figures and extra VMID-off 8/16 MHz baseline line plots'],
        ['analysis.json / analysis_cache.json','Structured results and raw-hash-bound analysis cache']],[230,528])
    p('Reproduce with Arduino_Sketches/TestBoard_ADC124/benchmarks/benchmark_adc124_upgrade.py, Arduino_Sketches/TestBoard_ADC124/benchmarks/analyze_adc124_upgrade.py and Arduino_Sketches/TestBoard_ADC124/benchmarks/report_adc124_upgrade.py. No firmware source changes, commit or push were made for this measurement session. The disconnected-array captures should be preserved as the reference for the later sensor-connected experiment.')
    def footer(canvas,doc):
        canvas.saveState();canvas.setFont('Helvetica',7);canvas.setFillColor(colors.HexColor('#536575'))
        canvas.drawString(42,22,'TestBoard_ADC124 | disconnected arrays | 2026-10-14'+(' | PREVIEW' if preview else ''))
        canvas.drawRightString(800,22,str(doc.page));canvas.restoreState()
    pdf.parent.mkdir(parents=True,exist_ok=True)
    SimpleDocTemplate(str(pdf),pagesize=landscape(A4),leftMargin=42,rightMargin=42,topMargin=35,bottomMargin=36,title=title,author='ADC124 benchmark').build(story,onFirstPage=footer,onLaterPages=footer)
    print('REPORT',pdf,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path);parser.add_argument('pdf',type=Path)
    parser.add_argument('--preview',action='store_true');args=parser.parse_args();build(args.folder,args.pdf,args.preview)
